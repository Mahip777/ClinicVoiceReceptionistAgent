from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
API_BASE = "https://api.retellai.com"


def dotenv_value(name: str) -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError(f"Missing {name} in .env")


def request(api_key: str, method: str, path: str, body: dict[str, Any] | None = None):
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=payload,
        method=method,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Retell {method} {path} returned {exc.code}: {detail}") from exc


def merge_booking_nodes(flow: dict[str, Any]) -> list[dict[str, Any]]:
    by_name = {node.get("name"): node for node in flow["nodes"]}
    required = {"Book Appointment", "Confirm Booking", "Execute Booking"}
    if not required.issubset(by_name):
        raise RuntimeError(f"Expected the three-node booking draft; found {sorted(by_name)}")

    book = by_name["Book Appointment"]
    confirm = by_name["Confirm Booking"]
    execute = by_name["Execute Booking"]
    book["instruction"]["text"] = (ROOT / "retell" / "booking_subagent_prompt.md").read_text(
        encoding="utf-8"
    )
    book["tool_ids"] = list(
        dict.fromkeys(book.get("tool_ids", []) + confirm.get("tool_ids", []) + execute.get("tool_ids", []))
    )
    book["edges"] = [
        {
            "id": "edge-booking-complete-single-node",
            "transition_condition": {
                "type": "prompt",
                "prompt": (
                    "book_appointment returned status=confirmed and the agent gave at most one "
                    "concise confirmation, or returned pending_sync and the agent explained it "
                    "once. Transition immediately; never repeat the final confirmation even if "
                    "interrupted or thanked."
                ),
            },
            "destination_node_id": "node-1784399834638",
        },
        {
            "id": "edge-booking-identity-single-node",
            "transition_condition": {
                "type": "prompt",
                "prompt": (
                    "Appointment-patient identity is missing, ambiguous, or changed. Do not search "
                    "or book; transition for identity resolution."
                ),
            },
            "destination_node_id": "node-1784306714774",
        },
        {
            "id": "edge-booking-human-single-node",
            "transition_condition": {
                "type": "prompt",
                "prompt": (
                    "The caller requests a human, the request is unsupported, or the booking "
                    "transaction has an unrecoverable tool failure after the allowed retry."
                ),
            },
            "destination_node_id": "node-1784306711927",
        },
        {
            "id": "edge-booking-stop-single-node",
            "transition_condition": {
                "type": "prompt",
                "prompt": "The caller explicitly declines the booking and does not want another option.",
            },
            "destination_node_id": "node-1784399834638",
        },
    ]
    return [node for node in flow["nodes"] if node.get("name") not in required - {"Book Appointment"}]


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge Retell's three booking nodes safely")
    parser.add_argument("--publish", action="store_true", help="publish after the returned graph verifies")
    args = parser.parse_args()

    api_key = os.environ.get("RETELL_API_KEY") or dotenv_value("RETELL_API_KEY")
    agent_id = os.environ.get("RETELL_AGENT_ID") or dotenv_value("RETELL_AGENT_ID")
    print("Loading the latest Retell agent draft.", flush=True)
    agent = request(api_key, "GET", f"/get-agent/{agent_id}")
    if agent["is_published"]:
        raise RuntimeError(f"Latest agent version {agent['version']} is published; create a draft first")

    flow_id = agent["response_engine"]["conversation_flow_id"]
    flow_version = agent["response_engine"]["version"]
    flow = request(api_key, "GET", f"/get-conversation-flow/{flow_id}?version={flow_version}")
    nodes = merge_booking_nodes(flow)
    print(f"Updating flow {flow_id} v{flow_version} with {len(nodes)} nodes.", flush=True)
    updated = request(
        api_key,
        "PATCH",
        f"/update-conversation-flow/{flow_id}?version={flow_version}",
        {"nodes": nodes},
    )

    updated_by_name = {node.get("name"): node for node in updated["nodes"]}
    book = updated_by_name.get("Book Appointment")
    valid = (
        book is not None
        and "Confirm Booking" not in updated_by_name
        and "Execute Booking" not in updated_by_name
        and len(book.get("tool_ids", [])) >= 4
        and "slot_selected checkpoint -> one spoken summary/question" in book["instruction"]["text"]
    )
    if not valid:
        raise RuntimeError("Retell returned an unexpected graph; refusing to publish")
    print(f"Verified flow v{updated['version']}: merged booking node has {len(book['tool_ids'])} tools.", flush=True)

    if not args.publish:
        print("Draft updated but not published.", flush=True)
        return

    published = request(
        api_key,
        "POST",
        f"/publish-agent-version/{agent_id}",
        {
            "version": agent["version"],
            "version_title": "Single-node booking confirmation fix",
            "version_description": (
                "Keep selection, one confirmation, checkpoint, and booking in one subagent to "
                "prevent transition-driven confirmation repeats."
            ),
        },
    )
    print(f"Published agent {published['agent_id']} version {published['version']}.", flush=True)


if __name__ == "__main__":
    main()
