from __future__ import annotations

from datetime import date, datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from clinic_voice.config import Settings
from clinic_voice.errors import PmsError
from clinic_voice.models import AppointmentType, Branch, Patient, Practitioner

from .base import PmsAdapter, PmsAppointment, PmsSlot


class ClinikoAdapter(PmsAdapter):
    def __init__(self, settings: Settings) -> None:
        if not settings.cliniko_api_key:
            raise RuntimeError("CLINIKO_API_KEY is required when PMS_PROVIDER=cliniko")
        self.settings = settings
        self.client = httpx.Client(
            base_url=settings.cliniko_api_base_url.rstrip("/"),
            auth=(settings.cliniko_api_key, ""),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": settings.cliniko_user_agent,
            },
            timeout=settings.request_timeout_seconds,
        )

    def create_patient(self, full_name: str, phone_e164: str) -> str:
        parts = full_name.strip().split()
        first_name = parts[0]
        last_name = " ".join(parts[1:])
        payload = self._request(
            "POST",
            "/patients",
            json={
                "first_name": first_name,
                "last_name": last_name,
                "patient_phone_numbers": [{"number": phone_e164, "phone_type": "Mobile"}],
                "country_code": "IN",
                "time_zone": self.settings.clinic_timezone,
                "notes": "Created by voice receptionist",
            },
        )
        return str(payload["id"])

    def available_times(
        self,
        db: Session,
        branch: Branch,
        practitioner: Practitioner,
        appointment_type: AppointmentType,
        date_from: date,
        date_to: date,
    ) -> list[PmsSlot]:
        self._require_external_ids(branch, practitioner, appointment_type)
        slots: list[PmsSlot] = []
        chunk_start = date_from
        while chunk_start <= date_to:
            chunk_end = min(chunk_start + timedelta(days=7), date_to)
            path = (
                f"/businesses/{branch.external_id}/practitioners/{practitioner.external_id}"
                f"/appointment_types/{appointment_type.external_id}/available_times"
            )
            page = 1
            while True:
                payload = self._request(
                    "GET",
                    path,
                    params={
                        "from": chunk_start.isoformat(),
                        "to": chunk_end.isoformat(),
                        "page": page,
                        "per_page": 100,
                    },
                )
                for item in payload.get("available_times", []):
                    starts_at = datetime.fromisoformat(
                        item["appointment_start"].replace("Z", "+00:00")
                    )
                    ends_at = starts_at + timedelta(
                        minutes=appointment_type.calendar_duration_minutes
                    )
                    slots.append(PmsSlot(starts_at, ends_at, item))
                if not payload.get("links", {}).get("next"):
                    break
                page += 1
            chunk_start = chunk_end + timedelta(days=1)
        return slots

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
        self._require_external_ids(patient, branch, practitioner, appointment_type)
        payload = self._request(
            "POST",
            "/individual_appointments",
            json={
                "patient_id": patient.external_id,
                "business_id": branch.external_id,
                "practitioner_id": practitioner.external_id,
                "appointment_type_id": appointment_type.external_id,
                "starts_at": starts_at.isoformat(),
                "ends_at": ends_at.isoformat(),
                "notes": f"Created by voice receptionist. Idempotency: {idempotency_key}",
            },
        )
        external_id = str(payload["id"])
        return PmsAppointment(
            external_id=external_id,
            starts_at=datetime.fromisoformat(payload["starts_at"].replace("Z", "+00:00")),
            ends_at=datetime.fromisoformat(payload["ends_at"].replace("Z", "+00:00")),
            has_conflict=self._has_conflict(external_id),
        )

    def reschedule_appointment(
        self,
        external_id: str,
        starts_at: datetime,
        ends_at: datetime,
        idempotency_key: str,
    ) -> PmsAppointment:
        payload = self._request(
            "PATCH",
            f"/individual_appointments/{external_id}",
            json={"starts_at": starts_at.isoformat(), "ends_at": ends_at.isoformat()},
        )
        return PmsAppointment(
            external_id=external_id,
            starts_at=datetime.fromisoformat(payload["starts_at"].replace("Z", "+00:00")),
            ends_at=datetime.fromisoformat(payload["ends_at"].replace("Z", "+00:00")),
            has_conflict=self._has_conflict(external_id),
        )

    def cancel_appointment(self, external_id: str, reason: str, idempotency_key: str) -> None:
        reason_map = {
            "Feeling better": 10,
            "Condition worse": 20,
            "Sick": 30,
            "Away": 40,
            "Work": 60,
        }
        self._request(
            "PATCH",
            f"/individual_appointments/{external_id}/cancel",
            json={
                "cancellation_reason": reason_map.get(reason, 50),
                "cancellation_note": f"Voice receptionist: {reason}. Idempotency: {idempotency_key}",
                "apply_to_repeats": False,
            },
            expect_json=False,
        )

    def _has_conflict(self, external_id: str) -> bool:
        payload = self._request("GET", f"/individual_appointments/{external_id}/conflicts")
        return bool(payload.get("conflicts", {}).get("exist"))

    def _request(self, method: str, path: str, expect_json: bool = True, **kwargs):
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise PmsError("PMS_TIMEOUT", "Cliniko did not respond in time") from exc
        except httpx.HTTPError as exc:
            raise PmsError("PMS_NETWORK_ERROR", "Could not reach Cliniko") from exc
        if response.status_code >= 400:
            retryable = response.status_code >= 500 or response.status_code == 429
            detail = response.text[:500]
            raise PmsError(
                "PMS_REJECTED_REQUEST",
                f"Cliniko returned {response.status_code}: {detail}",
                retryable=retryable,
            )
        if not expect_json or response.status_code == 204:
            return {}
        return response.json()

    @staticmethod
    def _require_external_ids(*entities) -> None:
        missing = [type(entity).__name__ for entity in entities if not entity.external_id]
        if missing:
            raise PmsError(
                "PMS_MAPPING_MISSING",
                "Missing Cliniko IDs for: " + ", ".join(missing),
                retryable=False,
            )
