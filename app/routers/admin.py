"""Admin (Kenneth) endpoints: biker application review queue + dispatch board.

A biker application is visible to riders/dispatch only after approval here.
Approve and reject are both idempotent and re-reviewable: approving an
approved profile is a no-op, and a rejected biker can be approved later.

The dispatch board (/admin/dispatch) shows every pending ride request with
the rider's contact info and the approved + available bikers ranked by
distance from the pickup (Kenneth's service rule: within each biker's own
service radius, default 50 miles). Manual assign sets the biker and moves
the ride to matched; unassign sends it back to pending. Auto-match does the
same as one click: it picks the nearest eligible biker for a pending ride
(exact distance ties go to the passenger-covered policy first).
"""
import csv
import datetime
import io

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from .. import auth, geo, importing, models, ride_out, schemas
from ..db import get_db

router = APIRouter(prefix="/admin", tags=["admin"])

_APPROVED = "approved"
_REJECTED = "rejected"


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def _get_profile(db: Session, profile_id: int) -> models.BikerProfile:
    profile = (
        db.query(models.BikerProfile)
        .options(joinedload(models.BikerProfile.user))
        .filter(models.BikerProfile.id == profile_id)
        .first()
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="Biker profile not found")
    return profile


@router.get("/bikers", response_model=list[schemas.BikerAdminOut])
def list_biker_applications(
    approval_status: str | None = Query(
        default=None, pattern="^(pending|approved|rejected)$"
    ),
    admin: models.User = Depends(auth.require_role("admin")),
    db: Session = Depends(get_db),
):
    """List biker applications, newest first. Filter with ?approval_status=pending."""
    q = db.query(models.BikerProfile).options(joinedload(models.BikerProfile.user))
    if approval_status:
        q = q.filter(models.BikerProfile.approval_status == approval_status)
    q = q.order_by(models.BikerProfile.created_at.desc()).limit(200)
    return q.all()


@router.post("/bikers/{profile_id}/approve", response_model=schemas.BikerAdminOut)
def approve_biker(
    profile_id: int,
    admin: models.User = Depends(auth.require_role("admin")),
    db: Session = Depends(get_db),
):
    profile = _get_profile(db, profile_id)
    profile.approval_status = _APPROVED
    profile.rejection_reason = None
    profile.reviewed_at = _utcnow()
    profile.reviewer_id = admin.id
    db.commit()
    db.refresh(profile)
    return profile


@router.post("/bikers/{profile_id}/reject", response_model=schemas.BikerAdminOut)
def reject_biker(
    profile_id: int,
    body: schemas.BikerReviewIn,
    admin: models.User = Depends(auth.require_role("admin")),
    db: Session = Depends(get_db),
):
    profile = _get_profile(db, profile_id)
    profile.approval_status = _REJECTED
    profile.rejection_reason = (body.reason or "").strip() or None
    profile.reviewed_at = _utcnow()
    profile.reviewer_id = admin.id
    db.commit()
    db.refresh(profile)
    return profile


# ---------- Dispatch board ----------


def _candidate_out(
    profile: models.BikerProfile, distance_miles: float | None
) -> schemas.DispatchCandidateOut:
    user = profile.user
    return schemas.DispatchCandidateOut(
        profile_id=profile.id,
        user_id=user.id,
        full_name=user.full_name,
        email=user.email,
        phone=user.phone,
        bike_year=profile.bike_year,
        bike_make=profile.bike_make,
        bike_model=profile.bike_model,
        city=profile.city,
        state=profile.state,
        service_radius_miles=profile.service_radius_miles,
        photo_url=profile.photo_url,
        is_available=profile.is_available,
        has_insurance=profile.has_insurance,
        insurance_covers_passenger=profile.insurance_covers_passenger,
        passenger_responsibility_accepted=profile.passenger_responsibility_accepted,
        passenger_responsibility_name=profile.passenger_responsibility_name,
        passenger_responsibility_date=profile.passenger_responsibility_date,
        insurance_company=profile.insurance_company,
        insurance_expiration_date=profile.insurance_expiration_date,
        distance_miles=distance_miles,
    )


