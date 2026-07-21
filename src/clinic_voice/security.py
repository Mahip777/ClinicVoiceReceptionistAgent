from __future__ import annotations

import hashlib
import hmac
import re
import time


def normalize_phone(value: str) -> str:
    value = value.strip()
    prefix = "+" if value.startswith("+") else ""
    digits = "".join(ch for ch in value if ch.isdigit())
    return prefix + digits


def normalize_name(value: str) -> str:
    return " ".join(value.casefold().split())


def request_hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def secure_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode(), right.encode())


def verify_retell_signature(
    raw_body: bytes,
    api_key: str,
    signature: str | None,
    *,
    now_ms: int | None = None,
) -> bool:
    """Verify Retell's v=<milliseconds>,d=<HMAC-SHA256> webhook signature."""
    if not api_key or not signature:
        return False
    match = re.fullmatch(r"v=(\d+),d=([0-9a-fA-F]+)", signature)
    if not match:
        return False
    timestamp, supplied_digest = match.groups()
    current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    if abs(current_ms - int(timestamp)) > 5 * 60 * 1000:
        return False
    expected = hmac.new(
        api_key.encode(), raw_body + timestamp.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, supplied_digest.casefold())
