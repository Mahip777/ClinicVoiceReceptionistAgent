from __future__ import annotations

from functools import lru_cache

from clinic_voice.config import get_settings

from .base import PmsAdapter
from .cliniko import ClinikoAdapter
from .mock import MockPmsAdapter


@lru_cache
def get_pms_adapter() -> PmsAdapter:
    settings = get_settings()
    if settings.pms_provider == "cliniko":
        return ClinikoAdapter(settings)
    return MockPmsAdapter(settings)
