"""Offline security contract for CMMS deployment file and process primitives."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import time
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import ifactory_cmms_deploy
from ifactory_cmms_deploy import process as secure_process
from ifactory_cmms_deploy import secure_io
from ifactory_cmms_deploy.cli import main
from ifactory_cmms_deploy.errors import DeploymentError
from ifactory_cmms_deploy.process import CommandResult, CommandRunner, CommandSpec
from ifactory_cmms_deploy.secure_io import (
    RuntimePathPolicy,
    SecureSnapshot,
    atomic_write_private,
    read_secure_bytes,
)
from e2e.support import SafeRuntimeFixture


SENTINEL_SECRET = "cmms-sentinel-secret-7f4d2a"


def _private_file(path: Path, data: bytes = b"private-value") -> Path:
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def test_secure_reader_accepts_current_uid_private_regular_file(
    tmp_path: Path,
) -> None:
    artifact = _private_file(tmp_path / "runtime.env")
    policy = RuntimePathPolicy.for_test(tmp_path, allowed_files={artifact})

    assert read_secure_bytes(artifact, max_bytes=64, policy=policy) == b"private-value"


@pytest.mark.parametrize(
    "invalid_limit",
    [
        pytest.param(True, id="bool"),
        pytest.param(-1, id="negative"),
        pytest.param(1.0, id="float"),
        pytest.param("64", id="string"),
    ],
)
def test_secure_reader_rejects_invalid_max_bytes_before_opening_any_path(
    tmp_path: Path,
    invalid_limit: object,
) -> None:
    artifact = tmp_path / "missing-parent" / "artifact"
    policy = RuntimePathPolicy.for_test(tmp_path, allowed_files={artifact})

    with pytest.raises(DeploymentError) as caught:
        read_secure_bytes(
            artifact,
            max_bytes=invalid_limit,  # type: ignore[arg-type]
            policy=policy,
        )

    assert caught.value.code == "CMMS-E002"
    assert not artifact.parent.exists()


def test_secure_snapshot_holds_and_closes_the_verified_descriptor(
    tmp_path: Path,
) -> None:
    artifact = _private_file(tmp_path / "runtime.env")
    descriptor = os.open(artifact, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    metadata = os.fstat(descriptor)
    data = b"private-value"

    with SecureSnapshot(
        path=artifact,
        fd=descriptor,
        stat=metadata,
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
    ) as snapshot:
        assert snapshot.fd == descriptor
        assert os.fstat(snapshot.fd).st_ino == metadata.st_ino
        assert snapshot.data == data

    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_secure_reader_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "secret"
    target.write_bytes(b"test-secret")
    target.chmod(0o600)
    link = tmp_path / "secret-link"
    link.symlink_to(target)
    policy = RuntimePathPolicy.for_test(tmp_path, allowed_files={link})

    with pytest.raises(DeploymentError) as caught:
        read_secure_bytes(link, max_bytes=64, policy=policy)

    assert caught.value.code == "CMMS-E001"
    assert "test-secret" not in caught.value.safe_message


@pytest.mark.parametrize(
    "unsafe_kind",
    ["hardlink", "fifo", "directory", "wrong-owner", "group-readable"],
)
def test_secure_reader_rejects_unsafe_file_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_kind: str,
) -> None:
    artifact = tmp_path / "artifact"
    if unsafe_kind == "fifo":
        os.mkfifo(artifact, mode=0o600)
    elif unsafe_kind == "directory":
        artifact.mkdir(mode=0o700)
    else:
        _private_file(artifact)
        if unsafe_kind == "hardlink":
            os.link(artifact, tmp_path / "second-link")
        elif unsafe_kind == "wrong-owner":
            monkeypatch.setattr(secure_io.os, "getuid", lambda: os.geteuid() + 1)
        elif unsafe_kind == "group-readable":
            artifact.chmod(0o640)
    policy = RuntimePathPolicy.for_test(tmp_path, allowed_files={artifact})

    with pytest.raises(DeploymentError) as caught:
        read_secure_bytes(artifact, max_bytes=64, policy=policy)

    assert caught.value.code == "CMMS-E001"
    assert SENTINEL_SECRET not in str(caught.value)


@pytest.mark.parametrize(
    ("data", "max_bytes"),
    [
        pytest.param(b"", 64, id="empty-required-value"),
        pytest.param(b"value\x00suffix", 64, id="nul-byte"),
        pytest.param(b"x" * 65, 64, id="over-limit"),
    ],
)
def test_secure_reader_rejects_invalid_or_over_limit_content(
    tmp_path: Path,
    data: bytes,
    max_bytes: int,
) -> None:
    artifact = _private_file(tmp_path / "artifact", data)
    policy = RuntimePathPolicy.for_test(tmp_path, allowed_files={artifact})

    with pytest.raises(DeploymentError) as caught:
        read_secure_bytes(artifact, max_bytes=max_bytes, policy=policy)

    assert caught.value.code in {"CMMS-E001", "CMMS-E002"}
    decoded = data.decode("utf-8", errors="ignore")
    if decoded:
        assert decoded not in caught.value.safe_message


@pytest.mark.parametrize("mutation", ["truncate", "same-size-overwrite"])
def test_secure_reader_rejects_real_descriptor_metadata_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    original = b"private-value"
    artifact = _private_file(tmp_path / "artifact", original)
    policy = RuntimePathPolicy.for_test(tmp_path, allowed_files={artifact})
    original_metadata = artifact.stat()
    real_read = secure_io.os.read
    mutated = False

    def mutate_real_inode_then_read(descriptor: int, size: int) -> bytes:
        nonlocal mutated
        if not mutated:
            mutated = True
            writer = os.open(artifact, os.O_WRONLY | os.O_NOFOLLOW)
            try:
                if mutation == "truncate":
                    os.ftruncate(writer, len(original) - 1)
                else:
                    assert os.pwrite(writer, b"changed-value", 0) == len(original)
                os.fsync(writer)
            finally:
                os.close(writer)
            if mutation == "same-size-overwrite":
                os.utime(
                    artifact,
                    ns=(
                        original_metadata.st_atime_ns,
                        original_metadata.st_mtime_ns + 1_000_000_000,
                    ),
                    follow_symlinks=False,
                )
        return real_read(descriptor, size)

    monkeypatch.setattr(
        secure_io.os,
        "read",
        mutate_real_inode_then_read,
    )

    with pytest.raises(DeploymentError) as caught:
        read_secure_bytes(artifact, max_bytes=64, policy=policy)

    assert caught.value.code == "CMMS-E001"
    after = artifact.stat()
    if mutation == "truncate":
        assert after.st_size == len(original) - 1
    else:
        assert after.st_size == len(original)
        assert after.st_mtime_ns != original_metadata.st_mtime_ns


def test_secure_reader_rejects_symlink_in_parent_directory(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    artifact = _private_file(real_parent / "artifact")
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    linked_artifact = linked_parent / artifact.name
    policy = RuntimePathPolicy.for_test(tmp_path, allowed_files={linked_artifact})

    with pytest.raises(DeploymentError) as caught:
        read_secure_bytes(linked_artifact, max_bytes=64, policy=policy)

    assert caught.value.code == "CMMS-E001"


@pytest.mark.parametrize("escape", ["sibling", "parent-alias"])
def test_runtime_policy_rejects_files_outside_its_exact_root(
    tmp_path: Path,
    escape: str,
) -> None:
    runtime = tmp_path / "repo" / ".runtime"
    runtime.mkdir(parents=True)
    outside = tmp_path / "outside"
    candidate = outside if escape == "sibling" else runtime / ".." / ".." / "outside"

    with pytest.raises(DeploymentError) as caught:
        RuntimePathPolicy.for_test(runtime, allowed_files={candidate})

    assert caught.value.code == "CMMS-E001"


def test_runtime_policy_rejects_a_non_whitelisted_runtime_file(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    allowed = _private_file(runtime / "cmms-development.env")
    unlisted = _private_file(runtime / "unexpected.env")
    policy = RuntimePathPolicy.for_test(runtime, allowed_files={allowed})

    with pytest.raises(DeploymentError) as caught:
        read_secure_bytes(unlisted, max_bytes=64, policy=policy)

    assert caught.value.code == "CMMS-E001"


def test_atomic_write_exclusively_creates_a_private_file(tmp_path: Path) -> None:
    artifact = tmp_path / "state.json"
    policy = RuntimePathPolicy.for_test(tmp_path, allowed_files={artifact})

    atomic_write_private(
        artifact,
        b'{"state":"new"}',
        replace=False,
        policy=policy,
    )

    assert artifact.read_bytes() == b'{"state":"new"}'
    metadata = artifact.stat()
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert metadata.st_uid == os.getuid()
    assert metadata.st_nlink == 1

    with pytest.raises(DeploymentError) as caught:
        atomic_write_private(
            artifact,
            b'{"state":"duplicate"}',
            replace=False,
            policy=policy,
        )
    assert caught.value.code == "CMMS-E001"
    assert artifact.read_bytes() == b'{"state":"new"}'


def test_atomic_write_replaces_existing_file_and_fsyncs_file_and_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _private_file(tmp_path / "state.json", b'{"state":"old"}')
    policy = RuntimePathPolicy.for_test(tmp_path, allowed_files={artifact})
    real_fsync = secure_io.os.fsync
    fsynced_kinds: list[str] = []

    def recording_fsync(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        fsynced_kinds.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(descriptor)

    monkeypatch.setattr(secure_io.os, "fsync", recording_fsync)

    atomic_write_private(
        artifact,
        b'{"state":"new"}',
        replace=True,
        policy=policy,
    )

    assert artifact.read_bytes() == b'{"state":"new"}'
    assert "file" in fsynced_kinds
    assert "directory" in fsynced_kinds


def test_atomic_write_reopens_and_revalidates_the_renamed_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _private_file(tmp_path / "state.json", b"old-value")
    attacker = _private_file(tmp_path / "attacker", b"attacker-value")
    policy = RuntimePathPolicy.for_test(tmp_path, allowed_files={artifact})
    real_replace = secure_io.os.replace

    def replace_then_swap(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )
        os.unlink(destination, dir_fd=dst_dir_fd)
        os.symlink(attacker.name, destination, dir_fd=dst_dir_fd)

    monkeypatch.setattr(secure_io.os, "replace", replace_then_swap)

    with pytest.raises(DeploymentError) as caught:
        atomic_write_private(
            artifact,
            b"new-value",
            replace=True,
            policy=policy,
        )

    assert caught.value.code == "CMMS-E001"
    assert "attacker-value" not in caught.value.safe_message


def test_atomic_write_rejects_same_content_regular_inode_swap_after_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _private_file(tmp_path / "state.json", b"old-value")
    policy = RuntimePathPolicy.for_test(tmp_path, allowed_files={artifact})
    expected = b"new-value"
    real_replace = secure_io.os.replace

    def replace_then_swap(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )
        attacker_name = ".same-content-attacker"
        attacker_fd = os.open(
            attacker_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=dst_dir_fd,
        )
        try:
            assert os.write(attacker_fd, expected) == len(expected)
            os.fsync(attacker_fd)
        finally:
            os.close(attacker_fd)
        real_replace(
            attacker_name,
            destination,
            src_dir_fd=dst_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(secure_io.os, "replace", replace_then_swap)

    with pytest.raises(DeploymentError) as caught:
        atomic_write_private(
            artifact,
            expected,
            replace=True,
            policy=policy,
        )

    assert caught.value.code == "CMMS-E001"
    assert artifact.read_bytes() == expected
    metadata = artifact.stat()
    assert stat.S_ISREG(metadata.st_mode)
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert metadata.st_nlink == 1


def test_atomic_write_does_not_create_a_missing_parent_chain(tmp_path: Path) -> None:
    artifact = tmp_path / "missing" / "state.json"
    policy = RuntimePathPolicy.for_test(tmp_path, allowed_files={artifact})

    with pytest.raises(DeploymentError) as caught:
        atomic_write_private(artifact, b"value", replace=False, policy=policy)

    assert caught.value.code == "CMMS-E001"
    assert not artifact.parent.exists()


def test_command_specs_and_results_are_immutable(tmp_path: Path) -> None:
    spec = CommandSpec(
        argv=("/usr/bin/env",),
        cwd=tmp_path,
        environment={"PATH": "/usr/bin:/bin"},
        timeout_seconds=5,
        stdout_limit=4096,
        stderr_limit=4096,
        safe_label="environment probe",
    )
    result = CommandResult(returncode=0, stdout="", stderr="")

    with pytest.raises(FrozenInstanceError):
        spec.cwd = Path("/")  # type: ignore[misc]
    with pytest.raises(TypeError):
        spec.environment["PATH"] = "/unsafe"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        result.returncode = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    "unsafe_relative",
    [
        pytest.param("", id="empty"),
        pytest.param(".", id="dot"),
        pytest.param("..", id="dot-dot"),
        pytest.param("../outside", id="parent-component"),
        pytest.param("safe/../escape", id="nested-parent-component"),
    ],
)
@pytest.mark.parametrize("operation", ["directory", "file"])
def test_safe_runtime_fixture_rejects_unsafe_lexical_paths_before_writing(
    tmp_path: Path,
    unsafe_relative: str,
    operation: str,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    fixture = SafeRuntimeFixture(root)

    with pytest.raises(ValueError, match="unsafe test fixture path"):
        if operation == "directory":
            fixture.private_directory(unsafe_relative)
        else:
            fixture.private_file(unsafe_relative, b"value")

    assert list(tmp_path.iterdir()) == [root]
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("operation", ["directory", "file"])
def test_safe_runtime_fixture_rejects_absolute_paths_before_writing(
    tmp_path: Path,
    operation: str,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    outside = tmp_path / "outside"
    fixture = SafeRuntimeFixture(root)

    with pytest.raises(ValueError, match="unsafe test fixture path"):
        if operation == "directory":
            fixture.private_directory(outside)
        else:
            fixture.private_file(outside, b"value")

    assert not outside.exists()
    assert list(root.iterdir()) == []


@pytest.mark.parametrize(
    ("argv", "cwd"),
    [
        pytest.param(("env",), Path("/"), id="relative-executable"),
        pytest.param(("/usr/bin/env",), Path("relative"), id="relative-cwd"),
    ],
)
def test_command_runner_rejects_relative_execution_boundaries(
    argv: tuple[str, ...],
    cwd: Path,
) -> None:
    spec = CommandSpec(
        argv=argv,
        cwd=cwd,
        environment={"PATH": "/usr/bin:/bin"},
        timeout_seconds=5,
        stdout_limit=4096,
        stderr_limit=4096,
        safe_label="invalid command",
    )

    with pytest.raises(DeploymentError) as caught:
        CommandRunner().run(spec)

    assert caught.value.code == "CMMS-E003"


def test_command_runner_uses_absolute_cwd_and_does_not_interpret_shell_text(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "shell-created"
    literal = f"; touch {marker}"
    spec = CommandSpec(
        argv=("/usr/bin/printf", "%s", literal),
        cwd=tmp_path,
        environment={"PATH": "/usr/bin:/bin"},
        timeout_seconds=5,
        stdout_limit=4096,
        stderr_limit=4096,
        safe_label="literal argv probe",
    )

    result = CommandRunner().run(spec)

    assert result.stdout == literal
    assert not marker.exists()

    cwd_result = CommandRunner().run(
        CommandSpec(
            argv=("/usr/bin/pwd",),
            cwd=tmp_path,
            environment={"PATH": "/usr/bin:/bin"},
            timeout_seconds=5,
            stdout_limit=4096,
            stderr_limit=4096,
            safe_label="cwd probe",
        )
    )
    assert cwd_result.stdout.strip() == str(tmp_path)


def test_command_runner_drops_inherited_control_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOCKER_HOST", "tcp://unsafe.invalid:2375")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid")
    spec = CommandSpec(
        argv=("/usr/bin/env",),
        cwd=tmp_path,
        environment={"PATH": "/usr/bin:/bin"},
        timeout_seconds=5,
        stdout_limit=4096,
        stderr_limit=4096,
        safe_label="environment probe",
    )

    result = CommandRunner().run(spec)

    assert result.returncode == 0
    assert "DOCKER_HOST=" not in result.stdout
    assert "HTTPS_PROXY=" not in result.stdout


def test_command_runner_uses_only_the_explicit_minimal_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inherited = {
        "DOCKER_CONTEXT": "unsafe",
        "COMPOSE_FILE": "unsafe.yml",
        "HTTP_PROXY": "http://proxy.invalid",
        "https_proxy": "http://proxy.invalid",
        "AWS_SECRET_ACCESS_KEY": SENTINEL_SECRET,
        "CREDENTIALS_DIRECTORY": "/unsafe",
        "HOME": "/unsafe-home",
        "XDG_CONFIG_HOME": "/unsafe-xdg",
    }
    for key, value in inherited.items():
        monkeypatch.setenv(key, value)
    spec = CommandSpec(
        argv=("/usr/bin/env",),
        cwd=tmp_path,
        environment={"PATH": "/usr/bin:/bin"},
        timeout_seconds=5,
        stdout_limit=4096,
        stderr_limit=4096,
        safe_label="minimal environment probe",
    )

    result = CommandRunner().run(spec)

    assert result.stdout.splitlines() == ["PATH=/usr/bin:/bin"]
    assert SENTINEL_SECRET not in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_command_runner_kills_the_process_group_when_output_exceeds_a_cap(
    tmp_path: Path,
    stream: str,
) -> None:
    program = (
        "import sys; "
        f"stream=sys.{stream}; "
        "stream.buffer.write(b'x'*65); stream.flush()"
    )
    spec = CommandSpec(
        argv=(sys.executable, "-c", program),
        cwd=tmp_path,
        environment={"PATH": "/usr/bin:/bin"},
        timeout_seconds=5,
        stdout_limit=64,
        stderr_limit=64,
        safe_label="bounded output probe",
    )

    with pytest.raises(DeploymentError) as caught:
        CommandRunner().run(spec)

    assert caught.value.code == "CMMS-E005"
    assert caught.value.exit_code == 5


def test_command_runner_kills_a_same_group_grandchild_holding_a_kernel_lock(
    tmp_path: Path,
) -> None:
    lock_file = tmp_path / "grandchild.lock"
    ready_file = tmp_path / "grandchild-ready.json"
    grandchild_program = (
        "import fcntl,json,os,sys,time; "
        "lock_file,ready_file,root_pid=sys.argv[1:]; "
        "lock_fd=os.open(lock_file,os.O_RDWR|os.O_CREAT,0o600); "
        "fcntl.flock(lock_fd,fcntl.LOCK_EX); "
        "payload=json.dumps({'root_pid':int(root_pid),'parent_pid':os.getppid(),"
        "'pid':os.getpid(),'pgid':os.getpgid(0)}).encode(); "
        "temporary=ready_file+'.tmp'; "
        "ready_fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); "
        "os.write(ready_fd,payload); os.fsync(ready_fd); os.close(ready_fd); "
        "os.replace(temporary,ready_file); time.sleep(10)"
    )
    child_program = (
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable,'-c',sys.argv[1],"
        "*sys.argv[2:]]).wait()"
    )
    root_program = (
        "import os,subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2],"
        "sys.argv[3],sys.argv[4],str(os.getpid())]); "
        "deadline=time.monotonic()+5; "
        "\nwhile not os.path.exists(sys.argv[4]):\n"
        "  assert time.monotonic()<deadline\n"
        "  time.sleep(0.005)\n"
        "sys.stdout.buffer.write(b'x'*65); sys.stdout.flush(); time.sleep(10)"
    )
    spec = CommandSpec(
        argv=(
            sys.executable,
            "-c",
            root_program,
            child_program,
            grandchild_program,
            str(lock_file),
            str(ready_file),
        ),
        cwd=tmp_path,
        environment={"PATH": "/usr/bin:/bin"},
        timeout_seconds=6,
        stdout_limit=64,
        stderr_limit=4096,
        safe_label="grandchild process group probe",
    )

    with pytest.raises(DeploymentError) as caught:
        CommandRunner().run(spec)

    assert caught.value.code == "CMMS-E005"
    identities = json.loads(ready_file.read_text(encoding="utf-8"))
    assert identities["root_pid"] == identities["pgid"]
    assert identities["parent_pid"] not in {
        identities["root_pid"],
        identities["pid"],
    }
    assert identities["pid"] != identities["root_pid"]

    lock_descriptor = os.open(lock_file, os.O_RDWR | os.O_NOFOLLOW)
    try:
        deadline = time.monotonic() + 1
        while True:
            try:
                fcntl.flock(
                    lock_descriptor,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
                break
            except BlockingIOError:
                assert time.monotonic() < deadline
                time.sleep(0.005)
    finally:
        os.close(lock_descriptor)


def test_command_runner_kills_the_process_group_at_the_deadline(
    tmp_path: Path,
) -> None:
    spec = CommandSpec(
        argv=("/usr/bin/sleep", "5"),
        cwd=tmp_path,
        environment={"PATH": "/usr/bin:/bin"},
        timeout_seconds=0.05,
        stdout_limit=64,
        stderr_limit=64,
        safe_label="deadline probe",
    )

    with pytest.raises(DeploymentError) as caught:
        CommandRunner().run(spec)

    assert caught.value.code == "CMMS-E005"
    assert caught.value.exit_code == 5


def test_command_runner_keeps_the_deadline_after_child_closes_both_pipes(
    tmp_path: Path,
) -> None:
    program = "import os,time; os.close(1); os.close(2); time.sleep(0.4); os._exit(0)"
    spec = CommandSpec(
        argv=(sys.executable, "-c", program),
        cwd=tmp_path,
        environment={"PATH": "/usr/bin:/bin"},
        timeout_seconds=0.05,
        stdout_limit=64,
        stderr_limit=64,
        safe_label="closed pipe deadline probe",
    )

    started = time.monotonic()
    with pytest.raises(DeploymentError) as caught:
        CommandRunner().run(spec)
    elapsed = time.monotonic() - started

    assert caught.value.code == "CMMS-E005"
    assert elapsed < 0.3


def test_command_runner_cleans_up_after_operational_selector_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    real_popen = secure_process.subprocess.Popen
    real_selector_factory = secure_process.selectors.DefaultSelector
    spawned: list[secure_process.subprocess.Popen[bytes]] = []

    def recording_popen(*args: object, **kwargs: object) -> object:
        child = real_popen(*args, **kwargs)
        spawned.append(child)
        return child

    class FailingSelector:
        def __init__(self) -> None:
            self._selector = real_selector_factory()

        def register(self, *args: object, **kwargs: object) -> object:
            return self._selector.register(*args, **kwargs)

        def unregister(self, *args: object, **kwargs: object) -> object:
            return self._selector.unregister(*args, **kwargs)

        def get_map(self) -> object:
            return self._selector.get_map()

        def select(self, _timeout: float | None = None) -> object:
            raise OSError(f"{SENTINEL_SECRET} /unsafe/selector")

        def close(self) -> None:
            self._selector.close()

    monkeypatch.setattr(secure_process.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(
        secure_process.selectors,
        "DefaultSelector",
        FailingSelector,
    )
    spec = CommandSpec(
        argv=(sys.executable, "-c", "import time; time.sleep(10)"),
        cwd=tmp_path,
        environment={"PATH": "/usr/bin:/bin"},
        timeout_seconds=5,
        stdout_limit=64,
        stderr_limit=64,
        safe_label="selector failure probe",
    )

    try:
        with pytest.raises(DeploymentError) as caught:
            CommandRunner().run(spec)

        assert caught.value.code == "CMMS-E005"
        assert SENTINEL_SECRET not in str(caught.value)
        assert "/unsafe/selector" not in str(caught.value)
        assert SENTINEL_SECRET not in caplog.text
        assert len(spawned) == 1
        child = spawned[0]
        assert child.poll() is not None
        assert all(
            stream is None or stream.closed
            for stream in (child.stdin, child.stdout, child.stderr)
        )
    finally:
        for child in spawned:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)


def test_failed_command_does_not_expose_output_or_secret_diagnostics(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    program = (
        "import sys; "
        f"print({SENTINEL_SECRET!r}); "
        f"print({SENTINEL_SECRET!r}, file=sys.stderr); "
        "raise SystemExit(9)"
    )
    spec = CommandSpec(
        argv=(sys.executable, "-c", program),
        cwd=tmp_path,
        environment={"PATH": "/usr/bin:/bin", "CMMS_CREDENTIAL": SENTINEL_SECRET},
        timeout_seconds=5,
        stdout_limit=4096,
        stderr_limit=4096,
        safe_label="credential verification",
    )

    with pytest.raises(DeploymentError) as caught:
        CommandRunner().run(spec)

    assert caught.value.code == "CMMS-E004"
    assert SENTINEL_SECRET not in str(caught.value)
    assert SENTINEL_SECRET not in repr(caught.value)
    assert SENTINEL_SECRET not in caplog.text
    assert SENTINEL_SECRET not in repr(spec)


def test_command_runner_writes_bounded_input_without_inheriting_stdin(
    tmp_path: Path,
) -> None:
    spec = CommandSpec(
        argv=(sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"),
        cwd=tmp_path,
        environment={"PATH": "/usr/bin:/bin"},
        timeout_seconds=5,
        stdout_limit=64,
        stderr_limit=64,
        safe_label="input probe",
        input_bytes=b"bounded-input",
    )

    result = CommandRunner().run(spec)

    assert result.stdout == "bounded-input"
    assert result.stderr == ""


def test_cli_unknown_option_returns_stable_unavailable_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["--unknown-control-option"])

    captured = capsys.readouterr()
    assert exit_code == 20
    assert captured.out == ""
    assert captured.err == "CMMS-E020 command-not-available\n"


@pytest.mark.parametrize("command", ["status", "secret", "plan", "apply", "internal"])
def test_task2_public_commands_remain_stably_unavailable(
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    exit_code = main([command])

    captured = capsys.readouterr()
    assert exit_code == 20
    assert captured.out == ""
    assert captured.err == "CMMS-E020 command-not-available\n"


def test_internal_capabilities_and_gateway_authority_are_not_root_exports() -> None:
    forbidden = (
        "ConfirmedDeploymentPlan",
        "PlanAttemptReservation",
        "DeploymentWriteLease",
        "FailClosedEvidence",
        "ClaimedApplyContext",
        "GatewayListenerChallenge",
        "GatewayListenerAbsentProof",
        "_create_gateway_fail_closed_authority_parts",
    )
    assert all(not hasattr(ifactory_cmms_deploy, name) for name in forbidden)


def test_cmms_wrapper_forwards_to_frozen_uv_and_keeps_commands_unavailable() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    wrapper = repository_root / "scripts" / "cmms-development.sh"

    completed = subprocess.run(
        (str(wrapper), "status"),
        cwd=repository_root,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
        text=True,
    )

    assert completed.returncode == 20
    assert completed.stdout == ""
    assert completed.stderr == "CMMS-E020 command-not-available\n"
