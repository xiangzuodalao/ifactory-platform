from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import yaml


ROOT = Path(__file__).parents[2]
COMPOSE = ROOT / "deploy/compose/closed-loop-pilot.yml"
BROWSER_GATEWAY = ROOT / "deploy/gateway/closed-loop-browser-nginx.conf"
CMMS_GATEWAY = ROOT / "deploy/gateway/closed-loop-cmms-nginx.conf"
PILOT_WRAPPER = ROOT / "scripts/closed-loop-pilot.sh"
GENERATED_KEYS = (
    "PLATFORM_INTEGRATION_TB_TENANT_ID",
    "PLATFORM_INTEGRATION_CMMS_COMPANY_ID",
    "PLATFORM_INTEGRATION_APPROVER_TB_USER_ID",
    "PLATFORM_INTEGRATION_PILOT_WORK_ORDER_EQUIPMENT_ID",
    "PILOT_CMMS_CREDENTIAL",
)


def _environment(service: dict) -> dict[str, str]:
    environment = service.get("environment", {})
    assert isinstance(environment, dict)
    return environment


def _write_runtime(tmp_path: Path) -> Path:
    control = tmp_path / "closed-loop"
    control.mkdir(mode=0o700)
    state_lock = control / ".state.lock"
    state_lock.touch(mode=0o600)
    state_lock.chmod(0o600)
    state = control / "state"
    state.mkdir(mode=0o700)
    (state / "receipts").mkdir(mode=0o700)
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    paths: dict[str, Path] = {}
    for name in (
        "cmms-postgres-password",
        "cmms-jwt-secret",
        "cmms-admin-password",
        "minio-root-user",
        "minio-root-password",
    ):
        path = secrets / name
        path.write_text(f"test-{name}\n", encoding="utf-8")
        path.chmod(0o600)
        paths[name] = path
    values = {
        "PILOT_RUNTIME_DIR": str(tmp_path),
        "PILOT_STATE_DIR": str(state),
        "PILOT_STATE_LOCK_FILE": str(state_lock),
        "PILOT_HOST_UID": str(os.getuid()),
        "PILOT_HOST_GID": str(os.getgid()),
        "CMMS_POSTGRES_DB": "ifactory_cmms_pilot",
        "CMMS_POSTGRES_USER": "ifactory_cmms_pilot",
        "CMMS_POSTGRES_PASSWORD_FILE": str(paths["cmms-postgres-password"]),
        "CMMS_JWT_SECRET_FILE": str(paths["cmms-jwt-secret"]),
        "CMMS_ADMIN_PASSWORD_FILE": str(paths["cmms-admin-password"]),
        "CMMS_PILOT_ADMIN_EMAIL": "pilot@example.test",
        "MINIO_ROOT_USER_FILE": str(paths["minio-root-user"]),
        "MINIO_ROOT_PASSWORD_FILE": str(paths["minio-root-password"]),
        "INTEGRATION_POSTGRES_DB": "ifactory_integration",
        "INTEGRATION_POSTGRES_USER": "ifactory_integration",
        "INTEGRATION_POSTGRES_PASSWORD": "integration-test-password",
        "PLATFORM_INTEGRATION_TENANT_ALIAS": "ifactory-pilot",
        "PLATFORM_INTEGRATION_TENANT_ID": "00000000-0000-4000-8000-000000000001",
        "PLATFORM_INTEGRATION_TB_TENANT_ID": "00000000-0000-4000-8000-000000000002",
        "PLATFORM_INTEGRATION_CMMS_COMPANY_ID": "42",
        "PLATFORM_INTEGRATION_APPROVER_TB_USER_ID": "00000000-0000-4000-8000-000000000003",
        "PLATFORM_INTEGRATION_PILOT_WORK_ORDER_EQUIPMENT_ID": "00000000-0000-4000-8000-000000000101",
        "PILOT_PDM_CREDENTIAL": '{"kind":"opaque_bearer","value":"pdm-token"}',
        "PILOT_TB_CREDENTIAL": '{"kind":"thingsboard_bearer","value":"tb-token"}',
        "PILOT_CMMS_CREDENTIAL": '{"kind":"cmms_bearer","value":"cmms-token"}',
        "VALEO_PDM_PREDICTION_V2_BEARER_TOKEN": "pdm-token",
    }
    generated_keys = set(GENERATED_KEYS)
    path = control / "closed-loop-pilot.env"
    path.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items() if key not in generated_keys),
        encoding="utf-8",
    )
    path.chmod(0o600)
    generated = state / "closed-loop-generated.env"
    generated.write_text(
        "".join(f"{key}={values[key]}\n" for key in GENERATED_KEYS),
        encoding="utf-8",
    )
    generated.chmod(0o600)
    return path


