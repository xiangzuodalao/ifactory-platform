#!/usr/bin/env python3
"""Hash-confirmed recovery of only the dedicated closed-loop pilot state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from pilot_preflight import (  # noqa: E402
    ENV_NAME,
    GENERATED_ENV_KEYS,
    GENERATED_ENV_NAME,
    MAX_ENV_BYTES,
    SECRET_FILES,
    STATE_LOCK_NAME,
    PreflightError,
    _metadata,
    parse_env_bytes,
)
from pilot_state_io import (  # noqa: E402
    StateIoError,
    atomic_replace,
    commit_atomic_replacement,
    exclusive_state_lock,
    fsync_directory,
    prepare_atomic_replacement,
)


PROJECT = "ifactory-closed-loop-pilot"
LOGICAL_VOLUMES = (
    "cmms-postgres-data",
    "cmms-minio-data",
    "integration-postgres-data",
    "tb-relay-sockets",
)
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
STATUS_RECEIPT = re.compile(r"^cmms-status-[0-9a-f]{64}\.json$")
MAX_RECEIPT_BYTES = 64 * 1024
MAX_STATUS_RECEIPTS = 1024


class ResetError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _docker_prefix(mode: str) -> list[str]:
    if mode == "direct":
        return ["docker", "--host", "unix:///var/run/docker.sock"]
    if mode == "sudo":
        return ["sudo", "docker", "--host", "unix:///var/run/docker.sock"]
    raise ResetError("DOCKER_MODE_INVALID")


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": SAFE_PATH},
    )


def _secure_directory(path: Path, code: str) -> dict[str, object]:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        raise ResetError(code) from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.getuid()
        or metadata.st_gid != os.getgid()
        or path.is_symlink()
    ):
        raise ResetError(code)
    return {
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "mode": stat.S_IMODE(metadata.st_mode),
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
    }


def _file_snapshot(
    path: Path,
    *,
    code: str,
    maximum: int,
) -> tuple[bytes, dict[str, object]]:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ResetError(code) from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
            or metadata.st_gid != os.getgid()
            or metadata.st_nlink != 1
        ):
            raise ResetError(code)
        chunks: list[bytes] = []
        length = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - length))
            if not chunk:
                break
            chunks.append(chunk)
            length += len(chunk)
            if length > maximum:
                raise ResetError(code)
        raw = b"".join(chunks)
        return raw, {
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "mode": stat.S_IMODE(metadata.st_mode),
            "uid": metadata.st_uid,
            "gid": metadata.st_gid,
            "links": metadata.st_nlink,
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    except OSError:
        raise ResetError(code) from None
    finally:
        os.close(descriptor)


def _layout(env_file: Path) -> tuple[Path, Path, Path]:
    repo_root = Path(__file__).resolve().parents[3]
    runtime = repo_root / ".runtime"
    control = runtime / "closed-loop"
    state = control / "state"
    _secure_directory(runtime, "PILOT_RUNTIME_DIR_INVALID")
    _secure_directory(control, "PILOT_CONTROL_DIR_INVALID")
    _secure_directory(state, "PILOT_STATE_DIR_INVALID")
    _secure_directory(state / "receipts", "PILOT_RECEIPTS_DIR_INVALID")
    try:
        _metadata(control / STATE_LOCK_NAME, kind="PILOT_STATE_LOCK_FILE", mode=0o600)
    except PreflightError as exc:
        raise ResetError(exc.code) from None
    if env_file != control / ENV_NAME:
        raise ResetError("PILOT_ENV_FILE_INVALID")
    return repo_root, runtime, state


def _validate_base(
    repo_root: Path,
    runtime: Path,
    state: Path,
    env_file: Path,
) -> tuple[dict[str, str], dict[str, object]]:
    raw, binding = _file_snapshot(
        env_file,
        code="PILOT_ENV_FILE_INVALID",
        maximum=MAX_ENV_BYTES,
    )
    try:
        values = parse_env_bytes(raw)
    except PreflightError as exc:
        raise ResetError(exc.code) from None
    if set(values) & set(GENERATED_ENV_KEYS):
        raise ResetError("PILOT_ENV_FILE_INVALID")
    if values.get("PILOT_RUNTIME_DIR") != str(runtime):
        raise ResetError("PILOT_RUNTIME_BINDING_INVALID")
    if values.get("PILOT_STATE_DIR") != str(state):
        raise ResetError("PILOT_STATE_BINDING_INVALID")
    state_lock = state.parent / STATE_LOCK_NAME
    if values.get("PILOT_STATE_LOCK_FILE") != str(state_lock):
        raise ResetError("PILOT_STATE_LOCK_BINDING_INVALID")
    if values.get("PILOT_HOST_UID") != str(os.getuid()):
        raise ResetError("PILOT_HOST_IDENTITY_INVALID")
    if values.get("PILOT_HOST_GID") != str(os.getgid()):
        raise ResetError("PILOT_HOST_IDENTITY_INVALID")
    secrets = runtime / "secrets"
    try:
        _metadata(secrets, kind="PILOT_SECRETS_DIR", mode=0o700)
        for key, filename in SECRET_FILES.items():
            expected = secrets / filename
            if values.get(key) != str(expected):
                raise ResetError("PILOT_SECRET_BINDING_INVALID")
            _metadata(expected, kind="PILOT_SECRET_FILE", mode=0o600)
    except PreflightError as exc:
        raise ResetError(exc.code) from None
    binding["path"] = ".runtime/closed-loop/closed-loop-pilot.env"
    return values, binding


def _receipt_binding(path: Path) -> dict[str, object]:
    raw, binding = _file_snapshot(
        path,
        code="PILOT_RECEIPT_INVALID",
        maximum=MAX_RECEIPT_BYTES,
    )
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ResetError("PILOT_RECEIPT_INVALID") from None
    if type(payload) is not dict:
        raise ResetError("PILOT_RECEIPT_INVALID")
    return {
        **binding,
        "present": True,
        "schema_version": payload.get("schema_version"),
        "status": payload.get("status"),
        "plan_sha256": payload.get("plan_sha256"),
    }


def _runtime_binding(env_file: Path) -> tuple[Path, dict[str, str], dict[str, object]]:
    repo_root, runtime, state = _layout(env_file)
    values, base_binding = _validate_base(repo_root, runtime, state, env_file)
    generated_path = state / GENERATED_ENV_NAME
    generated_raw, generated_binding = _file_snapshot(
        generated_path,
        code="PILOT_GENERATED_ENV_FILE_INVALID",
        maximum=MAX_ENV_BYTES,
    )
    try:
        generated = parse_env_bytes(generated_raw, kind="PILOT_GENERATED_ENV_FILE")
        generated_valid = set(generated) == set(GENERATED_ENV_KEYS)
    except PreflightError:
        generated = {}
        generated_valid = False
    generated_binding.update(
        {
            "path": ".runtime/closed-loop/state/closed-loop-generated.env",
            "valid": generated_valid,
        }
    )

    receipts_dir = state / "receipts"
    try:
        entries = sorted(path.name for path in receipts_dir.iterdir())
    except OSError:
        raise ResetError("PILOT_RECEIPTS_DIR_INVALID") from None
    allowed_fixed = {
        "closed-loop-bootstrap.json",
        "closed-loop-provision.json",
    }
    unexpected = [
        name
        for name in entries
        if name not in allowed_fixed and STATUS_RECEIPT.fullmatch(name) is None
    ]
    if unexpected:
        raise ResetError("PILOT_RECEIPTS_DIR_UNEXPECTED_ENTRY")
    status_names = [name for name in entries if STATUS_RECEIPT.fullmatch(name) is not None]
    if len(status_names) > MAX_STATUS_RECEIPTS:
        raise ResetError("PILOT_RECEIPTS_LIMIT_EXCEEDED")

    def optional_receipt(name: str) -> dict[str, object]:
        path = receipts_dir / name
        if name not in entries:
            return {"present": False}
        return _receipt_binding(path)

    receipt_binding = {
        "directory": _secure_directory(receipts_dir, "PILOT_RECEIPTS_DIR_INVALID"),
        "entries": entries,
        "bootstrap": optional_receipt("closed-loop-bootstrap.json"),
        "provision": optional_receipt("closed-loop-provision.json"),
        "cmms_status": [
            {"name": name, "binding": _receipt_binding(receipts_dir / name)}
            for name in status_names
        ],
    }
    binding = {
        "directories": {
            "state": _secure_directory(state, "PILOT_STATE_DIR_INVALID"),
            "receipts": receipt_binding["directory"],
        },
        "env_files": {"base": base_binding, "generated": generated_binding},
        "state_lock": _file_snapshot(
            state.parent / STATE_LOCK_NAME,
            code="PILOT_STATE_LOCK_FILE_INVALID",
            maximum=4096,
        )[1],
        "receipts": receipt_binding,
    }
    return state, values, binding


def _volumes(prefix: list[str]) -> list[dict[str, str]]:
    volumes: list[dict[str, str]] = []
    for logical_name in LOGICAL_VOLUMES:
        name = f"{PROJECT}_{logical_name}"
        listed = _run(
            [*prefix, "volume", "ls", "--filter", f"name=^{name}$", "--format", "{{.Name}}"]
        )
        if listed.returncode != 0:
            raise ResetError("VOLUME_LIST_FAILED")
        names = [line for line in listed.stdout.splitlines() if line]
        if any(candidate != name for candidate in names) or len(names) > 1:
            raise ResetError("VOLUME_LIST_INVALID")
        if not names:
            continue
        inspected = _run([*prefix, "volume", "inspect", name])
        if inspected.returncode != 0:
            raise ResetError("VOLUME_INSPECTION_FAILED")
        try:
            payload = json.loads(inspected.stdout)
            item = payload[0]
            labels = item["Labels"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
            raise ResetError("VOLUME_INSPECTION_INVALID") from None
        if (
            item.get("Name") != name
            or item.get("Driver") != "local"
            or labels.get("com.docker.compose.project") != PROJECT
            or labels.get("com.docker.compose.volume") != logical_name
        ):
            raise ResetError("VOLUME_IDENTITY_MISMATCH")
        volumes.append(
            {
                "name": name,
                "driver": "local",
                "project_label": PROJECT,
                "volume_label": logical_name,
            }
        )
    return volumes


def _build_plan_unlocked(env_file: Path, docker_mode: str) -> dict[str, object]:
    _state, _values, runtime_binding = _runtime_binding(env_file)
    prefix = _docker_prefix(docker_mode)
    daemon = _run([*prefix, "info", "--format", "{{.ServerVersion}}"])
    if daemon.returncode != 0 or not daemon.stdout.strip():
        raise ResetError("DOCKER_DAEMON_UNAVAILABLE")
    canonical = {
        "schema_version": "closed-loop-reset-v1",
        "project": PROJECT,
        "operation": "stop_project_remove_dedicated_volumes_and_rebuild_generated_state",
        "runtime": runtime_binding,
        "volumes": _volumes(prefix),
    }
    return {**canonical, "plan_sha256": hashlib.sha256(_canonical(canonical)).hexdigest()}


def build_plan(env_file: Path, docker_mode: str = "direct") -> dict[str, object]:
    _repo, _runtime, state = _layout(env_file)
    try:
        with exclusive_state_lock(state.parent / STATE_LOCK_NAME):
            return _build_plan_unlocked(env_file, docker_mode)
    except StateIoError:
        raise ResetError("PILOT_STATE_LOCK_FAILED") from None


def _down_environment() -> bytes:
    placeholders = {
        "PLATFORM_INTEGRATION_TB_TENANT_ID": "00000000-0000-4000-8000-000000000002",
        "PLATFORM_INTEGRATION_CMMS_COMPANY_ID": "1",
        "PLATFORM_INTEGRATION_APPROVER_TB_USER_ID": "00000000-0000-4000-8000-000000000003",
        "PLATFORM_INTEGRATION_PILOT_WORK_ORDER_EQUIPMENT_ID": "00000000-0000-4000-8000-000000000004",
        "PILOT_CMMS_CREDENTIAL": '{"kind":"cmms_bearer","value":"reset-placeholder"}',
    }
    return "".join(f"{key}={placeholders[key]}\n" for key in GENERATED_ENV_KEYS).encode()


def _write_temporary(directory: Path, prefix: str, content: bytes) -> Path:
    try:
        return prepare_atomic_replacement(
            directory / "closed-loop-target",
            content,
            prefix=prefix,
        )
    except StateIoError:
        raise ResetError("RESET_STATE_PREPARE_FAILED") from None


def _stage_receipt(
    state: Path,
    receipt: dict[str, object],
    source_name: str,
    archive_name: str,
) -> tuple[Path, Path, dict[str, object]] | None:
    if not receipt["present"]:
        return None
    source = state / "receipts" / source_name
    raw, observed = _file_snapshot(
        source,
        code="PILOT_RECEIPT_CHANGED",
        maximum=MAX_RECEIPT_BYTES,
    )
    expected = {key: receipt[key] for key in observed}
    if observed != expected:
        raise ResetError("PILOT_RECEIPT_CHANGED")
    recycle = state / "recycle"
    recycle_created = False
    try:
        recycle.mkdir(mode=0o700)
        recycle_created = True
    except FileExistsError:
        pass
    except OSError:
        raise ResetError("RECYCLE_DIR_INVALID") from None
    _secure_directory(recycle, "RECYCLE_DIR_INVALID")
    if recycle_created:
        try:
            fsync_directory(state)
        except StateIoError:
            raise ResetError("RECYCLE_DIR_SYNC_FAILED") from None
    target = recycle / f"{archive_name}-{observed['sha256'][:16]}.json"
    try:
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
    except FileExistsError:
        existing, metadata = _file_snapshot(
            target,
            code="RECYCLED_RECEIPT_INVALID",
            maximum=MAX_RECEIPT_BYTES,
        )
        if existing != raw or metadata["sha256"] != observed["sha256"]:
            raise ResetError("RECYCLED_RECEIPT_INVALID")
    except OSError:
        raise ResetError("BOOTSTRAP_RECEIPT_ARCHIVE_FAILED") from None
    else:
        try:
            offset = 0
            while offset < len(raw):
                written = os.write(descriptor, raw[offset:])
                if written <= 0:
                    raise OSError("short write")
                offset += written
            os.fsync(descriptor)
        except OSError:
            os.close(descriptor)
            try:
                target.unlink()
            except OSError:
                pass
            raise ResetError("BOOTSTRAP_RECEIPT_ARCHIVE_FAILED") from None
        os.close(descriptor)
    try:
        fsync_directory(recycle)
    except StateIoError:
        raise ResetError("BOOTSTRAP_RECEIPT_ARCHIVE_FAILED") from None
    return source, target, observed


def _commit_staged_receipts(
    staged: list[tuple[Path, Path, dict[str, object]]],
) -> None:
    for source, target, expected in staged:
        _target_raw, target_binding = _file_snapshot(
            target,
            code="RECYCLED_RECEIPT_INVALID",
            maximum=MAX_RECEIPT_BYTES,
        )
        _source_raw, source_binding = _file_snapshot(
            source,
            code="PILOT_RECEIPT_CHANGED",
            maximum=MAX_RECEIPT_BYTES,
        )
        if target_binding["sha256"] != expected["sha256"] or source_binding != expected:
            raise ResetError("PILOT_RECEIPT_CHANGED")
    for source, _target, _expected in staged:
        try:
            source.unlink()
        except OSError:
            raise ResetError("BOOTSTRAP_RECEIPT_ARCHIVE_FAILED") from None
    if staged:
        try:
            fsync_directory(staged[0][0].parent)
        except StateIoError:
            raise ResetError("BOOTSTRAP_RECEIPT_ARCHIVE_FAILED") from None


def _canonical_generated() -> bytes:
    return "".join(f"{key}=\n" for key in GENERATED_ENV_KEYS).encode()


def _clear_generated_env(env_file: Path) -> None:
    try:
        atomic_replace(
            env_file,
            _canonical_generated(),
            prefix=".closed-loop-reset-",
        )
    except StateIoError:
        raise ResetError("PILOT_GENERATED_ENV_RESET_FAILED") from None


def _compose_down(
    prefix: list[str],
    env_file: Path,
    generated_recovery: Path,
) -> None:
    compose_file = Path(__file__).resolve().parents[1] / "closed-loop-pilot.yml"
    stopped = _run(
        [
            *prefix,
            "compose",
            "--project-name",
            PROJECT,
            "--env-file",
            str(env_file),
            "--env-file",
            str(generated_recovery),
            "-f",
            str(compose_file),
            "--profile",
            "continuous",
            "--profile",
            "bootstrap",
            "--profile",
            "acceptance",
            "down",
            "--remove-orphans",
        ]
    )
    if stopped.returncode != 0:
        raise ResetError("PROJECT_STOP_FAILED")
    remaining = _run(
        [
            *prefix,
            "container",
            "ls",
            "--all",
            "--filter",
            f"label=com.docker.compose.project={PROJECT}",
            "--format",
            "{{.ID}}",
        ]
    )
    if remaining.returncode != 0:
        raise ResetError("PROJECT_CONTAINER_CHECK_FAILED")
    if remaining.stdout.split():
        raise ResetError("PROJECT_CONTAINERS_REMAIN")


def apply(
    plan: dict[str, object],
    confirmed_hash: str,
    env_file: Path,
    docker_mode: str = "direct",
) -> None:
    if SHA256.fullmatch(confirmed_hash) is None or confirmed_hash != plan.get("plan_sha256"):
        raise ResetError("RESET_CONFIRMATION_MISMATCH")
    _repo, _runtime, state = _layout(env_file)
    try:
        with exclusive_state_lock(state.parent / STATE_LOCK_NAME):
            current = _build_plan_unlocked(env_file, docker_mode)
            if current != plan or current["plan_sha256"] != confirmed_hash:
                raise ResetError("RESET_PLAN_CHANGED")
            prefix = _docker_prefix(docker_mode)
            recovery = _write_temporary(Path("/tmp"), ".closed-loop-down-", _down_environment())
            generated_path = state / GENERATED_ENV_NAME
            generated_replacement = _write_temporary(
                state,
                ".closed-loop-generated-reset-",
                _canonical_generated(),
            )
            staged: list[tuple[Path, Path, dict[str, object]]] = []
            try:
                _compose_down(prefix, env_file, recovery)
                post_stop = _build_plan_unlocked(env_file, docker_mode)
                if post_stop != plan:
                    raise ResetError("RESET_PLAN_CHANGED_AFTER_STOP")
                receipts = post_stop["runtime"]["receipts"]
                candidates = [
                    (receipts["bootstrap"], "closed-loop-bootstrap.json", "closed-loop-bootstrap"),
                    (receipts["provision"], "closed-loop-provision.json", "closed-loop-provision"),
                ]
                candidates.extend(
                    (
                        item["binding"],
                        item["name"],
                        item["name"].removesuffix(".json"),
                    )
                    for item in receipts["cmms_status"]
                )
                for binding, source_name, archive_name in candidates:
                    item = _stage_receipt(state, binding, source_name, archive_name)
                    if item is not None:
                        staged.append(item)
                for volume in plan["volumes"]:
                    removed = _run([*prefix, "volume", "rm", volume["name"]])
                    if removed.returncode != 0:
                        raise ResetError("VOLUME_REMOVE_FAILED")
                _commit_staged_receipts(staged)
                try:
                    commit_atomic_replacement(generated_replacement, generated_path)
                except StateIoError:
                    raise ResetError("PILOT_GENERATED_ENV_RESET_FAILED") from None
            finally:
                for temporary in (recovery, generated_replacement):
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass
    except StateIoError:
        raise ResetError("PILOT_STATE_LOCK_FAILED") from None


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "apply"):
        child = subparsers.add_parser(command)
        child.add_argument("--env-file", type=Path, required=True)
        child.add_argument("--docker-mode", choices=("direct", "sudo"), default="direct")
        if command == "apply":
            child.add_argument("--confirmed-hash", required=True)
    args = parser.parse_args()
    try:
        if args.command == "plan":
            plan = build_plan(args.env_file, args.docker_mode)
            print(_canonical(plan).decode())
        else:
            plan = build_plan(args.env_file, args.docker_mode)
            apply(plan, args.confirmed_hash, args.env_file, args.docker_mode)
            print(json.dumps({"status": "SUCCEEDED", "plan_sha256": plan["plan_sha256"]}))
        return 0
    except ResetError as exc:
        print(json.dumps({"status": "FAILED", "code": exc.code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
