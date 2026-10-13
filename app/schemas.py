"""Pydantic request/response schemas."""
import datetime

from pydantic import BaseModel, EmailStr, Field, model_validator


# ---------- Users / auth ----------
class UserRegister(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    role: str = Field(default="rider", pattern="^(rider|biker)$")


class UserOut(BaseModel):
    id: int
    email: str
    full_name: str
    phone: str | None
    role: str
    created_at: datetime.datetime

    class Config:
        from_attributes = True


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ---------- Biker profiles ----------
class BikerProfileIn(BaseModel):
    bike_year: int | None = Field(default=None, ge=1950, le=2030)
    bike_make: str = Field(min_length=1, max_length=120)
    bike_model: str = Field(min_length=1, max_length=120)
    city: str = Field(min_length=1, max_length=120)
    state: str = Field(min_length=2, max_length=2)
    service_radius_miles: int = Field(default=50, ge=1, le=500)
    # Optional manual coordinates (both required to count); otherwise the
    # server geocodes city/state at save time.
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    is_available: bool = False
    has_endorsement: bool = False
    pledge_accepted: bool = False
    liability_accepted: bool = False
    # Motorcycle insurance (Kenneth's rule, reinstated Oct 7, 2026): the app
    # collects insurance info; the policy must cover the passenger, otherwise
    # the biker must sign the passenger-responsibility agreement (checkbox +
    # typed name + date signature, same pattern as the rider waiver).
    has_insurance: bool = False
    insurance_covers_passenger: bool = False
    passenger_responsibility_accepted: bool = False
    passenger_responsibility_name: str | None = Field(
        default=None, max_length=255
    )
    passenger_responsibility_date: str | None = Field(
        default=None, max_length=32
    )
    insurance_company: str | None = Field(default=None, max_length=120)
    insurance_policy_number: str | None = Field(default=None, max_length=64)
    insurance_expiration_date: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def _insurance_rule(self) -> "BikerProfileIn":
        if self.has_insurance and not self.insurance_covers_passenger:
            missing = []
            if not self.passenger_responsibility_accepted:
                missing.append("the responsibility agreement must be accepted")
            if not (self.passenger_responsibility_name or "").strip():
                missing.append("the agreement must carry a typed name signature")
            if not (self.passenger_responsibility_date or "").strip():
                missing.append("the agreement must carry a date")
            if missing:
                raise ValueError(
                    "If the insurance policy does not cover passengers, "
                    + "; ".join(missing)
                    + "."
                )
        return self


class BikerProfileOut(BikerProfileIn):
    id: int
    user_id: int
    approval_status: str
    photo_url: str | None = None
    # Legal text versions stamped at save (M6) — reflect current acceptance state
    pledge_version: str | None = None
    liability_version: str | None = None
    # True when created by the M8 legacy CSV import (still needs the in-app
    # agreements completed before it can be approved for dispatch)
    imported: bool = False

    class Config:
        from_attributes = True


class AvailabilityIn(BaseModel):
    """Flip only the availability switch — no other profile fields touched."""

    is_available: bool


class BikerUserBrief(BaseModel):
    """The biker account behind a profile (admin sees this; riders do not)."""

    email: str
    full_name: str
    phone: str | None

    class Config:
        from_attributes = True


class BikerAdminOut(BikerProfileOut):
    """Full biker application as Kenneth sees it in the review queue."""

    user: BikerUserBrief
    reviewed_at: datetime.datetime | None
    reviewer_id: int | None
    rejection_reason: str | None


class BikerReviewIn(BaseModel):
    """Body for approve/reject. Rejection can carry a reason for the record."""

    reason: str | None = Field(default=None, max_length=500)


class BikerPublicOut(BaseModel):
    """What riders/dispatch can see about an available biker (no contact info)."""

    id: int
    bike_year: int | None
    bike_make: str
    bike_model: str
    city: str
    state: str
    service_radius_miles: int
    is_available: bool
    photo_url: str | None = None
    # Populated only when /bikers/available is called with near_lat/near_lng.
    distance_miles: float | None = None

    class Config:
        from_attributes = True


# ---------- Ride requests ----------
class RideRequestIn(BaseModel):
    ride_type: str = Field(default="ride", pattern="^(ride|event)$")
    pickup_address: str = Field(min_length=1)
    dropoff_address: str | None = None
    scheduled_at: datetime.datetime | None = None
    estimated_miles: float | None = Field(default=None, ge=0)
    duration_hours: float | None = Field(default=None, ge=0)
    notes: str | None = None
    # Rider wants the biker to bring/provide a helmet (riders asked for this)
    helmet_needed: bool = False
    # Liability waiver — required before booking (same rule as the static app)
    waiver_signed: bool = False
    waiver_name: str | None = None
    waiver_date: str | None = None


class RideEstimateIn(BaseModel):
    """Fare preview input — same math as booking, no waiver, no booking."""

    ride_type: str = Field(default="ride", pattern="^(ride|event)$")
    estimated_miles: float | None = Field(default=None, ge=0)
    duration_hours: float | None = Field(default=None, ge=0)


class RideEstimateOut(BaseModel):
    fare_estimate_cents: int


class AssignedBikerBrief(BaseModel):
    """Rider-facing brief of the biker assigned to a ride (matched rides).

    Deliberately carries no contact info and no insurance data — those stay
    in the admin/dispatch views. Built by app/ride_out.py.
    """

    user_id: int
    full_name: str
    bike_year: int | None
    bike_make: str
    bike_model: str
    city: str
    state: str
    photo_url: str | None = None


class RideRequestOut(BaseModel):
    id: int
    rider_id: int
    ride_type: str
    pickup_address: str
    dropoff_address: str | None
    scheduled_at: datetime.datetime | None
    estimated_miles: float | None
    duration_hours: float | None
    fare_estimate_cents: int
    status: str
    biker_id: int | None
    waiver_signed: bool
    # Version of the waiver text the rider signed (M6 — stamped at booking)
    waiver_version: str | None = None
    created_at: datetime.datetime
    helmet_needed: bool = False
    pickup_latitude: float | None = None
    pickup_longitude: float | None = None
    # Plain-English status line for ride cards (M3 polish) — computed
    # server-side by app/ride_out.py so every client shows the same words.
    status_detail: str = ""
    # The assigned biker's rider-facing brief (matched rides). No contact
    # info, no insurance — those stay in the admin/dispatch views.
    assigned_biker: AssignedBikerBrief | None = None

    class Config:
        from_attributes = True


# ---------- In-app messaging (M5) ----------
class MessageIn(BaseModel):
    """Post one message to a ride's thread."""

    body: str = Field(min_length=1, max_length=2000)


class MessageOut(BaseModel):
    """A message with its sender's name/role so the thread reads naturally."""

    id: int
    ride_id: int
    sender_id: int
    sender_name: str
    sender_role: str
    body: str
    created_at: datetime.datetime

    class Config:
        from_attributes = True


# ---------- Legal texts (M6) ----------
class LegalTextOut(BaseModel):
    """One versioned legal text — served publicly, stamped into signed records."""

    key: str
    version: str
    text: str


# ---------- Dispatch (Kenneth's dashboard, admin only) ----------
class UserBrief(BaseModel):
    """Contact brief for a rider or biker — admin/dispatch views only."""

    email: str
    full_name: str
    phone: str | None

    class Config:
        from_attributes = True


class RideAssignIn(BaseModel):
    """Assign a biker (by user id) to a ride. Kenneth dispatches manually."""

    biker_user_id: int


class DispatchCandidateOut(BaseModel):
    """One approved + available biker ranked for a pending ride.

    Contact info and insurance status are visible here so Kenneth can call
    the biker and see the insurance picture at dispatch time. Riders never
    see this shape — their public biker cards carry no contact or insurance
    info.
    """

    profile_id: int
    user_id: int
    full_name: str
    email: str
    phone: str | None
    bike_year: int | None
    bike_make: str
    bike_model: str
    city: str
    state: str
    service_radius_miles: int
    photo_url: str | None
    is_available: bool
    has_insurance: bool
    insurance_covers_passenger: bool
    passenger_responsibility_accepted: bool
    passenger_responsibility_name: str | None = None
    passenger_responsibility_date: str | None = None
    insurance_company: str | None
    insurance_expiration_date: str | None = None
    distance_miles: float | None = None


class RideDetailOut(RideRequestOut):
    """A ride with its rider + assigned-biker contact briefs (admin view)."""

    rider: UserBrief
    biker: UserBrief | None = None


class AutoMatchOut(BaseModel):
    """Result of an auto-match: the matched ride plus the biker it picked.

    The biker is the nearest approved + available biker within range;
    on an exact distance tie, the passenger-covered policy wins.
    """

    ride: RideDetailOut
    assigned_candidate: DispatchCandidateOut


class DispatchRideOut(RideDetailOut):
    """A pending ride as it appears on the dispatch board: the ride, the
    rider's contact info, and the ranked biker candidates for that ride."""

    candidates: list[DispatchCandidateOut] = []


class DispatchOverviewOut(BaseModel):
    """The whole dispatch board in one call: pending rides + their bikers."""

    requests: list[DispatchRideOut]


# ---------- Payouts (M7 part 1: manual PayPal record-keeping) ----------
class PayoutRecordOut(BaseModel):
    """One completed ride's recorded 75/25 split, as Kenneth sees it in the
    payout queue. Name briefs make it readable; riders never see this shape."""

    id: int
    ride_id: int
    fare_cents: int
    biker_share_cents: int
    platform_share_cents: int
    status: str
    paid_reference: str | None = None
    paid_at: datetime.datetime | None = None
    created_at: datetime.datetime
    biker: UserBrief
    rider: UserBrief


class PayoutMarkPaidIn(BaseModel):
    """Body for marking a payout paid after the manual PayPal transfer.
    The reference (e.g. the PayPal transaction id) is optional record-keeping."""

    reference: str | None = Field(default=None, max_length=255)


class CompleteRideOut(BaseModel):
    """Result of completing a ride: the ride (now 'completed') plus the
    payout record created for it."""

    ride: RideRequestOut
    payout: PayoutRecordOut


# ---------- Payout summary (M7 part 2: Kenneth's at-a-glance ledger) ----------
class PayoutBucket(BaseModel):
    """Totals for one side of the payout queue (pending or paid)."""

    count: int
    fare_cents: int
    biker_cents: int
    platform_cents: int


class PayoutBikerSummary(BaseModel):
    """What one biker is owed (pending) or has already been paid."""

    biker_id: int
    biker_name: str
    pending_count: int
    pending_biker_cents: int
    pending_platform_cents: int
    paid_count: int
    paid_biker_cents: int


class PayoutSummaryOut(BaseModel):
    """Kenneth's payout ledger at a glance: pending vs paid totals plus a
    per-biker breakdown, so he can see exactly who is owed what before
    sending the manual transfers."""

    pending: PayoutBucket
    paid: PayoutBucket
    by_biker: list[PayoutBikerSummary]


# ---------- Biker earnings (M7 part 3: the biker sees their own cut) ----------
class BikerPayoutOut(BaseModel):
    """One recorded payout in the biker's own earnings view.

    Deliberately omits the name briefs Kenneth's queue carries — the
    biker's view is about their own money, not rider contact data.
    """

    id: int
    ride_id: int
    fare_cents: int
    biker_share_cents: int
    status: str
    paid_at: datetime.datetime | None = None
    created_at: datetime.datetime


class BikerEarningsOut(BaseModel):
    """The signed-in user's earnings as a biker: summary totals plus their
    own payout records, newest first."""

    pending_owed_cents: int
    pending_ride_count: int
    paid_received_cents: int
    paid_ride_count: int
    lifetime_biker_cents: int
    payouts: list[BikerPayoutOut]


# ---------- Legacy import (M8: static-app-era signup lists into accounts) ----------
class LegacyImportErrorRow(BaseModel):
    """One CSV row that could not be imported."""

    row: int
    email: str | None = None
    message: str


class LegacyImportCredential(BaseModel):
    """The one-time sign-in credentials for an imported account.

    The temporary password is shown only here — it is not stored anywhere
    and cannot be recovered later. Kenneth distributes these to the bikers
    and riders; they sign in and set their own flow from there.
    """

    row: int
    email: str
    temp_password: str


class LegacyImportResultOut(BaseModel):
    """The result of one legacy CSV import."""

    kind: str  # riders | bikers
    rows_total: int
    created: int
    skipped_existing: int
    errors: list[LegacyImportErrorRow]
    credentials: list[LegacyImportCredential]
