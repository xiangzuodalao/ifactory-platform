"""Pure-Mock safety contracts for confirmed ThingsBoard shadow telemetry seeding."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid5

import pytest

from e2e.support.pilot_api import PilotApiError, PilotEnvironment, PilotApi
from e2e.support import shadow_seed
from e2e.support.shadow_seed import ShadowSeedError, apply, build_plan


TENANT = "00000000-0000-4000-8000-000000000001"
NAMESPACE = UUID("00000000-0000-4000-8000-000000000099")
MAX_PLAN_BYTES = 512 * 1024
MAX_PROVIDER_BYTES = 256 * 1024


def _environment() -> PilotEnvironment:
    return PilotEnvironment(
        tb_base_url="http://127.0.0.1:8080",
        cmms_base_url="http://127.0.0.1:3000",
        tb_tenant_id=TENANT,
        cmms_company_id=42,
        tb_bearer="tb-token",
        cmms_api_key="cmms-key",
    )


def _write_runtime_env(path: Path) -> None:
    path.write_text(
        "\n".join(
            (
                "INTEGRATION_POSTGRES_DB=ifactory_integration",
                "INTEGRATION_POSTGRES_USER=ifactory_integration",
                "INTEGRATION_POSTGRES_PASSWORD=offline-test-password",
                "PLATFORM_INTEGRATION_TENANT_ALIAS=ifactory-pilot",
                f"PLATFORM_INTEGRATION_TENANT_ID={TENANT}",
                "PLATFORM_INTEGRATION_ISOLATED_PILOT_MODE=1",
                "PLATFORM_INTEGRATION_PDM_BASE_URL=http://pdm:10021",
                "PLATFORM_INTEGRATION_TB_BASE_URL=http://host.docker.internal:8080",
                "PLATFORM_INTEGRATION_CMMS_BASE_URL=http://host.docker.internal:3000",
                "PLATFORM_INTEGRATION_PDM_CREDENTIAL_REF=PILOT_PDM_CREDENTIAL",
                "PLATFORM_INTEGRATION_TB_CREDENTIAL_REF=PILOT_TB_CREDENTIAL",
                "PLATFORM_INTEGRATION_CMMS_CREDENTIAL_REF=PILOT_CMMS_CREDENTIAL",
                "PLATFORM_INTEGRATION_CMMS_WEBHOOK_SECRET_REF=PILOT_CMMS_WEBHOOK_SECRET_FILE",
                f"PLATFORM_INTEGRATION_TB_TENANT_ID={TENANT}",
                "PLATFORM_INTEGRATION_CMMS_COMPANY_ID=42",
                'PILOT_PDM_CREDENTIAL={"kind":"opaque_bearer","value":"pdm-token"}',
                'PILOT_TB_CREDENTIAL={"kind":"thingsboard_bearer","value":"tb-token"}',
                'PILOT_CMMS_CREDENTIAL={"kind":"cmms_api_key","value":"cmms-key"}',
                "VALEO_PDM_PREDICTION_V2_BEARER_TOKEN=pdm-token",
                f"VALEO_PDM_ALLOWED_TENANT_IDS={TENANT}",
                "",
            )
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _seal_plan(payload: dict[str, object]) -> bytes:
    unsigned = {key: value for key, value in payload.items() if key != "plan_sha256"}
    payload["plan_sha256"] = hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        + b"\n"
    )


class _RecordingHttpServer(ThreadingHTTPServer):
    redirect_to: str | None
    payload: bytes
    requests: list[tuple[str, str | None]]


class _RecordingHttpHandler(BaseHTTPRequestHandler):
    server: _RecordingHttpServer

    def _respond(self) -> None:
        self.server.requests.append((self.command, self.headers.get("X-Authorization")))
        if self.server.redirect_to is not None:
            self.send_response(302)
            self.send_header("Location", self.server.redirect_to)
            self.end_headers()
            return
        payload = self.server.payload
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _respond
    do_POST = _respond

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _Server:
    def __init__(
        self,
        *,
        redirect_to: str | None = None,
        payload: bytes = b'{"ok":true}',
    ) -> None:
        self.httpd = _RecordingHttpServer(
            ("127.0.0.1", 0),
            _RecordingHttpHandler,
        )
        self.httpd.redirect_to = redirect_to
        self.httpd.payload = payload
        self.httpd.requests = []
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address
        return f"http://{host}:{port}"

    def __enter__(self) -> _Server:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)


class FakeTransport:
    def __init__(self) -> None:
        types = (
            "CNC",
            "INJECTION_MOLDING",
            "ASSEMBLY_ROBOT",
            "TIGHTENING",
            "AIR_COMPRESSOR",
            "EOL_TESTER",
        )
        counts = (2, 2, 2, 2, 1, 1)
        self.devices: list[dict[str, object]] = []
        for line in ("LINE-A", "LINE-B"):
            for device_type, count in zip(types, counts, strict=True):
                for index in range(1, count + 1):
                    name = f"{line}-{device_type}-{index:02d}"
                    self.devices.append(
                        {
                            "id": {"id": str(uuid5(NAMESPACE, name))},
                            "name": name,
                            "type": device_type,
                        }
                    )
        self.calls: list[tuple[str, str, object | None]] = []
        self.window: dict[str, list[dict[str, object]]] = {}
        self.drift = False
        self.response_loss = False

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        body: object | None = None,
    ) -> tuple[int, object]:
        self.calls.append((method, path, body))
        if path == "/api/auth/user":
            return 200, {"tenantId": {"id": TENANT}}
        if path == "/api/tenant/devices?pageSize=100&page=0":
            return 200, {"data": self.devices, "hasNext": False, "totalElements": 20}
        if "/values/attributes/SERVER_SCOPE?keys=equipment_id,cmms_asset_id" in path:
            device_id = path.split("/")[5]
            return 200, [
                {
                    "key": "equipment_id",
                    "value": str(uuid5(NAMESPACE, f"equipment:{device_id}")),
                },
                {
                    "key": "cmms_asset_id",
                    "value": int(UUID(device_id)) % 9_000_000_000 + 1,
                },
            ]
        if "/values/timeseries?" in path:
            device_id = path.split("/")[5]
            key = next(
                (
                    key
                    for key in (
                        "vibration_rms",
                        "injection_pressure",
                        "position_deviation",
                        "torque",
                        "discharge_pressure",
                        "pass_rate",
                    )
                    if f"keys={key}" in path
                ),
                "vibration_rms",
            )
            if self.drift:
                return 200, {key: [{"ts": 1785283740000, "value": "4.00"}]}
            return 200, {key: self.window.get(device_id, [])}
        if "/timeseries/ANY" in path:
            device_id = path.split("/")[5]
            assert isinstance(body, list)
            self.window[device_id] = [
                {"ts": row["ts"], "value": str(next(iter(row["values"].values())))}
                for row in body
            ]
            if self.response_loss:
                raise TimeoutError("response lost")
            return 204, {}
        if "/api/v2/alarm/DEVICE/" in path:
            return 200, {"totalElements": 0}
        if path == "/api/work-orders/search":
            return 200, {"totalElements": 0}
        raise AssertionError(f"unexpected route {method} {path}")


def test_plan_is_get_only_and_freezes_all_1320_telemetry_tuples(tmp_path: Path) -> None:
    transport = FakeTransport()
    output = tmp_path / "plan.json"
    plan = build_plan(
        _environment(),
        transport.request,
        actor="operator-a",
        output=output,
        now=datetime(2026, 7, 29, tzinfo=UTC),
    )
    assert {method for method, _, _ in transport.calls} == {"GET"}
    assert len(plan["targets"]) == 20
    assert sum(len(target["points"]) for target in plan["targets"]) == 1320
    assert all(len(target["body_sha256"]) == 64 for target in plan["targets"])
    assert plan["targets"][0]["points"][0]["ts"] == 1785283740000
    assert plan["targets"][0]["points"][-1]["ts"] == 1785287640000
    assert output.stat().st_mode & 0o777 == 0o600
    assert "tb-token" not in output.read_text(encoding="utf-8")


@pytest.mark.parametrize("actor", ("operator\x7f", "operator\x80"))
def test_plan_rejects_non_printable_actor_before_any_read(
    tmp_path: Path, actor: str
) -> None:
    """DEL and C1 controls must never enter an auditable plan identity."""
    transport = FakeTransport()

    with pytest.raises(ShadowSeedError, match="SHADOW_SEED_PLAN_INVALID"):
        build_plan(
            _environment(),
            transport.request,
            actor=actor,
            output=tmp_path / "plan.json",
            now=datetime(2026, 7, 29, tzinfo=UTC),
        )

    assert transport.calls == []


def test_apply_rechecks_drift_before_any_post(tmp_path: Path) -> None:
    transport = FakeTransport()
    plan_path = tmp_path / "plan.json"
    plan = build_plan(
        _environment(),
        transport.request,
        actor="operator-a",
        output=plan_path,
        now=datetime(2026, 7, 29, tzinfo=UTC),
    )
    transport.drift = True
    with pytest.raises(ShadowSeedError, match="WINDOW_DRIFT"):
        apply(
            _environment(),
            transport.request,
            plan=plan_path,
            plan_hash=plan["plan_sha256"],
            confirmed_hash=plan["plan_sha256"],
            actor="operator-a",
            receipt=tmp_path / "receipt.json",
            now=datetime(2026, 7, 29, 0, 1, tzinfo=UTC),
        )
    assert not any(
        method == "POST" and "/timeseries/ANY" in path
        for method, path, _ in transport.calls
    )


def test_apply_rejects_non_printable_actor_before_any_read(tmp_path: Path) -> None:
    """A forged plan must not bypass the same actor boundary enforced at planning."""
    transport = FakeTransport()
    plan_path = tmp_path / "plan.json"
    build_plan(
        _environment(),
        transport.request,
        actor="operator-a",
        output=plan_path,
        now=datetime(2026, 7, 29, tzinfo=UTC),
    )
    saved = json.loads(plan_path.read_text(encoding="utf-8"))
    saved["actor"] = "operator\x7f"
    saved["plan_sha256"] = shadow_seed._hash(
        {key: value for key, value in saved.items() if key != "plan_sha256"}
    )
    plan_path.write_bytes(shadow_seed._canonical(saved) + b"\n")
    transport.calls.clear()

    with pytest.raises(ShadowSeedError, match="SHADOW_SEED_CONFIRMATION_INVALID"):
        apply(
            _environment(),
            transport.request,
            plan=plan_path,
            plan_hash=saved["plan_sha256"],
            confirmed_hash=saved["plan_sha256"],
            actor="operator\x7f",
            receipt=tmp_path / "receipt.json",
            now=datetime(2026, 7, 29, 0, 1, tzinfo=UTC),
        )

    assert transport.calls == []


def test_apply_recovers_response_loss_by_exact_readback_and_seals_receipt(
    tmp_path: Path,
) -> None:
    transport = FakeTransport()
    plan_path = tmp_path / "plan.json"
    receipt = tmp_path / "receipt.json"
    plan = build_plan(
        _environment(),
        transport.request,
        actor="operator-a",
        output=plan_path,
        now=datetime(2026, 7, 29, tzinfo=UTC),
    )
    transport.response_loss = True
    result = apply(
        _environment(),
        transport.request,
        plan=plan_path,
        plan_hash=plan["plan_sha256"],
        confirmed_hash=plan["plan_sha256"],
        actor="operator-a",
        receipt=receipt,
        now=datetime(2026, 7, 29, 0, 1, tzinfo=UTC),
    )
    assert result["target_count"] == 20
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert sum(method == "POST" for method, _, _ in transport.calls) == 20
    with pytest.raises(ShadowSeedError, match="RECEIPT_ALREADY_COMPLETED"):
        apply(
            _environment(),
            transport.request,
            plan=plan_path,
            plan_hash=plan["plan_sha256"],
            confirmed_hash=plan["plan_sha256"],
            actor="operator-a",
            receipt=receipt,
            now=datetime(2026, 7, 29, 0, 2, tzinfo=UTC),
        )


def test_apply_requires_exact_hash_actor_and_unexpired_plan(tmp_path: Path) -> None:
    transport = FakeTransport()
    plan_path = tmp_path / "plan.json"
    plan = build_plan(
        _environment(),
        transport.request,
        actor="operator-a",
        output=plan_path,
        now=datetime(2026, 7, 29, tzinfo=UTC),
    )
    for hash_value, confirmed, actor, now in (
        ("A" * 64, "A" * 64, "operator-a", datetime(2026, 7, 29, tzinfo=UTC)),
        (
            plan["plan_sha256"],
            plan["plan_sha256"],
            "operator-b",
            datetime(2026, 7, 29, tzinfo=UTC),
        ),
        (
            plan["plan_sha256"],
            plan["plan_sha256"],
            "operator-a",
            datetime(2026, 7, 29, 0, 31, tzinfo=UTC),
        ),
    ):
        with pytest.raises(ShadowSeedError):
            apply(
                _environment(),
                transport.request,
                plan=plan_path,
                plan_hash=hash_value,
                confirmed_hash=confirmed,
                actor=actor,
                receipt=tmp_path / f"{actor}-{now.minute}.json",
                now=now,
            )


def test_plan_loader_reads_one_nofollow_nonblocking_regular_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reopening a checked pathname would let an attacker replace the confirmed plan."""
    transport = FakeTransport()
    plan_path = tmp_path / "plan.json"
    build_plan(
        _environment(),
        transport.request,
        actor="operator-a",
        output=plan_path,
        now=datetime(2026, 7, 29, tzinfo=UTC),
    )
    outside = tmp_path / "outside.json"
    outside_payload = json.loads(plan_path.read_text(encoding="utf-8"))
    outside_payload["actor"] = "operator-b"
    outside.write_bytes(_seal_plan(outside_payload))
    outside.chmod(0o600)
    real_open = os.open
    observed_flags: list[int] = []
    swapped = False

    def open_then_swap(path: os.PathLike[str] | str, flags: int, *args, **kwargs):
        nonlocal swapped
        descriptor = real_open(path, flags, *args, **kwargs)
        if Path(path) == plan_path and not swapped:
            observed_flags.append(flags)
            plan_path.unlink()
            plan_path.symlink_to(outside)
            swapped = True
        return descriptor

    monkeypatch.setattr(shadow_seed.os, "open", open_then_swap)

    with pytest.raises(ShadowSeedError, match="SHADOW_SEED_PLAN_MODE_INVALID"):
        shadow_seed._load_plan(plan_path)

    assert swapped
    assert observed_flags[0] & os.O_NOFOLLOW
    assert observed_flags[0] & os.O_NONBLOCK
    assert plan_path.is_symlink()


