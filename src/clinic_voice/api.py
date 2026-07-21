from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from clinic_voice.config import Settings, get_settings
from clinic_voice.database import get_db
from clinic_voice.pms import get_pms_adapter
from clinic_voice.schemas import (
    AvailabilityRequest,
    AvailabilityResponse,
    BookingRequest,
    BookingResponse,
    CallerContextRequest,
    CallerContextResponse,
    CancelRequest,
    ChangeResponse,
    CheckpointRequest,
    ClinicCatalogResponse,
    FollowupRequestSchema,
    IdentifyPatientRequest,
    IdentifyPatientResponse,
    ListAppointmentsRequest,
    ListAppointmentsResponse,
    RescheduleRequest,
    RetellEventPayload,
    RetellInboundPayload,
)
from clinic_voice.security import secure_equals, verify_retell_signature
from clinic_voice.services.appointments import AppointmentService
from clinic_voice.services.availability import AvailabilityService
from clinic_voice.services.calls import CallService
from clinic_voice.services.identity import IdentityService

router = APIRouter()


def services(settings: Settings):
    pms = get_pms_adapter()
    availability = AvailabilityService(settings, pms)
    return (
        IdentityService(settings, pms),
        availability,
        AppointmentService(settings, pms, availability),
        CallService(),
    )


