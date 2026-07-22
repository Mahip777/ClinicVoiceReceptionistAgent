from datetime import UTC, date, datetime, time, timedelta

from fastapi.testclient import TestClient

from clinic_voice.database import SessionLocal
from clinic_voice.main import app
from clinic_voice.models import Appointment, OfferedSlot
from clinic_voice.pms import PmsSlot
from clinic_voice.schemas import AvailabilityRequest, TimeWindow

from .helpers import patient_by_phone, service_bundle


def test_dec_13_around_one_resolves_to_one_pm_at_north():
    _, _, _, availability, _ = service_bundle()
    with SessionLocal() as db:
        result = availability.search(
            db,
            AvailabilityRequest(
                specialty="General Medicine",
                appointment_type_code="GENERAL_NEW",
                date_from=date(2026, 12, 13),
                date_to=date(2026, 12, 13),
                around_time=time(13, 0),
                around_minutes=1,
            ),
        )
        assert result.status == "available"
        assert result.slots[0].starts_at.hour == 7  # 13:00 IST stored as UTC
        assert result.slots[0].branch_code == "BR_NORTH"


def test_earliest_search_compares_all_eligible_branches_and_practitioners():
    _, _, _, availability, _ = service_bundle()
    with SessionLocal() as db:
        result = availability.search(
            db,
            AvailabilityRequest(
                specialty="General Medicine",
                appointment_type_code="GENERAL_NEW",
                date_from=date(2026, 12, 10),
                date_to=date(2026, 12, 10),
                earliest=True,
                limit=5,
            ),
        )
        starts = [slot.starts_at for slot in result.slots]
        assert starts == sorted(starts)
        assert result.slots[0].branch_code == "BR_CENTRAL"
        assert result.slots[0].practitioner_code == "DR_GENERAL_2"


def test_search_excludes_cross_branch_slots_for_a_locally_busy_practitioner(monkeypatch):
    settings, pms, _, availability, _ = service_bundle()
    starts_at = datetime(2026, 12, 14, 3, 30, tzinfo=UTC)
    ends_at = starts_at + timedelta(minutes=45)

    def same_time_at_every_branch(*_args, **_kwargs):
        return [PmsSlot(starts_at, ends_at, {"provider": "branch-scoped"})]

    monkeypatch.setattr(pms, "available_times", same_time_at_every_branch)
    request = AvailabilityRequest(
        practitioner_code="DR_GENERAL_1",
        appointment_type_code="GENERAL_NEW",
        date_from=date(2026, 12, 14),
        date_to=date(2026, 12, 14),
        limit=10,
    )

    with SessionLocal() as db:
        initial = availability.search(db, request)
        assert {slot.branch_code for slot in initial.slots} == {"BR_CENTRAL", "BR_NORTH"}
        offered = db.get(OfferedSlot, initial.slots[0].offer_id)
        patient = patient_by_phone(db, settings.test_phone_returning)
        db.add(
            Appointment(
                patient_id=patient.id,
                branch_id=offered.branch_id,
                practitioner_id=offered.practitioner_id,
                appointment_type_id=offered.appointment_type_id,
                starts_at=starts_at,
                ends_at=ends_at,
                status="confirmed",
                external_id="cross-branch-existing",
                idempotency_key="cross-branch-existing",
                pms_sync_status="synced",
            )
        )
        db.commit()

        refreshed = availability.search(db, request)

    assert refreshed.status == "unavailable"
    assert refreshed.code == "NO_AVAILABLE_SLOTS"
    assert refreshed.slots == []


def test_earliest_without_a_date_uses_clinic_local_today():
    _, _, _, availability, _ = service_bundle()
    with SessionLocal() as db:
        result = availability.search(
            db,
            AvailabilityRequest(
                specialty="General Medicine",
                appointment_type_code="GENERAL_NEW",
                earliest=True,
            ),
        )

    assert result.status == "available"
    assert all(
        slot.starts_at.astimezone(service_bundle()[0].timezone).date() >= date(2026, 12, 10)
        for slot in result.slots
    )


