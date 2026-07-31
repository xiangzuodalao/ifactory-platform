"""Offline behavior contracts for isolated PDM fixture lifecycle scripts."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
PREPARE = ROOT / "deploy" / "compose" / "scripts" / "prepare-pdm-fixtures.sh"
RESET = ROOT / "deploy" / "compose" / "scripts" / "reset-pdm-fixtures.sh"
VALIDATOR = ROOT / "deploy" / "compose" / "scripts" / "validate-pdm-fixtures.py"
CANONICAL_ARTIFACTS = (
    "pilot-cnc-vibration.json",
    "pilot-injection-pressure.json",
    "pilot-robot-position.json",
    "pilot-tightening-torque.json",
    "pilot-compressor-pressure.json",
    "pilot-eol-pass-rate.json",
)


def _copy_script(script: Path, destination: Path) -> Path:
    copied = destination / script.name
    shutil.copy2(script, copied)
    copied.chmod(0o755)
    return copied


def _fake_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").write_text(
        "#!/usr/bin/env bash\nset -Eeuo pipefail\nprintf '%s\\n' \"$FAKE_REPO_ROOT\"\n",
        encoding="utf-8",
    )
    (bin_dir / "uv").write_text(
        "#!/usr/bin/env bash\n"
        "set -Eeuo pipefail\n"
        'args="$*"\n'
        "if [[ $args == *'validate-pdm-fixtures.py'* ]]; then\n"
        '  test "${VALEO_PDM_ISOLATED_FIXTURE_MODE:-}" = 1\n'
        '  if [[ -n "${FAKE_UV_STDERR_SENTINEL:-}" ]]; then\n'
        '    printf "%s\\n" "$FAKE_UV_STDERR_SENTINEL" >&2\n'
        "  fi\n"
        "  printf 'isolated=%s args=%s\\n' "
        '"$VALEO_PDM_ISOLATED_FIXTURE_MODE" "$args" >>"$FAKE_UV_LOG"\n'
        "  while [[ $1 != python ]]; do shift; done\n"
        "  shift\n"
        '  script="$1"\n'
        "  shift\n"
        '  PYTHONPATH="$FAKE_PDM_STUB${PYTHONPATH:+:$PYTHONPATH}" '
        '"$FAKE_PYTHON" "$script" "$@"\n'
        "  exit $?\n"
        "fi\n"
        'out=""\n'
        "while (($#)); do\n"
        "  if [[ $1 == --output ]]; then out=$2; shift 2; continue; fi\n"
        "  shift\n"
        "done\n"
        'mkdir -p "$out/objects"\n'
        "printf 'fixture_mode: isolated-pilot\\nentries:\\n' "
        '> "$out/manifest.runtime.yaml"\n'
        "for name in "
        + " ".join(CANONICAL_ARTIFACTS)
        + "; do\n"
        + '  printf \'artifact:%s\' "$name" >"$out/objects/$name"\n'
        + "  digest=$(sha256sum \"$out/objects/$name\" | awk '{print $1}')\n"
        + "  printf '  - artifact_path: %s\\n    artifact_sha256: %s\\n' "
        + '"$name" "$digest" >> "$out/manifest.runtime.yaml"\n'
        + "done\n",
        encoding="utf-8",
    )
    (bin_dir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        "set -Eeuo pipefail\n"
        "mode=${FAKE_DOCKER_MODE:-none}\n"
        'args="$*"\n'
        'printf \'%s\\n\' "$args" >>"$FAKE_DOCKER_LOG"\n'
        "if [[ $mode == error ]]; then exit 17; fi\n"
        "if [[ $mode == active-project && "
        "$args == *'label=com.docker.compose.project=predictive-maintenance-shadow'* "
        "]]; then printf 'active-project\\n'; exit 0; fi\n"
        "if [[ $mode == active-bind && $args == 'ps -q' ]]; then "
        "printf 'active-bind\\n'; exit 0; fi\n"
        "if [[ $mode == active-bind && $args == inspect* ]]; then\n"
        "  if [[ $args == *'{{println .Source}}'* ]]; then\n"
        "    printf '%s\\n' \"$FAKE_REPO_ROOT/.runtime/pdm-fixtures\"\n"
        "  else\n"
        "    printf '%s\\\\n\\n' \"$FAKE_REPO_ROOT/.runtime/pdm-fixtures\"\n"
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        "if [[ $mode == inspect-error && $args == 'ps -q' ]]; then "
        "printf 'inspect-target\\n'; exit 0; fi\n"
        "if [[ $mode == inspect-error && $args == inspect* ]]; then exit 17; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    for item in bin_dir.iterdir():
        item.chmod(0o755)
    return bin_dir


def _run(
    script: Path,
    repo: Path,
    fake_bin: Path,
    *arguments: str,
    docker_mode: str = "none",
    validator_mode: str = "accept",
    validator_stderr_sentinel: str = "",
) -> subprocess.CompletedProcess[str]:
    environment = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_REPO_ROOT": str(repo),
        "FAKE_DOCKER_MODE": docker_mode,
        "FAKE_VALIDATOR_MODE": validator_mode,
        "FAKE_UV_STDERR_SENTINEL": validator_stderr_sentinel,
        "FAKE_UV_LOG": str(repo / ".fake-uv.log"),
        "FAKE_DOCKER_LOG": str(repo / ".fake-docker.log"),
        "FAKE_PDM_STUB": str(repo / ".fake-pdm"),
        "FAKE_PYTHON": sys.executable,
    }
    return subprocess.run(
        [str(script), *arguments],
        cwd=repo,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
    )


def _minimal_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitignore").write_text(".runtime/\n", encoding="utf-8")
    (repo / "components" / "pdm-algorithm" / "configs").mkdir(parents=True)
    (
        repo
        / "components"
        / "pdm-algorithm"
        / "configs"
        / "isolated_fixture_manifest.yaml"
    ).write_text(
        "fixture_mode: isolated-pilot\nentries:\n"
        + "".join(f"  - artifact_path: {name}\n" for name in CANONICAL_ARTIFACTS),
        encoding="utf-8",
    )
    if VALIDATOR.exists():
        validator = repo / "deploy" / "compose" / "scripts" / "validate-pdm-fixtures.py"
        validator.parent.mkdir(parents=True)
        shutil.copy2(VALIDATOR, validator)
    stub = repo / ".fake-pdm" / "valeo_pdm" / "prediction_v2"
    stub.mkdir(parents=True)
    (stub.parent / "__init__.py").write_text("", encoding="utf-8")
    (stub / "__init__.py").write_text("", encoding="utf-8")
    (stub / "catalog.py").write_text(
        "import os\n"
        "class ModelCatalog:\n"
        "    @classmethod\n"
        "    def from_manifest(cls, manifest_path, **kwargs):\n"
        "        if os.getenv('FAKE_VALIDATOR_MODE', 'accept') != 'accept':\n"
        "            raise ValueError('rejected')\n"
        "        return cls()\n",
        encoding="utf-8",
    )
    return repo


def _healthy_fixture_tree(repo: Path) -> Path:
    fixture = repo / ".runtime" / "pdm-fixtures"
    (fixture / "objects").mkdir(parents=True)
    entries: list[str] = []
    for name in CANONICAL_ARTIFACTS:
        content = f"artifact:{name}".encode()
        (fixture / "objects" / name).write_bytes(content)
        entries.append(
            f"  - artifact_path: {name}\n"
            f"    artifact_sha256: {hashlib.sha256(content).hexdigest()}\n"
        )
    (fixture / "manifest.runtime.yaml").write_text(
        "fixture_mode: isolated-pilot\nentries:\n" + "".join(entries),
        encoding="utf-8",
    )
    (fixture / "manifest.runtime.yaml").chmod(0o444)
    for name in CANONICAL_ARTIFACTS:
        (fixture / "objects" / name).chmod(0o444)
    (fixture / "objects").chmod(0o555)
    fixture.chmod(0o555)
    (repo / ".runtime").chmod(0o700)
    return fixture


def _load_validator_module(
    monkeypatch: pytest.MonkeyPatch,
    catalog: type,
):
    valeo_package = types.ModuleType("valeo_pdm")
    valeo_package.__path__ = []
    prediction_package = types.ModuleType("valeo_pdm.prediction_v2")
    prediction_package.__path__ = []
    catalog_module = types.ModuleType("valeo_pdm.prediction_v2.catalog")
    catalog_module.ModelCatalog = catalog
    monkeypatch.setitem(sys.modules, "valeo_pdm", valeo_package)
    monkeypatch.setitem(sys.modules, "valeo_pdm.prediction_v2", prediction_package)
    monkeypatch.setitem(
        sys.modules,
        "valeo_pdm.prediction_v2.catalog",
        catalog_module,
    )
    module_name = f"_fixture_validator_test_{id(catalog)}"
    spec = importlib.util.spec_from_file_location(module_name, VALIDATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_prepare_creates_an_atomic_read_only_fixture_tree(tmp_path: Path) -> None:
    """Removing safe staging or permission hardening must fail this lifecycle contract."""
    repo = _minimal_repo(tmp_path)
    script = _copy_script(PREPARE, tmp_path)
    result = _run(script, repo, _fake_bin(tmp_path))
    assert result.returncode == 0, result.stderr
    fixture = repo / ".runtime" / "pdm-fixtures"
    assert (
        (fixture / "manifest.runtime.yaml")
        .read_text(encoding="utf-8")
        .startswith("fixture_mode: isolated-pilot\nentries:\n")
    )
    assert {path.name for path in (fixture / "objects").iterdir()} == set(
        CANONICAL_ARTIFACTS
    )
    assert all(
        stat.S_IMODE((fixture / "objects" / name).stat().st_mode) == 0o444
        for name in CANONICAL_ARTIFACTS
    )
    assert stat.S_IMODE((fixture / "objects").stat().st_mode) == 0o555
    assert stat.S_IMODE((repo / ".runtime").stat().st_mode) == 0o700
    assert not list((repo / ".runtime").glob("pdm-fixtures.*"))


def test_prepare_succeeds_without_ripgrep(tmp_path: Path) -> None:
    """Fixture preparation must not depend on an undeclared host tool."""
    repo = _minimal_repo(tmp_path)
    script = _copy_script(PREPARE, tmp_path)
    fake_bin = _fake_bin(tmp_path)
    unavailable_rg = fake_bin / "rg"
    unavailable_rg.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'unexpected ripgrep dependency\\n' >&2\n"
        "exit 127\n",
        encoding="utf-8",
    )
    unavailable_rg.chmod(0o755)

    result = _run(script, repo, fake_bin)

    assert result.returncode == 0, result.stderr


def test_prepare_refuses_symlinked_runtime_and_existing_fixture_tree(
    tmp_path: Path,
) -> None:
    """Weakening canonical-target checks would allow the generator to overwrite another path."""
    repo = _minimal_repo(tmp_path)
    script = _copy_script(PREPARE, tmp_path)
    fake_bin = _fake_bin(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / ".runtime").symlink_to(outside, target_is_directory=True)
    result = _run(script, repo, fake_bin)
    assert result.returncode != 0
    assert not (outside / "pdm-fixtures").exists()
    (repo / ".runtime").unlink()
    (repo / ".runtime" / "pdm-fixtures").mkdir(parents=True)
    result = _run(script, repo, fake_bin)
    assert result.returncode != 0


def test_reset_moves_only_the_exact_fixture_target_and_requires_corrupt_confirmation(
    tmp_path: Path,
) -> None:
    """Replacing the recovery move with deletion or a broad path must fail this safety contract."""
    repo = _minimal_repo(tmp_path)
    script = _copy_script(RESET, tmp_path)
    fake_bin = _fake_bin(tmp_path)
    fixture = _healthy_fixture_tree(repo)
    result = _run(script, repo, fake_bin, "--confirm-path", ".runtime/pdm-fixtures")
    assert result.returncode == 0, result.stderr
    validator_call = (repo / ".fake-uv.log").read_text(encoding="utf-8")
    assert "--project components/pdm-algorithm --frozen python " in validator_call
    assert (
        str(repo / "deploy" / "compose" / "scripts" / "validate-pdm-fixtures.py")
        in validator_call
    )
    assert not fixture.exists()
    recycled = list((repo / ".runtime" / "recycle").iterdir())
    assert len(recycled) == 1
    assert {path.name for path in (recycled[0] / "objects").iterdir()} == set(
        CANONICAL_ARTIFACTS
    )

    fixture.mkdir(mode=0o555)
    result = _run(script, repo, fake_bin, "--confirm-path", ".runtime/pdm-fixtures")
    assert result.returncode != 0
    confirmed = _run(
        script,
        repo,
        fake_bin,
        "--confirm-path",
        ".runtime/pdm-fixtures",
        "--confirm-corrupt",
    )
    assert confirmed.returncode == 0, confirmed.stderr


def test_reset_exposes_the_bound_plan_before_reporting_a_successful_move(
    tmp_path: Path,
) -> None:
    """Suppressing helper stderr must not hide the exact pre-move recovery plan."""
    repo = _minimal_repo(tmp_path)
    script = _copy_script(RESET, tmp_path)
    fixture = _healthy_fixture_tree(repo)

    result = _run(
        script,
        repo,
        _fake_bin(tmp_path),
        "--confirm-path",
        ".runtime/pdm-fixtures",
        validator_stderr_sentinel="upstream-secret-sentinel",
    )

    assert result.returncode == 0, result.stderr
    assert "upstream-secret-sentinel" not in result.stdout + result.stderr
    recycled = list((repo / ".runtime" / "recycle").iterdir())
    assert len(recycled) == 1
    expected_lines = [
        "fixture manifest/hash status: healthy",
        f"fixture source: {fixture}",
        f"fixture destination: {recycled[0]}",
        "fixture move: pending",
        f"fixture moved to .runtime/recycle/{recycled[0].name}",
    ]
    positions = [result.stderr.index(line) for line in expected_lines]
    assert positions == sorted(positions)


@pytest.mark.parametrize(
    "corruption",
    (
        "missing",
        "extra",
        "partially-unreadable",
        "symlink",
        "nonregular",
        "hash-mismatch",
    ),
)
def test_reset_requires_exact_six_readable_regular_canonical_objects(
    tmp_path: Path, corruption: str
) -> None:
    """Weak object validation must never move a fixture without corrupt confirmation."""
    repo = _minimal_repo(tmp_path)
    script = _copy_script(RESET, tmp_path)
    fake_bin = _fake_bin(tmp_path)
    fixture = _healthy_fixture_tree(repo)
    (fixture / "objects").chmod(0o755)
    first = fixture / "objects" / CANONICAL_ARTIFACTS[0]
    if corruption == "missing":
        first.unlink()
    elif corruption == "extra":
        (fixture / "objects" / "unexpected.json").write_text(
            "artifact", encoding="utf-8"
        )
    elif corruption == "partially-unreadable":
        first.chmod(0)
    elif corruption == "symlink":
        first.unlink()
        first.symlink_to(fixture / "objects" / CANONICAL_ARTIFACTS[1])
    elif corruption == "nonregular":
        first.unlink()
        first.mkdir()
    else:
        first.chmod(0o644)
        first.write_text("tampered", encoding="utf-8")
        first.chmod(0o444)
    (fixture / "objects").chmod(0o555)

    result = _run(
        script,
        repo,
        fake_bin,
        "--confirm-path",
        ".runtime/pdm-fixtures",
        validator_mode="reject" if corruption == "hash-mismatch" else "accept",
    )

    assert result.returncode != 0
    assert fixture.exists()
    if corruption == "hash-mismatch":
        assert "validate-pdm-fixtures.py" in (repo / ".fake-uv.log").read_text(
            encoding="utf-8"
        )


@pytest.mark.parametrize(
    "corruption",
    ("not_entries", "duplicate-yaml-key", "invalid-entry-schema"),
)
def test_reset_delegates_manifest_semantics_to_the_pdm_catalog(
    tmp_path: Path, corruption: str
) -> None:
    """Line-oriented extraction must not replace the runtime catalog's YAML semantics."""
    repo = _minimal_repo(tmp_path)
    script = _copy_script(RESET, tmp_path)
    fixture = _healthy_fixture_tree(repo)
    manifest = fixture / "manifest.runtime.yaml"
    manifest.chmod(0o644)
    raw = manifest.read_text(encoding="utf-8")
    if corruption == "not_entries":
        raw = raw.replace("entries:\n", "not_entries:\n", 1)
    elif corruption == "duplicate-yaml-key":
        raw = raw.replace("entries:\n", "entries:\nentries:\n", 1)
    else:
        raw = raw.replace("artifact_sha256:", "artifact_sha257:", 1)
    manifest.write_text(raw + "# secret-sentinel-must-not-leak\n", encoding="utf-8")
    manifest.chmod(0o444)

    result = _run(
        script,
        repo,
        _fake_bin(tmp_path),
        "--confirm-path",
        ".runtime/pdm-fixtures",
        validator_mode="reject",
    )

    assert result.returncode != 0
    assert fixture.exists()
    assert "secret-sentinel-must-not-leak" not in result.stdout
    assert "secret-sentinel-must-not-leak" not in result.stderr
    invocation = (repo / ".fake-uv.log").read_text(encoding="utf-8")
    assert "isolated=1" in invocation
    assert "validate-pdm-fixtures.py" in invocation
    assert f"{repo} 0" in invocation


