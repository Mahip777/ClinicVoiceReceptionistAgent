from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class CallerContextRequest(BaseModel):
    phone_e164: str
    call_id: str | None = None


class PatientCandidate(BaseModel):
    patient_id: str
    display_name: str
    has_prior_appointments: bool


class CallerContextResponse(BaseModel):
    status: Literal["unknown", "recognized", "ambiguous"]
    candidates: list[PatientCandidate] = Field(default_factory=list)
    returning_patient: bool = False
    resume_context: dict[str, Any] | None = None
    callback_context: dict[str, Any] | None = None
    instruction: str


class IdentifyPatientRequest(BaseModel):
    phone_e164: str
    full_name: str = Field(min_length=2, max_length=255)
    call_id: str | None = None
    create_if_missing: bool = True


class IdentifyPatientResponse(BaseModel):
    status: Literal["matched", "created", "not_found", "ambiguous"]
    patient_id: str | None = None
    spoken_name: str | None = None
    instruction: str


class TimeWindow(BaseModel):
    start: time
    end: time

    @model_validator(mode="after")
    def validate_order(self) -> TimeWindow:
        if self.end <= self.start:
            raise ValueError("time window end must be after start")
        return self


class AvailabilityRequest(BaseModel):
    call_id: str | None = None
    branch_code: str | None = None
    practitioner_code: str | None = None
    practitioner_name: str | None = None
    appointment_type_code: str | None = None
    appointment_type_name: str | None = None
    specialty: str | None = None
    date_from: date
    date_to: date | None = None
    preferred_weekdays: list[int] = Field(default_factory=list)
    time_window: TimeWindow | None = None
    around_time: time | None = None
    around_minutes: int = Field(default=60, ge=0, le=240)
    earliest: bool = True
    limit: int = Field(default=3, ge=1, le=10)

    @model_validator(mode="after")
    def validate_dates(self) -> AvailabilityRequest:
        if self.date_to and self.date_to < self.date_from:
            raise ValueError("date_to must not precede date_from")
        if any(day < 0 or day > 6 for day in self.preferred_weekdays):
            raise ValueError("preferred_weekdays use Monday=0 through Sunday=6")
        return self


class SlotResult(BaseModel):
    offer_id: str
    branch_code: str
    branch_name: str
    practitioner_code: str
    practitioner_name: str
    appointment_type_code: str
    appointment_type_name: str
    starts_at: datetime
    ends_at: datetime
    patient_duration_minutes: int
    price_minor: int
    currency: str


class CatalogBranch(BaseModel):
    code: str
    name: str


class CatalogAppointmentType(BaseModel):
    code: str
    name: str
    specialty: str


class CatalogPractitioner(BaseModel):
    code: str
    name: str
    specialty: str
    branch_codes: list[str]
    appointment_type_codes: list[str]


class ClinicCatalogResponse(BaseModel):
    status: Literal["available"] = "available"
    specialties: list[str]
    branches: list[CatalogBranch]
    practitioners: list[CatalogPractitioner]
    appointment_types: list[CatalogAppointmentType]
    instruction: str


class AvailabilityResponse(BaseModel):
    status: Literal["available", "unavailable"]
    code: Literal[
        "OK",
        "UNSUPPORTED_SPECIALTY",
        "UNKNOWN_BRANCH",
        "UNKNOWN_PRACTITIONER",
        "UNKNOWN_APPOINTMENT_TYPE",
        "INELIGIBLE_COMBINATION",
        "NO_AVAILABLE_SLOTS",
    ]
    search_id: str
    searched_at: datetime
    slots: list[SlotResult]
    suggestions: list[str] = Field(default_factory=list)
    instruction: str


class BookingRequest(BaseModel):
    call_id: str | None = None
    phone_e164: str
    patient_id: str
    patient_full_name: str = Field(min_length=2, max_length=255)
    offer_id: str
    idempotency_key: str = Field(min_length=8, max_length=255)


class AppointmentResult(BaseModel):
    appointment_id: str
    external_id: str | None = None
    status: str
    patient_name: str
    branch_code: str
    branch_name: str
    practitioner_name: str
    appointment_type_name: str
    starts_at: datetime
    ends_at: datetime
    patient_duration_minutes: int
    pms_sync_status: str


class BookingResponse(BaseModel):
    status: Literal["confirmed", "pending_sync", "failed"]
    code: str
    appointment: AppointmentResult | None = None
    alternatives: list[SlotResult] = Field(default_factory=list)
    instruction: str


class ListAppointmentsRequest(BaseModel):
    patient_id: str
    include_past: bool = False


class ListAppointmentsResponse(BaseModel):
    appointments: list[AppointmentResult]


class RescheduleRequest(BaseModel):
    call_id: str | None = None
    appointment_id: str
    patient_id: str
    patient_full_name: str = Field(min_length=2, max_length=255)
    new_offer_id: str
    idempotency_key: str = Field(min_length=8, max_length=255)
    confirm_fee: bool = False


class CancelRequest(BaseModel):
    call_id: str | None = None
    appointment_id: str
    patient_id: str
    patient_full_name: str = Field(min_length=2, max_length=255)
    idempotency_key: str = Field(min_length=8, max_length=255)
    reason: str = "Other"
    confirm_fee: bool = False


class ChangeResponse(BaseModel):
    status: Literal["confirmed", "fee_confirmation_required", "failed"]
    code: str
    fee_minor: int = 0
    currency: str = "INR"
    appointment: AppointmentResult | None = None
    instruction: str


class CheckpointRequest(BaseModel):
    call_id: str
    phone_e164: str
    patient_id: str | None = None
    intent: str | None = None
    state: dict[str, Any] = Field(default_factory=dict)


class FollowupRequestSchema(BaseModel):
    call_id: str | None = None
    patient_id: str | None = None
    phone_e164: str
    reason: Literal["human_requested", "clinical_concern", "out_of_scope", "system_failure"]
    details: str | None = None
    priority: Literal["normal", "urgent"] = "normal"


class RetellInboundPayload(BaseModel):
    event: str | None = None
    call_inbound: dict[str, Any] | None = None
    agent_id: str | None = None
    from_number: str | None = None
    to_number: str | None = None
    call_id: str | None = None
    dynamic_variables: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize_current_payload(self) -> RetellInboundPayload:
        incoming = self.call_inbound or {}
        self.agent_id = self.agent_id or incoming.get("agent_id")
        self.from_number = self.from_number or incoming.get("from_number")
        self.to_number = self.to_number or incoming.get("to_number")
        if not self.from_number:
            raise ValueError("from_number is required in call_inbound")
        return self


class RetellEventPayload(BaseModel):
    event: str
    call: dict[str, Any]
