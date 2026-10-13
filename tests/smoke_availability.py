#!/usr/bin/env python3
"""Smoke test: biker availability toggle (M2 remainder).

Starts the API with a throwaway SQLite DB + env-seeded admin, then exercises
the dedicated availability endpoint and the frontend toggle UI with
stdlib-only HTTP calls. Exits 0 if every check passes.

Run from hidden_files/src/:  ./.venv/bin/python tests/smoke_availability.py
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
PORT = 8475
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


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="rab_avail_test_")
    db_path = os.path.join(tmp, "test.db")
    env = dict(
        os.environ,
        RAB_DATABASE_URL=f"sqlite:///{db_path}",
        RAB_UPLOAD_DIR=os.path.join(tmp, "uploads"),
        RAB_ADMIN_EMAIL="kenneth.admin@example.com",
        RAB_ADMIN_PASSWORD="test-admin-password-xyz-123",
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

        s, h = api("GET", "/health")
        check("health ok, version 0.20.0",
              s == 200 and h.get("version") == "0.20.0", str(h))

        biker_tok = register_and_login("biker1@example.com", "biker", "Biker One")
        rider_tok = register_and_login("rider1@example.com", "rider", "Rider One")
        s, admin_tok_raw = api("POST", "/auth/login", form=True,
                     body={"username": "kenneth.admin@example.com",
                           "password": "test-admin-password-xyz-123"})
        admin_tok = admin_tok_raw["access_token"] if s == 200 else None
        check("tokens issued", all([biker_tok, rider_tok, admin_tok]))

        # --- auth guards on the toggle endpoint ---
        s, _ = api("POST", "/bikers/profile/availability",
                   body={"is_available": True})
        check("toggle without token -> 401", s == 401, f"status={s}")

        s, _ = api("POST", "/bikers/profile/availability", token=rider_tok,
                   body={"is_available": True})
        check("rider role -> 403", s == 403, f"status={s}")

        s, body = api("POST", "/bikers/profile/availability", token=biker_tok,
                      body={"is_available": True})
        check("toggle with no profile -> 404", s == 404, f"status={s}")

        # --- create profile (manual coords: no geocoder network needed) ---
        s, prof = api("POST", "/bikers/profile", token=biker_tok, body={
            "bike_make": "Harley-Davidson", "bike_model": "Softail",
            "bike_year": 2007, "city": "Pensacola", "state": "FL",
            "latitude": 30.4213, "longitude": -87.2169,
            "has_endorsement": True, "pledge_accepted": True,
            "liability_accepted": True})
        check("profile created, unavailable by default",
              s == 200 and prof.get("is_available") is False, f"status={s}")

        s, me = api("GET", "/bikers/profile/me", token=biker_tok)
        check("own profile shows unavailable", s == 200 and me["is_available"] is False)

        # --- flip the switch on ---
        s, prof = api("POST", "/bikers/profile/availability", token=biker_tok,
                      body={"is_available": True})
        check("toggle on -> 200, flag true",
              s == 200 and prof.get("is_available") is True, f"status={s}")

        s, me = api("GET", "/bikers/profile/me", token=biker_tok)
        check("toggle persisted on own profile",
              s == 200 and me["is_available"] is True)

        # --- other profile fields untouched by the toggle ---
        s, full = api("POST", "/bikers/profile", token=biker_tok, body={
            "bike_make": "Harley-Davidson", "bike_model": "Softail",
            "bike_year": 2007, "city": "Pensacola", "state": "FL",
            "latitude": 30.4213, "longitude": -87.2169,
            "is_available": False, "has_endorsement": True,
            "pledge_accepted": True, "liability_accepted": True})
        check("upsert with full body still works",
              s == 200 and full["is_available"] is False)

        # --- dispatch visibility still gated by Kenneth's approval ---
        s, q = api("GET", "/admin/bikers", token=admin_tok)
        pending = q if isinstance(q, list) else []
        check("application lands in pending queue", len(pending) == 1,
              f"count={len(pending)}")
        profile_id = pending[0]["id"] if pending else None

        s, _ = api("POST", "/bikers/profile/availability", token=biker_tok,
                   body={"is_available": True})
        s, avail = api("GET", "/bikers/available", token=biker_tok)
        names = [b["id"] for b in (avail or [])]
        check("pending biker hidden from dispatch despite toggle",
              s == 200 and profile_id not in names, f"visible={names}")

        s, _ = api("POST", f"/admin/bikers/{profile_id}/approve", token=admin_tok,
                   body={})
        check("admin approves", s == 200, f"status={s}")

        s, avail = api("GET", "/bikers/available", token=biker_tok)
        names = [b["id"] for b in (avail or [])]
        check("approved + available -> visible in dispatch",
              profile_id in names, f"visible={names}")

        s, _ = api("POST", "/bikers/profile/availability", token=biker_tok,
                   body={"is_available": False})
        s, avail = api("GET", "/bikers/available", token=biker_tok)
        names = [b["id"] for b in (avail or [])]
        check("toggle off -> gone from dispatch",
              s == 200 and profile_id not in names, f"visible={names}")

        s, _ = api("POST", "/bikers/profile/availability", token=biker_tok,
                   body={"is_available": True})
        s, avail = api("GET", "/bikers/available", token=biker_tok)
        names = [b["id"] for b in (avail or [])]
        check("toggle back on -> visible again",
              s == 200 and profile_id in names, f"visible={names}")

        # --- frontend serves the toggle UI, branding-clean ---
        s, html = get_raw("/")
        check("frontend serves 200", s == 200)
        check("frontend has biker sign-in panel",
              "Register as biker" in html and "registerBikerBtn" in html)
        check("frontend has availability toggle",
              "toggleBtn" in html and "/bikers/profile/availability" in html)
        lowered = html.lower()
        check("frontend branding clean",
              "uber" not in lowered and "lyft" not in lowered)

        failures = [c for c in CHECKS if not c[1]]
        print(f"\n{len(CHECKS) - len(failures)}/{len(CHECKS)} checks passed")
        return 1 if failures else 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    sys.exit(main())
