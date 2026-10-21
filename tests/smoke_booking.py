#!/usr/bin/env python3
"""Smoke test: rider booking form backend (M3).

Starts the API with a throwaway SQLite DB + env-seeded admin, then exercises
the fare-estimate endpoint, the helmet flag, the waiver requirement, the
booking flow, my-rides listing, and cancellation — plus the frontend booking
form markup. stdlib-only HTTP calls. Exits 0 if every check passes.

Run from hidden_files/src/:  ./.venv/bin/python tests/smoke_booking.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # src/
PORT = 8476
BASE = f"http://127.0.0.1:{PORT}"
CHECKS: list[tuple[str, bool, str]] = []


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
        with urllib.request.urlopen(req, timeout=10) as resp:
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


def wait_for_server(timeout_s: int = 30) -> bool:
    for _ in range(timeout_s * 4):
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=1):
                s, _ = api("GET", "/health")
                if s == 200:
                    return True
        except OSError:
            pass
        time.sleep(0.25)
    return False


def register_and_login(email, role, name):
    api("POST", "/auth/register", body={
        "email": email, "password": "password-123",
        "full_name": name, "role": role})
    s, t = api("POST", "/auth/login", form=True,
               body={"username": email, "password": "password-123"})
    return t["access_token"] if s == 200 else None


def waiver():
    return {"waiver_signed": True, "waiver_name": "Test Rider", "waiver_date": "2026-10-07"}


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="rab_booking_test_")
    db_path = os.path.join(tmp, "test.db")
    env = dict(
        os.environ,
        RAB_DATABASE_URL=f"sqlite:///{db_path}",
        RAB_UPLOAD_DIR=os.path.join(tmp, "uploads"),
        RAB_ADMIN_EMAIL="kenneth.admin@example.com",
        RAB_ADMIN_PASSWORD="admin-pass-123",
    )
    venv_py = os.path.join(HERE, ".venv", "bin", "python")
    server = subprocess.Popen(
        [venv_py, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--log-level", "warning"],
        cwd=HERE,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_for_server():
            print("FAIL - server did not start")
            return 1

        # --- estimate endpoint ---
        s, h = api("GET", "/health")
        check("health ok, version 0.21.0",
              s == 200 and h.get("version") == "0.21.0", str(h))

        s, e = api("POST", "/rides/estimate",
                   body={"ride_type": "ride", "estimated_miles": 10})
        check("estimate ride 10 mi = 2500c",
              s == 200 and e.get("fare_estimate_cents") == 2500, str(e))

        s, e = api("POST", "/rides/estimate",
                   body={"ride_type": "ride", "estimated_miles": 0})
        check("estimate ride 0 mi = 0c",
              s == 200 and e.get("fare_estimate_cents") == 0, str(e))

        s, e = api("POST", "/rides/estimate",
                   body={"ride_type": "event", "duration_hours": 1})
        check("estimate event 1 hr bills 2-hr min = 15000c",
              s == 200 and e.get("fare_estimate_cents") == 15000, str(e))

        s, e = api("POST", "/rides/estimate",
                   body={"ride_type": "event", "duration_hours": 3})
        check("estimate event 3 hr = 22500c",
              s == 200 and e.get("fare_estimate_cents") == 22500, str(e))

        s, _ = api("POST", "/rides/estimate", body={"ride_type": "ride"})
        check("estimate ride without miles = 400", s == 400)

        s, _ = api("POST", "/rides/estimate", body={"ride_type": "event"})
        check("estimate event without hours = 400", s == 400)

        s, _ = api("POST", "/rides/estimate",
                   body={"ride_type": "taxi", "estimated_miles": 5})
        check("estimate bad ride_type = 422", s == 422)

        s, _ = api("POST", "/rides/estimate",
                   body={"ride_type": "ride", "estimated_miles": -1})
        check("estimate negative miles = 422", s == 422)

        # --- booking flow ---
        rider_tok = register_and_login("rider.booking@example.com", "rider", "Test Rider")
        biker_tok = register_and_login("biker.booking@example.com", "biker", "Test Biker")
        check("rider registered + logged in", bool(rider_tok))
        check("biker registered + logged in", bool(biker_tok))

        s, _ = api("POST", "/rides/", token=rider_tok, body={
            "ride_type": "ride", "pickup_address": "123 Main St",
            "estimated_miles": 10})
        check("booking without waiver = 400", s == 400)

        s, ride1 = api("POST", "/rides/", token=rider_tok, body={
            "ride_type": "ride", "pickup_address": "123 Main St",
            "dropoff_address": "456 Oak Ave", "estimated_miles": 10,
            "notes": "side door", "helmet_needed": True, **waiver()})
        check("book ride with helmet = 201",
              s == 201 and ride1.get("helmet_needed") is True, str(ride1))
        check("booked fare 10 mi = 2500c",
              ride1.get("fare_estimate_cents") == 2500)
        check("waiver recorded on booking", ride1.get("waiver_signed") is True)

        s, ride2 = api("POST", "/rides/", token=rider_tok, body={
            "ride_type": "event", "pickup_address": "Fairgrounds",
            "duration_hours": 1, **waiver()})
        check("helmet defaults to false when omitted",
              s == 201 and ride2.get("helmet_needed") is False, str(ride2))
        check("event 1 hr bills 2-hr min = 15000c",
              ride2.get("fare_estimate_cents") == 15000)

        s, _ = api("POST", "/rides/", token=biker_tok, body={
            "ride_type": "ride", "pickup_address": "123 Main St",
            "estimated_miles": 5, **waiver()})
        check("biker cannot book (rider/admin only) = 403", s == 403)

        s, mine = api("GET", "/rides/me", token=rider_tok)
        ids = [r["id"] for r in (mine or [])]
        check("my-rides lists both bookings",
              s == 200 and ride1["id"] in ids and ride2["id"] in ids, str(ids))
        r1 = next(r for r in mine if r["id"] == ride1["id"])
        check("my-rides carries helmet flag", r1.get("helmet_needed") is True)

        s, c = api("POST", f"/rides/{ride2['id']}/cancel", token=rider_tok)
        check("cancel pending ride = 200 cancelled",
              s == 200 and c.get("status") == "cancelled", str(c))

        s, _ = api("POST", f"/rides/{ride2['id']}/cancel", token=rider_tok)
        check("double cancel = 400", s == 400)

        s, mine2 = api("GET", "/rides/me", token=rider_tok)
        statuses = {r["id"]: r["status"] for r in (mine2 or [])}
        check("cancelled ride still listed with cancelled status",
              statuses.get(ride2["id"]) == "cancelled", str(statuses))

        # --- frontend booking form markup ---
        s, html = get_raw("/")
        markers = ["Book a ride", 'id="pickup"', 'id="helmet"',
                   'id="waiverCheck"', 'id="myRidesCard"',
                   "Register as rider", "/rides/estimate", "Liability waiver"]
        missing = [m for m in markers if m not in html]
        check("frontend serves rider booking form markup",
              s == 200 and not missing, f"missing={missing}")

        # --- branding scan ---
        lower = html.lower()
        bad = [w for w in ("uber", "lyft") if w in lower]
        check("frontend branding-clean", not bad, str(bad))

        fails = [c for c in CHECKS if not c[1]]
        print(f"\n{len(CHECKS) - len(fails)}/{len(CHECKS)} checks passed")
        return 1 if fails else 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except Exception:
            server.kill()


if __name__ == "__main__":
    sys.exit(main())
