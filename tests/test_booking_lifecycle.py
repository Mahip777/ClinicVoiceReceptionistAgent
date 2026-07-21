from datetime import date, timedelta

import pytest
from sqlalchemy import select

from clinic_voice.database import SessionLocal
from clinic_voice.errors import DomainError
from clinic_voice.models import Appointment, AppointmentType, Branch, Practitioner
from clinic_voice.schemas import (
    AvailabilityRequest,
    BookingRequest,
    CancelRequest,
    CheckpointRequest,
)
from clinic_voice.services.calls import CallService

from .helpers import FIXED_NOW, patient_by_phone, service_bundle


def offered_slot(db, availability):
    return availability.search(
        db,
        AvailabilityRequest(
            specialty="General Medicine",
            date_from=date(2026, 12, 14),
            date_to=date(2026, 12, 14),
        ),
    ).slots[0]


def test_booking_requires_full_name_and_confirms_persisted_branch():
    settings, _, _, availability, appointments = service_bundle()
    with SessionLocal() as db:
        patient = patient_by_phone(db, settings.test_phone_returning)
        slot = offered_slot(db, availability)
        with pytest.raises(DomainError, match="full name"):
            appointments.book(
                db,
                BookingRequest(
                    phone_e164=settings.test_phone_returning,
                    patient_id=patient.id,
                    patient_full_name="Asha",
                    offer_id=slot.offer_id,
                    idempotency_key="booking-short-name",
                ),
            )
        result = appointments.book(
            db,
            BookingRequest(
                phone_e164=settings.test_phone_returning,
                patient_id=patient.id,
                patient_full_name="Asha Verma",
                offer_id=slot.offer_id,
                idempotency_key="booking-full-name",
            ),
        )
        assert result.status == "confirmed"
        assert result.appointment.branch_code == slot.branch_code
        assert result.appointment.pms_sync_status == "synced"


def test_live_call_booking_requires_confirmation_after_exact_offer_selection():
    settings, _, _, availability, appointments = service_bundle()
    calls = CallService()
    with SessionLocal() as db:
        patient = patient_by_phone(db, settings.test_phone_returning)
        slot = offered_slot(db, availability)
        calls.checkpoint(
            db,
            CheckpointRequest(
                call_id="confirmation-gate-call",
                phone_e164=settings.test_phone_returning,
                patient_id=patient.id,
                intent="booking",
                state={"stage": "slot_selected", "selected_offer_id": slot.offer_id},
            ),
        )
        request = BookingRequest(
            call_id="confirmation-gate-call",
            phone_e164=settings.test_phone_returning,
            patient_id=patient.id,
            patient_full_name="Asha Verma",
            caller_full_name="Asha Verma",
            booking_for="self",
            offer_id=slot.offer_id,
            idempotency_key="confirmation-gate-booking",
        )
        with pytest.raises(DomainError, match="Do not book yet") as error:
            appointments.book(db, request)
        assert error.value.code == "EXPLICIT_CONFIRMATION_REQUIRED"

        calls.checkpoint(
            db,
            CheckpointRequest(
                call_id="confirmation-gate-call",
                phone_e164=settings.test_phone_returning,
                patient_id=patient.id,
                intent="booking",
                state={
                    "stage": "booking_confirmed",
                    "confirmed_offer_id": slot.offer_id,
                    "explicit_confirmation": True,
                },
            ),
        )
        result = appointments.book(db, request)
        assert result.status == "confirmed"


def test_booking_for_someone_else_cannot_reuse_the_callers_identity():
    settings, _, _, availability, appointments = service_bundle()
    with SessionLocal() as db:
        patient = patient_by_phone(db, settings.test_phone_returning)
        slot = offered_slot(db, availability)
        with pytest.raises(DomainError) as error:
            appointments.book(
                db,
                BookingRequest(
                    phone_e164=settings.test_phone_returning,
                    patient_id=patient.id,
                    patient_full_name="Asha Verma",
                    caller_full_name="Asha Verma",
                    booking_for="other",
                    offer_id=slot.offer_id,
                    idempotency_key="wrong-booking-subject",
                ),
            )
        assert error.value.code == "BOOKING_SUBJECT_MISMATCH"


