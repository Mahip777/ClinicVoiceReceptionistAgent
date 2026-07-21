from datetime import date, time

from clinic_voice.database import SessionLocal
from clinic_voice.schemas import AvailabilityRequest, TimeWindow

from .helpers import service_bundle


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
