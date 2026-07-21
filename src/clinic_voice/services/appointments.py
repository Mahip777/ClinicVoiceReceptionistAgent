from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from clinic_voice.config import Settings
from clinic_voice.errors import DomainError, PmsError
from clinic_voice.models import (
    Appointment,
    AppointmentType,
    Branch,
    CallSession,
    FollowupRequest,
    IdempotencyRecord,
    OfferedSlot,
    Patient,
    PmsOutbox,
    Practitioner,
    SlotReservation,
)
from clinic_voice.pms import PmsAdapter
from clinic_voice.schemas import (
    AppointmentResult,
    BookingRequest,
    BookingResponse,
    CancelRequest,
    ChangeResponse,
    RescheduleRequest,
)
from clinic_voice.security import normalize_name, request_hash

from .availability import AvailabilityService

ACTIVE_APPOINTMENT_STATUSES = ("pending_sync", "confirmed")


class AppointmentService:
    def __init__(
        self,
        settings: Settings,
        pms: PmsAdapter,
        availability: AvailabilityService,
        now_fn=None,
    ) -> None:
        self.settings = settings
        self.pms = pms
        self.availability = availability
        self.now_fn = now_fn or (lambda: datetime.now(UTC))

    def book(self, db: Session, request: BookingRequest) -> BookingResponse:
        if replay := self._replay(db, request.idempotency_key, "book", request.model_dump_json()):
            return BookingResponse.model_validate(replay)
        existing = db.scalar(
            select(Appointment).where(Appointment.idempotency_key == request.idempotency_key)
        )
        if existing:
            return self._booking_response(db, existing, "IDEMPOTENT_REPLAY")

        self._validate_booking_subject(request)
        self._require_explicit_confirmation(db, request)

        patient = self._verified_patient(
            db, request.patient_id, request.patient_full_name, request.phone_e164
        )
        offered = self._valid_offer(db, request.offer_id)
        if not self.availability.is_still_available(db, offered):
            alternatives = self.availability.alternatives_for_offer(db, offered)
            return BookingResponse(
                status="failed",
                code="SLOT_NO_LONGER_AVAILABLE",
                alternatives=alternatives,
                instruction=(
                    "Apologize briefly, say the slot was just taken, and offer only the fresh alternatives."
                ),
            )

        branch, practitioner, appointment_type = self._offer_entities(db, offered)
        slot_key = self._slot_key(offered)
        reservation = SlotReservation(
            slot_key=slot_key,
            offered_slot_id=offered.id,
            idempotency_key=request.idempotency_key,
            status="held",
            expires_at=self.now_fn() + timedelta(minutes=5),
        )
        appointment = Appointment(
            patient_id=patient.id,
            branch_id=branch.id,
            practitioner_id=practitioner.id,
            appointment_type_id=appointment_type.id,
            starts_at=offered.starts_at,
            ends_at=offered.ends_at,
            status="pending_sync",
            idempotency_key=request.idempotency_key,
            pms_sync_status="pending",
        )
        if self._has_local_overlap(db, offered.practitioner_id, offered.starts_at, offered.ends_at):
            alternatives = self.availability.alternatives_for_offer(db, offered)
            return BookingResponse(
                status="failed",
                code="SLOT_NO_LONGER_AVAILABLE",
                alternatives=alternatives,
                instruction="The slot changed. Offer the newly returned alternatives.",
            )
        db.add_all([reservation, appointment])
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            alternatives = self.availability.alternatives_for_offer(db, offered)
            return BookingResponse(
                status="failed",
                code="SLOT_NO_LONGER_AVAILABLE",
                alternatives=alternatives,
                instruction="A concurrent caller took the slot. Offer fresh alternatives.",
            )

        try:
            pms_result = self.pms.create_appointment(
                patient,
                branch,
                practitioner,
                appointment_type,
                offered.starts_at,
                offered.ends_at,
                request.idempotency_key,
            )
            if pms_result.has_conflict:
                self.pms.cancel_appointment(
                    pms_result.external_id,
                    "Conflict detected",
                    request.idempotency_key + ":rollback",
                )
                appointment.status = "failed"
                appointment.pms_sync_status = "conflict"
                reservation.status = "released"
                db.commit()
                alternatives = self.availability.alternatives_for_offer(db, offered)
                return BookingResponse(
                    status="failed",
                    code="PMS_CONFLICT_DETECTED",
                    alternatives=alternatives,
                    instruction="Do not confirm. Explain the slot changed and offer fresh alternatives.",
                )
            appointment.external_id = pms_result.external_id
            appointment.status = "confirmed"
            appointment.pms_sync_status = "synced"
            reservation.status = "consumed"
            db.commit()
            response = self._booking_response(db, appointment, "BOOKING_CONFIRMED")
            self._save_replay(
                db, request.idempotency_key, "book", request.model_dump_json(), response
            )
            return response

        except PmsError as exc:
            appointment.status = "pending_sync"
            appointment.pms_sync_status = "failed"
            db.add(
                PmsOutbox(
                    operation="create_appointment",
                    aggregate_id=appointment.id,
                    idempotency_key=f"pms:{request.idempotency_key}",
                    payload={
                        "patient_id": patient.id,
                        "branch_id": branch.id,
                        "practitioner_id": practitioner.id,
                        "appointment_type_id": appointment_type.id,
                        "starts_at": offered.starts_at.isoformat(),
                        "ends_at": offered.ends_at.isoformat(),
                    },
                    status="manual_review",
                    attempt_count=1,
                    last_error=exc.message,
                )
            )
            db.add(
                FollowupRequest(
                    call_id=request.call_id,
                    patient_id=patient.id,
                    phone_e164=request.phone_e164,
                    reason="system_failure",
                    details=f"Cliniko booking confirmation failed: {exc.code}",
                    priority="urgent",
                )
            )
            db.commit()
            response = BookingResponse(
                status="pending_sync",
                code=exc.code,
                appointment=self._result(db, appointment),
                instruction=(
                    "Do not say fully confirmed. Say the slot is reserved but system confirmation is "
                    "pending, and clinic staff will call back."
                ),
            )
            self._save_replay(
                db, request.idempotency_key, "book", request.model_dump_json(), response
            )
            return response

    def _validate_booking_subject(self, request: BookingRequest) -> None:
        caller_name = normalize_name(request.caller_full_name or "")
        patient_name = normalize_name(request.patient_full_name)
        if request.booking_for == "other":
            if not caller_name:
                raise DomainError(
                    "CALLER_FULL_NAME_REQUIRED",
                    "Capture the caller's full name separately before booking for another patient.",
                )
            if caller_name == patient_name:
                raise DomainError(
                    "BOOKING_SUBJECT_MISMATCH",
                    "The caller and appointment patient were confused. Identify the intended patient before booking.",
                )
        elif caller_name and caller_name != patient_name:
            raise DomainError(
                "BOOKING_SUBJECT_MISMATCH",
                "The caller name differs from the appointment patient. Set booking_for=other and identify that patient.",
            )

    def _require_explicit_confirmation(self, db: Session, request: BookingRequest) -> None:
        """Protect live voice calls from booking on a slot-selection utterance."""
        if not request.call_id:
            return
        session = db.scalar(select(CallSession).where(CallSession.call_id == request.call_id))
        state = session.checkpoint if session else {}
        if (
            state.get("stage") != "booking_confirmed"
            or state.get("confirmed_offer_id") != request.offer_id
            or state.get("explicit_confirmation") is not True
        ):
            raise DomainError(
                "EXPLICIT_CONFIRMATION_REQUIRED",
                (
                    "Do not book yet. Speak the selected date, time, doctor, and branch, ask whether "
                    "to confirm, wait for a new caller response, then checkpoint stage=booking_confirmed "
                    "with this offer_id and explicit_confirmation=true."
                ),
            )

    def reschedule(self, db: Session, request: RescheduleRequest) -> ChangeResponse:
        if replay := self._replay(
            db, request.idempotency_key, "reschedule", request.model_dump_json()
        ):
            return ChangeResponse.model_validate(replay)
        appointment = self._owned_appointment(
            db, request.appointment_id, request.patient_id, request.patient_full_name
        )
        fee = self._change_fee(appointment)
        if fee and not request.confirm_fee:
            return ChangeResponse(
                status="fee_confirmation_required",
                code="FEE_CONFIRMATION_REQUIRED",
                fee_minor=fee,
                currency=self.settings.clinic_currency,
                appointment=self._result(db, appointment),
                instruction="Mention the fee once and ask whether the patient wants to continue.",
            )
        offered = self._valid_offer(db, request.new_offer_id)
        if offered.appointment_type_id != appointment.appointment_type_id:
            raise DomainError(
                "APPOINTMENT_TYPE_MISMATCH", "Rescheduling must preserve the appointment type."
            )
        if not self.availability.is_still_available(db, offered):
            return ChangeResponse(
                status="failed",
                code="SLOT_NO_LONGER_AVAILABLE",
                fee_minor=fee,
                currency=self.settings.clinic_currency,
                instruction="The new slot was taken. Run a fresh availability search.",
            )
        if self._has_local_overlap(
            db,
            offered.practitioner_id,
            offered.starts_at,
            offered.ends_at,
            exclude_id=appointment.id,
        ):
            return ChangeResponse(
                status="failed",
                code="SLOT_NO_LONGER_AVAILABLE",
                instruction="The new slot conflicts. Run a fresh availability search.",
            )
        old = (appointment.starts_at, appointment.ends_at)
        try:
            pms_result = self.pms.reschedule_appointment(
                appointment.external_id or "",
                offered.starts_at,
                offered.ends_at,
                request.idempotency_key,
            )
        except PmsError as exc:
            return ChangeResponse(
                status="failed",
                code=exc.code,
                fee_minor=fee,
                currency=self.settings.clinic_currency,
                appointment=self._result(db, appointment),
                instruction="The original appointment is unchanged. Say the reschedule did not complete.",
            )
        if pms_result.has_conflict:
            self.pms.reschedule_appointment(
                appointment.external_id or "", old[0], old[1], request.idempotency_key + ":rollback"
            )
            return ChangeResponse(
                status="failed",
                code="PMS_CONFLICT_DETECTED",
                appointment=self._result(db, appointment),
                instruction="The original appointment remains unchanged. Search again.",
            )
        appointment.branch_id = offered.branch_id
        appointment.practitioner_id = offered.practitioner_id
        appointment.starts_at = offered.starts_at
        appointment.ends_at = offered.ends_at
        appointment.pms_sync_status = "synced"
        db.commit()
        response = ChangeResponse(
            status="confirmed",
            code="RESCHEDULE_CONFIRMED",
            fee_minor=fee,
            currency=self.settings.clinic_currency,
            appointment=self._result(db, appointment),
            instruction="Confirm the new date, local time, doctor, and persisted branch exactly once.",
        )
        self._save_replay(
            db, request.idempotency_key, "reschedule", request.model_dump_json(), response
        )
        return response

    def cancel(self, db: Session, request: CancelRequest) -> ChangeResponse:
        if replay := self._replay(db, request.idempotency_key, "cancel", request.model_dump_json()):
            return ChangeResponse.model_validate(replay)
        appointment = self._owned_appointment(
            db, request.appointment_id, request.patient_id, request.patient_full_name
        )
        fee = self._change_fee(appointment)
        if fee and not request.confirm_fee:
            return ChangeResponse(
                status="fee_confirmation_required",
                code="FEE_CONFIRMATION_REQUIRED",
                fee_minor=fee,
                currency=self.settings.clinic_currency,
                appointment=self._result(db, appointment),
                instruction="Mention the fee once and ask whether to continue with cancellation.",
            )
        try:
            self.pms.cancel_appointment(
                appointment.external_id or "", request.reason, request.idempotency_key
            )
        except PmsError as exc:
            return ChangeResponse(
                status="failed",
                code=exc.code,
                fee_minor=fee,
                currency=self.settings.clinic_currency,
                appointment=self._result(db, appointment),
                instruction="Do not claim cancellation. Say it could not be completed and log follow-up.",
            )
        appointment.status = "cancelled"
        appointment.cancellation_reason = request.reason
        appointment.pms_sync_status = "synced"
        db.commit()
        response = ChangeResponse(
            status="confirmed",
            code="CANCELLATION_CONFIRMED",
            fee_minor=fee,
            currency=self.settings.clinic_currency,
            appointment=self._result(db, appointment),
            instruction="Confirm that this specific appointment was cancelled.",
        )
        self._save_replay(
            db, request.idempotency_key, "cancel", request.model_dump_json(), response
        )
        return response

    def list_for_patient(self, db: Session, patient_id: str, include_past: bool = False):
        query = select(Appointment).where(Appointment.patient_id == patient_id)
        if not include_past:
            query = query.where(Appointment.starts_at >= self.now_fn())
        query = query.order_by(Appointment.starts_at)
        return [self._result(db, item) for item in db.scalars(query).all()]

    def _verified_patient(
        self, db: Session, patient_id: str, full_name: str, phone_e164: str | None = None
    ) -> Patient:
        if len(full_name.strip().split()) < 2:
            raise DomainError("FULL_NAME_REQUIRED", "A full name is required before booking.")
        patient = db.get(Patient, patient_id)
        if not patient or normalize_name(patient.full_name) != normalize_name(full_name):
            raise DomainError(
                "IDENTITY_MISMATCH", "The supplied full name does not match the patient."
            )
        return patient

    def _owned_appointment(
        self, db: Session, appointment_id: str, patient_id: str, full_name: str
    ) -> Appointment:
        patient = self._verified_patient(db, patient_id, full_name)
        appointment = db.get(Appointment, appointment_id)
        if not appointment or appointment.patient_id != patient.id:
            raise DomainError("APPOINTMENT_NOT_FOUND", "No matching appointment was found.", 404)
        if appointment.status != "confirmed":
            raise DomainError("APPOINTMENT_NOT_ACTIVE", "This appointment is not active.")
        return appointment

    def _valid_offer(self, db: Session, offer_id: str) -> OfferedSlot:
        offered = db.get(OfferedSlot, offer_id)
        if not offered:
            raise DomainError("OFFER_NOT_FOUND", "The offered slot does not exist.", 404)
        expires = self._aware(offered.expires_at)
        if expires < self.now_fn():
            raise DomainError(
                "OFFER_EXPIRED", "The offered slot expired; search availability again."
            )
        offered.starts_at = self._aware(offered.starts_at)
        offered.ends_at = self._aware(offered.ends_at)
        return offered

    def _has_local_overlap(
        self,
        db: Session,
        practitioner_id: str,
        starts_at: datetime,
        ends_at: datetime,
        exclude_id: str | None = None,
    ) -> bool:
        query = select(Appointment.id).where(
            Appointment.practitioner_id == practitioner_id,
            Appointment.status.in_(ACTIVE_APPOINTMENT_STATUSES),
            Appointment.starts_at < ends_at,
            Appointment.ends_at > starts_at,
        )
        if exclude_id:
            query = query.where(Appointment.id != exclude_id)
        return db.scalar(query.limit(1)) is not None

    def _change_fee(self, appointment: Appointment) -> int:
        starts_at = self._aware(appointment.starts_at)
        remaining = starts_at - self.now_fn()
        if remaining <= timedelta(hours=self.settings.cancellation_window_hours):
            return self.settings.cancellation_fee_minor
        return 0

    @staticmethod
    def _slot_key(offered: OfferedSlot) -> str:
        return f"{offered.practitioner_id}:{offered.starts_at.isoformat()}:{offered.ends_at.isoformat()}"

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    @staticmethod
    def _offer_entities(db: Session, offered: OfferedSlot):
        return (
            db.get(Branch, offered.branch_id),
            db.get(Practitioner, offered.practitioner_id),
            db.get(AppointmentType, offered.appointment_type_id),
        )

    def _booking_response(
        self, db: Session, appointment: Appointment, code: str
    ) -> BookingResponse:
        status = "confirmed" if appointment.status == "confirmed" else "pending_sync"
        instruction = (
            "Confirm the persisted date, clinic-local time, doctor, and branch exactly as returned."
            if status == "confirmed"
            else "Do not claim full confirmation; explain that clinic-system confirmation is pending."
        )
        return BookingResponse(
            status=status,
            code=code,
            appointment=self._result(db, appointment),
            instruction=instruction,
        )

    def _result(self, db: Session, appointment: Appointment) -> AppointmentResult:
        patient = db.get(Patient, appointment.patient_id)
        branch = db.get(Branch, appointment.branch_id)
        practitioner = db.get(Practitioner, appointment.practitioner_id)
        appointment_type = db.get(AppointmentType, appointment.appointment_type_id)
        return AppointmentResult(
            appointment_id=appointment.id,
            external_id=appointment.external_id,
            status=appointment.status,
            patient_name=patient.spoken_name,
            branch_code=branch.code,
            branch_name=branch.display_name,
            practitioner_name=practitioner.spoken_name,
            appointment_type_name=appointment_type.display_name,
            starts_at=self._aware(appointment.starts_at),
            ends_at=self._aware(appointment.ends_at),
            patient_duration_minutes=appointment_type.patient_duration_minutes,
            pms_sync_status=appointment.pms_sync_status,
        )

    def _replay(self, db: Session, key: str, operation: str, payload: str) -> dict | None:
        record = db.get(IdempotencyRecord, key)
        if not record:
            return None
        if record.operation != operation or record.request_hash != request_hash(payload):
            raise DomainError(
                "IDEMPOTENCY_KEY_REUSED",
                "This idempotency key was already used for a different request.",
                409,
            )
        return record.response_body

    @staticmethod
    def _save_replay(db: Session, key: str, operation: str, payload: str, response) -> None:
        if db.get(IdempotencyRecord, key):
            return
        db.add(
            IdempotencyRecord(
                key=key,
                operation=operation,
                request_hash=request_hash(payload),
                response_status=200,
                response_body=response.model_dump(mode="json"),
            )
        )
        db.commit()