def test_reset_rejects_a_fifo_without_hashing_or_blocking(tmp_path: Path) -> None:
    """Once an object is nonregular, reset must never pass it to a content reader."""
    repo = _minimal_repo(tmp_path)
    script = _copy_script(RESET, tmp_path)
    fixture = _healthy_fixture_tree(repo)
    (fixture / "objects").chmod(0o755)
    fifo = fixture / "objects" / CANONICAL_ARTIFACTS[0]
    fifo.unlink()
    os.mkfifo(fifo)
    (fixture / "objects").chmod(0o555)

    result = _run(
        script,
        repo,
        _fake_bin(tmp_path),
        "--confirm-path",
        ".runtime/pdm-fixtures",
    )

    assert result.returncode != 0
    assert fixture.exists()
    assert fifo.is_fifo()
    assert "validate-pdm-fixtures.py" in (repo / ".fake-uv.log").read_text(
        encoding="utf-8"
    )


def test_fixture_validator_imports_and_calls_the_real_pdm_catalog() -> None:
    """The root adapter must delegate domain validation instead of copying model rules."""
    source = VALIDATOR.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports_catalog = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "valeo_pdm.prediction_v2.catalog"
        and any(alias.name == "ModelCatalog" for alias in node.names)
        for node in ast.walk(tree)
    )
    calls_catalog = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "from_manifest"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "ModelCatalog"
        for node in ast.walk(tree)
    )

    assert imports_catalog
    assert calls_catalog
    assert "artifact_sha256" not in source
    assert "model_profile_id" not in source