async def verify_secret(
    request: Request,
    x_webhook_secret: str | None = Header(default=None),
    x_retell_signature: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    if settings.app_env in {"test", "development"} and not x_webhook_secret:
        return
    shared_secret_valid = bool(x_webhook_secret) and secure_equals(
        x_webhook_secret, settings.webhook_secret
    )
    bearer = authorization.removeprefix("Bearer ").strip() if authorization else ""
    retell_key_valid = bool(settings.retell_api_key and bearer) and secure_equals(
        bearer, settings.retell_api_key
    )
    signature_valid = verify_retell_signature(
        await request.body(), settings.retell_api_key, x_retell_signature
    )
    if not shared_secret_valid and not retell_key_valid and not signature_valid:
        raise HTTPException(status_code=401, detail="Invalid webhook authentication")


@router.get("/health")
def health(db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    db.execute(text("SELECT 1"))
    return {
        "status": "ok",
        "time": datetime.now(UTC).isoformat(),
        "pms_provider": settings.pms_provider,
        "database": db.bind.dialect.name,
    }


@router.post(
    "/v1/tools/get-caller-context",
    response_model=CallerContextResponse,
    dependencies=[Depends(verify_secret)],
)
def get_caller_context(
    payload: CallerContextRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    identity, _, _, _ = services(settings)
    return identity.caller_context(db, payload.phone_e164)


@router.post(
    "/v1/tools/identify-patient",
    response_model=IdentifyPatientResponse,
    dependencies=[Depends(verify_secret)],
)
def identify_patient(
    payload: IdentifyPatientRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    identity, _, _, calls = services(settings)
    result = identity.identify(db, payload.phone_e164, payload.full_name, payload.create_if_missing)
    if payload.call_id and result.patient_id:
        role = payload.subject_role
        calls.checkpoint(
            db,
            CheckpointRequest(
                call_id=payload.call_id,
                phone_e164=payload.phone_e164,
                patient_id=result.patient_id if role == "appointment_patient" else None,
                state={
                    f"{role}_full_name_captured": True,
                    f"{role}_patient_id": result.patient_id,
                    f"{role}_full_name": payload.full_name,
                },
            ),
        )
    return result


@router.post(
    "/v1/tools/get-clinic-catalog",
    response_model=ClinicCatalogResponse,
    dependencies=[Depends(verify_secret)],
)
def get_clinic_catalog(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    _, availability, _, _ = services(settings)
    return availability.catalog(db)


@router.post(
    "/v1/tools/search-availability",
    response_model=AvailabilityResponse,
    dependencies=[Depends(verify_secret)],
)
def search_availability(
    payload: AvailabilityRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    _, availability, _, _ = services(settings)
    return availability.search(db, payload)


@router.post(
    "/v1/tools/book-appointment",
    response_model=BookingResponse,
    dependencies=[Depends(verify_secret)],
)
def book_appointment(
    payload: BookingRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    _, _, appointments, calls = services(settings)
    result = appointments.book(db, payload)
    if payload.call_id and result.status == "confirmed":
        calls.checkpoint(
            db,
            CheckpointRequest(
                call_id=payload.call_id,
                phone_e164=payload.phone_e164,
                patient_id=payload.patient_id,
                intent="booking",
                state={"completed": True, "appointment_id": result.appointment.appointment_id},
            ),
        )
    return result


@router.post(
    "/v1/tools/list-appointments",
    response_model=ListAppointmentsResponse,
    dependencies=[Depends(verify_secret)],
)
def list_appointments(
    payload: ListAppointmentsRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    _, _, appointments, _ = services(settings)
    return ListAppointmentsResponse(
        appointments=appointments.list_for_patient(db, payload.patient_id, payload.include_past)
    )


@router.post(
    "/v1/tools/reschedule-appointment",
    response_model=ChangeResponse,
    dependencies=[Depends(verify_secret)],
)
def reschedule_appointment(
    payload: RescheduleRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    _, _, appointments, _ = services(settings)
    return appointments.reschedule(db, payload)


@router.post(
    "/v1/tools/cancel-appointment",
    response_model=ChangeResponse,
    dependencies=[Depends(verify_secret)],
)
def cancel_appointment(
    payload: CancelRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    _, _, appointments, _ = services(settings)
    return appointments.cancel(db, payload)


@router.post("/v1/tools/checkpoint", dependencies=[Depends(verify_secret)])
def checkpoint(
    payload: CheckpointRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    _, _, _, calls = services(settings)
    item = calls.checkpoint(db, payload)
    return {"status": "saved", "session_id": item.id}


@router.post("/v1/tools/create-followup", dependencies=[Depends(verify_secret)])
def create_followup(
    payload: FollowupRequestSchema,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    _, _, _, calls = services(settings)
    item = calls.create_followup(db, payload)
    return {
        "status": "logged",
        "followup_id": item.id,
        "instruction": "Say that clinic staff will call back; do not imply an immediate transfer.",
    }


@router.post("/webhooks/retell/inbound", dependencies=[Depends(verify_secret)])
def retell_inbound(
    payload: RetellInboundPayload,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    identity, _, _, calls = services(settings)
    phone = str(payload.from_number)
    context = identity.caller_context(db, phone)
    call_id = payload.call_id or f"pending:{phone}:{int(datetime.now().timestamp())}"
    calls.checkpoint(
        db,
        CheckpointRequest(
            call_id=call_id,
            phone_e164=phone,
            state={"inbound_context_loaded": True},
        ),
    )
    response = {
        "dynamic_variables": {
            "caller_status": context.status,
            "returning_patient": str(context.returning_patient).lower(),
            "candidate_count": str(len(context.candidates)),
            "resume_context": str(context.resume_context or {}),
            "callback_context": str(context.callback_context or {}),
            "clinic_timezone": settings.clinic_timezone,
        },
        "metadata": {"internal_call_id": call_id},
    }
    return {"call_inbound": response} if payload.call_inbound is not None else response


@router.post("/webhooks/retell/events", dependencies=[Depends(verify_secret)])
def retell_events(
    payload: RetellEventPayload,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    _, _, _, calls = services(settings)
    call = payload.call
    if payload.event in {"call_ended", "call_analyzed"}:
        calls.mark_ended(
            db,
            str(call.get("call_id", "")),
            call.get("disconnection_reason") or call.get("end_reason"),
            call.get("transcript"),
        )
    return {"status": "accepted"}
