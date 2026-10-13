#!/usr/bin/env python3
"""Smoke test: M3 rider-experience polish — ride status detail.

Starts a stub Nominatim server (no real network), then the API with a
throwaway SQLite DB + env-seeded admin, and:

  1. checks the served frontend renders the countdown, "Your biker" box,
     and status-detail UI (markers + 60s ticker);
  2. verifies the OpenAPI contract: RideRequestOut carries status_detail
     and assigned_biker; AssignedBikerBrief has the right fields and no
     contact/insurance fields;
  3. walks a ride pending -> matched -> cancelled, checking the enriched
     fields on /rides/me, GET /rides/{id}, and the biker's /rides/assigned;
  4. walks a second ride to completion and checks the completed wording;
  5. checks privacy: an unrelated rider gets 403 on the ride and never
     sees it in /rides/me; anonymous -> 401;
  6. runs a clean branding scan.

Run from hidden_files/src/:  ./.venv/bin/python tests/smoke_ride_status.py
"""
import datetime
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
API_PORT = 8505
STUB_PORT = 8506
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


def api_raw(method: str, path: str, token: str, data: bytes, ctype: str):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": ctype}
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
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


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


PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
    b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def multipart_png(data: bytes):
    boundary = "----RabStatusBoundary9e4c1b"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="photo"; filename="bike.png"\r\n'
        f"Content-Type: image/png\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


FORBIDDEN_KEYS = {"email", "phone", "has_insurance",
                  "insurance_covers_passenger", "insurance_company",
                  "insurance_policy_number", "passenger_responsibility_accepted"}


