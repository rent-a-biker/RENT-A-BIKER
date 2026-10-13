#!/usr/bin/env python3
"""Smoke test: M7 part 4 — payout CSV export for Kenneth's bookkeeping.

Starts a stub Nominatim server (no real network), then the API with a
throwaway SQLite DB + env-seeded admin, and:

  1. checks the served frontend renders the Export CSV button and the JS
     only calls an endpoint present in the live OpenAPI schema;
  2. verifies the OpenAPI contract for GET /admin/payouts/export.csv;
  3. walks a 20-mi ride to completion (5000c -> 3750c / 1250c) and checks
     the CSV download: content-type, attachment disposition, exact header
     row, and the row's names/emails/dollars/status/timestamps;
  4. marks the payout paid with a PayPal reference and re-exports: status,
     reference, and paid_at show up; ?status=pending / ?status=paid filter
     the rows and the filename;
  5. checks rider -> 403, anonymous -> 401, and runs a clean branding scan.

Run from hidden_files/src/:  ./.venv/bin/python tests/smoke_payouts_export.py
"""
import csv
import io
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
API_PORT = 8499
STUB_PORT = 8500
BASE = f"http://127.0.0.1:{API_PORT}"
CHECKS: list[tuple[str, bool, str]] = []


class StubHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps([]).encode()
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