def test_fixture_recovery_catalog_reads_the_private_single_fd_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Duplicate parsing and PDM validation must consume the same immutable bytes."""
    repo = _minimal_repo(tmp_path)
    fixture = _healthy_fixture_tree(repo)
    manifest = fixture / "manifest.runtime.yaml"
    original = manifest.read_bytes()

    class Catalog:
        calls: list[tuple[Path, bytes, Path]] = []

        @classmethod
        def from_manifest(cls, manifest_path: Path, **_kwargs):
            cls.calls.append(
                (
                    manifest_path,
                    manifest_path.read_bytes(),
                    manifest_path.resolve(strict=True),
                )
            )
            fixture.chmod(0o755)
            manifest.unlink()
            manifest.write_bytes(
                b"fixture_mode: wrong\nfixture_mode: isolated-pilot\nentries: []\n"
            )
            manifest.chmod(0o444)
            fixture.chmod(0o555)
            return object()

    validator = _load_validator_module(monkeypatch, Catalog)

    result = validator.main([str(repo), "0"])

    assert result != 0
    assert fixture.exists()
    assert len(Catalog.calls) == 1
    catalog_path, catalog_bytes, resolved_catalog_path = Catalog.calls[0]
    assert catalog_path != manifest
    assert ".pdm-fixture-validation-" in str(resolved_catalog_path)
    assert catalog_bytes == original
    assert not list((repo / ".runtime").glob(".pdm-fixture-validation-*"))


def test_fixture_recovery_rejects_a_catalog_snapshot_inode_exchange(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catalog success must stay bound to the private bytes supplied by the helper."""
    repo = _minimal_repo(tmp_path)
    fixture = _healthy_fixture_tree(repo)

    class Catalog:
        @classmethod
        def from_manifest(cls, manifest_path: Path, **_kwargs):
            content = manifest_path.read_bytes()
            manifest_path.unlink()
            manifest_path.write_bytes(content)
            return object()

    validator = _load_validator_module(monkeypatch, Catalog)

    result = validator.main([str(repo), "0"])

    assert result != 0
    assert fixture.exists()
    assert not list((repo / ".runtime" / "recycle").iterdir())
    assert not list((repo / ".runtime").glob(".pdm-fixture-validation-*"))


