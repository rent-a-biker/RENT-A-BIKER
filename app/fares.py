"""Fare math — mirrors the live static app's published rates."""

RIDE_PER_MILE_CENTS = 250  # $2.50/mile
EVENT_PER_HOUR_CENTS = 7500  # $75/hour
EVENT_MIN_HOURS = 2

# Commission split (decided Oct 3, 2026): biker keeps 75%, Kenneth/platform 25%.
BIKER_SHARE = 0.75
PLATFORM_SHARE = 0.25


def estimate_ride_fare_cents(miles: float) -> int:
    return int(round(miles * RIDE_PER_MILE_CENTS))


def estimate_event_fare_cents(hours: float) -> int:
    billable = max(hours, EVENT_MIN_HOURS)
    return int(round(billable * EVENT_PER_HOUR_CENTS))


def split_fare_cents(total_cents: int) -> dict:
    biker = int(round(total_cents * BIKER_SHARE))
    return {"biker_cents": biker, "platform_cents": total_cents - biker}
