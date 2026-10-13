#!/usr/bin/env python3
"""Smoke test: biker approval review queue (M2 increment).

Starts the API with a throwaway SQLite DB + env-seeded admin, then walks the
full review flow with stdlib-only HTTP calls. Exits 0 if every check passes.

Run from hidden_files/src/:  ./.venv/bin/python tests/smoke_admin_review.py
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
PORT = 8473
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


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="rab_review_test_")
    db_path = os.path.join(tmp, "test.db")
    env = dict(
        os.environ,
        RAB_DATABASE_URL=f"sqlite:///{db_path}",
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
        check("health ok", s == 200 and h.get("service") == "rent-a-biker-api", str(h))

        # --- seeded admin can log in ---
        s, tok = api("POST", "/auth/login", form=True,
                     body={"username": "kenneth.admin@example.com",
                           "password": "test-admin-password-xyz-123"})
        check("seeded admin login", s == 200 and "access_token" in tok, f"status={s}")
        admin_tok = tok["access_token"] if s == 200 else None

        s, admin_me = api("GET", "/auth/me", token=admin_tok)
        check("admin role", s == 200 and admin_me["role"] == "admin",
              f"status={s} role={admin_me.get('role') if isinstance(admin_me, dict) else '?'}")

        # --- register must NOT allow self-granting admin ---
        s, _ = api("POST", "/auth/register", body={
            "email": "sneaky@example.com", "password": "password-123",
            "full_name": "Sneaky", "role": "admin"})
        check("admin role rejected at register", s == 422, f"status={s}")

        def register(email, role, name):
            s, u = api("POST", "/auth/register", body={
                "email": email, "password": "password-123",
                "full_name": name, "role": role})
            s2, t = api("POST", "/auth/login", form=True,
                        body={"username": email, "password": "password-123"})
            return (t["access_token"] if s2 == 200 else None)

        biker1_tok = register("biker1@example.com", "biker", "Biker One")
        biker2_tok = register("biker2@example.com", "biker", "Biker Two")
        rider_tok = register("rider1@example.com", "rider", "Rider One")
        check("biker/rider register+login", all([biker1_tok, biker2_tok, rider_tok]))

        profile_body = {
            "bike_year": 2013, "bike_make": "Harley", "bike_model": "Street Bob",
            "city": "Ozark", "state": "mo", "service_radius_miles": 50,
            "is_available": True, "has_endorsement": True,
            "pledge_accepted": True, "liability_accepted": True,
        }

        # --- new biker profile starts pending ---
        s, p1 = api("POST", "/bikers/profile", token=biker1_tok, body=profile_body)
        check("biker1 profile created pending",
              s == 200 and p1["approval_status"] == "pending"
              and p1["state"] == "MO" and p1["is_available"] is True,
              f"status={s} approval={p1.get('approval_status') if isinstance(p1, dict) else '?'}")
        p1_id = p1["id"]

        s, p2 = api("POST", "/bikers/profile", token=biker2_tok,
                    body={**profile_body, "bike_model": "Sportster", "city": "Springfield"})
        p2_id = p2["id"] if s == 200 else None

        # --- pending bikers are invisible to dispatch ---
        s, avail = api("GET", "/bikers/available", token=rider_tok)
        check("available list hides pending bikers", s == 200 and avail == [],
              f"status={s} count={len(avail) if isinstance(avail, list) else '?'}")

        # --- non-admins are locked out of the review queue ---
        for label, tok in (("rider", rider_tok), ("biker", biker1_tok)):
            s, _ = api("GET", "/admin/bikers", token=tok)
            check(f"{label} cannot list admin queue (403)", s == 403, f"status={s}")
            s, _ = api("POST", f"/admin/bikers/{p1_id}/approve", token=tok, body={})
            check(f"{label} cannot approve (403)", s == 403, f"status={s}")

        # --- admin sees the pending queue with biker contact info ---
        s, q = api("GET", "/admin/bikers?approval_status=pending", token=admin_tok)
        ok = (s == 200 and len(q) == 2
              and all(i["approval_status"] == "pending" for i in q)
              and q[0]["user"]["email"] in ("biker1@example.com", "biker2@example.com"))
        check("admin pending queue lists 2 with contact info", ok,
              f"status={s} count={len(q) if isinstance(q, list) else '?'}")

        # --- approve biker1 -> appears in dispatch ---
        s, a1 = api("POST", f"/admin/bikers/{p1_id}/approve", token=admin_tok, body={})
        check("approve biker1",
              s == 200 and a1["approval_status"] == "approved"
              and a1["reviewed_at"] and a1["reviewer_id"] is not None,
              f"status={s} approval={a1.get('approval_status') if isinstance(a1, dict) else '?'}")

        s, avail = api("GET", "/bikers/available", token=rider_tok)
        check("approved biker visible in dispatch",
              s == 200 and len(avail) == 1 and avail[0]["bike_model"] == "Street Bob",
              f"status={s} count={len(avail) if isinstance(avail, list) else '?'}")

        s, mine = api("GET", "/bikers/profile/me", token=biker1_tok)
        check("biker sees own approval status",
              s == 200 and mine["approval_status"] == "approved",
              f"status={s} approval={mine.get('approval_status') if isinstance(mine, dict) else '?'}")

        # --- reject biker2 with a reason ---
        s, r2 = api("POST", f"/admin/bikers/{p2_id}/reject", token=admin_tok,
                    body={"reason": "No endorsement on file"})
        check("reject biker2 with reason",
              s == 200 and r2["approval_status"] == "rejected"
              and r2["rejection_reason"] == "No endorsement on file",
              f"status={s} approval={r2.get('approval_status') if isinstance(r2, dict) else '?'}")

        s, avail = api("GET", "/bikers/available", token=rider_tok)
        check("rejected biker stays out of dispatch",
              s == 200 and len(avail) == 1, f"count={len(avail) if isinstance(avail, list) else '?'}")

        # --- re-review: admin can approve after a rejection ---
        s, a2 = api("POST", f"/admin/bikers/{p2_id}/approve", token=admin_tok, body={})
        s, avail = api("GET", "/bikers/available", token=rider_tok)
        check("re-review approve works",
              s == 200 and len(avail) == 2 and a2["rejection_reason"] is None,
              f"count={len(avail) if isinstance(avail, list) else '?'}")

        # --- review of missing profile is 404; invalid filter is 422 ---
        s, _ = api("POST", "/admin/bikers/999999/approve", token=admin_tok, body={})
        check("approve missing profile 404", s == 404, f"status={s}")
        s, _ = api("GET", "/admin/bikers?approval_status=bogus", token=admin_tok)
        check("invalid approval_status filter 422", s == 422, f"status={s}")

        # --- state filter on dispatch still works with approval gate ---
        s, avail_mo = api("GET", "/bikers/available?state=mo", token=rider_tok)
        s, avail_tx = api("GET", "/bikers/available?state=tx", token=rider_tok)
        check("state filter + approval gate",
              s == 200 and len(avail_mo) == 2 and len(avail_tx) == 0,
              f"mo={len(avail_mo) if isinstance(avail_mo, list) else '?'} tx={len(avail_tx) if isinstance(avail_tx, list) else '?'}")
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    failed = [n for n, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
