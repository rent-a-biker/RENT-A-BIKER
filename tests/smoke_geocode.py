#!/usr/bin/env python3
"""Smoke test: geocoding + distance matching (M2 increment).

Starts a stub Nominatim server (so no real network is hit), then the API with
a throwaway SQLite DB + env-seeded admin, and walks the geocode flow with
stdlib-only HTTP calls. Exits 0 if every check passes.

Run from hidden_files/src/:  ./.venv/bin/python tests/smoke_geocode.py
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
API_PORT = 8475
STUB_PORT = 8476
BASE = f"http://127.0.0.1:{API_PORT}"
CHECKS: list[tuple[str, bool, str]] = []

# Stub "Nominatim": city/state -> (lat, lon)
STUB_CITIES = {
    ("alpha", "AA"): (10.0, 20.0),
    ("beta", "BB"): (10.5, 20.0),
    ("gamma", "GG"): (12.0, 20.0),
}
STUB_HITS: list[dict] = []
STUB_FAIL = {"on": False}


class StubHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        STUB_HITS.append({
            "path": self.path,
            "q": qs.get("q", [""])[0],
            "ua": self.headers.get("User-Agent", ""),
            "format": qs.get("format", [""])[0],
        })
        if STUB_FAIL["on"] or parsed.path != "/search":
            body, code = b"boom", 500
        else:
            city = (qs.get("q", [""])[0].split(",")[0] or "").strip().lower()
            state = (qs.get("q", [""])[0].split(",")[1] or "").strip().upper() \
                if "," in qs.get("q", [""])[0] else ""
            coords = STUB_CITIES.get((city, state))
            body = json.dumps(
                [{"lat": str(coords[0]), "lon": str(coords[1])}] if coords else []
            ).encode()
            code = 200
        self.send_response(code)
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


def register(email, role, name):
    s, u = api("POST", "/auth/register", body={
        "email": email, "password": "password-123",
        "full_name": name, "role": role})
    assert s in (200, 201), f"register {email} failed: {s} {u}"
    s, t = api("POST", "/auth/login", form=True,
               body={"username": email, "password": "password-123"})
    assert s == 200, f"login {email} failed: {s} {t}"
    return t["access_token"]


PROFILE_BODY = {
    "bike_year": 2020, "bike_make": "Harley-Davidson", "bike_model": "Street Bob",
    "service_radius_miles": 50, "is_available": True,
    "has_endorsement": True, "pledge_accepted": True, "liability_accepted": True,
}


def make_biker(tok, city, state, **kw):
    body = dict(PROFILE_BODY, city=city, state=state, **kw)
    return api("POST", "/bikers/profile", token=tok, body=body)


def main() -> int:
    stub = HTTPServer(("127.0.0.1", STUB_PORT), StubHandler)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    if not wait_for_server(STUB_PORT):
        print("FAIL - stub geocoder did not start")
        return 1

    tmp = __import__("tempfile").mkdtemp(prefix="rab_geo_test_")
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
        check("health ok, version 0.20.0",
              s == 200 and h.get("version") == "0.20.0", str(h))

        s, tok = api("POST", "/auth/login", form=True,
                     body={"username": "kenneth.admin@example.com",
                           "password": "test-admin-password-xyz-123"})
        admin_tok = tok["access_token"] if s == 200 else None

        tok_a = register("geo-a@example.com", "biker", "Geo A")
        tok_b = register("geo-b@example.com", "biker", "Geo B")
        tok_c = register("geo-c@example.com", "biker", "Geo C")
        tok_d = register("geo-d@example.com", "biker", "Geo D")
        tok_e = register("geo-e@example.com", "biker", "Geo E")
        tok_f = register("geo-f@example.com", "biker", "Geo F")

        # --- 1. geocode-on-save via stub ---
        hits_before = len(STUB_HITS)
        s, a = make_biker(tok_a, "Alpha", "AA")
        check("profile create geocodes city/state",
              s == 200 and a["latitude"] == 10.0 and a["longitude"] == 20.0,
              f"status={s} lat={a.get('latitude') if isinstance(a, dict) else '?'}")
        check("stub hit exactly once", len(STUB_HITS) == hits_before + 1,
              f"hits={len(STUB_HITS)}")
        last = STUB_HITS[-1]
        check("identifying user-agent sent",
              "rentabiker" in last["ua"].lower(), last["ua"])
        check("format=json requested", last["format"] == "json")
        prof_a_id = a["id"] if isinstance(a, dict) else None

        # --- 2. cache: same city/state costs no second lookup ---
        hits_before = len(STUB_HITS)
        s, b = make_biker(tok_b, "Alpha", "AA")
        check("second same-city profile uses cache (no new stub hit)",
              s == 200 and len(STUB_HITS) == hits_before
              and b["latitude"] == 10.0 and b["longitude"] == 20.0,
              f"status={s} hits={len(STUB_HITS)}")

        # --- 3. manual lat/lng override skips the geocoder ---
        hits_before = len(STUB_HITS)
        s, c = make_biker(tok_c, "Nowhere", "ZZ", latitude=10.05, longitude=20.0)
        check("manual coords stored exactly, no geocode call",
              s == 200 and c["latitude"] == 10.05 and c["longitude"] == 20.0
              and len(STUB_HITS) == hits_before,
              f"status={s} hits={len(STUB_HITS)}")

        # --- 4. geocoder failure is graceful ---
        STUB_FAIL["on"] = True
        s, f = make_biker(tok_f, "Nowhere", "ZZ")
        STUB_FAIL["on"] = False
        check("geocoder 500 -> profile still saves, coords empty",
              s == 200 and f["latitude"] is None and f["longitude"] is None,
              f"status={s} lat={f.get('latitude') if isinstance(f, dict) else '?'}")

        # --- 5. city change re-geocodes ---
        s, d = make_biker(tok_d, "Beta", "BB")
        check("Beta resolves to stub coords",
              s == 200 and d["latitude"] == 10.5 and d["longitude"] == 20.0,
              f"status={s}")
        s, e = make_biker(tok_e, "Gamma", "GG")
        check("Gamma resolves to stub coords",
              s == 200 and e["latitude"] == 12.0 and e["longitude"] == 20.0,
              f"status={s}")
        s, b2 = make_biker(tok_b, "Beta", "BB")  # B moves Alpha -> Beta
        check("city change re-geocodes to new coords",
              s == 200 and b2["latitude"] == 10.5 and b2["longitude"] == 20.0,
              f"status={s} lat={b2.get('latitude') if isinstance(b2, dict) else '?'}")

        # --- 6. approve everyone for dispatch ---
        for pid in [a["id"], b2["id"], c["id"], d["id"], e["id"], f["id"]]:
            s, _ = api("POST", f"/admin/bikers/{pid}/approve", token=admin_tok)
            assert s == 200, f"approve {pid} failed: {s}"

        # own profile exposes coords
        s, me_a = api("GET", "/bikers/profile/me", token=tok_a)
        check("own profile exposes latitude/longitude",
              s == 200 and me_a["latitude"] == 10.0 and me_a["longitude"] == 20.0,
              f"status={s}")

        # --- 7. distance-filtered dispatch ---
        s, all6 = api("GET", "/bikers/available", token=admin_tok)
        check("no near params -> all 6 approved bikers, no distances",
              s == 200 and len(all6) == 6
              and all(x.get("distance_miles") is None for x in all6),
              f"status={s} n={len(all6) if isinstance(all6, list) else '?'}")

        s, near = api("GET",
                      "/bikers/available?near_lat=10.0&near_lng=20.0&within_miles=100",
                      token=admin_tok)
        ids = [x["id"] for x in near] if s == 200 else []
        dists = [x["distance_miles"] for x in near] if s == 200 else []
        expect_ids = [a["id"], c["id"], d["id"], b2["id"]]  # 0.0, ~3.45, ~34.55 x2
        expect_d = [0.0, 3.45, 34.55, 34.55]
        ok = (s == 200 and ids == expect_ids
              and all(abs(got - want) < 1.0
                      for got, want in zip(dists, expect_d)))
        check("near query sorts by distance, filters 50-mi-default radius",
              ok, f"ids={ids} dists={dists}")
        check("out-of-radius biker excluded (Gamma ~138 mi)",
              s == 200 and e["id"] not in ids, f"ids={ids}")
        check("biker without coords excluded from near queries",
              s == 200 and f["id"] not in ids, f"ids={ids}")

        s, tiny = api("GET",
                      "/bikers/available?near_lat=10.0&near_lng=20.0&within_miles=3",
                      token=admin_tok)
        check("tight radius returns only closest",
              s == 200 and len(tiny) == 1 and tiny[0]["id"] == a["id"],
              f"n={len(tiny) if isinstance(tiny, list) else '?'}")

        # --- 8. edge cases ---
        s, partial = api("GET", "/bikers/available?near_lat=10.0", token=admin_tok)
        check("only one near param -> no distance filtering",
              s == 200 and len(partial) == 6
              and all(x.get("distance_miles") is None for x in partial),
              f"status={s} n={len(partial) if isinstance(partial, list) else '?'}")
        s, st = api("GET", "/bikers/available?state=BB", token=admin_tok)
        st_ids = [x["id"] for x in st] if s == 200 else []
        check("state filter still works alongside geocoding",
              s == 200 and st_ids == [b2["id"], d["id"]] or
              (s == 200 and set(st_ids) == {b2["id"], d["id"]}),
              f"ids={st_ids}")
        s, bad = api("GET", "/bikers/available?near_lat=999", token=admin_tok)
        check("out-of-range near_lat rejected", s == 422, f"status={s}")
        s, badp = api("POST", "/bikers/profile", token=tok_a,
                      body=dict(PROFILE_BODY, city="X", state="AA",
                                latitude=91.0, longitude=0.0))
        check("out-of-range manual latitude rejected", s == 422, f"status={s}")

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
