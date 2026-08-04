from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
import stat
import sys

import pytest


ROOT = Path(__file__).parents[2]
SCRIPT_DIR = ROOT / "deploy/compose/scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

state_io = importlib.import_module("pilot_state_io")
reset = importlib.import_module("pilot_reset")


def test_atomic_generated_env_replace_is_complete_and_durable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    target = tmp_path / "closed-loop-generated.env"
    target.write_bytes(b"ORIGINAL=value\n")
    target.chmod(0o600)
    old_inode = target.stat().st_ino
    old_descriptor = os.open(target, os.O_RDONLY)
    payload = b"".join(f"{key}=complete-value\n".encode() for key in reset.GENERATED_ENV_KEYS)
    original_write = state_io.os.write
    original_fsync = state_io.os.fsync
    original_replace = state_io.os.replace
    events: list[str] = []

    def short_write(descriptor: int, content: bytes) -> int:
        return original_write(descriptor, content[: max(1, min(7, len(content)))])

    def record_fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        kind = "directory" if stat.S_ISDIR(metadata.st_mode) else "file"
        events.append(f"fsync:{kind}")
        original_fsync(descriptor)

    def record_replace(source: Path, destination: Path) -> None:
        events.append("replace")
        original_replace(source, destination)

    monkeypatch.setattr(state_io.os, "write", short_write)
    monkeypatch.setattr(state_io.os, "fsync", record_fsync)
    monkeypatch.setattr(state_io.os, "replace", record_replace)
    try:
        state_io.atomic_replace(target, payload, prefix=".atomic-test-")
        assert os.read(old_descriptor, 4096) == b"ORIGINAL=value\n"
    finally:
        os.close(old_descriptor)

    assert target.read_bytes() == payload
    assert target.stat().st_ino != old_inode
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert events[0] == "fsync:file"
    replace_index = events.index("replace")
    assert "fsync:directory" in events[replace_index + 1 :]
    assert not list(tmp_path.glob(".atomic-test-*"))


def test_atomic_replace_fsync_failure_preserves_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    target = tmp_path / "closed-loop-generated.env"
    target.write_bytes(b"ORIGINAL=value\n")
    target.chmod(0o600)
    original_fsync = state_io.os.fsync

    def fail_file_fsync(descriptor: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("simulated fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(state_io.os, "fsync", fail_file_fsync)
    with pytest.raises(state_io.StateIoError, match="STATE_ATOMIC_WRITE_FAILED"):
        state_io.atomic_replace(target, b"REPLACEMENT=value\n", prefix=".atomic-fail-")
    assert target.read_bytes() == b"ORIGINAL=value\n"
    assert not list(tmp_path.glob(".atomic-fail-*"))


@pytest.mark.parametrize("replacement", [b"TOKEN=B\n", b"TOKEN=A\n"])
def test_reset_rejects_generated_state_drift_before_first_volume_remove(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    replacement: bytes,
) -> None:
    state = tmp_path / "state"
    receipts = state / "receipts"
    receipts.mkdir(parents=True, mode=0o700)
    state.chmod(0o700)
    state_lock = tmp_path / reset.STATE_LOCK_NAME
    state_lock.touch(mode=0o600)
    state_lock.chmod(0o600)
    generated = state / reset.GENERATED_ENV_NAME
    generated.write_bytes(b"TOKEN=A\n")
    generated.chmod(0o600)
    env_file = tmp_path / reset.ENV_NAME
    env_file.write_text("BASE=value\n", encoding="utf-8")
    env_file.chmod(0o600)

    def fake_plan(_env_file: Path, _docker_mode: str) -> dict[str, object]:
        _raw, binding = reset._file_snapshot(
            generated,
            code="PILOT_GENERATED_ENV_FILE_INVALID",
            maximum=reset.MAX_ENV_BYTES,
        )
        canonical = {
            "schema_version": "closed-loop-reset-v1",
            "project": reset.PROJECT,
            "operation": "test-reset",
            "runtime": {"env_files": {"generated": binding}},
            "volumes": [{"name": "must-not-be-removed"}],
        }
        return {
            **canonical,
            "plan_sha256": hashlib.sha256(reset._canonical(canonical)).hexdigest(),
        }

    monkeypatch.setattr(reset, "_layout", lambda _env: (tmp_path, tmp_path, state))
    monkeypatch.setattr(reset, "_build_plan_unlocked", fake_plan)

    def fake_down(_prefix: list[str], _env: Path, _recovery: Path) -> None:
        state_io.atomic_replace(generated, replacement, prefix=".drift-")

    monkeypatch.setattr(reset, "_compose_down", fake_down)
    docker_calls: list[list[str]] = []
    monkeypatch.setattr(
        reset,
        "_run",
        lambda command: docker_calls.append(command),
    )
    plan = fake_plan(env_file, "direct")
    assert "TOKEN=A" not in json.dumps(plan)

    with pytest.raises(reset.ResetError, match="RESET_PLAN_CHANGED_AFTER_STOP"):
        reset.apply(plan, str(plan["plan_sha256"]), env_file)

    assert docker_calls == []
    assert generated.read_bytes() == replacement
    assert not (state / "recycle").exists()


def test_reset_can_bind_corrupt_generated_state_without_parsing_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    receipts = state / "receipts"
    receipts.mkdir(parents=True, mode=0o700)
    state.chmod(0o700)
    state_lock = tmp_path / reset.STATE_LOCK_NAME
    state_lock.touch(mode=0o600)
    state_lock.chmod(0o600)
    generated = state / reset.GENERATED_ENV_NAME
    generated.write_bytes(b"not-an-env-file\x00truncated")
    generated.chmod(0o600)
    env_file = tmp_path / reset.ENV_NAME
    env_file.write_text("BASE=value\n", encoding="utf-8")
    env_file.chmod(0o600)
    monkeypatch.setattr(
        reset,
        "_layout",
        lambda _env: (tmp_path, tmp_path, state),
    )
    monkeypatch.setattr(
        reset,
        "_validate_base",
        lambda *_args: ({"BASE": "value"}, {"sha256": "a" * 64}),
    )

    _state, _values, binding = reset._runtime_binding(env_file)
    generated_binding = binding["env_files"]["generated"]
    assert generated_binding["valid"] is False
    assert generated_binding["sha256"] == hashlib.sha256(generated.read_bytes()).hexdigest()
