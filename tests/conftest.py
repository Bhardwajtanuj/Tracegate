from __future__ import annotations

import pytest

from tracegate import AgentHarness, Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


@pytest.fixture
def harness(settings: Settings):
    instance = AgentHarness(settings)
    yield instance
    instance.close()