def main() -> int:
    stub = HTTPServer(("127.0.0.1", STUB_PORT), StubHandler)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    if not wait_for_server(STUB_PORT):
        print("FAIL - stub geocoder did not start")
        return 1

    tmp = __import__("tempfile").mkdtemp(prefix="rab_ride_status_test_")
    db_path = os.path.join(tmp, "test.db")
    upload_dir = os.path.join(tmp, "uploads")
    admin_password = "status-admin-pw"
    env = dict(
        os.environ,
        RAB_DATABASE_URL=f"sqlite:///{db_path}",
        RAB_ADMIN_EMAIL="status.admin@example.com",
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
        check("health ok, version 0.20.0",
              s == 200 and h.get("version") == "0.20.0", str(h))

        s, html = get_raw("/")
        check("frontend served", s == 200 and "<html" in html)
        markers = [
            "data-countdown",
            "function countdownText",
            "function tickCountdowns",
            "setInterval(tickCountdowns, 60000)",
            "function assignedBikerBox",
            "Your biker",
            "ride.status_detail",
            "class=\"bikerbox\"",
            "class=\"countdown\"",
        ]
        for m in markers:
            check(f"frontend marker present: {m[:40]}", m in html)

        s, schema = api("GET", "/openapi.json")
        comps = schema.get("components", {}).get("schemas", {}) if s == 200 else {}
        rro = comps.get("RideRequestOut", {}).get("properties", {})
        check("OpenAPI: RideRequestOut has status_detail + assigned_biker",
              "status_detail" in rro and "assigned_biker" in rro)
        brief = comps.get("AssignedBikerBrief", {}).get("properties", {})
        want = {"user_id", "full_name", "bike_year", "bike_make",
                "bike_model", "city", "state", "photo_url"}
        check("OpenAPI: AssignedBikerBrief fields",
              want <= set(brief), str(sorted(brief)))
        check("OpenAPI: brief leaks no contact/insurance",
              not (FORBIDDEN_KEYS & set(brief)))

        # --- 2. setup: rider, biker (approved + available) with photo ---
        admin_tok = login("status.admin@example.com", admin_password)
        rider_tok, rider_uid = register("status.rider@example.com", "rider",
                                        "Status Rider", "555-0170")
        biker_tok, biker_uid = register("status.biker@example.com", "biker",
                                        "Status Biker", "555-0171")
        prof_body = {
            "bike_year": 2022, "bike_make": "Harley-Davidson",
            "bike_model": "Street Glide", "city": "Springfield", "state": "MO",
            "service_radius_miles": 50,
            "latitude": 37.21, "longitude": -93.29,
            "is_available": True, "has_endorsement": True,
            "pledge_accepted": True, "liability_accepted": True,
        }
        s, prof = api("POST", "/bikers/profile", token=biker_tok, body=prof_body)
        check("biker profile -> 200", s == 200, f"status={s}")
        body, ctype = multipart_png(PNG_1X1)
        s, up = api_raw("POST", "/bikers/profile/photo", biker_tok, body, ctype)
        check("bike photo upload -> 200", s == 200 and (up or {}).get("photo_url"),
              f"status={s}")
        s, _ = api("POST", f"/admin/bikers/{prof['id']}/approve", token=admin_tok)
        check("admin approve -> 200", s == 200, f"status={s}")

        # --- 3. pending ride: no brief, waiting wording ---
        tomorrow = (datetime.datetime.now(datetime.timezone.utc)
                    + datetime.timedelta(days=1)).isoformat()
        ride_body = {
            "ride_type": "ride", "pickup_address": "100 Main St, Springfield, MO",
            "estimated_miles": 10, "scheduled_at": tomorrow,
            "waiver_signed": True, "waiver_name": "Status Rider",
            "waiver_date": "2026-10-09",
        }
        s, ride = api("POST", "/rides/", token=rider_tok, body=ride_body)
        check("booking -> 201", s == 201, f"status={s}")
        check("pending ride: no assigned_biker",
              ride.get("assigned_biker") is None, str(ride.get("assigned_biker")))
        check("pending ride: waiting wording",
              "Waiting" in (ride.get("status_detail") or ""),
              ride.get("status_detail"))

        # --- 4. matched ride: brief + matched wording ---
        s, det = api("POST", f"/admin/rides/{ride['id']}/assign",
                     token=admin_tok, body={"biker_user_id": biker_uid})
        check("admin assign -> 200", s == 200, f"status={s}")
        check("assign response: matched wording",
              "assigned" in (det.get("status_detail") or "").lower(),
              det.get("status_detail"))
        check("assign response: brief has biker name + bike",
              (det.get("assigned_biker") or {}).get("full_name") == "Status Biker"
              and (det.get("assigned_biker") or {}).get("bike_model") == "Street Glide",
              str(det.get("assigned_biker")))
        check("assign response: brief leaks nothing",
              not (FORBIDDEN_KEYS & set((det.get("assigned_biker") or {}))))

        s, mine = api("GET", "/rides/me", token=rider_tok)
        check("/rides/me -> 200 with 1 ride", s == 200 and len(mine) == 1)
        m0 = mine[0]
        brief0 = m0.get("assigned_biker") or {}
        check("my-rides brief: name + city/state",
              brief0.get("full_name") == "Status Biker"
              and brief0.get("city") == "Springfield" and brief0.get("state") == "MO",
              str(brief0))
        check("my-rides brief: bike photo url",
              (brief0.get("photo_url") or "").startswith("/uploads/"),
              brief0.get("photo_url"))
        check("my-rides brief: no contact/insurance keys",
              not (FORBIDDEN_KEYS & set(brief0)))
        check("my-rides: matched wording",
              "assigned" in (m0.get("status_detail") or "").lower(),
              m0.get("status_detail"))

        s, one = api("GET", f"/rides/{ride['id']}", token=rider_tok)
        check("GET /rides/{id}: same brief",
              s == 200 and (one.get("assigned_biker") or {}).get("full_name")
              == "Status Biker", f"status={s}")

        s, assigned = api("GET", "/rides/assigned", token=biker_tok)
        check("biker /rides/assigned: brief present (self)",
              s == 200 and len(assigned) == 1
              and (assigned[0].get("assigned_biker") or {}).get("user_id") == biker_uid,
              f"status={s}")

        # --- 5. privacy: unrelated rider ---
        other_tok, _ = register("status.other@example.com", "rider",
                               "Status Other")
        s, _ = api("GET", f"/rides/{ride['id']}", token=other_tok)
        check("outsider GET /rides/{id} -> 403", s == 403, f"status={s}")
        s, others = api("GET", "/rides/me", token=other_tok)
        check("outsider /rides/me is empty",
              s == 200 and others == [], f"status={s} count={len(others) if s == 200 else '?'}")

        # --- 6. cancelled ride wording ---
        s, canc = api("POST", f"/rides/{ride['id']}/cancel", token=rider_tok)
        check("cancel -> 200", s == 200, f"status={s}")
        check("cancelled ride: cancelled wording",
              "cancelled" in (canc.get("status_detail") or "").lower(),
              canc.get("status_detail"))

        # --- 7. completed ride wording (second ride) ---
        s, ride2 = api("POST", "/rides/", token=rider_tok, body=ride_body)
        check("second booking -> 201", s == 201, f"status={s}")
        s, _ = api("POST", f"/admin/rides/{ride2['id']}/assign",
                   token=admin_tok, body={"biker_user_id": biker_uid})
        check("second assign -> 200", s == 200, f"status={s}")
        s, comp = api("POST", f"/rides/{ride2['id']}/complete", token=biker_tok)
        check("complete -> 200", s == 200, f"status={s}")
        check("completed ride: completed wording + brief kept",
              "completed" in ((comp.get("ride") or {}).get("status_detail") or "").lower()
              and ((comp.get("ride") or {}).get("assigned_biker") or {}).get(
                  "full_name") == "Status Biker",
              str((comp.get("ride") or {}).get("status_detail")))

        # --- 8. guards + branding ---
        s, _ = api("GET", "/rides/me")
        check("anonymous /rides/me -> 401", s == 401, f"status={s}")

        banned = ["uber", "lyft"]
        bad = []
        for src_name, text in (("ride_out.py",
                                open(os.path.join(HERE, "app", "ride_out.py")).read()),
                               ("index.html",
                                open(os.path.join(HERE, "app", "static", "index.html")).read()),
                               ("schemas.py",
                                open(os.path.join(HERE, "app", "schemas.py")).read())):
            low = text.lower()
            for b in banned:
                if b in low:
                    bad.append(f"{src_name}:{b}")
        check("branding-clean (no ride-service names)", not bad, str(bad))
    finally:
        server.terminate()
        server.wait(timeout=10)

    failed = [c for c in CHECKS if not c[1]]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
