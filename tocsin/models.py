from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Stable constraint names, so Alembic migrations can refer to (and later drop)
# constraints by name instead of relying on whatever Postgres generated.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB,
    }


# A check's stored state. LATE is never stored: it only depends on the clock
# (past the due time, still inside the grace period), so it is derived when a
# check is read instead of being written by a sweep that nobody acts on.
class State(StrEnum):
    NEW = "new"
    UP = "up"
    LATE = "late"
    DOWN = "down"
    PAUSED = "paused"


class CheckKind(StrEnum):
    JOB = "job"
    EVAL = "eval"


class PingKind(StrEnum):
    START = "start"
    SUCCESS = "success"
    FAIL = "fail"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAIL = "fail"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    RETRYING = "retrying"
    SENT = "sent"
    FAILED = "failed"


class ChannelKind(StrEnum):
    TELEGRAM = "telegram"


check_channels = Table(
    "check_channels",
    Base.metadata,
    Column("check_id", ForeignKey("checks.id", ondelete="CASCADE"), primary_key=True),
    Column("channel_id", ForeignKey("channels.id", ondelete="CASCADE"), primary_key=True),
)


class Check(Base):
    __tablename__ = "checks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # The secret in the ping URL. Kept apart from the id so the id can appear
    # in dashboard URLs and logs without letting anyone who sees it send pings.
    ping_key: Mapped[uuid.UUID] = mapped_column(unique=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(16), default=CheckKind.JOB)
    schedule: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    grace_seconds: Mapped[int] = mapped_column(default=300)
    state: Mapped[str] = mapped_column(String(16), default=State.NEW)
    # When the next ping is due according to the schedule, and that time plus
    # the grace period. The sweep only looks at the second one.
    due_at: Mapped[datetime | None]
    next_expected_at: Mapped[datetime | None] = mapped_column(index=True)
    last_ping_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class Ping(Base):
    __tablename__ = "pings"
    __table_args__ = (Index("ix_pings_check_id_received_at", "check_id", "received_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    check_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("checks.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    received_at: Mapped[datetime]
    exit_code: Mapped[int | None]
    source_ip: Mapped[str | None] = mapped_column(String(64))


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (Index("ix_runs_check_id_id", "check_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    check_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("checks.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    duration_ms: Mapped[int | None]
    exit_code: Mapped[int | None]
    output_tail: Mapped[str | None] = mapped_column(Text)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_check_id_id", "check_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    check_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("checks.id", ondelete="CASCADE"))
    # The run that caused the transition, when there is one, so an alert can
    # show (and later summarise) the output that went with the failure.
    run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("runs.id", ondelete="SET NULL")
    )
    from_state: Mapped[str] = mapped_column(String(16))
    to_state: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(200))
    config: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Delivery(Base):
    __tablename__ = "deliveries"
    # One row per (event, channel) is what makes retries safe: every attempt to
    # deliver an event to a channel lands on the same row, so a duplicate job
    # finds it already sent instead of sending again.
    __table_args__ = (
        UniqueConstraint("event_id", "channel_id"),
        Index(
            "ix_deliveries_due",
            "next_attempt_at",
            postgresql_where=text("status IN ('pending', 'retrying')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("events.id", ondelete="CASCADE"))
    channel_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16), default=DeliveryStatus.PENDING)
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None]
    sent_at: Mapped[datetime | None]
    # The message id the channel gave back (a Telegram message id, say), so the
    # message can be edited later.
    external_id: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    # The first characters of the key, kept so a person can tell keys apart in
    # a list. Only a hash of the full key is stored.
    prefix: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
