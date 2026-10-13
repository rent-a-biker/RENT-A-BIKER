"""Shared ride serialization: plain-English status lines and the rider-facing
assigned-biker brief (M3 rider-experience polish, Oct 2026).

The brief deliberately carries no contact info (email/phone) and no insurance
data — those stay in the admin/dispatch views. The rider and their assigned
biker coordinate through in-app messaging (M5).
"""
from sqlalchemy.orm import Session, joinedload

from . import models, schemas

STATUS_DETAIL = {
    "pending": (
        "Waiting for a biker to be assigned — Kenneth is lining one up "
        "and you'll see them here."
    ),
    "matched": (
        "Your biker is assigned — their details are below, "
        "and you can message them anytime."
    ),
    "en_route": "Your biker is on the way to your pickup.",
    "in_progress": "You're on the ride — enjoy!",
    "completed": "Ride completed — thanks for riding with Rent a Biker.",
    "cancelled": "This request was cancelled.",
}


def status_detail_for(status: str | None) -> str:
    """Plain-English one-liner for a ride status, shown on ride cards."""
    return STATUS_DETAIL.get(status or "", f"Status: {status}.")


def _brief_from_user(biker: models.User) -> schemas.AssignedBikerBrief | None:
    profile = biker.biker_profile
    if biker.role != "biker" or profile is None:
        return None
    return schemas.AssignedBikerBrief(
        user_id=biker.id,
        full_name=biker.full_name,
        bike_year=profile.bike_year,
        bike_make=profile.bike_make,
        bike_model=profile.bike_model,
        city=profile.city,
        state=profile.state,
        photo_url=profile.photo_url,
    )


def enrich_ride_outs(
    rides: list[models.RideRequest], db: Session
) -> list[schemas.RideRequestOut]:
    """Attach status_detail + the assigned-biker brief to each ride.

    Bikers are batch-loaded (one query) so lists don't N+1.
    """
    biker_ids = {r.biker_id for r in rides if r.biker_id}
    briefs: dict[int, schemas.AssignedBikerBrief] = {}
    if biker_ids:
        users = (
            db.query(models.User)
            .options(joinedload(models.User.biker_profile))
            .filter(models.User.id.in_(biker_ids))
            .all()
        )
        for u in users:
            brief = _brief_from_user(u)
            if brief is not None:
                briefs[u.id] = brief
    outs = []
    for ride in rides:
        out = schemas.RideRequestOut.model_validate(ride)
        out.status_detail = status_detail_for(ride.status)
        out.assigned_biker = briefs.get(ride.biker_id) if ride.biker_id else None
        outs.append(out)
    return outs


def enrich_ride_out(
    ride: models.RideRequest, db: Session
) -> schemas.RideRequestOut:
    return enrich_ride_outs([ride], db)[0]
