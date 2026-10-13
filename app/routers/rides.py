"""Ride/event request endpoints."""
import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from .. import auth, fares, geo, legal, models, ride_out, schemas
from ..db import get_db

router = APIRouter(prefix="/rides", tags=["rides"])

CANCELLABLE = {"pending", "matched"}


def _normalize_address(address: str) -> str:
    return " ".join(address.split()).lower()


def _resolve_pickup_coordinates(
    db: Session, pickup_address: str
) -> tuple[float, float] | None:
    """Geocode a pickup address for dispatch distance matching.

    Cached by normalized address (each address is looked up at most once) and
    never blocks booking: any failure returns None and the ride simply carries
    no pickup coordinates.
    """
    try:
        key = _normalize_address(pickup_address)
        if not key:
            return None
        cached = (
            db.query(models.AddressGeocodeCache)
            .filter(models.AddressGeocodeCache.address == key)
            .first()
        )
        if cached:
            return cached.latitude, cached.longitude
        coords = geo.geocode_address(pickup_address)
        if coords:
            db.add(
                models.AddressGeocodeCache(
                    address=key, latitude=coords[0], longitude=coords[1]
                )
            )
        return coords
    except Exception:
        return None


@router.post("/", response_model=schemas.RideRequestOut, status_code=201)
def create_ride(
    body: schemas.RideRequestIn,
    user: models.User = Depends(auth.require_role("rider", "admin")),
    db: Session = Depends(get_db),
):
    if not body.waiver_signed or not body.waiver_name or not body.waiver_date:
        raise HTTPException(
            status_code=400,
            detail="Liability waiver (signed, name, date) is required before booking.",
        )
    if body.ride_type == "ride":
        if body.estimated_miles is None:
            raise HTTPException(status_code=400, detail="estimated_miles is required for rides.")
        fare = fares.estimate_ride_fare_cents(body.estimated_miles)
    else:
        if body.duration_hours is None:
            raise HTTPException(
                status_code=400, detail="duration_hours is required for events."
            )
        fare = fares.estimate_event_fare_cents(body.duration_hours)

    pickup = body.pickup_address.strip()
    coords = _resolve_pickup_coordinates(db, pickup)
    ride = models.RideRequest(
        rider_id=user.id,
        ride_type=body.ride_type,
        pickup_address=pickup,
        dropoff_address=(body.dropoff_address or "").strip() or None,
        scheduled_at=body.scheduled_at,
        estimated_miles=body.estimated_miles,
        duration_hours=body.duration_hours,
        fare_estimate_cents=fare,
        pickup_latitude=coords[0] if coords else None,
        pickup_longitude=coords[1] if coords else None,
        waiver_signed=True,
        waiver_name=body.waiver_name.strip(),
        waiver_date=body.waiver_date.strip(),
        waiver_version=legal.RIDER_WAIVER_VERSION,
        helmet_needed=body.helmet_needed,
        notes=(body.notes or "").strip() or None,
    )
    db.add(ride)
    db.commit()
    db.refresh(ride)
    return ride_out.enrich_ride_out(ride, db)


@router.post("/estimate", response_model=schemas.RideEstimateOut)
def estimate_fare(body: schemas.RideEstimateIn):
    """Fare preview for the booking form — pure math, no booking, no waiver."""
    if body.ride_type == "ride":
        if body.estimated_miles is None:
            raise HTTPException(status_code=400, detail="estimated_miles is required for rides.")
        fare = fares.estimate_ride_fare_cents(body.estimated_miles)
    else:
        if body.duration_hours is None:
            raise HTTPException(
                status_code=400, detail="duration_hours is required for events."
            )
        fare = fares.estimate_event_fare_cents(body.duration_hours)
    return {"fare_estimate_cents": fare}


