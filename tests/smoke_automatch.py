#!/usr/bin/env python3
"""Smoke test: M4 auto-match (nearest-biker one-click dispatch) + its UI.

Starts a stub Nominatim server (no real network), then the API with a
throwaway SQLite DB + env-seeded admin, and:

  1. checks the served frontend contains the Auto-match button/flow and the
     JS only calls the new endpoint from the live OpenAPI schema;
  2. verifies the rules: pending-only, pickup must be mapped, no candidates
     -> 400, nearest biker wins, exact distance ties go to the
     passenger-covered policy, ?within_miles caps the radius;
  3. walks the data flow the UI drives: book -> auto-match -> matched ->
     board clears -> rider sees it;
  4. confirms admin-only guards still hold (403 rider, 401 anonymous).

Run from hidden_files/src/:  ./.venv/bin/python tests/smoke_automatch.py
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
API_PORT = 8487
STUB_PORT = 8488
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


def make_profile(tok, city_coords, insurance):
    body = {
        "bike_year": 2021, "bike_make": "Harley-Davidson",
        "bike_model": "Road Glide", "city": "Springfield", "state": "MO",
        "service_radius_miles": 50,
        "latitude": city_coords[0], "longitude": city_coords[1],
        "is_available": True, "has_endorsement": True,
        "pledge_accepted": True, "liability_accepted": True,
    }
    body.update(insurance)
    s, prof = api("POST", "/bikers/profile", token=tok, body=body)
    assert s in (200, 201), f"profile create failed: {s} {prof}"
    return prof


def book_ride(tok, pickup):
    s, ride = api("POST", "/rides/", token=tok, body={
        "ride_type": "ride", "pickup_address": pickup,
        "dropoff_address": "200 Oak Ave, Springfield, MO",
        "scheduled_at": None, "estimated_miles": 20,
        "notes": None, "helmet_needed": False,
        "waiver_signed": True, "waiver_name": "Auto Rider",
        "waiver_date": "2026-10-08",
    })
    assert s == 201, f"book ride failed: {s} {ride}"
    return ride


def approve(tok, profile_id):
    s, p = api("POST", f"/admin/bikers/{profile_id}/approve", token=tok)
    assert s == 200, f"approve failed: {s} {p}"


def main() -> int:
    stub = HTTPServer(("127.0.0.1", STUB_PORT), StubHandler)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    if not wait_for_server(STUB_PORT):
        print("FAIL - stub geocoder did not start")
        return 1

    tmp = __import__("tempfile").mkdtemp(prefix="rab_automatch_test_")
    db_path = os.path.join(tmp, "test.db")
    upload_dir = os.path.join(tmp, "uploads")
    admin_password = "automatch-admin-pw"
    env = dict(
        os.environ,
        RAB_DATABASE_URL=f"sqlite:///{db_path}",
        RAB_ADMIN_EMAIL="automatch.admin@example.com",
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

        # --- 1. version + frontend markup ---
        s, h = api("GET", "/health")
        check("health ok, version 0.21.0",
              s == 200 and h.get("version") == "0.21.0", str(h))

        s, html = get_raw("/")
        check("frontend served", s == 200 and "<html" in html)
        markers = [
            ">Auto-match</button>", '"automatch-\' + ride.id',
            "autoMatchRide(ride.id)",
            "'/admin/rides/' + rideId + '/auto-match'",
            "Auto-match the nearest eligible biker to ride #'",
            "async function autoMatchRide(rideId)",
        ]
        missing = [m for m in markers if m not in html]
        check("auto-match UI markup present",
              not missing, f"missing={missing}")

        # --- 2. OpenAPI contract ---
        s, spec = api("GET", "/openapi.json")
        paths = spec.get("paths", {}) if isinstance(spec, dict) else {}
        check("POST /admin/rides/{ride_id}/auto-match exists",
              "/admin/rides/{ride_id}/auto-match" in paths
              and "post" in paths.get("/admin/rides/{ride_id}/auto-match", {}))

        # --- 3. guards ---
        rider_tok, _ = register("am.rider@example.com", "rider",
                                "Auto Rider", phone="417-555-0201")
        s, _ = api("POST", "/admin/rides/1/auto-match", token=rider_tok)
        check("rider gets 403 on auto-match", s == 403, f"status={s}")
        s, _ = api("POST", "/admin/rides/1/auto-match")
        check("anonymous gets 401 on auto-match", s == 401, f"status={s}")
        admin_tok = login("automatch.admin@example.com", admin_password)

        # --- 4. unmapped pickup -> 400 ---
        ride_a = book_ride(rider_tok, "999 Nowhere Ln, Springfield, MO")
        s, err = api("POST", f"/admin/rides/{ride_a['id']}/auto-match",
                     token=admin_tok)
        check("auto-match 400 when pickup unmapped",
              s == 400 and "hasn't been mapped" in str(err), f"status={s} {err}")

        # --- 5. mapped pickup but no bikers -> 400 ---
        ride_b = book_ride(rider_tok, "100 Main St, Springfield, MO")
        check("ride B pickup geocoded",
              ride_b.get("pickup_latitude") == 37.2
              and ride_b.get("pickup_longitude") == -93.3, str(ride_b))
        s, err = api("POST", f"/admin/rides/{ride_b['id']}/auto-match",
                     token=admin_tok)
        check("auto-match 400 with no eligible bikers",
              s == 400 and "No approved + available bikers" in str(err),
              f"status={s} {err}")

        # --- 6. two bikers, identical distance; tie-break on passenger cover ---
        biker1_tok, biker1_uid = register(
            "am.biker1@example.com", "biker", "Near NoCover")
        prof1 = make_profile(biker1_tok, (37.20, -93.30), {
            "has_insurance": True, "insurance_covers_passenger": False,
            "passenger_responsibility_accepted": True,
            "passenger_responsibility_name": "Near NoCover",
            "passenger_responsibility_date": "2026-10-08",
            "insurance_company": "Test Ins Co",
        })
        approve(admin_tok, prof1["id"])
        biker2_tok, biker2_uid = register(
            "am.biker2@example.com", "biker", "Near Covered")
        prof2 = make_profile(biker2_tok, (37.20, -93.30), {
            "has_insurance": True, "insurance_covers_passenger": True,
            "insurance_company": "Test Ins Co", "insurance_policy_number": "POL-9",
        })
        approve(admin_tok, prof2["id"])

        s, out = api("POST", f"/admin/rides/{ride_b['id']}/auto-match",
                     token=admin_tok)
        ride_out = (out or {}).get("ride", {})
        cand = (out or {}).get("assigned_candidate", {})
        check("auto-match picks the passenger-covered biker on a distance tie",
              s == 200 and ride_out.get("status") == "matched"
              and ride_out.get("biker_id") == biker2_uid
              and cand.get("user_id") == biker2_uid
              and cand.get("distance_miles") == 0.0
              and cand.get("insurance_covers_passenger") is True
              and cand.get("email") == "am.biker2@example.com",
              f"status={s} {str(out)[:220]}")

        # board: ride B dropped off, ride A still pending
        s, board = api("GET", "/admin/dispatch", token=admin_tok)
        ids = [r.get("id") for r in board.get("requests", [])]
        check("matched ride leaves board, unmapped ride stays",
              s == 200 and ride_b["id"] not in ids and ride_a["id"] in ids,
              f"ids={ids}")

        s, mine = api("GET", "/rides/me", token=rider_tok)
        by_id = {r["id"]: r.get("status") for r in (mine or [])}
        check("rider sees ride B matched, ride A pending",
              by_id.get(ride_b["id"]) == "matched"
              and by_id.get(ride_a["id"]) == "pending", str(by_id))

        # --- 7. matched ride -> 400; missing ride -> 404 ---
        s, err = api("POST", f"/admin/rides/{ride_b['id']}/auto-match",
                     token=admin_tok)
        check("auto-match 400 on a matched ride",
              s == 400 and "matched" in str(err), f"status={s} {err}")
        s, _ = api("POST", "/admin/rides/99999/auto-match", token=admin_tok)
        check("auto-match 404 on missing ride", s == 404, f"status={s}")

        # --- 8. within_miles cap: far biker only in range vs excluded ---
        ride_c = book_ride(rider_tok, "100 Main St, Springfield, MO")
        for tok in (biker1_tok, biker2_tok):
            s, _ = api("POST", "/bikers/profile/availability", token=tok,
                       body={"is_available": False})
            assert s == 200, f"availability off failed: {s}"
        biker3_tok, biker3_uid = register(
            "am.biker3@example.com", "biker", "Far Covered")
        prof3 = make_profile(biker3_tok, (37.45, -93.30), {  # ~17.3 mi away
            "has_insurance": True, "insurance_covers_passenger": True,
            "insurance_company": "Test Ins Co",
        })
        approve(admin_tok, prof3["id"])

        s, out = api("POST", f"/admin/rides/{ride_c['id']}/auto-match",
                     token=admin_tok)
        cand = (out or {}).get("assigned_candidate", {})
        dist = cand.get("distance_miles")
        check("auto-match picks the only in-range biker (~17.3 mi)",
              s == 200 and cand.get("user_id") == biker3_uid
              and isinstance(dist, (int, float)) and abs(dist - 17.3) < 0.5,
              f"status={s} dist={dist}")
        # unassign, then shrink the radius -> nobody in range
        s, _ = api("POST", f"/admin/rides/{ride_c['id']}/unassign",
                   token=admin_tok)
        check("unassign puts ride C back to pending", s == 200
              and _.get("status") == "pending", f"status={s}")
        s, err = api("POST", f"/admin/rides/{ride_c['id']}/auto-match"
                             "?within_miles=5", token=admin_tok)
        check("within_miles=5 excludes the 17-mi biker",
              s == 400 and "No approved + available bikers" in str(err),
              f"status={s} {err}")
        # and the same cap with a near biker re-available still works
        s, _ = api("POST", "/bikers/profile/availability", token=biker1_tok,
                   body={"is_available": True})
        s, out = api("POST", f"/admin/rides/{ride_c['id']}/auto-match"
                             "?within_miles=5", token=admin_tok)
        cand = (out or {}).get("assigned_candidate", {})
        check("within_miles=5 still matches the 0.0-mi biker",
              s == 200 and cand.get("user_id") == biker1_uid
              and cand.get("distance_miles") == 0.0,
              f"status={s} {str(cand)[:160]}")

        # --- 9. branding: no ride-service names in the new UI or code paths ---
        lowered = html.lower()
        hits = [b for b in ("uber", "lyft") if b in lowered]
        check("frontend branding-clean", not hits, f"hits={hits}")
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except Exception:
            server.kill()
        stub.shutdown()

    failed = [n for n, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
