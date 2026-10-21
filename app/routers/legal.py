"""Legal text endpoints — public GETs serving the server-canonical text.

The frontend always renders these (never hard-coded copies), and every
booking / biker application stamps the text version into the signed record,
so the record always points at the wording that was actually agreed to.
"""
from fastapi import APIRouter

from .. import legal, schemas

router = APIRouter(prefix="/legal", tags=["legal"])


@router.get("/waiver", response_model=schemas.LegalTextOut)
def rider_waiver():
    """The rider liability waiver shown in the booking form."""
    return {
        "key": "rider-waiver",
        "version": legal.RIDER_WAIVER_VERSION,
        "text": legal.RIDER_WAIVER_TEXT,
    }


@router.get("/biker-pledge", response_model=schemas.LegalTextOut)
def biker_pledge():
    """The trusted-biker pledge accepted on every biker application."""
    return {
        "key": "biker-pledge",
        "version": legal.BIKER_PLEDGE_VERSION,
        "text": legal.BIKER_PLEDGE_TEXT,
    }


@router.get("/biker-liability", response_model=schemas.LegalTextOut)
def biker_liability():
    """The biker liability agreement accepted on every biker application."""
    return {
        "key": "biker-liability",
        "version": legal.BIKER_LIABILITY_VERSION,
        "text": legal.BIKER_LIABILITY_TEXT,
    }


@router.get("/privacy", response_model=schemas.LegalTextOut)
def privacy_policy():
    """The plain-English privacy policy (linked from store listings)."""
    return {
        "key": "privacy-policy",
        "version": legal.PRIVACY_POLICY_VERSION,
        "text": legal.PRIVACY_POLICY_TEXT,
    }