def test_fixture_recovery_rejects_object_mutation_after_catalog_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The healthy decision must remain bound to the object inode moved to recycle."""
    repo = _minimal_repo(tmp_path)
    fixture = _healthy_fixture_tree(repo)
    target = fixture / "objects" / CANONICAL_ARTIFACTS[0]

    class Catalog:
        @classmethod
        def from_manifest(cls, _manifest_path: Path, **_kwargs):
            target.chmod(0o644)
            target.write_text("tampered-after-validation", encoding="utf-8")
            target.chmod(0o444)
            return object()

    validator = _load_validator_module(monkeypatch, Catalog)

    result = validator.main([str(repo), "0"])

    assert result != 0
    assert fixture.exists()
    assert not (repo / ".runtime" / "recycle").exists() or not list(
        (repo / ".runtime" / "recycle").iterdir()
    )


@pytest.mark.parametrize("target_kind", ("manifest", "object"))
def test_fixture_recovery_never_moves_an_externally_hardlinked_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_kind: str,
) -> None:
    """Corrupt confirmation must not preserve a mutable alias outside recycle."""
    repo = _minimal_repo(tmp_path)
    fixture = _healthy_fixture_tree(repo)
    target = (
        fixture / "manifest.runtime.yaml"
        if target_kind == "manifest"
        else fixture / "objects" / CANONICAL_ARTIFACTS[0]
    )
    outside_alias = tmp_path / f"{target_kind}-alias"
    os.link(target, outside_alias)

    class Catalog:
        calls = 0

        @classmethod
        def from_manifest(cls, _manifest_path: Path, **_kwargs):
            cls.calls += 1
            return object()

    validator = _load_validator_module(monkeypatch, Catalog)

    result = validator.main([str(repo), "1"])

    assert result != 0
    assert fixture.exists()
    assert target.stat().st_ino == outside_alias.stat().st_ino
    assert Catalog.calls == 0


def test_fixture_recovery_rejects_fixture_tree_exchange_before_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replacement at the validated fixture name must never be moved as healthy."""
    repo = _minimal_repo(tmp_path)
    fixture = _healthy_fixture_tree(repo)
    held_fixture = repo / ".runtime" / "pdm-fixtures-held"

    class Catalog:
        @classmethod
        def from_manifest(cls, _manifest_path: Path, **_kwargs):
            fixture.rename(held_fixture)
            fixture.mkdir(mode=0o555)
            return object()

    validator = _load_validator_module(monkeypatch, Catalog)

    result = validator.main([str(repo), "0"])

    assert result != 0
    assert fixture.exists()
    assert held_fixture.exists()
    assert not (repo / ".runtime" / "recycle").exists() or not list(
        (repo / ".runtime" / "recycle").iterdir()
    )


