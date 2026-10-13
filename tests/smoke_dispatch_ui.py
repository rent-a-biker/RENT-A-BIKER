#!/usr/bin/env python3
"""Smoke test: M4 admin dispatch dashboard UI (frontend) + its API contract.

Starts a stub Nominatim server (no real network), then the API with a
throwaway SQLite DB + env-seeded admin, and:

  1. checks the served frontend contains the dispatch-board UI (admin card,
     board container, refresh button, assign flow, insurance badge), and that
     the UI's JS only calls endpoints that exist in the live OpenAPI schema;
  2. walks the exact data flow the UI drives: rider books a ride -> admin
     board lists it with rider contact + ranked candidates -> admin assigns
     -> board clears -> rider sees the matched ride;
  3. confirms admin-only endpoints are still 403/401 for non-admins.

Run from hidden_files/src/:  ./.venv/bin/python tests/smoke_dispatch_ui.py
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
API_PORT = 8483
STUB_PORT = 8484
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

    tmp = __import__("tempfile").mkdtemp(prefix="rab_dispatch_ui_test_")
    db_path = os.path.join(tmp, "test.db")
    upload_dir = os.path.join(tmp, "uploads")
    admin_password = "dispatch-ui-admin-pw"
    env = dict(
        os.environ,
        RAB_DATABASE_URL=f"sqlite:///{db_path}",
        RAB_ADMIN_EMAIL="dispatch.admin@example.com",
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
        check("health ok, version 0.20.0",
              s == 200 and h.get("version") == "0.20.0", str(h))

        s, html = get_raw("/")
        check("frontend served", s == 200 and "<html" in html)
        markers = [
            'id="adminCard"', 'id="dispatchBoard"', 'id="refreshDispatchBtn"',
            'id="dispatchErr"', "Dispatch board",
            "api('GET', '/admin/dispatch')",
            "/admin/rides/' + rideId + '/assign", "{ biker_user_id: userId }",
            "Biker candidates", "insuranceBadge", ">Assign</button>",
            "No approved + available bikers right now.",
            "No pending rides — the board is clear.",
            "loadDispatch", "assignBiker(",
        ]
        missing = [m for m in markers if m not in html]
        check("dispatch board UI markup present",
              not missing, f"missing={missing}")

        # --- 2. the UI's JS calls only endpoints that exist (OpenAPI contract) ---
        s, spec = api("GET", "/openapi.json")
        paths = spec.get("paths", {}) if isinstance(spec, dict) else {}
        check("openapi schema available", s == 200 and bool(paths))
        check("GET /admin/dispatch exists",
              "/admin/dispatch" in paths
              and "get" in paths.get("/admin/dispatch", {}))
        check("POST /admin/rides/{ride_id}/assign exists",
              "/admin/rides/{ride_id}/assign" in paths
              and "post" in paths.get("/admin/rides/{ride_id}/assign", {}))
        # other endpoints the new UI touches
        check("GET /auth/me exists",
              "/auth/me" in paths and "get" in paths.get("/auth/me", {}))
        check("POST /bikers/profile/availability exists",
              "/bikers/profile/availability" in paths
              and "post" in paths.get("/bikers/profile/availability", {}))

        # --- 3. admin-only guards still hold ---
        rider_tok, _ = register("ui.rider@example.com", "rider",
                               "UI Rider", phone="417-555-0101")
        s, _ = api("GET", "/admin/dispatch", token=rider_tok)
        check("rider gets 403 on dispatch board", s == 403, f"status={s}")
        s, _ = api("GET", "/admin/dispatch")
        check("anonymous gets 401 on dispatch board", s == 401, f"status={s}")
        admin_tok = login("dispatch.admin@example.com", admin_password)

        # --- 4. end-to-end mirroring the UI's exact data flow ---
        # rider books a ride (same payload shape the booking form sends)
        s, ride = api("POST", "/rides/", token=rider_tok, body={
            "ride_type": "ride", "pickup_address": "100 Main St, Springfield, MO",
            "dropoff_address": "200 Oak Ave, Springfield, MO",
            "scheduled_at": None, "estimated_miles": 20,
            "notes": None, "helmet_needed": True,
            "waiver_signed": True, "waiver_name": "UI Rider",
            "waiver_date": "2026-10-07",
        })
        check("ride booked (waiver ok)", s == 201,
              f"status={s} {str(ride)[:120]}")
        ride_id = ride["id"]

        # biker registers, creates profile with manual coords near the pickup,
        # insurance covering passengers, and flips availability on
        biker_tok, biker_user_id = register(
            "ui.biker@example.com", "biker", "UI Biker", phone="417-555-0102")
        s, prof = api("POST", "/bikers/profile", token=biker_tok, body={
            "bike_year": 2021, "bike_make": "Harley-Davidson",
            "bike_model": "Road Glide", "city": "Springfield", "state": "MO",
            "service_radius_miles": 50, "latitude": 37.21, "longitude": -93.29,
            "is_available": True, "has_endorsement": True,
            "pledge_accepted": True, "liability_accepted": True,
            "has_insurance": True, "insurance_covers_passenger": True,
            "insurance_company": "Test Ins Co", "insurance_policy_number": "POL-123",
        })
        check("biker profile created", s in (200, 201),
              f"status={s} {str(prof)[:160]}")
        s, _ = api("POST", f"/admin/bikers/{prof['id']}/approve",
                   token=admin_tok)
        check("admin approves biker", s == 200, f"status={s}")

        # UI loads the board: GET /admin/dispatch
        s, board = api("GET", "/admin/dispatch", token=admin_tok)
        reqs = board.get("requests", []) if isinstance(board, dict) else []
        check("board lists the pending ride", s == 200 and len(reqs) == 1,
              f"status={s} count={len(reqs)}")
        b = reqs[0]
        rider = b.get("rider", {}) or {}
        check("board shows rider contact info",
              rider.get("email") == "ui.rider@example.com"
              and rider.get("phone") == "417-555-0101"
              and rider.get("full_name") == "UI Rider", str(rider))
        cands = b.get("candidates", [])
        check("board ranks one candidate", len(cands) == 1,
              f"count={len(cands)}")
        c = cands[0]
        dist = c.get("distance_miles")
        check("candidate has distance + insurance flags",
              isinstance(dist, (int, float)) and abs(dist - 0.9) < 0.3
              and c.get("has_insurance") is True
              and c.get("insurance_covers_passenger") is True
              and c.get("phone") == "417-555-0102", str(c))
        check("candidate bike info present",
              c.get("bike_make") == "Harley-Davidson"
              and c.get("full_name") == "UI Biker", str(c))

        # UI assign click: POST /admin/rides/{id}/assign {biker_user_id}
        s, detail = api("POST", f"/admin/rides/{ride_id}/assign",
                        token=admin_tok,
                        body={"biker_user_id": c["user_id"]})
        check("assign (UI flow) -> matched", s == 200
              and detail.get("status") == "matched"
              and detail.get("biker_id") == biker_user_id,
              f"status={s} {str(detail)[:160]}")

        # UI reloads the board: matched ride drops off
        s, board = api("GET", "/admin/dispatch", token=admin_tok)
        reqs = board.get("requests", []) if isinstance(board, dict) else []
        check("matched ride leaves the board", s == 200 and not reqs,
              f"status={s} count={len(reqs)}")

        # rider's my-rides (what the rider panel shows) reflects the match
        s, mine = api("GET", "/rides/me", token=rider_tok)
        statuses = [r.get("status") for r in (mine or [])]
        check("rider sees matched ride", s == 200 and "matched" in statuses,
              str(statuses))

        # --- 5. branding: no ride-service names anywhere in the new UI ---
        lowered = html.lower()
        banned = ["uber", "lyft"]
        hits = [b for b in banned if b in lowered]
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
