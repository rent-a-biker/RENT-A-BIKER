#!/usr/bin/env python3
"""Smoke test: M7 part 5 — biker-side earnings CSV export.

Starts a stub Nominatim server (no real network), then the API with a
throwaway SQLite DB + env-seeded admin, and:

  1. checks the served frontend renders the Export CSV button on the biker
     Earnings section and the JS only calls an endpoint present in the live
     OpenAPI schema;
  2. verifies the OpenAPI contract for GET /rides/earnings/export.csv;
  3. walks a 20-mi ride to completion (5000c -> 3750c biker cut) and checks
     the biker's CSV download: content-type, attachment disposition, exact
     header row, row dollars/status/timestamps, and that no rider contact
     data leaks into the sheet;
  4. marks the payout paid with a PayPal reference and re-exports: status,
     reference, and paid_at show up; ?status=pending / ?status=paid filter
     the rows and the filename;
  5. checks privacy: another biker's export carries none of the first
     biker's rows, a rider's export is an empty sheet, anonymous -> 401;
  6. runs a clean branding scan.

Run from hidden_files/src/:  ./.venv/bin/python tests/smoke_earnings_export.py
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
API_PORT = 8503
STUB_PORT = 8504
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


EXPECTED_HEADER = ["payout_id", "ride_id", "fare_usd", "biker_share_usd",
                   "status", "paypal_reference", "recorded_at", "paid_at"]


def main() -> int:
    stub = HTTPServer(("127.0.0.1", STUB_PORT), StubHandler)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    if not wait_for_server(STUB_PORT):
        print("FAIL - stub geocoder did not start")
        return 1

    tmp = __import__("tempfile").mkdtemp(prefix="rab_earn_export_test_")
    db_path = os.path.join(tmp, "test.db")
    upload_dir = os.path.join(tmp, "uploads")
    admin_password = "earn-export-test-pw"
    env = dict(
        os.environ,
        RAB_DATABASE_URL=f"sqlite:///{db_path}",
        RAB_ADMIN_EMAIL="earnexport.admin@example.com",
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
        check("health ok, version 0.21.0",
              s == 200 and h.get("version") == "0.21.0", str(h))

        s, _, html = get_raw("/")
        check("frontend served", s == 200 and "<html" in html)
        markers = [
            'id="exportEarningsBtn"',
            "async function exportEarningsCsv",
            "/rides/earnings/export.csv",
            "rent-a-biker-earnings.csv",
        ]
        for m in markers:
            check(f"frontend marker present: {m[:40]}", m in html)

        s, schema = api("GET", "/openapi.json")
        paths = schema.get("paths", {}) if s == 200 else {}
        check("OpenAPI has GET /rides/earnings/export.csv",
              "/rides/earnings/export.csv" in paths
              and "get" in paths["/rides/earnings/export.csv"])

        # --- 2. setup: rider, biker (approved + available), second biker, admin ---
        admin_tok = login("earnexport.admin@example.com", admin_password)
        rider_tok, _ = register("earnexport.rider@example.com", "rider",
                                "EarnExport Rider", "555-0160")
        biker_tok, biker_uid = register("earnexport.biker@example.com", "biker",
                                        "EarnExport Biker", "555-0161")
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
        biker2_tok, _ = register("earnexport.biker2@example.com", "biker",
                                 "EarnExport Biker Two", "555-0162")

        # --- 3. book a 20-mi ride, assign, complete -> payout record ---
        book_body = {
            "ride_type": "ride", "pickup_address": "1 Anywhere Ln, Springfield, MO",
            "dropoff_address": "200 Oak Ave, Springfield, MO",
            "scheduled_at": None, "notes": None, "helmet_needed": False,
            "waiver_signed": True, "waiver_name": "EarnExport Rider",
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

        # --- 4. biker export: headers + row content + privacy ---
        s, headers, body = get_raw("/rides/earnings/export.csv", token=biker_tok)
        ctype = headers.get("content-type", "")
        dispo = headers.get("content-disposition", "")
        check("export -> 200", s == 200, f"status={s}")
        check("content-type is text/csv", "text/csv" in ctype, ctype)
        check("attachment disposition + filename",
              dispo.startswith("attachment;")
              and "rent-a-biker-earnings-all.csv" in dispo, dispo)
        rows = list(csv.reader(io.StringIO(body)))
        check("exact header row", rows and rows[0] == EXPECTED_HEADER,
              str(rows[0]) if rows else "no rows")
        data = rows[1:] if rows else []
        check("one pending payout row", len(data) == 1, f"rows={len(data)}")
        if data:
            r = dict(zip(EXPECTED_HEADER, data[0]))
            check("row dollars: fare 50.00 / biker cut 37.50",
                  r["fare_usd"] == "50.00" and r["biker_share_usd"] == "37.50",
                  str(r))
            check("row pending, no reference/paid_at yet",
                  r["status"] == "pending"
                  and r["paypal_reference"] == ""
                  and r["paid_at"] == ""
                  and r["recorded_at"] != "", str(r))
            check("row ride_id matches", r["ride_id"] == str(ride_id), str(r))
        check("no rider contact data in the export",
              "EarnExport Rider" not in body
              and "earnexport.rider@example.com" not in body, body[:200])

        # --- 5. mark paid, re-export, filters, filenames ---
        s, _ = api("POST", f"/admin/payouts/{payout_id}/mark-paid",
                   token=admin_tok, body={"reference": "PP-EARN-1"})
        check("mark-paid -> 200", s == 200, f"status={s}")
        s, headers, body = get_raw("/rides/earnings/export.csv", token=biker_tok)
        rows = list(csv.reader(io.StringIO(body)))
        data = rows[1:]
        check("paid row shows status + reference + paid_at",
              len(data) == 1 and data[0][4] == "paid"
              and data[0][5] == "PP-EARN-1" and data[0][7] != "",
              str(data[0]) if data else "no rows")

        s, headers, body = get_raw("/rides/earnings/export.csv?status=pending",
                                   token=biker_tok)
        rows = list(csv.reader(io.StringIO(body)))
        check("?status=pending filters the paid row out", len(rows) == 1,
              f"rows={len(rows)}")
        check("pending filename in disposition",
              "rent-a-biker-earnings-pending.csv" in headers.get("content-disposition", ""),
              headers.get("content-disposition", ""))

        s, headers, body = get_raw("/rides/earnings/export.csv?status=paid",
                                   token=biker_tok)
        rows = list(csv.reader(io.StringIO(body)))
        check("?status=paid keeps the row", len(rows) == 2, f"rows={len(rows)}")
        check("paid filename in disposition",
              "rent-a-biker-earnings-paid.csv" in headers.get("content-disposition", ""),
              headers.get("content-disposition", ""))

        # --- 6. privacy: other biker, rider, anonymous ---
        s, _, body2 = get_raw("/rides/earnings/export.csv", token=biker2_tok)
        rows2 = list(csv.reader(io.StringIO(body2)))
        check("other biker's export is an empty sheet",
              s == 200 and rows2 and rows2[0] == EXPECTED_HEADER
              and len(rows2) == 1, f"status={s} rows={len(rows2)}")
        s, _, body3 = get_raw("/rides/earnings/export.csv", token=rider_tok)
        rows3 = list(csv.reader(io.StringIO(body3)))
        check("rider's export is an empty sheet (never someone else's money)",
              s == 200 and rows3 and rows3[0] == EXPECTED_HEADER
              and len(rows3) == 1, f"status={s} rows={len(rows3)}")
        s, _, _ = get_raw("/rides/earnings/export.csv")
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
