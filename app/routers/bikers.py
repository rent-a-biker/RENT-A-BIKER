"""Biker profile endpoints."""
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from .. import auth, geo, legal, models, schemas, uploads
from ..db import get_db

router = APIRouter(prefix="/bikers", tags=["bikers"])


def resolve_coordinates(
    db: Session, city: str, state: str
) -> tuple[float, float] | None:
    """City/state -> (lat, lng): local cache first, free geocoder second.

    The cache means each place is looked up at most once. A failed lookup
    returns None and the profile simply keeps empty coordinates.
    """
    city = city.strip()
    state = state.strip().upper()
    cached = (
        db.query(models.GeocodeCache)
        .filter(models.GeocodeCache.city == city, models.GeocodeCache.state == state)
        .first()
    )
    if cached:
        return cached.latitude, cached.longitude
    coords = geo.geocode_city_state(city, state)
    if coords:
        db.add(
            models.GeocodeCache(
                city=city, state=state, latitude=coords[0], longitude=coords[1]
            )
        )
    return coords


@router.post("/profile", response_model=schemas.BikerProfileOut)
def upsert_profile(
    body: schemas.BikerProfileIn,
    user: models.User = Depends(auth.require_role("biker", "admin")),
    db: Session = Depends(get_db),
):
    profile = (
        db.query(models.BikerProfile)
        .filter(models.BikerProfile.user_id == user.id)
        .first()
    )
    data = body.model_dump()
    data["state"] = data["state"].upper()
    # M6: stamp the legal-text versions the biker actually accepted on this
    # save. Unchecking an agreement clears its version (the record always
    # matches the current acceptance state).
    data["pledge_version"] = (
        legal.BIKER_PLEDGE_VERSION if data["pledge_accepted"] else None
    )
    data["liability_version"] = (
        legal.BIKER_LIABILITY_VERSION if data["liability_accepted"] else None
    )
    # Manual coordinates only count as a pair; a changed city/state always
    # re-resolves (manual coords would be stale); otherwise the free geocoder
    # fills empty coordinates. Any failure leaves them empty — never blocks save.
    manual = data.get("latitude") is not None and data.get("longitude") is not None
    city_changed = profile is not None and (
        profile.city != data["city"] or profile.state != data["state"]
    )
    if not manual or city_changed:
        data["latitude"] = data["longitude"] = None
        coords = resolve_coordinates(db, data["city"], data["state"])
        if coords:
            data["latitude"], data["longitude"] = coords
    if profile is None:
        profile = models.BikerProfile(user_id=user.id, **data)
        db.add(profile)
    else:
        for key, value in data.items():
            setattr(profile, key, value)
    db.commit()
    db.refresh(profile)
    return profile


@router.get("/profile/me", response_model=schemas.BikerProfileOut)
def my_profile(
    user: models.User = Depends(auth.require_role("biker", "admin")),
    db: Session = Depends(get_db),
):
    profile = (
        db.query(models.BikerProfile)
        .filter(models.BikerProfile.user_id == user.id)
        .first()
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="No biker profile yet")
    return profile


@router.post("/profile/availability", response_model=schemas.BikerProfileOut)
def set_availability(
    body: schemas.AvailabilityIn,
    user: models.User = Depends(auth.require_role("biker", "admin")),
    db: Session = Depends(get_db),
):
    """Flip the biker's availability switch ("I'm ready to ride" toggle).

    Only the flag changes — no other profile fields are touched, so the
    frontend can drive this from a single button. Showing up in
    /bikers/available still requires Kenneth's approval (approval_status).
    """
    profile = (
        db.query(models.BikerProfile)
        .filter(models.BikerProfile.user_id == user.id)
        .first()
    )
    if profile is None:
        raise HTTPException(
            status_code=404, detail="Create a biker profile before toggling availability"
        )
    profile.is_available = body.is_available
    db.commit()
    db.refresh(profile)
    return profile


@router.get("/available", response_model=list[schemas.BikerPublicOut])
def available_bikers(
    state: str | None = None,
    near_lat: float | None = Query(default=None, ge=-90, le=90),
    near_lng: float | None = Query(default=None, ge=-180, le=180),
    within_miles: float = Query(default=50.0, ge=0, le=1000),
    db: Session = Depends(get_db),
    _user: models.User = Depends(auth.get_current_user),
):
    """List bikers currently flagged available AND approved (for dispatch matching).

    With near_lat/near_lng: only bikers with coordinates inside `within_miles`
    (default 50, Kenneth's service rule) are returned, nearest first, each
    carrying `distance_miles`. Bikers without coordinates are skipped. With only
    one of near_lat/near_lng given, distance filtering is skipped.
    """
    q = (
        db.query(models.BikerProfile)
        .filter(models.BikerProfile.is_available.is_(True))
        .filter(models.BikerProfile.approval_status == "approved")
    )
    if state:
        q = q.filter(models.BikerProfile.state == state.strip().upper())
    profiles = q.order_by(models.BikerProfile.updated_at.desc()).limit(100).all()

    if near_lat is None or near_lng is None:
        return [schemas.BikerPublicOut.model_validate(p) for p in profiles]

    ranked: list[tuple[float, schemas.BikerPublicOut]] = []
    for p in profiles:
        if p.latitude is None or p.longitude is None:
            continue
        dist = geo.haversine_miles(near_lat, near_lng, p.latitude, p.longitude)
        if dist <= within_miles:
            item = schemas.BikerPublicOut.model_validate(p)
            item.distance_miles = round(dist, 1)
            ranked.append((dist, item))
    ranked.sort(key=lambda t: t[0])
    return [item for _, item in ranked]


@router.post("/profile/photo", response_model=schemas.BikerProfileOut)
async def upload_bike_photo(
    photo: UploadFile = File(...),
    user: models.User = Depends(auth.require_role("biker", "admin")),
    db: Session = Depends(get_db),
):
    """Upload (or replace) the bike photo for the caller's biker profile.

    JPEG/PNG/WebP only, max 5 MB. Stored locally under a server-generated
    filename; the old photo is deleted on re-upload. Requires a profile to
    exist first (POST /bikers/profile).
    """
    profile = (
        db.query(models.BikerProfile)
        .filter(models.BikerProfile.user_id == user.id)
        .first()
    )
    if profile is None:
        raise HTTPException(
            status_code=404, detail="Create a biker profile before uploading a photo"
        )
    data = await photo.read()
    ok, detail = uploads.validate_photo(photo.content_type, data)
    if not ok:
        status = 413 if detail.startswith("Photo is too large") else 415
        raise HTTPException(status_code=status, detail=detail)
    filename = uploads.save_photo(data, detail)
    uploads.delete_photo(profile.photo_filename)
    profile.photo_filename = filename
    db.commit()
    db.refresh(profile)
    return profile
