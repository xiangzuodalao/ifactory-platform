"""Offline contracts plus the opt-in Phase 2 shadow prediction acceptance."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler

import pytest

from e2e.support import pilot_api


SLOT = "2026-07-29T01:15:00Z"
INTEGRATION_TENANT = "00000000-0000-4000-8000-000000000001"
THINGSBOARD_TENANT = "30000000-0000-4000-8000-000000000001"
OTHER_THINGSBOARD_TENANT = "30000000-0000-4000-8000-000000000002"
ARTIFACTS = {
    (
        "pilot-cnc-vibration",
        "5feeb31058fe0521f94758faa22214afe4619e466e22cbb8c6fb7ffcf2369562",
    ),
    (
        "pilot-injection-pressure",
        "6e3faef72b69bbd7e9562e871f405a38286075461d1b0c3a5d170c72d7e0f1a9",
    ),
    (
        "pilot-robot-position",
        "11f162e58f9958ed405a8c67dcbb780900321385d8c2eb45122367b11ed5016e",
    ),
    (
        "pilot-tightening-torque",
        "dae2043bc142ad6bb984e296d2cff21f6a93ccba49f1e42c02ca26b6e413d014",
    ),
    (
        "pilot-compressor-pressure",
        "5e2959d7617adb4fad4da45702bbef75f11832f8f7f9818c7f2e662e83bccf38",
    ),
    (
        "pilot-eol-pass-rate",
        "5fe0b2a40d796cbbd5e42d67cce20d195c2c67a75795cc9f4390848f1c119377",
    ),
}
DEVICE_PROFILES = (
    ("CNC", "vibration_rms"),
    ("CNC", "vibration_rms"),
    ("CNC", "vibration_rms"),
    ("CNC", "vibration_rms"),
    ("INJECTION_MOLDING", "injection_pressure"),
    ("INJECTION_MOLDING", "injection_pressure"),
    ("INJECTION_MOLDING", "injection_pressure"),
    ("INJECTION_MOLDING", "injection_pressure"),
    ("ASSEMBLY_ROBOT", "position_deviation"),
    ("ASSEMBLY_ROBOT", "position_deviation"),
    ("ASSEMBLY_ROBOT", "position_deviation"),
    ("ASSEMBLY_ROBOT", "position_deviation"),
    ("TIGHTENING", "torque"),
    ("TIGHTENING", "torque"),
    ("TIGHTENING", "torque"),
    ("TIGHTENING", "torque"),
    ("AIR_COMPRESSOR", "discharge_pressure"),
    ("AIR_COMPRESSOR", "discharge_pressure"),
    ("EOL_TESTER", "pass_rate"),
    ("EOL_TESTER", "pass_rate"),
)


def _feature(name: str):
    feature = getattr(pilot_api, name, None)
    assert feature is not None, f"missing Phase 2 acceptance feature: {name}"
    return feature


def _receipt_payload() -> dict[str, object]:
    targets = []
    for index, (device_type, telemetry_key) in enumerate(DEVICE_PROFILES, start=1):
        targets.append(
            {
                "tb_device_id": f"10000000-0000-4000-8000-{index:012d}",
                "equipment_id": f"20000000-0000-4000-8000-{index:012d}",
                "cmms_asset_id": 1000 + index,
                "device_type": device_type,
                "telemetry_key": telemetry_key,
                "body_sha256": f"{index:064x}",
            }
        )
    return {
        "schema_version": 1,
        "applied_at": "2026-07-29T01:00:00+00:00",
        "actor": "codex-isolated-pilot-operator",
        "plan_sha256": "a" * 64,
        "tb_tenant_id": THINGSBOARD_TENANT,
        "target_count": 20,
        "targets": targets,
    }


def _write_receipt(path: Path, payload: object | None = None) -> Path:
    path.write_bytes(
        json.dumps(
            _receipt_payload() if payload is None else payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        + b"\n"
    )
    path.chmod(0o600)
    return path


def _runtime_environment_values() -> dict[str, str]:
    return {
        "INTEGRATION_POSTGRES_DB": "ifactory_integration",
        "INTEGRATION_POSTGRES_USER": "ifactory_integration",
        "INTEGRATION_POSTGRES_PASSWORD": "offline-db-password",
        "PLATFORM_INTEGRATION_TENANT_ALIAS": "ifactory-pilot",
        "PLATFORM_INTEGRATION_TENANT_ID": INTEGRATION_TENANT,
        "PLATFORM_INTEGRATION_ISOLATED_PILOT_MODE": "1",
        "PLATFORM_INTEGRATION_PDM_BASE_URL": "http://pdm:10021",
        "PLATFORM_INTEGRATION_TB_BASE_URL": "http://host.docker.internal:8080",
        "PLATFORM_INTEGRATION_CMMS_BASE_URL": "http://host.docker.internal:3000",
        "PLATFORM_INTEGRATION_TB_TENANT_ID": THINGSBOARD_TENANT,
        "PLATFORM_INTEGRATION_CMMS_COMPANY_ID": "42",
        "PLATFORM_INTEGRATION_PDM_CREDENTIAL_REF": "PILOT_PDM_CREDENTIAL",
        "PLATFORM_INTEGRATION_TB_CREDENTIAL_REF": "PILOT_TB_CREDENTIAL",
        "PLATFORM_INTEGRATION_CMMS_CREDENTIAL_REF": "PILOT_CMMS_CREDENTIAL",
        "PLATFORM_INTEGRATION_CMMS_WEBHOOK_SECRET_REF": (
            "PILOT_CMMS_WEBHOOK_SECRET_FILE"
        ),
        "PILOT_PDM_CREDENTIAL": ('{"kind":"opaque_bearer","value":"pdm-token"}'),
        "PILOT_TB_CREDENTIAL": ('{"kind":"thingsboard_bearer","value":"tb-token"}'),
        "PILOT_CMMS_CREDENTIAL": '{"kind":"cmms_api_key","value":"cmms-key"}',
        "VALEO_PDM_PREDICTION_V2_BEARER_TOKEN": "pdm-token",
        "VALEO_PDM_ALLOWED_TENANT_IDS": INTEGRATION_TENANT,
    }


def _write_runtime_environment(
    path: Path,
    *,
    overrides: dict[str, str] | None = None,
) -> Path:
    values = _runtime_environment_values()
    values.update(overrides or {})
    path.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _summary(payload: dict[str, object] | None = None) -> dict[str, object]:
    receipt = _receipt_payload() if payload is None else payload
    return {
        "tenant_alias": "ifactory-pilot",
        "tenant_id": INTEGRATION_TENANT,
        "scheduled_at": SLOT,
        "status_counts": {"SUCCEEDED": 20},
        "mappings": [
            {
                "equipment_id": target["equipment_id"],
                "meas_code": target["telemetry_key"],
                "tb_device_id": target["tb_device_id"],
                "cmms_asset_id": target["cmms_asset_id"],
            }
            for target in receipt["targets"]
        ],
        "models": [
            {
                "model_profile_id": profile,
                "model_artifact_sha256": artifact,
            }
            for profile, artifact in sorted(ARTIFACTS)
        ],
    }


def _environment():
    environment_type = pilot_api.PilotEnvironment
    return environment_type(
        tb_base_url="http://127.0.0.1:8080",
        cmms_base_url="http://127.0.0.1:3000",
        tb_tenant_id=THINGSBOARD_TENANT,
        cmms_company_id=42,
        tb_bearer="tb-token",
        cmms_api_key="cmms-key",
    )


def test_runtime_environment_uses_one_secure_parse_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reopening the path for dotenv parsing could validate one tenant and consume another."""
    path = _write_runtime_environment(tmp_path / "pilot.env")
    real_dotenv_values = pilot_api.dotenv_values

    def replace_before_dotenv(*args, **kwargs):
        _write_runtime_environment(
            path,
            overrides={"PLATFORM_INTEGRATION_TB_TENANT_ID": OTHER_THINGSBOARD_TENANT},
        )
        return real_dotenv_values(*args, **kwargs)

    monkeypatch.setattr(pilot_api, "dotenv_values", replace_before_dotenv)

    environment = pilot_api.load_pilot_environment(path)

    assert environment.tb_tenant_id == THINGSBOARD_TENANT
    assert environment.runtime_env_path == path.absolute()
    assert environment.runtime_env_fingerprint
    assert "tb-token" not in repr(environment)
    assert "cmms-key" not in repr(environment)


