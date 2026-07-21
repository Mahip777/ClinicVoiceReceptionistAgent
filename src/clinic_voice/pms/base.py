from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy.orm import Session

from clinic_voice.models import AppointmentType, Branch, Patient, Practitioner


@dataclass(frozen=True)
class PmsSlot:
    starts_at: datetime
    ends_at: datetime
    source_payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PmsAppointment:
    external_id: str
    starts_at: datetime
    ends_at: datetime
    has_conflict: bool = False


class PmsAdapter(ABC):
    @abstractmethod
    def create_patient(self, full_name: str, phone_e164: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def available_times(
        self,
        db: Session,
        branch: Branch,
        practitioner: Practitioner,
        appointment_type: AppointmentType,
        date_from: date,
        date_to: date,
    ) -> list[PmsSlot]:
        raise NotImplementedError

    @abstractmethod
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
        raise NotImplementedError

    @abstractmethod
    def reschedule_appointment(
        self,
        external_id: str,
        starts_at: datetime,
        ends_at: datetime,
        idempotency_key: str,
    ) -> PmsAppointment:
        raise NotImplementedError

    @abstractmethod
    def cancel_appointment(self, external_id: str, reason: str, idempotency_key: str) -> None:
        raise NotImplementedError
