"""M8 legacy import: static-app-era signup lists into real accounts.

Kenneth collected rider "get notified" signups and biker applications while
the app was a static page; that data never lived in this database. This
module pulls such a list from a CSV so he can flip those lists into real
accounts:

- riders -> rider accounts (ready to book once a biker is near them),
- bikers -> biker accounts + profiles that land in the normal review queue
  as "pending". Endorsement, pledge, liability, and insurance are NOT
  carried over: the imported profile is flagged `imported=True`, and the
  biker completes/signs everything in-app on first sign-in, where the
  existing server-side rules enforce it. Kenneth approves from the review
  queue exactly as with any other application.

Imported accounts get a random temporary password, delivered once in the
import result (it is hashed into the DB and never stored in cleartext, so
it cannot be recovered later). Kenneth hands the passwords to the riders
and bikers; they sign in normally from there.
"""

import csv
import datetime
import io
import re
import secrets

from sqlalchemy.orm import Session

from . import auth, models
from .routers import bikers as bikers_router

MAX_ROWS = 500
MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB, enforced before parsing too

RIDER_COLUMNS = ["full_name", "email", "phone", "city", "state"]
RIDER_REQUIRED = ["full_name", "email"]

BIKER_COLUMNS = [
    "full_name",
    "email",
    "phone",
    "city",
    "state",
    "bike_year",
    "bike_make",
    "bike_model",
    "service_radius_miles",
]
BIKER_REQUIRED = ["full_name", "email", "city", "state", "bike_make", "bike_model"]

_COLUMNS = {"riders": RIDER_COLUMNS, "bikers": BIKER_COLUMNS}
_REQUIRED = {"riders": RIDER_REQUIRED, "bikers": BIKER_REQUIRED}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class LegacyImportFileError(Exception):
    """The uploaded file itself is unusable (not a CSV, wrong columns, ...)."""


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def _valid_email(raw: str) -> str | None:
    email = (raw or "").strip().lower()
    return email if _EMAIL_RE.match(email) else None


def _clean_state(raw: str) -> str | None:
    state = (raw or "").strip().upper()
    return state if len(state) == 2 and state.isalpha() else None


def _clean_int(raw: str, lo: int, hi: int) -> int | None:
    try:
        value = int((raw or "").strip())
    except (TypeError, ValueError):
        return None
    return value if lo <= value <= hi else None


def import_legacy_csv(
    db: Session, kind: str, file_bytes: bytes
) -> dict:
    """Validate and import a legacy rider/biker CSV. Returns the result dict
    matching schemas.LegacyImportResultOut.

    Raises LegacyImportFileError for structural problems (empty file, wrong
    header); individual bad rows are collected in the result's `errors`
    instead, so one bad row never blocks the rest.
    """
    if kind not in _COLUMNS:
        raise LegacyImportFileError("kind must be 'riders' or 'bikers'")
    if len(file_bytes) > MAX_FILE_BYTES:
        raise LegacyImportFileError("File is too large (max 5 MB)")
    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise LegacyImportFileError("File is not valid UTF-8 text")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise LegacyImportFileError("No data rows found")
    header = [h.strip() for h in (rows[0].keys() or [])]
    missing = [c for c in _REQUIRED[kind] if c not in header]
    if missing:
        raise LegacyImportFileError(
            f"Missing required column(s): {', '.join(missing)}"
        )
    rows = rows[:MAX_ROWS]

    existing_emails = {
        email.lower()
        for (email,) in db.query(models.User.email).all()
    }
    seen_in_file: set[str] = set()
    created = skipped = 0
    errors: list[dict] = []
    credentials: list[dict] = []

    for row_num, row in enumerate(rows, start=2):
        get = lambda c: (row.get(c) or "").strip()  # noqa: E731
        email = _valid_email(get("email"))
        full_name = get("full_name")
        if not full_name:
            errors.append(
                {"row": row_num, "email": email, "message": "missing full_name"}
            )
            continue
        if not email:
            errors.append(
                {
                    "row": row_num,
                    "email": get("email") or None,
                    "message": "missing or invalid email",
                }
            )
            continue
        if email in existing_emails or email in seen_in_file:
            skipped += 1
            continue

        if kind == "bikers":
            state = _clean_state(get("state"))
            city = get("city")
            bike_make = get("bike_make")
            bike_model = get("bike_model")
            missing_field = next(
                (
                    f
                    for f, v in [
                        ("city", city),
                        ("state", state),
                        ("bike_make", bike_make),
                        ("bike_model", bike_model),
                    ]
                    if not v
                ),
                None,
            )
            if missing_field:
                errors.append(
                    {
                        "row": row_num,
                        "email": email,
                        "message": f"missing or invalid {missing_field}",
                    }
                )
                continue
            bike_year = (
                _clean_int(get("bike_year"), 1900, 2100)
                if get("bike_year")
                else None
            )
            if get("bike_year") and bike_year is None:
                errors.append(
                    {
                        "row": row_num,
                        "email": email,
                        "message": "bike_year must be a year like 2022",
                    }
                )
                continue
            radius = (
                _clean_int(get("service_radius_miles"), 1, 1000)
                if get("service_radius_miles")
                else 50
            )
            if get("service_radius_miles") and radius is None:
                errors.append(
                    {
                        "row": row_num,
                        "email": email,
                        "message": "service_radius_miles must be 1-1000",
                    }
                )
                continue
        else:
            raw_state = get("state")
            state = _clean_state(raw_state) if raw_state else None
            if raw_state and state is None:
                errors.append(
                    {
                        "row": row_num,
                        "email": email,
                        "message": "state must be a 2-letter code like MO",
                    }
                )
                continue
            city = get("city")

        phone = get("phone") or None
        if phone and len(phone) > 32:
            errors.append(
                {"row": row_num, "email": email, "message": "phone too long"}
            )
            continue

        # --- the row is valid: create the account ---
        temp_password = secrets.token_urlsafe(16)
        user = models.User(
            email=email,
            full_name=full_name,
            phone=phone,
            role="biker" if kind == "bikers" else "rider",
            password_hash=auth.hash_password(temp_password),
            home_city=city or None,
            home_state=state,
            imported_at=_utcnow(),
        )
        db.add(user)
        db.flush()  # assign user.id for the profile FK below
        if kind == "bikers":
            coords = bikers_router.resolve_coordinates(db, city, state)
            profile = models.BikerProfile(
                user_id=user.id,
                bike_year=bike_year,
                bike_make=bike_make,
                bike_model=bike_model,
                city=city,
                state=state,
                service_radius_miles=radius,
                latitude=coords[0] if coords else None,
                longitude=coords[1] if coords else None,
                is_available=False,
                approval_status="pending",
                imported=True,
            )
            db.add(profile)
        seen_in_file.add(email)
        created += 1
        credentials.append(
            {"row": row_num, "email": email, "temp_password": temp_password}
        )

    db.commit()
    return {
        "kind": kind,
        "rows_total": len(rows),
        "created": created,
        "skipped_existing": skipped,
        "errors": errors,
        "credentials": credentials,
    }
