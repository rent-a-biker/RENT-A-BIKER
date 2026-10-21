#!/usr/bin/env python3
"""Smoke test: M5 in-app messaging rider <-> biker (+ Kenneth), polling.

Starts a stub Nominatim server (no real network), then the API with a
throwaway SQLite DB + env-seeded admin, and:

  1. checks the served frontend contains the thread UI (toggle, input, send,
     biker "My matched rides", admin dispatch thread) and the JS only calls
     the new endpoints from the live OpenAPI schema;
  2. verifies thread access rules: only the rider, the assigned biker, and
     Kenneth (admin) can read/post — outsiders get 403, anonymous gets 401;
  3. walks the data flow: book -> assign -> matched -> rider posts -> biker
     sees -> biker replies -> after_id polling returns only the new message ->
     admin posts/reads -> biker sees the ride in /rides/assigned;
  4. checks validations (empty message -> 400, overlong -> 422, unknown ride
     -> 404, message on a cancelled ride -> 400) and a clean branding scan.

Run from hidden_files/src/:  ./.venv/bin/python tests/smoke_messages.py
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
API_PORT = 8489
STUB_PORT = 8490
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


def make_profile(tok, insurance):
    body = {
        "bike_year": 2021, "bike_make": "Harley-Davidson",
        "bike_model": "Road Glide", "city": "Springfield", "state": "MO",
        "service_radius_miles": 50,
        "latitude": 37.21, "longitude": -93.29,
        "is_available": True, "has_endorsement": True,
        "pledge_accepted": True, "liability_accepted": True,
    }
    body.update(insurance)
    s, prof = api("POST", "/bikers/profile", token=tok, body=body)
    assert s in (200, 201), f"profile create failed: {s} {prof}"
    return prof


def book_ride(tok, pickup, name="Msg Rider"):
    s, ride = api("POST", "/rides/", token=tok, body={
        "ride_type": "ride", "pickup_address": pickup,
        "dropoff_address": "200 Oak Ave, Springfield, MO",
        "scheduled_at": None, "estimated_miles": 20,
        "notes": None, "helmet_needed": False,
        "waiver_signed": True, "waiver_name": name,
        "waiver_date": "2026-10-08",
    })
    assert s == 201, f"book ride failed: {s} {ride}"
    return ride


def main() -> int:
    stub = HTTPServer(("127.0.0.1", STUB_PORT), StubHandler)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    if not wait_for_server(STUB_PORT):
        print("FAIL - stub geocoder did not start")
        return 1

    tmp = __import__("tempfile").mkdtemp(prefix="rab_messages_test_")
    db_path = os.path.join(tmp, "test.db")
    upload_dir = os.path.join(tmp, "uploads")
    admin_password = "admin-pass-1234"
    env = dict(
        os.environ,
        RAB_DATABASE_URL=f"sqlite:///{db_path}",
        RAB_ADMIN_EMAIL="messages.admin@example.com",
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
            ">💬 Messages</button>",          # thread toggle buttons
            "'msgtoggle-' + key",             # per-ride toggle button lookup
            "id=\"msgsend-' + key",          # send button in the thread shell
            "id=\"msgin-' + key",            # message input in the thread shell
            "async function toggleThread(rideId, ctx)",
            "async function refreshThread(rideId, ctx)",
            "async function sendMsg(rideId, ctx)",
            "function wireThread(rideId, ctx)",
            "'/messages?after_id='",
            "My matched rides",              # biker's assigned-rides panel
            "async function loadBikerRides()",
            "'/rides/assigned'",
            "MSG_POLL_MS",                   # polling first
            "stopAllThreads()",              # timers cleaned on account switch
            "function threadShell(ctx, rideId)",
            "bikerRidesWrap",                # admin doesn't see the biker panel
        ]
        missing = [m for m in markers if m not in html]
        check("messaging UI markup present",
              not missing, f"missing={missing}")

        # --- 2. OpenAPI contract ---
        s, spec = api("GET", "/openapi.json")
        paths = spec.get("paths", {}) if isinstance(spec, dict) else {}
        check("GET /rides/{ride_id}/messages exists",
              "/rides/{ride_id}/messages" in paths
              and "get" in paths.get("/rides/{ride_id}/messages", {}))
        check("POST /rides/{ride_id}/messages exists",
              "/rides/{ride_id}/messages" in paths
              and "post" in paths.get("/rides/{ride_id}/messages", {}))
        check("GET /rides/assigned exists",
              "/rides/assigned" in paths
              and "get" in paths.get("/rides/assigned", {}))

        # --- 3. accounts ---
        rider_tok, _ = register("msg.rider@example.com", "rider",
                                "Msg Rider", phone="417-555-0301")
        outsider_tok, _ = register("msg.outsider@example.com", "rider",
                                   "Msg Outsider")
        biker_tok, biker_id = register("msg.biker@example.com", "biker",
                                       "Msg Biker", phone="417-555-0302")
        admin_tok = login("messages.admin@example.com", admin_password)

        # --- 4. book + assign (pending thread access before a biker exists) ---
        pending_ride = book_ride(rider_tok, "100 Main St, Springfield, MO")
        ride_id = pending_ride["id"]
        s, msgs = api("GET", f"/rides/{ride_id}/messages", token=rider_tok)
        check("pending ride: rider reads empty thread", s == 200 and msgs == [], f"{s} {msgs}")
        s, err = api("GET", f"/rides/{ride_id}/messages", token=biker_tok)
        check("pending ride: unassigned biker gets 403", s == 403, f"status={s}")
        s, err = api("GET", f"/rides/{ride_id}/messages")
        check("anonymous gets 401 on thread read", s == 401, f"status={s}")
        s, err = api("POST", f"/rides/{ride_id}/messages", token=biker_tok,
                     body={"body": "not my ride"})
        check("unassigned biker gets 403 posting", s == 403, f"status={s}")

        # --- 5. assign the biker -> matched ---
        prof = make_profile(biker_tok, {"has_insurance": True,
                                        "insurance_covers_passenger": True})
        s, _ = api("POST", f"/admin/bikers/{prof['id']}/approve", token=admin_tok)
        assert s == 200
        s, ride = api("POST", f"/admin/rides/{ride_id}/assign",
                      token=admin_tok, body={"biker_user_id": biker_id})
        assert s == 200, f"assign failed: {s} {ride}"

        # --- 6. the conversation ---
        s, m1 = api("POST", f"/rides/{ride_id}/messages", token=rider_tok,
                    body={"body": "Hi! I'll be at the curb in 5."})
        check("rider posts message -> 201", s == 201, f"status={s} {m1}")
        check("message shape carries sender name/role",
              m1.get("sender_name") == "Msg Rider"
              and m1.get("sender_role") == "rider"
              and m1.get("ride_id") == ride_id
              and m1.get("body") == "Hi! I'll be at the curb in 5.", str(m1))

        s, msgs = api("GET", f"/rides/{ride_id}/messages", token=biker_tok)
        check("assigned biker reads the rider's message",
              s == 200 and len(msgs) == 1 and msgs[0]["id"] == m1["id"],
              f"{s} {msgs}")

        s, m2 = api("POST", f"/rides/{ride_id}/messages", token=biker_tok,
                    body={"body": "Got it — see you there."})
        check("biker replies -> 201", s == 201, f"status={s} {m2}")

        s, msgs = api("GET", f"/rides/{ride_id}/messages?after_id={m1['id']}",
                      token=rider_tok)
        check("polling after_id returns only the new message",
              s == 200 and len(msgs) == 1 and msgs[0]["id"] == m2["id"],
              f"{s} {msgs}")

        s, msgs = api("GET", f"/rides/{ride_id}/messages", token=admin_tok)
        check("Kenneth reads the whole thread",
              s == 200 and len(msgs) == 2, f"{s} {len(msgs) if isinstance(msgs, list) else msgs}")
        s, m3 = api("POST", f"/rides/{ride_id}/messages", token=admin_tok,
                    body={"body": "Enjoy the ride, you two!"})
        check("Kenneth posts as admin -> 201 + admin role tag",
              s == 201 and m3.get("sender_role") == "admin", f"{s} {m3}")

        # --- 7. outsider + validation guards ---
        s, _ = api("GET", f"/rides/{ride_id}/messages", token=outsider_tok)
        check("outsider rider gets 403 on thread read", s == 403, f"status={s}")
        s, _ = api("POST", f"/rides/{ride_id}/messages", token=outsider_tok,
                   body={"body": "hello?"})
        check("outsider rider gets 403 posting", s == 403, f"status={s}")
        s, err = api("POST", f"/rides/{ride_id}/messages", token=rider_tok,
                     body={"body": "   "})
        check("blank message -> 400", s == 400, f"status={s} {err}")
        s, err = api("POST", f"/rides/{ride_id}/messages", token=rider_tok,
                     body={"body": "x" * 2001})
        check("overlong message -> 422", s == 422, f"status={s}")
        s, _ = api("GET", "/rides/999999/messages", token=rider_tok)
        check("unknown ride -> 404", s == 404, f"status={s}")
        s, _ = api("POST", "/rides/999999/messages", token=rider_tok,
                   body={"body": "hi"})
        check("posting to unknown ride -> 404", s == 404, f"status={s}")

        # --- 8. biker's assigned-rides endpoint ---
        s, assigned = api("GET", "/rides/assigned", token=biker_tok)
        check("biker sees their assigned ride",
              s == 200 and len(assigned) == 1 and assigned[0]["id"] == ride_id,
              f"{s} {assigned}")
        s, _ = api("GET", "/rides/assigned")
        check("anonymous gets 401 on /rides/assigned", s == 401, f"status={s}")
        biker2_tok, _ = register("msg.biker2@example.com", "biker", "Msg Biker2")
        s, assigned2 = api("GET", "/rides/assigned", token=biker2_tok)
        check("biker with no assignments gets empty list",
              s == 200 and assigned2 == [], f"{s} {assigned2}")

        # --- 9. cancelled ride: thread history stays readable, posting blocked ---
        s, _ = api("POST", f"/rides/{ride_id}/cancel", token=rider_tok)
        assert s == 200
        s, msgs = api("GET", f"/rides/{ride_id}/messages", token=rider_tok)
        check("thread history still readable after cancel",
              s == 200 and len(msgs) == 3, f"{s} {msgs}")
        s, err = api("POST", f"/rides/{ride_id}/messages", token=rider_tok,
                     body={"body": "after the fact"})
        check("posting on a cancelled ride -> 400", s == 400, f"status={s} {err}")

        # --- 10. branding scan ---
        s, html = get_raw("/")
        brand_terms = ["uber", "lyft"]
        hits = [t for t in brand_terms if t in html.lower()]
        check("frontend branding-clean", not hits, f"hits={hits}")
        import glob
        src_hits = []
        for f in glob.glob(os.path.join(HERE, "app", "*.py")) + \
                 glob.glob(os.path.join(HERE, "app", "routers", "*.py")):
            with open(f) as fh:
                low = fh.read().lower()
            if any(t in low for t in brand_terms):
                src_hits.append(os.path.basename(f))
        check("backend source branding-clean", not src_hits, f"hits={src_hits}")

        failed = [n for n, ok, d in CHECKS if not ok]
        print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
        return 0 if not failed else 1
    finally:
        server.terminate()
        stub.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
