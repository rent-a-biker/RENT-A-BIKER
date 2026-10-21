#!/usr/bin/env python3
"""Smoke test: M7 part 1 — ride completion + payout ledger (75/25) + Kenneth's payout queue.

Starts a stub Nominatim server (no real network), then the API with a
throwaway SQLite DB + env-seeded admin, and:

  1. checks the served frontend renders the matched-rides + payout-queue UI
     and the JS only calls endpoints present in the live OpenAPI schema;
  2. verifies the OpenAPI contract for POST /rides/{ride_id}/complete,
     GET /admin/matched, GET /admin/payouts, POST /admin/payouts/{id}/mark-paid;
  3. walks the full flow: rider books a 20-mi ride (5000c fare) -> admin
     assigns the biker -> the biker completes it -> the payout record lands
     with the exact 75/25 split (3750c / 1250c) in Kenneth's queue -> he
     marks it paid with a PayPal reference;
  4. checks the error cases (complete a pending ride, double-complete,
     cancel after complete, outsider/rider 403s, double mark-paid) and that
     an event ride (3 hr -> 22500c) splits 16875c / 5625c;
  5. runs a clean branding scan.

Run from hidden_files/src/:  ./.venv/bin/python tests/smoke_payouts.py
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
API_PORT = 8493
STUB_PORT = 8494
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


def book_ride(tok, ride_type, address, miles=None, hours=None):
    body = {
        "ride_type": ride_type, "pickup_address": address,
        "dropoff_address": "200 Oak Ave, Springfield, MO",
        "scheduled_at": None, "notes": None, "helmet_needed": False,
        "waiver_signed": True, "waiver_name": "Payout Rider",
        "waiver_date": "2026-10-08",
    }
    if ride_type == "ride":
        body["estimated_miles"] = miles
    else:
        body["duration_hours"] = hours
    return api("POST", "/rides/", token=tok, body=body)


def main() -> int:
    stub = HTTPServer(("127.0.0.1", STUB_PORT), StubHandler)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    if not wait_for_server(STUB_PORT):
        print("FAIL - stub geocoder did not start")
        return 1

    tmp = __import__("tempfile").mkdtemp(prefix="rab_payout_test_")
    db_path = os.path.join(tmp, "test.db")
    upload_dir = os.path.join(tmp, "uploads")
    admin_password = "admin-pass-1234"
    env = dict(
        os.environ,
        RAB_DATABASE_URL=f"sqlite:///{db_path}",
        RAB_ADMIN_EMAIL="payout.admin@example.com",
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
            "Matched rides",                # admin matched-rides section
            "Payout queue",                 # admin payout queue section
            "Ride complete",                # complete button label
            "Mark paid",                    # payout action button
            'id="matchedList"',
            'id="payoutsList"',
            'id="payoutsErr"',
            "/admin/matched",
            "/admin/payouts",
            "/complete",                    # complete endpoint path
            "/mark-paid",                   # mark-paid endpoint path
            "async function doCompleteRide",
            "async function loadMatched",
            "async function loadPayouts",
            "async function markPayoutPaid",
            "setPayoutFilter",
        ]
        for m in markers:
            check(f"frontend marker present: {m[:40]}", m in html)

        s, schema = api("GET", "/openapi.json")
        paths = schema.get("paths", {}) if s == 200 else {}
        for p, meth in [("/rides/{ride_id}/complete", "post"),
                        ("/admin/matched", "get"),
                        ("/admin/payouts", "get"),
                        ("/admin/payouts/{payout_id}/mark-paid", "post")]:
            check(f"OpenAPI has {meth.upper()} {p}",
                  p in paths and meth in paths[p])

        # --- 2. setup: rider, biker (approved + available), admin ---
        admin_tok = login("payout.admin@example.com", admin_password)
        rider_tok, rider_id = register("payout.rider@example.com", "rider",
                                      "Payout Rider", "555-0130")
        biker_tok, biker_uid = register("payout.biker@example.com", "biker",
                                        "Payout Biker", "555-0131")
        prof_body = {
            "bike_year": 2021, "bike_make": "Harley-Davidson",
            "bike_model": "Road Glide", "city": "Springfield", "state": "MO",
            "service_radius_miles": 50,
            "latitude": 37.21, "longitude": -93.29,
            "is_available": True, "has_endorsement": True,
            "pledge_accepted": True, "liability_accepted": True,
        }
        s, prof = api("POST", "/bikers/profile", token=biker_tok, body=prof_body)
        check("biker profile -> 200", s == 200, f"status={s}")
        s, _ = api("POST", f"/admin/bikers/{prof['id']}/approve",
                   token=admin_tok, body={"reason": None})
        check("admin approve -> 200", s == 200, f"status={s}")

        # --- 3. book a 20-mi ride (fare 5000c), assign, complete ---
        s, ride = book_ride(rider_tok, "ride", "100 Main St, Springfield, MO",
                           miles=20)
        check("booking -> 201", s == 201, f"status={s}")
        check("fare is 5000c for 20 mi",
              ride.get("fare_estimate_cents") == 5000,
              str(ride.get("fare_estimate_cents")))
        ride_id = ride["id"]

        s, second = book_ride(rider_tok, "ride", "100 Main St, Springfield, MO",
                              miles=5)
        check("second ride booked (stays pending) -> 201", s == 201)
        second_id = second["id"]

        s, _ = api("POST", f"/rides/{second_id}/complete", token=admin_tok)
        check("cannot complete a pending ride -> 400", s == 400)

        s, _ = api("POST", f"/rides/{second_id}/complete", token=biker_tok)
        check("unassigned biker cannot complete someone else's ride -> 403",
              s == 403)

        s, _ = api("POST", f"/rides/{ride_id}/complete")
        check("anonymous complete -> 401", s == 401)

        s, detail = api("POST", f"/admin/rides/{ride_id}/assign",
                        token=admin_tok, body={"biker_user_id": biker_uid})
        check("admin assign -> matched", s == 200
              and detail.get("status") == "matched", f"status={s}")

        s, _ = api("POST", f"/rides/{ride_id}/complete", token=rider_tok)
        check("rider cannot complete the ride -> 403", s == 403)

        outsider_tok, _ = register("payout.outsider@example.com", "rider",
                                  "Outsider", None)
        s, _ = api("POST", f"/rides/{ride_id}/complete", token=outsider_tok)
        check("outsider cannot complete the ride -> 403", s == 403)

        s, matched = api("GET", "/admin/matched", token=admin_tok)
        check("admin matched view shows the ride",
              s == 200 and len(matched.get("requests", [])) == 1
              and matched["requests"][0].get("biker", {}).get("full_name")
              == "Payout Biker")
        s, _ = api("GET", "/admin/matched", token=rider_tok)
        check("rider cannot see admin matched view -> 403", s == 403)

        # --- 4. the biker completes the ride: payout record created ---
        s, done = api("POST", f"/rides/{ride_id}/complete", token=biker_tok)
        check("biker completes the ride -> 200", s == 200, f"status={s}")
        check("ride status -> completed",
              (done.get("ride") or {}).get("status") == "completed")
        payout = done.get("payout") or {}
        check("payout fare 5000c",
              payout.get("fare_cents") == 5000, str(payout.get("fare_cents")))
        check("payout split exact 75/25 (3750c / 1250c)",
              payout.get("biker_share_cents") == 3750
              and payout.get("platform_share_cents") == 1250,
              f"biker={payout.get('biker_share_cents')} "
              f"platform={payout.get('platform_share_cents')}")
        check("payout status pending with name briefs",
              payout.get("status") == "pending"
              and (payout.get("biker") or {}).get("full_name") == "Payout Biker"
              and (payout.get("rider") or {}).get("full_name") == "Payout Rider")
        payout_id = payout["id"]

        s, _ = api("POST", f"/rides/{ride_id}/complete", token=biker_tok)
        check("double complete -> 400", s == 400)
        s, _ = api("POST", f"/rides/{ride_id}/cancel", token=rider_tok)
        check("cannot cancel a completed ride -> 400", s == 400)

        s, matched = api("GET", "/admin/matched", token=admin_tok)
        check("completed ride leaves the matched view",
              s == 200 and len(matched.get("requests", [])) == 0)

        s, mine = api("GET", "/rides/me", token=rider_tok)
        statuses = {r.get("id"): r.get("status") for r in (mine or [])}
        check("rider my-rides shows completed",
              statuses.get(ride_id) == "completed", str(statuses))

        # --- 5. Kenneth's payout queue ---
        s, _ = api("GET", "/admin/payouts")
        check("anonymous payouts -> 401", s == 401)
        s, _ = api("GET", "/admin/payouts", token=biker_tok)
        check("biker cannot see the payout queue -> 403", s == 403)

        s, queue = api("GET", "/admin/payouts", token=admin_tok)
        check("admin payouts -> 1 record", s == 200 and len(queue or []) == 1,
              f"status={s} n={len(queue or [])}")
        entry = (queue or [{}])[0]
        check("queue record carries split + names",
              entry.get("fare_cents") == 5000
              and entry.get("biker_share_cents") == 3750
              and entry.get("platform_share_cents") == 1250
              and entry.get("biker", {}).get("full_name") == "Payout Biker"
              and entry.get("rider", {}).get("full_name") == "Payout Rider")

        s, pending = api("GET", "/admin/payouts?status=pending", token=admin_tok)
        s2, paid = api("GET", "/admin/payouts?status=paid", token=admin_tok)
        check("status filter: 1 pending, 0 paid",
              s == 200 and len(pending or []) == 1
              and s2 == 200 and len(paid or []) == 0)

        s, _ = api("POST", "/admin/payouts/99999/mark-paid",
                   token=admin_tok, body={"reference": None})
        check("mark-paid unknown payout -> 404", s == 404)

        s, paid_rec = api("POST", f"/admin/payouts/{payout_id}/mark-paid",
                          token=admin_tok, body={"reference": "PP-TEST-1"})
        check("mark-paid -> 200", s == 200, f"status={s}")
        check("payout paid with reference recorded",
              paid_rec.get("status") == "paid"
              and paid_rec.get("paid_reference") == "PP-TEST-1"
              and paid_rec.get("paid_at") is not None)

        s, _ = api("POST", f"/admin/payouts/{payout_id}/mark-paid",
                   token=admin_tok, body={"reference": None})
        check("double mark-paid -> 400", s == 400)

        s, pending = api("GET", "/admin/payouts?status=pending", token=admin_tok)
        s2, paid = api("GET", "/admin/payouts?status=paid", token=admin_tok)
        check("queue flips: 0 pending, 1 paid",
              s == 200 and len(pending or []) == 0
              and s2 == 200 and len(paid or []) == 1)

        # --- 6. event ride split math (3 hr -> 22500c) ---
        s, event = book_ride(rider_tok, "event", "100 Main St, Springfield, MO",
                             hours=3)
        check("event booking -> 201", s == 201, f"status={s}")
        check("event fare is 22500c for 3 hr",
              event.get("fare_estimate_cents") == 22500,
              str(event.get("fare_estimate_cents")))
        s, detail = api("POST", f"/admin/rides/{event['id']}/assign",
                        token=admin_tok, body={"biker_user_id": biker_uid})
        check("assign event -> matched", s == 200)
        s, done = api("POST", f"/rides/{event['id']}/complete", token=admin_tok)
        ep = (done.get("payout") or {}) if s == 200 else {}
        check("event split exact 75/25 (16875c / 5625c)",
              s == 200 and ep.get("biker_share_cents") == 16875
              and ep.get("platform_share_cents") == 5625,
              f"s={s} biker={ep.get('biker_share_cents')}")

        # --- 7. branding scan (app source only — test files carry the
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
