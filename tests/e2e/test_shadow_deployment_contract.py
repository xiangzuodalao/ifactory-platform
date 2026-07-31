"""Offline contracts for the isolated Phase 2 deployment topology."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
COMPOSE_DIR = ROOT / "deploy" / "compose"
SHADOW = COMPOSE_DIR / "predictive-maintenance-shadow.yml"
DISCOVERY = COMPOSE_DIR / "predictive-maintenance-discovery.yml"


def _environment(service: dict[str, object]) -> dict[str, str]:
    value = service["environment"]
    assert isinstance(value, dict)
    return {str(key): str(item) for key, item in value.items()}


def _complete_env(*, identities: bool) -> dict[str, str]:
    values = {
        "INTEGRATION_POSTGRES_DB": "ifactory_integration",
        "INTEGRATION_POSTGRES_USER": "ifactory_integration",
        "INTEGRATION_POSTGRES_PASSWORD": "nonsecret-db-password",
        "PLATFORM_INTEGRATION_TENANT_ALIAS": "ifactory-pilot",
        "PLATFORM_INTEGRATION_TENANT_ID": "00000000-0000-4000-8000-000000000001",
        "PLATFORM_INTEGRATION_ISOLATED_PILOT_MODE": "1",
        "PLATFORM_INTEGRATION_PDM_BASE_URL": "http://pdm:10021",
        "PLATFORM_INTEGRATION_TB_BASE_URL": "http://host.docker.internal:8080",
        "PLATFORM_INTEGRATION_CMMS_BASE_URL": "http://host.docker.internal:3000",
        "PLATFORM_INTEGRATION_PDM_CREDENTIAL_REF": "PILOT_PDM_CREDENTIAL",
        "PLATFORM_INTEGRATION_TB_CREDENTIAL_REF": "PILOT_TB_CREDENTIAL",
        "PLATFORM_INTEGRATION_CMMS_CREDENTIAL_REF": "PILOT_CMMS_CREDENTIAL",
        "PLATFORM_INTEGRATION_CMMS_WEBHOOK_SECRET_REF": "PILOT_CMMS_WEBHOOK_SECRET_FILE",
        "PILOT_PDM_CREDENTIAL": '{"kind":"opaque_bearer","value":"pdm-token"}',
        "PILOT_TB_CREDENTIAL": '{"kind":"thingsboard_bearer","value":"tb-token"}',
        "PILOT_CMMS_CREDENTIAL": '{"kind":"cmms_api_key","value":"cmms-key"}',
        "VALEO_PDM_PREDICTION_V2_BEARER_TOKEN": "pdm-token",
        "VALEO_PDM_ALLOWED_TENANT_IDS": "00000000-0000-4000-8000-000000000001",
    }
    if identities:
        values.update(
            {
                "PLATFORM_INTEGRATION_TB_TENANT_ID": "00000000-0000-4000-8000-000000000002",
                "PLATFORM_INTEGRATION_CMMS_COMPANY_ID": "42",
            }
        )
    return values


def _write_env(path: Path, values: dict[str, str]) -> None:
    path.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8"
    )


def _compose_config(
    compose_file: Path, env_file: Path, *, profiles: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[str]:
    command = [
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "-f",
        str(compose_file),
    ]
    for profile in profiles:
        command.extend(("--profile", profile))
    command.append("config")
    return subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_discovery_compose_is_single_service_and_omits_discovered_identities(
    tmp_path: Path,
) -> None:
    """Removing the isolated discovery boundary must break this topology contract."""
    document = yaml.safe_load(DISCOVERY.read_text(encoding="utf-8"))
    services = document["services"]
    assert set(services) == {"identity-discovery"}
    environment = _environment(services["identity-discovery"])
    assert "PLATFORM_INTEGRATION_TB_TENANT_ID" not in environment
    assert "PLATFORM_INTEGRATION_CMMS_COMPANY_ID" not in environment
    assert environment["PILOT_TB_CREDENTIAL"] == "${PILOT_TB_CREDENTIAL:?required}"
    assert environment["PILOT_CMMS_CREDENTIAL"] == "${PILOT_CMMS_CREDENTIAL:?required}"
    assert "PILOT_PDM_CREDENTIAL" not in environment
    env_file = tmp_path / "discovery.env"
    _write_env(env_file, _complete_env(identities=False))
    configured = _compose_config(DISCOVERY, env_file)
    assert configured.returncode == 0, configured.stderr


def test_main_compose_requires_discovery_identities_without_leaking_values(
    tmp_path: Path,
) -> None:
    """Removing guarded identity interpolation must make the main deployment unsafe."""
    env_file = tmp_path / "main.env"
    values = _complete_env(identities=False)
    _write_env(env_file, values)
    blocked = _compose_config(SHADOW, env_file)
    assert blocked.returncode != 0
    assert (
        "PLATFORM_INTEGRATION_TB_TENANT_ID" in blocked.stderr
        or "PLATFORM_INTEGRATION_CMMS_COMPANY_ID" in blocked.stderr
    )
    assert "nonsecret-db-password" not in blocked.stdout + blocked.stderr

    _write_env(env_file, _complete_env(identities=True))
    configured = _compose_config(SHADOW, env_file, profiles=("continuous",))
    assert configured.returncode == 0, configured.stderr


def test_main_compose_has_minimal_credentials_and_exact_shadow_runtime_contract() -> (
    None
):
    """Changing credentials, profiles, mounts, ports, or dependencies must be visible offline."""
    document = yaml.safe_load(SHADOW.read_text(encoding="utf-8"))
    services = document["services"]
    assert set(services) == {
        "integration-db",
        "pdm",
        "integration-migrate",
        "integration-api",
        "scheduler",
        "prediction-worker",
    }
    for name in (
        "integration-migrate",
        "integration-api",
        "scheduler",
        "prediction-worker",
    ):
        environment = _environment(services[name])
        assert environment["PLATFORM_INTEGRATION_TB_TENANT_ID"] == (
            "${PLATFORM_INTEGRATION_TB_TENANT_ID:?run identity discovery first}"
        )
        assert environment["PLATFORM_INTEGRATION_CMMS_COMPANY_ID"] == (
            "${PLATFORM_INTEGRATION_CMMS_COMPANY_ID:?run identity discovery first}"
        )
        assert "PLATFORM_INTEGRATION_DATABASE_URL" in environment

    api_environment = _environment(services["integration-api"])
    worker_environment = _environment(services["prediction-worker"])
    migrate_environment = _environment(services["integration-migrate"])
    scheduler_environment = _environment(services["scheduler"])
    assert {"PILOT_TB_CREDENTIAL", "PILOT_CMMS_CREDENTIAL"} <= set(api_environment)
    assert {"PILOT_PDM_CREDENTIAL", "PILOT_TB_CREDENTIAL"} <= set(worker_environment)
    assert "PILOT_CMMS_CREDENTIAL" not in worker_environment
    assert not (
        {"PILOT_PDM_CREDENTIAL", "PILOT_TB_CREDENTIAL", "PILOT_CMMS_CREDENTIAL"}
        & set(migrate_environment)
    )
    assert not (
        {"PILOT_PDM_CREDENTIAL", "PILOT_TB_CREDENTIAL", "PILOT_CMMS_CREDENTIAL"}
        & set(scheduler_environment)
    )
    assert services["scheduler"]["profiles"] == ["continuous"]
    assert services["prediction-worker"]["profiles"] == ["continuous"]
    assert services["integration-api"]["ports"] == ["127.0.0.1:18080:8080"]
    assert services["pdm"]["volumes"] == ["../../.runtime/pdm-fixtures:/fixtures:ro"]
    assert (
        _environment(services["pdm"])["VALEO_PDM_PREDICTION_V2_MANIFEST"]
        == "/fixtures/manifest.runtime.yaml"
    )
    assert (
        _environment(services["pdm"])["VALEO_PDM_PREDICTION_V2_OBJECT_ROOT"]
        == "/fixtures/objects"
    )
    assert "readyz" in str(services["pdm"]["healthcheck"])
    for name in (
        "integration-migrate",
        "integration-api",
        "scheduler",
        "prediction-worker",
    ):
        assert "host.docker.internal:host-gateway" in services[name]["extra_hosts"]
    assert (
        services["integration-api"]["depends_on"]["integration-migrate"]["condition"]
        == "service_completed_successfully"
    )
    for name in ("scheduler", "prediction-worker"):
        assert (
            services[name]["depends_on"]["integration-migrate"]["condition"]
            == "service_completed_successfully"
        )
        assert services[name]["depends_on"]["pdm"]["condition"] == "service_healthy"


@pytest.mark.parametrize(
    "missing_key",
    (
        "INTEGRATION_POSTGRES_DB",
        "INTEGRATION_POSTGRES_USER",
        "INTEGRATION_POSTGRES_PASSWORD",
        "VALEO_PDM_PREDICTION_V2_BEARER_TOKEN",
        "VALEO_PDM_ALLOWED_TENANT_IDS",
    ),
)
@pytest.mark.parametrize("empty", (False, True))
def test_main_compose_rejects_missing_or_empty_required_runtime_values_without_echoing_them(
    tmp_path: Path, missing_key: str, empty: bool
) -> None:
    """Replacing guarded interpolation must not disclose a value while accepting an incomplete runtime."""
    values = _complete_env(identities=True)
    sentinel = "never-echo-this-sentinel"
    values["INTEGRATION_POSTGRES_PASSWORD"] = sentinel
    if empty:
        values[missing_key] = ""
    else:
        del values[missing_key]
    env_file = tmp_path / "incomplete.env"
    _write_env(env_file, values)
    blocked = _compose_config(SHADOW, env_file)
    assert blocked.returncode != 0
    assert sentinel not in blocked.stdout + blocked.stderr


def test_rendered_compose_injects_only_nonempty_target_credentials_and_matches_pdm_token(
    tmp_path: Path,
) -> None:
    """Removing an explicit credential mapping or injecting it into an unused role must fail offline."""
    env_file = tmp_path / "complete.env"
    _write_env(env_file, _complete_env(identities=True))
    configured = _compose_config(SHADOW, env_file, profiles=("continuous",))
    assert configured.returncode == 0, configured.stderr
    services = yaml.safe_load(configured.stdout)["services"]
    api = services["integration-api"]["environment"]
    worker = services["prediction-worker"]["environment"]
    migrate = services["integration-migrate"]["environment"]
    scheduler = services["scheduler"]["environment"]
    assert all(api[name] for name in ("PILOT_TB_CREDENTIAL", "PILOT_CMMS_CREDENTIAL"))
    assert api["PLATFORM_INTEGRATION_PDM_CREDENTIAL_REF"] == "PILOT_PDM_CREDENTIAL"
    assert "PILOT_PDM_CREDENTIAL" not in api
    assert all(worker[name] for name in ("PILOT_PDM_CREDENTIAL", "PILOT_TB_CREDENTIAL"))
    for environment in (migrate, scheduler):
        assert not (
            {"PILOT_PDM_CREDENTIAL", "PILOT_TB_CREDENTIAL", "PILOT_CMMS_CREDENTIAL"}
            & set(environment)
        )
    assert (
        json.loads(worker["PILOT_PDM_CREDENTIAL"])["value"]
        == services["pdm"]["environment"]["VALEO_PDM_PREDICTION_V2_BEARER_TOKEN"]
    )
