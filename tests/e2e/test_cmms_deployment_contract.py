"""Offline contract for the isolated CMMS Compose control boundary."""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

from e2e.support import SafeRuntimeFixture
from ifactory_cmms_deploy.compose import (
    DockerCompose,
    ImageReceipt,
    resolve_default_bridge_gateway,
)
from ifactory_cmms_deploy.config import RuntimeConfig
from ifactory_cmms_deploy.errors import DeploymentError
from ifactory_cmms_deploy.gateway import Gateway
from ifactory_cmms_deploy.process import CommandResult
from ifactory_cmms_deploy.records import (
    ActionCode,
    ActionTargetKind,
    BootstrapPlanBindings,
    ClaimedApplyContext,
    CredentialIdentity,
    DeploymentPlan,
    GatewayMode,
    InvitationProbePlanBinding,
    LicenseMode,
    Operation,
    PlannedAction,
    RuntimeProfile,
    claim_plan_application,
    load_confirmed_plan,
    reserve_plan_attempt,
    write_plan,
)


ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "deploy/compose/cmms-development.yml"
GATEWAY_TEMPLATE = ROOT / "deploy/gateway/cmms-development-nginx.conf.template"
ROUTES_MANIFEST = ROOT / "deploy/cmms/manifests/runtime-api-key-routes.json"
COMPOSE_TARGET = "compose:ifactory-cmms-dev"
PINNED_IMAGES = {
    "postgres": (
        "postgres:16-alpine@sha256:"
        "7a396fd264a2067788b6551122b50f162bf6136312c7fc9d74381cb92c648382"
    ),
    "minio": (
        "minio/minio:RELEASE.2025-04-22T22-12-26Z@sha256:"
        "3f97c5651cb6662b880c787a232b6b34fec8d8922e08d6617b25d241a21164bb"
    ),
    "nginx": (
        "nginx:1.27.0-alpine@sha256:"
        "a377278b7dde3a8012b25d141d025a88dbf9f5ed13c5cdf21ee241e7ec07ab57"
    ),
}
EXPECTED_RESOURCES = {
    "postgres": ("1.0", "1g", 256),
    "minio": ("1.0", "1g", 256),
    "nginx": ("0.5", "256m", 128),
}
SENTINEL_SECRET = "cmms-compose-secret-2a6541"
_F_GET_SEALS = 1034
_REQUIRED_MEMFD_SEALS = 0x000F
PROBE_SLOT = "11111111-1111-4111-8111-111111111111"


def _document() -> dict[str, Any]:
    value = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    assert type(value) is dict
    return value


def _environment(service: dict[str, Any]) -> dict[str, str]:
    raw = service.get("environment", {})
    if type(raw) is dict:
        assert all(
            type(key) is str and type(value) is str
            for key, value in raw.items()
        )
        return dict(raw)
    assert type(raw) is list
    result: dict[str, str] = {}
    for row in raw:
        assert type(row) is str and row.count("=") == 1
        key, value = row.split("=", 1)
        assert key not in result
        result[key] = value
    return result


def _short_volume_rows(service: dict[str, Any]) -> set[tuple[str, str, bool]]:
    result: set[tuple[str, str, bool]] = set()
    for row in service.get("volumes", []):
        if type(row) is str:
            source, target, *options = row.split(":")
            result.add((source, target, "ro" in options))
            continue
        assert type(row) is dict
        result.add(
            (
                row["source"],
                row["target"],
                bool(row.get("read_only", False)),
            )
        )
    return result


def _health_test(service: dict[str, Any]) -> tuple[str, ...]:
    health = service["healthcheck"]
    raw = health["test"]
    assert type(raw) is list and all(type(value) is str for value in raw)
    return tuple(raw)


def test_compose_contains_only_state_and_gateway_services() -> None:
    document = _document()

    assert document["name"] == "ifactory-cmms-dev"
    assert set(document["services"]) == {"postgres", "minio", "nginx"}
    assert all(
        "container_name" not in service
        for service in document["services"].values()
    )
    assert document["services"]["nginx"]["network_mode"] == "host"
    assert "ports" not in document["services"]["nginx"]
    assert all(
        "network_mode" not in document["services"][name]
        for name in ("postgres", "minio")
    )
    assert "networks" not in document


def test_compose_uses_only_exact_amd64_digest_images() -> None:
    document = _document()

    assert {
        name: service["image"]
        for name, service in document["services"].items()
    } == PINNED_IMAGES
    assert all(
        service["platform"] == "linux/amd64"
        for service in document["services"].values()
    )
    raw = COMPOSE_FILE.read_text(encoding="utf-8").casefold()
    assert "components/cmms/api" not in raw
    assert "components/cmms/frontend" not in raw
    assert "thingsboard" not in raw
    assert "build:" not in raw


def test_postgres_contract_is_loopback_secret_file_and_one_owned_volume() -> None:
    postgres = _document()["services"]["postgres"]

    assert postgres["ports"] == ["127.0.0.1:5433:5432"]
    assert _environment(postgres) == {
        "POSTGRES_USER": "${POSTGRES_USER:?POSTGRES_USER is required}",
        "POSTGRES_DB": "${POSTGRES_DB:?POSTGRES_DB is required}",
        "POSTGRES_PASSWORD_FILE": "/run/secrets/postgres_password",
    }
    assert postgres["secrets"] == ["postgres_password"]
    assert _short_volume_rows(postgres) == {
        ("postgres_data", "/var/lib/postgresql/data", False)
    }
    test = _health_test(postgres)
    assert test[0] == "CMD-SHELL"
    assert test[1] == 'pg_isready -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'
    assert "password" not in test[1].casefold()


def test_minio_contract_is_loopback_secret_file_and_shell_free() -> None:
    minio = _document()["services"]["minio"]

    assert minio["ports"] == [
        "127.0.0.1:9000:9000",
        "127.0.0.1:9001:9001",
    ]
    assert _environment(minio) == {
        "MINIO_ROOT_USER_FILE": "/run/secrets/minio_root_user",
        "MINIO_ROOT_PASSWORD_FILE": "/run/secrets/minio_root_password",
        "MINIO_REGION_NAME": "us-east-1",
    }
    assert minio["secrets"] == ["minio_root_user", "minio_root_password"]
    assert minio["command"] == ["server", "/data", "--console-address", ":9001"]
    assert all(token not in {"sh", "bash", "-c"} for token in minio["command"])
    assert _short_volume_rows(minio) == {("minio_data", "/data", False)}
    assert _health_test(minio) == (
        "CMD",
        "curl",
        "-f",
        "http://127.0.0.1:9000/minio/health/live",
    )


def test_compose_secret_sources_are_only_validated_file_references() -> None:
    document = _document()

    assert document["secrets"] == {
        "postgres_password": {
            "file": "${POSTGRES_PASSWORD_FILE:?POSTGRES_PASSWORD_FILE is required}"
        },
        "minio_root_user": {
            "file": "${MINIO_ROOT_USER_FILE:?MINIO_ROOT_USER_FILE is required}"
        },
        "minio_root_password": {
            "file": "${MINIO_ROOT_PASSWORD_FILE:?MINIO_ROOT_PASSWORD_FILE is required}"
        },
    }
    raw = COMPOSE_FILE.read_text(encoding="utf-8")
    for forbidden in (
        "JWT_SECRET_KEY",
        "LICENSE_KEY",
        "ALLOWED_ORGANIZATION_ADMINS",
        "API_KEY",
        "pls_change_me",
    ):
        assert forbidden not in raw


def test_nginx_contract_is_unprivileged_read_only_and_runtime_mount_visible() -> None:
    nginx = _document()["services"]["nginx"]

    assert nginx["user"] == "101:101"
    assert nginx["read_only"] is True
    assert nginx["cap_drop"] == ["ALL"]
    assert nginx["security_opt"] == ["no-new-privileges:true"]
    assert nginx["entrypoint"] == ["/usr/sbin/nginx"]
    assert nginx["command"] == [
        "-g",
        "daemon off;",
        "-c",
        "/etc/ifactory-cmms/nginx.conf",
    ]
    assert nginx["tmpfs"] == ["/tmp:uid=101,gid=101,mode=0700"]
    mounts = _short_volume_rows(nginx)
    assert len(mounts) == 1
    source, target, read_only = mounts.pop()
    assert source == "${CMMS_NGINX_RUNTIME_DIR:?CMMS_NGINX_RUNTIME_DIR is required}"
    assert target == "/etc/ifactory-cmms"
    assert read_only is True
    assert _health_test(nginx) == (
        "CMD",
        "/usr/sbin/nginx",
        "-t",
        "-q",
        "-c",
        "/etc/ifactory-cmms/nginx.conf",
    )