def test_fixture_recovery_rejects_runtime_parent_exchange_without_outside_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A renamed `.runtime` plus symlink replacement must not redirect recovery."""
    repo = _minimal_repo(tmp_path)
    _healthy_fixture_tree(repo)
    runtime = repo / ".runtime"
    held_runtime = repo / ".runtime-held"
    outside = tmp_path / "outside"
    outside.mkdir()

    class Catalog:
        @classmethod
        def from_manifest(cls, _manifest_path: Path, **_kwargs):
            runtime.rename(held_runtime)
            runtime.symlink_to(outside, target_is_directory=True)
            return object()

    validator = _load_validator_module(monkeypatch, Catalog)

    result = validator.main([str(repo), "0"])

    assert result != 0
    assert (held_runtime / "pdm-fixtures").exists()
    assert not (outside / "recycle").exists()


@pytest.mark.parametrize(
    "directory_kind",
    ("repo", "runtime", "fixture", "objects", "recycle"),
)
@pytest.mark.parametrize("confirm_corrupt", ("0", "1"))
def test_fixture_recovery_rejects_catalog_directory_mode_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    directory_kind: str,
    confirm_corrupt: str,
) -> None:
    """Neither catalog success nor corrupt confirmation may relax directory safety."""
    repo = _minimal_repo(tmp_path)
    fixture = _healthy_fixture_tree(repo)
    if directory_kind == "repo":
        repo.chmod(0o700)
    target = {
        "repo": repo,
        "runtime": repo / ".runtime",
        "fixture": fixture,
        "objects": fixture / "objects",
        "recycle": repo / ".runtime" / "recycle",
    }[directory_kind]

    class Catalog:
        calls = 0

        @classmethod
        def from_manifest(cls, _manifest_path: Path, **_kwargs):
            cls.calls += 1
            target.chmod(0o755)
            return object()

    validator = _load_validator_module(monkeypatch, Catalog)

    result = validator.main([str(repo), confirm_corrupt])

    assert result != 0
    assert fixture.exists()
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert Catalog.calls == 1
    recycle = repo / ".runtime" / "recycle"
    assert not recycle.exists() or not list(recycle.iterdir())


@pytest.mark.parametrize("target_kind", ("manifest", "object"))
def test_fixture_recovery_requires_confirmation_for_mode_zero_regular_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_kind: str,
) -> None:
    """An O_PATH-bound unreadable regular inode is corrupt but recoverable."""
    repo = _minimal_repo(tmp_path)
    fixture = _healthy_fixture_tree(repo)
    target = (
        fixture / "manifest.runtime.yaml"
        if target_kind == "manifest"
        else fixture / "objects" / CANONICAL_ARTIFACTS[0]
    )
    target.chmod(0)

    class Catalog:
        calls = 0

        @classmethod
        def from_manifest(cls, _manifest_path: Path, **_kwargs):
            cls.calls += 1
            return object()

    validator = _load_validator_module(monkeypatch, Catalog)

    assert validator.main([str(repo), "0"]) != 0
    assert fixture.exists()
    assert Catalog.calls == 0

    assert validator.main([str(repo), "1"]) == 0
    assert not fixture.exists()
    recycled = list((repo / ".runtime" / "recycle").iterdir())
    assert len(recycled) == 1
    moved_target = (
        recycled[0] / "manifest.runtime.yaml"
        if target_kind == "manifest"
        else recycled[0] / "objects" / CANONICAL_ARTIFACTS[0]
    )
    assert stat.S_IMODE(moved_target.stat().st_mode) == 0
    assert Catalog.calls == 0


def test_fixture_recovery_fails_closed_without_o_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The non-reading inode-binding primitive is a required platform capability."""
    repo = _minimal_repo(tmp_path)
    fixture = _healthy_fixture_tree(repo)

    class Catalog:
        calls = 0

        @classmethod
        def from_manifest(cls, _manifest_path: Path, **_kwargs):
            cls.calls += 1
            return object()

    validator = _load_validator_module(monkeypatch, Catalog)
    monkeypatch.delattr(validator.os, "O_PATH")

    result = validator.main([str(repo), "1"])

    assert result != 0
    assert fixture.exists()
    assert Catalog.calls == 0