def _ride_detail_out(db: Session, ride: models.RideRequest) -> schemas.RideDetailOut:
    base = ride_out.enrich_ride_out(ride, db)
    out = schemas.RideDetailOut(
        **base.model_dump(),
        rider=schemas.UserBrief.model_validate(ride.rider),
    )
    if ride.biker_id is not None:
        biker = db.get(models.User, ride.biker_id)
        if biker is not None:
            out.biker = schemas.UserBrief.model_validate(biker)
    return out


def _approved_available_bikers(db: Session) -> list[models.BikerProfile]:
    return (
        db.query(models.BikerProfile)
        .options(joinedload(models.BikerProfile.user))
        .filter(models.BikerProfile.is_available.is_(True))
        .filter(models.BikerProfile.approval_status == _APPROVED)
        .order_by(models.BikerProfile.updated_at.desc())
        .limit(100)
        .all()
    )


def _rank_candidates(
    db: Session, ride: models.RideRequest, within_miles: float | None
) -> tuple[bool, list[schemas.DispatchCandidateOut]]:
    """Approved + available bikers ranked for one ride.

    Returns (pickup_has_coords, candidates). When the pickup was geocoded,
    candidates are filtered to each biker's service radius (or
    `within_miles`) and sorted nearest first; bikers without coordinates
    list last with distance "n/a" so Kenneth can still assign them manually.
    Exact distance ties sort passenger-covered policies first.
    """
    has_coords = (
        ride.pickup_latitude is not None and ride.pickup_longitude is not None
    )
    bikers = _approved_available_bikers(db)
    ranked: list[tuple[models.BikerProfile, float | None]] = []
    for profile in bikers:
        distance: float | None = None
        if (
            has_coords
            and profile.latitude is not None
            and profile.longitude is not None
        ):
            distance = geo.haversine_miles(
                ride.pickup_latitude,
                ride.pickup_longitude,
                profile.latitude,
                profile.longitude,
            )
            cap = (
                within_miles
                if within_miles is not None
                else profile.service_radius_miles
            )
            if distance > cap:
                continue
        ranked.append((profile, distance))
    if has_coords:
        ranked.sort(
            key=lambda t: (
                t[1] if t[1] is not None else float("inf"),
                not t[0].insurance_covers_passenger,
            )
        )
    return has_coords, [
        _candidate_out(p, round(d, 1) if d is not None else None)
        for p, d in ranked
    ]


@router.get("/dispatch", response_model=schemas.DispatchOverviewOut)
def dispatch_overview(
    within_miles: float | None = Query(
        default=None,
        ge=1,
        le=1000,
        description="Uniform distance cap overriding each biker's service radius",
    ),
    admin: models.User = Depends(auth.require_role("admin")),
    db: Session = Depends(get_db),
):
    """Kenneth's dispatch board: pending rides + ranked biker candidates.

    Each pending ride carries the rider's contact info and the approved +
    available bikers for that ride. When the pickup was geocoded, candidates
    are filtered to the biker's service radius (or `within_miles`) and sorted
    nearest first with `distance_miles`; otherwise every eligible biker is
    listed without distances.
    """
    rides = (
        db.query(models.RideRequest)
        .options(joinedload(models.RideRequest.rider))
        .filter(models.RideRequest.status == "pending")
        .order_by(models.RideRequest.created_at.desc())
        .limit(200)
        .all()
    )
    board: list[schemas.DispatchRideOut] = []
    for ride in rides:
        _, candidates = _rank_candidates(db, ride, within_miles)
        board.append(
            schemas.DispatchRideOut(
                **schemas.RideRequestOut.model_validate(ride).model_dump(),
                rider=schemas.UserBrief.model_validate(ride.rider),
                candidates=candidates,
            )
        )
    return schemas.DispatchOverviewOut(requests=board)


@router.post("/rides/{ride_id}/assign", response_model=schemas.RideDetailOut)
def assign_ride(
    ride_id: int,
    body: schemas.RideAssignIn,
    admin: models.User = Depends(auth.require_role("admin")),
    db: Session = Depends(get_db),
):
    """Manually dispatch: assign a biker to a ride, moving it to matched.

    The biker (by user id) must have an approved profile and be currently
    available. Assigning to a matched ride re-assigns it to the new biker.
    """
    ride = db.get(models.RideRequest, ride_id)
    if ride is None:
        raise HTTPException(status_code=404, detail="Ride not found")
    if ride.status not in {"pending", "matched"}:
        raise HTTPException(
            status_code=400, detail=f"Cannot assign a {ride.status} ride"
        )
    biker_user = db.get(models.User, body.biker_user_id)
    if biker_user is None:
        raise HTTPException(status_code=404, detail="Biker user not found")
    if biker_user.role != "biker":
        raise HTTPException(status_code=400, detail="That user is not a biker")
    profile = biker_user.biker_profile
    if (
        profile is None
        or profile.approval_status != _APPROVED
        or not profile.is_available
    ):
        raise HTTPException(
            status_code=400,
            detail="That biker is not approved and available for dispatch",
        )
    ride.biker_id = biker_user.id
    ride.status = "matched"
    db.commit()
    db.refresh(ride)
    return _ride_detail_out(db, ride)