def test_closed_loop_compose_has_exact_services_networks_and_single_published_port() -> None:
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = document["services"]
    assert set(services) == {
        "cmms-db",
        "cmms-minio",
        "cmms-api",
        "cmms-gateway",
        "integration-db",
        "integration-migrate",
        "integration-api",
        "pdm",
        "scheduler",
        "prediction-worker",
        "alarm-worker",
        "work-order-worker",
        "status-poll-worker",
        "browser-gateway",
        "tb-relay-init",
        "tb-host-relay",
        "tb-relay",
        "bootstrap-runner",
        "provision-runner",
        "status-runner",
    }
    published = {name: service["ports"] for name, service in services.items() if "ports" in service}
    assert published == {"browser-gateway": ["127.0.0.1:18081:8080"]}
    assert document["networks"]["cmms"]["internal"] is True
    assert document["networks"]["integration"]["internal"] is True
    assert document["networks"]["browser"]["internal"] is True
    assert "ports" not in services["cmms-api"]
    assert "ports" not in services["integration-api"]
    assert services["bootstrap-runner"]["profiles"] == ["bootstrap"]
    assert services["provision-runner"]["profiles"] == ["acceptance"]
    assert services["status-runner"]["profiles"] == ["acceptance"]
    assert services["tb-relay-init"]["network_mode"] == "none"
    assert services["tb-relay-init"]["cap_add"] == ["CHOWN", "FOWNER"]
    assert "rm -f" not in str(services["tb-relay-init"]["command"])
    assert "rm -f /relay/backend.sock /relay/ui.sock" in str(services["tb-host-relay"]["command"])
    for relay in ("tb-host-relay", "tb-relay"):
        assert services[relay]["user"] == "101:101"
        assert services[relay]["cap_drop"] == ["ALL"]
        assert "wget" in str(services[relay]["healthcheck"])


def test_role_credentials_are_minimal_and_closed_loop_is_explicit() -> None:
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    credentials = {"PILOT_PDM_CREDENTIAL", "PILOT_TB_CREDENTIAL", "PILOT_CMMS_CREDENTIAL"}
    expected = {
        "integration-migrate": set(),
        "integration-api": set(),
        "scheduler": set(),
        "prediction-worker": {"PILOT_PDM_CREDENTIAL", "PILOT_TB_CREDENTIAL"},
        "alarm-worker": {"PILOT_TB_CREDENTIAL"},
        "work-order-worker": {"PILOT_CMMS_CREDENTIAL"},
        "status-poll-worker": {"PILOT_CMMS_CREDENTIAL"},
        "bootstrap-runner": {"PILOT_TB_CREDENTIAL"},
        "provision-runner": {"PILOT_TB_CREDENTIAL", "PILOT_CMMS_CREDENTIAL"},
        "status-runner": {"PILOT_CMMS_CREDENTIAL"},
    }
    for service_name, allowed in expected.items():
        environment = _environment(services[service_name])
        assert credentials & set(environment) == allowed
        if service_name not in {"bootstrap-runner", "status-runner"}:
            assert environment["PLATFORM_INTEGRATION_CLOSED_LOOP_ENABLED"] == "1"
    assert "PLATFORM_INTEGRATION_CMMS_WEBHOOK_SECRET_REF" not in str(COMPOSE.read_text())
    assert (
        _environment(services["work-order-worker"])["PLATFORM_INTEGRATION_FEEDBACK_POLL_SECONDS"]
        == "30"
    )
    status_environment = _environment(services["status-runner"])
    assert "PLATFORM_INTEGRATION_DATABASE_URL" not in status_environment
    assert services["status-runner"]["networks"] == ["providers"]

    def target(volume: str) -> str:
        fields = volume.rsplit(":", 2)
        return fields[-2] if fields[-1] in {"ro", "rw"} else fields[-1]

    for runner in ("bootstrap-runner", "provision-runner", "status-runner"):
        volumes = services[runner]["volumes"]
        assert all("/secrets" not in volume for volume in volumes)
        assert all("PILOT_RUNTIME_DIR" not in volume for volume in volumes)
    for runner in ("bootstrap-runner", "provision-runner"):
        assert "${PILOT_STATE_DIR:?required}:/runtime" in services[runner]["volumes"]
    for runner in ("bootstrap-runner", "provision-runner", "status-runner"):
        assert (
            "${PILOT_STATE_LOCK_FILE:?required}:/tmp/pilot-state.lock:ro"
            in services[runner]["volumes"]
        )
        assert _environment(services[runner])["PILOT_STATE_LOCK_FILE"] == ("/tmp/pilot-state.lock")
    assert [target(volume) for volume in services["status-runner"]["volumes"]] == [
        "/pilot",
        "/runtime/receipts",
        "/tmp/pilot-state.lock",
    ]


