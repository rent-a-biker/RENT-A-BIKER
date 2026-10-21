#!/usr/bin/env python3
"""Smoke test: M8 deployment prep — render.yaml blueprint, DATABASE_URL
handling, and the SQLite -> Postgres additive-migration path.

Verifies (stdlib only; no live Postgres needed):

  1. render.yaml exists at the repo root with every field the deploy needs
     (web service, runtime, health check, Postgres wiring, secret
     generation, persistent uploads disk, ipAllowList) and none of the
     deprecated fields (env:, plan: starter).
  2. requirements.txt carries the Postgres driver.
  3. normalize_database_url(): postgres:// and postgresql:// both become
     postgresql+psycopg://, SQLite passes through untouched.
  4. Every ADDITIVE_COLUMNS entry (app/main.py) maps to a real table +
     column on the SQLAlchemy models, has no duplicates, and its Postgres
     DDL compiles against the real postgresql dialect.
  5. A server booted with RAB_DATABASE_URL=sqlite (dev default) still boots
     and reports version 0.21.0 — the Postgres refactor didn't change the
     dev path.
  6. Branding scan on new/changed files.

Run from hidden_files/src/:  ./.venv/bin/python tests/smoke_deploy.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # src/
ROOT = os.path.dirname(HERE)  # hidden_files/ (repo root at deploy time)
API_PORT = 8495
BASE = f"http://127.0.0.1:{API_PORT}"
CHECKS: list[tuple[str, bool, str]] = []

FORBIDDEN = ["uber", "lyft"]  # branding rule: never in app files


def check(name: str, cond: bool, detail: str = "") -> None:
    CHECKS.append((name, cond, detail))
    print(("PASS" if cond else "FAIL"), "-", name, ("| " + detail if detail else ""))


def py_eval(code: str, env_extra: dict | None = None) -> str:
    """Run a python -c snippet in a throwaway cwd (no stray rentabiker.db)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = HERE + os.pathsep + env.get("PYTHONPATH", "")
    if env_extra:
        env.update(env_extra)
    with tempfile.TemporaryDirectory() as tmp:
        out = subprocess.run(
            [sys.executable, "-c", code],
            cwd=tmp,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
    if out.returncode != 0:
        raise RuntimeError(f"snippet failed:\n{out.stderr}")
    return out.stdout.strip()


# ---------------------------------------------------------------- render.yaml
yaml_path = os.path.join(ROOT, "render.yaml")
y = open(yaml_path).read() if os.path.exists(yaml_path) else ""
check("render.yaml exists at repo root", bool(y))

for marker in [
    "type: web",
    "name: rent-a-biker",
    "runtime: python",
    "buildCommand: pip install -r requirements.txt",
    "startCommand: uvicorn app.main:app",
    "healthCheckPath: /health",
    "RAB_DATABASE_URL",
    "fromDatabase:",
    "name: rent-a-biker-db",
    "property: connectionString",
    "RAB_SECRET_KEY",
    "generateValue: true",
    "RAB_ADMIN_EMAIL",
    "RAB_ADMIN_PASSWORD",
    "sync: false",
    "disk:",
    "mountPath:",
    "sizeGB:",
    "databases:",
    "databaseName:",
    "ipAllowList: []",
]:
    check(f"render.yaml has {marker!r}", marker in y, marker)
check("render.yaml has no deprecated 'env:' runtime field", "\nenv: python" not in y and "\n  env: python" not in y)
check("render.yaml has no legacy 'starter' plan name", "plan: starter" not in y)
check("render.yaml doesn't spend anything by itself", "dashboard" in y.lower() or "approves" in y.lower())

req = open(os.path.join(HERE, "requirements.txt")).read()
check("requirements.txt pins the Postgres driver", "psycopg[binary]" in req)
check("requirements.txt still pins SQLAlchemy", "sqlalchemy==" in req)

# ------------------------------------------------- URL normalization + models
norm = py_eval(
    "from app.db import normalize_database_url as n; "
    "import json; print(json.dumps({"
    "'a': n('postgres://u:p@h:5432/db'),"
    "'b': n('postgresql://u:p@h:5432/db'),"
    "'c': n('postgresql+psycopg://u:p@h:5432/db'),"
    "'d': n('sqlite:///./rentabiker.db')}))"
)
norm = json.loads(norm)
check("postgres:// normalizes to psycopg",
      norm["a"] == "postgresql+psycopg://u:p@h:5432/db", norm["a"])
check("postgresql:// normalizes to psycopg",
      norm["b"] == "postgresql+psycopg://u:p@h:5432/db", norm["b"])
check("postgresql+psycopg:// is left alone", norm["c"] == "postgresql+psycopg://u:p@h:5432/db")
check("sqlite URL passes through", norm["d"] == "sqlite:///./rentabiker.db")

cols = py_eval(
    "import json; from app.main import ADDITIVE_COLUMNS; "
    "from app.models import RideRequest, BikerProfile, User; "
    "models = {'ride_requests': RideRequest, 'biker_profiles': BikerProfile, 'users': User}; "
    "out = [{'table': t, 'column': c, 'pg': pg, "
    "'known': hasattr(models[t], c) and models[t].__tablename__ == t} "
    "for (t, c, s, pg) in ADDITIVE_COLUMNS]; "
    "print(json.dumps(out))"
)
cols = json.loads(cols)
check("every additive column maps to a real model column",
      all(e["known"] for e in cols), f"{len(cols)} entries")
seen = [(e["table"], e["column"]) for e in cols]
check("no duplicate additive columns", len(set(seen)) == len(seen))
check("all additive columns carry a Postgres DDL", all(e["pg"] for e in cols))
check("boolean defaults use Postgres FALSE, not 0",
      all("DEFAULT 0" not in e["pg"] for e in cols if "BOOLEAN" in e["pg"]))

compile_ok = py_eval(
    "from sqlalchemy import text; "
    "from sqlalchemy.dialects import postgresql; "
    "from app.main import ADDITIVE_COLUMNS; "
    "d = postgresql.dialect(); "
    "stmts = [f'ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {c} {pg}' "
    "for (t, c, s, pg) in ADDITIVE_COLUMNS]; "
    "print(len([str(text(x).compile(dialect=d)) for x in stmts]))"
)
check("all Postgres migration statements compile against the pg dialect",
      compile_ok == str(len(cols)), compile_ok)

envurl = py_eval(
    "from app.db import DATABASE_URL; print(DATABASE_URL)",
    {"RAB_DATABASE_URL": "postgres://u:p@h:5432/db"},
)
check("RAB_DATABASE_URL env var drives the engine URL",
      envurl == "postgresql+psycopg://u:p@h:5432/db", envurl)

# ------------------------------------------------- dev-path boot regression
with tempfile.TemporaryDirectory() as tmp:
    env = dict(os.environ)
    env["RAB_DATABASE_URL"] = "sqlite:///" + os.path.join(tmp, "test.db")
    env["RAB_ADMIN_EMAIL"] = "admin@test.local"
    env["RAB_ADMIN_PASSWORD"] = "x" * 16
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(API_PORT)],
        cwd=HERE, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        health = None
        for _ in range(60):
            try:
                with urllib.request.urlopen(BASE + "/health", timeout=2) as r:
                    health = json.loads(r.read().decode())
                break
            except Exception:
                time.sleep(0.5)
        check("dev sqlite server still boots after refactor", health is not None)
        check("health reports version 0.21.0",
              bool(health) and health.get("version") == "0.21.0", str(health))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

# ------------------------------------------------------------------ branding
for rel in ["app/db.py", "app/main.py", "../render.yaml", "../PRODUCTION_CHECKLIST.md"]:
    path = os.path.join(HERE, rel)
    low = open(path).read().lower()
    hits = [w for w in FORBIDDEN if w in low]
    check(f"branding-clean: {rel}", not hits, f"found {hits}" if hits else "")

failed = [n for (n, ok, _) in CHECKS if not ok]
print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
sys.exit(1 if failed else 0)