@router.post("/rides/{ride_id}/unassign", response_model=schemas.RideDetailOut)
def unassign_ride(
    ride_id: int,
    admin: models.User = Depends(auth.require_role("admin")),
    db: Session = Depends(get_db),
):
    """Pull a biker off a matched ride, sending it back to pending."""
    ride = db.get(models.RideRequest, ride_id)
    if ride is None:
        raise HTTPException(status_code=404, detail="Ride not found")
    if ride.status != "matched":
        raise HTTPException(
            status_code=400, detail=f"Cannot unassign a {ride.status} ride"
        )
    ride.biker_id = None
    ride.status = "pending"
    db.commit()
    db.refresh(ride)
    return _ride_detail_out(db, ride)


# ---------- Matched rides + payout queue (M7 part 1) ----------
@router.get("/matched", response_model=schemas.DispatchOverviewOut)
def matched_rides(
    admin: models.User = Depends(auth.require_role("admin")),
    db: Session = Depends(get_db),
):
    """Kenneth's matched-rides view: rides that have a biker but aren't
    completed yet. This is where he completes rides (or the biker does it
    from their own panel), which moves the ride to the payout queue."""
    rides = (
        db.query(models.RideRequest)
        .options(joinedload(models.RideRequest.rider))
        .filter(models.RideRequest.status == "matched")
        .order_by(models.RideRequest.created_at.desc())
        .limit(200)
        .all()
    )
    board: list[schemas.DispatchRideOut] = []
    for ride in rides:
        detail = _ride_detail_out(db, ride)
        board.append(
            schemas.DispatchRideOut(
                **detail.model_dump(), candidates=[]
            )
        )
    return schemas.DispatchOverviewOut(requests=board)


def _payout_out(db: Session, payout: models.PayoutRecord) -> schemas.PayoutRecordOut:
    return schemas.PayoutRecordOut(
        id=payout.id,
        ride_id=payout.ride_id,
        fare_cents=payout.fare_cents,
        biker_share_cents=payout.biker_share_cents,
        platform_share_cents=payout.platform_share_cents,
        status=payout.status,
        paid_reference=payout.paid_reference,
        paid_at=payout.paid_at,
        created_at=payout.created_at,
        biker=schemas.UserBrief.model_validate(payout.biker),
        rider=schemas.UserBrief.model_validate(payout.rider),
    )


