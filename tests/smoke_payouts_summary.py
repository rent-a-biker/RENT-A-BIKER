#!/usr/bin/env python3
"""Smoke test: M7 part 2 — Kenneth's payout summary ledger.

Starts a stub Nominatim server (no real network), then the API with a
throwaway SQLite DB + env-seeded admin, and:

  1. checks the served frontend renders the payout-summary strip and the JS
     only references endpoints present in the live OpenAPI schema;
  2. verifies the OpenAPI contract for GET /admin/payouts/summary;
  3. checks the 403/401 guards (rider 403, anonymous 401);
  4. walks a two-biker scenario: Biker A completes a 20-mi ride (5000c) and a
     3-hr event (22500c), one marked paid; Biker B completes a 10-mi ride
     (2500c), pending — then asserts the summary totals, the per-biker
     breakdown, and the pending-most-owed-first sort order;
  5. runs a clean branding scan.

Run from hidden_files/src/:  ./.venv/bin/python tests/smoke_payouts_summary.py
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
API_PORT = 8495
STUB_PORT = 8496
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
        "waiver_signed": True, "waiver_name": "Summary Rider",
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

    tmp = __import__("tempfile").mkdtemp(prefix="rab_paysum_test_")
    db_path = os.path.join(tmp, "test.db")
    upload_dir = os.path.join(tmp, "uploads")
    admin_password = "summary-admin-pw"
    env = dict(
        os.environ,
        RAB_DATABASE_URL=f"sqlite:///{db_path}",
        RAB_ADMIN_EMAIL="paysum.admin@example.com",
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
            "Payout summary",               # summary strip heading
            'id="payoutSummary"',           # summary strip container
            "/admin/payouts/summary",       # summary endpoint path
            "function renderPayoutSummary",
            "Owe bikers",                   # summary strip copy
            "Already paid",                 # summary strip copy
        ]
        for m in markers:
            check(f"frontend marker present: {m[:40]}", m in html)

        s, schema = api("GET", "/openapi.json")
        paths = schema.get("paths", {}) if s == 200 else {}
        check("OpenAPI has GET /admin/payouts/summary",
              "/admin/payouts/summary" in paths
              and "get" in paths["/admin/payouts/summary"])
        # the int-typed mark-paid route must not swallow /summary
        check("mark-paid route still intact",
              "/admin/payouts/{payout_id}/mark-paid" in paths)

        # --- 2. guards ---
        rider_tok, _ = register("paysum.rider@example.com", "rider",
                                "Summary Rider", "555-0140")
        s, _ = api("GET", "/admin/payouts/summary", token=rider_tok)
        check("rider -> 403", s == 403, f"status={s}")
        s, _ = api("GET", "/admin/payouts/summary")
        check("anonymous -> 401", s == 401, f"status={s}")

        # --- 3. setup: admin + two approved bikers ---
        admin_tok = login("paysum.admin@example.com", admin_password)
        bikers = []
        for i, name in enumerate(("Sum Biker A", "Sum Biker B"), start=1):
            tok, uid = register(f"paysum.biker{i}@example.com", "biker",
                                name, f"555-014{i}")
            prof_body = {
                "bike_year": 2021, "bike_make": "Harley-Davidson",
                "bike_model": "Road Glide", "city": "Springfield", "state": "MO",
                "service_radius_miles": 50,
                "latitude": 37.21, "longitude": -93.29,
                "is_available": True, "has_endorsement": True,
                "pledge_accepted": True, "liability_accepted": True,
            }
            s, prof = api("POST", "/bikers/profile", token=tok, body=prof_body)
            assert s == 200, f"biker {i} profile failed: {s} {prof}"
            s, _ = api("POST", f"/admin/bikers/{prof['id']}/approve",
                       token=admin_tok)
            assert s == 200, f"approve biker {i} failed: {s}"
            bikers.append((tok, uid, name))
        (a_tok, a_uid, a_name), (b_tok, b_uid, b_name) = bikers

        # --- 4. empty summary before any payouts ---
        s, empty = api("GET", "/admin/payouts/summary", token=admin_tok)
        check("empty summary -> zeros",
              s == 200 and empty["pending"]["count"] == 0
              and empty["paid"]["count"] == 0
              and empty["by_biker"] == [], str(empty))

        # --- 5. Biker A: 20-mi ride (5000c) + 3-hr event (22500c), one paid ---
        s, r1 = book_ride(rider_tok, "ride", "100 Main St, Springfield, MO",
                          miles=20)
        assert s == 201, f"book r1 failed: {s} {r1}"
        s, ev = book_ride(rider_tok, "event", "100 Main St, Springfield, MO",
                          hours=3)
        assert s == 201, f"book event failed: {s} {ev}"
        for rid in (r1["id"], ev["id"]):
            s, _ = api("POST", f"/admin/rides/{rid}/assign", token=admin_tok,
                       body={"biker_user_id": a_uid})
            assert s == 200, f"assign {rid} failed: {s}"
            s, _ = api("POST", f"/rides/{rid}/complete", token=admin_tok)
            assert s == 200, f"complete {rid} failed: {s}"
        s, pending = api("GET", "/admin/payouts?status=pending", token=admin_tok)
        assert s == 200 and len(pending) == 2, f"queue: {s} {pending}"
        payout_id = next(p["id"] for p in pending if p["ride_id"] == r1["id"])
        s, _ = api("POST", f"/admin/payouts/{payout_id}/mark-paid",
                   token=admin_tok, body={"reference": "PP-SUM-1"})
        check("mark A's ride paid -> 200", s == 200, f"status={s}")

        # --- 6. Biker B: 10-mi ride (2500c), stays pending ---
        s, r2 = book_ride(rider_tok, "ride", "100 Main St, Springfield, MO",
                          miles=10)
        assert s == 201, f"book r2 failed: {s} {r2}"
        s, _ = api("POST", f"/admin/rides/{r2['id']}/assign", token=admin_tok,
                   body={"biker_user_id": b_uid})
        assert s == 200, f"assign r2 failed: {s}"
        s, _ = api("POST", f"/rides/{r2['id']}/complete", token=admin_tok)
        assert s == 200, f"complete r2 failed: {s}"

        # --- 7. summary math ---
        s, summ = api("GET", "/admin/payouts/summary", token=admin_tok)
        check("summary -> 200", s == 200, f"status={s}")
        pen, paid = summ["pending"], summ["paid"]
        # pending: event (22500c -> 16875/5625) + B's ride (2500c -> 1875/625)
        check("pending totals exact",
              pen["count"] == 2 and pen["fare_cents"] == 25000
              and pen["biker_cents"] == 18750
              and pen["platform_cents"] == 6250, str(pen))
        # paid: A's 20-mi ride (5000c -> 3750/1250)
        check("paid totals exact",
              paid["count"] == 1 and paid["fare_cents"] == 5000
              and paid["biker_cents"] == 3750
              and paid["platform_cents"] == 1250, str(paid))
        bk = summ["by_biker"]
        check("two bikers in breakdown, most-owed first",
              len(bk) == 2 and bk[0]["biker_name"] == a_name
              and bk[1]["biker_name"] == b_name,
              str([b["biker_name"] for b in bk]))
        a, b = bk
        check("biker A breakdown (1 pending + 1 paid)",
              a["pending_count"] == 1 and a["pending_biker_cents"] == 16875
              and a["pending_platform_cents"] == 5625
              and a["paid_count"] == 1 and a["paid_biker_cents"] == 3750,
              str(a))
        check("biker B breakdown (1 pending, nothing paid)",
              b["biker_id"] == b_uid and b["pending_count"] == 1
              and b["pending_biker_cents"] == 1875
              and b["pending_platform_cents"] == 625
              and b["paid_count"] == 0 and b["paid_biker_cents"] == 0,
              str(b))

        # --- 8. frontend JS only calls registered endpoints ---
        import re
        called = set(re.findall(r"api\('(?:GET|POST|PUT|DELETE)', '([^']+)'", html))
        called |= set(re.findall(r'"/admin/[^"]*"', html))
        # fragments: strip query strings; a fragment passes if it exactly
        # matches an OpenAPI path or is a prefix of one (the JS appends
        # {id}-style segments afterwards, e.g. '/admin/payouts/' + id + ...)
        missing = []
        for c in called:
            frag = c.split("?", 1)[0]
            if not frag.startswith("/admin/"):
                continue
            if not any(p == frag or p.startswith(frag) for p in paths):
                missing.append(c)
        check("frontend admin endpoints all in OpenAPI", not missing,
              str(missing) if missing else f"{len(called)} endpoints checked")

        # --- 9. branding scan (app source only — test files carry the
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
