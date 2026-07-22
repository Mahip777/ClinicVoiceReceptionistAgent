from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

LATENCY_COMPONENTS = ("asr_ms", "llm_ms", "tts_ms", "tool_ms", "network_ms", "end_to_end_ms")
COMPLIANCE_FIELDS = (
    "fresh_search_compliant",
    "full_name_captured",
    "spoken_branch_matches_backend",
    "drop_recovery_success",
)


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * fraction))
    return round(ordered[index], 2)


def normalized_question(text: str) -> str:
    return " ".join("".join(ch.lower() for ch in text if ch.isalnum() or ch.isspace()).split())


def redundant_question_count(turns: list[dict[str, Any]]) -> int:
    """Count annotated, exact-repeat, and structured semantic re-asks.

    Export converters should populate `provided_fields` on caller turns and `asked_for` on agent
    turns. Exact normalized repeats remain a conservative fallback for unannotated transcripts.
    """
    seen_questions: set[str] = set()
    known_fields: set[str] = set()
    redundant = 0
    for turn in turns:
        if turn.get("role") == "user":
            known_fields.update(str(field) for field in turn.get("provided_fields", []))
            continue
        if turn.get("role") != "agent" or "?" not in turn.get("text", ""):
            continue
        question = normalized_question(turn["text"])
        repeated = bool(question and question in seen_questions)
        semantic_reask = bool(known_fields.intersection(turn.get("asked_for", [])))
        annotated = bool(turn.get("redundant_question"))
        if repeated or semantic_reask or annotated:
            redundant += 1
        if question:
            seen_questions.add(question)
    return redundant


def _latency_values(calls: list[dict[str, Any]], component: str) -> list[float]:
    values: list[float] = []
    for call in calls:
        value = call.get("latency", {}).get(component)
        if value is None:
            continue
        samples = value if isinstance(value, list) else [value]
        values.extend(float(sample) for sample in samples if sample is not None)
    return values


def latency_report(calls: list[dict[str, Any]]) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for component in LATENCY_COMPONENTS:
        values = _latency_values(calls, component)
        report[component] = {
            "count": len(values),
            "p50": percentile(values, 0.50),
            "p95": percentile(values, 0.95),
        }
    return report


def ratio(calls: list[dict[str, Any]], field: str) -> float | None:
    eligible = [item for item in calls if field in item]
    if not eligible:
        return None
    return round(sum(bool(item[field]) for item in eligible) / len(eligible), 3)


def _turns_to_completion(call: dict[str, Any]) -> int | None:
    if not call.get("task_completed"):
        return None
    explicit = call.get("completion_turn")
    if explicit is not None:
        return int(explicit)
    return sum(1 for turn in call.get("turns", []) if turn.get("role") == "user")


def _language_metrics(items: list[dict[str, Any]]) -> dict[str, Any]:
    completion_turns = [value for item in items if (value := _turns_to_completion(item))]
    booking_calls = [item for item in items if item.get("intent") == "booking"]
    confirmed_booking_calls = [item for item in booking_calls if item.get("booking_confirmed")]
    confirmed_turns = [
        value
        for item in confirmed_booking_calls
        if (value := _turns_to_completion(item)) is not None
    ]
    redundant = [redundant_question_count(item.get("turns", [])) for item in items]
    return {
        "calls": len(items),
        "completion_rate": round(sum(bool(item.get("task_completed")) for item in items) / len(items), 3),
        "mean_turns_to_completion": round(statistics.mean(completion_turns), 2)
        if completion_turns
        else None,
        "p95_turns_to_completion": percentile([float(value) for value in completion_turns], 0.95),
        "confirmed_booking_rate": round(len(confirmed_booking_calls) / len(booking_calls), 3)
        if booking_calls
        else None,
        "mean_turns_to_confirmed_booking": round(statistics.mean(confirmed_turns), 2)
        if confirmed_turns
        else None,
        "redundant_questions_total": sum(redundant),
        "redundant_questions_per_call": round(statistics.mean(redundant), 3),
        "calls_with_redundant_question_rate": round(
            sum(value > 0 for value in redundant) / len(redundant), 3
        ),
        "fresh_search_compliance": ratio(items, "fresh_search_compliant"),
        "identity_compliance": ratio(items, "full_name_captured"),
        "branch_match_rate": ratio(items, "spoken_branch_matches_backend"),
        "dropped_call_recovery_rate": ratio(items, "drop_recovery_success"),
        "latency": latency_report(items),
    }


def evaluate(calls: list[dict[str, Any]]) -> dict[str, Any]:
    by_language: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for call in calls:
        by_language[call.get("language", "Unknown")].append(call)
    return {
        "calls": len(calls),
        "scenarios": [
            {
                "scenario_id": item.get("scenario_id", ""),
                "label": item.get("label", item.get("scenario_id", "")),
                "call_ids": item.get("call_ids", []),
                "language": item.get("language", "Unknown"),
                "intent": item.get("intent", ""),
                "task_completed": bool(item.get("task_completed")),
            }
            for item in calls
        ],
        "per_language": {
            language: _language_metrics(items) for language, items in sorted(by_language.items())
        },
        "latency_note": (
            "Latency is reported per language and component. Missing components remain null rather "
            "than being inferred from end-to-end latency."
        ),
        "limitations": [
            "This measured set uses Retell audio calls, but six scenarios cannot represent the full range of accents, background noise, devices, or carrier conditions.",
            "Scripted callers are more cooperative and consistent than real callers.",
            "A successful tool trace does not prove natural voice timing or intelligible speech.",
            "Retell component timings depend on platform instrumentation and may overlap, so they must not be summed as independent stages.",
            "Exact-repeat detection misses paraphrased redundant questions unless turns are annotated with asked_for and provided_fields.",
            "Small language samples have wide uncertainty; English, Hindi, and code-switch results must not be blended.",
            "Separate mock-mode backend tests prove determinism but not Cliniko permissions or carrier behavior.",
            "A warm test endpoint understates free-tier cold-start latency.",
        ],
    }


