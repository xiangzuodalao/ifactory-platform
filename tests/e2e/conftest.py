"""Shared strict runtime-environment fixture for opt-in Phase 2 E2E tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from e2e.support.pilot_api import PilotCompose, PilotEnvironment, load_pilot_environment


DEFAULT_PILOT_ENV_FILE = "../.runtime/predictive-maintenance-shadow.env"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--pilot-env-file",
        action="store",
        default=DEFAULT_PILOT_ENV_FILE,
    )
    parser.addoption("--confirmed-seed-receipt", action="store", default=None)


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    if config.getoption("--confirmed-seed-receipt", default=None) is not None:
        return
    marker = pytest.mark.skip(
        reason="pilot E2E requires --confirmed-seed-receipt",
    )
    for item in items:
        if item.get_closest_marker("pilot_e2e") is not None:
            item.add_marker(marker)


@pytest.fixture
def pilot_environment_file(request: pytest.FixtureRequest) -> Path:
    value = request.config.getoption("--pilot-env-file", default=None)
    if type(value) is not str or not value:
        pytest.fail("pilot E2E requires a non-empty --pilot-env-file")
    return Path(value)


@pytest.fixture
def pilot_environment(pilot_environment_file: Path) -> PilotEnvironment:
    return load_pilot_environment(pilot_environment_file)


@pytest.fixture
def pilot_compose(pilot_environment: PilotEnvironment) -> PilotCompose:
    """Bind Compose to the exact secure env snapshot selected by pytest."""
    return PilotCompose(environment=pilot_environment)


@pytest.fixture
def confirmed_seed_receipt_file(request: pytest.FixtureRequest) -> Path:
    value = request.config.getoption("--confirmed-seed-receipt", default=None)
    if type(value) is not str or not value:
        pytest.skip("pilot E2E requires --confirmed-seed-receipt")
    return Path(value)
