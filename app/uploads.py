"""Bike-photo upload helpers: validation + local storage.

Photos are stored under <upload_dir>/ with server-generated UUID filenames,
so client-supplied filenames can never reach the filesystem (no traversal).
Served publicly at /uploads/<filename> via the StaticFiles mount in main.
"""
import os
import uuid

from .config import settings

# content-type -> (extension, magic-bytes check)
_SUPPORTED: dict[str, tuple[str, callable]] = {
    "image/jpeg": ("jpg", lambda d: d[:3] == b"\xff\xd8\xff"),
    "image/png": ("png", lambda d: d[:8] == b"\x89PNG\r\n\x1a\n"),
    "image/webp": ("webp", lambda d: d[:4] == b"RIFF" and d[8:12] == b"WEBP"),
}


def upload_root() -> str:
    """Absolute upload dir; created on demand (server + tests create it)."""
    root = os.path.abspath(os.path.expanduser(settings.upload_dir))
    os.makedirs(root, exist_ok=True)
    return root


def supported_types() -> list[str]:
    return sorted(_SUPPORTED)


def validate_photo(content_type: str | None, data: bytes) -> tuple[bool, str]:
    """Return (ok, error). Checks type, magic bytes, size."""
    if content_type not in _SUPPORTED:
        return False, (
            f"Photo must be JPEG, PNG, or WebP (got {content_type or 'unknown'})"
        )
    ext, magic = _SUPPORTED[content_type]
    if not data:
        return False, "Empty file"
    if len(data) > settings.max_photo_bytes:
        return False, (
            f"Photo is too large ({len(data)} bytes); "
            f"max {settings.max_photo_bytes} bytes"
        )
    if not magic(data):
        return False, "File contents do not look like a valid image"
    return True, ext


def save_photo(data: bytes, ext: str) -> str:
    """Write bytes to a UUID filename. Returns the filename."""
    filename = f"{uuid.uuid4().hex}.{ext}"
    with open(os.path.join(upload_root(), filename), "wb") as f:
        f.write(data)
    return filename


def delete_photo(filename: str | None) -> None:
    """Remove a stored photo; never raises, never leaves upload_root."""
    if not filename or "/" in filename or "\\" in filename:
        return
    try:
        os.remove(os.path.join(upload_root(), filename))
    except FileNotFoundError:
        pass