def validate_scenarios(payload: dict[str, Any]) -> dict[str, int]:
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Scenario catalog must contain a non-empty cases list")
    identifiers: set[str] = set()
    languages: dict[str, int] = defaultdict(int)
    for case in cases:
        identifier = case.get("id")
        if not identifier or identifier in identifiers:
            raise ValueError(f"Scenario id is missing or duplicated: {identifier!r}")
        identifiers.add(identifier)
        language = case.get("language")
        if language not in {"English", "Hindi", "Code-switch"}:
            raise ValueError(f"{identifier}: unsupported language {language!r}")
        script = case.get("script")
        if not isinstance(script, list) or len(script) < 2:
            raise ValueError(f"{identifier}: script must contain at least two caller turns")
        for index, turn in enumerate(script, start=1):
            if not turn.get("caller") or not turn.get("expected"):
                raise ValueError(f"{identifier}: scripted turn {index} needs caller and expected")
        if not case.get("success_criteria"):
            raise ValueError(f"{identifier}: success_criteria is required")
        languages[language] += 1
    for required in ("English", "Hindi"):
        if not languages[required]:
            raise ValueError(f"Scenario catalog requires at least one {required} scenario")
    return dict(languages)


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Voice receptionist evaluation",
        "",
        f"Calls evaluated: {report['calls']}",
        "",
        "All outcome, efficiency, correctness, and latency metrics below are separated by language.",
        "",
        "## Measured Retell scenarios",
        "",
        "| Scenario | Language | Intent | Retell call ID(s) | Completed |",
        "|---|---|---|---|---:|",
    ]
    for scenario in report.get("scenarios", []):
        call_ids = ", ".join(scenario["call_ids"])
        completed = "yes" if scenario["task_completed"] else "no"
        lines.append(
            f"| {scenario['label']} | {scenario['language']} | {scenario['intent']} | "
            f"{call_ids} | {completed} |"
        )
    lines += [
        "",
        "## Per-language outcomes and efficiency",
        "",
        "| Language | Calls | Completion | Confirmed booking | Mean turns/completion | Mean turns/confirmed booking | Redundant questions/call | Calls with redundancy |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for language, item in report["per_language"].items():
        confirmed = item["confirmed_booking_rate"]
        lines.append(
            f"| {language} | {item['calls']} | {item['completion_rate']:.1%} | "
            f"{confirmed:.1%} | {item['mean_turns_to_completion']} | "
            f"{item['mean_turns_to_confirmed_booking']} | "
            f"{item['redundant_questions_per_call']} | "
            f"{item['calls_with_redundant_question_rate']:.1%} |"
            if confirmed is not None
            else f"| {language} | {item['calls']} | {item['completion_rate']:.1%} | n/a | "
            f"{item['mean_turns_to_completion']} | n/a | {item['redundant_questions_per_call']} | "
            f"{item['calls_with_redundant_question_rate']:.1%} |"
        )

    lines += [
        "",
        "## Per-language correctness",
        "",
        "| Language | Fresh search | Full-name identity | Spoken/backend branch | Dropped-call recovery |",
        "|---|---:|---:|---:|---:|",
    ]
    for language, item in report["per_language"].items():
        def display(value: float | None) -> str:
            return "n/a" if value is None else f"{value:.1%}"

        lines.append(
            f"| {language} | {display(item['fresh_search_compliance'])} | "
            f"{display(item['identity_compliance'])} | {display(item['branch_match_rate'])} | "
            f"{display(item['dropped_call_recovery_rate'])} |"
        )

    lines += [
        "",
        "## Per-language component latency",
        "",
        "| Language | Component | Samples | p50 ms | p95 ms |",
        "|---|---|---:|---:|---:|",
    ]
    for language, item in report["per_language"].items():
        for component, values in item["latency"].items():
            lines.append(
                f"| {language} | {component} | {values['count']} | "
                f"{values['p50']} | {values['p95']} |"
            )
    lines += ["", report["latency_note"], "", "## Where this harness gives false confidence", ""]
    lines += [f"- {item}" for item in report["limitations"]]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Report multilingual voice-agent evaluation metrics")
    parser.add_argument("input", type=Path, nargs="?", help="Normalized call-results JSON file")
    parser.add_argument("--output", type=Path, default=Path("eval-results"))
    parser.add_argument("--validate-scenarios", action="store_true")
    args = parser.parse_args()
    if args.validate_scenarios:
        payload = json.loads(Path("retell/test_cases.json").read_text(encoding="utf-8"))
        languages = validate_scenarios(payload)
        print(f"Validated {len(payload['cases'])} multi-turn scenarios: {languages}")
        return
    if not args.input:
        parser.error("input is required unless --validate-scenarios is used")
    calls = json.loads(args.input.read_text(encoding="utf-8"))
    report = evaluate(calls)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (args.output / "report.md").write_text(markdown(report), encoding="utf-8")
    print(markdown(report))


if __name__ == "__main__":
    main()
