#!/usr/bin/env python3
"""Smoke test: M9 store-prep privacy policy page.

Starts a stub Nominatim server (no real network), then the API with a
throwaway SQLite DB + env-seeded admin, and:

  1. checks GET /privacy returns the server-rendered privacy page (200,
     text/html) with all required sections, the contact address, the
     policy version, and a back-to-home link;
  2. verifies GET /legal/privacy returns {key, version, text} without auth
     and that the /privacy page body comes from that exact text;
  3. checks the static privacy.html template carries no hard-coded policy
     text (placeholders only — server renders from app/legal.py);
  4. checks index.html has a Privacy Policy footer link;
  5. runs a clean branding scan over the privacy files + the served page.

Run from hidden_files/src/:  ./.venv/bin/python tests/smoke_privacy.py
"""
import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # src/
API_PORT = 8508
STUB_PORT = 8509
BASE = f"http://127.0.0.1:{API_PORT}"
CHECKS: list[tuple[str, bool, str]] = []


class StubHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"[]"
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


def api(method: str, path: str, body=None):
    data = None
    headers = {}
    if body is not None:
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
        ctype = resp.getheader("Content-Type") or ""
        return resp.status, ctype, resp.read().decode()


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


def main() -> int:
    stub = HTTPServer(("127.0.0.1", STUB_PORT), StubHandler)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    if not wait_for_server(STUB_PORT):
        print("FAIL - stub geocoder did not start")
        return 1

    tmp = __import__("tempfile").mkdtemp(prefix="rab_privacy_test_")
    db_path = os.path.join(tmp, "test.db")
    upload_dir = os.path.join(tmp, "uploads")
    env = dict(
        os.environ,
        RAB_DATABASE_URL=f"sqlite:///{db_path}",
        RAB_ADMIN_EMAIL="privacy.admin@example.com",
        RAB_ADMIN_PASSWORD="test-admin-pw-123",
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

        # --- 1. version + /privacy page ---
        s, h = api("GET", "/health")
        check("health ok, version 0.21.0",
              s == 200 and h.get("version") == "0.21.0", str(h))

        s, ctype, page = get_raw("/privacy")
        check("/privacy returns 200", s == 200, f"status={s}")
        check("/privacy content-type is html",
              "text/html" in ctype, ctype)
        check("/privacy page has title",
              "<title>Privacy Policy" in page)

        required_sections = [
            "What we collect",
            "Location and ride details",
            "Photos",
            "Messages and agreements",
            "Payments",
            "What we share",
            "How long we keep it",
            "Your choices",
            "Contact",
        ]
        for sec in required_sections:
            check(f"/privacy section present: {sec}", sec in page)
        check("/privacy sections are <h2> headings",
              page.count("<h2>") >= len(required_sections),
              f"h2 count={page.count('<h2>')}")
        check("/privacy has contact email",
              "rentabikerservice@gmail.com" in page)
        check("/privacy shows policy version",
              "Version 1.0" in page)
        check("/privacy shows updated date",
              "October 12, 2026" in page)
        check("/privacy links back home",
              'href="/"' in page)
        check("/privacy has no unrendered placeholders",
              "{{POLICY" not in page and "{{UPDATED" not in page)
        check("/privacy mentions the 75/25 split accurately",
              "75% to the biker" in page and "25% to Rent a Biker" in page)

        # --- 2. /legal/privacy endpoint + single source of truth ---
        s, priv = api("GET", "/legal/privacy")
        check("/legal/privacy returns 200 without auth", s == 200, str(s))
        check("/legal/privacy key", priv.get("key") == "privacy-policy",
              str(priv.get("key")))
        check("/legal/privacy version", priv.get("version") == "1.0",
              str(priv.get("version")))
        text = priv.get("text", "")
        check("/legal/privacy text non-empty",
              isinstance(text, str) and len(text) > 200,
              f"len={len(text) if isinstance(text, str) else 'n/a'}")
        # The rendered page body must come from this exact text: the first
        # sentence and every heading appear in the served HTML.
        check("served page body comes from the /legal/privacy text",
              text.split("\n\n")[0][:80] in page,
              "intro paragraph missing from /privacy page")
        for sec in required_sections:
            check(f"/legal/privacy text has section: {sec}", sec in text)

        s, schema = api("GET", "/openapi.json")
        paths = schema.get("paths", {}) if s == 200 else {}
        check("openapi lists GET /legal/privacy",
              paths.get("/legal/privacy", {}).get("get") is not None)

        # --- 3. static template carries no hard-coded policy text ---
        tpl_path = os.path.join(HERE, "app", "static", "privacy.html")
        with open(tpl_path, encoding="utf-8") as fh:
            tpl = fh.read()
        check("privacy.html template uses placeholders",
              "{{POLICY_HTML}}" in tpl and "{{POLICY_VERSION}}" in tpl
              and "{{UPDATED_DATE}}" in tpl)
        check("privacy.html has no hard-coded policy body",
              "75% to the biker" not in tpl and "approximate coordinates" not in tpl)

        # --- 4. index.html footer link ---
        # The footer link carries an id now (Capacitor M9: JS rewrites its href
        # to u('/privacy') at load so the bundled wrapper points at the live
        # backend; with the override unset the page talks to its own origin).
        # Assert the web-mode default: a Privacy Policy anchor whose href is
        # /privacy, plus the JS line that honors the API base.
        s, _, index = get_raw("/")
        check("index.html footer links /privacy",
              re.search(r'<a[^>]*href="/privacy"[^>]*>Privacy Policy</a>', index) is not None)
        check("index.html footer privacy link honors the API base",
              "document.getElementById('privacyLink').href = u('/privacy');" in index)

        # --- 5. branding scan: privacy files + served page ---
        bad = []
        for label, blob in (("privacy.html", tpl), ("served /privacy", page),
                            ("legal.py", None)):
            if blob is None:
                with open(os.path.join(HERE, "app", "legal.py"),
                          encoding="utf-8") as fh:
                    blob = fh.read()
            low = blob.lower()
            for name in ("uber", "lyft"):
                if name in low:
                    bad.append((label, name))
                    break
        check("branding scan clean on privacy surfaces",
              not bad, str(bad) if bad else "no mentions")

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
