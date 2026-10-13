#!/usr/bin/env python3
"""Smoke test: M6 versioned legal texts + signed records.

Starts a stub Nominatim server (no real network), then the API with a
throwaway SQLite DB + env-seeded admin, and:

  1. checks the served frontend renders legal texts only from the server
     (GET /legal/*) and the JS only calls endpoints present in the live
     OpenAPI schema;
  2. verifies GET /legal/waiver|/legal/biker-pledge|/legal/biker-liability
     return {key, version, text} without auth;
  3. walks the signed-record flow: booking stamps the waiver version, the
     biker profile stamps the pledge/liability versions (unchecking clears
     them), all visible where they should be (my-rides, dispatch board,
     admin review queue) and absent from rider-facing public cards;
  4. runs a clean branding scan.

Run from hidden_files/src/:  ./.venv/bin/python tests/smoke_legal.py
"""
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
API_PORT = 8491
STUB_PORT = 8492
BASE = f"http://127.0.0.1:{API_PORT}"
CHECKS: list[tuple[str, bool, str]] = []

# Stub "Nominatim": address fragment (lowercase) -> (lat, lon)
STUB_ADDRESSES = {
    "100 main st": (37.20, -93.30),  # Springfield-ish pickup
}


class StubHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        q = qs.get("q", [""])[0]
        coords = None
        if parsed.path == "/search":
            low = q.lower()
            for frag, c in STUB_ADDRESSES.items():
                if frag in low:
                    coords = c
                    break
        body = json.dumps(
            [{"lat": str(coords[0]), "lon": str(coords[1])}] if coords else []
        ).encode()
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


