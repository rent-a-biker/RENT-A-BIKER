"""Database engine + session."""
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings


def normalize_database_url(url: str) -> str:
    """Normalize a database URL into a SQLAlchemy URL.

    Hosted providers (Render, Heroku-style) hand out `postgres://...` or bare
    `postgresql://...`. This app talks to Postgres through psycopg, so both
    become `postgresql+psycopg://...`. SQLite URLs pass through untouched.
    """
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


# RAB_DATABASE_URL (Render) is mapped here; dev keeps SQLite.
DATABASE_URL = normalize_database_url(settings.database_url)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