def test_compose_renders_with_complete_isolated_runtime(tmp_path: Path) -> None:
    env_file = _write_runtime(tmp_path)
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(env_file),
            "--env-file",
            str(env_file.parent / "state/closed-loop-generated.env"),
            "-f",
            str(COMPOSE),
            "--profile",
            "continuous",
            "--profile",
            "bootstrap",
            "--profile",
            "acceptance",
            "config",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    rendered = yaml.safe_load(completed.stdout)
    assert rendered["name"] == "ifactory-closed-loop-pilot"
    assert rendered["services"]["browser-gateway"]["ports"][0]["host_ip"] == "127.0.0.1"
    assert "cmms-token" not in completed.stderr


def test_bootstrap_profile_renders_with_fresh_empty_generated_state(tmp_path: Path) -> None:
    env_file = _write_runtime(tmp_path)
    generated = env_file.parent / "state/closed-loop-generated.env"
    generated.write_text(
        "".join(f"{key}=\n" for key in GENERATED_KEYS),
        encoding="utf-8",
    )
    generated.chmod(0o600)
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(env_file),
            "--env-file",
            str(generated),
            "-f",
            str(COMPOSE),
            "--profile",
            "bootstrap",
            "config",
            "--quiet",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_browser_gateway_exposes_only_exact_action_routes() -> None:
    config = BROWSER_GATEWAY.read_text(encoding="utf-8")
    assert config.count("proxy_pass http://integration-api:8080") == 2
    assert "/work-order-plan$" in config
    assert "/actions$" in config
    assert "client_max_body_size 4k" in config
    assert 'proxy_set_header Authorization ""' in config
    assert 'proxy_set_header Cookie ""' in config
    assert "proxy_set_header X-Authorization $http_x_authorization" in config
    for forbidden in ("/healthz", "/admin", "/provision", "/metrics", "cmms-gateway"):
        assert forbidden not in config
    assert config.count("proxy_read_timeout 3600s") == 2
    assert config.count("proxy_send_timeout 3600s") == 2


def test_continuous_provider_roles_have_live_readiness_checks() -> None:
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    for role in ("alarm", "work-order", "status-poll"):
        service = services[f"{role}-worker"]
        health = service["healthcheck"]
        assert health["test"] == [
            "CMD",
            "platform-integration",
            "provider-readiness",
            "--role",
            role,
        ]
        assert service["restart"] == "unless-stopped"


def test_cmms_gateway_is_internal_and_allowlisted() -> None:
    config = CMMS_GATEWAY.read_text(encoding="utf-8")
    assert "listen 8080" in config
    for route in (
        "/api/auth/me",
        "/api/assets",
        "/api/work-orders/by-external-ref",
        "/api/work-orders/search",
        "/api/work-orders",
        "/change-status$",
    ):
        assert route in config
    assert "location / { return 404; }" in config
    assert "/api/auth/signup" not in config
    assert "/api/auth/signin" not in config
    assert "proxy_pass_request_headers off" in config
    assert config.count("location = /api/work-orders/search") == 1
    assert "limit_except PATCH" in config
    assert config.count("rewrite ^/api/(.*)$ /$1 break;") == 2
    assert "client_max_body_size 4k" in config
    assert 'proxy_set_header Cookie ""' in config


def test_examples_and_compose_do_not_contain_committed_secret_values() -> None:
    example = (ROOT / "deploy/compose/closed-loop-pilot.env.example").read_text(encoding="utf-8")
    assert "Bearer " not in example
    assert '"value":"' not in example
    assert "password=test" not in example.lower()
    compose = COMPOSE.read_text(encoding="utf-8")
    assert "cmms_bearer" not in compose
    assert "api-key" not in compose.lower()


