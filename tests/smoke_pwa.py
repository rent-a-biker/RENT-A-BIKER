#!/usr/bin/env python3
"""Smoke test: M9 (part 1) — PWA installability / store-wrapper readiness.

Starts the API with a throwaway SQLite DB + env-seeded admin, and:

  1. checks the web app manifest is served at /static/manifest.webmanifest
     with the correct content-type and parses as JSON;
  2. verifies the manifest contract: name/short_name, start_url="/",
     scope="/", display="standalone", theme_color, and icon entries
     (src, sizes, type) that resolve to real PNGs;
  3. checks each icon file serves 200 as image/png with the PNG IHDR
     dimensions matching its manifest size, and that the files are
     non-trivial (real artwork, not a blank placeholder);
  4. checks the apple-touch-icon (180x180) serves 200 as image/png;
  5. checks the served frontend HTML links the manifest, declares the
     theme-color, and references the apple-touch-icon;
  6. runs a clean branding scan (manifest + changed HTML head).

Run from hidden_files/src/:  ./.venv/bin/python tests/smoke_pwa.py
"""
import json
import os
import socket
import struct
import subprocess
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # src/
API_PORT = 8507
BASE = f"http://127.0.0.1:{API_PORT}"
CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    CHECKS.append((name, cond, detail))
    print(("PASS" if cond else "FAIL"), "-", name, ("| " + detail if detail else ""))


