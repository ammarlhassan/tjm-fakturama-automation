from pathlib import Path

import pytest

from image_to_cash.config import Settings
from image_to_cash.models import OrderInput
from image_to_cash.store import Store

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def source():
    return OrderInput.model_validate_json(
        (ROOT / "fixtures/sample.json").read_text(encoding="utf-8")
    )


@pytest.fixture
def settings(tmp_path):
    value = Settings(runtime=tmp_path / "runtime")
    value.prepare()
    return value


@pytest.fixture
def store(settings):
    value = Store(settings.db_url)
    yield value
    value.engine.dispose()
