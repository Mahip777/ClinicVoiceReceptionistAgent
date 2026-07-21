from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, selectinload

from clinic_voice.config import Settings
from clinic_voice.errors import DomainError, PmsError
from clinic_voice.models import (
    Appointment,
    CallSession,
    OutboundIntent,
    Patient,
    PatientPhone,
)
from clinic_voice.pms import PmsAdapter
from clinic_voice.schemas import (
    CallerContextResponse,
    IdentifyPatientResponse,
    PatientCandidate,
)
from clinic_voice.security import normalize_name, normalize_phone


class IdentityService:
    def __init__(self, settings: Settings, pms: PmsAdapter) -> None:
        self.settings = settings
        self.pms = pms

    def caller_context(self, db: Session, phone_e164: str) -> CallerContextResponse:
        phone = normalize_phone(phone_e164)
        patients = db.scalars(
            select(Patient)
            .join(PatientPhone)
            .where(PatientPhone.phone_e164 == phone, Patient.active.is_(True))
            .options(selectinload(Patient.phones))
        ).all()
        candidates = [
            PatientCandidate(
                patient_id=patient.id,
                display_name=patient.spoken_name,
                has_prior_appointments=bool(
                    db.scalar(select(exists().where(Appointment.patient_id == patient.id)))
                )
                or patient.has_prior_pms_history,
            )
            for patient in patients
        ]

        cutoff = datetime.now(UTC) - timedelta(minutes=self.settings.session_resume_minutes)
        resume = db.scalars(
            select(CallSession)
            .where(
                CallSession.phone_e164 == phone,
                CallSession.status.in_(["active", "disconnected"]),
                CallSession.updated_at >= cutoff,
            )
            .order_by(CallSession.updated_at.desc())
        ).first()
        callback = db.scalars(
            select(OutboundIntent)
            .where(
                OutboundIntent.phone_e164 == phone,
                OutboundIntent.status == "awaiting_callback",
            )
            .order_by(OutboundIntent.updated_at.desc())
        ).first()

        if not patients:
            status = "unknown"
            instruction = "Ask for the caller's full name before creating a patient or booking."
        elif len(patients) == 1:
            status = "recognized"
            instruction = (
                "A returning record exists. Use prior context, but still capture the caller's full "
                "name before any booking write."
            )
        else:
            status = "ambiguous"
            instruction = (
                "Multiple patients share this phone. Ask for the full name first and do not assume "
                "which patient is calling."
            )

        return CallerContextResponse(
            status=status,
            candidates=candidates,
            returning_patient=any(candidate.has_prior_appointments for candidate in candidates),
            resume_context=resume.checkpoint if resume else None,
            callback_context={"purpose": callback.purpose, **callback.context}
            if callback
            else None,
            instruction=instruction,
        )

    def identify(
        self,
        db: Session,
        phone_e164: str,
        full_name: str,
        create_if_missing: bool,
    ) -> IdentifyPatientResponse:
        if len(full_name.strip().split()) < 2:
            raise DomainError(
                "FULL_NAME_REQUIRED",
                "Capture the patient's full name, including at least given and family name.",
            )
        phone = normalize_phone(phone_e164)
        normalized = normalize_name(full_name)
        candidates = db.scalars(
            select(Patient)
            .join(PatientPhone)
            .where(PatientPhone.phone_e164 == phone, Patient.active.is_(True))
        ).all()
        matches = [
            patient for patient in candidates if normalize_name(patient.full_name) == normalized
        ]
        if len(matches) == 1:
            patient = matches[0]
            if not patient.external_id and create_if_missing:
                patient.external_id = self.pms.create_patient(patient.full_name, phone)
                db.commit()
            return IdentifyPatientResponse(
                status="matched",
                patient_id=patient.id,
                spoken_name=patient.spoken_name,
                instruction="Identity matched by full name and phone. Continue without re-asking it.",
            )
        if len(matches) > 1:
            return IdentifyPatientResponse(
                status="ambiguous",
                instruction="More than one exact record exists. Log a human follow-up; do not book.",
            )
        if candidates and not create_if_missing:
            return IdentifyPatientResponse(
                status="not_found",
                instruction="The name did not match a patient on this phone number.",
            )
        if not create_if_missing:
            return IdentifyPatientResponse(
                status="not_found", instruction="No matching patient record was found."
            )

        try:
            external_id = self.pms.create_patient(full_name.strip(), phone)
        except PmsError:
            raise
        patient = Patient(
            full_name=" ".join(full_name.strip().split()),
            spoken_name=" ".join(part.capitalize() for part in full_name.strip().split()),
            external_id=external_id,
        )
        patient.phones.append(PatientPhone(phone_e164=phone, is_primary=True))
        db.add(patient)
        db.commit()
        return IdentifyPatientResponse(
            status="created",
            patient_id=patient.id,
            spoken_name=patient.spoken_name,
            instruction="New patient created after capturing a full name. Continue to scheduling.",
        )
