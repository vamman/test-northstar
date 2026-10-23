"""Persistent simulation, transactional outbox, and immutable integration reports."""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, UniqueConstraint, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.orm.attributes import flag_modified


def now():
    return datetime.now(timezone.utc)


DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///./transit-dev.db')
engine = create_engine(DATABASE_URL, pool_pre_ping=True,
    connect_args={'check_same_thread': False, 'timeout': 30} if DATABASE_URL.startswith('sqlite') else {})
Session = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Simulation(Base):
    __tablename__ = 'simulation'
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    run_id: Mapped[str] = mapped_column(String(100), unique=True)
    payload: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    worker_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ArchivedRun(Base):
    __tablename__ = 'archived_runs'
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    archived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Outbox(Base):
    __tablename__ = 'dispatch_outbox'
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(100), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    body_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default='pending', index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    receipt: Mapped[dict | None] = mapped_column(JSON)
    remote_incident_id: Mapped[str | None] = mapped_column(String(120), index=True)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FeedbackPoll(Base):
    """Durable observation schedule; independent of immutable dispatch envelopes."""
    __tablename__ = 'feedback_polls'
    outbox_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    next_poll_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(30), default='active', index=True)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    terminal_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class Report(Base):
    __tablename__ = 'integration_reports'
    __table_args__ = (UniqueConstraint('source', 'external_id'),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    source: Mapped[str] = mapped_column(String(80), index=True)
    external_id: Mapped[str] = mapped_column(String(160))
    incident_id: Mapped[str | None] = mapped_column(String(120), index=True)
    run_id: Mapped[str | None] = mapped_column(String(100), index=True)
    body_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    transit_sim_time: Mapped[str | None] = mapped_column(String(60))


class Connection(Base):
    __tablename__ = 'connections'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    status: Mapped[str] = mapped_column(String(40))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Workplace(Base):
    __tablename__ = 'office_workplaces'
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OfficeWorker(Base):
    __tablename__ = 'office_commuters'
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    workplace_id: Mapped[str] = mapped_column(String(120), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class UISession(Base):
    __tablename__ = 'ui_sessions'
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def init_db():
    from .engine import create_state, ensure_road_traffic
    from .infrastructure import ensure_infrastructure
    import uuid
    Base.metadata.create_all(engine)
    with Session.begin() as session:
        if engine.dialect.name == 'sqlite':
            session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        row = session.execute(select(Simulation).where(Simulation.id == 1).with_for_update()).scalar_one_or_none()
        if row is None:
            state = create_state(seed=int(os.getenv('SIMULATION_SEED', '42')),
                start=os.getenv('SIMULATION_START', '2026-07-18T08:00:00-03:00'))
            state['run_id'] = str(uuid.uuid4())
            session.add(Simulation(id=1, run_id=state['run_id'], payload=state))
        else:
            # Upgrade the current run additively under the same lock as the worker.
            # Archives stay immutable; meters never invent pre-installation usage.
            ensure_road_traffic(row.payload)
            ensure_infrastructure(row.payload)
            flag_modified(row, 'payload')


@contextmanager
def state_transaction():
    with Session.begin() as session:
        # Serialize clock, controls, feedback and incident enqueue against one row.
        if engine.dialect.name == 'sqlite':
            session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        row = session.execute(select(Simulation).where(Simulation.id == 1).with_for_update()).scalar_one()
        yield session, row, row.payload
        flag_modified(row, 'payload')
        row.updated_at = now()