def get_bytes(path: str):
    """Return (status, content_type, body_bytes)."""
    req = urllib.request.Request(BASE + path, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


def png_size(data: bytes):
    """Return (width, height) from a PNG's IHDR, or None if not a PNG."""
    if len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    if data[12:16] != b"IHDR":
        return None
    w, h = struct.unpack(">II", data[16:24])
    return w, h


def wait_for_server(timeout_s: int = 30) -> bool:
    for _ in range(timeout_s * 4):
        try:
            with socket.create_connection(("127.0.0.1", API_PORT), timeout=1):
                s, _, _ = get_bytes("/health")
                if s == 200:
                    return True
        except OSError:
            pass
        time.sleep(0.25)
    return False


def main() -> int:
    tmp = __import__("tempfile").mkdtemp(prefix="rab_pwa_test_")
    db_path = os.path.join(tmp, "test.db")
    upload_dir = os.path.join(tmp, "uploads")
    admin_password = os.urandom(16).hex()
    env = dict(
        os.environ,
        RAB_DATABASE_URL=f"sqlite:///{db_path}",
        RAB_ADMIN_EMAIL="pwa.admin@example.com",
        RAB_ADMIN_PASSWORD=admin_password,
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
        if not wait_for_server():
            print("FAIL - server did not start")
            return 1

        # --- 1. version ---
        s, ctype, body = get_bytes("/health")
        try:
            h = json.loads(body.decode())
        except Exception:
            h = {}
        check("health ok, version 0.20.0",
              s == 200 and h.get("version") == "0.20.0", str(h))

        # --- 2. manifest served ---
        s, ctype, mbody = get_bytes("/static/manifest.webmanifest")
        check("manifest served 200", s == 200, f"status={s}")
        check("manifest content-type application/manifest+json",
              "application/manifest+json" in (ctype or ""),
              f"content-type={ctype!r}")
        try:
            manifest = json.loads(mbody.decode())
        except Exception as e:
            manifest = None
            check("manifest parses as JSON", False, str(e))
        else:
            check("manifest parses as JSON", isinstance(manifest, dict), "")

        # --- 3. manifest contract ---
        if manifest is not None:
            check("manifest name is Rent a Biker",
                  manifest.get("name") == "Rent a Biker",
                  repr(manifest.get("name")))
            check("manifest short_name present",
                  bool(manifest.get("short_name")),
                  repr(manifest.get("short_name")))
            check("manifest start_url/scope/display",
                  manifest.get("start_url") == "/" and
                  manifest.get("scope") == "/" and
                  manifest.get("display") == "standalone",
                  f"start={manifest.get('start_url')} scope={manifest.get('scope')} "
                  f"display={manifest.get('display')}")
            check("manifest theme_color #141414",
                  manifest.get("theme_color") == "#141414",
                  repr(manifest.get("theme_color")))
            check("manifest description present",
                  bool(manifest.get("description")),
                  repr(manifest.get("description")))
            icons = manifest.get("icons")
            ok_icons = isinstance(icons, list) and len(icons) == 2
            check("manifest has 2 icon entries", ok_icons,
                  f"icons={icons!r}"[:120])
            if ok_icons:
                entries_ok = all(
                    isinstance(ic, dict) and
                    ic.get("type") == "image/png" and
                    isinstance(ic.get("src"), str) and
                    ic["src"].startswith("/static/icons/") and
                    ic.get("sizes") in ("192x192", "512x512")
                    for ic in icons
                )
                check("icon entries: src under /static/icons/, type image/png, sizes",
                      entries_ok, str(icons))
            else:
                entries_ok = False
                icons = []
        else:
            icons, entries_ok = [], False

        # --- 4. icon files: 200, PNG, dimensions match, non-trivial ---
        for ic in icons:
            src = ic.get("src", "")
            expected = ic.get("sizes", "?")
            s, ctype, data = get_bytes(src)
            check(f"icon {expected} served 200", s == 200, f"status={s} src={src}")
            check(f"icon {expected} content-type image/png",
                  (ctype or "").startswith("image/png"), f"content-type={ctype!r}")
            size = png_size(data)
            want = tuple(int(x) for x in expected.split("x"))
            check(f"icon {expected} IHDR dimensions match", size == want,
                  f"ihdr={size}")
            check(f"icon {expected} non-trivial file", len(data) > 1024,
                  f"bytes={len(data)}")

        # --- 5. apple-touch-icon ---
        s, ctype, adata = get_bytes("/static/icons/apple-touch-icon.png")
        check("apple-touch-icon served 200", s == 200, f"status={s}")
        check("apple-touch-icon is image/png",
              (ctype or "").startswith("image/png"), f"content-type={ctype!r}")
        check("apple-touch-icon is 180x180", png_size(adata) == (180, 180),
              f"ihdr={png_size(adata)}")

        # --- 6. frontend HTML wiring ---
        s, ctype, hbody = get_bytes("/")
        html = hbody.decode(errors="replace")
        check("frontend / served 200", s == 200, f"status={s}")
        check("HTML links the web app manifest",
              '<link rel="manifest" href="/static/manifest.webmanifest">' in html,
              "manifest link tag")
        check("HTML declares theme-color #141414",
              '<meta name="theme-color" content="#141414">' in html,
              "theme-color meta")
        check("HTML references the apple-touch-icon",
              '<link rel="apple-touch-icon" href="/static/icons/apple-touch-icon.png">' in html,
              "apple-touch-icon link")
        check("HTML marks mobile-web-app-capable",
              'name="mobile-web-app-capable" content="yes"' in html and
              'name="apple-mobile-web-app-capable" content="yes"' in html,
              "capable metas")

        # --- 7. branding scan (app source only; the tests' own meta-assertion
        # strings are excluded by convention, as in prior suites) ---
        banned = ["uber", "lyft"]
        bad = []
        head_html = html.split("</head>")[0]
        for src_name, text in (
                ("manifest.webmanifest",
                 open(os.path.join(HERE, "app", "static", "manifest.webmanifest")).read()),
                ("index.html <head>",
                 open(os.path.join(HERE, "app", "static", "index.html")).read().split("</head>")[0])):
            low = text.lower()
            for b in banned:
                if b in low:
                    bad.append(f"{src_name}:{b}")
        check("branding-clean (no ride-service names)", not bad, str(bad))
    finally:
        server.terminate()
        server.wait(timeout=10)

    failed = [c for c in CHECKS if not c[1]]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
