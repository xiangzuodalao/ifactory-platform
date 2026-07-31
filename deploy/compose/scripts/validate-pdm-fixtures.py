"""Descriptor-bound validation and recovery for the isolated PDM fixture tree."""

from __future__ import annotations

import math
import os
import re
import secrets
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

from valeo_pdm.prediction_v2.catalog import ModelCatalog


TENANT_ID = "00000000-0000-4000-8000-000000000001"
CANONICAL_ARTIFACTS = (
    "pilot-cnc-vibration.json",
    "pilot-injection-pressure.json",
    "pilot-robot-position.json",
    "pilot-tightening-torque.json",
    "pilot-compressor-pressure.json",
    "pilot-eol-pass-rate.json",
)
_MAX_MANIFEST_BYTES = 512 * 1024
_MAX_OBJECT_BYTES = 1024 * 1024
_RECOVERY_OUTPUT_FD_ENV = "IFACTORY_PDM_FIXTURE_RECOVERY_OUTPUT_FD"
_RECOVERY_DESTINATION = re.compile(
    r"^pdm-fixtures-[0-9]{8}T[0-9]{6}\.[0-9]{6}Z-[0-9a-f]{12}$"
)


class _UnsafeFixture(RuntimeError):
    pass


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _mapping_without_duplicates(
    loader: _UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError("duplicate YAML key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _mapping_without_duplicates,
)


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _stable_metadata(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_nlink,
        metadata.st_uid,
    )


def _directory_flags() -> int:
    required = ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required):
        raise _UnsafeFixture
    if os.open not in os.supports_dir_fd:
        raise _UnsafeFixture
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _path_flags() -> int:
    required = ("O_CLOEXEC", "O_NOFOLLOW", "O_PATH")
    if any(not hasattr(os, name) for name in required):
        raise _UnsafeFixture
    if os.open not in os.supports_dir_fd:
        raise _UnsafeFixture
    return os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC


def _read_flags() -> int:
    required = ("O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK")
    if any(not hasattr(os, name) for name in required):
        raise _UnsafeFixture
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _open_directory(name: str, parent_fd: int) -> int:
    return os.open(name, _directory_flags(), dir_fd=parent_fd)


def _require_directory(fd: int, mode: int | None = None) -> None:
    metadata = os.fstat(fd)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or (mode is not None and stat.S_IMODE(metadata.st_mode) != mode)
    ):
        raise _UnsafeFixture


