import pytest

from clinic_voice.eval_harness import (
    evaluate,
    markdown,
    redundant_question_count,
    validate_scenarios,
)


def test_redundant_question_metric_and_language_separation():
    calls = [
        {
            "language": "English",
            "intent": "booking",
            "task_completed": True,
            "booking_confirmed": True,
            "completion_turn": 2,
            "full_name_captured": True,
            "fresh_search_compliant": True,
            "spoken_branch_matches_backend": True,
            "turns": [
                {"role": "agent", "text": "What is your full name?"},
                {"role": "user", "text": "Asha Verma", "provided_fields": ["full_name"]},
                {"role": "agent", "text": "Could I have your name?", "asked_for": ["full_name"]},
            ],
            "latency": {"asr_ms": 100},
        },
        {
            "language": "Hindi",
            "task_completed": False,
            "full_name_captured": True,
            "fresh_search_compliant": True,
            "spoken_branch_matches_backend": True,
            "turns": [],
            "latency": {"asr_ms": 200},
        },
    ]
    report = evaluate(calls)
    assert report["per_language"]["English"]["completion_rate"] == 1
    assert report["per_language"]["Hindi"]["completion_rate"] == 0
    assert redundant_question_count(calls[0]["turns"]) == 1
    assert report["per_language"]["English"]["latency"]["asr_ms"]["p50"] == 100
    assert report["per_language"]["Hindi"]["latency"]["asr_ms"]["p50"] == 200
    assert report["per_language"]["English"]["mean_turns_to_confirmed_booking"] == 2


def test_scenario_validation_requires_actual_multi_turn_scripts():
    valid = {
        "cases": [
            {
                "id": "en",
                "language": "English",
                "script": [
                    {"caller": "Book", "expected": ["ask name"]},
                    {"caller": "Asha Verma", "expected": ["search"]},
                ],
                "success_criteria": ["confirmed"],
            },
            {
                "id": "hi",
                "language": "Hindi",
                "script": [
                    {"caller": "बुक करें", "expected": ["नाम पूछे"]},
                    {"caller": "आशा वर्मा", "expected": ["खोज करे"]},
                ],
                "success_criteria": ["confirmed"],
            },
        ]
    }
    assert validate_scenarios(valid) == {"English": 1, "Hindi": 1}

    valid["cases"][0]["script"] = [{"caller": "Book", "expected": ["ask name"]}]
    with pytest.raises(ValueError, match="at least two"):
        validate_scenarios(valid)


def test_report_includes_measured_call_provenance():
    report = evaluate(
        [
            {
                "scenario_id": "measured_booking",
                "label": "Measured booking",
                "call_ids": ["call_one", "call_two"],
                "language": "Hindi",
                "intent": "booking",
                "task_completed": True,
                "booking_confirmed": True,
                "turns": [],
            }
        ]
    )

    rendered = markdown(report)

    assert report["scenarios"][0]["call_ids"] == ["call_one", "call_two"]
    assert "| Measured booking | Hindi | booking | call_one, call_two | yes |" in rendered
