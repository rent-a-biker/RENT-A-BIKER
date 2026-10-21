#!/usr/bin/env python3
"""Smoke test: M8 legacy import — static-app-era signup CSVs into accounts.

Starts a stub Nominatim server (no real network), then the API with a
throwaway SQLite DB + env-seeded admin, and:

  1. checks the version, the served frontend's import markers, and the
     OpenAPI contract for the template downloads + POST /admin/import;
  2. downloads both CSV templates and checks their exact header rows;
  3. imports a 2-row rider CSV: accounts are created, temp passwords from
     the result log the new riders in, and re-importing skips them;
  4. imports a biker CSV: profiles land as pending + imported=True in the
     review queue with bike data, unavailable, unsigned agreements;
  5. checks per-row validation: missing name, bad email, missing bike
     fields, bad state, duplicate email inside the file — bad rows are
     listed in errors and never block good rows;
  6. checks structural failures: wrong header -> 400, non-UTF-8 -> 400,
     file over the 5 MB cap -> 400;
  7. checks rider -> 403, anonymous -> 401, and runs a clean branding scan.

Run from hidden_files/src/:  ./.venv/bin/python tests/smoke_legacy_import.py
"""
import io
import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # src/
API_PORT = 8501
STUB_PORT = 8502
BASE = f"http://127.0.0.1:{API_PORT}"
CHECKS: list[tuple[str, bool, str]] = []


class StubHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps([]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def check(name: str, cond: bool, detail: str = "") -> None:
    CHECKS.append((name, cond, detail))
    print(("PASS" if cond else "FAIL"), "-", name, ("| " + detail if detail else ""))


def api(method: str, path: str, token: str | None = None, body=None, form: bool = False):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw) if raw else None
        except Exception:
            return e.code, raw


def get_raw(path: str, token: str | None = None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, hdrs, resp.read().decode()
    except urllib.error.HTTPError as e:
        hdrs = {k.lower(): v for k, v in e.headers.items()}
        return e.code, hdrs, e.read().decode()


def post_multipart(path: str, token: str | None, fields: dict, files: dict):
    boundary = "RABTestBoundary9f8e7d"
    body = b""
    for name, value in fields.items():
        body += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode()
    for name, (filename, content, ctype) in files.items():
        body += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n'
        ).encode() + content + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw) if raw else None
        except Exception:
            return e.code, raw


def wait_for_server(port: int, timeout_s: int = 30) -> bool:
    for _ in range(timeout_s * 4):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                if port == API_PORT:
                    s, _ = api("GET", "/health")
                    if s == 200:
                        return True
                else:
                    return True
        except OSError:
            pass
        time.sleep(0.25)
    return False


def register(email, role, name, phone=None):
    body = {"email": email, "password": "password-123",
            "full_name": name, "role": role}
    if phone:
        body["phone"] = phone
    s, u = api("POST", "/auth/register", body=body)
    assert s in (200, 201), f"register {email} failed: {s} {u}"
    s, t = api("POST", "/auth/login", form=True,
               body={"username": email, "password": "password-123"})
    assert s == 200, f"login {email} failed: {s} {t}"
    return t["access_token"], u["id"]


def login(email, password):
    s, t = api("POST", "/auth/login", form=True,
               body={"username": email, "password": password})
    assert s == 200, f"login {email} failed: {s} {t}"
    return t["access_token"]