def test_fixture_recovery_flushes_the_exact_plan_before_the_rename_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Moving before the bound health/source/destination output is flushed is unsafe UX."""
    repo = _minimal_repo(tmp_path)
    fixture = _healthy_fixture_tree(repo)

    class Catalog:
        @classmethod
        def from_manifest(cls, _manifest_path: Path, **_kwargs):
            return object()

    validator = _load_validator_module(monkeypatch, Catalog)
    destination_name = "pdm-fixtures-20260730T120000.000000Z-abcdef123456"
    monkeypatch.setattr(
        validator,
        "_recovery_destination",
        lambda: destination_name,
    )
    events: list[tuple[str, object]] = []

    class EventStream:
        def write(self, value: str) -> int:
            events.append(("write", value))
            return len(value)

        def flush(self) -> None:
            events.append(("flush", None))

    monkeypatch.setattr(validator.sys, "stderr", EventStream())
    real_rename = validator.os.rename

    def observed_rename(*args, **kwargs):
        events.append(("rename", args))
        assert fixture.exists()
        return real_rename(*args, **kwargs)

    monkeypatch.setattr(validator.os, "rename", observed_rename)

    destination = validator._recover(repo, False)

    rename_index = next(
        index for index, (kind, _) in enumerate(events) if kind == "rename"
    )
    before_rename = events[:rename_index]
    output = "".join(value for kind, value in before_rename if kind == "write")
    expected_destination = repo / ".runtime" / "recycle" / destination_name
    assert output == (
        "fixture manifest/hash status: healthy\n"
        f"fixture source: {fixture}\n"
        f"fixture destination: {expected_destination}\n"
        "fixture move: pending\n"
    )
    assert any(kind == "flush" for kind, _ in before_rename)
    assert destination == f".runtime/recycle/{destination_name}"


@pytest.mark.parametrize(
    "destination_name",
    (
        "../pdm-fixtures-escape",
        "/tmp/pdm-fixtures-escape",
        "pdm-fixtures/subdirectory",
        ".",
    ),
)
def test_fixture_recovery_rejects_an_escaping_destination_before_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    destination_name: str,
) -> None:
    """A generated destination that is not one canonical basename must never mutate."""
    repo = _minimal_repo(tmp_path)
    fixture = _healthy_fixture_tree(repo)

    class Catalog:
        @classmethod
        def from_manifest(cls, _manifest_path: Path, **_kwargs):
            return object()

    validator = _load_validator_module(monkeypatch, Catalog)
    monkeypatch.setattr(
        validator,
        "_recovery_destination",
        lambda: destination_name,
    )
    rename_calls = 0

    def reject_rename(*_args, **_kwargs):
        nonlocal rename_calls
        rename_calls += 1
        raise AssertionError("invalid destination reached rename")

    monkeypatch.setattr(validator.os, "rename", reject_rename)

    assert validator.main([str(repo), "0"]) != 0
    assert rename_calls == 0
    assert fixture.exists()


def test_fixture_recovery_moves_healthy_and_explicitly_confirmed_corrupt_trees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Confirmation may relax catalog health only after structural safety holds."""
    healthy_root = tmp_path / "healthy"
    healthy_root.mkdir()
    healthy_repo = _minimal_repo(healthy_root)
    healthy_fixture = _healthy_fixture_tree(healthy_repo)

    class HealthyCatalog:
        @classmethod
        def from_manifest(cls, _manifest_path: Path, **_kwargs):
            return object()

    healthy_validator = _load_validator_module(monkeypatch, HealthyCatalog)
    assert healthy_validator.main([str(healthy_repo), "0"]) == 0
    assert not healthy_fixture.exists()
    assert len(list((healthy_repo / ".runtime" / "recycle").iterdir())) == 1

    corrupt_root = tmp_path / "corrupt"
    corrupt_root.mkdir()
    corrupt_repo = _minimal_repo(corrupt_root)
    corrupt_fixture = _healthy_fixture_tree(corrupt_repo)

    class RejectingCatalog:
        @classmethod
        def from_manifest(cls, _manifest_path: Path, **_kwargs):
            raise ValueError("catalog rejected")

    corrupt_validator = _load_validator_module(monkeypatch, RejectingCatalog)
    assert corrupt_validator.main([str(corrupt_repo), "0"]) != 0
    assert corrupt_fixture.exists()
    assert corrupt_validator.main([str(corrupt_repo), "1"]) == 0
    assert not corrupt_fixture.exists()
    assert len(list((corrupt_repo / ".runtime" / "recycle").iterdir())) == 1


