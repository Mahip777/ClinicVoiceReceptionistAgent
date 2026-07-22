import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_single_booking_subagent_owns_confirmation_and_execution():
    prompt = (ROOT / "retell" / "booking_subagent_prompt.md").read_text(encoding="utf-8")

    ordered_contract = (
        "slot_selected checkpoint -> one spoken summary/question -> new explicit caller approval "
        "-> booking_confirmed checkpoint -> book_appointment"
    )
    assert ordered_contract in prompt
    assert "Do not transition to another node between these steps" in prompt
    assert "do not repeat the summary or question" in prompt
    assert "Never ask the caller to confirm again because a tool failed" in prompt
    assert 'status="confirmed"' in prompt


def test_booking_subagent_uses_distinct_offer_fields_for_each_checkpoint():
    prompt = (ROOT / "retell" / "booking_subagent_prompt.md").read_text(encoding="utf-8")

    assert 'state.selected_offer_id=<exact latest offer_id>' in prompt
    assert '"confirmed_offer_id": "<exact selected_offer_id>"' in prompt
    assert '"explicit_confirmation": true' in prompt


def test_retell_updater_merges_three_booking_nodes_without_losing_tools():
    script_path = ROOT / "scripts" / "update_retell_booking_flow.py"
    spec = importlib.util.spec_from_file_location("retell_booking_updater", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    flow = {
        "nodes": [
            {
                "name": "Book Appointment",
                "instruction": {"text": "old"},
                "tool_ids": ["catalog", "search", "checkpoint"],
                "edges": [],
            },
            {"name": "Confirm Booking", "tool_ids": ["checkpoint"], "edges": []},
            {"name": "Execute Booking", "tool_ids": ["book"], "edges": []},
            {"name": "Anything Else?", "tool_ids": [], "edges": []},
        ]
    }

    merged = module.merge_booking_nodes(flow)
    names = {node["name"] for node in merged}
    book = next(node for node in merged if node["name"] == "Book Appointment")

    assert names == {"Book Appointment", "Anything Else?"}
    assert book["tool_ids"] == ["catalog", "search", "checkpoint", "book"]
    assert len(book["edges"]) == 4
    assert "one confirmation request" in book["instruction"]["text"]
