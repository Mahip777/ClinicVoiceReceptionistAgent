from __future__ import annotations

import uuid
from datetime import UTC, datetime, time

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Branch(TimestampMixin, Base):
    __tablename__ = "branches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    external_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Practitioner(TimestampMixin, Base):
    __tablename__ = "practitioners"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(255))
    spoken_name: Mapped[str] = mapped_column(String(255))
    specialty: Mapped[str] = mapped_column(String(100), index=True)
    external_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class PractitionerBranch(Base):
    __tablename__ = "practitioner_branches"
    __table_args__ = (UniqueConstraint("practitioner_id", "branch_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    practitioner_id: Mapped[str] = mapped_column(ForeignKey("practitioners.id", ondelete="CASCADE"))
    branch_id: Mapped[str] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"))

    practitioner: Mapped[Practitioner] = relationship()
    branch: Mapped[Branch] = relationship()


class AppointmentType(TimestampMixin, Base):
    __tablename__ = "appointment_types"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(255))
    specialty: Mapped[str] = mapped_column(String(100), index=True)
    patient_duration_minutes: Mapped[int] = mapped_column(Integer)
    calendar_duration_minutes: Mapped[int] = mapped_column(Integer)
    price_minor: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    external_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class PractitionerAppointmentType(Base):
    __tablename__ = "practitioner_appointment_types"
    __table_args__ = (UniqueConstraint("practitioner_id", "appointment_type_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    practitioner_id: Mapped[str] = mapped_column(ForeignKey("practitioners.id", ondelete="CASCADE"))
    appointment_type_id: Mapped[str] = mapped_column(
        ForeignKey("appointment_types.id", ondelete="CASCADE")
    )


class AvailabilityRule(TimestampMixin, Base):
    __tablename__ = "availability_rules"
    __table_args__ = (Index("ix_availability_lookup", "practitioner_id", "branch_id", "weekday"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    practitioner_id: Mapped[str] = mapped_column(ForeignKey("practitioners.id", ondelete="CASCADE"))
    branch_id: Mapped[str] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"))
    weekday: Mapped[int] = mapped_column(Integer)  # Monday=0
    starts_at_local: Mapped[time] = mapped_column(Time)
    ends_at_local: Mapped[time] = mapped_column(Time)
    slot_interval_minutes: Mapped[int] = mapped_column(Integer, default=15)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class AvailabilityOverride(TimestampMixin, Base):
    __tablename__ = "availability_overrides"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    practitioner_id: Mapped[str] = mapped_column(ForeignKey("practitioners.id", ondelete="CASCADE"))
    branch_id: Mapped[str] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"))
    local_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    starts_at_local: Mapped[time] = mapped_column(Time)
    ends_at_local: Mapped[time] = mapped_column(Time)
    slot_interval_minutes: Mapped[int] = mapped_column(Integer, default=15)
    available: Mapped[bool] = mapped_column(Boolean, default=True)


class Patient(TimestampMixin, Base):
    __tablename__ = "patients"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    full_name: Mapped[str] = mapped_column(String(255), index=True)
    spoken_name: Mapped[str] = mapped_column(String(255))
    external_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    preferred_branch_id: Mapped[str | None] = mapped_column(ForeignKey("branches.id"))
    preferred_practitioner_id: Mapped[str | None] = mapped_column(ForeignKey("practitioners.id"))
    has_prior_pms_history: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    phones: Mapped[list[PatientPhone]] = relationship(
        back_populates="patient", cascade="all, delete-orphan"
    )


class PatientPhone(Base):
    __tablename__ = "patient_phones"
    __table_args__ = (UniqueConstraint("patient_id", "phone_e164"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"))
    phone_e164: Mapped[str] = mapped_column(String(32), index=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True)

    patient: Mapped[Patient] = relationship(back_populates="phones")


class AvailabilitySearch(TimestampMixin, Base):
    __tablename__ = "availability_searches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    call_id: Mapped[str | None] = mapped_column(String(100), index=True)
    constraints: Mapped[dict] = mapped_column(JSON)
    fresh_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OfferedSlot(Base):
    __tablename__ = "offered_slots"
    __table_args__ = (Index("ix_offered_slot_time", "practitioner_id", "starts_at", "ends_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    search_id: Mapped[str] = mapped_column(
        ForeignKey("availability_searches.id", ondelete="CASCADE")
    )
    branch_id: Mapped[str] = mapped_column(ForeignKey("branches.id"))
    practitioner_id: Mapped[str] = mapped_column(ForeignKey("practitioners.id"))
    appointment_type_id: Mapped[str] = mapped_column(ForeignKey("appointment_types.id"))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_payload: Mapped[dict] = mapped_column(JSON, default=dict)


class Appointment(TimestampMixin, Base):
    __tablename__ = "appointments"
    __table_args__ = (
        Index("ix_appointment_overlap", "practitioner_id", "starts_at", "ends_at", "status"),
        UniqueConstraint("idempotency_key", name="uq_appointment_idempotency"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id"), index=True)
    branch_id: Mapped[str] = mapped_column(ForeignKey("branches.id"), index=True)
    practitioner_id: Mapped[str] = mapped_column(ForeignKey("practitioners.id"), index=True)
    appointment_type_id: Mapped[str] = mapped_column(ForeignKey("appointment_types.id"))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30), default="pending_sync", index=True)
    external_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(255))
    pms_sync_status: Mapped[str] = mapped_column(String(30), default="pending")
    cancellation_reason: Mapped[str | None] = mapped_column(Text)


class SlotReservation(TimestampMixin, Base):
    __tablename__ = "slot_reservations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    slot_key: Mapped[str] = mapped_column(String(255), unique=True)
    offered_slot_id: Mapped[str] = mapped_column(ForeignKey("offered_slots.id"))
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(String(30), default="held")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CallSession(TimestampMixin, Base):
    __tablename__ = "call_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    call_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    phone_e164: Mapped[str] = mapped_column(String(32), index=True)
    patient_id: Mapped[str | None] = mapped_column(ForeignKey("patients.id"))
    direction: Mapped[str] = mapped_column(String(20), default="inbound")
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    intent: Mapped[str | None] = mapped_column(String(50))
    checkpoint: Mapped[dict] = mapped_column(JSON, default=dict)
    ended_reason: Mapped[str | None] = mapped_column(String(100))
    transcript: Mapped[str | None] = mapped_column(Text)


class OutboundIntent(TimestampMixin, Base):
    __tablename__ = "outbound_intents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    phone_e164: Mapped[str] = mapped_column(String(32), index=True)
    patient_id: Mapped[str | None] = mapped_column(ForeignKey("patients.id"))
    purpose: Mapped[str] = mapped_column(String(100))
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="awaiting_callback", index=True)


class FollowupRequest(TimestampMixin, Base):
    __tablename__ = "followup_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    call_id: Mapped[str | None] = mapped_column(String(100), index=True)
    patient_id: Mapped[str | None] = mapped_column(ForeignKey("patients.id"))
    phone_e164: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(String(100))
    details: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(String(20), default="normal")
    status: Mapped[str] = mapped_column(String(30), default="open")


class IdempotencyRecord(TimestampMixin, Base):
    __tablename__ = "idempotency_records"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    operation: Mapped[str] = mapped_column(String(50))
    request_hash: Mapped[str] = mapped_column(String(64))
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict | None] = mapped_column(JSON)


class PmsOutbox(TimestampMixin, Base):
    __tablename__ = "pms_outbox"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    operation: Mapped[str] = mapped_column(String(50))
    aggregate_id: Mapped[str] = mapped_column(String(36), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ToolAuditLog(Base):
    __tablename__ = "tool_audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    call_id: Mapped[str | None] = mapped_column(String(100), index=True)
    tool_name: Mapped[str] = mapped_column(String(100), index=True)
    request_body: Mapped[dict] = mapped_column(JSON)
    response_code: Mapped[str] = mapped_column(String(50))
    duration_ms: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