def main() -> int:
    stub = HTTPServer(("127.0.0.1", STUB_PORT), StubHandler)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    if not wait_for_server(STUB_PORT):
        print("FAIL - stub geocoder did not start")
        return 1

    tmp = __import__("tempfile").mkdtemp(prefix="rab_import_test_")
    db_path = os.path.join(tmp, "test.db")
    upload_dir = os.path.join(tmp, "uploads")
    admin_password = "import-admin-pw"
    env = dict(
        os.environ,
        RAB_DATABASE_URL=f"sqlite:///{db_path}",
        RAB_ADMIN_EMAIL="import.admin@example.com",
        RAB_ADMIN_PASSWORD=admin_password,
        RAB_GEOCODER_BASE_URL=f"http://127.0.0.1:{STUB_PORT}",
        RAB_GEOCODER_MIN_INTERVAL_SECONDS="0",
        RAB_UPLOAD_DIR=upload_dir,
    )
    venv_py = os.path.join(HERE, ".venv", "bin", "python")
    server = subprocess.Popen(
        [venv_py, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(API_PORT), "--log-level", "warning"],
        cwd=HERE, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_for_server(API_PORT):
            print("FAIL - server did not start")
            return 1

        # --- 1. version + frontend markers + OpenAPI contract ---
        s, h = api("GET", "/health")
        check("health ok, version 0.21.0",
              s == 200 and h.get("version") == "0.21.0", str(h))

        s, _, html = get_raw("/")
        check("frontend served", s == 200 and "<html" in html)
        markers = [
            'id="tplRidersBtn"',
            'id="tplBikersBtn"',
            'id="importKind"',
            'id="importFile"',
            'id="importUploadBtn"',
            'id="importCredsBtn"',
            "async function downloadImportTemplate",
            "async function uploadImport",
            "/admin/import/templates/",
            "'/admin/import'",
        ]
        for m in markers:
            check(f"frontend marker present: {m[:40]}", m in html)

        s, schema = api("GET", "/openapi.json")
        paths = schema.get("paths", {}) if s == 200 else {}
        check("OpenAPI has GET /admin/import/templates/{kind}.csv",
              "/admin/import/templates/{kind}.csv" in paths
              and "get" in paths["/admin/import/templates/{kind}.csv"])
        check("OpenAPI has POST /admin/import",
              "/admin/import" in paths and "post" in paths["/admin/import"])

        # --- 2. templates ---
        admin_tok = login("import.admin@example.com", admin_password)
        s, hdrs, body = get_raw("/admin/import/templates/riders.csv", token=admin_tok)
        lines = [ln for ln in body.splitlines() if ln.strip()]
        check("riders template -> 200 csv", s == 200
              and hdrs.get("content-type", "").startswith("text/csv"),
              str(hdrs.get("content-type")))
        check("riders template header exact",
              lines == ["full_name,email,phone,city,state"], str(lines))
        check("riders template attachment disposition",
              "import-riders-template" in hdrs.get("content-disposition", ""))

        s, _, body = get_raw("/admin/import/templates/bikers.csv", token=admin_tok)
        lines = [ln for ln in body.splitlines() if ln.strip()]
        check("bikers template header exact",
              s == 200 and lines == ["full_name,email,phone,city,state,bike_year,"
                                     "bike_make,bike_model,service_radius_miles"],
              str(lines))

        s, _ = api("GET", "/admin/import/templates/drivers.csv", token=admin_tok)
        check("unknown template kind -> 404", s == 404, f"status={s}")

        rider_tok, _ = register("import.rider@example.com", "rider",
                                "Import Rider")
        s, _ = api("GET", "/admin/import/templates/riders.csv", token=rider_tok)
        check("template download as rider -> 403", s == 403, f"status={s}")
        s, _, _ = get_raw("/admin/import/templates/riders.csv")
        check("template download anonymous -> 401", s == 401, f"status={s}")

        # --- 3. rider import ---
        rider_csv = (
            "full_name,email,phone,city,state\r\n"
            "Legacy Rider One,riderone@example.com,555-1001,Springfield,MO\r\n"
            "Legacy Rider Two,ridertwo@example.com,,Ozark,MO\r\n"
        ).encode()
        s, res = post_multipart("/admin/import", admin_tok,
                                {"kind": "riders"},
                                {"file": ("riders.csv", rider_csv, "text/csv")})
        check("rider import -> 201", s == 201, f"status={s}")
        check("rider import counts",
              res["kind"] == "riders" and res["rows_total"] == 2
              and res["created"] == 2 and res["skipped_existing"] == 0
              and res["errors"] == [], str(res))
        check("rider import credentials shape",
              len(res["credentials"]) == 2
              and all(c["temp_password"] for c in res["credentials"]))

        # the temp passwords actually sign the new riders in
        for cred in res["credentials"]:
            tok = login(cred["email"], cred["temp_password"])
            s2, me = api("GET", "/auth/me", token=tok)
            check(f"imported {cred['email']} signs in as rider",
                  s2 == 200 and me["role"] == "rider", str(me.get("role")))

        # re-importing the same file skips both (idempotent)
        s, res2 = post_multipart("/admin/import", admin_tok,
                                 {"kind": "riders"},
                                 {"file": ("riders.csv", rider_csv, "text/csv")})
        check("rider re-import -> 201, both skipped",
              s == 201 and res2["created"] == 0 and res2["skipped_existing"] == 2,
              str(res2))

        # --- 4. biker import ---
        biker_csv = (
            "full_name,email,phone,city,state,bike_year,bike_make,bike_model,service_radius_miles\r\n"
            "Legacy Biker,legacybiker@example.com,555-2001,Joplin,MO,2021,Harley-Davidson,Road Glide,60\r\n"
        ).encode()
        s, res = post_multipart("/admin/import", admin_tok,
                                {"kind": "bikers"},
                                {"file": ("bikers.csv", biker_csv, "text/csv")})
        check("biker import -> 201, created 1", s == 201 and res["created"] == 1,
              str(res))
        s, pending = api("GET", "/admin/bikers?approval_status=pending",
                         token=admin_tok)
        prof = next((p for p in pending
                     if p["user"]["email"] == "legacybiker@example.com"), None)
        check("imported biker profile in review queue", prof is not None)
        if prof:
            check("profile pending + imported flag",
                  prof["approval_status"] == "pending" and prof["imported"] is True,
                  str((prof["approval_status"], prof.get("imported"))))
            check("profile bike data + area",
                  prof["bike_make"] == "Harley-Davidson"
                  and prof["bike_model"] == "Road Glide"
                  and prof["bike_year"] == 2021
                  and prof["city"] == "Joplin" and prof["state"] == "MO"
                  and prof["service_radius_miles"] == 60)
            check("profile unavailable, no agreements signed",
                  prof["is_available"] is False
                  and prof["pledge_accepted"] is False
                  and prof["liability_accepted"] is False
                  and prof["has_endorsement"] is False)
            check("profile coords empty on stub geocoder",
                  prof["latitude"] is None and prof["longitude"] is None)
        cred = res["credentials"][0]
        tok = login(cred["email"], cred["temp_password"])
        s2, me = api("GET", "/auth/me", token=tok)
        check("imported biker signs in as biker",
              s2 == 200 and me["role"] == "biker", str(me.get("role")))
        # imported-but-unapproved biker does not appear in dispatch
        s3, dispatch = api("GET", "/admin/dispatch", token=admin_tok)
        in_dispatch = any(c["email"] == "legacybiker@example.com"
                          for r in dispatch["requests"] for c in r["candidates"])
        check("imported biker not in dispatch candidates", s3 == 200
              and not in_dispatch)

        # --- 5. per-row validation ---
        bad_csv = (
            "full_name,email,phone,city,state,bike_year,bike_make,bike_model,service_radius_miles\r\n"
            ",noname@example.com,555-1,Springfield,MO,2020,Harley-Davidson,Street Glide,\r\n"
            "Bad Email,not-an-email,555-2,Springfield,MO,2020,Harley-Davidson,Street Glide,\r\n"
            "No Bike,nobike@example.com,555-3,Springfield,MO,2020,,,\r\n"
            "Bad State,badstate@example.com,555-4,Springfield,Missouri,2020,Harley-Davidson,Street Glide,\r\n"
            "Bad Year,badyear@example.com,555-5,Springfield,MO,19x1,Harley-Davidson,Street Glide,\r\n"
            "Good Biker,goodbiker@example.com,555-6,Springfield,MO,2022,Indian,Scout,\r\n"
            "Dupe In File,goodbiker@example.com,555-7,Springfield,MO,2022,Indian,Scout,\r\n"
        ).encode()
        s, res = post_multipart("/admin/import", admin_tok,
                                {"kind": "bikers"},
                                {"file": ("bad.csv", bad_csv, "text/csv")})
        check("mixed import -> 201", s == 201, f"status={s}")
        check("mixed import: 1 created, 1 dup skipped, 5 error rows",
              res["created"] == 1 and res["skipped_existing"] == 1
              and len(res["errors"]) == 5, str(res))
        err_rows = {e["row"]: e["message"] for e in res["errors"]}
        check("error rows numbered 2-6",
              set(err_rows) == {2, 3, 4, 5, 6}, str(err_rows))
        check("error messages specific",
              "full_name" in err_rows[2] and "email" in err_rows[3]
              and "bike_make" in err_rows[4] and "state" in err_rows[5]
              and "bike_year" in err_rows[6], str(err_rows))
        check("only the good row got credentials",
              len(res["credentials"]) == 1
              and res["credentials"][0]["email"] == "goodbiker@example.com",
              str(res["credentials"]))
        check("default radius 50 when column blank",
              any(p["user"]["email"] == "goodbiker@example.com"
                  and p["service_radius_miles"] == 50
                  for p in api("GET", "/admin/bikers?approval_status=pending",
                               token=admin_tok)[1] or []))

        # --- 6. structural failures ---
        wrong_header = ("name,email_address\r\nBob,bob@example.com\r\n").encode()
        s, res = post_multipart("/admin/import", admin_tok,
                                {"kind": "riders"},
                                {"file": ("bad.csv", wrong_header, "text/csv")})
        check("missing required column -> 400",
              s == 400 and "email" in str(res.get("detail", "")), f"{s} {res}")
        s, res = post_multipart("/admin/import", admin_tok,
                                {"kind": "riders"},
                                {"file": ("empty.csv", b"", "text/csv")})
        check("empty file -> 400", s == 400, f"{s} {res}")
        s, res = post_multipart("/admin/import", admin_tok,
                                {"kind": "riders"},
                                {"file": ("bin.csv", b"\xff\xfe bad bytes", "text/csv")})
        check("non-UTF-8 file -> 400", s == 400, f"{s} {res}")
        big = b"full_name,email\r\n" + b"x,y@example.com\r\n" * 10
        s, res = post_multipart("/admin/import", admin_tok,
                                {"kind": "riders"},
                                {"file": ("big.csv", b"0" * (6 * 1024 * 1024),
                                          "text/csv")})
        check("oversize file -> 400", s == 400, f"{s} {res}")
        s, res = post_multipart("/admin/import", admin_tok,
                                {"kind": "nonsense"},
                                {"file": ("r.csv", rider_csv, "text/csv")})
        check("bad kind -> 422", s == 422, f"{s} {res}")

        # --- 7. guards + branding ---
        s, res = post_multipart("/admin/import", rider_tok,
                                {"kind": "riders"},
                                {"file": ("r.csv", rider_csv, "text/csv")})
        check("import as rider -> 403", s == 403, f"status={s}")
        s, res = post_multipart("/admin/import", None,
                                {"kind": "riders"},
                                {"file": ("r.csv", rider_csv, "text/csv")})
        check("import anonymous -> 401", s == 401, f"status={s}")

        banned = ["uber", "lyft"]
        dirty = []
        for root, _, files in os.walk(HERE):
            if ".venv" in root or "__pycache__" in root:
                continue
            for fn in files:
                if not fn.endswith((".py", ".html")) or fn.startswith("smoke_"):
                    continue
                p = os.path.join(root, fn)
                with open(p, encoding="utf-8") as f:
                    text = f.read().lower()
                for word in banned:
                    if word in text:
                        dirty.append(f"{p}: {word}")
        check("branding scan clean (app source only)",
              not dirty, "; ".join(dirty[:3]))

        # --- 8. OpenAPI coverage of the new JS endpoints ---
        admin_paths = {p for p in paths if p.startswith("/admin/")}
        js_paths = {"/admin/import", "/admin/import/templates/"}
        missing = {p for p in js_paths
                   if not any(q.startswith(p.rstrip("/")) for q in admin_paths)}
        check("frontend JS admin endpoints covered by OpenAPI",
              not missing, str(missing))
    finally:
        server.terminate()
        server.wait(timeout=10)

    fails = [c for c in CHECKS if not c[1]]
    print(f"\n{len(CHECKS) - len(fails)}/{len(CHECKS)} checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
