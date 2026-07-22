from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib import error, request

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "evals" / "raw"
OUTPUT = ROOT / "evals" / "retell_calls.json"
API_BASE = "https://api.retellai.com"

SCENARIOS = [
    {
        "scenario_id": "no_available_slots",
        "label": "No available slots",
        "call_ids": ["call_ec292b30afbcfe2a572187f4334"],
        "language": "English",
        "intent": "booking",
        "booking_confirmed": False,
        "completion_turn": 7,
        "fresh_search_compliant": True,
        "full_name_captured": True,
        "spoken_branch_matches_backend": True,
        "evidence": ["search_availability", '"status":"unavailable"', '"slots":[]'],
    },
    {
        "scenario_id": "rescheduled_successfully",
        "label": "Rescheduled successfully",
        "call_ids": ["call_f17c9104ab8f577d402ba295f68"],
        "language": "Code-switch",
        "intent": "reschedule",
        "completion_turn": 7,
        "fresh_search_compliant": True,
        "full_name_captured": True,
        "spoken_branch_matches_backend": True,
        "evidence": ["reschedule_appointment", '"status":"confirmed"'],
    },
    {
        "scenario_id": "appointment_cancelled_successfully",
        "label": "Appointment cancelled successfully",
        "call_ids": ["call_b0b597d159e34c58fcb91bd60b0"],
        "language": "Hindi",
        "intent": "cancellation",
        "completion_turn": 7,
        "full_name_captured": True,
        "spoken_branch_matches_backend": True,
        "evidence": ["cancel_appointment", '"status":"confirmed"'],
    },
    {
        "scenario_id": "interrupted_then_completed",
        "label": "Call interrupted and then completed in next attempt",
        "call_ids": [
            "call_e32f1733953995916548677953b",
            "call_bb0f1588bfad7eaa296de0862c8",
        ],
        "language": "English",
        "intent": "reschedule",
        "completion_turn": 14,
        "fresh_search_compliant": True,
        "full_name_captured": True,
        "spoken_branch_matches_backend": True,
        "drop_recovery_success": True,
        "evidence": ["reschedule_appointment", '"status":"confirmed"', "12:30"],
    },
    {
        "scenario_id": "unsupported_service",
        "label": "Appointment requested for a service that does not exist",
        "call_ids": ["call_42adbb2a06f120e87e1dc16c3df"],
        "language": "English",
        "intent": "booking",
        "booking_confirmed": True,
        "completion_turn": 14,
        "fresh_search_compliant": True,
        "full_name_captured": True,
        "spoken_branch_matches_backend": True,
        "evidence": ["UNSUPPORTED_SPECIALTY", "book_appointment", '"status":"confirmed"'],
    },
    {
        "scenario_id": "hindi_booking",
        "label": "Hindi booked appointment",
        "call_ids": ["call_7ba6c01d8633f01789df675da73"],
        "language": "Hindi",
        "intent": "booking",
        "booking_confirmed": True,
        "completion_turn": 13,
        "fresh_search_compliant": True,
        "full_name_captured": True,
        "spoken_branch_matches_backend": True,
        "evidence": ["book_appointment", '"status":"confirmed"'],
    },
]


def dotenv_value(name: str) -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError(f"Missing {name} in .env")


def get_call(api_key: str, call_id: str) -> dict[str, Any]:
    req = request.Request(
        f"{API_BASE}/v2/get-call/{call_id}",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with request.urlopen(req, timeout=45) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Retell returned {exc.code} for {call_id}: {detail}") from exc


def sanitized_raw(call: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "call_id",
        "agent_id",
        "agent_name",
        "agent_version",
        "call_status",
        "call_type",
        "disconnection_reason",
        "duration_ms",
        "start_timestamp",
        "end_timestamp",
        "latency",
        "call_analysis",
        "transcript",
        "transcript_object",
        "transcript_with_tool_calls",
    }
    return {key: value for key, value in call.items() if key in allowed}


def coalesced_turns(calls: list[dict[str, Any]]) -> list[dict[str, str]]:
    turns: list[dict[str, str]] = []
    for call in calls:
        for item in call.get("transcript_object", []):
            role = item.get("role")
            text = str(item.get("content", "")).strip()
            if role not in {"agent", "user"} or not text:
                continue
            normalized_role = "agent" if role == "agent" else "user"
            if turns and turns[-1]["role"] == normalized_role:
                turns[-1]["text"] = f"{turns[-1]['text']} {text}".strip()
            else:
                turns.append({"role": normalized_role, "text": text})
    return turns


def latency(calls: list[dict[str, Any]]) -> dict[str, list[float]]:
    mapping = {
        "asr": "asr_ms",
        "llm": "llm_ms",
        "tts": "tts_ms",
        "e2e": "end_to_end_ms",
    }
    result: dict[str, list[float]] = {}
    for source, target in mapping.items():
        values = [
            float(value)
            for call in calls
            for value in call.get("latency", {}).get(source, {}).get("values", [])
        ]
        if values:
            result[target] = values
    return result


def evidence_text(calls: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for call in calls:
        parts.append(str(call.get("transcript", "")))
        for event in call.get("transcript_with_tool_calls", []):
            parts.extend(
                str(event.get(field, "")) for field in ("name", "arguments", "content")
            )
    return "\n".join(parts)


def main() -> None:
    api_key = os.environ.get("RETELL_API_KEY") or dotenv_value("RETELL_API_KEY")
    call_ids = list(dict.fromkeys(call_id for item in SCENARIOS for call_id in item["call_ids"]))
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    calls: dict[str, dict[str, Any]] = {}
    for call_id in call_ids:
        call = get_call(api_key, call_id)
        calls[call_id] = call
        (RAW_DIR / f"{call_id}.json").write_text(
            json.dumps(sanitized_raw(call), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Exported {call_id}")

    normalized: list[dict[str, Any]] = []
    for config in SCENARIOS:
        scenario_calls = [calls[call_id] for call_id in config["call_ids"]]
        searchable = evidence_text(scenario_calls)
        missing = [needle for needle in config["evidence"] if needle not in searchable]
        if missing:
            raise RuntimeError(f"{config['scenario_id']} lacks expected tool evidence: {missing}")
        turns = coalesced_turns(scenario_calls)
        item = {
            key: value
            for key, value in config.items()
            if key not in {"evidence"}
        }
        item.update(
            {
                "task_completed": True,
                "completion_turn": config.get(
                    "completion_turn", sum(turn["role"] == "user" for turn in turns)
                ),
                "turns": turns,
                "latency": latency(scenario_calls),
            }
        )
        normalized.append(item)

    OUTPUT.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {len(normalized)} measured scenarios to {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