def test_runtime_environment_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    """Opening a FIFO without O_NONBLOCK would hang the confirmation gate indefinitely."""
    fifo = tmp_path / "pilot.env"
    os.mkfifo(fifo, mode=0o600)
    script = """
import sys
from pathlib import Path
from e2e.support.pilot_api import PilotApiError, load_pilot_environment

try:
    load_pilot_environment(Path(sys.argv[1]))
except PilotApiError as error:
    raise SystemExit(0 if error.code == "RUNTIME_ENV_MODE_INVALID" else 3)
raise SystemExit(4)
"""

    result = subprocess.run(
        [sys.executable, "-c", script, str(fifo)],
        cwd=Path(pilot_api.__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
        timeout=2,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_runtime_environment_read_is_size_bounded(tmp_path: Path) -> None:
    """An unbounded runtime file read could exhaust memory before any settings check."""
    path = tmp_path / "pilot.env"
    path.write_bytes(b"#" + b"x" * 65_536)
    path.chmod(0o600)

    with pytest.raises(pilot_api.PilotApiError, match="RUNTIME_ENV_TOO_LARGE"):
        pilot_api.load_pilot_environment(path)


def test_runtime_environment_handles_bounded_short_fd_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assuming one os.read fills the buffer could reject or truncate a valid snapshot."""
    path = _write_runtime_environment(tmp_path / "pilot.env")
    real_read = pilot_api.os.read

    def short_read(descriptor: int, size: int) -> bytes:
        return real_read(descriptor, min(size, 7))

    monkeypatch.setattr(pilot_api.os, "read", short_read)

    environment = pilot_api.load_pilot_environment(path)

    assert environment.tb_tenant_id == THINGSBOARD_TENANT


@pytest.mark.parametrize(
    ("setting", "drift"),
    [
        ("INTEGRATION_POSTGRES_DB", "other_database"),
        ("INTEGRATION_POSTGRES_USER", "other_user"),
        ("PLATFORM_INTEGRATION_TENANT_ALIAS", "other-pilot"),
        (
            "PLATFORM_INTEGRATION_TENANT_ID",
            "00000000-0000-4000-8000-000000000002",
        ),
        ("PLATFORM_INTEGRATION_ISOLATED_PILOT_MODE", "0"),
        ("PLATFORM_INTEGRATION_PDM_BASE_URL", "http://other-pdm:10021"),
        (
            "PLATFORM_INTEGRATION_TB_BASE_URL",
            "http://host.docker.internal:18080",
        ),
        (
            "PLATFORM_INTEGRATION_CMMS_BASE_URL",
            "http://host.docker.internal:13000",
        ),
        ("PLATFORM_INTEGRATION_PDM_CREDENTIAL_REF", "OTHER_PDM_CREDENTIAL"),
        ("PLATFORM_INTEGRATION_TB_CREDENTIAL_REF", "OTHER_TB_CREDENTIAL"),
        ("PLATFORM_INTEGRATION_CMMS_CREDENTIAL_REF", "OTHER_CMMS_CREDENTIAL"),
        (
            "PLATFORM_INTEGRATION_CMMS_WEBHOOK_SECRET_REF",
            "OTHER_WEBHOOK_SECRET",
        ),
        (
            "VALEO_PDM_ALLOWED_TENANT_IDS",
            "00000000-0000-4000-8000-000000000002",
        ),
    ],
)
def test_runtime_settings_drift_stops_before_compose_runner(
    tmp_path: Path,
    setting: str,
    drift: str,
) -> None:
    """Any internal tenant, isolation, URL, or reference drift changes the approved target."""
    path = _write_runtime_environment(
        tmp_path / "pilot.env",
        overrides={setting: drift},
    )
    calls = 0

    def runner(command, **kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(pilot_api.PilotApiError, match="RUNTIME_ENV_SETTINGS_INVALID"):
        environment = pilot_api.load_pilot_environment(path)
        pilot_api.PilotCompose(
            environment=environment,
            runner=runner,
        ).collect_shadow_summary()

    assert calls == 0


def test_runtime_credential_json_rejects_duplicate_keys(tmp_path: Path) -> None:
    """A duplicated credential kind could mask a conflicting envelope in the approved file."""
    path = _write_runtime_environment(
        tmp_path / "pilot.env",
        overrides={
            "PILOT_TB_CREDENTIAL": (
                '{"kind":"wrong","kind":"thingsboard_bearer","value":"tb-token"}'
            )
        },
    )

    with pytest.raises(pilot_api.PilotApiError, match="RUNTIME_CREDENTIAL_INVALID"):
        pilot_api.load_pilot_environment(path)


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("COMPOSE_PROJECT_NAME", "wrong-project"),
        ("COMPOSE_PROFILES", "continuous"),
        ("COMPOSE_FILE", "wrong-compose.yml"),
    ],
)
def test_runtime_environment_rejects_compose_control_variables(
    tmp_path: Path,
    setting: str,
    value: str,
) -> None:
    """Control variables inside --env-file must not retarget or activate the stack."""
    path = _write_runtime_environment(
        tmp_path / "pilot.env",
        overrides={setting: value},
    )

    with pytest.raises(pilot_api.PilotApiError, match="RUNTIME_ENV_SETTINGS_INVALID"):
        pilot_api.load_pilot_environment(path)


def test_confirmed_receipt_is_secure_secret_free_and_exactly_reversible(
    tmp_path: Path,
) -> None:
    """Accepting a loose or credential-bearing receipt could authorize the wrong pilot set."""
    load_receipt = _feature("load_confirmed_seed_receipt")
    receipt = load_receipt(_write_receipt(tmp_path / "receipt.json"))

    assert receipt.tb_tenant_id == THINGSBOARD_TENANT
    assert len(receipt.mappings) == 20
    assert len({mapping.tb_device_id for mapping in receipt.mappings}) == 20
    assert len({mapping.equipment_id for mapping in receipt.mappings}) == 20
    assert len({mapping.cmms_asset_id for mapping in receipt.mappings}) == 20


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    [
        (
            lambda payload: payload.update({"target_count": 19}),
            "SEED_RECEIPT_INVALID",
        ),
        (
            lambda payload: payload["targets"][1].update(
                {"equipment_id": payload["targets"][0]["equipment_id"]}
            ),
            "SEED_RECEIPT_MAPPING_INVALID",
        ),
        (
            lambda payload: payload["targets"][0].update(
                {"authorization": "Bearer should-never-be-stored"}
            ),
            "SEED_RECEIPT_SECRET_FORBIDDEN",
        ),
        (
            lambda payload: payload["targets"][0].update(
                {"telemetry_key": "wrong-measurement"}
            ),
            "SEED_RECEIPT_MAPPING_INVALID",
        ),
    ],
)
def test_confirmed_receipt_rejects_count_identity_secret_and_profile_drift(
    tmp_path: Path,
    mutation,
    error_code: str,
) -> None:
    """Receipt drift must fail before any Compose or provider operation is possible."""
    load_receipt = _feature("load_confirmed_seed_receipt")
    payload = _receipt_payload()
    mutation(payload)

    with pytest.raises(pilot_api.PilotApiError, match=error_code):
        load_receipt(_write_receipt(tmp_path / "receipt.json", payload))


@pytest.mark.parametrize(
    "actor",
    [
        "another-operator",
        "codex-isolated-pilot-operator\x7f",
        "codex-isolated-pilot-operator\x85",
    ],
)
def test_confirmed_receipt_requires_the_fixed_printable_operator(
    tmp_path: Path,
    actor: str,
) -> None:
    """An unbound or control-bearing actor could detach acceptance from its approved plan."""
    load_receipt = _feature("load_confirmed_seed_receipt")
    payload = _receipt_payload()
    payload["actor"] = actor

    with pytest.raises(pilot_api.PilotApiError, match="SEED_RECEIPT_INVALID"):
        load_receipt(_write_receipt(tmp_path / "receipt.json", payload))


@pytest.mark.parametrize("unsafe_kind", ["mode", "symlink", "hardlink"])
def test_confirmed_receipt_rejects_unsafe_files(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    """A replaceable or shared inode could change the confirmed target set after validation."""
    load_receipt = _feature("load_confirmed_seed_receipt")
    source = _write_receipt(tmp_path / "source.json")
    candidate = source
    if unsafe_kind == "mode":
        source.chmod(0o640)
    elif unsafe_kind == "symlink":
        candidate = tmp_path / "receipt.json"
        candidate.symlink_to(source)
    else:
        candidate = tmp_path / "receipt.json"
        os.link(source, candidate)

    with pytest.raises(pilot_api.PilotApiError, match="SEED_RECEIPT_MODE_INVALID"):
        load_receipt(candidate)


def test_confirmed_receipt_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    """Receipt validation must reject a FIFO before any blocking read."""
    fifo = tmp_path / "receipt.json"
    os.mkfifo(fifo, mode=0o600)
    script = """
import sys
from pathlib import Path
from e2e.support.pilot_api import PilotApiError, load_confirmed_seed_receipt

try:
    load_confirmed_seed_receipt(Path(sys.argv[1]))
except PilotApiError as error:
    raise SystemExit(0 if error.code == "SEED_RECEIPT_MODE_INVALID" else 3)
raise SystemExit(4)
"""

    result = subprocess.run(
        [sys.executable, "-c", script, str(fifo)],
        cwd=Path(pilot_api.__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
        timeout=2,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_confirmed_receipt_handles_bounded_short_fd_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A secure receipt reader must continue after a valid short descriptor read."""
    path = _write_receipt(tmp_path / "receipt.json")
    real_read = pilot_api.os.read
    reads = 0

    def short_read(descriptor: int, size: int) -> bytes:
        nonlocal reads
        reads += 1
        return real_read(descriptor, min(size, 11))

    monkeypatch.setattr(pilot_api.os, "read", short_read)

    receipt = pilot_api.load_confirmed_seed_receipt(path)

    assert receipt.tb_tenant_id == THINGSBOARD_TENANT
    assert reads > 2


def test_confirmed_receipt_rejects_metadata_change_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A canonical payload is not evidence if its inode metadata changes mid-read."""
    path = _write_receipt(tmp_path / "receipt.json")
    real_read = pilot_api.os.read
    changed = False

    def read_then_touch(descriptor: int, size: int) -> bytes:
        nonlocal changed
        chunk = real_read(descriptor, min(size, 13))
        if not chunk and not changed:
            metadata = path.stat()
            os.utime(
                path,
                ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000),
            )
            changed = True
        return chunk

    monkeypatch.setattr(pilot_api.os, "read", read_then_touch)

    with pytest.raises(pilot_api.PilotApiError, match="SEED_RECEIPT_CHANGED"):
        pilot_api.load_confirmed_seed_receipt(path)


def test_confirmed_receipt_rejects_truncation_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Truncating the opened receipt while it is read must fail before JSON parsing."""
    path = _write_receipt(tmp_path / "receipt.json")
    original_size = path.stat().st_size
    real_read = pilot_api.os.read
    truncated = False

    def read_then_truncate(descriptor: int, size: int) -> bytes:
        nonlocal truncated
        chunk = real_read(descriptor, min(size, 17))
        if chunk and not truncated:
            os.truncate(path, original_size // 2)
            truncated = True
        return chunk

    monkeypatch.setattr(pilot_api.os, "read", read_then_truncate)

    with pytest.raises(pilot_api.PilotApiError, match="SEED_RECEIPT_CHANGED"):
        pilot_api.load_confirmed_seed_receipt(path)


@pytest.mark.parametrize("depth", [500, 5_000])
def test_confirmed_receipt_maps_deep_json_to_stable_error(
    tmp_path: Path,
    depth: int,
) -> None:
    """Parser, canonicalization, or secret-scan depth must not escape as RecursionError."""
    path = tmp_path / "receipt.json"
    path.write_bytes(b'{"x":' + b"[" * depth + b"0" + b"]" * depth + b"}\n")
    path.chmod(0o600)

    with pytest.raises(pilot_api.PilotApiError, match="SEED_RECEIPT_INVALID"):
        pilot_api.load_confirmed_seed_receipt(path)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_confirmed_receipt_rejects_masked_nonfinite_json(
    tmp_path: Path,
    constant: str,
) -> None:
    """A non-standard value followed by a duplicate valid key must not become evidence."""
    load_receipt = _feature("load_confirmed_seed_receipt")
    canonical = json.dumps(
        _receipt_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    raw = canonical.replace(
        '"target_count":20',
        f'"target_count":{constant},"target_count":20',
    )
    path = tmp_path / "receipt.json"
    path.write_text(raw + "\n", encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(pilot_api.PilotApiError, match="SEED_RECEIPT_INVALID"):
        load_receipt(path)


def test_confirmed_receipt_requires_canonical_bytes(tmp_path: Path) -> None:
    """Equivalent pretty-printed JSON is not the immutable canonical seed evidence."""
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(_receipt_payload(), indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(pilot_api.PilotApiError, match="SEED_RECEIPT_INVALID"):
        pilot_api.load_confirmed_seed_receipt(path)


def test_fixture_manifest_exposes_the_six_exact_phase_gate_artifacts() -> None:
    """Reading a different model object set would make the phase evidence non-reproducible."""
    load_artifacts = _feature("load_fixture_artifact_hashes")

    assert load_artifacts() == frozenset(ARTIFACTS)


def test_shadow_summary_requires_twenty_successes_exact_mappings_and_artifacts(
    tmp_path: Path,
) -> None:
    """Counts alone must not hide a replaced mapping or model artifact."""
    load_receipt = _feature("load_confirmed_seed_receipt")
    validate_summary = _feature("validate_shadow_summary")
    receipt = load_receipt(_write_receipt(tmp_path / "receipt.json"))

    evidence = validate_summary(
        _summary(),
        receipt=receipt,
        expected_artifacts=frozenset(ARTIFACTS),
    )

    assert evidence.total_bindings == 20
    assert evidence.succeeded_runs == 20
    assert evidence.artifact_hashes == frozenset(artifact for _, artifact in ARTIFACTS)


@pytest.mark.parametrize("drift", ["status", "mapping", "artifact"])
def test_shadow_summary_rejects_status_mapping_and_artifact_drift(
    tmp_path: Path,
    drift: str,
) -> None:
    """Any incomplete run, substituted identity, or unapproved object must close the gate."""
    load_receipt = _feature("load_confirmed_seed_receipt")
    validate_summary = _feature("validate_shadow_summary")
    receipt = load_receipt(_write_receipt(tmp_path / "receipt.json"))
    summary = _summary()
    if drift == "status":
        summary["status_counts"] = {"FAILED": 1, "SUCCEEDED": 19}
    elif drift == "mapping":
        summary["mappings"][0]["cmms_asset_id"] = 999999
    else:
        summary["models"][0]["model_artifact_sha256"] = "f" * 64

    with pytest.raises(pilot_api.PilotApiError, match="SHADOW_SUMMARY_INVALID"):
        validate_summary(
            summary,
            receipt=receipt,
            expected_artifacts=frozenset(ARTIFACTS),
        )


def test_compose_child_environment_uses_only_fixed_minimum_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parent interpolation, credential, client-config, and executable paths must not leak in."""
    environment = pilot_api.load_pilot_environment(
        _write_runtime_environment(tmp_path / "pilot.env")
    )
    remote_config = tmp_path / "remote-docker-config"
    remote_config.mkdir(mode=0o700)
    (remote_config / "config.json").write_text(
        '{"currentContext":"remote-production"}',
        encoding="utf-8",
    )
    parent_pollution = {
        **{key: "PARENT_OVERRIDE" for key in _runtime_environment_values()},
        "COMPOSE_PROJECT_NAME": "remote-project",
        "COMPOSE_PROFILES": "continuous",
        "DOCKER_CONFIG": str(remote_config),
        "DOCKER_CONTEXT": "remote-production",
        "DOCKER_HOST": "tcp://remote.invalid:2375",
        "HOME": str(tmp_path / "remote-home"),
        "PATH": str(tmp_path / "remote-bin"),
        "XDG_CONFIG_HOME": str(tmp_path / "remote-xdg"),
    }
    for key, value in parent_pollution.items():
        monkeypatch.setenv(key, value)
    observations: list[tuple[dict[str, str], int, tuple[str, ...]]] = []
    responses = iter(
        [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, json.dumps(_summary()), ""),
        ]
    )

    def runner(command, **kwargs):
        child_environment = dict(kwargs["env"])
        docker_config = Path(child_environment["DOCKER_CONFIG"])
        observations.append(
            (
                child_environment,
                stat.S_IMODE(docker_config.stat().st_mode),
                tuple(sorted(item.name for item in docker_config.iterdir())),
            )
        )
        return next(responses)

    pilot_api.PilotCompose(
        environment=environment,
        runner=runner,
    ).collect_shadow_summary()

    assert len(observations) == 3
    for child_environment, mode, entries in observations:
        assert set(child_environment) == {"DOCKER_CONFIG", "PATH"}
        assert child_environment["PATH"] == (
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        )
        assert child_environment["DOCKER_CONFIG"] != str(remote_config)
        assert mode == 0o700
        assert entries == ()


def test_compose_argv_pins_the_approved_local_docker_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persisted or inherited remote context must not receive pilot credentials."""
    environment = pilot_api.load_pilot_environment(
        _write_runtime_environment(tmp_path / "pilot.env")
    )
    monkeypatch.setenv("DOCKER_CONTEXT", "remote-production")
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote.invalid:2375")
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path / "remote-config"))
    monkeypatch.setenv("HOME", str(tmp_path / "remote-home"))
    commands: list[list[str]] = []
    responses = iter(
        [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, json.dumps(_summary()), ""),
        ]
    )

    def runner(command, **kwargs):
        commands.append(command)
        return next(responses)

    pilot_api.PilotCompose(
        environment=environment,
        runner=runner,
    ).collect_shadow_summary()

    assert len(commands) == 3
    assert all(
        command[:4]
        == [
            "docker",
            "--host",
            "unix:///var/run/docker.sock",
            "compose",
        ]
        for command in commands
    )


def test_compose_helper_runs_only_the_three_fixed_ephemeral_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adding a profile, shell, or mutable command would violate deterministic acceptance."""
    compose_type = _feature("PilotCompose")
    env_path = _write_runtime_environment(tmp_path / "override.env")
    environment = pilot_api.load_pilot_environment(env_path)
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "wrong-project")
    monkeypatch.setenv("COMPOSE_PROFILES", "continuous")
    monkeypatch.setenv("COMPOSE_FILE", "wrong-compose.yml")
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote.invalid:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "remote-context")
    calls: list[tuple[list[str], dict[str, object]]] = []
    responses = iter(
        [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, json.dumps(_summary()), ""),
        ]
    )

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return next(responses)

    summary = compose_type(
        environment=environment,
        runner=runner,
    ).collect_shadow_summary()

    root = Path(pilot_api.__file__).resolve().parents[3]
    prefix = [
        "docker",
        "--host",
        "unix:///var/run/docker.sock",
        "compose",
        "--project-name",
        "predictive-maintenance-shadow",
        "--env-file",
    ]
    command_tails = [
        [
            "run",
            "--rm",
            "--no-deps",
            "scheduler",
            "platform-integration",
            "scheduler",
            "--once",
            "--now",
            SLOT,
        ],
        [
            "run",
            "--rm",
            "--no-deps",
            "prediction-worker",
            "platform-integration",
            "prediction-worker",
            "--once",
            "--now",
            SLOT,
        ],
        [
            "exec",
            "-T",
            "integration-api",
            "platform-integration",
            "shadow-summary",
            "--tenant-alias",
            "ifactory-pilot",
            "--scheduled-at",
            SLOT,
            "--format",
            "json",
        ],
    ]
    assert summary == _summary()
    assert len(calls) == 3
    for (command, kwargs), tail in zip(calls, command_tails, strict=True):
        assert command[:7] == prefix
        assert command[8:10] == [
            "-f",
            str(root / "deploy" / "compose" / "predictive-maintenance-shadow.yml"),
        ]
        assert command[10:] == tail
        pass_fds = kwargs["pass_fds"]
        assert type(pass_fds) is tuple and len(pass_fds) == 1
        assert command[7] == f"/proc/{os.getpid()}/fd/{pass_fds[0]}"
        child_environment = kwargs["env"]
        assert {key: value for key, value in kwargs.items() if key != "env"} == {
            "cwd": root,
            "capture_output": True,
            "text": True,
            "check": False,
            "shell": False,
            "timeout": 180,
            "pass_fds": pass_fds,
        }
        assert set(child_environment) == {"DOCKER_CONFIG", "PATH"}
        assert child_environment["PATH"] == (
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        )
        assert child_environment["DOCKER_CONFIG"].startswith(
            "/tmp/ifactory-docker-config-"
        )
    assert all("--profile" not in command for command, _ in calls)


def test_accelerated_acceptance_binds_both_once_commands_to_the_same_clock_without_a_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping either fixed clock or enabling continuous mode must break acceptance."""
    environment = pilot_api.load_pilot_environment(
        _write_runtime_environment(tmp_path / "pilot.env")
    )
    monkeypatch.setenv("COMPOSE_PROFILES", "continuous")
    calls: list[tuple[list[str], dict[str, object]]] = []
    responses = iter(
        [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, json.dumps(_summary()), ""),
        ]
    )

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return next(responses)

    pilot_api.PilotCompose(
        environment=environment,
        runner=runner,
    ).collect_shadow_summary()

    once_commands = [command for command, _ in calls[:2]]
    assert [command[command.index("--once") :] for command in once_commands] == [
        ["--once", "--now", SLOT],
        ["--once", "--now", SLOT],
    ]
    assert all("--profile" not in command for command in once_commands)
    assert all("COMPOSE_PROFILES" not in kwargs["env"] for _, kwargs in calls)


def test_compose_helper_fails_closed_without_leaking_subprocess_output(
    tmp_path: Path,
) -> None:
    """A failed scheduler must stop before worker execution and redact provider output."""
    compose_type = _feature("PilotCompose")
    environment = pilot_api.load_pilot_environment(
        _write_runtime_environment(tmp_path / "pilot.env")
    )
    calls = 0

    def runner(command, **kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            command,
            17,
            "Bearer leaked-output",
            "upstream stack and password=leaked",
        )

    with pytest.raises(pilot_api.PilotApiError) as caught:
        compose_type(
            environment=environment,
            runner=runner,
        ).collect_shadow_summary()

    assert caught.value.code == "COMPOSE_COMMAND_FAILED"
    assert "leaked" not in str(caught.value)
    assert calls == 1


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_compose_summary_rejects_duplicate_and_nonfinite_json(
    tmp_path: Path,
    constant: str,
) -> None:
    """Default JSON parsing must not let a later valid status mask invalid evidence."""
    environment = pilot_api.load_pilot_environment(
        _write_runtime_environment(tmp_path / "pilot.env")
    )
    canonical = json.dumps(_summary(), sort_keys=True, separators=(",", ":"))
    raw = canonical.replace(
        '"status_counts":{"SUCCEEDED":20}',
        (f'"status_counts":{constant},"status_counts":{{"SUCCEEDED":20}}'),
    )
    responses = iter(
        [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, raw, ""),
        ]
    )

    def runner(command, **kwargs):
        return next(responses)

    with pytest.raises(pilot_api.PilotApiError, match="SHADOW_SUMMARY_INVALID"):
        pilot_api.PilotCompose(
            environment=environment,
            runner=runner,
        ).collect_shadow_summary()


def test_runtime_tenant_mismatch_stops_before_the_first_compose_command(
    tmp_path: Path,
) -> None:
    """A stale receipt from another TB tenant must never schedule this pilot slot."""
    load_receipt = _feature("load_confirmed_seed_receipt")
    compose_type = _feature("PilotCompose")
    receipt = load_receipt(_write_receipt(tmp_path / "receipt.json"))
    environment = pilot_api.load_pilot_environment(
        _write_runtime_environment(
            tmp_path / "pilot.env",
            overrides={"PLATFORM_INTEGRATION_TB_TENANT_ID": OTHER_THINGSBOARD_TENANT},
        )
    )
    calls = 0

    def runner(command, **kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(command, 0, "", "")

    compose = compose_type(environment=environment, runner=runner)
    collect = getattr(compose, "collect_confirmed_shadow_summary", None)
    assert collect is not None, "missing receipt/runtime tenant gate"
    with pytest.raises(
        pilot_api.PilotApiError,
        match="SEED_RECEIPT_TENANT_MISMATCH",
    ):
        collect(receipt=receipt)

    assert calls == 0


def test_replaced_runtime_environment_stops_before_compose_runner(
    tmp_path: Path,
) -> None:
    """Replacing the confirmed env path after loading must not reach scheduler execution."""
    path = _write_runtime_environment(tmp_path / "pilot.env")
    environment = pilot_api.load_pilot_environment(path)
    replacement = _write_runtime_environment(
        tmp_path / "replacement.env",
        overrides={"PLATFORM_INTEGRATION_TB_TENANT_ID": OTHER_THINGSBOARD_TENANT},
    )
    replacement.replace(path)
    calls = 0

    def runner(command, **kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(pilot_api.PilotApiError, match="RUNTIME_ENV_CHANGED"):
        pilot_api.PilotCompose(
            environment=environment,
            runner=runner,
        ).collect_shadow_summary()

    assert calls == 0


def test_runtime_environment_is_rechecked_before_each_compose_command(
    tmp_path: Path,
) -> None:
    """A replacement after scheduler exits must stop before the worker command."""
    path = _write_runtime_environment(tmp_path / "pilot.env")
    environment = pilot_api.load_pilot_environment(path)
    calls = 0

    def runner(command, **kwargs):
        nonlocal calls
        calls += 1
        replacement = _write_runtime_environment(
            tmp_path / "replacement.env",
            overrides={"PLATFORM_INTEGRATION_TB_TENANT_ID": OTHER_THINGSBOARD_TENANT},
        )
        replacement.replace(path)
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(pilot_api.PilotApiError, match="RUNTIME_ENV_CHANGED"):
        pilot_api.PilotCompose(
            environment=environment,
            runner=runner,
        ).collect_shadow_summary()

    assert calls == 1


def test_compose_consumes_the_sealed_verified_env_snapshot(
    tmp_path: Path,
) -> None:
    """Replacing the operator path at runner entry must not affect the first command."""
    path = _write_runtime_environment(tmp_path / "pilot.env")
    original = path.read_bytes()
    environment = pilot_api.load_pilot_environment(path)
    replacement = _write_runtime_environment(
        tmp_path / "replacement.env",
        overrides={"PLATFORM_INTEGRATION_TB_TENANT_ID": OTHER_THINGSBOARD_TENANT},
    )
    observed: dict[str, object] = {}
    calls = 0

    def runner(command, **kwargs):
        nonlocal calls
        calls += 1
        replacement.replace(path)
        env_file = command[command.index("--env-file") + 1]
        observed["argv"] = tuple(command)
        observed["bytes"] = Path(env_file).read_bytes()
        observed["child_environment"] = dict(kwargs["env"])
        observed["env_file"] = env_file
        observed["pass_fds"] = tuple(kwargs.get("pass_fds", ()))
        if observed["pass_fds"]:
            descriptor = observed["pass_fds"][0]
            observed["descriptor"] = descriptor
            observed["seals"] = fcntl.fcntl(descriptor, 1034)
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(pilot_api.PilotApiError, match="RUNTIME_ENV_CHANGED"):
        pilot_api.PilotCompose(
            environment=environment,
            runner=runner,
        ).collect_shadow_summary()

    required_seals = 0x0001 | 0x0002 | 0x0004 | 0x0008
    assert calls == 1
    assert observed["bytes"] == original
    assert observed["env_file"] != str(path)
    assert len(observed["pass_fds"]) == 1
    assert observed["seals"] & required_seals == required_seals
    assert "pdm-token" not in repr(observed["argv"])
    assert "pdm-token" not in repr(observed["child_environment"])
    with pytest.raises(OSError):
        os.fstat(observed["descriptor"])


def test_compose_plugin_reads_parent_snapshot_without_inheriting_the_descriptor(
    tmp_path: Path,
) -> None:
    """A second-hop CLI plugin must read the sealed bytes without inherited extra fds."""
    path = _write_runtime_environment(tmp_path / "pilot.env")
    original = path.read_bytes()
    environment = pilot_api.load_pilot_environment(path)
    replacement = _write_runtime_environment(
        tmp_path / "replacement.env",
        overrides={"PLATFORM_INTEGRATION_TB_TENANT_ID": OTHER_THINGSBOARD_TENANT},
    )
    plugin = r"""
import fcntl
import hashlib
import json
import os
import sys

direct_descriptor = int(sys.argv[1].rsplit("/", 1)[1])
try:
    os.fstat(direct_descriptor)
except OSError as error:
    direct_fd_errno = error.errno
else:
    direct_fd_errno = 0

try:
    descriptor = os.open(sys.argv[1], os.O_RDONLY | os.O_CLOEXEC)
    try:
        chunks = []
        while True:
            chunk = os.read(descriptor, 65_537)
            if not chunk:
                break
            chunks.append(chunk)
        seals = fcntl.fcntl(descriptor, 1034)
    finally:
        os.close(descriptor)
except OSError:
    raise SystemExit(42)

raw = b"".join(chunks)
sys.stdout.write(
    json.dumps(
        {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
            "seals": seals,
            "direct_fd_errno": direct_fd_errno,
        },
        sort_keys=True,
    )
)
"""
    first_hop = r"""
import subprocess
import sys

result = subprocess.run(
    [sys.executable, "-c", sys.argv[1], sys.argv[2]],
    capture_output=True,
    text=True,
    check=False,
    close_fds=True,
    timeout=5,
)
sys.stdout.write(result.stdout)
sys.stderr.write(result.stderr)
raise SystemExit(result.returncode)
"""
    observed: dict[str, object] = {}
    calls = 0

    def runner(command, **kwargs):
        nonlocal calls
        calls += 1
        replacement.replace(path)
        env_file = command[command.index("--env-file") + 1]
        inherited = tuple(kwargs["pass_fds"])
        result = subprocess.run(
            [sys.executable, "-c", first_hop, plugin, env_file],
            cwd=kwargs["cwd"],
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            timeout=min(kwargs["timeout"], 10),
            pass_fds=inherited,
            env=kwargs["env"],
        )
        observed["command"] = tuple(command)
        observed["descriptor"] = inherited[0]
        observed["env_file"] = env_file
        observed["plugin_result"] = result
        return subprocess.CompletedProcess(
            command,
            result.returncode,
            result.stdout,
            result.stderr,
        )

    with pytest.raises(pilot_api.PilotApiError) as raised:
        pilot_api.PilotCompose(
            environment=environment,
            runner=runner,
        ).collect_shadow_summary()

    result = observed["plugin_result"]
    expected_seals = 0x0001 | 0x0002 | 0x0004 | 0x0008
    assert raised.value.code == "RUNTIME_ENV_CHANGED"
    assert calls == 1
    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "sha256": hashlib.sha256(original).hexdigest(),
        "size": len(original),
        "seals": expected_seals,
        "direct_fd_errno": errno.EBADF,
    }
    assert observed["env_file"] == (f"/proc/{os.getpid()}/fd/{observed['descriptor']}")
    assert str(path) not in observed["env_file"]
    assert "pdm-token" not in repr(observed["command"])
    assert "pdm-token" not in observed["env_file"]
    with pytest.raises(OSError):
        os.fstat(observed["descriptor"])
    with pytest.raises(OSError):
        Path(observed["env_file"]).read_bytes()


class _Response:
    def __init__(self, payload: object, *, raw: bytes | None = None) -> None:
        self.status = 200
        self._body = json.dumps(payload).encode() if raw is None else raw

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self, limit: int = -1) -> bytes:
        return self._body if limit < 0 else self._body[:limit]


def test_host_request_supports_only_the_exact_read_only_phase_gate_routes() -> None:
    """The acceptance transport itself must be unable to issue telemetry mutations."""
    host_request = _feature("build_read_only_host_request")
    requests = []

    def opener(request, *, timeout):
        requests.append((request, timeout))
        return _Response({"totalElements": 0})

    request = host_request(_environment(), opener=opener)
    alarm_path = (
        "/api/v2/alarm/DEVICE/10000000-0000-4000-8000-000000000001"
        "?pageSize=1&page=0&statusList=ACTIVE&typeList=PDM_FORECAST_RISK"
    )
    assert request(
        "GET", alarm_path, headers={"X-Authorization": "Bearer tb-token"}
    ) == (
        200,
        {"totalElements": 0},
    )
    assert request(
        "POST",
        "/api/work-orders/search",
        headers={"x-api-key": "cmms-key"},
        body={
            "filterFields": [],
            "direction": "ASC",
            "pageNum": 0,
            "pageSize": 1,
            "sortField": "id",
        },
    ) == (200, {"totalElements": 0})
    assert requests[0][0].full_url.startswith("http://127.0.0.1:8080/")
    assert requests[1][0].full_url == "http://127.0.0.1:3000/api/work-orders/search"
    assert all(timeout == 10 for _, timeout in requests)

    with pytest.raises(pilot_api.PilotApiError, match="PILOT_ROUTE_FORBIDDEN"):
        request(
            "POST",
            "/api/plugins/telemetry/DEVICE/example/timeseries/ANY",
            headers={},
            body=[],
        )
    assert len(requests) == 2


def test_host_request_maps_http_status_without_exposing_error_body() -> None:
    """Provider error bodies may contain secrets or stack traces and must never cross the helper."""
    host_request = _feature("build_read_only_host_request")

    def opener(request, *, timeout):
        raise HTTPError(
            request.full_url,
            503,
            "Bearer upstream-secret",
            hdrs=None,
            fp=None,
        )

    request = host_request(_environment(), opener=opener)
    path = (
        "/api/v2/alarm/DEVICE/10000000-0000-4000-8000-000000000001"
        "?pageSize=1&page=0&statusList=ACTIVE&typeList=PDM_FORECAST_RISK"
    )

    assert request(
        "GET",
        path,
        headers={"X-Authorization": "Bearer tb-token"},
    ) == (503, {})


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_host_request_rejects_duplicate_and_nonfinite_provider_json(
    constant: str,
) -> None:
    """A later zero must not mask duplicate or non-standard provider evidence."""
    host_request = _feature("build_read_only_host_request")
    raw = f'{{"totalElements":{constant},"totalElements":0}}'.encode()

    def opener(request, *, timeout):
        return _Response({}, raw=raw)

    request = host_request(_environment(), opener=opener)
    path = (
        "/api/v2/alarm/DEVICE/10000000-0000-4000-8000-000000000001"
        "?pageSize=1&page=0&statusList=ACTIVE&typeList=PDM_FORECAST_RISK"
    )

    with pytest.raises(
        pilot_api.PilotApiError,
        match="PILOT_PROVIDER_RESPONSE_INVALID",
    ):
        request(
            "GET",
            path,
            headers={"X-Authorization": "Bearer tb-token"},
        )


@pytest.mark.parametrize("provider", ["thingsboard", "cmms"])
def test_default_host_transport_disables_proxy_and_redirects(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    """Neither bearer nor API key may leave its fixed loopback origin through defaults."""
    handlers: list[object] = []
    requests = []

    class RejectingOpener:
        def open(self, request, *, timeout):
            requests.append((request, timeout))
            raise HTTPError(
                request.full_url,
                302,
                "redirect",
                hdrs={"Location": "http://second.invalid/credential"},
                fp=None,
            )

    def build_opener(*configured_handlers):
        handlers.extend(configured_handlers)
        return RejectingOpener()

    def legacy_urlopen(request, *, timeout):
        return RejectingOpener().open(request, timeout=timeout)

    monkeypatch.setattr(pilot_api.urllib.request, "build_opener", build_opener)
    monkeypatch.setattr(pilot_api.urllib.request, "urlopen", legacy_urlopen)
    request = pilot_api.build_read_only_host_request(_environment())
    if provider == "thingsboard":
        status = request(
            "GET",
            (
                "/api/v2/alarm/DEVICE/10000000-0000-4000-8000-000000000001"
                "?pageSize=1&page=0&statusList=ACTIVE"
                "&typeList=PDM_FORECAST_RISK"
            ),
            headers={"X-Authorization": "Bearer tb-token"},
        )
    else:
        status = request(
            "POST",
            "/api/work-orders/search",
            headers={"x-api-key": "cmms-key"},
            body={
                "filterFields": [],
                "direction": "ASC",
                "pageNum": 0,
                "pageSize": 1,
                "sortField": "id",
            },
        )

    assert status == (302, {})
    assert len(requests) == 1
    proxy_handlers = [item for item in handlers if isinstance(item, ProxyHandler)]
    redirect_handlers = [
        item for item in handlers if isinstance(item, HTTPRedirectHandler)
    ]
    assert len(proxy_handlers) == 1
    assert proxy_handlers[0].proxies == {}
    assert len(redirect_handlers) == 1
    assert (
        redirect_handlers[0].redirect_request(
            requests[0][0],
            None,
            302,
            "redirect",
            {},
            "http://second.invalid/credential",
        )
        is None
    )


@pytest.mark.parametrize("dirty_provider", ["thingsboard", "cmms"])
def test_final_isolation_gate_rejects_each_nonzero_provider_without_mutation(
    tmp_path: Path,
    dirty_provider: str,
) -> None:
    """A single alarm or work order must fail acceptance using read-only queries only."""
    load_receipt = _feature("load_confirmed_seed_receipt")
    evaluate = _feature("evaluate_shadow_acceptance")
    receipt = load_receipt(_write_receipt(tmp_path / "receipt.json"))
    calls: list[tuple[str, str]] = []

    def request(method, path, *, headers, body=None):
        calls.append((method, path))
        if "/api/v2/alarm/DEVICE/" in path:
            return 200, {
                "totalElements": 1
                if dirty_provider == "thingsboard" and len(calls) == 1
                else 0
            }
        if path == "/api/work-orders/search":
            return 200, {"totalElements": 1 if dirty_provider == "cmms" else 0}
        raise AssertionError(path)

    api = pilot_api.PilotApi(_environment(), request)
    with pytest.raises(pilot_api.PilotApiError, match="ISOLATION_BASELINE_NOT_CLEAN"):
        evaluate(
            _summary(),
            receipt=receipt,
            expected_artifacts=frozenset(ARTIFACTS),
            api=api,
        )

    assert all(
        method == "GET" or (method == "POST" and path == "/api/work-orders/search")
        for method, path in calls
    )


@pytest.mark.pilot_e2e
def test_confirmed_shadow_prediction_phase_gate(
    pilot_environment,
    pilot_compose,
    confirmed_seed_receipt_file: Path,
) -> None:
    """Run only after the separately confirmed seed receipt exists."""
    receipt = pilot_api.load_confirmed_seed_receipt(confirmed_seed_receipt_file)
    expected_artifacts = pilot_api.load_fixture_artifact_hashes()
    summary = pilot_compose.collect_confirmed_shadow_summary(receipt=receipt)
    api = pilot_api.PilotApi(
        pilot_environment,
        pilot_api.build_read_only_host_request(pilot_environment),
    )
    evidence = pilot_api.evaluate_shadow_acceptance(
        summary,
        receipt=receipt,
        expected_artifacts=expected_artifacts,
        api=api,
    )

    assert evidence.total_bindings == 20
    assert evidence.succeeded_runs == 20
    assert evidence.active_pdm_alarms == 0
    assert evidence.cmms_work_orders == 0