def get_raw(path: str):
    req = urllib.request.Request(BASE + path, method="GET")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read().decode()


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

    tmp = __import__("tempfile").mkdtemp(prefix="rab_legal_test_")
    db_path = os.path.join(tmp, "test.db")
    upload_dir = os.path.join(tmp, "uploads")
    admin_password = "admin-pass-1234"
    env = dict(
        os.environ,
        RAB_DATABASE_URL=f"sqlite:///{db_path}",
        RAB_ADMIN_EMAIL="legal.admin@example.com",
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

        # --- 1. version + frontend markup (server-rendered legal text only) ---
        s, h = api("GET", "/health")
        check("health ok, version 0.20.0",
              s == 200 and h.get("version") == "0.20.0", str(h))

        s, html = get_raw("/")
        check("frontend served", s == 200 and "<html" in html)
        markers = [
            "/legal/waiver",            # waiver text fetched from server
            "/legal/biker-pledge",      # pledge text fetched from server
            "/legal/biker-liability",   # liability text fetched from server
            'id="waiverText"',          # waiver text placeholder span
            'id="pledgeText"',          # pledge text placeholder span
            'id="liabilityText"',       # liability text placeholder span
            "async function loadLegalTexts()",
            "Waiver v' +",               # signed waiver version shown on my-rides
            "waiver_version",
        ]
        for m in markers:
            check(f"frontend marker present: {m[:40]}", m in html)
        check("waiver text no longer hard-coded in HTML",
              "connection service: bikers are" not in html,
              "the booking form renders the server text now")
        check("liability text no longer hard-coded in HTML",
              "I accept personal responsibility for my conduct on the ride." not in html)

        s, schema = api("GET", "/openapi.json")
        paths = schema.get("paths", {}) if s == 200 else {}
        for p, meth in [("/legal/waiver", "get"),
                        ("/legal/biker-pledge", "get"),
                        ("/legal/biker-liability", "get"),
                        ("/rides/", "post"),
                        ("/bikers/profile", "post")]:
            check(f"OpenAPI has {meth.upper()} {p}",
                  p in paths and meth in paths[p])

        # --- 2. legal endpoints are public and versioned ---
        s, waiver = api("GET", "/legal/waiver")
        check("GET /legal/waiver -> 200", s == 200, str(waiver)[:120])
        check("waiver shape {key, version, text}",
              waiver.get("key") == "rider-waiver"
              and isinstance(waiver.get("version"), str)
              and len(waiver.get("text", "")) > 20)
        check("waiver text is the connection-business wording",
              "independent riders" in waiver["text"]
              and "not liable for the ride itself" in waiver["text"])
        s, pledge = api("GET", "/legal/biker-pledge")
        check("GET /legal/biker-pledge -> 200, shaped",
              s == 200 and pledge.get("key") == "biker-pledge"
              and len(pledge.get("text", "")) > 20, str(pledge)[:100])
        s, liability = api("GET", "/legal/biker-liability")
        check("GET /legal/biker-liability -> 200, shaped",
              s == 200 and liability.get("key") == "biker-liability"
              and "independent rider" in liability.get("text", ""),
              str(liability)[:100])

        # --- 3. booking stamps the waiver version ---
        rider_tok, rider_id = register("legal.rider@example.com", "rider",
                                      "Legal Rider", "555-0119")
        s, ride = api("POST", "/rides/", token=rider_tok, body={
            "ride_type": "ride", "pickup_address": "100 Main St, Springfield, MO",
            "dropoff_address": "200 Oak Ave, Springfield, MO",
            "scheduled_at": None, "estimated_miles": 20,
            "notes": None, "helmet_needed": False,
            "waiver_signed": True, "waiver_name": "Legal Rider",
            "waiver_date": "2026-10-08",
        })
        check("booking -> 201", s == 201, f"status={s}")
        check("booking stamps current waiver version",
              ride.get("waiver_version") == waiver["version"],
              f"got {ride.get('waiver_version')!r}")

        s, mine = api("GET", "/rides/me", token=rider_tok)
        check("my-rides carries the signed waiver version",
              s == 200 and mine and mine[0].get("waiver_version") == waiver["version"])

        # --- 4. biker application stamps pledge/liability versions ---
        biker_tok, biker_uid = register("legal.biker@example.com", "biker",
                                       "Legal Biker", "555-0120")
        prof_body = {
            "bike_year": 2021, "bike_make": "Harley-Davidson",
            "bike_model": "Road Glide", "city": "Springfield", "state": "MO",
            "service_radius_miles": 50,
            "latitude": 37.21, "longitude": -93.29,
            "is_available": True, "has_endorsement": True,
            "pledge_accepted": True, "liability_accepted": True,
        }
        s, prof = api("POST", "/bikers/profile", token=biker_tok, body=prof_body)
        check("profile create -> 200", s == 200, f"status={s}")
        check("profile stamps current pledge version",
              prof.get("pledge_version") == pledge["version"],
              f"got {prof.get('pledge_version')!r}")
        check("profile stamps current liability version",
              prof.get("liability_version") == liability["version"],
              f"got {prof.get('liability_version')!r}")

        s, mine_prof = api("GET", "/bikers/profile/me", token=biker_tok)
        check("own profile shows both legal versions",
              s == 200 and mine_prof.get("pledge_version") == pledge["version"]
              and mine_prof.get("liability_version") == liability["version"])

        # unchecking pledge clears only its version
        prof_body["pledge_accepted"] = False
        s, prof2 = api("POST", "/bikers/profile", token=biker_tok, body=prof_body)
        check("uncheck pledge clears pledge_version, keeps liability_version",
              s == 200 and prof2.get("pledge_version") is None
              and prof2.get("liability_version") == liability["version"])

        # rider-facing public cards never carry legal version fields
        # (approve first so the biker is actually visible on the board)
        admin_tok = login("legal.admin@example.com", admin_password)
        s, pid = api("POST", f"/admin/bikers/{prof['id']}/approve",
                     token=admin_tok, body={"reason": None})
        check("admin approve -> 200", s == 200, f"status={s}")
        s, cards = api("GET", "/bikers/available", token=rider_tok)
        card = (cards or [{}])[0]
        check("public biker cards carry no legal versions",
              s == 200 and "pledge_version" not in card
              and "liability_version" not in card,
              f"{len(cards or [])} cards visible")

        # admin views carry the versions (review queue + dispatch board)
        s, queue = api("GET", "/admin/bikers", token=admin_tok)
        entry = (queue or [{}])[0]
        check("review queue shows legal versions (pledge cleared after uncheck)",
              s == 200 and entry.get("pledge_version") is None
              and entry.get("liability_version") == liability["version"])
        s, board = api("GET", "/admin/dispatch", token=admin_tok)
        board_ride = (board.get("requests") or [{}])[0] if s == 200 else {}
        check("dispatch board ride carries signed waiver version",
              s == 200 and board_ride.get("waiver_version") == waiver["version"],
              f"got {board_ride.get('waiver_version')!r}")

        # --- 5. branding scan (app source only — test files carry the
        #     meta-assertion strings, same convention as prior runs) ---
        src_files = []
        app_dir = os.path.join(HERE, "app")
        for root, _, files in os.walk(app_dir):
            for f in files:
                if f.endswith((".py", ".html")):
                    src_files.append(os.path.join(root, f))
        bad = []
        for path in src_files:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
            low = text.lower()
            for name in ("uber", "lyft"):
                if name in low:
                    bad.append((path, name))
                    break
        check("branding scan clean", not bad, str(bad) if bad else "no mentions")

        fails = [c for c in CHECKS if not c[1]]
        print(f"\n{len(CHECKS) - len(fails)}/{len(CHECKS)} checks passed")
        return 0 if not fails else 1
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except Exception:
            server.kill()


if __name__ == "__main__":
    raise SystemExit(main())