def test_fixture_recovery_never_reads_or_moves_a_fifo_even_when_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O_NONBLOCK plus regular-file checks must precede every content read."""
    repo = _minimal_repo(tmp_path)
    fixture = _healthy_fixture_tree(repo)
    objects = fixture / "objects"
    objects.chmod(0o755)
    fifo = objects / CANONICAL_ARTIFACTS[0]
    fifo.unlink()
    os.mkfifo(fifo)
    objects.chmod(0o555)

    class Catalog:
        calls = 0

        @classmethod
        def from_manifest(cls, _manifest_path: Path, **_kwargs):
            cls.calls += 1
            return object()

    validator = _load_validator_module(monkeypatch, Catalog)

    result = validator.main([str(repo), "1"])

    assert result != 0
    assert fixture.exists()
    assert fifo.is_fifo()
    assert Catalog.calls == 0


def test_reset_refuses_an_active_project_container_before_moving_the_fixture(
    tmp_path: Path,
) -> None:
    """Dropping the exact Compose project label check would permit an unsafe reset."""
    repo = _minimal_repo(tmp_path)
    script = _copy_script(RESET, tmp_path)
    fake_bin = _fake_bin(tmp_path)
    fixture = repo / ".runtime" / "pdm-fixtures"
    (fixture / "objects").mkdir(parents=True)
    (fixture / "manifest.runtime.yaml").write_text("fixture", encoding="utf-8")
    result = _run(
        script,
        repo,
        fake_bin,
        "--confirm-path",
        ".runtime/pdm-fixtures",
        docker_mode="active-project",
    )
    assert result.returncode != 0
    assert fixture.exists()


def test_reset_refuses_an_exact_active_bind_mount_before_moving_the_fixture(
    tmp_path: Path,
) -> None:
    """Removing active bind inspection would let a running container retain an old inode."""
    repo = _minimal_repo(tmp_path)
    script = _copy_script(RESET, tmp_path)
    fake_bin = _fake_bin(tmp_path)
    fixture = repo / ".runtime" / "pdm-fixtures"
    (fixture / "objects").mkdir(parents=True)
    (fixture / "manifest.runtime.yaml").write_text("fixture", encoding="utf-8")
    result = _run(
        script,
        repo,
        fake_bin,
        "--confirm-path",
        ".runtime/pdm-fixtures",
        docker_mode="active-bind",
    )
    assert result.returncode != 0
    assert fixture.exists()
    docker_calls = (repo / ".fake-docker.log").read_text(encoding="utf-8")
    assert "{{println .Source}}" in docker_calls


def test_reset_fails_closed_when_docker_queries_or_inspection_fail(
    tmp_path: Path,
) -> None:
    """Swallowing a Docker query failure would make fixture recovery unsafe."""
    repo = _minimal_repo(tmp_path)
    script = _copy_script(RESET, tmp_path)
    fake_bin = _fake_bin(tmp_path)
    fixture = repo / ".runtime" / "pdm-fixtures"
    (fixture / "objects").mkdir(parents=True)
    (fixture / "manifest.runtime.yaml").write_text("fixture", encoding="utf-8")
    result = _run(
        script,
        repo,
        fake_bin,
        "--confirm-path",
        ".runtime/pdm-fixtures",
        docker_mode="error",
    )
    assert result.returncode != 0
    assert fixture.exists()


def test_reset_fails_closed_when_docker_inspection_fails(tmp_path: Path) -> None:
    """Ignoring a failed mount inspection would make reset race a live container."""
    repo = _minimal_repo(tmp_path)
    script = _copy_script(RESET, tmp_path)
    fake_bin = _fake_bin(tmp_path)
    fixture = repo / ".runtime" / "pdm-fixtures"
    (fixture / "objects").mkdir(parents=True)
    (fixture / "manifest.runtime.yaml").write_text("fixture", encoding="utf-8")
    result = _run(
        script,
        repo,
        fake_bin,
        "--confirm-path",
        ".runtime/pdm-fixtures",
        docker_mode="inspect-error",
    )
    assert result.returncode != 0
    assert fixture.exists()