def _directory_metadata(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        metadata.st_uid,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _capture_directory(
    fd: int,
    *,
    mode: int | None,
) -> tuple[tuple[int, ...], frozenset[str]]:
    _require_directory(fd, mode)
    before = _directory_metadata(os.fstat(fd))
    entries = frozenset(os.listdir(fd))
    after = _directory_metadata(os.fstat(fd))
    if before != after:
        raise _UnsafeFixture
    return before, entries


def _verify_directory(
    fd: int,
    expected: tuple[tuple[int, ...], frozenset[str]],
    *,
    mode: int | None,
    allow_internal_churn: bool = False,
) -> None:
    actual = _capture_directory(fd, mode=mode)
    if allow_internal_churn:
        if actual[0][:6] != expected[0][:6] or actual[1] != expected[1]:
            raise _UnsafeFixture
    elif actual != expected:
        raise _UnsafeFixture


class _HeldFile:
    __slots__ = ("content", "fd", "metadata", "name", "read_fd")

    def __init__(
        self,
        *,
        name: str,
        fd: int,
        read_fd: int,
        metadata: tuple[int, ...],
        content: bytes | None,
    ) -> None:
        self.name = name
        self.fd = fd
        self.read_fd = read_fd
        self.metadata = metadata
        self.content = content

    def close(self) -> None:
        if self.read_fd >= 0 and self.read_fd != self.fd:
            os.close(self.read_fd)
        os.close(self.fd)


def _read_bounded_stable(fd: int, maximum: int) -> bytes:
    before = os.fstat(fd)
    if before.st_size > maximum:
        raise ValueError
    chunks: list[bytes] = []
    remaining = maximum + 1
    while remaining:
        chunk = os.read(fd, min(64 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    content = b"".join(chunks)
    after = os.fstat(fd)
    if (
        len(content) > maximum
        or len(content) != before.st_size
        or _stable_metadata(before) != _stable_metadata(after)
    ):
        raise ValueError
    return content


def _hold_regular_file(
    parent_fd: int,
    name: str,
    *,
    maximum: int,
) -> tuple[_HeldFile, bool]:
    fd = os.open(name, _path_flags(), dir_fd=parent_fd)
    read_fd = -1
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.getuid()
        ):
            raise _UnsafeFixture
        healthy = (
            stat.S_IMODE(metadata.st_mode) == 0o444 and metadata.st_size <= maximum
        )
        content: bytes | None = None
        if healthy:
            try:
                read_fd = os.open(name, _read_flags(), dir_fd=parent_fd)
            except PermissionError:
                healthy = False
            except OSError:
                raise _UnsafeFixture from None
            if healthy:
                if _stable_metadata(os.fstat(read_fd)) != _stable_metadata(metadata):
                    raise _UnsafeFixture
                content = _read_bounded_stable(read_fd, maximum)
                if _stable_metadata(os.fstat(fd)) != _stable_metadata(
                    metadata
                ) or _stable_metadata(os.fstat(read_fd)) != _stable_metadata(metadata):
                    raise _UnsafeFixture
        return (
            _HeldFile(
                name=name,
                fd=fd,
                read_fd=read_fd,
                metadata=_stable_metadata(metadata),
                content=content,
            ),
            healthy,
        )
    except BaseException:
        if read_fd >= 0:
            os.close(read_fd)
        os.close(fd)
        raise


def _reject_nonfinite(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_nonfinite(key)
            _reject_nonfinite(item)
    elif isinstance(value, list):
        for item in value:
            _reject_nonfinite(item)


def _strict_manifest(content: bytes) -> tuple[str, ...]:
    parsed = yaml.load(content.decode("utf-8"), Loader=_UniqueKeyLoader)
    _reject_nonfinite(parsed)
    if (
        type(parsed) is not dict
        or set(parsed) != {"fixture_mode", "entries"}
        or parsed.get("fixture_mode") != "isolated-pilot"
        or type(parsed.get("entries")) is not list
        or len(parsed["entries"]) != len(CANONICAL_ARTIFACTS)
    ):
        raise ValueError
    names: list[str] = []
    for entry in parsed["entries"]:
        if type(entry) is not dict:
            raise ValueError
        name = entry.get("artifact_path")
        if (
            type(name) is not str
            or name not in CANONICAL_ARTIFACTS
            or Path(name).name != name
        ):
            raise ValueError
        names.append(name)
    if len(set(names)) != len(names) or set(names) != set(CANONICAL_ARTIFACTS):
        raise ValueError
    return tuple(names)


def _write_snapshot_file(parent_fd: int, name: str, content: bytes) -> _HeldFile:
    fd = os.open(
        name,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o400,
        dir_fd=parent_fd,
    )
    try:
        offset = 0
        while offset < len(content):
            written = os.write(fd, content[offset:])
            if written <= 0:
                raise OSError
            offset += written
        os.fsync(fd)
        os.fchmod(fd, 0o400)
        return _HeldFile(
            name=name,
            fd=fd,
            read_fd=fd,
            metadata=_stable_metadata(os.fstat(fd)),
            content=content,
        )
    except BaseException:
        os.close(fd)
        raise


def _catalog_snapshot(
    runtime_fd: int,
    manifest: bytes,
    objects: dict[str, bytes],
    source_directories: dict[str, tuple[int, int | None]],
) -> bool:
    snapshot_name = f".pdm-fixture-validation-{secrets.token_hex(12)}"
    snapshot_fd = -1
    object_fd = -1
    snapshot_manifest: _HeldFile | None = None
    snapshot_objects: dict[str, _HeldFile] = {}
    try:
        os.mkdir(snapshot_name, mode=0o700, dir_fd=runtime_fd)
        snapshot_fd = _open_directory(snapshot_name, runtime_fd)
        snapshot_manifest = _write_snapshot_file(
            snapshot_fd,
            "manifest.runtime.yaml",
            manifest,
        )
        os.mkdir("objects", mode=0o700, dir_fd=snapshot_fd)
        object_fd = _open_directory("objects", snapshot_fd)
        for name, content in objects.items():
            snapshot_objects[name] = _write_snapshot_file(object_fd, name, content)
        os.fchmod(object_fd, 0o500)
        source_signatures = {
            name: _capture_directory(fd, mode=mode)
            for name, (fd, mode) in source_directories.items()
        }
        os.environ["VALEO_PDM_ISOLATED_FIXTURE_MODE"] = "1"
        snapshot_root = Path(f"/proc/self/fd/{snapshot_fd}")
        catalog_valid = True
        try:
            ModelCatalog.from_manifest(
                snapshot_root / "manifest.runtime.yaml",
                object_root=snapshot_root / "objects",
                allowed_tenant_ids={TENANT_ID},
            )
        except Exception:
            catalog_valid = False
        for name, (fd, mode) in source_directories.items():
            _verify_directory(
                fd,
                source_signatures[name],
                mode=mode,
            )
        return (
            catalog_valid
            and _verify_file(snapshot_fd, snapshot_manifest)
            and all(_verify_file(object_fd, held) for held in snapshot_objects.values())
        )
    except _UnsafeFixture:
        raise
    except Exception:
        return False
    finally:
        if snapshot_manifest is not None:
            snapshot_manifest.close()
        for held in snapshot_objects.values():
            held.close()
        if object_fd >= 0:
            try:
                os.fchmod(object_fd, 0o700)
            except OSError:
                pass
            for name in objects:
                try:
                    os.unlink(name, dir_fd=object_fd)
                except OSError:
                    pass
            os.close(object_fd)
        if snapshot_fd >= 0:
            try:
                os.rmdir("objects", dir_fd=snapshot_fd)
            except OSError:
                pass
            try:
                os.unlink("manifest.runtime.yaml", dir_fd=snapshot_fd)
            except OSError:
                pass
            os.close(snapshot_fd)
        try:
            os.rmdir(snapshot_name, dir_fd=runtime_fd)
        except OSError:
            pass


def _linked_directory(parent_fd: int, name: str, held_fd: int) -> bool:
    linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    held = os.fstat(held_fd)
    return (
        stat.S_ISDIR(linked.st_mode)
        and stat.S_ISDIR(held.st_mode)
        and _identity(linked) == _identity(held)
    )


def _verify_file(parent_fd: int, held: _HeldFile) -> bool:
    linked = os.stat(held.name, dir_fd=parent_fd, follow_symlinks=False)
    current = os.fstat(held.fd)
    if (
        not stat.S_ISREG(linked.st_mode)
        or _identity(linked) != _identity(current)
        or _stable_metadata(linked) != held.metadata
        or _stable_metadata(current) != held.metadata
    ):
        return False
    if held.content is not None:
        if (
            held.read_fd < 0
            or _stable_metadata(os.fstat(held.read_fd)) != held.metadata
            or os.pread(held.read_fd, len(held.content) + 1, 0) != held.content
        ):
            return False
    return True


def _open_absolute_directory(
    path: Path,
) -> tuple[list[int], list[tuple[int, str, int]]]:
    absolute = Path(os.path.abspath(path))
    fds = [os.open("/", _directory_flags())]
    links: list[tuple[int, str, int]] = []
    try:
        current = fds[0]
        for component in absolute.parts[1:]:
            child = _open_directory(component, current)
            fds.append(child)
            links.append((current, component, child))
            current = child
        return fds, links
    except BaseException:
        while fds:
            os.close(fds.pop())
        raise


def _verify_links(links: list[tuple[int, str, int]]) -> bool:
    return all(_linked_directory(parent, name, child) for parent, name, child in links)


def _recovery_destination() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"pdm-fixtures-{timestamp}-{secrets.token_hex(6)}"


def _validated_recovery_destination() -> str:
    destination = _recovery_destination()
    if _RECOVERY_DESTINATION.fullmatch(destination) is None:
        raise _UnsafeFixture
    return destination


def _recovery_output_stream():
    raw_descriptor = os.environ.get(_RECOVERY_OUTPUT_FD_ENV)
    if raw_descriptor is None:
        return sys.stderr, False
    if raw_descriptor != "3":
        raise _UnsafeFixture
    try:
        descriptor = os.dup(3)
    except OSError:
        raise _UnsafeFixture from None
    return os.fdopen(descriptor, "w", encoding="utf-8"), True


def _verify_move_transition(
    before: dict[str, tuple[tuple[int, ...], frozenset[str]]],
    after: dict[str, tuple[tuple[int, ...], frozenset[str]]],
    destination: str,
) -> None:
    if after["repo"] != before["repo"]:
        raise _UnsafeFixture
    if "objects" in before and after["objects"] != before["objects"]:
        raise _UnsafeFixture

    fixture_before, fixture_entries = before["fixture"]
    fixture_after, moved_entries = after["fixture"]
    if (
        fixture_after[:8] != fixture_before[:8]
        or fixture_after[8] < fixture_before[8]
        or moved_entries != fixture_entries
    ):
        raise _UnsafeFixture

    runtime_before, runtime_entries = before["runtime"]
    runtime_after, remaining_entries = after["runtime"]
    if (
        runtime_after[:5] != runtime_before[:5]
        or runtime_after[5] != runtime_before[5] - 1
        or runtime_after[7] < runtime_before[7]
        or runtime_after[8] < runtime_before[8]
        or remaining_entries != runtime_entries - {"pdm-fixtures"}
    ):
        raise _UnsafeFixture

    recycle_before, recycle_entries = before["recycle"]
    recycle_after, recovered_entries = after["recycle"]
    if (
        recycle_after[:5] != recycle_before[:5]
        or recycle_after[5] != recycle_before[5] + 1
        or recycle_after[7] < recycle_before[7]
        or recycle_after[8] < recycle_before[8]
        or recovered_entries != recycle_entries | {destination}
    ):
        raise _UnsafeFixture


def _recover(repo_path: Path, confirm_corrupt: bool) -> str:
    _path_flags()
    _read_flags()
    root_fds, root_links = _open_absolute_directory(repo_path)
    repo_fd = root_fds[-1]
    runtime_fd = fixture_fd = objects_fd = recycle_fd = -1
    manifest: _HeldFile | None = None
    object_files: dict[str, _HeldFile] = {}
    try:
        runtime_fd = _open_directory(".runtime", repo_fd)
        fixture_fd = _open_directory("pdm-fixtures", runtime_fd)
        _require_directory(repo_fd)
        _require_directory(runtime_fd, 0o700)
        _require_directory(fixture_fd, 0o555)
        original_fixture_mode = stat.S_IMODE(os.fstat(fixture_fd).st_mode)
        fixture_entries = set(os.listdir(fixture_fd))
        healthy = fixture_entries == {
            "manifest.runtime.yaml",
            "objects",
        }

        try:
            os.mkdir("recycle", mode=0o700, dir_fd=runtime_fd)
        except FileExistsError:
            pass
        recycle_fd = _open_directory("recycle", runtime_fd)
        _require_directory(recycle_fd, 0o700)

        if "manifest.runtime.yaml" in fixture_entries:
            manifest, manifest_healthy = _hold_regular_file(
                fixture_fd,
                "manifest.runtime.yaml",
                maximum=_MAX_MANIFEST_BYTES,
            )
            healthy = healthy and manifest_healthy
        else:
            healthy = False

        if "objects" in fixture_entries:
            objects_fd = _open_directory("objects", fixture_fd)
            _require_directory(objects_fd, 0o555)
            object_entries = set(os.listdir(objects_fd))
            healthy = healthy and object_entries == set(CANONICAL_ARTIFACTS)
            for name in sorted(object_entries):
                if name not in CANONICAL_ARTIFACTS:
                    raise _UnsafeFixture
                held, file_healthy = _hold_regular_file(
                    objects_fd,
                    name,
                    maximum=_MAX_OBJECT_BYTES,
                )
                object_files[name] = held
                healthy = healthy and file_healthy
        else:
            object_entries = set()
            healthy = False

        if fixture_entries - {"manifest.runtime.yaml", "objects"}:
            raise _UnsafeFixture

        source_directories: dict[str, tuple[int, int | None]] = {
            "repo": (repo_fd, None),
            "runtime": (runtime_fd, 0o700),
            "fixture": (fixture_fd, 0o555),
            "recycle": (recycle_fd, 0o700),
        }
        if objects_fd >= 0:
            source_directories["objects"] = (objects_fd, 0o555)
        source_baseline = {
            name: _capture_directory(fd, mode=mode)
            for name, (fd, mode) in source_directories.items()
        }

        snapshot_attempted = False
        if healthy:
            try:
                assert manifest is not None and manifest.content is not None
                names = _strict_manifest(manifest.content)
                snapshot_objects = {name: object_files[name].content for name in names}
                if any(content is None for content in snapshot_objects.values()):
                    raise ValueError
                snapshot_attempted = True
                healthy = _catalog_snapshot(
                    runtime_fd,
                    manifest.content,
                    {
                        name: content
                        for name, content in snapshot_objects.items()
                        if content is not None
                    },
                    source_directories,
                )
            except (
                AssertionError,
                KeyError,
                UnicodeDecodeError,
                ValueError,
                yaml.YAMLError,
            ):
                healthy = False

        if not healthy and not confirm_corrupt:
            raise _UnsafeFixture

        for name, (fd, mode) in source_directories.items():
            _verify_directory(
                fd,
                source_baseline[name],
                mode=mode,
                allow_internal_churn=name == "runtime" and snapshot_attempted,
            )

        pre_move_directories = {
            name: _capture_directory(fd, mode=mode)
            for name, (fd, mode) in source_directories.items()
        }

        if (
            not _verify_links(root_links)
            or not _linked_directory(repo_fd, ".runtime", runtime_fd)
            or not _linked_directory(runtime_fd, "pdm-fixtures", fixture_fd)
            or not _linked_directory(runtime_fd, "recycle", recycle_fd)
            or set(os.listdir(fixture_fd)) != fixture_entries
            or (
                objects_fd >= 0
                and (
                    not _linked_directory(fixture_fd, "objects", objects_fd)
                    or set(os.listdir(objects_fd)) != object_entries
                )
            )
            or (manifest is not None and not _verify_file(fixture_fd, manifest))
            or any(not _verify_file(objects_fd, held) for held in object_files.values())
        ):
            raise _UnsafeFixture
        for name, (fd, mode) in source_directories.items():
            _verify_directory(
                fd,
                pre_move_directories[name],
                mode=mode,
            )

        destination = _validated_recovery_destination()
        try:
            os.stat(destination, dir_fd=recycle_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise _UnsafeFixture

        canonical_repo = Path(os.path.abspath(repo_path))
        output, close_output = _recovery_output_stream()
        try:
            output.write(
                "fixture manifest/hash status: "
                f"{'healthy' if healthy else 'corrupt-confirmed'}\n"
                f"fixture source: {canonical_repo / '.runtime' / 'pdm-fixtures'}\n"
                "fixture destination: "
                f"{canonical_repo / '.runtime' / 'recycle' / destination}\n"
                "fixture move: pending\n"
            )
            output.flush()
        finally:
            if close_output:
                output.close()
        os.fchmod(fixture_fd, original_fixture_mode | stat.S_IWUSR)
        try:
            os.rename(
                "pdm-fixtures",
                destination,
                src_dir_fd=runtime_fd,
                dst_dir_fd=recycle_fd,
            )
        except BaseException:
            os.fchmod(fixture_fd, original_fixture_mode)
            raise
        os.fchmod(fixture_fd, original_fixture_mode)
        try:
            destination_metadata = os.stat(
                destination,
                dir_fd=recycle_fd,
                follow_symlinks=False,
            )
            try:
                os.stat(
                    "pdm-fixtures",
                    dir_fd=runtime_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                source_absent = True
            else:
                source_absent = False
            if (
                not source_absent
                or _identity(destination_metadata) != _identity(os.fstat(fixture_fd))
                or not _verify_links(root_links)
                or not _linked_directory(repo_fd, ".runtime", runtime_fd)
                or not _linked_directory(runtime_fd, "recycle", recycle_fd)
                or not _linked_directory(recycle_fd, destination, fixture_fd)
                or (
                    objects_fd >= 0
                    and not _linked_directory(fixture_fd, "objects", objects_fd)
                )
                or (manifest is not None and not _verify_file(fixture_fd, manifest))
                or any(
                    not _verify_file(objects_fd, held) for held in object_files.values()
                )
            ):
                raise _UnsafeFixture
            post_move_directories = {
                name: _capture_directory(fd, mode=mode)
                for name, (fd, mode) in source_directories.items()
            }
            _verify_move_transition(
                pre_move_directories,
                post_move_directories,
                destination,
            )
        except BaseException:
            try:
                destination_metadata = os.stat(
                    destination,
                    dir_fd=recycle_fd,
                    follow_symlinks=False,
                )
                try:
                    os.stat(
                        "pdm-fixtures",
                        dir_fd=runtime_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    if _identity(destination_metadata) == _identity(
                        os.fstat(fixture_fd)
                    ):
                        os.fchmod(
                            fixture_fd,
                            original_fixture_mode | stat.S_IWUSR,
                        )
                        try:
                            os.rename(
                                destination,
                                "pdm-fixtures",
                                src_dir_fd=recycle_fd,
                                dst_dir_fd=runtime_fd,
                            )
                        finally:
                            os.fchmod(fixture_fd, original_fixture_mode)
            except OSError:
                pass
            raise
        return f".runtime/recycle/{destination}"
    finally:
        for held in object_files.values():
            held.close()
        if manifest is not None:
            manifest.close()
        for fd in (objects_fd, recycle_fd, fixture_fd, runtime_fd):
            if fd >= 0:
                os.close(fd)
        while root_fds:
            os.close(root_fds.pop())


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if (
        len(arguments) != 2
        or arguments[1] not in {"0", "1"}
        or not Path(arguments[0]).is_absolute()
    ):
        return 2
    try:
        destination = _recover(Path(arguments[0]), arguments[1] == "1")
    except Exception:
        return 1
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
