import os
from pathlib import Path

os.environ["APP_ENV"] = "test"
os.environ["PMS_PROVIDER"] = "mock"
os.environ["DATABASE_URL"] = "sqlite:///./test_clinic_voice.db"
os.environ["SAME_DAY_LEAD_MINUTES"] = "0"
os.environ["OFFER_TTL_SECONDS"] = "600"

import pytest

from clinic_voice.pms.factory import get_pms_adapter
from clinic_voice.seed import seed


@pytest.fixture(autouse=True)
def reset_database():
    get_pms_adapter.cache_clear()
    seed(reset=True)
    yield


def pytest_sessionfinish(session, exitstatus):
    from clinic_voice.database import engine

    engine.dispose()
    Path("test_clinic_voice.db").unlink(missing_ok=True)
