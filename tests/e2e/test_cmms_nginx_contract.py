"""Offline contract for CMMS Nginx rendering and fail-closed authority."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import pickle
import re
import stat
import subprocess
from dataclasses import FrozenInstanceError, dataclass, field, replace
from datetime import datetime, timezone
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any

import pytest

import ifactory_cmms_deploy
from e2e.support import SafeRuntimeFixture, ScriptedRunner
from ifactory_cmms_deploy import gateway as gateway_module
from ifactory_cmms_deploy.config import RuntimeConfig
from ifactory_cmms_deploy.errors import DeploymentError
from ifactory_cmms_deploy.gateway import (
    Gateway,
    GatewayEvidence,
    GatewayEvidencePurpose,
    RenderedGateway,
    RuntimeApiKeyRoutes,
    gateway_listen_lines,
)
from ifactory_cmms_deploy.process import CommandResult
from ifactory_cmms_deploy.records import (
    ActionCode,
    ClaimedApplyContext,
    ConfirmedDeploymentPlan,
    DeploymentPlan,
    FailClosedEvidence,
    GatewayMode,
    LicenseMode,
    Operation,
    PlannedAction,
    RuntimeProfile,
    _create_gateway_fail_closed_authority_parts,
    claim_plan_application,
    load_confirmed_plan,
    reserve_plan_attempt,
    write_plan,
)


ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "deploy/gateway/cmms-development-nginx.conf.template"
ROUTES_MANIFEST = ROOT / "deploy/cmms/manifests/runtime-api-key-routes.json"
PLATFORM_CLIENT = (
    ROOT
    / "components/platform-integration/src/platform_integration/clients/cmms.py"
)
CMMS_OPENAPI_CONTRACT = ROOT / "contracts/openapi/cmms-integration-v1.yaml"
EXPECTED_GITLINK = "025d72a0928514ab73495b034ffa83c22627dc49"
EXPECTED_CLIENT_SHA256 = (
    "11ae82ce14b895de35cf8caf8a52d7b27ff4ea00ae41960822f0b392aed0f85d"
)
EXPECTED_CONTRACT_SHA256 = (
    "c3d403630f64770facca412b29edd0bebc85451de7444a40fd6dca7f8af73dc9"
)
EQUIPMENT_UUID_PATTERN = (
    r"^/api/assets/by-equipment-id/[0-9a-f]{8}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
IDEMPOTENCY_PATTERN = (
    r"^pilot-asset:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$"
)
DENIAL_BODY = '{"success":false,"message":"API key route denied"}'
UNIT_GENERATION = "a" * 64
RECOVERY_CONTAINER_ID = "d" * 64


def _manifest() -> dict[str, Any]:
    value = json.loads(ROUTES_MANIFEST.read_text(encoding="utf-8"))
    assert type(value) is dict
    return value


def _render(
    mode: GatewayMode = GatewayMode.LOOPBACK,
    gateway_ip: IPv4Address | None = None,
) -> RenderedGateway:
    return Gateway(template=TEMPLATE, runner=ScriptedRunner()).render(
        mode=mode,
        gateway_ip=gateway_ip,
        unit_generation=UNIT_GENERATION,
    )


def _location(text: str, prefix: str) -> str:
    start = text.index(prefix)
    brace = text.index("{", start)
    depth = 0
    for index in range(brace, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"unterminated location: {prefix}")


def _map_body(text: str, output_variable: str) -> str:
    match = re.search(
        rf"(?m)^\s*map\s+[^\n]+\s+\${re.escape(output_variable)}\s*\{{",
        text,
    )
    assert match is not None, f"missing Nginx map: {output_variable}"
    brace = text.index("{", match.start())
    depth = 0
    for index in range(brace, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[brace + 1 : index]
    raise AssertionError(f"unterminated Nginx map: {output_variable}")


def _map_value(text: str, output_variable: str, source: str) -> str:
    default: str | None = None
    exact: list[tuple[str, str]] = []
    regex_rows: list[tuple[re.Pattern[str], str]] = []
    for raw_line in _map_body(text, output_variable).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r'(?:("[^"]*")|(\S+))\s+("[^"]*"|\S+);', line)
        assert match is not None, f"unparseable Nginx map row: {line}"
        raw_key = match.group(1) or match.group(2)
        raw_value = match.group(3)
        key = raw_key[1:-1] if raw_key.startswith('"') else raw_key
        value = raw_value[1:-1] if raw_value.startswith('"') else raw_value
        if key == "default":
            assert default is None
            default = value
        elif key.startswith("~*"):
            regex_rows.append((re.compile(key[2:], re.IGNORECASE), value))
        elif key.startswith("~"):
            regex_rows.append((re.compile(key[1:]), value))
        else:
            exact.append((key, value))
    for key, value in exact:
        if key == source:
            return value
    for pattern, value in regex_rows:
        if pattern.search(source) is not None:
            return value
    assert default is not None
    return default


def _rendered_policy_allows(
    text: str,
    *,
    method: str,
    uri: str,
    api_key: str,
    idempotency_key: str = "",
) -> bool:
    request = f"{method}:{uri}"
    present = _map_value(text, "api_key_present", api_key)
    route = _map_value(text, "api_key_route_allowed", request)
    asset = _map_value(text, "api_key_asset_create", request)
    idempotent = _map_value(text, "api_key_idempotency_valid", idempotency_key)
    decision = _map_value(
        text,
        "api_key_route_denied",
        f"{present}:{route}:{asset}:{idempotent}",
    )
    assert decision in {"0", "1"}
    return decision == "0"


def test_runtime_api_key_manifest_is_exact_and_residual_trust_is_explicit() -> None:
    manifest = _manifest()

    assert set(manifest) == {
        "schema_version",
        "bindings",
        "routes",
        "asset_create_idempotency_key_pattern",
        "trusted_client_residual",
    }
    assert manifest["schema_version"] == 1
    assert manifest["bindings"] == {
        "platform_integration_gitlink": EXPECTED_GITLINK,
        "platform_integration_client_sha256": EXPECTED_CLIENT_SHA256,
        "cmms_openapi_contract_sha256": EXPECTED_CONTRACT_SHA256,
    }
    assert manifest["routes"] == [
        {"method": "GET", "path": "/api/auth/me", "path_kind": "exact"},
        {
            "method": "POST",
            "path": "/api/work-orders/search",
            "path_kind": "exact",
        },
        {
            "method": "GET",
            "path": EQUIPMENT_UUID_PATTERN,
            "path_kind": "regex",
        },
        {"method": "POST", "path": "/api/assets", "path_kind": "exact"},
    ]
    assert manifest["asset_create_idempotency_key_pattern"] == IDEMPOTENCY_PATTERN
    assert manifest["trusted_client_residual"] == (
        "asset-create-equipment-id-body-is-trusted-client-and-confirmed-plan-"
        "responsibility-not-gateway-validation"
    )


def test_manifest_bindings_match_current_client_and_openapi_bytes() -> None:
    manifest = _manifest()
    bindings = manifest["bindings"]

    assert hashlib.sha256(PLATFORM_CLIENT.read_bytes()).hexdigest() == bindings[
        "platform_integration_client_sha256"
    ]
    assert hashlib.sha256(CMMS_OPENAPI_CONTRACT.read_bytes()).hexdigest() == bindings[
        "cmms_openapi_contract_sha256"
    ]


def test_manifest_gitlink_binding_matches_the_actual_superproject_entry() -> None:
    completed = subprocess.run(
        (
            "/usr/bin/git",
            "-c",
            "core.pager=cat",
            "-C",
            str(ROOT),
            "ls-tree",
            "HEAD",
            "--",
            "components/platform-integration",
        ),
        cwd=ROOT,
        env={
            "PATH": "/usr/bin:/bin",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert completed.stdout == (
        f"160000 commit {EXPECTED_GITLINK}\tcomponents/platform-integration\n"
    )


def test_runtime_routes_loader_exposes_one_immutable_exact_policy() -> None:
    routes = RuntimeApiKeyRoutes.load(ROUTES_MANIFEST)

    assert routes.platform_integration_gitlink == EXPECTED_GITLINK
    assert routes.platform_integration_client_sha256 == EXPECTED_CLIENT_SHA256
    assert routes.cmms_openapi_contract_sha256 == EXPECTED_CONTRACT_SHA256
    assert routes.asset_create_idempotency_key_pattern == IDEMPOTENCY_PATTERN
    assert tuple((row.method, row.path_kind, row.path) for row in routes.routes) == tuple(
        (row["method"], row["path_kind"], row["path"])
        for row in _manifest()["routes"]
    )
    with pytest.raises((FrozenInstanceError, AttributeError)):
        routes.platform_integration_gitlink = "0" * 40  # type: ignore[misc]


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param("unknown-field", id="unknown-field"),
        pytest.param("missing-route", id="missing-route"),
        pytest.param("route-widening", id="route-widening"),
        pytest.param("gitlink-drift", id="gitlink-drift"),
        pytest.param("client-drift", id="client-drift"),
        pytest.param("contract-drift", id="contract-drift"),
        pytest.param("idempotency-widening", id="idempotency-widening"),
        pytest.param("residual-claim-removed", id="residual-claim-removed"),
    ],
)
def test_runtime_routes_loader_rejects_drift_and_widening(
    tmp_path: Path,
    mutation: str,
) -> None:
    candidate = copy.deepcopy(_manifest())
    if mutation == "unknown-field":
        candidate["allow_all"] = True
    elif mutation == "missing-route":
        candidate["routes"].pop()
    elif mutation == "route-widening":
        candidate["routes"][0]["path"] = "/api/"
    elif mutation == "gitlink-drift":
        candidate["bindings"]["platform_integration_gitlink"] = "0" * 40
    elif mutation == "client-drift":
        candidate["bindings"]["platform_integration_client_sha256"] = "0" * 64
    elif mutation == "contract-drift":
        candidate["bindings"]["cmms_openapi_contract_sha256"] = "0" * 64
    elif mutation == "idempotency-widening":
        candidate["asset_create_idempotency_key_pattern"] = ".*"
    else:
        candidate["trusted_client_residual"] = "gateway-validates-the-body"
    path = tmp_path / "routes.json"
    path.write_text(json.dumps(candidate, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(DeploymentError) as caught:
        RuntimeApiKeyRoutes.load(path)

    assert caught.value.code == "CMMS-E043"


def test_runtime_routes_loader_rejects_symlink_manifest(tmp_path: Path) -> None:
    link = tmp_path / "routes.json"
    link.symlink_to(ROUTES_MANIFEST)

    with pytest.raises(DeploymentError) as caught:
        RuntimeApiKeyRoutes.load(link)

    assert caught.value.code == "CMMS-E043"


def test_loopback_gateway_has_only_two_exact_listeners() -> None:
    rendered = _render()

    assert "listen 127.0.0.1:3000;" in rendered.text
    assert "listen [::1]:3000;" in rendered.text
    assert "0.0.0.0" not in rendered.text
    assert "listen [::]:3000;" not in rendered.text
    assert "listen 172.17.0.1:3000;" not in rendered.text
    assert rendered.mode is GatewayMode.LOOPBACK
    assert rendered.gateway_ip is None
    assert rendered.unit_generation == UNIT_GENERATION
    assert rendered.sha256 == hashlib.sha256(rendered.text.encode("utf-8")).hexdigest()


def test_dual_gateway_adds_only_verified_bridge_address() -> None:
    rendered = _render(GatewayMode.DUAL, IPv4Address("172.17.0.1"))

    assert "listen 127.0.0.1:3000;" in rendered.text
    assert "listen [::1]:3000;" in rendered.text
    assert "listen 172.17.0.1:3000;" in rendered.text
    assert "0.0.0.0" not in rendered.text
    assert "listen [::]:3000;" not in rendered.text
    assert "192.168." not in rendered.text
    assert rendered.gateway_ip == IPv4Address("172.17.0.1")


@pytest.mark.parametrize(
    ("mode", "gateway_ip"),
    [
        pytest.param(GatewayMode.LOOPBACK, IPv4Address("172.17.0.1"), id="loopback-extra"),
        pytest.param(GatewayMode.DUAL, None, id="dual-missing"),
        pytest.param(GatewayMode.DUAL, IPv4Address("0.0.0.0"), id="wildcard"),
        pytest.param(GatewayMode.DUAL, IPv4Address("127.0.0.1"), id="loopback"),
        pytest.param(GatewayMode.DUAL, IPv4Address("169.254.1.1"), id="link-local"),
        pytest.param(GatewayMode.DUAL, IPv4Address("224.0.0.1"), id="multicast"),
        pytest.param(GatewayMode.DUAL, IPv4Address("192.168.1.1"), id="lan"),
    ],
)
def test_gateway_listeners_reject_unverified_or_cross_mode_addresses(
    mode: GatewayMode,
    gateway_ip: IPv4Address | None,
) -> None:
    with pytest.raises(DeploymentError) as caught:
        gateway_listen_lines(mode, gateway_ip)

    assert caught.value.code == "CMMS-E042"


@pytest.mark.parametrize("value", ["", "a" * 63, "A" * 64, "g" * 64, None])
def test_render_rejects_noncanonical_unit_generation(value: object) -> None:
    with pytest.raises(DeploymentError):
        Gateway(template=TEMPLATE, runner=ScriptedRunner()).render(
            mode=GatewayMode.LOOPBACK,
            gateway_ip=None,
            unit_generation=value,  # type: ignore[arg-type]
        )


def test_render_is_deterministic_complete_and_contains_no_placeholder() -> None:
    first = _render()
    second = _render()

    assert first == second
    assert first.text.endswith("\n")
    assert "@" not in first.text
    assert "{{" not in first.text
    assert "}}" not in first.text
    assert f"X-iFactory-CMMS-Gateway {UNIT_GENERATION}" in first.text
    assert first.text.count("proxy_hide_header X-iFactory-CMMS-Gateway;") == 1
    assert first.text.count("proxy_hide_header X-iFactory-CMMS-Policy;") == 1


def test_frontend_and_api_routes_use_only_exact_loopback_upstreams() -> None:
    text = _render().text
    frontend = _location(text, "location /")
    api = _location(text, "location /api/")

    assert "proxy_pass http://127.0.0.1:3001;" in frontend
    assert "proxy_http_version 1.1;" in frontend
    assert "proxy_set_header Upgrade $http_upgrade;" in frontend
    assert "proxy_set_header Connection $connection_upgrade;" in frontend
    assert "proxy_pass http://127.0.0.1:8082/;" in api
    assert "127.0.0.1:3001" not in api
    assert "127.0.0.1:8082" not in frontend


def test_storage_route_preserves_sigv4_host_and_suppresses_secret_logs() -> None:
    text = _render().text
    storage = _location(text, "location /storage/")

    assert "proxy_pass http://127.0.0.1:9000/;" in storage
    assert "proxy_set_header Host 127.0.0.1:9000;" in storage
    assert "proxy_http_version 1.1;" in storage
    assert "proxy_request_buffering off;" in storage
    assert "access_log off;" in storage
    assert "error_log /dev/null crit;" in storage
    assert "client_max_body_size 500M;" in storage
    assert "$request_uri" not in storage
    assert "$args" not in storage
    assert "$http_" not in storage


def test_global_access_log_is_bounded_to_method_normalized_uri_and_status() -> None:
    text = _render().text
    match = re.search(r"log_format\s+cmms_bounded\s+(['\"])(.*?)\1\s*;", text)
    assert match is not None
    fields = match.group(2).split()

    assert fields == ["$request_method", "$uri", "$status"]
    assert "$request" not in fields
    assert "$request_uri" not in fields
    assert "$args" not in fields
    assert all(not field.startswith("$http_") for field in fields)


def test_rendered_api_key_policy_contains_only_four_exact_allowlist_rows() -> None:
    text = _render().text

    for method, path in (
        ("GET", "/api/auth/me"),
        ("POST", "/api/work-orders/search"),
        ("GET", EQUIPMENT_UUID_PATTERN),
        ("POST", "/api/assets"),
    ):
        assert method in text
        assert path in text
    assert IDEMPOTENCY_PATTERN in text
    assert "/api/*" not in text
    assert "^/api/.*" not in text
    assert "equipment_id" not in text


@pytest.mark.parametrize(
    ("method", "uri", "idempotency_key"),
    [
        pytest.param("GET", "/api/auth/me", "", id="auth-me"),
        pytest.param("POST", "/api/work-orders/search", "", id="work-order-search"),
        pytest.param(
            "GET",
            "/api/assets/by-equipment-id/123e4567-e89b-12d3-a456-426614174000",
            "",
            id="asset-by-equipment-id",
        ),
        pytest.param(
            "POST",
            "/api/assets",
            "pilot-asset:123e4567-e89b-12d3-a456-426614174000",
            id="asset-create",
        ),
    ],
)
def test_rendered_api_key_policy_semantically_allows_only_the_bound_shapes(
    method: str,
    uri: str,
    idempotency_key: str,
) -> None:
    assert _rendered_policy_allows(
        _render().text,
        method=method,
        uri=uri,
        api_key="present",
        idempotency_key=idempotency_key,
    )


@pytest.mark.parametrize(
    ("method", "uri", "idempotency_key"),
    [
        pytest.param("get", "/api/auth/me", "", id="method-case"),
        pytest.param("GET", "/api/auth/me/", "", id="trailing-slash"),
        pytest.param("GET", "/api/auth/me-extra", "", id="exact-prefix"),
        pytest.param("POST", "/api/work-orders/search/", "", id="search-slash"),
        pytest.param(
            "GET",
            "/api/assets/by-equipment-id/123E4567-E89B-12D3-A456-426614174000",
            "",
            id="uuid-case",
        ),
        pytest.param(
            "GET",
            "/api/assets/by-equipment-id/123e4567-e89b-12d3-a456-426614174000/more",
            "",
            id="uuid-suffix",
        ),
        pytest.param("POST", "/api/assets", "", id="missing-idempotency"),
        pytest.param(
            "POST",
            "/api/assets",
            "pilot-asset:123E4567-E89B-12D3-A456-426614174000",
            id="idempotency-case",
        ),
        pytest.param(
            "POST",
            "/api/assets",
            "other:123e4567-e89b-12d3-a456-426614174000",
            id="idempotency-prefix",
        ),
        pytest.param("DELETE", "/api/assets", "", id="unbound-method"),
    ],
)
def test_rendered_api_key_policy_semantically_denies_near_misses(
    method: str,
    uri: str,
    idempotency_key: str,
) -> None:
    assert not _rendered_policy_allows(
        _render().text,
        method=method,
        uri=uri,
        api_key="present",
        idempotency_key=idempotency_key,
    )


def test_requests_without_an_api_key_do_not_enter_the_runtime_key_allowlist() -> None:
    assert _rendered_policy_allows(
        _render().text,
        method="DELETE",
        uri="/api/operator-session-route",
        api_key="",
    )


def test_api_key_denial_is_json_policy_response_before_api_proxy() -> None:
    text = _render().text
    api = _location(text, "location /api/")

    assert "default_type application/json;" in text
    assert "X-iFactory-CMMS-Policy api-key-route-denied" in text
    assert DENIAL_BODY in text
    assert "return 403" in text
    assert "error_page" not in text or "@api_key_route_denied" in text
    denial_offset = text.index(DENIAL_BODY)
    proxy_offset = api.index("proxy_pass") + text.index("location /api/")
    assert denial_offset < proxy_offset
    assert "<html" not in text.casefold()
    assert "return 301" not in text
    assert "return 302" not in text


def test_nginx_pid_proxy_temp_paths_and_log_sinks_are_bounded() -> None:
    text = _render().text

    assert re.search(r"(?m)^pid /tmp/nginx\.pid;$", text) is not None
    assert re.search(r"(?m)^\s*client_body_temp_path /tmp/client_body;$", text) is not None
    assert re.search(r"(?m)^\s*proxy_temp_path /tmp/proxy;$", text) is not None
    assert re.findall(r"(?m)^\s*[a-z_]+_temp_path\s+(\S+);$", text) == [
        "/tmp/client_body",
        "/tmp/proxy",
    ]
    assert "access_log /dev/stdout cmms_bounded;" in text
    assert "error_log /dev/stderr warn;" in text
    storage = _location(text, "location /storage/")
    assert "access_log off;" in storage
    assert "error_log /dev/null crit;" in storage


def test_public_gateway_is_render_only_and_privileged_types_are_not_exported() -> None:
    gateway = Gateway(template=TEMPLATE, runner=ScriptedRunner())

    assert isinstance(
        gateway.render(
            mode=GatewayMode.LOOPBACK,
            gateway_ip=None,
            unit_generation=UNIT_GENERATION,
        ),
        RenderedGateway,
    )
    for privileged in (
        "begin_fail_closed",
        "require_external_listener_absent",
        "fail_closed_claimed",
        "emergency_fail_closed",
        "enable_dual",
    ):
        assert not hasattr(gateway, privileged)
    forbidden_root_exports = (
        "GatewayEvidence",
        "GatewayEvidencePurpose",
        "RuntimeApiKeyRoutes",
        "_OperationalGateway",
        "_ExactListenerInspector",
        "_create_production_gateway",
        "_create_gateway_fail_closed_authority_parts",
    )
    assert all(not hasattr(ifactory_cmms_deploy, name) for name in forbidden_root_exports)
    assert all(
        name not in getattr(gateway_module, "__all__", ())
        for name in (
            "_OperationalGateway",
            "_ExactListenerInspector",
            "_create_production_gateway",
        )
    )


def test_private_operational_adapter_and_listener_inspector_require_module_token() -> None:
    facade = Gateway(template=TEMPLATE, runner=ScriptedRunner())
    parts = _create_gateway_fail_closed_authority_parts()

    with pytest.raises((DeploymentError, TypeError)):
        gateway_module._OperationalGateway(facade)  # type: ignore[attr-defined]
    with pytest.raises((DeploymentError, TypeError)):
        gateway_module._ExactListenerInspector(  # type: ignore[attr-defined]
            parts.listener_proof_mint,
            token=object(),
        )


@pytest.mark.parametrize("capability", [GatewayEvidence, FailClosedEvidence])
def test_gateway_evidence_public_construction_is_rejected(
    capability: type[object],
) -> None:
    with pytest.raises((DeploymentError, TypeError)):
        capability()


def _private(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def _stage_operational_root(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "gateway-root"
    staged_template = root / "deploy/gateway" / TEMPLATE.name
    staged_template.parent.mkdir(parents=True)
    staged_template.write_bytes(TEMPLATE.read_bytes())
    staged_manifest = root / "deploy/cmms/manifests" / ROUTES_MANIFEST.name
    staged_manifest.parent.mkdir(parents=True)
    staged_manifest.write_bytes(ROUTES_MANIFEST.read_bytes())
    compose_source = ROOT / "deploy/compose/cmms-development.yml"
    staged_compose = root / "deploy/compose" / compose_source.name
    staged_compose.parent.mkdir(parents=True)
    staged_compose.write_bytes(compose_source.read_bytes())
    secrets = root / ".runtime/secrets"
    secret_paths = {
        name: _private(secrets / name, f"gateway-secret-{name}".encode())
        for name in (
            "postgres-password",
            "minio-root-user",
            "minio-root-password",
            "jwt-secret",
            "license-key",
            "license-file",
        )
    }
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
    return root, staged_template


def test_template_cannot_inject_an_additional_listener(tmp_path: Path) -> None:
    _root, template = _stage_operational_root(tmp_path)
    template.write_text(
        template.read_text(encoding="utf-8").replace(
            "@@LISTEN_LINES@@",
            "@@LISTEN_LINES@@\n        listen 0.0.0.0:3000;",
        ),
        encoding="utf-8",
    )

    with pytest.raises(DeploymentError):
        Gateway(template=template, runner=ScriptedRunner()).render(
            GatewayMode.LOOPBACK,
            None,
            UNIT_GENERATION,
        )


def _confirmed(
    safe_runtime: SafeRuntimeFixture,
    *,
    root: Path,
    nonce: str,
    application_id: str,
    operation: Operation = Operation.START,
    actions: tuple[PlannedAction, ...] | None = None,
) -> tuple[ConfirmedDeploymentPlan, Any]:
    base = safe_runtime.make_plan(
        operation=operation,
        actions=actions,
        bootstrap_bindings=(
            safe_runtime.bootstrap_plan_bindings if actions is not None else None
        ),
        plan_nonce=nonce,
    )
    plan = DeploymentPlan.create(
        snapshot=replace(
            base.snapshot,
            config_sha256=RuntimeConfig.load(root).config_sha256,
        ),
        operation=operation,
        profile=RuntimeProfile.DEVELOPMENT,
        license_mode=LicenseMode.OFFLINE,
        bootstrap_bindings=base.bootstrap_bindings,
        actions=base.actions,
        now=datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc),
        plan_nonce=nonce,
    )
    path, digest = write_plan(plan, safe_runtime.plans_dir)
    reservation = reserve_plan_attempt(
        path,
        confirmed_sha256=digest,
        now=plan.created_at,
        plans_dir=safe_runtime.plans_dir,
        application_id=application_id,
    )
    lease = safe_runtime.acquire_deployment_write_lease(reservation)
    confirmed = load_confirmed_plan(
        reservation,
        snapshot=plan.snapshot,
        current_bootstrap_bindings=plan.bootstrap_bindings,
        lease=lease,
    )
    return confirmed, lease


def _repair_pull_actions() -> tuple[PlannedAction, ...]:
    return (
        PlannedAction(ActionCode.GATEWAY_FAIL_CLOSED),
        PlannedAction(ActionCode.IMAGES_PULL_EXACT),
        PlannedAction(ActionCode.LICENSE_VERIFY_OFFLINE),
        PlannedAction(ActionCode.PROCESS_CREATE_API_PERMIT),
        PlannedAction(ActionCode.PROCESS_START_API),
        PlannedAction(ActionCode.PROCESS_START_FRONTEND),
        PlannedAction(ActionCode.READINESS_REQUIRE_API_LOOPBACK),
        PlannedAction(ActionCode.READINESS_REQUIRE_LOOPBACK),
        PlannedAction(ActionCode.GATEWAY_ENABLE_DUAL),
        PlannedAction(ActionCode.READINESS_REQUIRE_DUAL),
    )


@dataclass
class OperationalGatewayRunner:
    gateway_ipv4: str = "172.17.0.1"
    local_ipv4: str = "172.17.0.1"
    gateway_ipv4_sequence: tuple[str, ...] = ()
    listener_count: int = 0
    listener_stdout: str | None = None
    listener_returncode: int = 0
    listener_stderr: str = ""
    listener_hook: Any | None = None
    nginx_running: bool = False
    probe_owner: bool = True
    fail_operation: str | None = None
    failure_kind: str = "nonzero"
    calls: list[Any] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    _bridge_network_calls: int = field(default=0, init=False)
    _active_gateway_ipv4: str = field(default="", init=False)

    def run(self, spec: Any) -> Any:
        self.calls.append(spec)
        argv = tuple(spec.argv)
        label = spec.safe_label.casefold()
        if self.fail_operation is not None and self.fail_operation in label:
            self.events.append(f"fail:{self.fail_operation}")
            if self.failure_kind == "exception":
                raise RuntimeError("simulated untrusted command uncertainty")
            if self.failure_kind == "stdout":
                return CommandResult(0, "unexpected output\n", "")
            if self.failure_kind == "stderr":
                return CommandResult(0, "", "unexpected diagnostic\n")
            assert self.failure_kind == "nonzero"
            return CommandResult(1, "", "")
        if argv[:5] == (
            "/usr/bin/docker",
            "--host",
            "unix:///var/run/docker.sock",
            "network",
            "inspect",
        ):
            self.events.append("bridge-network")
            selected_gateway = self.gateway_ipv4
            if self.gateway_ipv4_sequence:
                selected_gateway = self.gateway_ipv4_sequence[
                    min(
                        self._bridge_network_calls,
                        len(self.gateway_ipv4_sequence) - 1,
                    )
                ]
                self._bridge_network_calls += 1
            self._active_gateway_ipv4 = selected_gateway
            return CommandResult(
                0,
                (
                    '[{"Name":"bridge","Options":'
                    '{"com.docker.network.bridge.name":"docker0"},'
                    '"IPAM":{"Config":[{"Subnet":"172.17.0.0/16",'
                    f'"Gateway":"{selected_gateway}"}}]}}}}]'
                ),
                "",
            )
        if argv[:6] == (
            "/usr/sbin/ip",
            "-json",
            "address",
            "show",
            "dev",
            "docker0",
        ):
            self.events.append("bridge-local")
            selected_local = (
                self._active_gateway_ipv4
                if self.gateway_ipv4_sequence
                else self.local_ipv4
            )
            return CommandResult(
                0,
                (
                    '[{"ifname":"docker0","addr_info":['
                    f'{{"family":"inet","local":"{selected_local}",'
                    '"prefixlen":16}]}]'
                ),
                "",
            )
        if argv and argv[0] in {"/usr/sbin/ss", "/usr/bin/ss"}:
            self.events.append("listener")
            if self.listener_hook is not None:
                hook = self.listener_hook
                self.listener_hook = None
                hook()
            if self.listener_stdout is not None:
                stdout = self.listener_stdout
            elif self.listener_count == 0:
                stdout = ""
            else:
                rows = "\n".join(
                    f"LISTEN 0 511 {self.gateway_ipv4}:3000 0.0.0.0:*"
                    for _ in range(self.listener_count)
                )
                stdout = rows + "\n"
            return CommandResult(
                self.listener_returncode,
                stdout,
                self.listener_stderr,
            )
        if argv and argv[0] == "/usr/bin/curl":
            url = argv[-1]
            label_suffix = "loopback" if "127.0.0.1" in url else "gateway"
            self.events.append(f"probe-{label_suffix}")
            if not self.probe_owner:
                return CommandResult(22, "", "")
            token = url.rsplit("/", 1)[-1]
            assert re.fullmatch(r"[0-9a-f]{64}", token)
            return CommandResult(0, f"{token}\n200\n", "")
        if label == "cmms-gateway-status":
            self.events.append("status")
            return CommandResult(0, "nginx\n" if self.nginx_running else "", "")
        if label == "cmms-gateway-validate":
            self.events.append("validate")
            return CommandResult(0, "", "")
        if label == "cmms-gateway-reload":
            self.events.append("reload")
            return CommandResult(0, "", "")
        if label == "cmms-gateway-stop":
            self.events.append("stop")
            self.nginx_running = False
            if self.listener_stdout is None:
                self.listener_count = 0
            return CommandResult(0, "", "")
        if label == "cmms-gateway-recovery-list":
            self.events.append("recovery-list")
            return CommandResult(
                0,
                f"{RECOVERY_CONTAINER_ID}\n" if self.nginx_running else "",
                "",
            )
        if label == "cmms-gateway-recovery-inspect":
            self.events.append("recovery-inspect")
            return CommandResult(
                0,
                json.dumps(
                    [
                        {
                            "Id": RECOVERY_CONTAINER_ID,
                            "Name": "/ifactory-cmms-dev-nginx-1",
                            "Config": {
                                "Image": gateway_module._NGINX_IMAGE,
                                "Labels": {
                                    "com.docker.compose.project": "ifactory-cmms-dev",
                                    "com.docker.compose.service": "nginx",
                                    "com.docker.compose.oneoff": "False",
                                    "com.docker.compose.container-number": "1",
                                },
                            },
                            "HostConfig": {"NetworkMode": "host"},
                            "State": {"Running": self.nginx_running},
                        }
                    ],
                    separators=(",", ":"),
                )
                + "\n",
                "",
            )
        if label == "cmms-gateway-recovery-stop":
            self.events.append("stop")
            self.nginx_running = False
            if self.listener_stdout is None:
                self.listener_count = 0
            return CommandResult(0, f"{RECOVERY_CONTAINER_ID}\n", "")
        raise AssertionError(f"unexpected gateway command: {spec.safe_label} {argv!r}")


def _operational_gateway(root: Path, template: Path, runner: Any) -> Any:
    return gateway_module._create_production_gateway(  # type: ignore[attr-defined]
        template=template,
        runner=runner,
        root=root,
        controller_pid=os.getpid(),
    )


def _claim_context(
    *,
    root: Path,
    template: Path,
    runner: OperationalGatewayRunner,
    safe_runtime: SafeRuntimeFixture,
    nonce: str,
    application_id: str,
) -> tuple[Any, ConfirmedDeploymentPlan, Any, ClaimedApplyContext]:
    confirmed, lease = _confirmed(
        safe_runtime,
        root=root,
        nonce=nonce,
        application_id=application_id,
    )
    gateway = _operational_gateway(root, template, runner)
    preclaim = gateway.begin_fail_closed(confirmed, lease, "preclaim")
    fail_closed = gateway.require_external_listener_absent(preclaim)
    context = claim_plan_application(
        confirmed,
        safe_runtime.plans_dir,
        lease,
        fail_closed,
    )
    return gateway, confirmed, lease, context


def _assert_gateway_runtime_modes(root: Path) -> Path:
    runtime = root / ".runtime/cmms-nginx"
    metadata = runtime.lstat()
    assert stat.S_ISDIR(metadata.st_mode)
    assert stat.S_IMODE(metadata.st_mode) == 0o755
    assert metadata.st_uid == os.getuid()
    published = [path for path in runtime.iterdir() if not path.name.startswith(".")]
    assert len(published) == 1
    config = published[0]
    assert config.name == "nginx.conf"
    config_metadata = config.lstat()
    assert stat.S_ISREG(config_metadata.st_mode)
    assert stat.S_IMODE(config_metadata.st_mode) == 0o444
    assert config_metadata.st_uid == os.getuid()
    assert config_metadata.st_nlink == 1
    return config


def test_production_gateway_preclaim_exact_state_machine_and_replay(
    tmp_path: Path,
) -> None:
    root, template = _stage_operational_root(tmp_path)
    safe_runtime = SafeRuntimeFixture(tmp_path / "plans")
    confirmed, lease = _confirmed(
        safe_runtime,
        root=root,
        nonce="1" * 32,
        application_id="2" * 32,
    )
    runner = OperationalGatewayRunner()
    gateway = _operational_gateway(root, template, runner)
    try:
        preclaim = gateway.begin_fail_closed(confirmed, lease, "preclaim")
        assert type(preclaim) is GatewayEvidence
        assert preclaim.purpose is GatewayEvidencePurpose.PRECLAIM
        loaded = _assert_gateway_runtime_modes(root)
        assert f"X-iFactory-CMMS-Gateway {confirmed.plan.snapshot.unit_generation}" in loaded.read_text(
            encoding="utf-8"
        )

        evidence = gateway.require_external_listener_absent(preclaim)
        assert type(evidence) is FailClosedEvidence
        context = claim_plan_application(
            confirmed,
            safe_runtime.plans_dir,
            lease,
            evidence,
        )
        assert type(context) is ClaimedApplyContext

        with pytest.raises(DeploymentError):
            gateway.require_external_listener_absent(preclaim)
        with pytest.raises(DeploymentError):
            claim_plan_application(
                confirmed,
                safe_runtime.plans_dir,
                lease,
                evidence,
            )
    finally:
        lease.close()


def test_candidate_validation_never_implicitly_pulls_nginx_image(
    tmp_path: Path,
) -> None:
    root, template = _stage_operational_root(tmp_path)
    safe_runtime = SafeRuntimeFixture(tmp_path / "plans")
    confirmed, lease = _confirmed(
        safe_runtime,
        root=root,
        nonce="4" * 32,
        application_id="6" * 32,
    )
    runner = OperationalGatewayRunner()
    gateway = _operational_gateway(root, template, runner)
    try:
        gateway.begin_fail_closed(confirmed, lease, "preclaim")
    finally:
        lease.close()

    validate_calls = [
        call for call in runner.calls if call.safe_label == "cmms-gateway-validate"
    ]
    assert len(validate_calls) == 1
    argv = tuple(validate_calls[0].argv)
    pull_index = argv.index("--pull")
    image_index = argv.index(gateway_module._NGINX_IMAGE)
    assert argv[pull_index : pull_index + 2] == ("--pull", "never")
    assert pull_index < image_index


@pytest.mark.parametrize("nginx_running", [False, True])
def test_explicit_image_pull_plan_can_establish_cold_preclaim_without_image(
    tmp_path: Path,
    nginx_running: bool,
) -> None:
    root, template = _stage_operational_root(tmp_path)
    safe_runtime = SafeRuntimeFixture(tmp_path / "plans")
    confirmed, lease = _confirmed(
        safe_runtime,
        root=root,
        nonce="0" * 32,
        application_id="1" * 32,
        operation=Operation.REPAIR,
        actions=_repair_pull_actions(),
    )
    runner = OperationalGatewayRunner(
        nginx_running=nginx_running,
        fail_operation="validate",
    )
    gateway = _operational_gateway(root, template, runner)
    try:
        preclaim = gateway.begin_fail_closed(confirmed, lease, "cold-preclaim")
        fail_closed = gateway.require_external_listener_absent(preclaim)
        context = claim_plan_application(
            confirmed,
            safe_runtime.plans_dir,
            lease,
            fail_closed,
        )

        assert type(context) is ClaimedApplyContext
        assert "validate" not in runner.events
        assert "reload" not in runner.events
        assert "pull" not in tuple(
            token for call in runner.calls for token in tuple(call.argv)
        )
        if nginx_running:
            assert "stop" in runner.events
            assert runner.events.index("stop") < runner.events.index("listener")
        _assert_gateway_runtime_modes(root)
    finally:
        lease.close()


def test_gateway_runtime_creation_is_not_weakened_by_controller_umask(
    tmp_path: Path,
) -> None:
    root, template = _stage_operational_root(tmp_path)
    safe_runtime = SafeRuntimeFixture(tmp_path / "plans")
    confirmed, lease = _confirmed(
        safe_runtime,
        root=root,
        nonce="2" * 32,
        application_id="3" * 32,
    )
    gateway = _operational_gateway(root, template, OperationalGatewayRunner())
    previous_umask = os.umask(0o077)
    try:
        gateway.begin_fail_closed(confirmed, lease, "umask-preclaim")
        _assert_gateway_runtime_modes(root)
    finally:
        os.umask(previous_umask)
        lease.close()


def test_issued_gateway_evidence_rejects_copy_deepcopy_and_pickle(
    tmp_path: Path,
) -> None:
    root, template = _stage_operational_root(tmp_path)
    safe_runtime = SafeRuntimeFixture(tmp_path / "plans")
    confirmed, lease = _confirmed(
        safe_runtime,
        root=root,
        nonce="b" * 32,
        application_id="c" * 32,
    )
    gateway = _operational_gateway(root, template, OperationalGatewayRunner())
    try:
        preclaim = gateway.begin_fail_closed(confirmed, lease, "preclaim")
        for operation in (copy.copy, copy.deepcopy, pickle.dumps):
            with pytest.raises((DeploymentError, TypeError)):
                operation(preclaim)

        fail_closed = gateway.require_external_listener_absent(preclaim)
        for operation in (copy.copy, copy.deepcopy, pickle.dumps):
            with pytest.raises((DeploymentError, TypeError)):
                operation(fail_closed)
    finally:
        lease.close()


def test_preclaim_evidence_is_bound_to_one_operational_authority(
    tmp_path: Path,
) -> None:
    root, template = _stage_operational_root(tmp_path)
    safe_runtime = SafeRuntimeFixture(tmp_path / "plans")
    confirmed, lease = _confirmed(
        safe_runtime,
        root=root,
        nonce="3" * 32,
        application_id="4" * 32,
    )
    first = _operational_gateway(root, template, OperationalGatewayRunner())
    second = _operational_gateway(root, template, OperationalGatewayRunner())
    try:
        preclaim = first.begin_fail_closed(confirmed, lease, "preclaim")
        with pytest.raises(DeploymentError):
            second.require_external_listener_absent(preclaim)
        assert type(first.require_external_listener_absent(preclaim)) is FailClosedEvidence
    finally:
        lease.close()


def test_cross_plan_application_or_lease_cannot_begin_preclaim_mint(
    tmp_path: Path,
) -> None:
    root, template = _stage_operational_root(tmp_path)
    first_runtime = SafeRuntimeFixture(tmp_path / "first-plans")
    second_runtime = SafeRuntimeFixture(tmp_path / "second-plans")
    first, first_lease = _confirmed(
        first_runtime,
        root=root,
        nonce="6" * 32,
        application_id="7" * 32,
    )
    second, second_lease = _confirmed(
        second_runtime,
        root=root,
        nonce="8" * 32,
        application_id="9" * 32,
    )
    gateway = _operational_gateway(root, template, OperationalGatewayRunner())
    try:
        for confirmed, lease in (
            (first, second_lease),
            (second, first_lease),
        ):
            with pytest.raises(DeploymentError):
                gateway.begin_fail_closed(confirmed, lease, "preclaim")
    finally:
        first_lease.close()
        second_lease.close()


def test_loaded_config_drift_is_rejected_before_fail_closed_evidence(
    tmp_path: Path,
) -> None:
    root, template = _stage_operational_root(tmp_path)
    safe_runtime = SafeRuntimeFixture(tmp_path / "plans")
    confirmed, lease = _confirmed(
        safe_runtime,
        root=root,
        nonce="5" * 32,
        application_id="6" * 32,
    )
    runner = OperationalGatewayRunner()
    gateway = _operational_gateway(root, template, runner)
    try:
        preclaim = gateway.begin_fail_closed(confirmed, lease, "preclaim")
        loaded = _assert_gateway_runtime_modes(root)
        loaded.chmod(0o644)
        loaded.write_text(
            loaded.read_text(encoding="utf-8") + "# generation drift\n",
            encoding="utf-8",
        )
        loaded.chmod(0o444)

        with pytest.raises(DeploymentError):
            gateway.require_external_listener_absent(preclaim)
    finally:
        lease.close()


@pytest.mark.parametrize("failed_operation", ["validate", "reload"])
@pytest.mark.parametrize(
    "failure_kind",
    [
        pytest.param("nonzero", id="nonzero"),
        pytest.param("stdout", id="unexpected-stdout"),
        pytest.param("stderr", id="unexpected-stderr"),
        pytest.param("exception", id="runner-exception"),
    ],
)
def test_validation_or_reload_uncertainty_stops_nginx_and_reproves_absence(
    tmp_path: Path,
    failed_operation: str,
    failure_kind: str,
) -> None:
    root, template = _stage_operational_root(tmp_path)
    safe_runtime = SafeRuntimeFixture(tmp_path / "plans")
    confirmed, lease = _confirmed(
        safe_runtime,
        root=root,
        nonce="d" * 32,
        application_id="e" * 32,
    )
    runner = OperationalGatewayRunner(
        nginx_running=failed_operation == "reload",
        fail_operation=failed_operation,
        failure_kind=failure_kind,
    )
    gateway = _operational_gateway(root, template, runner)
    try:
        with pytest.raises(DeploymentError):
            gateway.begin_fail_closed(confirmed, lease, "preclaim")

        assert f"fail:{failed_operation}" in runner.events
        assert "stop" in runner.events
        assert "listener" in runner.events
        assert runner.events.index(f"fail:{failed_operation}") < runner.events.index(
            "stop"
        )
        assert runner.events.index("stop") < max(
            index
            for index, event in enumerate(runner.events)
            if event == "listener"
        )
        stop_calls = [
            call for call in runner.calls if call.safe_label == "cmms-gateway-stop"
        ]
        assert stop_calls
        assert tuple(stop_calls[-1].argv[-4:]) == (
            "--progress",
            "quiet",
            "stop",
            "nginx",
        )
        runtime = root / ".runtime/cmms-nginx"
        assert not any(
            path.name.startswith(f".{gateway_module._CONFIG_NAME}.candidate-")
            for path in runtime.iterdir()
        )
    finally:
        lease.close()


def test_nonzero_exact_gateway_listener_cannot_mint_absence_proof(
    tmp_path: Path,
) -> None:
    root, template = _stage_operational_root(tmp_path)
    safe_runtime = SafeRuntimeFixture(tmp_path / "plans")
    confirmed, lease = _confirmed(
        safe_runtime,
        root=root,
        nonce="7" * 32,
        application_id="8" * 32,
    )
    gateway = _operational_gateway(
        root,
        template,
        OperationalGatewayRunner(listener_count=1),
    )
    try:
        preclaim = gateway.begin_fail_closed(confirmed, lease, "preclaim")
        with pytest.raises(DeploymentError):
            gateway.require_external_listener_absent(preclaim)
    finally:
        lease.close()


@pytest.mark.parametrize(
    "runner_options",
    [
        pytest.param({"listener_count": 1}, id="precise-gateway-listener"),
        pytest.param(
            {
                "listener_stdout": (
                    "LISTEN 0 511 0.0.0.0:3000 0.0.0.0:*\n"
                )
            },
            id="ipv4-wildcard",
        ),
        pytest.param(
            {"listener_stdout": "LISTEN 0 511 [::]:3000 [::]:*\n"},
            id="ipv6-wildcard",
        ),
        pytest.param(
            {"listener_stdout": "not-an-ss-listener-row\n"},
            id="malformed",
        ),
        pytest.param({"listener_returncode": 1}, id="nonzero"),
        pytest.param(
            {"listener_stderr": "unexpected diagnostic\n"},
            id="stderr",
        ),
        pytest.param(
            {
                "listener_stdout": (
                    "LISTEN 0 511 172.17.0.1:3000 0.0.0.0:*\n"
                    "LISTEN 0 511 172.17.0.1:3000 0.0.0.0:*\n"
                )
            },
            id="ambiguous-duplicate",
        ),
    ],
)
def test_listener_inspection_uncertainty_never_mints_absence_evidence(
    tmp_path: Path,
    runner_options: dict[str, Any],
) -> None:
    root, template = _stage_operational_root(tmp_path)
    safe_runtime = SafeRuntimeFixture(tmp_path / "plans")
    confirmed, lease = _confirmed(
        safe_runtime,
        root=root,
        nonce="f" * 32,
        application_id="0" * 32,
    )
    runner = OperationalGatewayRunner(**runner_options)
    gateway = _operational_gateway(root, template, runner)
    try:
        preclaim = gateway.begin_fail_closed(confirmed, lease, "preclaim")
        with pytest.raises(DeploymentError):
            gateway.require_external_listener_absent(preclaim)

        assert runner.events.count("listener") >= 2
        assert "stop" in runner.events
    finally:
        lease.close()


def test_only_unrelated_exact_loopback_listeners_still_prove_external_absence(
    tmp_path: Path,
) -> None:
    root, template = _stage_operational_root(tmp_path)
    safe_runtime = SafeRuntimeFixture(tmp_path / "plans")
    confirmed, lease = _confirmed(
        safe_runtime,
        root=root,
        nonce="0" * 32,
        application_id="1" * 32,
    )
    runner = OperationalGatewayRunner(
        listener_stdout=(
            "LISTEN 0 511 127.0.0.1:3000 0.0.0.0:*\n"
            "LISTEN 0 511 [::1]:3000 [::]:*\n"
        )
    )
    gateway = _operational_gateway(root, template, runner)
    try:
        preclaim = gateway.begin_fail_closed(confirmed, lease, "preclaim")
        assert type(gateway.require_external_listener_absent(preclaim)) is FailClosedEvidence
        assert runner.events.count("listener") == 1
        assert "stop" not in runner.events
    finally:
        lease.close()


def test_loaded_config_replacement_during_listener_probe_cannot_mint_evidence(
    tmp_path: Path,
) -> None:
    root, template = _stage_operational_root(tmp_path)
    safe_runtime = SafeRuntimeFixture(tmp_path / "plans")
    confirmed, lease = _confirmed(
        safe_runtime,
        root=root,
        nonce="2" * 32,
        application_id="3" * 32,
    )
    runner = OperationalGatewayRunner()
    gateway = _operational_gateway(root, template, runner)
    try:
        preclaim = gateway.begin_fail_closed(confirmed, lease, "preclaim")
        loaded = _assert_gateway_runtime_modes(root)

        def replace_loaded_config() -> None:
            replacement = loaded.with_name(f".{loaded.name}.replacement")
            replacement.write_text("# replaced during listener probe\n", encoding="utf-8")
            replacement.chmod(0o444)
            os.replace(replacement, loaded)

        runner.listener_hook = replace_loaded_config
        with pytest.raises(DeploymentError):
            gateway.require_external_listener_absent(preclaim)

        assert "stop" in runner.events
    finally:
        lease.close()


def test_loaded_config_symlink_is_rejected_using_lstat_identity(
    tmp_path: Path,
) -> None:
    root, template = _stage_operational_root(tmp_path)
    safe_runtime = SafeRuntimeFixture(tmp_path / "plans")
    confirmed, lease = _confirmed(
        safe_runtime,
        root=root,
        nonce="4" * 32,
        application_id="5" * 32,
    )
    runner = OperationalGatewayRunner()
    gateway = _operational_gateway(root, template, runner)
    try:
        preclaim = gateway.begin_fail_closed(confirmed, lease, "preclaim")
        loaded = _assert_gateway_runtime_modes(root)
        target = loaded.with_name("attacker-controlled.conf")
        target.write_text(loaded.read_text(encoding="utf-8"), encoding="utf-8")
        target.chmod(0o444)
        loaded.unlink()
        loaded.symlink_to(target.name)
        assert stat.S_ISLNK(loaded.lstat().st_mode)

        with pytest.raises(DeploymentError):
            gateway.require_external_listener_absent(preclaim)
        assert "stop" in runner.events
    finally:
        lease.close()


def test_boolean_or_fabricated_preclaim_cannot_enter_proof_mint(
    tmp_path: Path,
) -> None:
    root, template = _stage_operational_root(tmp_path)
    gateway = _operational_gateway(root, template, OperationalGatewayRunner())

    for fabricated in (True, False, object(), "absent"):
        with pytest.raises(DeploymentError):
            gateway.require_external_listener_absent(fabricated)


def test_enable_dual_rejects_generation_gateway_and_sha_mismatch_before_publish(
    tmp_path: Path,
) -> None:
    root, template = _stage_operational_root(tmp_path)
    safe_runtime = SafeRuntimeFixture(tmp_path / "plans")
    runner = OperationalGatewayRunner()
    gateway, _confirmed_plan, lease, context = _claim_context(
        root=root,
        template=template,
        runner=runner,
        safe_runtime=safe_runtime,
        nonce="a" * 32,
        application_id="b" * 32,
    )
    generation = context.plan.snapshot.unit_generation
    actual_gateway = IPv4Address(runner.gateway_ipv4)
    alternate_root, alternate_template = _stage_operational_root(
        tmp_path / "alternate"
    )
    del alternate_root
    alternate_template.write_text(
        alternate_template.read_text(encoding="utf-8")
        + "# distinct-but-valid-candidate\n",
        encoding="utf-8",
    )
    wrong_sha = Gateway(
        template=alternate_template,
        runner=ScriptedRunner(),
    ).render(GatewayMode.DUAL, actual_gateway, generation)
    wrong_candidates = (
        gateway.render(GatewayMode.DUAL, actual_gateway, "c" * 64),
        gateway.render(GatewayMode.DUAL, IPv4Address("172.18.0.1"), generation),
        wrong_sha,
    )
    try:
        for candidate in wrong_candidates:
            runner.events.clear()
            with pytest.raises(DeploymentError):
                gateway.enable_dual(context, candidate)
            assert "validate" not in runner.events
            assert "reload" not in runner.events
    finally:
        lease.close()


def test_enable_dual_requires_the_exact_planned_action(tmp_path: Path) -> None:
    root, template = _stage_operational_root(tmp_path)
    safe_runtime = SafeRuntimeFixture(tmp_path / "plans")
    confirmed, lease = _confirmed(
        safe_runtime,
        root=root,
        nonce="c" * 32,
        application_id="d" * 32,
        operation=Operation.STOP,
    )
    runner = OperationalGatewayRunner()
    gateway = _operational_gateway(root, template, runner)
    try:
        preclaim = gateway.begin_fail_closed(confirmed, lease, "preclaim")
        fail_closed = gateway.require_external_listener_absent(preclaim)
        context = claim_plan_application(
            confirmed,
            safe_runtime.plans_dir,
            lease,
            fail_closed,
        )
        expected = gateway.render(
            GatewayMode.DUAL,
            IPv4Address(runner.gateway_ipv4),
            context.plan.snapshot.unit_generation,
        )
        runner.events.clear()

        with pytest.raises(DeploymentError):
            gateway.enable_dual(context, expected)
        assert runner.events == []
    finally:
        lease.close()


def test_enable_dual_rechecks_that_claim_and_lease_are_live(tmp_path: Path) -> None:
    root, template = _stage_operational_root(tmp_path)
    safe_runtime = SafeRuntimeFixture(tmp_path / "plans")
    runner = OperationalGatewayRunner()
    gateway, _confirmed_plan, lease, context = _claim_context(
        root=root,
        template=template,
        runner=runner,
        safe_runtime=safe_runtime,
        nonce="e" * 32,
        application_id="f" * 32,
    )
    expected = gateway.render(
        GatewayMode.DUAL,
        IPv4Address(runner.gateway_ipv4),
        context.plan.snapshot.unit_generation,
    )
    lease.close()
    runner.events.clear()

    with pytest.raises(DeploymentError):
        gateway.enable_dual(context, expected)
    assert runner.events == []


def test_enable_dual_validates_publishes_reloads_reopens_then_inspects_listener(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, template = _stage_operational_root(tmp_path)
    safe_runtime = SafeRuntimeFixture(tmp_path / "plans")
    runner = OperationalGatewayRunner()
    gateway, confirmed, lease, context = _claim_context(
        root=root,
        template=template,
        runner=runner,
        safe_runtime=safe_runtime,
        nonce="1" * 32,
        application_id="a" * 32,
    )
    expected = gateway.render(
        GatewayMode.DUAL,
        IPv4Address(runner.gateway_ipv4),
        context.plan.snapshot.unit_generation,
    )
    original_publish = gateway_module._publish_rendered
    original_reopen = gateway_module._reopen_rendered

    def observed_publish(*args: Any, **kwargs: Any) -> Any:
        result = original_publish(*args, **kwargs)
        runner.events.append("publish")
        return result

    def observed_reopen(*args: Any, **kwargs: Any) -> Any:
        result = original_reopen(*args, **kwargs)
        runner.events.append("reopen")
        return result

    monkeypatch.setattr(gateway_module, "_publish_rendered", observed_publish)
    monkeypatch.setattr(gateway_module, "_reopen_rendered", observed_reopen)
    runner.nginx_running = True
    runner.listener_count = 1
    runner.events.clear()
    try:
        result = gateway.enable_dual(context, expected)

        ordered = ("validate", "publish", "reload", "reopen", "listener")
        positions = [runner.events.index(event) for event in ordered]
        assert positions == sorted(positions)
        reload_calls = [
            call for call in runner.calls if call.safe_label == "cmms-gateway-reload"
        ]
        assert len(reload_calls) == 1
        assert tuple(reload_calls[0].argv[-6:]) == (
            "exec",
            "--no-TTY",
            "nginx",
            "/bin/kill",
            "-HUP",
            "1",
        )
        assert type(result) is not GatewayEvidence
        assert not hasattr(result, "purpose")
        assert result.rendered == expected
        assert result.rendered.unit_generation == context.plan.snapshot.unit_generation
        assert result.rendered.sha256 == expected.sha256
        assert result.rendered.gateway_ip == IPv4Address(runner.gateway_ipv4)
        with pytest.raises(DeploymentError):
            gateway.require_external_listener_absent(result)
        with pytest.raises(DeploymentError):
            claim_plan_application(
                confirmed,
                safe_runtime.plans_dir,
                lease,
                result,
            )
    finally:
        lease.close()


def test_unrelated_exact_listener_cannot_impersonate_reloaded_nginx(
    tmp_path: Path,
) -> None:
    root, template = _stage_operational_root(tmp_path)
    safe_runtime = SafeRuntimeFixture(tmp_path / "plans")
    runner = OperationalGatewayRunner()
    gateway, _confirmed_plan, lease, context = _claim_context(
        root=root,
        template=template,
        runner=runner,
        safe_runtime=safe_runtime,
        nonce="2" * 32,
        application_id="a" * 32,
    )
    expected = gateway.render(
        GatewayMode.DUAL,
        IPv4Address(runner.gateway_ipv4),
        context.plan.snapshot.unit_generation,
    )
    runner.nginx_running = True
    runner.listener_count = 1
    runner.probe_owner = False
    runner.events.clear()
    try:
        with pytest.raises(DeploymentError):
            gateway.enable_dual(context, expected)
        assert "probe-loopback" in runner.events
        assert "stop" in runner.events
        assert runner.listener_count == 0
    finally:
        lease.close()


def test_bridge_drift_during_dual_proof_stops_nginx(
    tmp_path: Path,
) -> None:
    root, template = _stage_operational_root(tmp_path)
    safe_runtime = SafeRuntimeFixture(tmp_path / "plans")
    runner = OperationalGatewayRunner(
        gateway_ipv4_sequence=(
            "172.17.0.1",
            "172.17.0.1",
            "172.17.0.2",
        )
    )
    gateway, _confirmed_plan, lease, context = _claim_context(
        root=root,
        template=template,
        runner=runner,
        safe_runtime=safe_runtime,
        nonce="3" * 32,
        application_id="b" * 32,
    )
    expected = gateway.render(
        GatewayMode.DUAL,
        IPv4Address("172.17.0.1"),
        context.plan.snapshot.unit_generation,
    )
    runner.nginx_running = True
    runner.listener_count = 1
    runner.events.clear()
    try:
        with pytest.raises(DeploymentError):
            gateway.enable_dual(context, expected)
        assert runner.events.count("bridge-network") >= 2
        assert "stop" in runner.events
        assert runner.listener_count == 0
    finally:
        lease.close()


@pytest.mark.parametrize(
    "listener_state",
    [
        pytest.param({"listener_count": 0}, id="missing"),
        pytest.param({"listener_count": 2}, id="duplicate"),
        pytest.param(
            {"listener_stdout": "LISTEN 0 511 0.0.0.0:3000 0.0.0.0:*\n"},
            id="wildcard",
        ),
        pytest.param({"listener_returncode": 1}, id="nonzero"),
        pytest.param(
            {"listener_stderr": "unexpected diagnostic\n"},
            id="stderr",
        ),
    ],
)
def test_enable_dual_requires_one_exact_verified_gateway_listener(
    tmp_path: Path,
    listener_state: dict[str, Any],
) -> None:
    root, template = _stage_operational_root(tmp_path)
    safe_runtime = SafeRuntimeFixture(tmp_path / "plans")
    runner = OperationalGatewayRunner()
    gateway, _confirmed_plan, lease, context = _claim_context(
        root=root,
        template=template,
        runner=runner,
        safe_runtime=safe_runtime,
        nonce="3" * 32,
        application_id="c" * 32,
    )
    expected = gateway.render(
        GatewayMode.DUAL,
        IPv4Address(runner.gateway_ipv4),
        context.plan.snapshot.unit_generation,
    )
    for name, value in listener_state.items():
        setattr(runner, name, value)
    runner.events.clear()
    try:
        with pytest.raises(DeploymentError):
            gateway.enable_dual(context, expected)
        assert "stop" in runner.events
    finally:
        lease.close()


def test_enable_dual_result_cannot_impersonate_fail_closed_authority(
    tmp_path: Path,
) -> None:
    root, template = _stage_operational_root(tmp_path)
    safe_runtime = SafeRuntimeFixture(tmp_path / "plans")
    runner = OperationalGatewayRunner()
    gateway, confirmed, lease, context = _claim_context(
        root=root,
        template=template,
        runner=runner,
        safe_runtime=safe_runtime,
        nonce="2" * 32,
        application_id="b" * 32,
    )
    expected = gateway.render(
        GatewayMode.DUAL,
        IPv4Address(runner.gateway_ipv4),
        context.plan.snapshot.unit_generation,
    )
    runner.listener_count = 1
    try:
        result = gateway.enable_dual(context, expected)

        assert type(result) is not GatewayEvidence
        assert not hasattr(result, "purpose")
        assert result.rendered == expected
        for operation in (copy.copy, copy.deepcopy, pickle.dumps):
            with pytest.raises((DeploymentError, TypeError)):
                operation(result)
        with pytest.raises(DeploymentError):
            gateway.require_external_listener_absent(result)
        with pytest.raises(DeploymentError):
            claim_plan_application(
                confirmed,
                safe_runtime.plans_dir,
                lease,
                result,
            )
    finally:
        lease.close()


def test_claimed_compensation_is_not_claim_capable(
    tmp_path: Path,
) -> None:
    root, template = _stage_operational_root(tmp_path)
    safe_runtime = SafeRuntimeFixture(tmp_path / "plans")
    confirmed, lease = _confirmed(
        safe_runtime,
        root=root,
        nonce="9" * 32,
        application_id="a" * 32,
    )
    gateway = _operational_gateway(root, template, OperationalGatewayRunner())
    try:
        preclaim = gateway.begin_fail_closed(confirmed, lease, "preclaim")
        evidence = gateway.require_external_listener_absent(preclaim)
        context = claim_plan_application(
            confirmed,
            safe_runtime.plans_dir,
            lease,
            evidence,
        )
        compensation = gateway.fail_closed_claimed(context, "apply-failed")

        assert type(compensation) is GatewayEvidence
        assert compensation.purpose is GatewayEvidencePurpose.CLAIMED_COMPENSATION
        with pytest.raises(DeploymentError):
            gateway.require_external_listener_absent(compensation)
        with pytest.raises(DeploymentError):
            claim_plan_application(
                confirmed,
                safe_runtime.plans_dir,
                lease,
                compensation,
            )
    finally:
        lease.close()


def test_emergency_fail_closed_has_monotonic_service_surface(tmp_path: Path) -> None:
    root, template = _stage_operational_root(tmp_path)
    runner = OperationalGatewayRunner()
    gateway = _operational_gateway(root, template, runner)

    evidence = gateway.emergency_fail_closed("api-ended")

    assert type(evidence) is GatewayEvidence
    assert evidence.purpose is GatewayEvidencePurpose.EMERGENCY
    with pytest.raises(DeploymentError):
        gateway.require_external_listener_absent(evidence)
    commands = [tuple(call.argv) for call in runner.calls]
    assert commands
    flattened = tuple(token for argv in commands for token in argv)
    assert "pull" not in flattened
    assert "up" not in flattened
    assert "start" not in flattened
    assert "postgres" not in flattened
    assert "minio" not in flattened


def test_preclaim_bridge_uncertainty_stops_and_probes_without_known_address(
    tmp_path: Path,
) -> None:
    root, template = _stage_operational_root(tmp_path)
    safe_runtime = SafeRuntimeFixture(tmp_path / "plans")
    confirmed, lease = _confirmed(
        safe_runtime,
        root=root,
        nonce="4" * 32,
        application_id="5" * 32,
    )
    runner = OperationalGatewayRunner(fail_operation="bridge")
    gateway = _operational_gateway(root, template, runner)
    try:
        with pytest.raises(DeploymentError):
            gateway.begin_fail_closed(confirmed, lease, "bridge-uncertain")
        assert "stop" in runner.events
        assert "listener" in runner.events
        assert runner.events.index("stop") < runner.events.index("listener")
    finally:
        lease.close()


def test_claimed_render_uncertainty_stops_and_reproves_absence(
    tmp_path: Path,
) -> None:
    root, template = _stage_operational_root(tmp_path)
    safe_runtime = SafeRuntimeFixture(tmp_path / "plans")
    runner = OperationalGatewayRunner()
    gateway, _confirmed_plan, lease, context = _claim_context(
        root=root,
        template=template,
        runner=runner,
        safe_runtime=safe_runtime,
        nonce="6" * 32,
        application_id="7" * 32,
    )
    template.unlink()
    runner.events.clear()
    try:
        with pytest.raises(DeploymentError):
            gateway.fail_closed_claimed(context, "render-uncertain")
        assert "stop" in runner.events
        assert "listener" in runner.events
        assert runner.events.index("stop") < runner.events.index("listener")
    finally:
        lease.close()


def test_emergency_invalid_runtime_env_uses_secret_free_recovery_stop(
    tmp_path: Path,
) -> None:
    root, template = _stage_operational_root(tmp_path)
    runner = OperationalGatewayRunner(nginx_running=True, listener_count=1)
    gateway = _operational_gateway(root, template, runner)
    (root / ".runtime/cmms-development.env").unlink()

    with pytest.raises(DeploymentError):
        gateway.emergency_fail_closed("runtime-env-uncertain")

    assert "stop" in runner.events
    assert "listener" in runner.events
    assert runner.events.index("stop") < runner.events.index("listener")
    stop = next(
        call
        for call in runner.calls
        if call.safe_label == "cmms-gateway-recovery-stop"
    )
    assert tuple(stop.argv[-6:]) == (
        "stop",
        "--signal",
        "SIGTERM",
        "--timeout",
        "10",
        RECOVERY_CONTAINER_ID,
    )


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_recovery_stop_does_not_depend_on_mutable_compose_yaml(
    tmp_path: Path,
    damage: str,
) -> None:
    root, template = _stage_operational_root(tmp_path)
    compose_path = root / "deploy/compose/cmms-development.yml"
    if damage == "missing":
        compose_path.unlink()
    else:
        compose_path.write_text(
            compose_path.read_text(encoding="utf-8") + "# drift\n",
            encoding="utf-8",
        )
    runner = OperationalGatewayRunner(nginx_running=True, listener_count=1)
    gateway = _operational_gateway(root, template, runner)

    with pytest.raises(DeploymentError):
        gateway.emergency_fail_closed("compose-uncertain")

    assert "recovery-list" in runner.events
    assert "recovery-inspect" in runner.events
    assert "stop" in runner.events
    assert "listener" in runner.events
    assert runner.listener_count == 0


def test_busy_gateway_lock_makes_emergency_stop_without_waiting(
    tmp_path: Path,
) -> None:
    root, template = _stage_operational_root(tmp_path)
    runner = OperationalGatewayRunner(nginx_running=True, listener_count=1)
    gateway = _operational_gateway(root, template, runner)

    with gateway_module._gateway_operation_lock(root):
        with pytest.raises(DeploymentError):
            gateway.emergency_fail_closed("concurrent-emergency")

    assert "stop" in runner.events
    assert "listener" in runner.events
    assert runner.listener_count == 0


def test_listener_probe_uses_the_verified_host_binary(tmp_path: Path) -> None:
    root, template = _stage_operational_root(tmp_path)
    safe_runtime = SafeRuntimeFixture(tmp_path / "plans")
    confirmed, lease = _confirmed(
        safe_runtime,
        root=root,
        nonce="8" * 32,
        application_id="9" * 32,
    )
    runner = OperationalGatewayRunner()
    gateway = _operational_gateway(root, template, runner)
    try:
        preclaim = gateway.begin_fail_closed(confirmed, lease, "probe-binary")
        gateway.require_external_listener_absent(preclaim)
    finally:
        lease.close()
    listener = next(
        call for call in runner.calls if call.safe_label == "cmms-gateway-listener-inspect"
    )
    assert tuple(listener.argv) == (
        "/usr/bin/ss",
        "-H",
        "-ltn",
        "sport",
        "=",
        ":3000",
    )


def test_gateway_evidence_purpose_is_closed_to_three_fail_closed_paths() -> None:
    assert set(GatewayEvidencePurpose) == {
        GatewayEvidencePurpose.PRECLAIM,
        GatewayEvidencePurpose.CLAIMED_COMPENSATION,
        GatewayEvidencePurpose.EMERGENCY,
    }
