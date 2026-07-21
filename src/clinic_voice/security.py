from __future__ import annotations

import hashlib
import hmac


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
