from __future__ import annotations

import argparse
import re
from collections.abc import Iterable
from datetime import UTC, datetime

import httpx
from sqlalchemy import delete, select

from clinic_voice.config import get_settings
from clinic_voice.database import SessionLocal, create_schema
from clinic_voice.models import (
    Appointment,
    AppointmentType,
    Branch,
    Patient,
    PatientPhone,
    Practitioner,
    PractitionerAppointmentType,
    PractitionerBranch,
)
from clinic_voice.security import normalize_name, normalize_phone


class ClinikoSync:
    def __init__(self) -> None:
        self.settings = get_settings()
        if not self.settings.cliniko_api_key:
            raise SystemExit("CLINIKO_API_KEY is required")
        self.client = httpx.Client(
            base_url=self.settings.cliniko_api_base_url.rstrip("/"),
            auth=(self.settings.cliniko_api_key, ""),
            headers={"User-Agent": self.settings.cliniko_user_agent, "Accept": "application/json"},
            timeout=self.settings.request_timeout_seconds,
        )

    def list_all(self, path: str, collection: str) -> list[dict]:
        page = 1
        result: list[dict] = []
        while True:
            response = self.client.get(path, params={"page": page, "per_page": 100})
            response.raise_for_status()
            payload = response.json()
            result.extend(payload.get(collection, []))
            if not payload.get("links", {}).get("next"):
                return result
            page += 1

    @staticmethod
    def map_by_name(local_items: Iterable, remote_items: list[dict], remote_name) -> list[str]:
        messages: list[str] = []
        remote_map = {
            normalize_name(remote_name(item)): str(item["id"])
            for item in remote_items
            if remote_name(item)
        }
        for local in local_items:
            match = remote_map.get(normalize_name(local.display_name))
            if match:
                local.external_id = match
                messages.append(f"mapped {local.code}: {match}")
            else:
                messages.append(
                    f"MISSING {local.code}: expected Cliniko name '{local.display_name}'"
                )
        return messages

    def run(self, include_patients: bool = False) -> None:
        create_schema()
        with SessionLocal() as db:
            messages = self.import_catalog(db)
            if include_patients:
                messages += self.import_patients_and_appointments(db)
            db.commit()
        print("\n".join(messages))

    def import_catalog(self, db) -> list[str]:
        """Upsert Cliniko scheduling metadata and retire stale mock catalogue rows."""
        businesses = self.list_all("/businesses", "businesses")
        practitioners = self.list_all("/practitioners", "practitioners")
        appointment_types = self.list_all("/appointment_types", "appointment_types")
        messages: list[str] = []

        for item in db.scalars(select(Branch)).all():
            item.active = False
        for item in db.scalars(select(Practitioner)).all():
            item.active = False
        for item in db.scalars(select(AppointmentType)).all():
            item.active = False

        branch_map: dict[str, Branch] = {}
        for remote in businesses:
            external_id = str(remote["id"])
            name = remote.get("business_name") or remote.get("name") or f"Branch {external_id}"
            branch = self._by_external_or_name(db, Branch, external_id, name)
            if not branch:
                branch = Branch(code=self._unique_code(db, Branch, name), display_name=name)
                db.add(branch)
            branch.display_name = name
            branch.address = ", ".join(
                filter(
                    None,
                    [
                        remote.get("address_1"), remote.get("address_2"),
                        remote.get("city"), remote.get("state_name"), remote.get("post_code"),
                    ],
                )
            ) or None
            branch.timezone = self.settings.clinic_timezone
            branch.external_id = external_id
            branch.active = bool(remote.get("active", True))
            db.flush()
            branch_map[external_id] = branch
            messages.append(f"synced branch {branch.display_name} ({branch.code})")
            if remote.get("show_in_online_bookings") is False:
                messages.append(
                    f"WARNING: {branch.display_name} is hidden from Cliniko online bookings"
                )

        practitioner_map: dict[str, Practitioner] = {}
        for remote in practitioners:
            external_id = str(remote["id"])
            full_name = " ".join(
                filter(None, [remote.get("title"), remote.get("first_name"), remote.get("last_name")])
            ).strip()
            name = full_name or remote.get("display_name") or f"Practitioner {external_id}"
            practitioner = self._by_external_or_name(db, Practitioner, external_id, name)
            if not practitioner:
                practitioner = Practitioner(
                    code=self._unique_code(db, Practitioner, name),
                    display_name=name,
                    spoken_name=name,
                    specialty=self.settings.cliniko_default_specialty,
                )
                db.add(practitioner)
            practitioner.display_name = name
            practitioner.spoken_name = name
            practitioner.specialty = self.settings.cliniko_default_specialty
            practitioner.external_id = external_id
            practitioner.active = bool(remote.get("active", True))
            db.flush()
            practitioner_map[external_id] = practitioner
            messages.append(f"synced practitioner {name} ({practitioner.code})")

        type_map: dict[str, AppointmentType] = {}
        for remote in appointment_types:
            external_id = str(remote["id"])
            name = remote.get("name") or f"Appointment Type {external_id}"
            appointment_type = self._by_external_or_name(db, AppointmentType, external_id, name)
            duration = int(remote.get("duration_in_minutes") or 30)
            if not appointment_type:
                appointment_type = AppointmentType(
                    code=self._unique_code(db, AppointmentType, name),
                    display_name=name,
                    specialty=self.settings.cliniko_default_specialty,
                    patient_duration_minutes=duration,
                    calendar_duration_minutes=duration,
                    price_minor=0,
                    currency=self.settings.clinic_currency,
                )
                db.add(appointment_type)
            appointment_type.display_name = name
            appointment_type.specialty = self.settings.cliniko_default_specialty
            appointment_type.patient_duration_minutes = duration
            appointment_type.calendar_duration_minutes = duration
            appointment_type.external_id = external_id
            appointment_type.active = bool(remote.get("active", True))
            db.flush()
            type_map[external_id] = appointment_type
            messages.append(f"synced appointment type {name} ({appointment_type.code})")

        imported_practitioner_ids = [item.id for item in practitioner_map.values()]
        if imported_practitioner_ids:
            db.execute(
                delete(PractitionerBranch).where(
                    PractitionerBranch.practitioner_id.in_(imported_practitioner_ids)
                )
            )
            db.execute(
                delete(PractitionerAppointmentType).where(
                    PractitionerAppointmentType.practitioner_id.in_(imported_practitioner_ids)
                )
            )

        for external_id, branch in branch_map.items():
            linked = self.list_all(f"/businesses/{external_id}/practitioners", "practitioners")
            linked_count = 0
            for remote in linked:
                practitioner = practitioner_map.get(str(remote["id"]))
                if practitioner:
                    db.add(PractitionerBranch(practitioner_id=practitioner.id, branch_id=branch.id))
                    linked_count += 1
            if not linked_count:
                messages.append(f"WARNING: {branch.display_name} has no practitioners assigned")

        for external_id, practitioner in practitioner_map.items():
            linked = self.list_all(
                f"/practitioners/{external_id}/appointment_types", "appointment_types"
            )
            linked_count = 0
            for remote in linked:
                appointment_type = type_map.get(str(remote["id"]))
                if appointment_type:
                    db.add(
                        PractitionerAppointmentType(
                            practitioner_id=practitioner.id,
                            appointment_type_id=appointment_type.id,
                        )
                    )
                    linked_count += 1
            if not linked_count:
                messages.append(
                    f"WARNING: {practitioner.display_name} has no appointment types assigned"
                )
        return messages

    @staticmethod
    def _by_external_or_name(db, model, external_id: str, display_name: str):
        item = db.scalar(select(model).where(model.external_id == external_id))
        if item:
            return item
        return db.scalar(select(model).where(model.display_name == display_name))

    @staticmethod
    def _unique_code(db, model, display_name: str) -> str:
        base = re.sub(r"[^A-Z0-9]+", "_", display_name.upper()).strip("_")[:42] or "ITEM"
        code = base
        suffix = 2
        while db.scalar(select(model).where(model.code == code)):
            code = f"{base[:45]}_{suffix}"
            suffix += 1
        return code

    def import_patients_and_appointments(self, db) -> list[str]:
        messages: list[str] = []
        existing_by_external = {
            item.external_id: item for item in db.scalars(select(Patient)).all() if item.external_id
        }
        existing_by_name = {
            normalize_name(item.full_name): item for item in db.scalars(select(Patient)).all()
        }
        for remote in self.list_all("/patients", "patients"):
            external_id = str(remote["id"])
            full_name = remote.get("label") or " ".join(
                filter(None, [remote.get("first_name"), remote.get("last_name")])
            )
            patient = existing_by_external.get(external_id) or existing_by_name.get(
                normalize_name(full_name)
            )
            if not patient:
                patient = Patient(
                    full_name=full_name,
                    spoken_name=" ".join(part.capitalize() for part in full_name.split()),
                    external_id=external_id,
                )
                db.add(patient)
                db.flush()
            else:
                patient.external_id = external_id
            patient.has_prior_pms_history = bool(remote.get("latest_booking"))
            known = {item.phone_e164 for item in patient.phones}
            for phone in remote.get("patient_phone_numbers") or []:
                phone_e164 = self._phone_e164(
                    phone.get("normalized_number") or phone.get("number") or "",
                    remote.get("country_code"),
                )
                if phone_e164 and phone_e164 not in known:
                    patient.phones.append(PatientPhone(phone_e164=phone_e164, is_primary=not known))
                    known.add(phone_e164)
            messages.append(f"imported patient {full_name}: {external_id}")

        db.flush()
        entity_maps = {
            "patients": {item.external_id: item for item in db.scalars(select(Patient)).all()},
            "branches": {item.external_id: item for item in db.scalars(select(Branch)).all()},
            "practitioners": {
                item.external_id: item for item in db.scalars(select(Practitioner)).all()
            },
            "types": {item.external_id: item for item in db.scalars(select(AppointmentType)).all()},
        }
        for remote in self.list_all("/individual_appointments", "individual_appointments"):
            external_id = str(remote["id"])
            patient = entity_maps["patients"].get(self._linked_id(remote.get("patient")))
            branch = entity_maps["branches"].get(self._linked_id(remote.get("business")))
            practitioner = entity_maps["practitioners"].get(
                self._linked_id(remote.get("practitioner"))
            )
            appointment_type = entity_maps["types"].get(
                self._linked_id(remote.get("appointment_type"))
            )
            if not all([patient, branch, practitioner, appointment_type]):
                messages.append(f"SKIPPED appointment {external_id}: incomplete resource mapping")
                continue
            starts_at = datetime.fromisoformat(remote["starts_at"].replace("Z", "+00:00"))
            ends_at = datetime.fromisoformat(remote["ends_at"].replace("Z", "+00:00"))
            item = db.scalar(select(Appointment).where(Appointment.external_id == external_id))
            status = (
                "cancelled"
                if remote.get("cancelled_at")
                else "completed"
                if ends_at < datetime.now(UTC)
                else "confirmed"
            )
            if not item:
                item = Appointment(
                    external_id=external_id,
                    idempotency_key=f"cliniko-sync:{external_id}",
                    patient_id=patient.id,
                    branch_id=branch.id,
                    practitioner_id=practitioner.id,
                    appointment_type_id=appointment_type.id,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    status=status,
                    pms_sync_status="synced",
                )
                db.add(item)
            else:
                item.patient_id = patient.id
                item.branch_id = branch.id
                item.practitioner_id = practitioner.id
                item.appointment_type_id = appointment_type.id
                item.starts_at = starts_at
                item.ends_at = ends_at
                item.status = status
            messages.append(f"imported appointment {external_id}")
        return messages

    @staticmethod
    def _linked_id(value: dict | None) -> str | None:
        url = (value or {}).get("links", {}).get("self")
        return url.rstrip("/").rsplit("/", 1)[-1] if url else None

    @staticmethod
    def _phone_e164(value: str, country_code: str | None) -> str:
        normalized = normalize_phone(value)
        if normalized.startswith("+"):
            return normalized
        digits = "".join(character for character in normalized if character.isdigit())
        if country_code == "IN" and len(digits) == 10:
            return "+91" + digits
        return "+" + digits if digits else ""


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Map local clinic codes to an existing Cliniko account"
    )
    parser.add_argument("--include-patients", action="store_true")
    args = parser.parse_args()
    ClinikoSync().run(include_patients=args.include_patients)


if __name__ == "__main__":
    main()
