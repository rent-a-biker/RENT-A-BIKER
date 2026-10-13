"""Database models."""
import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


class User(Base):
    """Account holder. role is 'rider', 'biker', or 'admin' (Kenneth)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    role: Mapped[str] = mapped_column(String(16), default="rider")  # rider | biker | admin
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)

    # Legacy import (M8): static-app-era signups that were pulled into the
    # full app from a CSV. home_city/home_state preserve the original
    # signup's location; imported_at records when it happened. Imported
    # accounts get a random temporary password (delivered once in the
    # import result) and sign in normally.
    home_city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    home_state: Mapped[str | None] = mapped_column(String(2), nullable=True)
    imported_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime, nullable=True
    )

    biker_profile: Mapped["BikerProfile | None"] = relationship(
        "BikerProfile",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
        foreign_keys="BikerProfile.user_id",
    )
    ride_requests: Mapped[list["RideRequest"]] = relationship(
        "RideRequest", back_populates="rider", foreign_keys="RideRequest.rider_id"
    )


class BikerProfile(Base):
    """A biker's bike, service area, and availability. One per biker user."""

    __tablename__ = "biker_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)

    bike_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bike_make: Mapped[str] = mapped_column(String(120))
    bike_model: Mapped[str] = mapped_column(String(120))

    city: Mapped[str] = mapped_column(String(120))
    state: Mapped[str] = mapped_column(String(2))
    service_radius_miles: Mapped[int] = mapped_column(Integer, default=50)

    # Geocoded coordinates for the biker's city/state (filled at profile save;
    # manual lat/lng entry also accepted). Nullable — geocoding can fail.
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)

    is_available: Mapped[bool] = mapped_column(Boolean, default=False)
    has_endorsement: Mapped[bool] = mapped_column(Boolean, default=False)
    pledge_accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    liability_accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    # Versions of the server-side pledge / liability text the biker accepted
    # (M6). Cleared when the acceptance is unchecked on a re-save.
    pledge_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    liability_version: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Motorcycle insurance (Kenneth's rule, reinstated Oct 7, 2026): the app
    # collects insurance info; the policy must cover the passenger, otherwise
    # the biker signs an agreement accepting personal responsibility for the
    # passenger (passenger_responsibility_accepted) with a typed name + date
    # signature (passenger_responsibility_name/date).
    has_insurance: Mapped[bool] = mapped_column(Boolean, default=False)
    insurance_covers_passenger: Mapped[bool] = mapped_column(Boolean, default=False)
    passenger_responsibility_accepted: Mapped[bool] = mapped_column(
        Boolean, default=False
    )
    passenger_responsibility_name: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    passenger_responsibility_date: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    insurance_company: Mapped[str | None] = mapped_column(String(120), nullable=True)
    insurance_policy_number: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    insurance_expiration_date: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )

    # Bike photo, stored locally; filename is server-generated (never a client name).
    photo_filename: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Legacy import (M8): True when this profile was created by the
    # static-app-era CSV import rather than the biker filling the form
    # in-app. Imported profiles always land with approval_status="pending":
    # endorsement, pledge, liability, and insurance still have to be
    # completed/signed in-app before they can be approved for dispatch.
    imported: Mapped[bool] = mapped_column(Boolean, default=False)

    @property
    def photo_url(self) -> str | None:
        return f"/uploads/{self.photo_filename}" if self.photo_filename else None

    # Kenneth's review queue: a profile is visible to dispatch only once approved.
    # pending | approved | rejected
    approval_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    reviewed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )

    user: Mapped[User] = relationship(
        "User",
        back_populates="biker_profile",
        foreign_keys=[user_id],
    )
    reviewer: Mapped["User | None"] = relationship("User", foreign_keys=[reviewer_id])


class GeocodeCache(Base):
    """Caches city/state -> lat/lng lookups so the free geocoder is hit at most
    once per place (and a profile re-save after a city change re-resolves)."""

    __tablename__ = "geocode_cache"
    __table_args__ = (UniqueConstraint("city", "state"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    city: Mapped[str] = mapped_column(String(120), index=True)
    state: Mapped[str] = mapped_column(String(2), index=True)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    fetched_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)


class AddressGeocodeCache(Base):
    """Caches free-text address -> lat/lng for pickup geocoding, so each
    address is looked up at most once. The address key is normalized
    (whitespace-collapsed, lowercased)."""

    __tablename__ = "address_geocode_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    address: Mapped[str] = mapped_column(Text, unique=True, index=True)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    fetched_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)


class RideRequest(Base):
    """A rider's ride or event request."""

    __tablename__ = "ride_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rider_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)

    ride_type: Mapped[str] = mapped_column(String(16), default="ride")  # ride | event
    pickup_address: Mapped[str] = mapped_column(Text)
    dropoff_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    scheduled_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)

    # Geocoded pickup location for dispatch distance matching. Filled at
    # booking time via the free geocoder (cached); a failed lookup just leaves
    # them empty and the ride is still dispatchable without distances.
    pickup_latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    pickup_longitude: Mapped[float | None] = mapped_column(Float, nullable=True)

    estimated_miles: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    fare_estimate_cents: Mapped[int] = mapped_column(Integer, default=0)

    # pending | matched | en_route | in_progress | completed | cancelled
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    biker_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    # Liability waiver (required before booking — carried over from the static app)
    waiver_signed: Mapped[bool] = mapped_column(Boolean, default=False)
    waiver_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    waiver_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Version of the server-side waiver text the rider signed (M6). The text
    # itself lives in app/legal.py; the record points at the agreed wording.
    waiver_version: Mapped[str | None] = mapped_column(String(16), nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Rider asked the biker to bring/provide a helmet (M3)
    helmet_needed: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )

    rider: Mapped[User] = relationship(
        "User", back_populates="ride_requests", foreign_keys=[rider_id]
    )


class Message(Base):
    """One chat message on a ride's thread.

    Only the ride's rider, its assigned biker, and Kenneth (admin) can read
    or post — enforced in the endpoints, not here.
    """

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ride_id: Mapped[int] = mapped_column(
        ForeignKey("ride_requests.id"), index=True
    )
    sender_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)

    sender: Mapped[User] = relationship("User", foreign_keys=[sender_id])


class PayoutRecord(Base):
    """Ledger row for a completed ride's 75/25 split (M7, part 1).

    Manual PayPal payouts continue — this table is what Kenneth works from:
    who is owed what, and who has been paid. Created exactly once when a
    matched ride is marked completed (75% to the biker, 25% to Kenneth);
    Kenneth marks it paid after the PayPal transfer, with an optional
    transaction reference for the record.
    """

    __tablename__ = "payout_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ride_id: Mapped[int] = mapped_column(
        ForeignKey("ride_requests.id"), unique=True, index=True
    )
    biker_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    rider_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    fare_cents: Mapped[int] = mapped_column(Integer)
    biker_share_cents: Mapped[int] = mapped_column(Integer)
    platform_share_cents: Mapped[int] = mapped_column(Integer)

    # pending | paid
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # Optional reference for the manual payout (e.g. the PayPal transaction id)
    paid_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    paid_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )

    biker: Mapped[User] = relationship("User", foreign_keys=[biker_user_id])
    rider: Mapped[User] = relationship("User", foreign_keys=[rider_user_id])