def test_all_healthchecks_and_resource_limits_are_bounded_exactly() -> None:
    services = _document()["services"]

    duration = re.compile(r"(?P<amount>[1-9][0-9]*)(?P<unit>ms|s|m)\Z")
    multipliers = {"ms": 0.001, "s": 1.0, "m": 60.0}
    for name, service in services.items():
        health = service["healthcheck"]
        assert set(health) == {"test", "interval", "timeout", "start_period", "retries"}
        for key in ("interval", "timeout", "start_period"):
            match = duration.fullmatch(health[key])
            assert match is not None
            seconds = int(match.group("amount")) * multipliers[match.group("unit")]
            assert 0 < seconds <= 300
        assert type(health["retries"]) is int
        assert 1 <= health["retries"] <= 30
        cpus, memory, pids = EXPECTED_RESOURCES[name]
        assert str(service["cpus"]) == cpus
        assert service["mem_limit"] == memory
        assert service["pids_limit"] == pids


def test_named_volumes_are_owned_and_never_external() -> None:
    document = _document()

    assert document["volumes"] == {"postgres_data": {}, "minio_data": {}}
    assert all(
        not definition.get("external", False)
        for definition in document["volumes"].values()
    )


def _private(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def _runtime_config(root: Path) -> RuntimeConfig:
    secrets = root / ".runtime" / "secrets"
    secret_paths = {
        name: _private(secrets / name, f"{SENTINEL_SECRET}-{name}".encode())
        for name in (
            "postgres-password",
            "minio-root-user",
            "minio-root-password",
            "jwt-secret",
            "license-key",
            "license-file",
        )
    }
    nginx_runtime = root / ".runtime" / "cmms-nginx"
    nginx_runtime.mkdir(parents=True, mode=0o755)
    nginx_runtime.chmod(0o755)
    runtime_rows = (
        ("LICENSE_MODE", "offline"),
        ("POSTGRES_USER", "atlas_user"),
        ("POSTGRES_DB", "atlas"),
        ("POSTGRES_PASSWORD_FILE", str(secret_paths["postgres-password"])),
        ("MINIO_ROOT_USER_FILE", str(secret_paths["minio-root-user"])),
        ("MINIO_ROOT_PASSWORD_FILE", str(secret_paths["minio-root-password"])),
        ("JWT_SECRET_KEY_FILE", str(secret_paths["jwt-secret"])),
        ("LICENSE_KEY_FILE", str(secret_paths["license-key"])),
        ("LICENSE_FILE_PATH", str(secret_paths["license-file"])),
        ("ALLOWED_ORGANIZATION_ADMINS", "orgadmin@example.test"),
    )
    _private(
        root / ".runtime/cmms-development.env",
        ("\n".join(f"{key}={value}" for key, value in runtime_rows) + "\n").encode(),
    )
    _private(
        root / ".runtime/cmms-frontend.env",
        b"HOST=127.0.0.1\nPORT=3001\nAPI_URL=/api\n",
    )
    return RuntimeConfig.load(root)


def _stage_compose_root(tmp_path: Path) -> Path:
    compose = tmp_path / "deploy" / "compose" / COMPOSE_FILE.name
    compose.parent.mkdir(parents=True)
    compose.write_bytes(COMPOSE_FILE.read_bytes())
    template = tmp_path / "deploy" / "gateway" / GATEWAY_TEMPLATE.name
    template.parent.mkdir(parents=True)
    template.write_bytes(GATEWAY_TEMPLATE.read_bytes())
    manifest = tmp_path / "deploy/cmms/manifests" / ROUTES_MANIFEST.name
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(ROUTES_MANIFEST.read_bytes())
    return tmp_path


@dataclass
class InspectingRunner:
    outcomes: deque[CommandResult] = field(default_factory=deque)
    calls: list[Any] = field(default_factory=list)
    env_snapshots: list[dict[str, str]] = field(default_factory=list)
    docker_config_paths: list[Path] = field(default_factory=list)
    docker_config_identities: list[tuple[int, int]] = field(default_factory=list)
    memfd_seals: list[int] = field(default_factory=list)
    compose_file_paths: list[Path] = field(default_factory=list)
    compose_file_bytes: list[bytes] = field(default_factory=list)
    compose_file_seals: list[int] = field(default_factory=list)
    secret_fd_paths: list[tuple[Path, ...]] = field(default_factory=list)
    secret_source_targets: list[dict[str, Path]] = field(default_factory=list)
    nginx_runtime_fd_paths: list[Path] = field(default_factory=list)
    nginx_runtime_fd_counts: list[int] = field(default_factory=list)
    nginx_validation_paths: list[Path] = field(default_factory=list)
    nginx_validation_bytes: list[bytes] = field(default_factory=list)
    nginx_validation_seals: list[int] = field(default_factory=list)

    @classmethod
    def succeeding(cls, count: int = 1) -> "InspectingRunner":
        return cls(deque(CommandResult(0, "", "") for _ in range(count)))

    def run(self, spec: Any) -> CommandResult:
        self.calls.append(spec)
        if spec.safe_label == "cmms-nginx-pre-up-validate":
            mount = spec.argv[spec.argv.index("--mount") + 1]
            fields = dict(row.split("=", 1) for row in mount.split(",")[:-1])
            nginx_path = Path(fields["source"])
            assert nginx_path.as_posix().startswith(f"/proc/{os.getpid()}/fd/")
            descriptor = os.open(nginx_path, os.O_RDONLY | os.O_CLOEXEC)
            try:
                metadata = os.fstat(descriptor)
                assert stat.S_ISREG(metadata.st_mode)
                assert stat.S_IMODE(metadata.st_mode) == 0o444
                self.nginx_validation_bytes.append(os.read(descriptor, 256 * 1024))
                self.nginx_validation_seals.append(
                    fcntl.fcntl(descriptor, _F_GET_SEALS)
                )
            finally:
                os.close(descriptor)
            self.nginx_validation_paths.append(nginx_path)
        if "-f" in spec.argv:
            compose_path = Path(spec.argv[spec.argv.index("-f") + 1])
            descriptor = os.open(compose_path, os.O_RDONLY | os.O_CLOEXEC)
            try:
                self.compose_file_bytes.append(os.read(descriptor, 64 * 1024))
                self.compose_file_seals.append(
                    fcntl.fcntl(descriptor, _F_GET_SEALS)
                )
            finally:
                os.close(descriptor)
            self.compose_file_paths.append(compose_path)
        if "--env-file" in spec.argv:
            env_path = Path(spec.argv[spec.argv.index("--env-file") + 1])
            descriptor = os.open(env_path, os.O_RDONLY | os.O_CLOEXEC)
            try:
                raw = os.read(descriptor, 64 * 1024)
                self.memfd_seals.append(fcntl.fcntl(descriptor, _F_GET_SEALS))
            finally:
                os.close(descriptor)
            rows = raw.decode("utf-8").splitlines()
            parsed: dict[str, str] = {}
            for row in rows:
                assert row.count("=") == 1
                key, value = row.split("=", 1)
                assert key not in parsed
                parsed[key] = value
            self.env_snapshots.append(parsed)
            held_secret_paths: list[Path] = []
            secret_source_targets: dict[str, Path] = {}
            for key in (
                "POSTGRES_PASSWORD_FILE",
                "MINIO_ROOT_USER_FILE",
                "MINIO_ROOT_PASSWORD_FILE",
            ):
                secret_path = Path(parsed[key])
                assert secret_path.is_absolute()
                assert not secret_path.as_posix().startswith("/proc/")
                assert secret_path.resolve(strict=True) == secret_path
                metadata = secret_path.stat()
                assert stat.S_ISREG(metadata.st_mode)
                assert stat.S_IMODE(metadata.st_mode) == 0o600
                assert metadata.st_uid == os.getuid()
                assert metadata.st_nlink == 1
                matching_descriptors: list[int] = []
                for entry in Path(f"/proc/{os.getpid()}/fd").iterdir():
                    try:
                        descriptor = int(entry.name)
                        opened = os.fstat(descriptor)
                    except (OSError, ValueError):
                        continue
                    if (opened.st_dev, opened.st_ino) == (
                        metadata.st_dev,
                        metadata.st_ino,
                    ):
                        matching_descriptors.append(descriptor)
                assert len(matching_descriptors) == 1
                descriptor = matching_descriptors[0]
                flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
                assert flags & os.O_ACCMODE == os.O_RDONLY
                assert flags & os.O_NONBLOCK == os.O_NONBLOCK
                held_path = Path(f"/proc/{os.getpid()}/fd/{descriptor}")
                held_secret_paths.append(held_path)
                secret_source_targets[key] = secret_path
            self.secret_fd_paths.append(tuple(held_secret_paths))
            self.secret_source_targets.append(secret_source_targets)

            nginx_runtime = Path(parsed["CMMS_NGINX_RUNTIME_DIR"])
            runtime_metadata = nginx_runtime.stat()
            matching_directories: list[Path] = []
            for entry in Path(f"/proc/{os.getpid()}/fd").iterdir():
                try:
                    opened = os.fstat(int(entry.name))
                except (OSError, ValueError):
                    continue
                if (
                    stat.S_ISDIR(opened.st_mode)
                    and (opened.st_dev, opened.st_ino)
                    == (runtime_metadata.st_dev, runtime_metadata.st_ino)
                ):
                    matching_directories.append(entry)
            assert matching_directories
            self.nginx_runtime_fd_paths.append(matching_directories[0])
            self.nginx_runtime_fd_counts.append(len(matching_directories))
        docker_config = Path(spec.environment["DOCKER_CONFIG"])
        metadata = docker_config.stat()
        assert stat.S_ISDIR(metadata.st_mode)
        assert stat.S_IMODE(metadata.st_mode) == 0o700
        assert metadata.st_uid == os.getuid()
        assert list(docker_config.iterdir()) == []
        self.docker_config_paths.append(docker_config)
        self.docker_config_identities.append((metadata.st_dev, metadata.st_ino))
        if not self.outcomes:
            raise AssertionError("unexpected process invocation")
        return self.outcomes.popleft()


def _assert_compose_prefix(spec: Any, root: Path) -> tuple[str, ...]:
    argv = spec.argv
    assert argv[:7] == (
        "/usr/bin/docker",
        "--host",
        "unix:///var/run/docker.sock",
        "compose",
        "--project-name",
        "ifactory-cmms-dev",
        "--env-file",
    )
    env_path = argv[7]
    assert env_path.startswith(f"/proc/{os.getpid()}/fd/")
    assert argv[8:12] == (
        "--project-directory",
        str(root),
        "-f",
        argv[11],
    )
    compose_path = argv[11]
    assert compose_path.startswith(f"/proc/{os.getpid()}/fd/")
    assert dict(spec.environment) == {
        "PATH": "/usr/bin:/bin",
        "DOCKER_CONFIG": str(Path(spec.environment["DOCKER_CONFIG"])),
    }
    assert spec.cwd == root
    return argv[12:]


def _assert_sealed_snapshot(
    snapshot: dict[str, str],
    config: RuntimeConfig,
    root: Path,
    source_targets: dict[str, Path],
) -> None:
    assert set(snapshot) == {
        "POSTGRES_USER",
        "POSTGRES_DB",
        "POSTGRES_PASSWORD_FILE",
        "MINIO_ROOT_USER_FILE",
        "MINIO_ROOT_PASSWORD_FILE",
        "CMMS_NGINX_RUNTIME_DIR",
    }
    assert snapshot["POSTGRES_USER"] == config.postgres_user
    assert snapshot["POSTGRES_DB"] == config.postgres_db
    assert snapshot["CMMS_NGINX_RUNTIME_DIR"] == str(root / ".runtime/cmms-nginx")
    expected_sources = {
        "POSTGRES_PASSWORD_FILE": config.postgres_password_file,
        "MINIO_ROOT_USER_FILE": config.minio_root_user_file,
        "MINIO_ROOT_PASSWORD_FILE": config.minio_root_password_file,
    }
    assert source_targets == expected_sources
    assert {key: Path(snapshot[key]) for key in expected_sources} == expected_sources
    assert len({snapshot[key] for key in expected_sources}) == 3
    assert SENTINEL_SECRET not in repr(snapshot)


def _expected_persistent_mount_snapshot(
    config: RuntimeConfig, root: Path
) -> dict[str, str]:
    return {
        "POSTGRES_USER": config.postgres_user,
        "POSTGRES_DB": config.postgres_db,
        "POSTGRES_PASSWORD_FILE": str(config.postgres_password_file),
        "MINIO_ROOT_USER_FILE": str(config.minio_root_user_file),
        "MINIO_ROOT_PASSWORD_FILE": str(config.minio_root_password_file),
        "CMMS_NGINX_RUNTIME_DIR": str(root / ".runtime/cmms-nginx"),
    }


def test_config_quiet_uses_sealed_cli_snapshot_and_persistent_mount_paths(
    tmp_path: Path,
) -> None:
    root = _stage_compose_root(tmp_path)
    config = _runtime_config(root)
    runner = InspectingRunner.succeeding()

    DockerCompose(
        root=root,
        runner=runner,
        controller_pid=os.getpid(),
    ).config_quiet(config)

    assert len(runner.calls) == 1
    assert _assert_compose_prefix(runner.calls[0], root) == ("config", "-q")
    _assert_sealed_snapshot(
        runner.env_snapshots[0],
        config,
        root,
        runner.secret_source_targets[0],
    )
    assert runner.env_snapshots[0] == _expected_persistent_mount_snapshot(config, root)
    assert (
        runner.memfd_seals[0] & _REQUIRED_MEMFD_SEALS
        == _REQUIRED_MEMFD_SEALS
    )
    assert runner.compose_file_bytes == [COMPOSE_FILE.read_bytes()]
    assert (
        runner.compose_file_seals[0] & _REQUIRED_MEMFD_SEALS
        == _REQUIRED_MEMFD_SEALS
    )
    assert all(not path.exists() for path in runner.docker_config_paths)
    assert all(not path.exists() for path in runner.compose_file_paths)
    assert len(runner.secret_fd_paths) == 1
    assert all(not path.exists() for path in runner.secret_fd_paths[0])
    assert all(not path.exists() for path in runner.nginx_runtime_fd_paths)


@pytest.mark.parametrize(
    "result",
    [
        pytest.param(CommandResult(1, "", "unsafe diagnostic"), id="nonzero"),
        pytest.param(CommandResult(0, "rendered config", ""), id="stdout"),
        pytest.param(CommandResult(0, "", "unexpected warning"), id="stderr"),
    ],
)
def test_config_quiet_rejects_nonquiet_or_failed_fake_results(
    tmp_path: Path,
    result: CommandResult,
) -> None:
    root = _stage_compose_root(tmp_path)
    runner = InspectingRunner(deque([result]))

    with pytest.raises(DeploymentError) as caught:
        DockerCompose(root=root, runner=runner).config_quiet(_runtime_config(root))

    assert caught.value.code == "CMMS-E041"
    assert "unsafe diagnostic" not in str(caught.value)
    assert "rendered config" not in str(caught.value)


def _action(
    code: ActionCode,
    kind: ActionTargetKind | None = None,
    target: str | None = None,
) -> PlannedAction:
    return PlannedAction(code, kind, target)


def _claim(
    safe_runtime: SafeRuntimeFixture,
    *,
    config: RuntimeConfig,
    operation: Operation,
    actions: tuple[PlannedAction, ...],
    bindings: BootstrapPlanBindings | None,
    nonce: str,
) -> tuple[ClaimedApplyContext, Any]:
    snapshot = replace(
        safe_runtime.make_snapshot(),
        config_sha256=config.config_sha256,
    )
    plan = DeploymentPlan.create(
        snapshot=snapshot,
        operation=operation,
        profile=RuntimeProfile.DEVELOPMENT,
        license_mode=LicenseMode.OFFLINE,
        now=safe_runtime.make_plan().created_at,
        actions=actions,
        bootstrap_bindings=bindings,
        plan_nonce=nonce,
    )
    plan_path, digest = write_plan(plan, safe_runtime.plans_dir)
    reservation = reserve_plan_attempt(
        plan_path,
        confirmed_sha256=digest,
        now=plan.created_at,
        plans_dir=safe_runtime.plans_dir,
        application_id=("f" if nonce[0] != "f" else "e") * 32,
    )
    lease = safe_runtime.acquire_deployment_write_lease(reservation)
    confirmed = load_confirmed_plan(
        reservation,
        snapshot=plan.snapshot,
        current_bootstrap_bindings=plan.bootstrap_bindings,
        lease=lease,
    )
    evidence = safe_runtime.gateway_authority(confirmed, lease).issue()
    return (
        claim_plan_application(confirmed, safe_runtime.plans_dir, lease, evidence),
        lease,
    )


def _start_actions() -> tuple[PlannedAction, ...]:
    return (
        _action(ActionCode.GATEWAY_FAIL_CLOSED),
        _action(
            ActionCode.COMPOSE_START_STATE_GATEWAY,
            ActionTargetKind.COMPOSE_RESOURCE_SET,
            COMPOSE_TARGET,
        ),
        _action(ActionCode.LICENSE_VERIFY_OFFLINE),
        _action(ActionCode.PROCESS_CREATE_API_PERMIT),
        _action(ActionCode.PROCESS_START_API),
        _action(ActionCode.PROCESS_START_FRONTEND),
        _action(ActionCode.READINESS_REQUIRE_LOOPBACK),
        _action(ActionCode.GATEWAY_ENABLE_DUAL),
        _action(ActionCode.READINESS_REQUIRE_DUAL),
    )


def _repair_pull_actions() -> tuple[PlannedAction, ...]:
    return (
        _action(ActionCode.GATEWAY_FAIL_CLOSED),
        _action(ActionCode.IMAGES_PULL_EXACT),
        _action(ActionCode.LICENSE_VERIFY_OFFLINE),
        _action(ActionCode.PROCESS_CREATE_API_PERMIT),
        _action(ActionCode.PROCESS_START_API),
        _action(ActionCode.PROCESS_START_FRONTEND),
        _action(ActionCode.READINESS_REQUIRE_API_LOOPBACK),
        _action(ActionCode.READINESS_REQUIRE_LOOPBACK),
        _action(ActionCode.GATEWAY_ENABLE_DUAL),
        _action(ActionCode.READINESS_REQUIRE_DUAL),
    )


def _fresh_bootstrap_bindings(
    safe_runtime: SafeRuntimeFixture,
) -> BootstrapPlanBindings:
    base = safe_runtime.bootstrap_plan_bindings
    assert base.credentials is not None
    credentials = tuple(
        replace(
            row,
            current_file=None,
            candidate_file=safe_runtime.stat_binding(
                f"credential:{row.identity.value}:candidate",
                500 + index,
            ),
        )
        for index, row in enumerate(base.credentials)
    )
    return replace(
        base,
        credentials=credentials,
        invitation_probe=InvitationProbePlanBinding(
            slot_id=PROBE_SLOT,
            canonical_email="probe@example.test",
            descriptor_file=safe_runtime.stat_binding("probe:descriptor", 601),
            password_file=safe_runtime.stat_binding("probe:password", 602),
        ),
    )


def _bootstrap_actions() -> tuple[PlannedAction, ...]:
    return (
        _action(ActionCode.GATEWAY_FAIL_CLOSED),
        _action(ActionCode.IMAGES_PULL_EXACT),
        _action(
            ActionCode.COMPOSE_CREATE_STATE_GATEWAY,
            ActionTargetKind.COMPOSE_RESOURCE_SET,
            COMPOSE_TARGET,
        ),
        _action(ActionCode.LICENSE_VERIFY_OFFLINE),
        _action(ActionCode.PROCESS_CREATE_API_PERMIT),
        _action(ActionCode.PROCESS_START_API),
        _action(ActionCode.CMMS_INITIALIZE_FRESH_DATABASE),
        _action(ActionCode.PROCESS_START_FRONTEND),
        _action(ActionCode.READINESS_REQUIRE_API_LOOPBACK),
        _action(
            ActionCode.BOOTSTRAP_ROTATE_SUPER_ADMIN,
            ActionTargetKind.IDENTITY,
            CredentialIdentity.SUPER_ADMIN.value,
        ),
        _action(
            ActionCode.BOOTSTRAP_CREATE_ORGANIZATION,
            ActionTargetKind.IDENTITY,
            CredentialIdentity.ORGANIZATION_ADMIN.value,
        ),
        _action(
            ActionCode.BOOTSTRAP_CREATE_ROLE,
            ActionTargetKind.ROLE_EXTERNAL_ID,
            "ifactory-pdm-runtime",
        ),
        _action(
            ActionCode.BOOTSTRAP_PROBE_INVITATION,
            ActionTargetKind.INVITATION_PROBE_SLOT,
            PROBE_SLOT,
        ),
        _action(
            ActionCode.BOOTSTRAP_CREATE_INVITATION,
            ActionTargetKind.IDENTITY,
            CredentialIdentity.RUNTIME_USER.value,
        ),
        _action(
            ActionCode.BOOTSTRAP_CREATE_RUNTIME_IDENTITY,
            ActionTargetKind.IDENTITY,
            CredentialIdentity.RUNTIME_USER.value,
        ),
        _action(
            ActionCode.BOOTSTRAP_CREATE_API_KEY,
            ActionTargetKind.API_KEY_LABEL,
            "ifactory-pdm-runtime",
        ),
        _action(
            ActionCode.BOOTSTRAP_FINALIZE_ROLE,
            ActionTargetKind.ROLE_EXTERNAL_ID,
            "ifactory-pdm-runtime",
        ),
        _action(
            ActionCode.BOOTSTRAP_PUBLISH_PHASE2_KEY,
            ActionTargetKind.PHASE2_ENV,
            "predictive-maintenance-shadow.env",
        ),
        _action(ActionCode.READINESS_REQUIRE_LOOPBACK),
        _action(ActionCode.GATEWAY_ENABLE_DUAL),
        _action(ActionCode.READINESS_REQUIRE_DUAL),
    )


@pytest.fixture
def safe_runtime(tmp_path: Path) -> SafeRuntimeFixture:
    return SafeRuntimeFixture(tmp_path / "plans")


def test_start_plan_uses_start_without_pull_or_recreate(
    tmp_path: Path,
    safe_runtime: SafeRuntimeFixture,
) -> None:
    root = _stage_compose_root(tmp_path / "compose-root")
    config = _runtime_config(root)
    context, lease = _claim(
        safe_runtime,
        config=config,
        operation=Operation.START,
        actions=_start_actions(),
        bindings=safe_runtime.bootstrap_plan_bindings,
        nonce="1" * 32,
    )
    runner = InspectingRunner.succeeding()
    try:
        DockerCompose(root=root, runner=runner).up_state_and_gateway(context)
    finally:
        lease.close()

    suffix = _assert_compose_prefix(runner.calls[0], root)
    assert suffix == ("start", "postgres", "minio", "nginx")
    assert "pull" not in suffix
    assert "up" not in suffix
    _assert_sealed_snapshot(
        runner.env_snapshots[0],
        config,
        root,
        runner.secret_source_targets[0],
    )


def test_stop_preserves_named_volumes_and_uses_exact_services(
    tmp_path: Path,
    safe_runtime: SafeRuntimeFixture,
) -> None:
    root = _stage_compose_root(tmp_path / "compose-root")
    config = _runtime_config(root)
    stop_plan = safe_runtime.make_plan(
        operation=Operation.STOP,
        plan_nonce="2" * 32,
    )
    context, lease = _claim(
        safe_runtime,
        config=config,
        operation=Operation.STOP,
        actions=stop_plan.actions,
        bindings=None,
        nonce="3" * 32,
    )
    runner = InspectingRunner.succeeding()
    try:
        DockerCompose(root=root, runner=runner).stop_preserving_volumes(context)
    finally:
        lease.close()

    suffix = _assert_compose_prefix(runner.calls[0], root)
    assert suffix == ("stop", "nginx", "minio", "postgres")
    forbidden = {"down", "-v", "--volumes", "volume", "rm", "prune", "*"}
    assert forbidden.isdisjoint(suffix)


def test_pull_requires_visible_exact_plan_action_before_runner_call(
    tmp_path: Path,
    safe_runtime: SafeRuntimeFixture,
) -> None:
    root = _stage_compose_root(tmp_path / "compose-root")
    config = _runtime_config(root)
    context, lease = _claim(
        safe_runtime,
        config=config,
        operation=Operation.START,
        actions=_start_actions(),
        bindings=safe_runtime.bootstrap_plan_bindings,
        nonce="4" * 32,
    )
    runner = InspectingRunner.succeeding()
    try:
        with pytest.raises(DeploymentError):
            DockerCompose(root=root, runner=runner).pull_exact_images(context)
    finally:
        lease.close()

    assert runner.calls == []


def _repo_digest(reference: str) -> str:
    tagged_name, digest = reference.rsplit("@", 1)
    repository, _tag = tagged_name.rsplit(":", 1)
    return f"{repository}@{digest}"


def _image_inspection_rows() -> list[dict[str, Any]]:
    return [
        {
            "RepoDigests": [_repo_digest(reference)],
            "Os": "linux",
            "Architecture": "amd64",
        }
        for reference in PINNED_IMAGES.values()
    ]


def _image_inspection_stdout(rows: list[dict[str, Any]] | None = None) -> str:
    selected = _image_inspection_rows() if rows is None else rows
    return "".join(
        f"{json.dumps(row, separators=(',', ':'))}\n" for row in selected
    )


def _assert_image_inspect(spec: Any, root: Path) -> None:
    assert tuple(spec.argv) == (
        "/usr/bin/docker",
        "--host",
        "unix:///var/run/docker.sock",
        "image",
        "inspect",
        "--platform",
        "linux/amd64",
        "--format",
        "json",
        *PINNED_IMAGES.values(),
    )
    assert dict(spec.environment) == {
        "PATH": "/usr/bin:/bin",
        "DOCKER_CONFIG": str(Path(spec.environment["DOCKER_CONFIG"])),
    }
    assert spec.cwd == root


def _assert_nginx_pre_up_validation(spec: Any, root: Path) -> None:
    source = spec.argv[spec.argv.index("--mount") + 1].split(",", 2)[
        1
    ].removeprefix("source=")
    assert source.startswith(f"/proc/{os.getpid()}/fd/")
    assert tuple(spec.argv) == (
        "/usr/bin/docker",
        "--host",
        "unix:///var/run/docker.sock",
        "run",
        "--pull",
        "never",
        "--rm",
        "--network",
        "none",
        "--platform",
        "linux/amd64",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        "101:101",
        "--tmpfs",
        "/tmp:uid=101,gid=101,mode=0700",
        "--entrypoint",
        "/usr/sbin/nginx",
        "--mount",
        f"type=bind,source={source},target=/etc/ifactory-cmms/nginx.conf,readonly",
        PINNED_IMAGES["nginx"],
        "-t",
        "-q",
        "-c",
        "/etc/ifactory-cmms/nginx.conf",
    )
    assert dict(spec.environment) == {
        "PATH": "/usr/bin:/bin",
        "DOCKER_CONFIG": str(Path(spec.environment["DOCKER_CONFIG"])),
    }
    assert spec.cwd == root
    assert spec.timeout_seconds == 30.0
    assert spec.stdout_limit == 0
    assert spec.stderr_limit == 0
    assert spec.safe_label == "cmms-nginx-pre-up-validate"


@dataclass
class ImageInspectingRunner(InspectingRunner):
    inspection_results: deque[CommandResult] = field(
        default_factory=lambda: deque(
            [CommandResult(0, _image_inspection_stdout(), "")]
        )
    )
    inspected: bool = False

    def run(self, spec: Any) -> CommandResult:
        if spec.safe_label == "cmms-compose-pull":
            self.outcomes.append(CommandResult(0, "", ""))
        elif tuple(spec.argv[3:5]) == ("image", "inspect"):
            self.inspected = True
            if not self.inspection_results:
                raise AssertionError("unexpected repeated image inspection")
            self.outcomes.append(self.inspection_results.popleft())
        else:
            raise AssertionError(f"unexpected image command: {spec.safe_label}")
        return super().run(spec)


def test_pull_is_exactly_planned_and_returns_bound_amd64_receipt(
    tmp_path: Path,
    safe_runtime: SafeRuntimeFixture,
) -> None:
    root = _stage_compose_root(tmp_path / "compose-root")
    config = _runtime_config(root)
    context, lease = _claim(
        safe_runtime,
        config=config,
        operation=Operation.REPAIR,
        actions=_repair_pull_actions(),
        bindings=safe_runtime.bootstrap_plan_bindings,
        nonce="5" * 32,
    )
    runner = ImageInspectingRunner()
    try:
        receipt = DockerCompose(root=root, runner=runner).pull_exact_images(context)
    finally:
        lease.close()

    assert type(receipt) is ImageReceipt
    assert receipt.platform == "linux/amd64"
    assert receipt.images == PINNED_IMAGES
    assert runner.inspected is True
    pull_calls = [
        spec
        for spec in runner.calls
        if tuple(spec.argv[3:4]) == ("compose",)
        and _assert_compose_prefix(spec, root)[0] == "pull"
    ]
    assert len(pull_calls) == 1
    assert _assert_compose_prefix(pull_calls[0], root) == (
        "pull",
        "postgres",
        "minio",
        "nginx",
    )
    inspect_calls = [
        spec
        for spec in runner.calls
        if tuple(spec.argv[3:5]) == ("image", "inspect")
    ]
    assert len(inspect_calls) == 1
    _assert_image_inspect(inspect_calls[0], root)
    assert len(runner.docker_config_identities) == 2
    assert all(not path.exists() for path in runner.docker_config_paths)


def test_image_inspection_accepts_full_docker_json_with_canonical_aliases(
    tmp_path: Path,
    safe_runtime: SafeRuntimeFixture,
) -> None:
    root = _stage_compose_root(tmp_path / "compose-root")
    config = _runtime_config(root)
    context, lease = _claim(
        safe_runtime,
        config=config,
        operation=Operation.REPAIR,
        actions=_repair_pull_actions(),
        bindings=safe_runtime.bootstrap_plan_bindings,
        nonce="c" * 32,
    )
    rows = _image_inspection_rows()
    aliases = (
        "docker.io/library/postgres",
        "docker.io/minio/minio",
        "docker.io/library/nginx",
    )
    for row, alias, reference in zip(
        rows,
        aliases,
        PINNED_IMAGES.values(),
        strict=True,
    ):
        digest = reference.rsplit("@", 1)[1]
        row["RepoDigests"] = [f"{alias}@{digest}"]
        row["Id"] = "sha256:" + "1" * 64
        row["Config"] = {"Labels": None}
    runner = ImageInspectingRunner(
        inspection_results=deque(
            [CommandResult(0, _image_inspection_stdout(rows), "")]
        )
    )
    try:
        receipt = DockerCompose(root=root, runner=runner).pull_exact_images(context)
    finally:
        lease.close()

    assert receipt.images == PINNED_IMAGES


def _bootstrap_context(
    safe_runtime: SafeRuntimeFixture,
    config: RuntimeConfig,
    *,
    nonce: str,
) -> tuple[ClaimedApplyContext, Any]:
    return _claim(
        safe_runtime,
        config=config,
        operation=Operation.BOOTSTRAP,
        actions=_bootstrap_actions(),
        bindings=_fresh_bootstrap_bindings(safe_runtime),
        nonce=nonce,
    )


def _publish_loopback_nginx(
    root: Path,
    context: ClaimedApplyContext,
) -> tuple[Path, bytes]:
    rendered = Gateway(
        template=root / "deploy/gateway/cmms-development-nginx.conf.template",
        runner=object(),
    ).render(
        GatewayMode.LOOPBACK,
        None,
        context.plan.snapshot.unit_generation,
    )
    data = rendered.text.encode("utf-8")
    target = root / ".runtime/cmms-nginx/nginx.conf"
    target.write_bytes(data)
    target.chmod(0o444)
    return target, data


def _replace_published_nginx(path: Path, data: bytes) -> None:
    replacement = path.with_name(f".{path.name}.replacement")
    replacement.write_bytes(data)
    replacement.chmod(0o444)
    os.replace(replacement, path)


@dataclass
class ImageLifecycleRunner(InspectingRunner):
    inspection_results: deque[CommandResult] = field(default_factory=deque)
    validation_results: deque[CommandResult] = field(
        default_factory=lambda: deque([CommandResult(0, "", "")])
    )
    validation_mutation: Callable[[Any], None] | None = None

    def run(self, spec: Any) -> CommandResult:
        if spec.safe_label in {"cmms-compose-pull", "cmms-compose-create"}:
            self.outcomes.append(CommandResult(0, "", ""))
        elif spec.safe_label == "cmms-nginx-pre-up-validate":
            if not self.validation_results:
                raise AssertionError("unexpected nginx validation")
            self.outcomes.append(self.validation_results.popleft())
        elif tuple(spec.argv[3:5]) == ("image", "inspect"):
            if not self.inspection_results:
                raise AssertionError("unexpected image inspection")
            self.outcomes.append(self.inspection_results.popleft())
        else:
            raise AssertionError(
                f"unexpected image lifecycle command: {spec.safe_label}"
            )
        result = super().run(spec)
        if (
            spec.safe_label == "cmms-nginx-pre-up-validate"
            and self.validation_mutation is not None
        ):
            self.validation_mutation(spec)
        return result


def test_bootstrap_pull_and_create_reinspect_exact_images_before_fixed_up(
    tmp_path: Path,
    safe_runtime: SafeRuntimeFixture,
) -> None:
    root = _stage_compose_root(tmp_path / "compose-root")
    config = _runtime_config(root)
    context, lease = _bootstrap_context(safe_runtime, config, nonce="6" * 32)
    _config_path, expected_nginx = _publish_loopback_nginx(root, context)
    exact = CommandResult(0, _image_inspection_stdout(), "")
    runner = ImageLifecycleRunner(
        inspection_results=deque([exact, exact]),
    )
    controller = DockerCompose(root=root, runner=runner)
    try:
        receipt = controller.pull_exact_images(context)
        controller.up_state_and_gateway(context)
    finally:
        lease.close()

    assert receipt.images == PINNED_IMAGES
    assert len(runner.calls) == 5
    assert _assert_compose_prefix(runner.calls[0], root) == (
        "pull",
        "postgres",
        "minio",
        "nginx",
    )
    _assert_image_inspect(runner.calls[1], root)
    _assert_image_inspect(runner.calls[2], root)
    _assert_nginx_pre_up_validation(runner.calls[3], root)
    assert runner.nginx_validation_bytes == [expected_nginx]
    assert (
        runner.nginx_validation_seals[0] & _REQUIRED_MEMFD_SEALS
        == _REQUIRED_MEMFD_SEALS
    )
    assert _assert_compose_prefix(runner.calls[4], root) == (
        "up",
        "--detach",
        "--no-build",
        "--pull",
        "never",
        "postgres",
        "minio",
        "nginx",
    )
    assert len(runner.docker_config_paths) == 5
    assert len(runner.docker_config_identities) == 5
    assert all(not path.exists() for path in runner.docker_config_paths)
    assert len(runner.secret_fd_paths) == 2
    assert runner.nginx_runtime_fd_counts == [1, 2]
    assert all(
        not path.exists()
        for invocation in runner.secret_fd_paths
        for path in invocation
    )
    assert all(not path.exists() for path in runner.nginx_validation_paths)


@pytest.mark.parametrize(
    "result",
    (
        CommandResult(1, "", "untrusted nginx diagnostic"),
        CommandResult(0, "unexpected output", ""),
        CommandResult(0, "", "unexpected warning"),
    ),
    ids=("nonzero", "stdout", "stderr"),
)
def test_nginx_pre_up_validation_failure_blocks_compose_create(
    tmp_path: Path,
    safe_runtime: SafeRuntimeFixture,
    result: CommandResult,
) -> None:
    root = _stage_compose_root(tmp_path / "compose-root")
    config = _runtime_config(root)
    context, lease = _bootstrap_context(safe_runtime, config, nonce="d" * 32)
    _publish_loopback_nginx(root, context)
    runner = ImageLifecycleRunner(
        inspection_results=deque(
            [CommandResult(0, _image_inspection_stdout(), "")]
        ),
        validation_results=deque([result]),
    )
    try:
        with pytest.raises(DeploymentError) as caught:
            DockerCompose(root=root, runner=runner).up_state_and_gateway(context)
    finally:
        lease.close()

    assert caught.value.code == "CMMS-E041"
    assert "untrusted nginx" not in str(caught.value)
    assert len(runner.calls) == 2
    _assert_image_inspect(runner.calls[0], root)
    _assert_nginx_pre_up_validation(runner.calls[1], root)
    assert all(spec.safe_label != "cmms-compose-create" for spec in runner.calls)
    assert all(not path.exists() for path in runner.docker_config_paths)
    assert all(not path.exists() for path in runner.nginx_validation_paths)


def test_published_nginx_content_drift_blocks_validation_and_compose_create(
    tmp_path: Path,
    safe_runtime: SafeRuntimeFixture,
) -> None:
    root = _stage_compose_root(tmp_path / "compose-root")
    config = _runtime_config(root)
    context, lease = _bootstrap_context(safe_runtime, config, nonce="e" * 32)
    config_path, expected = _publish_loopback_nginx(root, context)
    _replace_published_nginx(config_path, expected + b"# drift\n")
    runner = ImageLifecycleRunner(
        inspection_results=deque(
            [CommandResult(0, _image_inspection_stdout(), "")]
        )
    )
    try:
        with pytest.raises(DeploymentError) as caught:
            DockerCompose(root=root, runner=runner).up_state_and_gateway(context)
    finally:
        lease.close()

    assert caught.value.code == "CMMS-E041"
    assert [spec.safe_label for spec in runner.calls] == ["cmms-image-inspect"]
    assert runner.nginx_validation_paths == []
    assert all(not path.exists() for path in runner.docker_config_paths)


@pytest.mark.parametrize("unsafe_kind", ("mode", "hardlink", "symlink"))
def test_published_nginx_unsafe_metadata_blocks_compose_create(
    tmp_path: Path,
    safe_runtime: SafeRuntimeFixture,
    unsafe_kind: str,
) -> None:
    root = _stage_compose_root(tmp_path / "compose-root")
    config = _runtime_config(root)
    context, lease = _bootstrap_context(safe_runtime, config, nonce="b" * 32)
    config_path, expected = _publish_loopback_nginx(root, context)
    if unsafe_kind == "mode":
        config_path.chmod(0o600)
    elif unsafe_kind == "hardlink":
        os.link(config_path, config_path.with_name("nginx-linked.conf"))
    elif unsafe_kind == "symlink":
        target = config_path.with_name("nginx-target.conf")
        target.write_bytes(expected)
        target.chmod(0o444)
        config_path.unlink()
        config_path.symlink_to(target.name)
    else:
        raise AssertionError("unknown unsafe Nginx fixture")
    runner = ImageLifecycleRunner(
        inspection_results=deque(
            [CommandResult(0, _image_inspection_stdout(), "")]
        )
    )
    try:
        with pytest.raises(DeploymentError) as caught:
            DockerCompose(root=root, runner=runner).up_state_and_gateway(context)
    finally:
        lease.close()

    assert caught.value.code == "CMMS-E041"
    assert [spec.safe_label for spec in runner.calls] == ["cmms-image-inspect"]
    assert runner.nginx_validation_paths == []
    assert all(not path.exists() for path in runner.docker_config_paths)


def test_published_nginx_replacement_during_validation_blocks_compose_create(
    tmp_path: Path,
    safe_runtime: SafeRuntimeFixture,
) -> None:
    root = _stage_compose_root(tmp_path / "compose-root")
    config = _runtime_config(root)
    context, lease = _bootstrap_context(safe_runtime, config, nonce="f" * 32)
    config_path, expected = _publish_loopback_nginx(root, context)
    runner = ImageLifecycleRunner(
        inspection_results=deque(
            [CommandResult(0, _image_inspection_stdout(), "")]
        ),
        validation_mutation=lambda _spec: _replace_published_nginx(
            config_path,
            expected,
        ),
    )
    try:
        with pytest.raises(DeploymentError) as caught:
            DockerCompose(root=root, runner=runner).up_state_and_gateway(context)
    finally:
        lease.close()

    assert caught.value.code == "CMMS-E041"
    assert [spec.safe_label for spec in runner.calls] == [
        "cmms-image-inspect",
        "cmms-nginx-pre-up-validate",
    ]
    assert all(spec.safe_label != "cmms-compose-create" for spec in runner.calls)
    assert all(not path.exists() for path in runner.docker_config_paths)
    assert all(not path.exists() for path in runner.nginx_validation_paths)


def _invalid_image_inspection(kind: str) -> CommandResult:
    rows = _image_inspection_rows()
    if kind == "malformed":
        return CommandResult(0, "{not-json}\n", "")
    if kind == "missing":
        rows.pop()
    elif kind == "extra":
        rows.append(dict(rows[-1]))
    elif kind == "wrong-platform":
        rows[1] = {**rows[1], "Architecture": "arm64"}
    elif kind == "wrong-digest":
        rows[0] = {
            **rows[0],
            "RepoDigests": ["postgres@sha256:" + "0" * 64],
        }
    elif kind == "duplicate-key":
        valid_tail = _image_inspection_stdout(rows[1:])
        duplicate = (
            '{"RepoDigests":[],"RepoDigests":[],"Os":"linux",'
            '"Architecture":"amd64"}\n'
        )
        return CommandResult(0, duplicate + valid_tail, "")
    else:
        raise AssertionError("unknown invalid image inspection fixture")
    return CommandResult(0, _image_inspection_stdout(rows), "")


@pytest.mark.parametrize(
    "drift",
    (
        "malformed",
        "missing",
        "extra",
        "wrong-platform",
        "wrong-digest",
        "duplicate-key",
    ),
)
def test_image_drift_after_pull_receipt_blocks_bootstrap_up(
    tmp_path: Path,
    safe_runtime: SafeRuntimeFixture,
    drift: str,
) -> None:
    root = _stage_compose_root(tmp_path / "compose-root")
    config = _runtime_config(root)
    context, lease = _bootstrap_context(safe_runtime, config, nonce="7" * 32)
    runner = ImageLifecycleRunner(
        inspection_results=deque(
            [
                CommandResult(0, _image_inspection_stdout(), ""),
                _invalid_image_inspection(drift),
            ]
        )
    )
    controller = DockerCompose(root=root, runner=runner)
    try:
        controller.pull_exact_images(context)
        with pytest.raises(DeploymentError) as caught:
            controller.up_state_and_gateway(context)
    finally:
        lease.close()

    assert caught.value.code == "CMMS-E041"
    assert all(
        not (
            "compose" in spec.argv
            and _assert_compose_prefix(spec, root)[0] == "up"
        )
        for spec in runner.calls
    )
    assert len(
        [spec for spec in runner.calls if tuple(spec.argv[3:5]) == ("image", "inspect")]
    ) == 2
    assert all(not path.exists() for path in runner.docker_config_paths)


@pytest.mark.parametrize(
    "result",
    (
        CommandResult(1, "", "untrusted image diagnostic"),
        CommandResult(0, _image_inspection_stdout(), "untrusted image warning"),
    ),
    ids=("nonzero", "stderr"),
)
def test_image_inspection_failure_or_stderr_is_fail_closed(
    tmp_path: Path,
    safe_runtime: SafeRuntimeFixture,
    result: CommandResult,
) -> None:
    root = _stage_compose_root(tmp_path / "compose-root")
    config = _runtime_config(root)
    context, lease = _claim(
        safe_runtime,
        config=config,
        operation=Operation.REPAIR,
        actions=_repair_pull_actions(),
        bindings=safe_runtime.bootstrap_plan_bindings,
        nonce="8" * 32,
    )
    runner = ImageInspectingRunner(inspection_results=deque([result]))
    try:
        with pytest.raises(DeploymentError) as caught:
            DockerCompose(root=root, runner=runner).pull_exact_images(context)
    finally:
        lease.close()

    assert caught.value.code == "CMMS-E041"
    assert "untrusted image" not in str(caught.value)
    assert all(not path.exists() for path in runner.docker_config_paths)


def _atomic_replace(path: Path, data: bytes) -> None:
    replacement = path.with_name(f".{path.name}.replacement")
    replacement.write_bytes(data)
    replacement.chmod(0o600)
    os.replace(replacement, path)


@dataclass
class PostCallMutationRunner(InspectingRunner):
    mutation: Callable[[Any], None] | None = None

    def run(self, spec: Any) -> CommandResult:
        result = super().run(spec)
        if self.mutation is not None:
            self.mutation(spec)
        return result


def test_secret_source_atomic_replacement_during_runner_fails_lineage_check(
    tmp_path: Path,
) -> None:
    root = _stage_compose_root(tmp_path)
    config = _runtime_config(root)
    original = config.postgres_password_file.read_bytes()
    runner = PostCallMutationRunner(
        outcomes=deque([CommandResult(0, "", "")]),
        mutation=lambda _spec: _atomic_replace(
            config.postgres_password_file,
            original,
        ),
    )

    with pytest.raises(DeploymentError) as caught:
        DockerCompose(root=root, runner=runner).config_quiet(config)

    assert caught.value.code == "CMMS-E041"
    assert len(runner.calls) == 1
    assert len(runner.secret_fd_paths) == 1
    assert all(not path.exists() for path in runner.secret_fd_paths[0])
    assert all(not path.exists() for path in runner.docker_config_paths)


def test_runtime_env_same_bytes_atomic_replacement_during_runner_is_detected(
    tmp_path: Path,
) -> None:
    root = _stage_compose_root(tmp_path)
    config = _runtime_config(root)
    runtime_env = root / ".runtime/cmms-development.env"
    original = runtime_env.read_bytes()
    runner = PostCallMutationRunner(
        outcomes=deque([CommandResult(0, "", "")]),
        mutation=lambda _spec: _atomic_replace(runtime_env, original),
    )

    with pytest.raises(DeploymentError) as caught:
        DockerCompose(root=root, runner=runner).config_quiet(config)

    assert caught.value.code == "CMMS-E041"
    assert len(runner.calls) == 1
    assert all(not path.exists() for path in runner.docker_config_paths)


def test_compose_source_same_bytes_replacement_during_runner_is_detected(
    tmp_path: Path,
) -> None:
    root = _stage_compose_root(tmp_path)
    config = _runtime_config(root)
    compose_file = root / "deploy/compose/cmms-development.yml"
    original = compose_file.read_bytes()
    runner = PostCallMutationRunner(
        outcomes=deque([CommandResult(0, "", "")]),
        mutation=lambda _spec: _atomic_replace(compose_file, original),
    )

    with pytest.raises(DeploymentError) as caught:
        DockerCompose(root=root, runner=runner).config_quiet(config)

    assert caught.value.code == "CMMS-E041"
    assert len(runner.calls) == 1
    assert runner.compose_file_bytes == [original]
    assert (
        runner.compose_file_seals[0] & _REQUIRED_MEMFD_SEALS
        == _REQUIRED_MEMFD_SEALS
    )
    assert all(not path.exists() for path in runner.compose_file_paths)
    assert all(not path.exists() for path in runner.docker_config_paths)


def test_compose_source_drift_is_rejected_before_any_runner_call(
    tmp_path: Path,
) -> None:
    root = _stage_compose_root(tmp_path)
    compose_file = root / "deploy/compose/cmms-development.yml"
    compose_file.write_bytes(compose_file.read_bytes() + b"\n")
    runner = InspectingRunner.succeeding()

    with pytest.raises(DeploymentError) as caught:
        DockerCompose(root=root, runner=runner)

    assert caught.value.code == "CMMS-E041"
    assert runner.calls == []


def test_nginx_runtime_directory_replacement_during_runner_is_detected(
    tmp_path: Path,
) -> None:
    root = _stage_compose_root(tmp_path)
    config = _runtime_config(root)
    nginx_runtime = root / ".runtime/cmms-nginx"
    displaced = root / ".runtime/cmms-nginx-displaced"

    def replace_runtime_directory(_spec: Any) -> None:
        nginx_runtime.rename(displaced)
        nginx_runtime.mkdir(mode=0o755)
        nginx_runtime.chmod(0o755)

    runner = PostCallMutationRunner(
        outcomes=deque([CommandResult(0, "", "")]),
        mutation=replace_runtime_directory,
    )

    with pytest.raises(DeploymentError) as caught:
        DockerCompose(root=root, runner=runner).config_quiet(config)

    assert caught.value.code == "CMMS-E041"
    assert len(runner.calls) == 1
    assert len(runner.nginx_runtime_fd_paths) == 1
    assert not runner.nginx_runtime_fd_paths[0].exists()
    assert all(not path.exists() for path in runner.docker_config_paths)


def test_secret_symlink_is_rejected_before_any_runner_call(tmp_path: Path) -> None:
    root = _stage_compose_root(tmp_path)
    config = _runtime_config(root)
    target = config.postgres_password_file.with_name("postgres-password-target")
    target.write_bytes(config.postgres_password_file.read_bytes())
    target.chmod(0o600)
    config.postgres_password_file.unlink()
    config.postgres_password_file.symlink_to(target)
    runner = InspectingRunner.succeeding()

    with pytest.raises(DeploymentError) as caught:
        DockerCompose(root=root, runner=runner).config_quiet(config)

    assert caught.value.code == "CMMS-E041"
    assert runner.calls == []


@pytest.mark.parametrize("mutation_kind", ("action", "lease", "application"))
def test_claimed_context_toctou_during_runner_blocks_completion(
    tmp_path: Path,
    safe_runtime: SafeRuntimeFixture,
    mutation_kind: str,
) -> None:
    root = _stage_compose_root(tmp_path / "compose-root")
    config = _runtime_config(root)
    context, lease = _claim(
        safe_runtime,
        config=config,
        operation=Operation.START,
        actions=_start_actions(),
        bindings=safe_runtime.bootstrap_plan_bindings,
        nonce="9" * 32,
    )
    application_path = (
        safe_runtime.plans_dir / f"{context.plan.plan_sha256}.application.json"
    )

    def mutate(_spec: Any) -> None:
        if mutation_kind == "action":
            object.__setattr__(
                context.plan,
                "actions",
                tuple(
                    row
                    for row in context.plan.actions
                    if row.code is not ActionCode.COMPOSE_START_STATE_GATEWAY
                ),
            )
        elif mutation_kind == "lease":
            lease.close()
        elif mutation_kind == "application":
            _atomic_replace(application_path, b"{}\n")
        else:
            raise AssertionError("unknown context mutation")

    runner = PostCallMutationRunner(
        outcomes=deque([CommandResult(0, "", "")]),
        mutation=mutate,
    )
    try:
        with pytest.raises(DeploymentError) as caught:
            DockerCompose(root=root, runner=runner).up_state_and_gateway(context)
    finally:
        lease.close()

    assert SENTINEL_SECRET not in str(caught.value)
    assert len(runner.calls) == 1
    assert all(not path.exists() for path in runner.docker_config_paths)
    assert all(
        not path.exists()
        for invocation in runner.secret_fd_paths
        for path in invocation
    )


class SwappingRunDescriptor:
    def __init__(self) -> None:
        self.resolutions = 0
        self.poison_calls = 0
        self.inner = ImageInspectingRunner()

    @property
    def run(self) -> Callable[[Any], CommandResult]:
        self.resolutions += 1
        if self.resolutions == 1:
            return self.inner.run

        def poison(_spec: Any) -> CommandResult:
            self.poison_calls += 1
            raise AssertionError("runner descriptor was resolved more than once")

        return poison


def test_pull_resolves_runner_descriptor_once_for_pull_and_inspection(
    tmp_path: Path,
    safe_runtime: SafeRuntimeFixture,
) -> None:
    root = _stage_compose_root(tmp_path / "compose-root")
    config = _runtime_config(root)
    context, lease = _claim(
        safe_runtime,
        config=config,
        operation=Operation.REPAIR,
        actions=_repair_pull_actions(),
        bindings=safe_runtime.bootstrap_plan_bindings,
        nonce="a" * 32,
    )
    runner = SwappingRunDescriptor()
    try:
        DockerCompose(root=root, runner=runner).pull_exact_images(context)
    finally:
        lease.close()

    assert runner.resolutions == 1
    assert runner.poison_calls == 0
    assert len(runner.inner.calls) == 2


@dataclass
class RaisingRunner:
    calls: list[Any] = field(default_factory=list)
    docker_config_paths: list[Path] = field(default_factory=list)

    def run(self, spec: Any) -> CommandResult:
        self.calls.append(spec)
        docker_config = Path(spec.environment["DOCKER_CONFIG"])
        assert stat.S_IMODE(docker_config.stat().st_mode) == 0o700
        assert list(docker_config.iterdir()) == []
        self.docker_config_paths.append(docker_config)
        raise RuntimeError("untrusted runner failure")


def test_fresh_docker_config_is_removed_when_runner_raises(tmp_path: Path) -> None:
    root = _stage_compose_root(tmp_path)
    config = _runtime_config(root)
    runner = RaisingRunner()

    with pytest.raises(DeploymentError) as caught:
        DockerCompose(root=root, runner=runner).config_quiet(config)

    assert caught.value.code == "CMMS-E041"
    assert "untrusted runner failure" not in str(caught.value)
    assert len(runner.docker_config_paths) == 1
    assert not runner.docker_config_paths[0].exists()


@dataclass
class BridgeRunner:
    gateway: str = "172.17.0.1"
    local: str = "172.17.0.1"
    calls: list[Any] = field(default_factory=list)
    docker_config_paths: list[Path] = field(default_factory=list)

    def run(self, spec: Any) -> CommandResult:
        self.calls.append(spec)
        if spec.argv[0] == "/usr/bin/docker":
            assert set(spec.environment) == {"PATH", "DOCKER_CONFIG"}
            docker_config = Path(spec.environment["DOCKER_CONFIG"])
            assert docker_config.as_posix().startswith(
                f"/proc/{os.getpid()}/fd/"
            )
            metadata = docker_config.stat()
            assert stat.S_ISDIR(metadata.st_mode)
            assert stat.S_IMODE(metadata.st_mode) == 0o700
            assert metadata.st_uid == os.getuid()
            assert list(docker_config.iterdir()) == []
            self.docker_config_paths.append(docker_config)
            return CommandResult(
                0,
                (
                    '[{"Name":"bridge","Options":'
                    '{"com.docker.network.bridge.name":"docker0"},'
                    '"IPAM":{"Config":[{"Subnet":"172.17.0.0/16",'
                    f'"Gateway":"{self.gateway}"}}]}}}}]'
                ),
                "",
            )
        if spec.argv[0] == "/usr/sbin/ip":
            return CommandResult(
                0,
                (
                    '[{"ifname":"docker0","addr_info":['
                    f'{{"family":"inet","local":"{self.local}","prefixlen":16}}]}}]'
                ),
                "",
            )
        raise AssertionError("unexpected bridge probe")


def test_default_bridge_gateway_uses_fixed_inspection_and_local_proof() -> None:
    runner = BridgeRunner()

    address = resolve_default_bridge_gateway(runner)

    assert str(address) == "172.17.0.1"
    assert tuple(runner.calls[0].argv[:5]) == (
        "/usr/bin/docker",
        "--host",
        "unix:///var/run/docker.sock",
        "network",
        "inspect",
    )
    assert runner.calls[0].argv[5] == "bridge"
    assert runner.calls[1].argv[:6] == (
        "/usr/sbin/ip",
        "-json",
        "address",
        "show",
        "dev",
        "docker0",
    )
    assert dict(runner.calls[0].environment) == {
        "PATH": "/usr/bin:/bin",
        "DOCKER_CONFIG": str(runner.docker_config_paths[0]),
    }
    assert dict(runner.calls[1].environment) == {"PATH": "/usr/bin:/bin"}
    assert not runner.docker_config_paths[0].exists()


@pytest.mark.parametrize(
    ("gateway", "local"),
    [
        pytest.param("0.0.0.0", "0.0.0.0", id="wildcard"),
        pytest.param("127.0.0.1", "127.0.0.1", id="loopback"),
        pytest.param("169.254.1.1", "169.254.1.1", id="link-local"),
        pytest.param("224.0.0.1", "224.0.0.1", id="multicast"),
        pytest.param("172.17.0.1", "172.17.0.2", id="not-configured"),
    ],
)
def test_default_bridge_gateway_rejects_unsafe_or_nonlocal_results(
    gateway: str,
    local: str,
) -> None:
    with pytest.raises(DeploymentError) as caught:
        resolve_default_bridge_gateway(BridgeRunner(gateway, local))

    assert caught.value.code == "CMMS-E042"


def _bridge_network_stdout(
    *,
    configs: list[dict[str, Any]] | None = None,
) -> str:
    selected = (
        [{"Subnet": "172.17.0.0/16", "Gateway": "172.17.0.1"}]
        if configs is None
        else configs
    )
    return json.dumps(
        [
            {
                "Name": "bridge",
                "Options": {"com.docker.network.bridge.name": "docker0"},
                "IPAM": {"Config": selected},
            }
        ],
        separators=(",", ":"),
    )


def _bridge_local_stdout(
    *,
    addresses: list[dict[str, Any]] | None = None,
    ifname: str = "docker0",
) -> str:
    selected = (
        [{"family": "inet", "local": "172.17.0.1", "prefixlen": 16}]
        if addresses is None
        else addresses
    )
    return json.dumps(
        [{"ifname": ifname, "addr_info": selected}],
        separators=(",", ":"),
    )


@dataclass
class BridgeOutcomeRunner:
    outcomes: deque[CommandResult]
    calls: list[Any] = field(default_factory=list)
    docker_config_paths: list[Path] = field(default_factory=list)

    def run(self, spec: Any) -> CommandResult:
        self.calls.append(spec)
        if spec.argv[0] == "/usr/bin/docker":
            assert set(spec.environment) == {"PATH", "DOCKER_CONFIG"}
            docker_config = Path(spec.environment["DOCKER_CONFIG"])
            assert docker_config.as_posix().startswith(
                f"/proc/{os.getpid()}/fd/"
            )
            assert stat.S_IMODE(docker_config.stat().st_mode) == 0o700
            assert list(docker_config.iterdir()) == []
            self.docker_config_paths.append(docker_config)
        if not self.outcomes:
            raise AssertionError("unexpected bridge command")
        return self.outcomes.popleft()


@pytest.mark.parametrize(
    "network_result",
    (
        CommandResult(1, "", "untrusted bridge diagnostic"),
        CommandResult(0, _bridge_network_stdout(), "untrusted bridge warning"),
        CommandResult(0, "{not-json", ""),
        CommandResult(0, _bridge_network_stdout()[:-1] + ",{}]", ""),
        CommandResult(
            0,
            _bridge_network_stdout(
                configs=[
                    {"Subnet": "172.17.0.0/16", "Gateway": "172.17.0.1"},
                    {"Subnet": "172.18.0.0/16", "Gateway": "172.18.0.1"},
                ]
            ),
            "",
        ),
        CommandResult(
            0,
            '[{"Name":"bridge","Name":"bridge","Options":'
            '{"com.docker.network.bridge.name":"docker0"},'
            '"IPAM":{"Config":[{"Subnet":"172.17.0.0/16",'
            '"Gateway":"172.17.0.1"}]}}]',
            "",
        ),
    ),
    ids=(
        "nonzero",
        "stderr",
        "malformed",
        "multiple-networks",
        "multiple-gateways",
        "duplicate-key",
    ),
)
def test_bridge_network_inspection_is_strict_and_stops_before_local_probe(
    network_result: CommandResult,
) -> None:
    runner = BridgeOutcomeRunner(deque([network_result]))

    with pytest.raises(DeploymentError) as caught:
        resolve_default_bridge_gateway(runner)

    assert caught.value.code == "CMMS-E042"
    assert "untrusted bridge" not in str(caught.value)
    assert len(runner.calls) == 1
    assert tuple(runner.calls[0].argv) == (
        "/usr/bin/docker",
        "--host",
        "unix:///var/run/docker.sock",
        "network",
        "inspect",
        "bridge",
    )
    assert dict(runner.calls[0].environment) == {
        "PATH": "/usr/bin:/bin",
        "DOCKER_CONFIG": str(runner.docker_config_paths[0]),
    }
    assert not runner.docker_config_paths[0].exists()


@pytest.mark.parametrize(
    "local_result",
    (
        CommandResult(1, "", "untrusted local diagnostic"),
        CommandResult(0, _bridge_local_stdout(), "untrusted local warning"),
        CommandResult(0, "[not-json]", ""),
        CommandResult(
            0,
            _bridge_local_stdout(
                addresses=[
                    {"family": "inet", "local": "172.17.0.1", "prefixlen": 16},
                    {"family": "inet", "local": "172.17.0.2", "prefixlen": 16},
                ]
            ),
            "",
        ),
        CommandResult(0, _bridge_local_stdout(ifname="br-unsafe"), ""),
    ),
    ids=("nonzero", "stderr", "malformed", "multiple-ipv4", "wrong-interface"),
)
def test_local_bridge_proof_is_strict_after_one_exact_network_result(
    local_result: CommandResult,
) -> None:
    runner = BridgeOutcomeRunner(
        deque(
            [
                CommandResult(0, _bridge_network_stdout(), ""),
                local_result,
            ]
        )
    )

    with pytest.raises(DeploymentError) as caught:
        resolve_default_bridge_gateway(runner)

    assert caught.value.code == "CMMS-E042"
    assert "untrusted local" not in str(caught.value)
    assert len(runner.calls) == 2
    assert tuple(runner.calls[1].argv) == (
        "/usr/sbin/ip",
        "-json",
        "address",
        "show",
        "dev",
        "docker0",
    )
    assert dict(runner.calls[1].environment) == {"PATH": "/usr/bin:/bin"}
    assert len(runner.docker_config_paths) == 1
    assert not runner.docker_config_paths[0].exists()


def test_compose_controller_source_has_no_destructive_volume_cleanup() -> None:
    source = (ROOT / "deploy/cmms/src/ifactory_cmms_deploy/compose.py").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(source.casefold().split())

    for forbidden in (
        "down -v",
        "down --volumes",
        "volume rm",
        "volume prune",
        "postgres_data*",
        "minio_data*",
    ):
        assert forbidden not in normalized
