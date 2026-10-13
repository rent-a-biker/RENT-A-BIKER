"""Geocoding + distance math for dispatch matching (no paid APIs).

City/state geocoding uses the free OpenStreetMap Nominatim service, with a
local ``geocode_cache`` table so each place is looked up at most once, and a
configurable throttle so we stay inside Nominatim's usage policy (<=1
request/second, identifying User-Agent). Stdlib HTTP only — no new deps.

If a lookup fails for any reason, callers keep working: coordinates just stay
empty and the biker is skipped by distance-filtered dispatch queries.
"""
import json
import math
import threading
import time
import urllib.parse
import urllib.request

from .config import settings

EARTH_RADIUS_MILES = 3958.8

_LAST_REQUEST_AT = 0.0
_THROTTLE_LOCK = threading.Lock()


def haversine_miles(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Great-circle distance between two points, in miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def _throttled_fetch(url: str) -> str | None:
    global _LAST_REQUEST_AT
    with _THROTTLE_LOCK:
        wait = settings.geocoder_min_interval_seconds - (
            time.monotonic() - _LAST_REQUEST_AT
        )
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(
            url, headers={"User-Agent": settings.geocoder_user_agent}
        )
        try:
            with urllib.request.urlopen(
                req, timeout=settings.geocoder_timeout_seconds
            ) as resp:
                body = resp.read().decode("utf-8", "replace")
            _LAST_REQUEST_AT = time.monotonic()
            return body
        except Exception:
            return None


def geocode_address(address: str) -> tuple[float, float] | None:
    """Return (lat, lng) for a free-text US address, or None on any failure.

    Used for ride pickup addresses so dispatch can rank nearby bikers. Same
    free Nominatim service, same throttle, same graceful-failure contract as
    ``geocode_city_state`` — callers cache the result and never block on it.
    """
    address = (address or "").strip()
    if not address:
        return None
    q = urllib.parse.quote(address)
    url = (
        f"{settings.geocoder_base_url.rstrip('/')}"
        f"/search?q={q}&format=json&limit=1&countrycodes=us"
    )
    body = _throttled_fetch(url)
    if not body:
        return None
    try:
        results = json.loads(body)
        if not results:
            return None
        return float(results[0]["lat"]), float(results[0]["lon"])
    except (ValueError, KeyError, TypeError, IndexError):
        return None


def geocode_city_state(city: str, state: str) -> tuple[float, float] | None:
    """Return (lat, lng) for a US city/state, or None on any failure."""
    city = (city or "").strip()
    state = (state or "").strip().upper()
    if not city or not state:
        return None
    q = urllib.parse.quote(f"{city}, {state}, USA")
    url = (
        f"{settings.geocoder_base_url.rstrip('/')}"
        f"/search?q={q}&format=json&limit=1"
    )
    body = _throttled_fetch(url)
    if not body:
        return None
    try:
        results = json.loads(body)
        if not results:
            return None
        return float(results[0]["lat"]), float(results[0]["lon"])
    except (ValueError, KeyError, TypeError, IndexError):
        return None