@router.get("/payouts/summary", response_model=schemas.PayoutSummaryOut)
def payout_summary(
    admin: models.User = Depends(auth.require_role("admin")),
    db: Session = Depends(get_db),
):
    """Kenneth's at-a-glance payout ledger: pending vs paid totals plus a
    per-biker breakdown (pending only — the actionable side), sorted so the
    biker owed the most comes first. Admin-only."""
    rows = (
        db.query(
            models.PayoutRecord.status,
            models.PayoutRecord.biker_user_id,
            models.User.full_name,
            func.count(models.PayoutRecord.id),
            func.coalesce(func.sum(models.PayoutRecord.fare_cents), 0),
            func.coalesce(func.sum(models.PayoutRecord.biker_share_cents), 0),
            func.coalesce(func.sum(models.PayoutRecord.platform_share_cents), 0),
        )
        .join(models.User, models.User.id == models.PayoutRecord.biker_user_id)
        .group_by(
            models.PayoutRecord.status,
            models.PayoutRecord.biker_user_id,
            models.User.full_name,
        )
        .all()
    )
    pending = {"count": 0, "fare_cents": 0, "biker_cents": 0, "platform_cents": 0}
    paid = {"count": 0, "fare_cents": 0, "biker_cents": 0, "platform_cents": 0}
    per_biker: dict[int, dict] = {}
    for status, biker_id, biker_name, count, fare, biker_c, platform_c in rows:
        bucket = pending if status == "pending" else paid
        bucket["count"] += count
        bucket["fare_cents"] += int(fare)
        bucket["biker_cents"] += int(biker_c)
        bucket["platform_cents"] += int(platform_c)
        entry = per_biker.setdefault(
            biker_id,
            {
                "biker_id": biker_id,
                "biker_name": biker_name or "Unknown biker",
                "pending_count": 0,
                "pending_biker_cents": 0,
                "pending_platform_cents": 0,
                "paid_count": 0,
                "paid_biker_cents": 0,
            },
        )
        if status == "pending":
            entry["pending_count"] = count
            entry["pending_biker_cents"] = int(biker_c)
            entry["pending_platform_cents"] = int(platform_c)
        else:
            entry["paid_count"] = count
            entry["paid_biker_cents"] = int(biker_c)
    by_biker = [e for e in per_biker.values() if e["pending_count"] > 0]
    by_biker.sort(key=lambda e: e["pending_biker_cents"], reverse=True)
    return schemas.PayoutSummaryOut(
        pending=schemas.PayoutBucket(**pending),
        paid=schemas.PayoutBucket(**paid),
        by_biker=[schemas.PayoutBikerSummary(**e) for e in by_biker],
    )


@router.get("/payouts/export.csv")
def export_payouts_csv(
    status: str | None = Query(default=None, pattern="^(pending|paid)$"),
    admin: models.User = Depends(auth.require_role("admin")),
    db: Session = Depends(get_db),
):
    """Download the recorded 75/25 splits as a CSV for Kenneth's bookkeeping.

    Same data as the payout queue (optionally filtered with
    ?status=pending|paid), so he can keep the manual PayPal payout records
    in a spreadsheet. Money columns are dollars with 2 decimals; timestamps
    are ISO-8601.
    """
    q = (
        db.query(models.PayoutRecord)
        .options(
            joinedload(models.PayoutRecord.biker),
            joinedload(models.PayoutRecord.rider),
        )
    )
    if status:
        q = q.filter(models.PayoutRecord.status == status)
    rows = q.order_by(models.PayoutRecord.created_at.desc()).limit(2000).all()

    def dollars(cents: int) -> str:
        return f"{cents / 100:.2f}"

    def iso(dt) -> str:
        return dt.isoformat() if dt else ""

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "payout_id", "ride_id", "rider_name", "rider_email",
        "biker_name", "biker_email", "fare_usd", "biker_share_usd",
        "platform_share_usd", "status", "paypal_reference",
        "recorded_at", "paid_at",
    ])
    for p in rows:
        biker = p.biker or models.User()
        rider = p.rider or models.User()
        w.writerow([
            p.id,
            p.ride_id,
            rider.full_name or "",
            rider.email or "",
            biker.full_name or "",
            biker.email or "",
            dollars(p.fare_cents),
            dollars(p.biker_share_cents),
            dollars(p.platform_share_cents),
            p.status,
            p.paid_reference or "",
            iso(p.created_at),
            iso(p.paid_at),
        ])
    fname = f"rent-a-biker-payouts-{status or 'all'}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/payouts", response_model=list[schemas.PayoutRecordOut])
def payout_queue(
    status: str | None = Query(default=None, pattern="^(pending|paid)$"),
    admin: models.User = Depends(auth.require_role("admin")),
    db: Session = Depends(get_db),
):
    """Kenneth's payout queue: every recorded 75/25 split.

    Manual PayPal payouts continue — he works down this list and marks each
    payout paid after the transfer. Filter with ?status=pending (default
    view) or ?status=paid.
    """
    q = (
        db.query(models.PayoutRecord)
        .options(
            joinedload(models.PayoutRecord.biker),
            joinedload(models.PayoutRecord.rider),
        )
    )
    if status:
        q = q.filter(models.PayoutRecord.status == status)
    q = q.order_by(models.PayoutRecord.created_at.desc()).limit(200)
    return [_payout_out(db, p) for p in q.all()]


