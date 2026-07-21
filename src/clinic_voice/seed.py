from __future__ import annotations

import argparse
from datetime import UTC, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from clinic_voice.config import get_settings
from clinic_voice.database import Base, SessionLocal, create_schema, engine
from clinic_voice.models import (
    Appointment,
    AppointmentType,
    AvailabilityOverride,
    AvailabilityRule,
    Branch,
    CallSession,
    OutboundIntent,
    Patient,
    PatientPhone,
    Practitioner,
    PractitionerAppointmentType,
    PractitionerBranch,
)


def seed(reset: bool = False) -> None:
    settings = get_settings()
    if reset:
        Base.metadata.drop_all(bind=engine)
    create_schema()
    with SessionLocal() as db:
        if db.scalar(select(Branch.id).limit(1)):
            print("Seed skipped: clinic data already exists. Use --reset to recreate it.")
            return

        def external(mock_id: str) -> str | None:
            return mock_id if settings.pms_provider == "mock" else None

        central = Branch(
            code="BR_CENTRAL",
            display_name="VoiceCare Clinic – Central Branch",
            address="Demo address; replace with sourced clinic data",
            timezone=settings.clinic_timezone,
            external_id=external("mock-business-central"),
        )
        north = Branch(
            code="BR_NORTH",
            display_name="VoiceCare Clinic – North Branch",
            address="Demo address; replace with sourced clinic data",
            timezone=settings.clinic_timezone,
            external_id=external("mock-business-north"),
        )
        aarav = Practitioner(
            code="DR_GENERAL_1",
            display_name="Dr Aarav Mehta",
            spoken_name="Dr Aarav Mehta",
            specialty="General Medicine",
            external_id=external("mock-practitioner-aarav"),
        )
        nisha = Practitioner(
            code="DR_GENERAL_2",
            display_name="Dr Nisha Verma",
            spoken_name="Dr Nisha Verma",
            specialty="General Medicine",
            external_id=external("mock-practitioner-nisha"),
        )
        kavya = Practitioner(
            code="DR_DERM_1",
            display_name="DR KAVYA IYER",
            spoken_name="Dr Kavya Iyer",
            specialty="Dermatology",
            external_id=external("mock-practitioner-kavya"),
        )
        general_new = AppointmentType(
            code="GENERAL_NEW",
            display_name="General Medicine – New Patient, 30 min",
            specialty="General Medicine",
            patient_duration_minutes=30,
            calendar_duration_minutes=45,
            price_minor=80000,
            currency=settings.clinic_currency,
            external_id=external("mock-type-general-new"),
        )
        general_followup = AppointmentType(
            code="GENERAL_FOLLOWUP",
            display_name="General Medicine – Follow-up, 20 min",
            specialty="General Medicine",
            patient_duration_minutes=20,
            calendar_duration_minutes=30,
            price_minor=50000,
            currency=settings.clinic_currency,
            external_id=external("mock-type-general-followup"),
        )
        derm = AppointmentType(
            code="DERM_CONSULT",
            display_name="Dermatology Consultation, 30 min",
            specialty="Dermatology",
            patient_duration_minutes=30,
            calendar_duration_minutes=45,
            price_minor=100000,
            currency=settings.clinic_currency,
            external_id=external("mock-type-derm"),
        )
        db.add_all([central, north, aarav, nisha, kavya, general_new, general_followup, derm])
        db.flush()
        db.add_all(
            [
                PractitionerBranch(practitioner_id=aarav.id, branch_id=central.id),
                PractitionerBranch(practitioner_id=aarav.id, branch_id=north.id),
                PractitionerBranch(practitioner_id=nisha.id, branch_id=central.id),
                PractitionerBranch(practitioner_id=nisha.id, branch_id=north.id),
                PractitionerBranch(practitioner_id=kavya.id, branch_id=north.id),
                PractitionerAppointmentType(
                    practitioner_id=aarav.id, appointment_type_id=general_new.id
                ),
                PractitionerAppointmentType(
                    practitioner_id=aarav.id, appointment_type_id=general_followup.id
                ),
                PractitionerAppointmentType(
                    practitioner_id=nisha.id, appointment_type_id=general_new.id
                ),
                PractitionerAppointmentType(
                    practitioner_id=nisha.id, appointment_type_id=general_followup.id
                ),
                PractitionerAppointmentType(practitioner_id=kavya.id, appointment_type_id=derm.id),
            ]
        )

        def rule(doctor, branch, weekday, start, end, interval=15):
            return AvailabilityRule(
                practitioner_id=doctor.id,
                branch_id=branch.id,
                weekday=weekday,
                starts_at_local=time.fromisoformat(start),
                ends_at_local=time.fromisoformat(end),
                slot_interval_minutes=interval,
            )

        db.add_all(
            [
                rule(aarav, central, 0, "09:00", "13:30"),
                rule(aarav, central, 2, "09:00", "13:30"),
                rule(aarav, north, 3, "09:00", "13:30"),
                rule(nisha, central, 1, "13:30", "18:30"),
                rule(nisha, central, 3, "13:30", "18:30"),
                rule(nisha, north, 0, "13:30", "18:30"),
                rule(nisha, north, 2, "13:30", "18:30"),
                rule(kavya, north, 1, "09:00", "14:00"),
                rule(kavya, north, 3, "09:00", "14:00"),
                rule(kavya, north, 5, "09:00", "14:00"),
            ]
        )
        dec_13 = datetime(2026, 12, 13, tzinfo=settings.timezone)
        db.add(
            AvailabilityOverride(
                practitioner_id=aarav.id,
                branch_id=north.id,
                local_date=dec_13.astimezone(UTC),
                starts_at_local=time(12, 15),
                ends_at_local=time(14, 30),
                slot_interval_minutes=45,
            )
        )

        def patient(name: str, phone: str, external_id: str, spoken: str | None = None):
            item = Patient(
                full_name=name,
                spoken_name=spoken or name.title(),
                external_id=external(external_id),
            )
            item.phones.append(PatientPhone(phone_e164=phone, is_primary=True))
            db.add(item)
            db.flush()
            return item

        asha = patient("Asha Verma", settings.test_phone_returning, "mock-patient-asha")
        asha.has_prior_pms_history = True
        patient("Riya Sharma", settings.test_phone_family, "mock-patient-riya")
        patient("Kabir Sharma", settings.test_phone_family, "mock-patient-kabir")
        imran = patient("Imran Khan", settings.test_phone_callback, "mock-patient-imran")
        neelam = patient("Neelam Gupta", settings.test_phone_dropped, "mock-patient-neelam")
        patient("ANIL KUMAR", settings.test_phone_caps_name, "mock-patient-anil", "Anil Kumar")

        now = datetime.now(UTC)
        db.add(
            Appointment(
                patient_id=asha.id,
                branch_id=central.id,
                practitioner_id=aarav.id,
                appointment_type_id=general_followup.id,
                starts_at=now - timedelta(days=30),
                ends_at=now - timedelta(days=30) + timedelta(minutes=30),
                status="completed",
                external_id=external("mock-appointment-past-asha"),
                idempotency_key="seed:asha:past",
                pms_sync_status="synced",
            )
        )
        db.add(
            OutboundIntent(
                phone_e164=settings.test_phone_callback,
                patient_id=imran.id,
                purpose="reschedule an existing General Medicine appointment",
                context={"original_context": "Clinic requested a scheduling change"},
                status="awaiting_callback",
            )
        )
        db.add(
            CallSession(
                call_id="seed-dropped-call",
                phone_e164=settings.test_phone_dropped,
                patient_id=neelam.id,
                status="disconnected",
                intent="booking",
                checkpoint={
                    "full_name_captured": True,
                    "specialty": "General Medicine",
                    "preference": "Thursday morning",
                    "stage": "availability_selection",
                },
                ended_reason="network_disconnect",
            )
        )
        db.commit()
        print("Seeded 2 branches, 3 practitioners, 3 appointment types, and test patients.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed deterministic clinic test data")
    parser.add_argument("--reset", action="store_true", help="Drop and recreate all tables")
    args = parser.parse_args()
    try:
        seed(reset=args.reset)
    except OperationalError as exc:
        target = get_settings().database_url.rsplit("@", 1)[-1]
        raise SystemExit(
            "Database connection failed for "
            f"{target}. Start the configured database and verify DATABASE_URL credentials.\n"
            f"Driver error: {exc.orig}"
        ) from None


if __name__ == "__main__":
    main()
