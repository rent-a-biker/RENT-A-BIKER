#!/usr/bin/env python3
"""Smoke test: M4 dispatch backend (admin overview, assign/unassign,
pickup geocoding, insurance fields).

Starts a stub Nominatim server (so no real network is hit), then the API with
a throwaway SQLite DB + env-seeded admin, and walks the dispatch flow with
stdlib-only HTTP calls. Exits 0 if every check passes.

Run from hidden_files/src/:  ./.venv/bin/python tests/smoke_dispatch.py
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
API_PORT = 8481
STUB_PORT = 8482
BASE = f"http://127.0.0.1:{API_PORT}"
CHECKS: list[tuple[str, bool, str]] = []

# Stub "Nominatim": address fragment (lowercase) -> (lat, lon)
STUB_ADDRESSES = {
    "100 main st": (37.20, -93.30),   # Springfield-ish pickup
    "200 oak ave": (37.21, -93.29),   # ~1 mi away
}
STUB_HITS: list[dict] = []


class StubHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        q = qs.get("q", [""])[0]
        STUB_HITS.append({"q": q, "ua": self.headers.get("User-Agent", "")})
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


PROFILE_BODY = {
    "bike_year": 2020, "bike_make": "Harley-Davidson", "bike_model": "Street Bob",
    "service_radius_miles": 50, "is_available": True,
    "has_endorsement": True, "pledge_accepted": True, "liability_accepted": True,
    "city": "Springfield", "state": "MO",
}


def make_biker(tok, **kw):
    return api("POST", "/bikers/profile", token=tok,
               body=dict(PROFILE_BODY, **kw))


RIDE_BODY = {
    "ride_type": "ride", "estimated_miles": 10,
    "waiver_signed": True, "waiver_name": "Rider One", "waiver_date": "2026-10-07",
}


def make_ride(tok, pickup):
    return api("POST", "/rides/", token=tok,
               body=dict(RIDE_BODY, pickup_address=pickup))


def approve(pid, admin_tok):
    s, _ = api("POST", f"/admin/bikers/{pid}/approve", token=admin_tok)
    assert s == 200, f"approve {pid} failed: {s}"


def main() -> int:
    stub = HTTPServer(("127.0.0.1", STUB_PORT), StubHandler)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    if not wait_for_server(STUB_PORT):
        print("FAIL - stub geocoder did not start")
        return 1

    tmp = __import__("tempfile").mkdtemp(prefix="rab_dispatch_test_")
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

        rider_tok, rider_id = register("rider1@example.com", "rider",
                                       "Rider One", "555-0101")
        rider2_tok, _ = register("rider2@example.com", "rider", "Rider Two")

        # --- 1. booking geocodes the pickup via stub ---
        hits_before = len(STUB_HITS)
        s, r1 = make_ride(rider_tok, "100 Main St, Springfield, MO")
        ok = (s == 201 and r1["pickup_latitude"] == 37.2
              and r1["pickup_longitude"] == -93.3)
        check("booking geocodes pickup address", ok,
              f"status={s} lat={r1.get('pickup_latitude') if isinstance(r1, dict) else '?'}")
        check("exactly one geocode lookup for the pickup",
              len(STUB_HITS) == hits_before + 1, f"hits={len(STUB_HITS)}")
        r1_id = r1["id"]

        # --- 2. same address reuses the cache ---
        hits_before = len(STUB_HITS)
        s, r2 = make_ride(rider2_tok, "100 Main St, Springfield, MO")
        check("second same-address booking uses the cache (no new hit)",
              s == 201 and r2["pickup_latitude"] == 37.2
              and len(STUB_HITS) == hits_before,
              f"status={s} hits={len(STUB_HITS)}")
        r2_id = r2["id"]

        # --- 3. unknown address -> graceful empty coords ---
        s, r3 = make_ride(rider2_tok, "999 Nowhere Rd, Unknown, ZZ")
        check("unknown address books fine with empty pickup coords",
              s == 201 and r3["pickup_latitude"] is None
              and r3["pickup_longitude"] is None,
              f"status={s}")

        # --- 4. seed bikers with manual coords (no geocode hits) ---
        hits_before = len(STUB_HITS)
        a_tok, a_uid = register("biker-a@example.com", "biker", "Biker A", "555-0201")
        b_tok, b_uid = register("biker-b@example.com", "biker", "Biker B", "555-0202")
        c_tok, c_uid = register("biker-c@example.com", "biker", "Biker C", "555-0203")
        d_tok, d_uid = register("biker-d@example.com", "biker", "Biker D", "555-0204")
        e_tok, e_uid = register("biker-e@example.com", "biker", "Biker E", "555-0205")
        f_tok, f_uid = register("biker-f@example.com", "biker", "Biker F", "555-0206")
        s, pa = make_biker(a_tok, latitude=37.21, longitude=-93.29)   # ~1 mi
        s, pb = make_biker(b_tok, latitude=37.50, longitude=-93.30)    # ~21 mi
        s, pc = make_biker(c_tok, latitude=40.00, longitude=-95.00)   # far away
        s, pd = make_biker(d_tok, latitude=37.22, longitude=-93.30)   # ~1.4 mi, stays pending
        s, pe = make_biker(e_tok, latitude=37.20, longitude=-93.30,
                           is_available=False)                        # available off
        s, pf = make_biker(f_tok, latitude=37.20, longitude=-93.30,   # 0.0 mi, insured
                           has_insurance=True, insurance_covers_passenger=False,
                           passenger_responsibility_accepted=True,
                           passenger_responsibility_name="Biker F",
                           passenger_responsibility_date="2026-10-07",
                           insurance_company="TestCo",
                           insurance_policy_number="POL-123")
        g_tok, g_uid = register("biker-g@example.com", "biker", "Biker G")
        all_ok = all(x["latitude"] is not None for x in (pa, pb, pc, pd, pe, pf)
                     if isinstance(x, dict))
        check("six biker profiles created with manual coords",
              all_ok and len(STUB_HITS) == hits_before,
              f"hits={len(STUB_HITS)}")
        for pid in (pa["id"], pb["id"], pc["id"], pe["id"], pf["id"]):
            approve(pid, admin_tok)
        check("own profile exposes insurance fields",
              s == 200 and pf["has_insurance"] is True
              and pf["insurance_company"] == "TestCo"
              and pf["passenger_responsibility_accepted"] is True,
              f"status={s}")

        # --- 5. insurance rule: insured but no passenger coverage + no
        # agreement -> 400; agreement flips it to 200 ---
        s, bad = make_biker(g_tok, has_insurance=True,
                            insurance_covers_passenger=False,
                            passenger_responsibility_accepted=False)
        check("insurance rule enforced (insured, no coverage, no agreement -> 422)",
              s == 422, f"status={s}")
        s, good = make_biker(g_tok, has_insurance=True,
                             insurance_covers_passenger=True)
        check("insurance with passenger coverage saves fine",
              s == 200 and good["insurance_covers_passenger"] is True,
              f"status={s}")

        # --- 6. dispatch overview ---
        s, board = api("GET", "/admin/dispatch", token=admin_tok)
        check("dispatch overview returns 200", s == 200, f"status={s}")
        by_id = {r["id"]: r for r in board["requests"]} if s == 200 else {}
        check("all three pending rides listed", set(by_id) == {r1_id, r2_id, r3["id"]},
              f"ids={sorted(by_id)}")

        ride1 = by_id.get(r1_id, {})
        check("rider contact info on the ride",
              ride1.get("rider", {}).get("email") == "rider1@example.com"
              and ride1["rider"].get("phone") == "555-0101",
              str(ride1.get("rider")))
        cands = ride1.get("candidates", [])
        cand_names = [c["full_name"] for c in cands]
        dists = [c["distance_miles"] for c in cands]
        check("candidates sorted nearest-first with distances",
              cand_names == ["Biker F", "Biker A", "Biker B"]
              and dists == sorted(d for d in dists if d is not None)
              and abs(dists[0] - 0.0) < 0.01 and 0.5 < dists[1] < 2.0
              and 15.0 < dists[2] < 30.0,
              f"names={cand_names} dists={dists}")
        check("far biker excluded by service radius",
              "Biker C" not in cand_names, f"names={cand_names}")
        check("pending-approval biker excluded",
              "Biker D" not in cand_names, f"names={cand_names}")
        check("unavailable biker excluded",
              "Biker E" not in cand_names, f"names={cand_names}")
        f_cand = next(c for c in cands if c["full_name"] == "Biker F")
        check("candidate shows contact info",
              f_cand["email"] == "biker-f@example.com"
              and f_cand["phone"] == "555-0206", f_cand["email"])
        check("candidate shows insurance status",
              f_cand["has_insurance"] is True
              and f_cand["insurance_covers_passenger"] is False
              and f_cand["passenger_responsibility_accepted"] is True
              and f_cand["insurance_company"] == "TestCo"
              and "insurance_policy_number" not in f_cand,
              f"flags={ {k: f_cand.get(k) for k in ('has_insurance','insurance_covers_passenger','passenger_responsibility_accepted','insurance_company')} }")

        ride3 = by_id.get(r3["id"], {})
        c3 = [c["full_name"] for c in ride3.get("candidates", [])]
        check("ride without pickup coords lists all eligible bikers, no distances",
              set(c3) == {"Biker F", "Biker A", "Biker B", "Biker C"}
              and all(c["distance_miles"] is None
                      for c in ride3.get("candidates", [])),
              f"names={c3}")

        # --- 7. dispatch guards ---
        s, _ = api("GET", "/admin/dispatch", token=rider_tok)
        check("rider cannot open dispatch board", s == 403, f"status={s}")
        s, _ = api("GET", "/admin/dispatch", token=a_tok)
        check("biker cannot open dispatch board", s == 403, f"status={s}")
        s, _ = api("GET", "/admin/dispatch")
        check("dispatch board needs a token", s == 401, f"status={s}")

        # --- 8. manual assign ---
        s, assigned = api("POST", f"/admin/rides/{r1_id}/assign",
                          token=admin_tok, body={"biker_user_id": a_uid})
        check("assign moves ride to matched with the biker",
              s == 200 and assigned["status"] == "matched"
              and assigned["biker_id"] == a_uid
              and assigned["biker"]["full_name"] == "Biker A"
              and assigned["rider"]["email"] == "rider1@example.com",
              f"status={s}")
        s, _ = api("POST", "/admin/rides/999999/assign",
                   token=admin_tok, body={"biker_user_id": a_uid})
        check("assign to missing ride -> 404", s == 404, f"status={s}")
        s, _ = api("POST", f"/admin/rides/{r2_id}/assign",
                   token=admin_tok, body={"biker_user_id": 999999})
        check("assign to missing biker user -> 404", s == 404, f"status={s}")
        s, _ = api("POST", f"/admin/rides/{r2_id}/assign",
                   token=admin_tok, body={"biker_user_id": rider_id})
        check("assign to a rider user -> 400", s == 400, f"status={s}")
        s, _ = api("POST", f"/admin/rides/{r2_id}/assign",
                   token=admin_tok, body={"biker_user_id": d_uid})
        check("assign pending-approval biker -> 400", s == 400, f"status={s}")
        s, _ = api("POST", f"/admin/rides/{r2_id}/assign",
                   token=admin_tok, body={"biker_user_id": e_uid})
        check("assign unavailable biker -> 400", s == 400, f"status={s}")
        s, _ = api("POST", f"/admin/rides/{r2_id}/assign",
                   token=rider_tok, body={"biker_user_id": a_uid})
        check("non-admin assign -> 403", s == 403, f"status={s}")
        s, reassigned = api("POST", f"/admin/rides/{r1_id}/assign",
                            token=admin_tok, body={"biker_user_id": f_uid})
        check("reassign moves the ride to the new biker",
              s == 200 and reassigned["status"] == "matched"
              and reassigned["biker_id"] == f_uid,
              f"status={s}")

        # --- 9. unassign ---
        s, back = api("POST", f"/admin/rides/{r1_id}/unassign", token=admin_tok)
        check("unassign sends the ride back to pending",
              s == 200 and back["status"] == "pending"
              and back["biker_id"] is None and back["biker"] is None,
              f"status={s}")
        s, _ = api("POST", f"/admin/rides/{r1_id}/unassign", token=admin_tok)
        check("unassign on a pending ride -> 400", s == 400, f"status={s}")
        s, _ = api("POST", "/admin/rides/999999/unassign", token=admin_tok)
        check("unassign missing ride -> 404", s == 404, f"status={s}")

        # --- 10. dispatch board drops matched rides ---
        s, assigned2 = api("POST", f"/admin/rides/{r2_id}/assign",
                           token=admin_tok, body={"biker_user_id": b_uid})
        s, board2 = api("GET", "/admin/dispatch", token=admin_tok)
        ids2 = {r["id"] for r in board2["requests"]} if s == 200 else set()
        check("matched ride leaves the pending board",
              r2_id not in ids2 and r1_id in ids2,
              f"ids={sorted(ids2)}")

        # --- 11. rider-facing cards carry no insurance info ---
        s, pub = api("GET", "/bikers/available", token=rider_tok)
        check("public biker cards leak no insurance fields",
              s == 200 and "insurance" not in json.dumps(pub),
              f"status={s}")

        n_pass = sum(1 for _, ok_, _ in CHECKS if ok_)
        print(f"\n{n_pass}/{len(CHECKS)} checks passed")
        return 0 if n_pass == len(CHECKS) else 1
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except Exception:
            server.kill()
        stub.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
