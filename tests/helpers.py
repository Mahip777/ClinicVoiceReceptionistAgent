from datetime import UTC, datetime

from sqlalchemy import select

from clinic_voice.config import get_settings
from clinic_voice.models import Patient, PatientPhone
from clinic_voice.pms import get_pms_adapter
from clinic_voice.services.appointments import AppointmentService
from clinic_voice.services.availability import AvailabilityService
from clinic_voice.services.identity import IdentityService

FIXED_NOW = datetime(2026, 12, 10, 7, 30, tzinfo=UTC)  # 13:00 Asia/Kolkata


def service_bundle():
    settings = get_settings()
    pms = get_pms_adapter()
    availability = AvailabilityService(settings, pms, now_fn=lambda: FIXED_NOW)
    appointments = AppointmentService(settings, pms, availability, now_fn=lambda: FIXED_NOW)
    identity = IdentityService(settings, pms)
    return settings, pms, identity, availability, appointments


def patient_by_phone(db, phone: str, name: str | None = None) -> Patient:
    query = select(Patient).join(PatientPhone).where(PatientPhone.phone_e164 == phone)
    if name:
        query = query.where(Patient.full_name == name)
    return db.scalar(query)