def get_raw(path: str, token: str | None = None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            # Starlette/uvicorn send header names lowercase
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, hdrs, resp.read().decode()
    except urllib.error.HTTPError as e:
        hdrs = {k.lower(): v for k, v in e.headers.items()}
        return e.code, hdrs, e.read().decode()


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

    tmp = __import__("tempfile").mkdtemp(prefix="rab_export_test_")
    db_path = os.path.join(tmp, "test.db")
    upload_dir = os.path.join(tmp, "uploads")
    admin_password = "csv-export-test-pw"
    env = dict(
        os.environ,
        RAB_DATABASE_URL=f"sqlite:///{db_path}",
        RAB_ADMIN_EMAIL="export.admin@example.com",
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

        # --- 1. version + frontend markers + OpenAPI contract ---
        s, h = api("GET", "/health")
        check("health ok, version 0.20.0",
              s == 200 and h.get("version") == "0.20.0", str(h))

        s, _, html = get_raw("/")
        check("frontend served", s == 200 and "<html" in html)
        markers = [
            'id="exportPayoutsBtn"',
            "async function exportPayoutsCsv",
            "/admin/payouts/export.csv",
            "exportPayoutsBtn')",
            "rent-a-biker-payouts-",
        ]
        for m in markers:
            check(f"frontend marker present: {m[:40]}", m in html)

        s, schema = api("GET", "/openapi.json")
        paths = schema.get("paths", {}) if s == 200 else {}
        check("OpenAPI has GET /admin/payouts/export.csv",
              "/admin/payouts/export.csv" in paths
              and "get" in paths["/admin/payouts/export.csv"])

        # --- 2. setup: rider, biker (approved + available), admin ---
        admin_tok = login("export.admin@example.com", admin_password)
        rider_tok, _ = register("export.rider@example.com", "rider",
                                "Export Rider", "555-0140")
        biker_tok, biker_uid = register("export.biker@example.com", "biker",
                                        "Export Biker", "555-0141")
        prof_body = {
            "bike_year": 2022, "bike_make": "Harley-Davidson",
            "bike_model": "Street Glide", "city": "Springfield", "state": "MO",
            "service_radius_miles": 50,
            "latitude": 37.21, "longitude": -93.29,
            "is_available": True, "has_endorsement": True,
            "pledge_accepted": True, "liability_accepted": True,
        }
        s, prof = api("POST", "/bikers/profile", token=biker_tok, body=prof_body)
        check("biker profile -> 200", s == 200, f"status={s}")
        s, _ = api("POST", f"/admin/bikers/{prof['id']}/approve", token=admin_tok)
        check("admin approve -> 200", s == 200, f"status={s}")

        # --- 3. book a 20-mi ride, assign, complete -> payout record ---
        book_body = {
            "ride_type": "ride", "pickup_address": "1 Anywhere Ln, Springfield, MO",
            "dropoff_address": "200 Oak Ave, Springfield, MO",
            "scheduled_at": None, "notes": None, "helmet_needed": False,
            "waiver_signed": True, "waiver_name": "Export Rider",
            "waiver_date": "2026-10-09", "estimated_miles": 20,
        }
        s, ride = api("POST", "/rides/", token=rider_tok, body=book_body)
        check("booking -> 201", s == 201, f"status={s}")
        ride_id = ride["id"]
        s, _ = api("POST", f"/admin/rides/{ride_id}/assign",
                   token=admin_tok, body={"biker_user_id": biker_uid})
        check("admin assign -> matched", s == 200, f"status={s}")
        s, done = api("POST", f"/rides/{ride_id}/complete", token=biker_tok)
        payout = (done.get("payout") or {}) if s == 200 else {}
        payout_id = payout.get("id")
        check("payout record created", s == 200 and payout_id, f"s={s}")

        # --- 4. export: headers + row content ---
        s, headers, body = get_raw("/admin/payouts/export.csv", token=admin_tok)
        ctype = headers.get("content-type", "")
        dispo = headers.get("content-disposition", "")
        check("export -> 200", s == 200, f"status={s}")
        check("content-type is text/csv", "text/csv" in ctype, ctype)
        check("attachment disposition + filename",
              dispo.startswith("attachment;")
              and "rent-a-biker-payouts-all.csv" in dispo, dispo)
        rows = list(csv.reader(io.StringIO(body)))
        expected_header = ["payout_id", "ride_id", "rider_name", "rider_email",
                           "biker_name", "biker_email", "fare_usd",
                           "biker_share_usd", "platform_share_usd", "status",
                           "paypal_reference", "recorded_at", "paid_at"]
        check("exact header row", rows and rows[0] == expected_header,
              str(rows[0]) if rows else "no rows")
        data = rows[1:] if rows else []
        check("one pending payout row", len(data) == 1, f"rows={len(data)}")
        if data:
            r = dict(zip(expected_header, data[0]))
            check("row names/emails",
                  r["rider_name"] == "Export Rider"
                  and r["rider_email"] == "export.rider@example.com"
                  and r["biker_name"] == "Export Biker"
                  and r["biker_email"] == "export.biker@example.com",
                  str(r))
            check("row dollars: 50.00 / 37.50 / 12.50",
                  r["fare_usd"] == "50.00"
                  and r["biker_share_usd"] == "37.50"
                  and r["platform_share_usd"] == "12.50", str(r))
            check("row pending, no reference/paid_at yet",
                  r["status"] == "pending"
                  and r["paypal_reference"] == ""
                  and r["paid_at"] == ""
                  and r["recorded_at"] != "", str(r))

        # --- 5. mark paid, re-export, filters, filename ---
        s, _ = api("POST", f"/admin/payouts/{payout_id}/mark-paid",
                   token=admin_tok, body={"reference": "PP-EXPORT-1"})
        check("mark-paid -> 200", s == 200, f"status={s}")
        s, headers, body = get_raw("/admin/payouts/export.csv", token=admin_tok)
        rows = list(csv.reader(io.StringIO(body)))
        data = rows[1:]
        check("paid row shows status + reference + paid_at",
              len(data) == 1 and data[0][9] == "paid"
              and data[0][10] == "PP-EXPORT-1" and data[0][12] != "",
              str(data[0]) if data else "no rows")

        s, headers, body = get_raw("/admin/payouts/export.csv?status=pending",
                                   token=admin_tok)
        rows = list(csv.reader(io.StringIO(body)))
        check("?status=pending filters the paid row out", len(rows) == 1,
              f"rows={len(rows)}")
        check("pending filename in disposition",
              "rent-a-biker-payouts-pending.csv" in headers.get("content-disposition", ""),
              headers.get("content-disposition", ""))

        s, headers, body = get_raw("/admin/payouts/export.csv?status=paid",
                                   token=admin_tok)
        rows = list(csv.reader(io.StringIO(body)))
        check("?status=paid keeps the row", len(rows) == 2, f"rows={len(rows)}")
        check("paid filename in disposition",
              "rent-a-biker-payouts-paid.csv" in headers.get("content-disposition", ""),
              headers.get("content-disposition", ""))

        # --- 6. auth guards ---
        s, _, _ = get_raw("/admin/payouts/export.csv", token=rider_tok)
        check("rider export -> 403", s == 403, f"status={s}")
        s, _, _ = get_raw("/admin/payouts/export.csv")
        check("anonymous export -> 401", s == 401, f"status={s}")

        # --- 7. branding scan (app source only) ---
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
