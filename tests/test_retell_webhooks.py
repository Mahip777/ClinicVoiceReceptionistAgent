import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from clinic_voice.config import get_settings
from clinic_voice.main import app
from clinic_voice.security import verify_retell_signature


def signed(body: bytes, api_key: str, timestamp: int) -> str:
    digest = hmac.new(
        api_key.encode(), body + str(timestamp).encode(), hashlib.sha256
    ).hexdigest()
    return f"v={timestamp},d={digest}"


def test_retell_signature_verification_and_replay_window():
    body = b'{"event":"call_ended"}'
    timestamp = 1_800_000_000_000
    signature = signed(body, "test-key", timestamp)
    assert verify_retell_signature(body, "test-key", signature, now_ms=timestamp)
    assert not verify_retell_signature(body + b" ", "test-key", signature, now_ms=timestamp)
    assert not verify_retell_signature(body, "test-key", signature, now_ms=timestamp + 300_001)


def test_current_retell_inbound_payload_returns_wrapped_dynamic_variables(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "retell_api_key", "webhook-key")
    payload = {
        "event": "call_inbound",
        "call_inbound": {
            "agent_id": "agent-test",
            "from_number": settings.test_phone_returning,
            "to_number": "+14155550100",
        },
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = 1_800_000_000_000
    signature = signed(body, settings.retell_api_key, timestamp)
    monkeypatch.setattr("clinic_voice.security.time.time", lambda: timestamp / 1000)

    response = TestClient(app).post(
        "/webhooks/retell/inbound",
        content=body,
        headers={"Content-Type": "application/json", "X-Retell-Signature": signature},
    )

    assert response.status_code == 200
    call_inbound = response.json()["call_inbound"]
    assert call_inbound["dynamic_variables"]["caller_status"] == "recognized"
    assert "internal_call_id" in call_inbound["metadata"]


def test_unsigned_production_webhook_is_rejected(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "retell_api_key", "webhook-key")
    response = TestClient(app).post(
        "/webhooks/retell/events",
        json={"event": "call_started", "call": {"call_id": "test"}},
    )
    assert response.status_code == 401