def test_branch_specific_specialty_is_deterministic():
    _, _, _, availability, _ = service_bundle()
    with SessionLocal() as db:
        central = availability.search(
            db,
            AvailabilityRequest(
                specialty="Dermatology",
                branch_code="BR_CENTRAL",
                date_from=date(2026, 12, 10),
            ),
        )
        north = availability.search(
            db,
            AvailabilityRequest(
                specialty="Dermatology",
                branch_code="BR_NORTH",
                date_from=date(2026, 12, 10),
                time_window=TimeWindow(start=time(9), end=time(14)),
            ),
        )
        assert central.status == "unavailable"
        assert central.code == "INELIGIBLE_COMBINATION"
        assert north.status == "available"
        assert all(slot.branch_code == "BR_NORTH" for slot in north.slots)


def test_weekday_and_afternoon_constraints_are_applied():
    _, _, _, availability, _ = service_bundle()
    with SessionLocal() as db:
        result = availability.search(
            db,
            AvailabilityRequest(
                specialty="General Medicine",
                date_from=date(2026, 12, 14),
                date_to=date(2026, 12, 16),
                preferred_weekdays=[0, 2],
                around_time=time(16, 30),
                around_minutes=60,
            ),
        )
        assert result.status == "available"
        for slot in result.slots:
            local = slot.starts_at.astimezone(service_bundle()[0].timezone)
            assert local.weekday() in {0, 2}
            assert time(15, 30) <= local.time() <= time(17, 30)


def test_natural_practitioner_name_resolves_without_a_backend_code():
    _, _, _, availability, _ = service_bundle()
    with SessionLocal() as db:
        result = availability.search(
            db,
            AvailabilityRequest(
                practitioner_name="Doctor Nisha",
                appointment_type_code="GENERAL_NEW",
                date_from=date(2026, 12, 10),
            ),
        )
        assert result.status == "available"
        assert all(slot.practitioner_name == "Dr Nisha Verma" for slot in result.slots)


def test_catalog_exposes_only_live_supported_values_and_relationships():
    _, _, _, availability, _ = service_bundle()
    with SessionLocal() as db:
        catalog = availability.catalog(db)

    assert catalog.specialties == ["Dermatology", "General Medicine"]
    assert "ENT" not in catalog.specialties
    kavya = next(item for item in catalog.practitioners if item.name == "Dr Kavya Iyer")
    assert kavya.branch_codes == ["BR_NORTH"]
    assert kavya.appointment_type_codes == ["DERM_CONSULT"]


def test_unsupported_specialty_is_rejected_before_pms_lookup(monkeypatch):
    _, pms, _, availability, _ = service_bundle()

    def unexpected_lookup(*_args, **_kwargs):
        raise AssertionError("PMS availability must not be queried for an unsupported specialty")

    monkeypatch.setattr(pms, "available_times", unexpected_lookup)
    with SessionLocal() as db:
        result = availability.search(
            db,
            AvailabilityRequest(
                specialty="ENT",
                date_from=date(2026, 12, 14),
            ),
        )

    assert result.status == "unavailable"
    assert result.code == "UNSUPPORTED_SPECIALTY"
    assert result.suggestions == ["Dermatology", "General Medicine"]
    assert "does not currently offer ENT" in result.instruction


def test_unknown_doctor_is_not_reported_as_no_slots():
    _, _, _, availability, _ = service_bundle()
    with SessionLocal() as db:
        result = availability.search(
            db,
            AvailabilityRequest(
                practitioner_name="Dr Imaginary Person",
                date_from=date(2026, 12, 14),
            ),
        )

    assert result.code == "UNKNOWN_PRACTITIONER"
    assert "Dr Nisha Verma" in result.suggestions


def test_supported_items_in_an_invalid_combination_are_classified_separately():
    _, _, _, availability, _ = service_bundle()
    with SessionLocal() as db:
        result = availability.search(
            db,
            AvailabilityRequest(
                practitioner_name="Dr Kavya Iyer",
                specialty="General Medicine",
                date_from=date(2026, 12, 14),
            ),
        )

    assert result.code == "INELIGIBLE_COMBINATION"


def test_catalog_tool_endpoint_is_available_to_retell():
    response = TestClient(app).post("/v1/tools/get-clinic-catalog", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "available"
    assert body["specialties"] == ["Dermatology", "General Medicine"]