def test_slot_taken_after_offer_returns_fresh_alternatives():
    settings, _, _, availability, appointments = service_bundle()
    with SessionLocal() as db:
        patient = patient_by_phone(db, settings.test_phone_returning)
        slot = offered_slot(db, availability)
        offered = db.get(
            __import__("clinic_voice.models", fromlist=["OfferedSlot"]).OfferedSlot, slot.offer_id
        )
        db.add(
            Appointment(
                patient_id=patient.id,
                branch_id=offered.branch_id,
                practitioner_id=offered.practitioner_id,
                appointment_type_id=offered.appointment_type_id,
                starts_at=offered.starts_at,
                ends_at=offered.ends_at,
                status="confirmed",
                external_id="competitor",
                idempotency_key="competitor-booking",
                pms_sync_status="synced",
            )
        )
        db.commit()
        result = appointments.book(
            db,
            BookingRequest(
                phone_e164=settings.test_phone_returning,
                patient_id=patient.id,
                patient_full_name="Asha Verma",
                offer_id=slot.offer_id,
                idempotency_key="race-loser",
            ),
        )
        assert result.code == "SLOT_NO_LONGER_AVAILABLE"
        assert result.status == "failed"
        assert all(item.offer_id != slot.offer_id for item in result.alternatives)


def test_booking_idempotency_returns_same_appointment():
    settings, _, _, availability, appointments = service_bundle()
    with SessionLocal() as db:
        patient = patient_by_phone(db, settings.test_phone_returning)
        slot = offered_slot(db, availability)
        request = BookingRequest(
            phone_e164=settings.test_phone_returning,
            patient_id=patient.id,
            patient_full_name="Asha Verma",
            offer_id=slot.offer_id,
            idempotency_key="same-request-123",
        )
        first = appointments.book(db, request)
        second = appointments.book(db, request)
        assert first.appointment.appointment_id == second.appointment.appointment_id
        assert db.scalar(
            select(Appointment).where(Appointment.idempotency_key == "same-request-123")
        )


def test_cancellation_fee_only_inside_policy_window_and_is_idempotent():
    settings, _, _, _, appointments = service_bundle()
    with SessionLocal() as db:
        patient = patient_by_phone(db, settings.test_phone_returning)
        branch = db.scalar(select(Branch).where(Branch.code == "BR_CENTRAL"))
        doctor = db.scalar(select(Practitioner).where(Practitioner.code == "DR_GENERAL_1"))
        kind = db.scalar(select(AppointmentType).where(AppointmentType.code == "GENERAL_FOLLOWUP"))
        item = Appointment(
            patient_id=patient.id,
            branch_id=branch.id,
            practitioner_id=doctor.id,
            appointment_type_id=kind.id,
            starts_at=FIXED_NOW + timedelta(hours=10),
            ends_at=FIXED_NOW + timedelta(hours=10, minutes=30),
            status="confirmed",
            external_id="fee-test",
            idempotency_key="fee-seed",
            pms_sync_status="synced",
        )
        db.add(item)
        db.commit()
        request = CancelRequest(
            appointment_id=item.id,
            patient_id=patient.id,
            patient_full_name="Asha Verma",
            idempotency_key="cancel-fee-test",
            confirm_fee=False,
        )
        warning = appointments.cancel(db, request)
        assert warning.status == "fee_confirmation_required"
        request.confirm_fee = True
        confirmed = appointments.cancel(db, request)
        replay = appointments.cancel(db, request)
        assert confirmed.status == "confirmed"
        assert confirmed.fee_minor == settings.cancellation_fee_minor
        assert replay.code == "CANCELLATION_CONFIRMED"


def test_pms_failure_never_returns_false_confirmation():
    settings, pms, _, availability, appointments = service_bundle()
    with SessionLocal() as db:
        patient = patient_by_phone(db, settings.test_phone_returning)
        slot = offered_slot(db, availability)
        pms.fail_next_operation = "create"
        result = appointments.book(
            db,
            BookingRequest(
                phone_e164=settings.test_phone_returning,
                patient_id=patient.id,
                patient_full_name="Asha Verma",
                offer_id=slot.offer_id,
                idempotency_key="pms-failure-test",
            ),
        )
        assert result.status == "pending_sync"
        assert result.appointment.pms_sync_status == "failed"
        assert "Do not say fully confirmed" in result.instruction
