"""App configuration. Dev-safe defaults; override via environment."""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Insecure dev default on purpose — BLOCKED.md requires a real secret before launch.
    secret_key: str = "dev-only-insecure-secret-key-change-before-launch"
    database_url: str = "sqlite:///./rentabiker.db"
    access_token_expire_minutes: int = 60 * 24 * 7  # 7 days
    jwt_algorithm: str = "HS256"
    # Local bike-photo storage (dir is created at startup; absolute path wins).
    upload_dir: str = "uploads"
    # Max bike photo size, bytes.
    max_photo_bytes: int = 5 * 1024 * 1024
    # Free geocoder (OpenStreetMap Nominatim). No account, no key, no spend.
    # Usage policy: <=1 request/second with an identifying User-Agent — the
    # throttle below honors that; the local cache keeps lookups to one per place.
    geocoder_base_url: str = "https://nominatim.openstreetmap.org"
    geocoder_user_agent: str = "RentABikerApp/0.4 (internal pre-launch build)"
    geocoder_timeout_seconds: float = 8.0
    geocoder_min_interval_seconds: float = 1.1

    class Config:
        env_prefix = "RAB_"


settings = Settings()