def test_plan_loader_rejects_an_inode_changed_during_its_bounded_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stable pathname and valid bytes are insufficient if the opened inode changed."""
    transport = FakeTransport()
    plan_path = tmp_path / "plan.json"
    build_plan(
        _environment(),
        transport.request,
        actor="operator-a",
        output=plan_path,
        now=datetime(2026, 7, 29, tzinfo=UTC),
    )
    real_read = os.read
    changed = False

    def read_then_touch(descriptor: int, size: int) -> bytes:
        nonlocal changed
        chunk = real_read(descriptor, size)
        if not chunk and not changed:
            metadata = plan_path.stat()
            os.utime(
                plan_path,
                ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000),
            )
            changed = True
        return chunk

    monkeypatch.setattr(shadow_seed.os, "read", read_then_touch)

    with pytest.raises(ShadowSeedError, match="SHADOW_SEED_PLAN_CHANGED"):
        shadow_seed._load_plan(plan_path)

    assert changed


def test_plan_loader_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    """Opening a plan FIFO without O_NONBLOCK would hang the confirmation command."""
    fifo = tmp_path / "plan.json"
    os.mkfifo(fifo, 0o600)
    command = (
        "from pathlib import Path\n"
        "from e2e.support.shadow_seed import ShadowSeedError, _load_plan\n"
        "try:\n"
        "    _load_plan(Path(__import__('sys').argv[1]))\n"
        "except ShadowSeedError as error:\n"
        "    print(error.code)\n"
        "else:\n"
        "    raise SystemExit(3)\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", command, str(fifo)],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "SHADOW_SEED_PLAN_MODE_INVALID"


@pytest.mark.parametrize("constant", ("NaN", "Infinity", "-Infinity"))
def test_plan_loader_rejects_non_finite_json_with_a_stable_error(
    tmp_path: Path, constant: str
) -> None:
    """Python's permissive constants must not escape as an uncaught hashing error."""
    path = tmp_path / "plan.json"
    path.write_text(f'{{"schema_version":{constant}}}\n', encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(ShadowSeedError) as caught:
        shadow_seed._load_plan(path)

    assert caught.value.code == "SHADOW_SEED_PLAN_INVALID"
    assert constant not in str(caught.value)


def test_plan_loader_rejects_duplicate_keys_that_hide_a_secret(
    tmp_path: Path,
) -> None:
    """Last-wins parsing must not hide raw evidence outside the confirmed hash."""
    transport = FakeTransport()
    path = tmp_path / "plan.json"
    build_plan(
        _environment(),
        transport.request,
        actor="operator-a",
        output=path,
        now=datetime(2026, 7, 29, tzinfo=UTC),
    )
    raw = path.read_bytes()
    hidden = b"Bearer duplicate-key-sentinel"
    path.write_bytes(b'{"actor":"' + hidden + b'",' + raw[1:])

    with pytest.raises(ShadowSeedError) as caught:
        shadow_seed._load_plan(path)

    assert caught.value.code == "SHADOW_SEED_PLAN_INVALID"
    assert hidden.decode() not in str(caught.value)


def test_plan_loader_redacts_out_of_range_timestamps_as_a_stable_error(
    tmp_path: Path,
) -> None:
    """Datetime overflow from a canonical forged plan must not escape the CLI gate."""
    transport = FakeTransport()
    path = tmp_path / "plan.json"
    build_plan(
        _environment(),
        transport.request,
        actor="operator-a",
        output=path,
        now=datetime(2026, 7, 29, tzinfo=UTC),
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["created_at"] = "9999-12-31T23:59:59+00:00"
    payload["expires_at"] = "9999-12-31T23:59:59+00:00"
    path.write_bytes(_seal_plan(payload))

    with pytest.raises(ShadowSeedError) as caught:
        shadow_seed._load_plan(path)

    assert caught.value.code == "SHADOW_SEED_PLAN_INVALID"


@pytest.mark.parametrize("encoding", ("pretty", "missing-newline", "extra-newline"))
def test_plan_loader_requires_exact_canonical_bytes_and_one_newline(
    tmp_path: Path, encoding: str
) -> None:
    """A hash over parsed values must not admit alternate raw plan evidence."""
    transport = FakeTransport()
    path = tmp_path / "plan.json"
    build_plan(
        _environment(),
        transport.request,
        actor="operator-a",
        output=path,
        now=datetime(2026, 7, 29, tzinfo=UTC),
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if encoding == "pretty":
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode() + b"\n"
    elif encoding == "missing-newline":
        raw = path.read_bytes()[:-1]
    else:
        raw = path.read_bytes() + b"\n"
    path.write_bytes(raw)

    with pytest.raises(ShadowSeedError, match="SHADOW_SEED_PLAN_INVALID"):
        shadow_seed._load_plan(path)


def test_plan_loader_rejects_oversized_input_before_json_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing the byte bound would let an untrusted plan consume arbitrary memory."""
    path = tmp_path / "plan.json"
    path.write_bytes(b" " * (MAX_PLAN_BYTES + 1))
    path.chmod(0o600)

    def parsing_is_too_late(*_args, **_kwargs):
        raise AssertionError("oversized input reached JSON parsing")

    monkeypatch.setattr(shadow_seed.json, "loads", parsing_is_too_late)

    with pytest.raises(ShadowSeedError, match="SHADOW_SEED_PLAN_INVALID"):
        shadow_seed._load_plan(path)


@pytest.mark.parametrize("missing", ("expires_at", "targets"))
def test_apply_rejects_incomplete_canonical_plan_before_provider_reads(
    tmp_path: Path, missing: str
) -> None:
    """A schema-valid hash must not turn a missing plan field into a traceback."""
    transport = FakeTransport()
    path = tmp_path / "plan.json"
    plan = build_plan(
        _environment(),
        transport.request,
        actor="operator-a",
        output=path,
        now=datetime(2026, 7, 29, tzinfo=UTC),
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload[missing]
    raw = _seal_plan(payload)
    path.write_bytes(raw)
    transport.calls.clear()

    with pytest.raises(ShadowSeedError) as caught:
        apply(
            _environment(),
            transport.request,
            plan=path,
            plan_hash=payload["plan_sha256"],
            confirmed_hash=payload["plan_sha256"],
            actor="operator-a",
            receipt=tmp_path / "receipt.json",
            now=datetime(2026, 7, 29, 0, 1, tzinfo=UTC),
        )

    assert caught.value.code == "SHADOW_SEED_PLAN_INVALID"
    assert transport.calls == []
    assert payload["plan_sha256"] != plan["plan_sha256"]


def test_default_host_transport_rejects_get_and_post_redirects(
    tmp_path: Path,
) -> None:
    """A 30x must be a definite failure and must never carry TB auth to a second origin."""
    del tmp_path
    with _Server() as sink:
        with _Server(redirect_to=f"{sink.url}/captured") as source:
            environment = PilotEnvironment(
                tb_base_url=source.url,
                cmms_base_url="http://127.0.0.1:3000",
                tb_tenant_id=TENANT,
                cmms_company_id=42,
                tb_bearer="tb-token",
                cmms_api_key="cmms-key",
            )
            request = shadow_seed._host_request(environment)

            with pytest.raises(ShadowSeedError, match="THINGSBOARD_REQUEST_FAILED"):
                shadow_seed._call(environment, request, "GET", "/redirect")
            with pytest.raises(ShadowSeedError, match="THINGSBOARD_REQUEST_FAILED"):
                shadow_seed._call(
                    environment,
                    request,
                    "POST",
                    "/redirect",
                    [{"ts": 1, "values": {"vibration_rms": 4.0}}],
                )

            assert [method for method, _ in source.httpd.requests] == ["GET", "POST"]
            assert sink.httpd.requests == []


def test_default_host_transport_ignores_environment_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ambient proxy settings must not receive the ThingsBoard bearer header."""
    with _Server() as proxy:
        for key in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
            monkeypatch.setenv(key, proxy.url)
        for key in ("no_proxy", "NO_PROXY"):
            monkeypatch.setenv(key, "")
        environment = PilotEnvironment(
            tb_base_url="http://phase2-shadow-seed.invalid",
            cmms_base_url="http://127.0.0.1:3000",
            tb_tenant_id=TENANT,
            cmms_company_id=42,
            tb_bearer="tb-token",
            cmms_api_key="cmms-key",
        )

        with pytest.raises(ShadowSeedError, match="THINGSBOARD_UNAVAILABLE"):
            shadow_seed._call(
                environment,
                shadow_seed._host_request(environment),
                "GET",
                "/must-not-use-proxy",
            )

        assert proxy.httpd.requests == []


@pytest.mark.parametrize(
    "payload",
    (
        b'{"ok":true,"ok":false}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
    ),
)
def test_default_host_transport_rejects_non_strict_provider_json(
    payload: bytes,
) -> None:
    """Duplicate or non-finite provider evidence must not reach plan/readback logic."""
    with _Server(payload=payload) as provider:
        environment = PilotEnvironment(
            tb_base_url=provider.url,
            cmms_base_url="http://127.0.0.1:3000",
            tb_tenant_id=TENANT,
            cmms_company_id=42,
            tb_bearer="tb-token",
            cmms_api_key="cmms-key",
        )

        with pytest.raises(ShadowSeedError, match="THINGSBOARD_UNAVAILABLE"):
            shadow_seed._call(
                environment,
                shadow_seed._host_request(environment),
                "GET",
                "/invalid-json",
            )

        assert provider.httpd.requests == [("GET", "Bearer tb-token")]


def test_default_host_transport_bounds_provider_json_before_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider response larger than the evidence budget must be rejected early."""
    payload = b'{"value":"' + (b"x" * MAX_PROVIDER_BYTES) + b'"}'
    with _Server(payload=payload) as provider:
        environment = PilotEnvironment(
            tb_base_url=provider.url,
            cmms_base_url="http://127.0.0.1:3000",
            tb_tenant_id=TENANT,
            cmms_company_id=42,
            tb_bearer="tb-token",
            cmms_api_key="cmms-key",
        )
        parsed = False

        def parsing_is_too_late(*_args, **_kwargs):
            nonlocal parsed
            parsed = True
            raise AssertionError("oversized provider JSON reached parsing")

        monkeypatch.setattr(shadow_seed.json, "loads", parsing_is_too_late)
        with pytest.raises(ShadowSeedError, match="THINGSBOARD_UNAVAILABLE"):
            shadow_seed._call(
                environment,
                shadow_seed._host_request(environment),
                "GET",
                "/oversized-json",
            )

        assert not parsed


def test_runtime_env_rejects_duplicate_and_interpolated_values_without_leaking(
    tmp_path: Path,
) -> None:
    from e2e.conftest import load_pilot_environment

    path = tmp_path / "bad.env"
    path.write_text(
        "PILOT_TB_CREDENTIAL=${LEAK}\nPILOT_TB_CREDENTIAL=x\n", encoding="utf-8"
    )
    path.chmod(0o600)
    with pytest.raises(PilotApiError) as caught:
        load_pilot_environment(path)
    assert "LEAK" not in str(caught.value)


def test_pilot_api_uses_only_the_exact_read_only_baseline_routes() -> None:
    transport = FakeTransport()
    ids = tuple(str(device["id"]["id"]) for device in transport.devices)
    assert PilotApi(_environment(), transport.request).assert_clean_baseline(ids) == (
        0,
        0,
    )
    assert len(transport.calls) == 21
    assert all(method == "GET" for method, path, _ in transport.calls[:-1])
    assert transport.calls[-1][0:2] == ("POST", "/api/work-orders/search")


def test_cli_plan_and_apply_use_the_confirmed_host_contract_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing CLI dispatch or replacing its injected transport must fail this operator contract."""
    monkeypatch.setattr(shadow_seed, "REPO_ROOT", tmp_path)
    tests_cwd = tmp_path / "tests"
    tests_cwd.mkdir()
    monkeypatch.chdir(tests_cwd)
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    env = runtime / "pilot.env"
    _write_runtime_env(env)
    transport = FakeTransport()
    assert (
        shadow_seed.main(
            [
                "plan",
                "--env-file",
                "../.runtime/pilot.env",
                "--actor",
                "operator-a",
                "--output",
                "../.runtime/plans/seed.json",
            ],
            request=transport.request,
            now=datetime(2026, 7, 29, tzinfo=UTC),
        )
        == 0
    )
    plan = json.loads((runtime / "plans" / "seed.json").read_text(encoding="utf-8"))
    assert (
        shadow_seed.main(
            [
                "apply",
                "--env-file",
                "../.runtime/pilot.env",
                "--plan",
                "../.runtime/plans/seed.json",
                "--plan-hash",
                plan["plan_sha256"],
                "--confirmed-hash",
                plan["plan_sha256"],
                "--actor",
                "operator-a",
                "--receipt",
                "../.runtime/receipts/seed.json",
            ],
            request=transport.request,
            now=datetime(2026, 7, 29, 0, 1, tzinfo=UTC),
        )
        == 0
    )
    assert (
        sum(
            method == "POST" and path.endswith("/timeseries/ANY")
            for method, path, _ in transport.calls
        )
        == 20
    )


def test_artifact_paths_reject_symlink_alias_and_expiry_at_the_exact_boundary(
    tmp_path: Path,
) -> None:
    transport = FakeTransport()
    plan_path = tmp_path / "plan.json"
    plan = build_plan(
        _environment(),
        transport.request,
        actor="operator-a",
        output=plan_path,
        now=datetime(2026, 7, 29, tzinfo=UTC),
    )
    receipt = tmp_path / "receipt.json"
    with pytest.raises(ShadowSeedError, match="PLAN_RECEIPT_ALIAS"):
        apply(
            _environment(),
            transport.request,
            plan=plan_path,
            plan_hash=plan["plan_sha256"],
            confirmed_hash=plan["plan_sha256"],
            actor="operator-a",
            receipt=plan_path,
            now=datetime(2026, 7, 29, tzinfo=UTC),
        )
    with pytest.raises(ShadowSeedError, match="SHADOW_SEED_PLAN_EXPIRED"):
        apply(
            _environment(),
            transport.request,
            plan=plan_path,
            plan_hash=plan["plan_sha256"],
            confirmed_hash=plan["plan_sha256"],
            actor="operator-a",
            receipt=receipt,
            now=datetime(2026, 7, 29, 0, 30, tzinfo=UTC),
        )


def test_cli_rejects_lexical_runtime_symlink_before_resolving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolving `.runtime` first must not redirect an artifact write outside the root."""
    monkeypatch.setattr(shadow_seed, "REPO_ROOT", tmp_path)
    tests_cwd = tmp_path / "tests"
    tests_cwd.mkdir()
    monkeypatch.chdir(tests_cwd)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".runtime").symlink_to(outside, target_is_directory=True)
    _write_runtime_env(outside / "pilot.env")
    output = outside / "plans" / "seed.json"

    result = shadow_seed.main(
        [
            "plan",
            "--env-file",
            "../.runtime/pilot.env",
            "--actor",
            "operator-a",
            "--output",
            "../.runtime/plans/seed.json",
        ],
        request=FakeTransport().request,
        now=datetime(2026, 7, 29, tzinfo=UTC),
    )

    assert result == 2
    assert not output.exists()


def test_cli_rejects_nested_lexical_parent_symlink_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nested alias must not hide just because its target remains under `.runtime`."""
    monkeypatch.setattr(shadow_seed, "REPO_ROOT", tmp_path)
    tests_cwd = tmp_path / "tests"
    tests_cwd.mkdir()
    monkeypatch.chdir(tests_cwd)
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    actual = runtime / "actual-plans"
    actual.mkdir()
    (runtime / "plans").symlink_to(actual, target_is_directory=True)
    _write_runtime_env(runtime / "pilot.env")
    output = actual / "seed.json"

    result = shadow_seed.main(
        [
            "plan",
            "--env-file",
            "../.runtime/pilot.env",
            "--actor",
            "operator-a",
            "--output",
            "../.runtime/plans/seed.json",
        ],
        request=FakeTransport().request,
        now=datetime(2026, 7, 29, tzinfo=UTC),
    )

    assert result == 2
    assert not output.exists()


def test_cli_plan_parent_swap_never_creates_an_outside_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A validated plans directory must stay descriptor-bound across provider GETs."""
    monkeypatch.setattr(shadow_seed, "REPO_ROOT", tmp_path)
    tests_cwd = tmp_path / "tests"
    tests_cwd.mkdir()
    monkeypatch.chdir(tests_cwd)
    runtime = tmp_path / ".runtime"
    plans = runtime / "plans"
    plans.mkdir(parents=True)
    held_plans = runtime / "plans-held"
    outside = tmp_path / "outside"
    outside.mkdir()
    _write_runtime_env(runtime / "pilot.env")
    transport = FakeTransport()
    swapped = False

    def request(method: str, path: str, **kwargs):
        nonlocal swapped
        if not swapped:
            plans.rename(held_plans)
            plans.symlink_to(outside, target_is_directory=True)
            swapped = True
        return transport.request(method, path, **kwargs)

    result = shadow_seed.main(
        [
            "plan",
            "--env-file",
            "../.runtime/pilot.env",
            "--actor",
            "operator-a",
            "--output",
            "../.runtime/plans/seed.json",
        ],
        request=request,
        now=datetime(2026, 7, 29, tzinfo=UTC),
    )

    assert result == 2
    assert swapped
    assert not (outside / "seed.json").exists()
    assert not (held_plans / "seed.json").exists()


def test_cli_apply_receipt_parent_swap_aborts_before_the_first_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A swapped receipt parent must fail before telemetry can become irreversible."""
    monkeypatch.setattr(shadow_seed, "REPO_ROOT", tmp_path)
    tests_cwd = tmp_path / "tests"
    tests_cwd.mkdir()
    monkeypatch.chdir(tests_cwd)
    runtime = tmp_path / ".runtime"
    plans = runtime / "plans"
    receipts = runtime / "receipts"
    plans.mkdir(parents=True)
    receipts.mkdir()
    held_receipts = runtime / "receipts-held"
    outside = tmp_path / "outside"
    outside.mkdir()
    _write_runtime_env(runtime / "pilot.env")
    transport = FakeTransport()
    plan_path = plans / "seed.json"
    plan = build_plan(
        _environment(),
        transport.request,
        actor="operator-a",
        output=plan_path,
        now=datetime(2026, 7, 29, tzinfo=UTC),
    )
    transport.calls.clear()
    swapped = False

    def request(method: str, path: str, **kwargs):
        nonlocal swapped
        if not swapped:
            receipts.rename(held_receipts)
            receipts.symlink_to(outside, target_is_directory=True)
            swapped = True
        return transport.request(method, path, **kwargs)

    result = shadow_seed.main(
        [
            "apply",
            "--env-file",
            "../.runtime/pilot.env",
            "--plan",
            "../.runtime/plans/seed.json",
            "--plan-hash",
            plan["plan_sha256"],
            "--confirmed-hash",
            plan["plan_sha256"],
            "--actor",
            "operator-a",
            "--receipt",
            "../.runtime/receipts/seed.json",
        ],
        request=request,
        now=datetime(2026, 7, 29, 0, 1, tzinfo=UTC),
    )

    assert result == 2
    assert swapped
    assert not any(method == "POST" for method, _, _ in transport.calls)
    assert not (outside / "seed.json").exists()
    assert not (held_receipts / "seed.json").exists()


def test_failed_plan_generation_cleans_its_exclusive_reservation(
    tmp_path: Path,
) -> None:
    """A provider failure must not leave an empty plan that blocks a safe retry."""
    transport = FakeTransport()
    transport.drift = True
    output = tmp_path / "plan.json"

    with pytest.raises(ShadowSeedError, match="WINDOW_DRIFT"):
        build_plan(
            _environment(),
            transport.request,
            actor="operator-a",
            output=output,
            now=datetime(2026, 7, 29, tzinfo=UTC),
        )

    assert not output.exists()
