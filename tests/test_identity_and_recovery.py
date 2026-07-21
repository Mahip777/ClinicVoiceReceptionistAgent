from clinic_voice.config import get_settings
from clinic_voice.database import SessionLocal
from clinic_voice.models import Patient
from clinic_voice.schemas import CheckpointRequest
from clinic_voice.services.calls import CallService

from .helpers import service_bundle


def test_shared_phone_requires_name_and_matches_correct_patient():
    settings, _, identity, _, _ = service_bundle()
    with SessionLocal() as db:
        context = identity.caller_context(db, settings.test_phone_family)
        assert context.status == "ambiguous"
        assert len(context.candidates) == 2

        matched = identity.identify(db, settings.test_phone_family, "Kabir Sharma", False)
        assert matched.status == "matched"
        assert matched.spoken_name == "Kabir Sharma"


def test_returning_patient_and_missed_callback_context_are_loaded():
    settings, _, identity, _, _ = service_bundle()
    with SessionLocal() as db:
        returning = identity.caller_context(db, settings.test_phone_returning)
        callback = identity.caller_context(db, settings.test_phone_callback)
        assert returning.returning_patient is True
        assert callback.callback_context["purpose"].startswith("reschedule")


def test_dropped_call_checkpoint_is_resumable():
    settings, _, identity, _, _ = service_bundle()
    with SessionLocal() as db:
        context = identity.caller_context(db, settings.test_phone_dropped)
        assert context.resume_context["stage"] == "availability_selection"
        assert context.resume_context["preference"] == "Thursday morning"


def test_checkpoint_merges_state_instead_of_losing_prior_answers():
    settings = get_settings()
    calls = CallService()
    with SessionLocal() as db:
        calls.checkpoint(
            db,
            CheckpointRequest(
                call_id="merge-test",
                phone_e164=settings.test_phone_returning,
                state={"full_name": "Asha Verma"},
            ),
        )
        result = calls.checkpoint(
            db,
            CheckpointRequest(
                call_id="merge-test",
                phone_e164=settings.test_phone_returning,
                state={"weekday": "Thursday"},
            ),
        )
        assert result.checkpoint == {"full_name": "Asha Verma", "weekday": "Thursday"}


def test_matching_local_patient_is_created_in_pms_when_external_id_is_missing():
    settings, _, identity, _, _ = service_bundle()
    with SessionLocal() as db:
        patient_id = identity.caller_context(
            db, settings.test_phone_returning
        ).candidates[0].patient_id
        patient = db.get(Patient, patient_id)
        patient.external_id = None
        db.commit()

        matched = identity.identify(db, settings.test_phone_returning, "Asha Verma", True)

        assert matched.status == "matched"
        assert db.get(Patient, matched.patient_id).external_id.startswith("mock-patient-")