@router.post(
    "/payouts/{payout_id}/mark-paid", response_model=schemas.PayoutRecordOut
)
def mark_payout_paid(
    payout_id: int,
    body: schemas.PayoutMarkPaidIn,
    admin: models.User = Depends(auth.require_role("admin")),
    db: Session = Depends(get_db),
):
    """Record a completed manual payout: after Kenneth sends the biker's
    75% via PayPal, this marks the payout paid (optional reference, e.g.
    the PayPal transaction id, for the record)."""
    payout = db.get(models.PayoutRecord, payout_id)
    if payout is None:
        raise HTTPException(status_code=404, detail="Payout not found")
    if payout.status == "paid":
        raise HTTPException(status_code=400, detail="This payout is already marked paid")
    payout.status = "paid"
    payout.paid_at = _utcnow()
    payout.paid_reference = (body.reference or "").strip() or None
    db.commit()
    db.refresh(payout)
    return _payout_out(db, payout)


@router.post("/rides/{ride_id}/auto-match", response_model=schemas.AutoMatchOut)
def auto_match_ride(
    ride_id: int,
    within_miles: float | None = Query(
        default=None,
        ge=1,
        le=1000,
        description="Uniform distance cap overriding each biker's service radius",
    ),
    admin: models.User = Depends(auth.require_role("admin")),
    db: Session = Depends(get_db),
):
    """One-click dispatch: assign the nearest eligible biker to a pending ride.

    Only pending rides can be auto-matched. The pickup must have coordinates
    (booking geocodes it); only bikers with a measurable distance inside
    their service radius (default 50 miles, or `within_miles`) are eligible,
    nearest first, with passenger-covered policies winning exact ties.
    Kenneth keeps the final say: unassign or re-assign works as usual.
    """
    ride = db.get(models.RideRequest, ride_id)
    if ride is None:
        raise HTTPException(status_code=404, detail="Ride not found")
    if ride.status != "pending":
        raise HTTPException(
            status_code=400, detail=f"Cannot auto-match a {ride.status} ride"
        )
    has_coords, candidates = _rank_candidates(db, ride, within_miles)
    if not has_coords:
        raise HTTPException(
            status_code=400,
            detail="The pickup location hasn't been mapped yet — "
            "assign a biker manually.",
        )
    measurable = [c for c in candidates if c.distance_miles is not None]
    if not measurable:
        raise HTTPException(
            status_code=400,
            detail="No approved + available bikers within range right now.",
        )
    best = measurable[0]
    ride.biker_id = best.user_id
    ride.status = "matched"
    db.commit()
    db.refresh(ride)
    return schemas.AutoMatchOut(
        ride=_ride_detail_out(db, ride), assigned_candidate=best
    )


# ---------- Legacy import (M8: static-app-era signup lists into accounts) ----------


@router.get("/import/templates/{kind}.csv")
def download_import_template(
    kind: str,
    admin: models.User = Depends(auth.require_role("admin")),
):
    """Download a header-only CSV template for the legacy import.

    `kind` is `riders` or `bikers`. Fill one row per signup from the static
    app's era and upload it with POST /admin/import — imported accounts are
    created with temporary passwords and land in the normal flow (bikers in
    the review queue as pending, agreements still required in-app).
    """
    if kind not in importing._COLUMNS:
        raise HTTPException(
            status_code=404, detail="Template not found (riders or bikers)"
        )
    buf = io.StringIO()
    csv.writer(buf).writerow(importing._COLUMNS[kind])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="rent-a-biker-import-{kind}-template.csv"'
        },
    )


@router.post("/import", response_model=schemas.LegacyImportResultOut, status_code=201)
def import_legacy_signups(
    kind: str = Form(pattern="^(riders|bikers)$"),
    file: UploadFile = File(),
    admin: models.User = Depends(auth.require_role("admin")),
    db: Session = Depends(get_db),
):
    """Import a legacy rider/biker signup CSV into real accounts.

    Multipart form: `kind` (riders|bikers) + `file` (the CSV, <= 5 MB).
    Rows with problems are listed in `errors` and never block the good
    rows; emails that already have an account are skipped. The result's
    `credentials` carry each new account's one-time temporary password —
    shown only here, so Kenneth downloads it now and hands them out.
    """
    raw = file.file.read()
    if len(raw) > importing.MAX_FILE_BYTES:
        raise HTTPException(status_code=400, detail="File is too large (max 5 MB)")
    try:
        result = importing.import_legacy_csv(db, kind, raw)
    except importing.LegacyImportFileError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return schemas.LegacyImportResultOut(**result)
