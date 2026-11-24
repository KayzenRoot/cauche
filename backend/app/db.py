"""Database and transaction primitives shared by application processes."""

from datetime import UTC, datetime

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import DATABASE_URL


class Base(DeclarativeBase):
    pass


connect_args = (
    {"check_same_thread": False} if DATABASE_URL.startswith("sqlite")
    else {"options": "-c timezone=UTC"}
)
engine = create_engine(DATABASE_URL, pool_pre_ping=True, hide_parameters=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def utcnow() -> datetime:
    return datetime.now(UTC)


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def lock(session: Session, key: int) -> None:
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
