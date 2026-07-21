from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from clinic_voice.config import Settings
from clinic_voice.errors import PmsError
from clinic_voice.models import (
    Appointment,
    AppointmentType,
    AvailabilityOverride,
    AvailabilityRule,
    Branch,
    Patient,
    Practitioner,
)

from .base import PmsAdapter, PmsAppointment, PmsSlot


class MockPmsAdapter(PmsAdapter):
    """Deterministic PMS used for local development and clean-clone evaluation."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.fail_next_operation: str | None = None

    def available_times(
        self,
        db: Session,
        branch: Branch,
        practitioner: Practitioner,
        appointment_type: AppointmentType,
        date_from: date,
        date_to: date,
    ) -> list[PmsSlot]:
        rules = db.scalars(
            select(AvailabilityRule).where(
                AvailabilityRule.branch_id == branch.id,
                AvailabilityRule.practitioner_id == practitioner.id,
                AvailabilityRule.active.is_(True),
            )
        ).all()
        overrides = db.scalars(
            select(AvailabilityOverride).where(
                AvailabilityOverride.branch_id == branch.id,
                AvailabilityOverride.practitioner_id == practitioner.id,
                AvailabilityOverride.available.is_(True),
            )
        ).all()
        busy = db.scalars(
            select(Appointment).where(
                Appointment.practitioner_id == practitioner.id,
                Appointment.status.in_(["pending_sync", "confirmed"]),
            )
        ).all()
        result: list[PmsSlot] = []
        current = date_from
        while current <= date_to:
            day_rules = [rule for rule in rules if current.weekday() == rule.weekday]
            day_rules.extend(
                override
                for override in overrides
                if self._aware(override.local_date).astimezone(self.settings.timezone).date()
                == current
            )
            for rule in day_rules:
                local_start = datetime.combine(
                    current, rule.starts_at_local, self.settings.timezone
                )
                local_end = datetime.combine(current, rule.ends_at_local, self.settings.timezone)
                cursor = local_start
                duration = timedelta(minutes=appointment_type.calendar_duration_minutes)
                interval = timedelta(minutes=rule.slot_interval_minutes)
                while cursor + duration <= local_end:
                    starts_utc = cursor.astimezone(UTC)
                    ends_utc = (cursor + duration).astimezone(UTC)
                    overlaps = any(
                        self._aware(item.starts_at) < ends_utc
                        and self._aware(item.ends_at) > starts_utc
                        for item in busy
                    )
                    if not overlaps:
                        result.append(
                            PmsSlot(
                                starts_at=starts_utc,
                                ends_at=ends_utc,
                                source_payload={"provider": "mock"},
                            )
                        )
                    cursor += interval
            current += timedelta(days=1)
        return result

    def create_patient(self, full_name: str, phone_e164: str) -> str:
        self._maybe_fail("create_patient")
        return f"mock-patient-{uuid.uuid4()}"

    def create_appointment(
        self,
        patient: Patient,
        branch: Branch,
        practitioner: Practitioner,
        appointment_type: AppointmentType,
        starts_at: datetime,
        ends_at: datetime,
        idempotency_key: str,
    ) -> PmsAppointment:
        self._maybe_fail("create")
        return PmsAppointment(f"mock-{uuid.uuid4()}", starts_at, ends_at, False)

    def reschedule_appointment(
        self,
        external_id: str,
        starts_at: datetime,
        ends_at: datetime,
        idempotency_key: str,
    ) -> PmsAppointment:
        self._maybe_fail("reschedule")
        return PmsAppointment(external_id, starts_at, ends_at, False)

    def cancel_appointment(self, external_id: str, reason: str, idempotency_key: str) -> None:
        self._maybe_fail("cancel")

    def _maybe_fail(self, operation: str) -> None:
        if self.fail_next_operation == operation:
            self.fail_next_operation = None
            raise PmsError("PMS_TEMPORARILY_UNAVAILABLE", f"Injected {operation} failure")

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=UTC)