def test_runner_mount_exposes_only_generated_state() -> None:
    def keys(path: Path) -> set[str]:
        return {
            line.split("=", 1)[0]
            for line in path.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        }

    base_path = ROOT / "deploy/compose/closed-loop-pilot.env.example"
    generated_path = ROOT / "deploy/compose/closed-loop-generated.env.example"
    base_keys = keys(base_path)
    generated_keys = keys(generated_path)
    assert generated_keys == set(GENERATED_KEYS)
    assert base_keys.isdisjoint(generated_keys)
    assert {
        "PILOT_PDM_CREDENTIAL",
        "PILOT_TB_CREDENTIAL",
        "VALEO_PDM_PREDICTION_V2_BEARER_TOKEN",
        "INTEGRATION_POSTGRES_PASSWORD",
        "CMMS_POSTGRES_PASSWORD_FILE",
        "CMMS_JWT_SECRET_FILE",
        "CMMS_ADMIN_PASSWORD_FILE",
        "MINIO_ROOT_USER_FILE",
        "MINIO_ROOT_PASSWORD_FILE",
    }.isdisjoint(generated_keys)

    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    for runner in ("bootstrap-runner", "provision-runner"):
        assert _environment(services[runner])["PILOT_ENV_FILE"] == (
            "/runtime/closed-loop-generated.env"
        )
        assert "${PILOT_STATE_DIR:?required}:/runtime" in services[runner]["volumes"]
        assert (
            "${PILOT_STATE_LOCK_FILE:?required}:/tmp/pilot-state.lock:ro"
            in services[runner]["volumes"]
        )
    assert "PILOT_STATE_LOCK_FILE=" in base_path.read_text(encoding="utf-8")
    assert "PILOT_STATE_LOCK_FILE" not in generated_keys


def test_wrapper_scrubs_ambient_compose_overrides(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    script_dir = repo / "deploy/compose/scripts"
    script_dir.mkdir(parents=True)
    shutil.copy2(PILOT_WRAPPER, repo / "scripts/closed-loop-pilot.sh")
    for name in ("pilot_preflight.py", "pilot_operation_lock.py"):
        shutil.copy2(ROOT / "deploy/compose/scripts" / name, script_dir / name)
    shutil.copy2(COMPOSE, repo / "deploy/compose/closed-loop-pilot.yml")

    runtime = repo / ".runtime"
    runtime.mkdir(mode=0o700)
    env_file = _write_runtime(runtime)
    state = runtime / "closed-loop/state"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "capture.json"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/usr/bin/python3\n"
        "import json, os, sys\n"
        f"open({str(capture)!r}, 'w').write(json.dumps({{'env': dict(os.environ), 'args': sys.argv[1:]}}))\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    wrapper = repo / "scripts/closed-loop-pilot.sh"
    wrapper.write_text(
        wrapper.read_text(encoding="utf-8").replace(
            'safe_path="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"',
            f'safe_path="{fake_bin}:/usr/bin:/bin"',
        ),
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    hostile = {
        "PATH": os.environ["PATH"],
        "PILOT_STATE_DIR": "/tmp/attacker-state",
        "PILOT_STATE_LOCK_FILE": "/tmp/attacker-lock",
        "PILOT_HOST_UID": "0",
        "PILOT_HOST_GID": "0",
        "PILOT_PDM_CREDENTIAL": "attacker",
        "PILOT_TB_CREDENTIAL": "attacker",
        "PILOT_CMMS_CREDENTIAL": "attacker",
        "CMMS_POSTGRES_PASSWORD_FILE": "/tmp/attacker",
        "CMMS_JWT_SECRET_FILE": "/tmp/attacker",
        "CMMS_ADMIN_PASSWORD_FILE": "/tmp/attacker",
        "MINIO_ROOT_USER_FILE": "/tmp/attacker",
        "MINIO_ROOT_PASSWORD_FILE": "/tmp/attacker",
        "COMPOSE_FILE": "/tmp/attacker.yml",
        "COMPOSE_ENV_FILES": "/tmp/attacker.env",
        "COMPOSE_PROJECT_NAME": "attacker",
        "PILOT_ENV_FILE": "/tmp/attacker.env",
        "IFACTORY_PILOT_OPERATION_LOCK_FD": "999",
    }
    completed = subprocess.run(
        [str(wrapper), "config"],
        cwd=repo,
        env=hostile,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    captured = json.loads(capture.read_text(encoding="utf-8"))
    assert (set(hostile) - {"PATH"}).isdisjoint(captured["env"])
    assert set(captured["env"]) <= {"PATH", "LC_CTYPE"}
    args = captured["args"]
    env_positions = [index for index, value in enumerate(args) if value == "--env-file"]
    assert len(env_positions) == 2
    assert args[env_positions[0] + 1] == str(env_file)
    assert args[env_positions[1] + 1] == str(state / "closed-loop-generated.env")
    assert not any("attacker" in value for value in args)


def test_pilot_wrapper_enforces_verified_recovery_wait_and_complete_down_scope() -> None:
    wrapper = PILOT_WRAPPER.read_text(encoding="utf-8")
    assert wrapper.count("platform-integration provision-verify") == 3
    assert "up -d --build --wait --wait-timeout 300" in wrapper
    assert "--profile continuous --profile bootstrap --profile acceptance" in wrapper
    assert "pilot_cmms_status.py plan" in wrapper
    assert "pilot_cmms_status.py apply" in wrapper
    assert "platform-integration closed-loop-acceptance-verify" in wrapper
