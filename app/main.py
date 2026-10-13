"""Rent a Biker — full app backend (M1 scaffold, M2 + M3 done, M4 dispatch,
M5 messaging, M6 legal, M7 payout record-keeping part 1, M8 deploy prep)."""
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import uploads
from .auth import hash_password
from .db import Base, SessionLocal, engine, DATABASE_URL
from .config import settings  # noqa: F401  (imported so env config loads at startup)
from .models import User
from .routers import auth as auth_router
from .routers import bikers as bikers_router
from .routers import rides as rides_router
from .routers import admin as admin_router
from .routers import legal as legal_router

app = FastAPI(title="Rent a Biker API", version="0.20.0")

# Create tables on startup (Alembic migrations come with milestone 2+ if needed).
Base.metadata.create_all(bind=engine)


# Additive, idempotent column migrations for existing databases.
# create_all() only creates missing tables — it never adds columns to an
# existing table. Each brand-new column gets one entry here:
# (table, column, sqlite_ddl, postgres_ddl).
# The SQLite path keeps the dev DB working; the Postgres path runs at deploy
# time (Render) against RAB_DATABASE_URL — so a fresh Postgres deploy and a
# redeploy over an older schema both land on the same schema with no data loss.
ADDITIVE_COLUMNS = [
    # (table, column, sqlite ddl, postgres ddl)
    ("ride_requests", "helmet_needed", "BOOLEAN NOT NULL DEFAULT 0", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("ride_requests", "pickup_latitude", "FLOAT", "DOUBLE PRECISION"),
    ("ride_requests", "pickup_longitude", "FLOAT", "DOUBLE PRECISION"),
    ("biker_profiles", "has_insurance", "BOOLEAN NOT NULL DEFAULT 0", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("biker_profiles", "insurance_covers_passenger", "BOOLEAN NOT NULL DEFAULT 0", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("biker_profiles", "passenger_responsibility_accepted", "BOOLEAN NOT NULL DEFAULT 0", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("biker_profiles", "insurance_company", "VARCHAR(120)", "VARCHAR(120)"),
    ("biker_profiles", "insurance_policy_number", "VARCHAR(64)", "VARCHAR(64)"),
    ("biker_profiles", "insurance_expiration_date", "VARCHAR(32)", "VARCHAR(32)"),
    ("biker_profiles", "passenger_responsibility_name", "VARCHAR(255)", "VARCHAR(255)"),
    ("biker_profiles", "passenger_responsibility_date", "VARCHAR(32)", "VARCHAR(32)"),
    # M6 — versioned legal-text records on signed bookings/applications
    ("ride_requests", "waiver_version", "VARCHAR(16)", "VARCHAR(16)"),
    ("biker_profiles", "pledge_version", "VARCHAR(16)", "VARCHAR(16)"),
    ("biker_profiles", "liability_version", "VARCHAR(16)", "VARCHAR(16)"),
    # M8 — legacy import of static-app-era signups
    ("users", "home_city", "VARCHAR(120)", "VARCHAR(120)"),
    ("users", "home_state", "VARCHAR(2)", "VARCHAR(2)"),
    ("users", "imported_at", "DATETIME", "TIMESTAMPTZ"),
    ("biker_profiles", "imported", "BOOLEAN NOT NULL DEFAULT 0", "BOOLEAN NOT NULL DEFAULT FALSE"),
]


def _ensure_additive_columns() -> None:
    """Apply ADDITIVE_COLUMNS to the live database, idempotently.

    SQLite: PRAGMA table_info guard + ALTER TABLE ADD COLUMN.
    Postgres: ALTER TABLE ... ADD COLUMN IF NOT EXISTS (one statement per
    column; concurrent deploys racing here are safe).
    """
    from sqlalchemy import text

    dialect = engine.dialect.name
    if dialect not in ("sqlite", "postgresql"):
        return
    with engine.begin() as conn:
        for table, column, sqlite_ddl, postgres_ddl in ADDITIVE_COLUMNS:
            if dialect == "sqlite":
                cols = [row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))]
                if column in cols:
                    continue
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sqlite_ddl}"))
            else:
                conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {postgres_ddl}")
                )


_ensure_additive_columns()


def seed_admin() -> None:
    """Create the first admin account from env vars (Kenneth).

    `RAB_ADMIN_EMAIL` + `RAB_ADMIN_PASSWORD` must both be set; nothing is
    created otherwise, and admin role is never grantable through /auth/register.
    """
    email = os.environ.get("RAB_ADMIN_EMAIL")
    password = os.environ.get("RAB_ADMIN_PASSWORD")
    if not email or not password:
        return
    db = SessionLocal()
    try:
        exists = db.query(User).filter(User.email == email).first()
        if exists is None:
            db.add(
                User(
                    email=email,
                    full_name="Kenneth",
                    role="admin",
                    password_hash=hash_password(password),
                )
            )
            db.commit()
    finally:
        db.close()


seed_admin()

app.include_router(auth_router.router)
app.include_router(bikers_router.router)
app.include_router(rides_router.router)
app.include_router(admin_router.router)
app.include_router(legal_router.router)

static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Bike photos: local storage, served at /uploads/<uuid>.<ext>
app.mount("/uploads", StaticFiles(directory=uploads.upload_root()), name="uploads")


@app.get("/health")
def health():
    return {"ok": True, "service": "rent-a-biker-api", "version": app.version}


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(static_dir, "index.html"))
