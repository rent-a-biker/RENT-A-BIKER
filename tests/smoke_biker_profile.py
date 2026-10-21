#!/usr/bin/env python3
"""Smoke test: M2 biker profile form — insurance completion
(expiration date + passenger-responsibility typed name/date signature) and
the new create/edit profile form in the frontend.

Starts a stub Nominatim server (so no real network is hit), then the API with
a throwaway SQLite DB + env-seeded admin, and walks the insurance rule matrix
and profile upsert flow with stdlib-only HTTP calls. Exits 0 if all checks pass.

Run from hidden_files/src/:  ./.venv/bin/python tests/smoke_biker_profile.py
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
API_PORT = 8485
STUB_PORT = 8486
BASE = f"http://127.0.0.1:{API_PORT}"
CHECKS: list[tuple[str, bool, str]] = []

# Stub "Nominatim": (city, state) -> (lat, lon); address fragment -> (lat, lon)
STUB_CITIES = {
    ("springfield", "MO"): (37.21, -93.29),
    ("ozark", "MO"): (37.02, -93.21),
}
STUB_ADDRESSES = {
    "500 elm st": (37.20, -93.30),
}
STUB_HITS: list[dict] = []


class StubHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        q = qs.get("q", [""])[0]
        STUB_HITS.append({"q": q})
        coords = None
        if parsed.path == "/search":
            if "countrycodes" in qs:  # address lookup
                low = q.lower()
                for frag, c in STUB_ADDRESSES.items():
                    if frag in low:
                        coords = c
                        break
            else:  # city/state lookup
                city = (q.split(",")[0] if "," in q else "").strip().lower()
                state = (q.split(",")[1] if "," in q else "").strip().upper()
                coords = STUB_CITIES.get((city, state))
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


BASE_PROFILE = {
    "bike_year": 2007, "bike_make": "Harley-Davidson",
    "bike_model": "Softail", "city": "Springfield", "state": "MO",
    "service_radius_miles": 50, "is_available": True,
    "has_endorsement": True, "pledge_accepted": True, "liability_accepted": True,
}


def main() -> int:
    stub = HTTPServer(("127.0.0.1", STUB_PORT), StubHandler)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    if not wait_for_server(STUB_PORT):
        print("FAIL - stub geocoder did not start")
        return 1

    tmp = __import__("tempfile").mkdtemp(prefix="rab_profile_test_")
    db_path = os.path.join(tmp, "test.db")
    upload_dir = os.path.join(tmp, "uploads")
    env = dict(
        os.environ,
        RAB_DATABASE_URL=f"sqlite:///{db_path}",
        RAB_ADMIN_EMAIL="kenneth.admin@example.com",
        RAB_ADMIN_PASSWORD="test-admin-password-xyz-123",
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

        s, h = api("GET", "/health")
        check("health ok, version 0.21.0",
              s == 200 and h.get("version") == "0.21.0", str(h))

        s, tok = api("POST", "/auth/login", form=True,
                     body={"username": "kenneth.admin@example.com",
                           "password": "test-admin-password-xyz-123"})
        admin_tok = tok["access_token"] if s == 200 else None
        check("seeded admin logs in", admin_tok is not None)

        biker_tok, biker_uid = register("biker-p@example.com", "biker",
                                        "Biker P", "555-0301")
        rider_tok, rider_uid = register("rider-p@example.com", "rider",
                                        "Rider P", "555-0302")

        # --- 1. no profile yet -> 404 ---
        s, _ = api("GET", "/bikers/profile/me", token=biker_tok)
        check("GET /bikers/profile/me is 404 before profile exists",
              s == 404, f"status={s}")

        # --- 2. rider role is still fenced out of profile endpoints ---
        s, _ = api("POST", "/bikers/profile", token=rider_tok,
                   body=dict(BASE_PROFILE))
        check("rider role cannot POST /bikers/profile (403)",
              s == 403, f"status={s}")

        # --- 3. insurance rule matrix (signature typed name + date) ---
        ins_base = dict(has_insurance=True, insurance_covers_passenger=False,
                        insurance_company="TestCo", insurance_policy_number="POL-9",
                        insurance_expiration_date="2027-05-01")
        s, _ = api("POST", "/bikers/profile", token=biker_tok,
                   body=dict(BASE_PROFILE, **ins_base,
                             passenger_responsibility_accepted=False))
        check("insured, no coverage, no agreement -> 422", s == 422,
              f"status={s}")

        s, _ = api("POST", "/bikers/profile", token=biker_tok,
                   body=dict(BASE_PROFILE, **ins_base,
                             passenger_responsibility_accepted=True,
                             passenger_responsibility_name="   ",
                             passenger_responsibility_date="2026-10-08"))
        check("agreement accepted but blank signature name -> 422", s == 422,
              f"status={s}")

        s, _ = api("POST", "/bikers/profile", token=biker_tok,
                   body=dict(BASE_PROFILE, **ins_base,
                             passenger_responsibility_accepted=True,
                             passenger_responsibility_name="Biker P",
                             passenger_responsibility_date=""))
        check("agreement accepted + name but no date -> 422", s == 422,
              f"status={s}")

        s, prof = api("POST", "/bikers/profile", token=biker_tok,
                      body=dict(BASE_PROFILE, **ins_base,
                                passenger_responsibility_accepted=True,
                                passenger_responsibility_name="Biker P",
                                passenger_responsibility_date="2026-10-08"))
        ok = (s == 200 and prof["insurance_expiration_date"] == "2027-05-01"
              and prof["passenger_responsibility_name"] == "Biker P"
              and prof["passenger_responsibility_date"] == "2026-10-08")
        check("full agreement (checkbox + typed name + date) -> 200 "
              "with expiration and signature stored",
              ok, f"status={s}")
        check("profile city geocoded from stub",
              prof["latitude"] == 37.21 and prof["longitude"] == -93.29,
              f"lat={prof.get('latitude')} lng={prof.get('longitude')}")

        # --- 4. GET /bikers/profile/me round-trips the new fields ---
        s, me = api("GET", "/bikers/profile/me", token=biker_tok)
        check("own profile returns insurance + signature fields",
              s == 200 and me["insurance_expiration_date"] == "2027-05-01"
              and me["passenger_responsibility_name"] == "Biker P",
              f"status={s}")

        # --- 5. edit (upsert): city change re-geocodes, insurance persists ---
        s, upd = api("POST", "/bikers/profile", token=biker_tok,
                     body=dict(BASE_PROFILE, **ins_base,
                               city="Ozark", state="MO",
                               passenger_responsibility_accepted=True,
                               passenger_responsibility_name="Biker P",
                               passenger_responsibility_date="2026-10-08"))
        check("edit saves (upsert 200), city change re-geocodes",
              s == 200 and upd["latitude"] == 37.02
              and upd["longitude"] == -93.21
              and upd["insurance_expiration_date"] == "2027-05-01",
              f"status={s} lat={upd.get('latitude') if isinstance(upd, dict) else '?'}")

        # --- 6. admin review queue exposes the new fields ---
        s, q = api("GET", "/admin/bikers?approval_status=pending",
                   token=admin_tok)
        app0 = q[0] if s == 200 and q else {}
        check("admin review queue carries insurance + signature fields",
              s == 200 and app0.get("insurance_expiration_date") == "2027-05-01"
              and app0.get("passenger_responsibility_name") == "Biker P",
              f"status={s}")

        # --- 7. dispatch candidate carries signature info for Kenneth ---
        s, _ = api("POST", f"/admin/bikers/{app0['id']}/approve",
                   token=admin_tok)
        check("admin approves the biker", s == 200, f"status={s}")
        ride_body = {"ride_type": "ride", "estimated_miles": 10,
                     "pickup_address": "500 Elm St, Springfield, MO",
                     "waiver_signed": True, "waiver_name": "Rider P",
                     "waiver_date": "2026-10-08"}
        s, ride = api("POST", "/rides/", token=rider_tok, body=ride_body)
        check("rider books a ride", s == 201, f"status={s}")
        s, board = api("GET", "/admin/dispatch", token=admin_tok)
        cand = {}
        if s == 200:
            for r in board["requests"]:
                for c in r["candidates"]:
                    if c["user_id"] == biker_uid:
                        cand = c
        check("dispatch candidate shows expiration + signature to Kenneth",
              cand.get("insurance_expiration_date") == "2027-05-01"
              and cand.get("passenger_responsibility_name") == "Biker P",
              f"cand={ {k: cand.get(k) for k in ('insurance_expiration_date', 'passenger_responsibility_name')} }")

        # --- 8. public cards still leak no insurance info ---
        s, pub = api("GET", "/bikers/available", token=biker_tok)
        check("public biker cards leak no insurance fields",
              s == 200 and "insurance" not in json.dumps(pub),
              f"status={s}")

        # --- 9. frontend: profile form markers present ---
        s, html = get_raw("/")
        markers = ("profileFormWrap", "saveProfileBtn", "editProfileBtn",
                   "insExp", "respName", "respDate", "insCovers",
                   "Passenger-responsibility agreement", "openProfileForm",
                   "toggleBtn", "/bikers/profile/availability")
        missing = [m for m in markers if m not in html]
        check("frontend profile form markup present", s == 200 and not missing,
              f"missing={missing}")
        lowered = html.lower()
        check("frontend branding-clean",
              "uber" not in lowered and "lyft" not in lowered)

        # --- 10. backend source files branding-clean ---
        brand_ok = True
        for rel in ("app/models.py", "app/schemas.py", "app/routers/admin.py",
                    "app/routers/bikers.py", "app/main.py"):
            with open(os.path.join(HERE, rel), encoding="utf-8") as f:
                text = f.read().lower()
            if "uber" in text or "lyft" in text:
                brand_ok = False
        check("changed backend files branding-clean", brand_ok)

        failures = [c for c in CHECKS if not c[1]]
        print(f"\n{len(CHECKS) - len(failures)}/{len(CHECKS)} checks passed")
        return 0 if not failures else 1
    finally:
        server.terminate()
        server.wait(timeout=15)


if __name__ == "__main__":
    raise SystemExit(main())
