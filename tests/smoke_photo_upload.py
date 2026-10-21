#!/usr/bin/env python3
"""Smoke test: bike photo upload (M2 increment).

Starts the API with a throwaway SQLite DB + throwaway upload dir, then walks
photo upload validation, serving, replacement, and dispatch visibility with
stdlib-only HTTP calls. Exits 0 if every check passes.

Run from hidden_files/src/:  ./.venv/bin/python tests/smoke_photo_upload.py
"""
import base64
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
PORT = 8474
BASE = f"http://127.0.0.1:{PORT}"
CHECKS: list[tuple[str, bool, str]] = []

# 1x1 transparent PNG, valid magic bytes.
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
# Second distinct "image" (valid PNG header + extra bytes) for the replace test.
PNG_2 = PNG_1X1 + b"\x00" * 128


def check(name: str, cond: bool, detail: str = "") -> None:
    CHECKS.append((name, cond, detail))
    print(("PASS" if cond else "FAIL"), "-", name, ("| " + detail if detail else ""))


def api(method, path, token=None, body=None, raw=None, ctype=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif raw is not None and ctype:
        headers["Content-Type"] = ctype
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw_out = resp.read()
            try:
                parsed = json.loads(raw_out.decode())
            except Exception:
                parsed = raw_out
            return resp.status, parsed, dict(resp.headers)
    except urllib.error.HTTPError as e:
        raw_out = e.read()
        try:
            parsed = json.loads(raw_out.decode())
        except Exception:
            parsed = raw_out
        return e.code, parsed, dict(e.headers)


def multipart(filename: str, content_type: str, data: bytes):
    boundary = "----RabTestBoundary7d3f2a"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="photo"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def wait_for_server(timeout_s: int = 30) -> bool:
    for _ in range(timeout_s * 4):
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=1):
                s, _, _ = api("GET", "/health")
                if s == 200:
                    return True
        except OSError:
            pass
        time.sleep(0.25)
    return False


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="rab_photo_test_")
    db_path = os.path.join(tmp, "test.db")
    upload_dir = os.path.join(tmp, "uploads")
    env = dict(
        os.environ,
        RAB_DATABASE_URL=f"sqlite:///{db_path}",
        RAB_UPLOAD_DIR=upload_dir,
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

        s, h, _ = api("GET", "/health")
        check("health ok version 0.21.0", s == 200 and h.get("version") == "0.21.0", str(h))

        def register(email, role, name):
            s, _, _ = api("POST", "/auth/register", body={
                "email": email, "password": "password-123",
                "full_name": name, "role": role})
            s2, t, _ = api("POST", "/auth/login",
                        raw=urllib.parse.urlencode(
                            {"username": email, "password": "password-123"}).encode(),
                        ctype="application/x-www-form-urlencoded")
            return t["access_token"] if s2 == 200 else None

        biker_tok = register("biker1@example.com", "biker", "Biker One")
        rider_tok = register("rider1@example.com", "rider", "Rider One")
        check("biker+rider register+login", all([biker_tok, rider_tok]))

        # --- photo upload needs a profile first ---
        body, ctype = multipart("bike.png", "image/png", PNG_1X1)
        s, resp, _ = api("POST", "/bikers/profile/photo", token=biker_tok,
                         raw=body, ctype=ctype)
        check("upload before profile -> 404", s == 404, f"status={s}")

        s, p, _ = api("POST", "/bikers/profile", token=biker_tok, body={
            "bike_year": 2013, "bike_make": "Harley", "bike_model": "Street Bob",
            "city": "Ozark", "state": "mo", "service_radius_miles": 50,
            "is_available": True, "has_endorsement": True,
            "pledge_accepted": True, "liability_accepted": True,
        })
        check("biker profile created", s == 200 and p.get("photo_url") is None,
              f"status={s} photo_url={p.get('photo_url') if isinstance(p, dict) else '?'}")

        # --- valid upload ---
        body, ctype = multipart("my-bike.png", "image/png", PNG_1X1)
        s, resp, _ = api("POST", "/bikers/profile/photo", token=biker_tok,
                         raw=body, ctype=ctype)
        photo_url = resp.get("photo_url") if isinstance(resp, dict) else None
        check("valid PNG upload 200 + photo_url",
              s == 200 and isinstance(photo_url, str)
              and photo_url.startswith("/uploads/") and photo_url.endswith(".png"),
              f"status={s} photo_url={photo_url}")
        first_filename = photo_url.rsplit("/", 1)[-1] if photo_url else ""

        # --- the photo is served back, bytes intact ---
        s, served, hdrs = api("GET", photo_url, token=rider_tok)
        ct = hdrs.get("Content-Type") or hdrs.get("content-type") or ""
        check("photo served with image content-type + exact bytes",
              s == 200 and ct.startswith("image/png") and served == PNG_1X1,
              f"status={s} content-type={ct}")

        # --- own profile shows the photo url ---
        s, mine, _ = api("GET", "/bikers/profile/me", token=biker_tok)
        check("profile/me includes photo_url",
              s == 200 and mine.get("photo_url") == photo_url, f"status={s}")

        # --- non-image upload rejected ---
        body, ctype = multipart("notes.txt", "text/plain", b"hello not an image")
        s, resp, _ = api("POST", "/bikers/profile/photo", token=biker_tok,
                         raw=body, ctype=ctype)
        check("text upload rejected (415)", s == 415, f"status={s}")

        # --- spoofed content-type (garbage bytes) rejected ---
        body, ctype = multipart("fake.png", "image/png", b"definitely not a png" * 8)
        s, resp, _ = api("POST", "/bikers/profile/photo", token=biker_tok,
                         raw=body, ctype=ctype)
        check("magic-byte mismatch rejected (415)", s == 415, f"status={s}")

        # --- oversized upload rejected ---
        big = b"\xff\xd8\xff" + b"\x00" * (6 * 1024 * 1024)
        body, ctype = multipart("huge.jpg", "image/jpeg", big)
        s, resp, _ = api("POST", "/bikers/profile/photo", token=biker_tok,
                         raw=body, ctype=ctype)
        check("oversized upload rejected (413)", s == 413, f"status={s}")

        # --- rider cannot hit the biker endpoint ---
        body, ctype = multipart("bike.png", "image/png", PNG_1X1)
        s, resp, _ = api("POST", "/bikers/profile/photo", token=rider_tok,
                         raw=body, ctype=ctype)
        check("rider blocked (403)", s == 403, f"status={s}")

        # --- malicious filename cannot escape the upload dir ---
        body, ctype = multipart("../../evil.png", "image/png", PNG_1X1)
        s, resp, _ = api("POST", "/bikers/profile/photo", token=biker_tok,
                         raw=body, ctype=ctype)
        evil_anywhere = any(
            "evil" in name
            for name in os.listdir(tmp)
            if os.path.isfile(os.path.join(tmp, name))
        )
        url2 = resp.get("photo_url") if isinstance(resp, dict) else ""
        check("traversal filename neutralized",
              s == 200 and not evil_anywhere and "/uploads/" in (url2 or "")
              and ".." not in (url2 or ""),
              f"status={s} photo_url={url2}")

        # --- re-upload replaces the old photo ---
        old_url = url2
        old_file = os.path.join(upload_dir, old_url.rsplit("/", 1)[-1])
        body, ctype = multipart("bike2.png", "image/png", PNG_2)
        s, resp, _ = api("POST", "/bikers/profile/photo", token=biker_tok,
                         raw=body, ctype=ctype)
        new_url = resp.get("photo_url") if isinstance(resp, dict) else ""
        files_now = os.listdir(upload_dir)
        check("re-upload swaps photo, old file deleted",
              s == 200 and new_url != old_url
              and not os.path.exists(old_file)
              and len(files_now) == 1 and files_now[0] == new_url.rsplit("/", 1)[-1],
              f"status={s} files={files_now}")

        s, served, hdrs = api("GET", new_url, token=rider_tok)
        check("new photo bytes served", s == 200 and served == PNG_2, f"status={s}")

        # --- approve biker -> photo appears on the public dispatch card ---
        s, t, _ = api("POST", "/auth/login",
                   raw=urllib.parse.urlencode(
                       {"username": "kenneth.admin@example.com",
                        "password": "test-admin-password-xyz-123"}).encode(),
                   ctype="application/x-www-form-urlencoded")
        admin_tok = t["access_token"] if s == 200 else None
        s, q, _ = api("GET", "/admin/bikers?approval_status=pending", token=admin_tok)
        pid = q[0]["id"] if s == 200 and q else None
        s, _, _ = api("POST", f"/admin/bikers/{pid}/approve", token=admin_tok, body={})
        s, avail, _ = api("GET", "/bikers/available", token=rider_tok)
        check("dispatch card carries photo_url",
              s == 200 and len(avail) == 1 and avail[0]["photo_url"] == new_url,
              f"status={s} photo_url={avail[0].get('photo_url') if isinstance(avail, list) and avail else '?'}")

        # --- admin queue item also carries the photo ---
        s, q, _ = api("GET", "/admin/bikers?approval_status=approved", token=admin_tok)
        check("admin queue item carries photo_url",
              s == 200 and q and q[0]["photo_url"] == new_url,
              f"status={s}")

        # --- no directory listing leak at /uploads/ ---
        s, _, _ = api("GET", "/uploads/")
        check("no /uploads/ directory listing (404)", s == 404, f"status={s}")
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