@router.get("/me", response_model=list[schemas.RideRequestOut])
def my_rides(
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    return ride_out.enrich_ride_outs(
        db.query(models.RideRequest)
        .filter(models.RideRequest.rider_id == user.id)
        .order_by(models.RideRequest.created_at.desc())
        .all(),
        db,
    )


@router.get("/assigned", response_model=list[schemas.RideRequestOut])
def my_assigned_rides(
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    """Rides assigned to the signed-in user (bikers see their dispatch queue)."""
    return ride_out.enrich_ride_outs(
        db.query(models.RideRequest)
        .filter(models.RideRequest.biker_id == user.id)
        .order_by(models.RideRequest.created_at.desc())
        .all(),
        db,
    )


@router.get("/earnings", response_model=schemas.BikerEarningsOut)
def my_earnings(
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    """The signed-in user's earnings as a biker: what Kenneth owes them
    (pending), what they have already received (paid), and their lifetime
    biker-cut total, plus their own payout records newest first.

    Only records where the user IS the biker are ever returned — a rider,
    another biker, or an outsider can never see someone else's payouts.
    """
    records = (
        db.query(models.PayoutRecord)
        .filter(models.PayoutRecord.biker_user_id == user.id)
        .order_by(models.PayoutRecord.created_at.desc())
        .all()
    )
    payouts = [
        schemas.BikerPayoutOut(
            id=r.id,
            ride_id=r.ride_id,
            fare_cents=r.fare_cents,
            biker_share_cents=r.biker_share_cents,
            status=r.status,
            paid_at=r.paid_at,
            created_at=r.created_at,
        )
        for r in records
    ]
    pending = [r for r in records if r.status == "pending"]
    paid = [r for r in records if r.status == "paid"]
    return schemas.BikerEarningsOut(
        pending_owed_cents=sum(r.biker_share_cents for r in pending),
        pending_ride_count=len(pending),
        paid_received_cents=sum(r.biker_share_cents for r in paid),
        paid_ride_count=len(paid),
        lifetime_biker_cents=sum(r.biker_share_cents for r in records),
        payouts=payouts,
    )


@router.get("/earnings/export.csv")
def export_earnings_csv(
    status: str | None = Query(default=None, pattern="^(pending|paid)$"),
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    """Download the signed-in user's biker earnings as a CSV for their records.

    The same payout records as GET /rides/earnings (optionally filtered with
    ?status=pending|paid): only rows where the user IS the biker are ever
    included — a rider, another biker, or an outsider can never export someone
    else's money. Money columns are dollars with 2 decimals; timestamps are
    ISO-8601. Rider contact data is deliberately excluded from the export.
    """
    q = db.query(models.PayoutRecord).filter(
        models.PayoutRecord.biker_user_id == user.id
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
        "payout_id", "ride_id", "fare_usd", "biker_share_usd",
        "status", "paypal_reference", "recorded_at", "paid_at",
    ])
    for p in rows:
        w.writerow([
            p.id,
            p.ride_id,
            dollars(p.fare_cents),
            dollars(p.biker_share_cents),
            p.status,
            p.paid_reference or "",
            iso(p.created_at),
            iso(p.paid_at),
        ])
    fname = f"rent-a-biker-earnings-{status or 'all'}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/{ride_id}", response_model=schemas.RideRequestOut)
def get_ride(
    ride_id: int,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    ride = db.get(models.RideRequest, ride_id)
    if ride is None:
        raise HTTPException(status_code=404, detail="Ride not found")
    if user.role != "admin" and ride.rider_id != user.id and ride.biker_id != user.id:
        raise HTTPException(status_code=403, detail="Not your ride")
    return ride_out.enrich_ride_out(ride, db)


@router.post("/{ride_id}/cancel", response_model=schemas.RideRequestOut)
def cancel_ride(
    ride_id: int,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    ride = db.get(models.RideRequest, ride_id)
    if ride is None:
        raise HTTPException(status_code=404, detail="Ride not found")
    if user.role != "admin" and ride.rider_id != user.id:
        raise HTTPException(status_code=403, detail="Not your ride")
    if ride.status not in CANCELLABLE:
        raise HTTPException(status_code=400, detail=f"Cannot cancel a {ride.status} ride")
    ride.status = "cancelled"
    db.commit()
    db.refresh(ride)
    return ride_out.enrich_ride_out(ride, db)


# ---------- Ride completion + payout record (M7 part 1) ----------
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


@router.post("/{ride_id}/complete", response_model=schemas.CompleteRideOut)
def complete_ride(
    ride_id: int,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    """Mark a matched ride completed and record the 75/25 split in the DB.

    Only the assigned biker (after the ride) or Kenneth (admin) can
    complete a ride. Manual PayPal payouts continue — the returned payout
    record lands in Kenneth's payout queue until he marks it paid.
    """
    ride = db.get(models.RideRequest, ride_id)
    if ride is None:
        raise HTTPException(status_code=404, detail="Ride not found")
    if user.role != "admin" and ride.biker_id != user.id:
        raise HTTPException(
            status_code=403,
            detail="Only the assigned biker or Kenneth can complete this ride",
        )
    if ride.status != "matched":
        raise HTTPException(
            status_code=400, detail=f"Cannot complete a {ride.status} ride"
        )
    existing = (
        db.query(models.PayoutRecord)
        .filter(models.PayoutRecord.ride_id == ride.id)
        .first()
    )
    if existing is not None:
        raise HTTPException(status_code=400, detail="This ride already has a payout record")
    split = fares.split_fare_cents(ride.fare_estimate_cents)
    payout = models.PayoutRecord(
        ride_id=ride.id,
        biker_user_id=ride.biker_id,
        rider_user_id=ride.rider_id,
        fare_cents=ride.fare_estimate_cents,
        biker_share_cents=split["biker_cents"],
        platform_share_cents=split["platform_cents"],
    )
    ride.status = "completed"
    db.add(payout)
    db.commit()
    db.refresh(ride)
    db.refresh(payout)
    return schemas.CompleteRideOut(
        ride=ride_out.enrich_ride_out(ride, db),
        payout=_payout_out(db, payout),
    )


# ---------- In-app messaging (M5) ----------
def _ride_party(db: Session, ride_id: int, user: models.User) -> models.RideRequest:
    """The ride's conversation parties: the rider, the assigned biker (if
    any), and Kenneth (admin). Anyone else is out."""
    ride = db.get(models.RideRequest, ride_id)
    if ride is None:
        raise HTTPException(status_code=404, detail="Ride not found")
    if (
        user.role == "admin"
        or ride.rider_id == user.id
        or (ride.biker_id is not None and ride.biker_id == user.id)
    ):
        return ride
    raise HTTPException(status_code=403, detail="Not your ride")


def _message_out(db: Session, msg: models.Message) -> schemas.MessageOut:
    sender = db.get(models.User, msg.sender_id)
    return schemas.MessageOut(
        id=msg.id,
        ride_id=msg.ride_id,
        sender_id=msg.sender_id,
        sender_name=sender.full_name if sender else "?",
        sender_role=sender.role if sender else "?",
        body=msg.body,
        created_at=msg.created_at,
    )


@router.get("/{ride_id}/messages", response_model=list[schemas.MessageOut])
def list_messages(
    ride_id: int,
    after_id: int = 0,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    """A ride's message thread, newest-first-pollable via ?after_id=<last seen>.

    Used for polling: the frontend asks every few seconds for messages with
    an id greater than the last one it has, so the thread stays fresh.
    """
    ride = _ride_party(db, ride_id, user)
    msgs = (
        db.query(models.Message)
        .filter(models.Message.ride_id == ride.id, models.Message.id > after_id)
        .order_by(models.Message.id.asc())
        .all()
    )
    return [_message_out(db, m) for m in msgs]


@router.post("/{ride_id}/messages", response_model=schemas.MessageOut, status_code=201)
def post_message(
    ride_id: int,
    body: schemas.MessageIn,
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    """Post one message to a ride's thread."""
    ride = _ride_party(db, ride_id, user)
    text = (body.body or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")
    if ride.status == "cancelled":
        raise HTTPException(
            status_code=400, detail="Cannot message on a cancelled ride."
        )
    msg = models.Message(ride_id=ride.id, sender_id=user.id, body=text[:2000])
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return _message_out(db, msg)
