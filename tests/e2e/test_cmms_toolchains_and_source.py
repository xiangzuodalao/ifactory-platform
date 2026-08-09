"""Offline tests for pinned CMMS host toolchains (Task 3, Slice A)."""

from __future__ import annotations

import copy
import errno
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from e2e.support.cmms_deployment import SafeRuntimeFixture
from ifactory_cmms_deploy.errors import DeploymentError
from ifactory_cmms_deploy.process import CommandResult, CommandRunner, CommandSpec
from ifactory_cmms_deploy.records import (
    ACTION_RANK,
    ActionCode,
    BuildArtifact,
    DependencyReceipt,
    DeploymentPlan,
    InstalledToolchain,
    LicenseMode,
    Operation,
    PlanApplicationState,
    PlannedAction,
    RuntimeProfile,
    SourceBinding,
    ToolchainReceipt,
    canonical_json_bytes,
    claim_plan_application,
    load_confirmed_plan,
    reserve_plan_attempt,
    write_plan,
)
from ifactory_cmms_deploy.source import (
    SensitiveBaselineResult,
    SensitiveManifest,
    SourceInspector,
    build_api_artifact,
    install_frontend_dependencies,
    verify_sensitive_baseline,
)
from ifactory_cmms_deploy.toolchains import (
    BoundedDownloadResult,
    BoundedDownloadSpec,
    BoundedToolchainRunner,
    ToolchainInstaller,
    ToolchainManifest,
    checked_archive_target,
)
import ifactory_cmms_deploy.toolchains as toolchains_module
import ifactory_cmms_deploy.source as source_module


ROOT = Path(__file__).resolve().parents[2]
TOOLCHAIN_MANIFEST = ROOT / "deploy/cmms/manifests/toolchains.json"
MAVEN_SETTINGS = ROOT / "deploy/cmms/maven-settings.xml"
SENSITIVE_MANIFEST = ROOT / "deploy/cmms/manifests/startup-sensitive-files.json"
ROOT_PROTECTED_GENERATED_DIRECTORIES = (
    "deploy/cmms/.venv",
    "deploy/cmms/.pytest_cache",
    "deploy/cmms/.mypy_cache",
    "deploy/cmms/.ruff_cache",
    "deploy/cmms/src/ifactory_cmms_deploy/__pycache__",
    "tests/.venv",
    "tests/.pytest_cache",
    "tests/.mypy_cache",
    "tests/.ruff_cache",
    "tests/__pycache__",
    "tests/contract/__pycache__",
    "tests/contract/phase1/__pycache__",
    "tests/e2e/__pycache__",
    "tests/e2e/support/__pycache__",
)

EXPECTED_TOOLS = {
    "temurin": {
        "version": "17.0.19+10",
        "url": (
            "https://github.com/adoptium/temurin17-binaries/releases/download/"
            "jdk-17.0.19%2B10/"
            "OpenJDK17U-jdk_x64_linux_hotspot_17.0.19_10.tar.gz"
        ),
        "sha256": "400fc5b6d000c158d5ee7937543faa06b6bda8408caa2444a9c947c21472fde0",
        "archive_kind": "tar.gz",
        "top_level_directory": "jdk-17.0.19+10",
        "strip_components": 1,
        "max_bytes": 256 * 1024 * 1024,
        "probes": (("bin/java", ("-version",), 'openjdk version "17.0.19"'),),
    },
    "maven": {
        "version": "3.9.3",
        "url": (
            "https://archive.apache.org/dist/maven/maven-3/3.9.3/"
            "binaries/apache-maven-3.9.3-bin.tar.gz"
        ),
        "sha256": "e1e13ac0c42f3b64d900c57ffc652ecef682b8255d7d354efbbb4f62519da4f1",
        "archive_kind": "tar.gz",
        "top_level_directory": "apache-maven-3.9.3",
        "strip_components": 1,
        "max_bytes": 32 * 1024 * 1024,
        "probes": (("bin/mvn", ("--version",), "Apache Maven 3.9.3"),),
    },
    "node": {
        "version": "21.6.1",
        "url": (
            "https://nodejs.org/dist/v21.6.1/"
            "node-v21.6.1-linux-x64.tar.xz"
        ),
        "sha256": "c65cbf7342260df8e59dd2fe2e06dc1f36ac46c9d433a64cd84521fd4915c291",
        "archive_kind": "tar.xz",
        "top_level_directory": "node-v21.6.1-linux-x64",
        "strip_components": 1,
        "max_bytes": 64 * 1024 * 1024,
        "probes": (
            ("bin/node", ("--version",), "v21.6.1"),
            ("bin/npm", ("--version",), "10.2.4"),
        ),
    },
}

EXPECTED_SENSITIVE_MANIFEST_SHA256 = (
    "04e09bc701da35b361f41d73c8815d9"
    "771bd1f54aeae11b2fbd226c441b9458c"
)
EXPECTED_SENSITIVE_FILES = {
    "api/Dockerfile": "5050d82e0f4ac79c2fd121b411017d315afa15ebdfe3ad14bff659293cc54146",
    "api/pom.xml": "2c4922c1a567fa97935890c30b271c0a0097ad247f7976ab3623f4c6e741f7ca",
    "api/src/main/java/com/grash/ApplicationInitializer.java": "1da45ceb2b360601dcd707c3e8e36fc1e385ecc9d9bfc30a34a002e3ed6e0279",
    "api/src/main/java/com/grash/advancedsearch/SearchCriteria.java": "8ede079994781c58604420e52233b730c37eda5f07724e5ae0e4f58f9b85d357",
    "api/src/main/java/com/grash/configuration/WebSecurityConfig.java": "cd43cd577c7d41dbea2ba2a62a5d0c8fa67544d77ee30ef25b8abf8453d33fec",
    "api/src/main/java/com/grash/controller/ApiKeyController.java": "376c622f7546ad9d629d61ae0f0ef943f2a75de23f19e23cdf73eb57732eea66",
    "api/src/main/java/com/grash/controller/AssetController.java": "bcabf88bce0c081a0ae020401bb2bade1bc9bfd9d55046342774fe3037e1ab22",
    "api/src/main/java/com/grash/controller/AuthController.java": "f51824a7fce40209c0f6fca215b3ed20d63b7866685399ad01ee0271ae76858e",
    "api/src/main/java/com/grash/controller/CompanyController.java": "faa8ccf1ab316162ac7f784fab7713a06ff11bc87251e7890993761b61c969f3",
    "api/src/main/java/com/grash/controller/LicenseController.java": "8bd6e0c661615f8d4af41fc6225b9757a69b6ae88f09b7ce2abead004f559eab",
    "api/src/main/java/com/grash/controller/RoleController.java": "b8e04e05a507a5a342e631e48de3d9c3debb27a5738f2407acd41b8707939080",
    "api/src/main/java/com/grash/controller/UserController.java": "e08202378a5d9b5df6f7f33c081844bd2472c9338e3cfa8d2c53fc7f27869716",
    "api/src/main/java/com/grash/controller/WorkOrderController.java": "9bec78ca28c19adbd133828fd7abb499ba6d06d6fa71de6a36814ee3ec998372",
    "api/src/main/java/com/grash/dto/CompanyShowDTO.java": "af733cf19c5c8e319a244b4d2e6ce6dac434713854d987b5797c6f2f36c4110e",
    "api/src/main/java/com/grash/dto/RolePatchDTO.java": "04a367d6d4b6222f164f898444a8fe264186bd5b24909b254d6b96a9c6a440d9",
    "api/src/main/java/com/grash/dto/UpdatePasswordRequest.java": "a8dec36ff6573a86a93926b96feba89630f3c2e10837e327dfe694b9bf78070e",
    "api/src/main/java/com/grash/dto/UserInvitationDTO.java": "abd3377431cc5b353e949b8ef5a3fca601dbe8c05882460fe1baecff7e2885dd",
    "api/src/main/java/com/grash/dto/UserResponseDTO.java": "6b373999ee908b302609c034ccfd6e12ee9d00816113803d20b4871b537af2e7",
    "api/src/main/java/com/grash/dto/UserSignupRequest.java": "2dc6acba56edbd94e85c31659397a495d218d53105a19030ef39e41e767fe352",
    "api/src/main/java/com/grash/dto/apiKey/ApiKeyPostDTO.java": "ba2b686409c323452076a59d31f22c0ac5308182e08c3f8ee52450c6ca0d6ad0",
    "api/src/main/java/com/grash/dto/apiKey/ApiKeyShowDTO.java": "c08052f66e34cc68fbda2ef1aa7f8067bf6a4866d41b3ef919bda4bd3e2cd213",
    "api/src/main/java/com/grash/dto/license/LicensingState.java": "08c4c0ae5654aeff8c93fe4e179a3c0b1d424334f7be7c216f0d1666a7ea699e",
    "api/src/main/java/com/grash/dto/workOrder/WorkOrderPostDTO.java": "8ceedd0868a685285439fa0eda81b43b8c02530c80810075030ce461d033afb1",
    "api/src/main/java/com/grash/dto/workOrder/WorkOrderShowDTO.java": "325b80b00063253af7285b55e5eadf30405418ebc0d0299416c713296f77e44f",
    "api/src/main/java/com/grash/mapper/ApiKeyMapper.java": "630cd292f808ce3a2d438c2e907ec29ec23514401b27641daca6005e8be4681d",
    "api/src/main/java/com/grash/mapper/WorkOrderMapper.java": "643d791a886c1e31b1ae8a55ad139fb43c93d38e27e92f0e72a3116e42232120",
    "api/src/main/java/com/grash/model/Role.java": "31b3f8c657bb262eb17b3e62f436a3ede84887ff244dccae5fc9fd1daea59d3a",
    "api/src/main/java/com/grash/model/WorkOrder.java": "c5288e1965f7132233002306fbb71766da45695c1d63ebad69795c2901d45274",
    "api/src/main/java/com/grash/model/enums/PermissionEntity.java": "17ac2e499dac9f79e9016d30e08dc2a733dedb5442500b8bed6841a11d781377",
    "api/src/main/java/com/grash/repository/UserInvitationRepository.java": "be52c0e0c30e84c3dea7e1d01265ccee6c4c0e25c402ef257f79240d0440d59d",
    "api/src/main/java/com/grash/repository/WorkOrderRepository.java": "b1a1eefc99f3f8ac441e015ea7130266b553cfa8b760af8d3357dbe0cc987463",
    "api/src/main/java/com/grash/security/ApiKeyAuthFilter.java": "ecad771d33deb5e2151984300eb66078445c249d35329faf9df4e5507389e9f7",
    "api/src/main/java/com/grash/service/ApiKeyService.java": "1f2209c72aaa90ea0530c3f31cb72779e8471d4a662d14b865d5e2db98a6aa04",
    "api/src/main/java/com/grash/service/AssetIntegrationService.java": "72cc60854dc7ff30e279dd687f2a652cd45605bbc86270c83318ebdd58c2df02",
    "api/src/main/java/com/grash/service/DemoDataService.java": "6ec9d61840edd929237f9cf604c71356ba4fcc8cbf0678a712406147d6b647ed",
    "api/src/main/java/com/grash/service/IntercomService.java": "9e564aff418a77e96104ea4c70cbefe80fa5c3230479aa4823afd90910558abf",
    "api/src/main/java/com/grash/service/LicenseService.java": "e3c4df47ed6439128b1caa6e29a33cbbb1c29a8e878bca736df1be1023d42c07",
    "api/src/main/java/com/grash/service/MinioService.java": "9753a44ccb36be08c8a6fc2170794fd3090f030cbd4a584f4e6bbbe395f59b07",
    "api/src/main/java/com/grash/service/RoleService.java": "2d81452647053d42bc9b8a2e22f3d77c815ce37f52e236897887172c6bcdef2d",
    "api/src/main/java/com/grash/service/UserService.java": "431b89ad6fbb035f93409abbc313150da30d27836db823c9685945ed5db697a6",
    "api/src/main/java/com/grash/service/WorkOrderIntegrationService.java": "51fddb4bb307d754d1ec5762ce7ec1c0dec424e01dbc8249889cd741938e5745",
    "api/src/main/resources/application.yml": "f052826d412400f43e7db04a90ef65c6f2d32f8fdf5f5cabc494909fb54a0bbb",
    "frontend/Dockerfile": "68f67c990a6dccfbb9ee77325d68d20b6f0aa9954fc6e7c65a27d3d868b2abe1",
    "frontend/package-lock.json": "484fdb709b700a50daaaf252f48cc9e11fb096401b52c8f4ef6d47de9100d46b",
    "frontend/package.json": "b52191055ea0ef64c0dae83ad50fc6923d32202678bd314e7dc6b38db4f1f669",
    "frontend/src/config.ts": "853c40596c8f9f01691dd0e8dc12e77be6d05577951abf7b3ea736578beabfca",
}


@pytest.fixture
def safe_runtime(tmp_path: Path) -> SafeRuntimeFixture:
    return SafeRuntimeFixture(tmp_path)


def _ordered_actions(*codes: ActionCode) -> tuple[PlannedAction, ...]:
    rank = {code: index for index, code in enumerate(ACTION_RANK)}
    return tuple(
        sorted((PlannedAction(code) for code in codes), key=lambda row: rank[row.code])
    )


def _repair_actions(*extra: ActionCode) -> tuple[PlannedAction, ...]:
    return _ordered_actions(
        ActionCode.GATEWAY_FAIL_CLOSED,
        *extra,
        ActionCode.LICENSE_VERIFY_OFFLINE,
        ActionCode.PROCESS_CREATE_API_PERMIT,
        ActionCode.PROCESS_START_API,
        ActionCode.PROCESS_START_FRONTEND,
        ActionCode.READINESS_REQUIRE_API_LOOPBACK,
        ActionCode.READINESS_REQUIRE_LOOPBACK,
        ActionCode.GATEWAY_ENABLE_DUAL,
        ActionCode.READINESS_REQUIRE_DUAL,
    )


@dataclass(frozen=True)
class ClaimedFixture:
    context: Any
    application_path: Path
    lease: Any


def _claim(
    safe_runtime: SafeRuntimeFixture,
    *,
    manifest_sha256: str,
    include_toolchain_action: bool = True,
    nonce: str = "d" * 32,
    application_id: str = "e" * 32,
) -> ClaimedFixture:
    snapshot = replace(
        safe_runtime.make_snapshot(),
        toolchain_manifest_sha256=manifest_sha256,
    )
    actions = _repair_actions(
        *(
            (ActionCode.TOOLCHAIN_INSTALL,)
            if include_toolchain_action
            else ()
        )
    )
    plan = DeploymentPlan.create(
        snapshot=snapshot,
        operation=Operation.REPAIR,
        profile=RuntimeProfile.DEVELOPMENT,
        license_mode=LicenseMode.OFFLINE,
        bootstrap_bindings=safe_runtime.bootstrap_plan_bindings,
        actions=actions,
        now=safe_runtime.make_plan().created_at,
        plan_nonce=nonce,
    )
    plan_path, plan_hash = write_plan(plan, safe_runtime.plans_dir)
    reservation = reserve_plan_attempt(
        plan_path,
        confirmed_sha256=plan_hash,
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
    evidence = safe_runtime.gateway_authority(confirmed, lease).issue()
    context = claim_plan_application(
        confirmed,
        safe_runtime.plans_dir,
        lease,
        evidence,
    )
    return ClaimedFixture(context, reservation.application_path, lease)


def test_toolchain_manifest_pins_node_archive_and_hash() -> None:
    manifest = ToolchainManifest.load(TOOLCHAIN_MANIFEST)
    node = manifest.require("node")
    assert node.version == "21.6.1"
    assert node.url == (
        "https://nodejs.org/dist/v21.6.1/"
        "node-v21.6.1-linux-x64.tar.xz"
    )
    assert node.sha256 == (
        "c65cbf7342260df8e59dd2fe2e06dc1"
        "f36ac46c9d433a64cd84521fd4915c291"
    )


def test_toolchain_manifest_is_the_exact_linux_amd64_reviewed_set() -> None:
    manifest = ToolchainManifest.load(TOOLCHAIN_MANIFEST)
    assert manifest.schema_version == 1
    assert manifest.platform == "linux/amd64"
    assert tuple(tool.name for tool in manifest.tools) == (
        "temurin",
        "maven",
        "node",
    )
    assert manifest.sha256 == hashlib.sha256(TOOLCHAIN_MANIFEST.read_bytes()).hexdigest()
    for name, expected in EXPECTED_TOOLS.items():
        tool = manifest.require(name)
        assert {
            "version": tool.version,
            "url": tool.url,
            "sha256": tool.sha256,
            "archive_kind": tool.archive_kind,
            "top_level_directory": tool.top_level_directory,
            "strip_components": tool.strip_components,
            "max_bytes": tool.max_bytes,
            "probes": tuple(
                (probe.executable, probe.arguments, probe.contains)
                for probe in tool.probes
            ),
        } == expected


def _write_manifest(path: Path, value: dict[str, Any]) -> Path:
    path.write_bytes(canonical_json_bytes(value))
    return path


def test_manifest_loader_rejects_unknown_missing_duplicate_and_unsafe_rows(
    tmp_path: Path,
) -> None:
    raw = json.loads(TOOLCHAIN_MANIFEST.read_text(encoding="utf-8"))
    invalid: list[dict[str, Any]] = []
    for key in tuple(raw):
        candidate = copy.deepcopy(raw)
        del candidate[key]
        invalid.append(candidate)
    candidate = copy.deepcopy(raw)
    candidate["unknown"] = "value"
    invalid.append(candidate)
    candidate = copy.deepcopy(raw)
    candidate["platform"] = "linux/arm64"
    invalid.append(candidate)
    candidate = copy.deepcopy(raw)
    candidate["tools"].append(copy.deepcopy(candidate["tools"][0]))
    invalid.append(candidate)

    for row_index, row in enumerate(raw["tools"]):
        for key in tuple(row):
            candidate = copy.deepcopy(raw)
            del candidate["tools"][row_index][key]
            invalid.append(candidate)
        candidate = copy.deepcopy(raw)
        candidate["tools"][row_index]["unknown"] = "value"
        invalid.append(candidate)
        candidate = copy.deepcopy(raw)
        candidate["tools"][row_index]["url"] = "http://example.test/archive"
        invalid.append(candidate)
        candidate = copy.deepcopy(raw)
        candidate["tools"][row_index]["url"] = "https://example.test/latest/archive"
        invalid.append(candidate)
        candidate = copy.deepcopy(raw)
        candidate["tools"][row_index]["url"] = "https://example.test/archive?version=latest"
        invalid.append(candidate)
        candidate = copy.deepcopy(raw)
        candidate["tools"][row_index]["strip_components"] = True
        invalid.append(candidate)
        for probe_index, probe in enumerate(row["probes"]):
            for key in tuple(probe):
                candidate = copy.deepcopy(raw)
                del candidate["tools"][row_index]["probes"][probe_index][key]
                invalid.append(candidate)
            candidate = copy.deepcopy(raw)
            candidate["tools"][row_index]["probes"][probe_index]["unknown"] = "value"
            invalid.append(candidate)

    for index, candidate in enumerate(invalid):
        with pytest.raises(DeploymentError):
            ToolchainManifest.load(
                _write_manifest(tmp_path / f"invalid-{index}.json", candidate)
            )


def test_manifest_loader_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_bytes(
        b'{"platform":"linux/amd64","platform":"linux/amd64",'
        b'"schema_version":1,"tools":[]}\n'
    )
    with pytest.raises(DeploymentError):
        ToolchainManifest.load(path)


@pytest.mark.parametrize("field", ("version", "url", "sha256", "probe"))
def test_public_manifest_loader_freezes_every_reviewed_toolchain_row(
    tmp_path: Path,
    field: str,
) -> None:
    raw = json.loads(TOOLCHAIN_MANIFEST.read_text(encoding="utf-8"))
    row = raw["tools"][0]
    if field == "version":
        row["version"] = "17.0.20+1"
    elif field == "url":
        row["url"] = "https://fixtures.invalid/temurin.tar.gz"
    elif field == "sha256":
        row["sha256"] = "f" * 64
    else:
        row["probes"][0]["contains"] = 'openjdk version "17.0.20"'
    with pytest.raises(DeploymentError, match="CMMS-E030"):
        ToolchainManifest.load(
            _write_manifest(tmp_path / f"altered-{field}.json", raw)
        )


def test_public_manifest_constructor_cannot_mint_an_install_authority() -> None:
    manifest = ToolchainManifest.load(TOOLCHAIN_MANIFEST)
    with pytest.raises((DeploymentError, TypeError)):
        ToolchainManifest(
            schema_version=manifest.schema_version,
            platform=manifest.platform,
            tools=manifest.tools,
            sha256=manifest.sha256,
        )


def test_claimed_context_requires_exact_live_planned_action(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    claimed = _claim(safe_runtime, manifest_sha256="4" * 64)
    try:
        assert claimed.context.require_action(ActionCode.TOOLCHAIN_INSTALL) == PlannedAction(
            ActionCode.TOOLCHAIN_INSTALL
        )
        assert claimed.context.require_action(
            PlannedAction(ActionCode.TOOLCHAIN_INSTALL)
        ) == PlannedAction(ActionCode.TOOLCHAIN_INSTALL)
        with pytest.raises(DeploymentError):
            claimed.context.require_action(ActionCode.BUILD_API)
        with pytest.raises(DeploymentError):
            claimed.context.require_action("toolchain.install")

        safe_runtime.transition_application(
            claimed.application_path,
            claimed.context.application,
            PlanApplicationState.SUCCEEDED,
            claimed.context.application.claimed_at + timedelta(seconds=1),
        )
        with pytest.raises(DeploymentError):
            claimed.context.require_action(ActionCode.TOOLCHAIN_INSTALL)
    finally:
        claimed.lease.close()


def _regular_member(name: str, data: bytes, mode: int = 0o755) -> tuple[tarfile.TarInfo, bytes]:
    member = tarfile.TarInfo(name)
    member.size = len(data)
    member.mode = mode
    member.type = tarfile.REGTYPE
    return member, data


def _archive_bytes(
    archive_kind: str,
    top_level: str,
    executable_names: tuple[str, ...],
    *,
    extra_members: tuple[tuple[tarfile.TarInfo, bytes | None], ...] = (),
) -> bytes:
    buffer = io.BytesIO()
    mode = "w:gz" if archive_kind == "tar.gz" else "w:xz"
    with tarfile.open(fileobj=buffer, mode=mode) as archive:
        directory = tarfile.TarInfo(top_level)
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        archive.addfile(directory)
        bin_directory = tarfile.TarInfo(f"{top_level}/bin")
        bin_directory.type = tarfile.DIRTYPE
        bin_directory.mode = 0o755
        archive.addfile(bin_directory)
        for executable in executable_names:
            member, data = _regular_member(
                f"{top_level}/bin/{executable}",
                f"fixture-{executable}\n".encode(),
            )
            archive.addfile(member, io.BytesIO(data))
        for member, data in extra_members:
            archive.addfile(member, None if data is None else io.BytesIO(data))
    return buffer.getvalue()


def _fixture_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    temurin_members: tuple[tuple[tarfile.TarInfo, bytes | None], ...] = (),
    max_bytes: dict[str, int] | None = None,
    node_npm_symlink: bool = False,
) -> tuple[ToolchainManifest, dict[str, bytes]]:
    archives: dict[str, bytes] = {}
    tools: list[dict[str, Any]] = []
    executables = {
        "temurin": ("java",),
        "maven": ("mvn",),
        "node": ("node", "npm"),
    }
    for name in ("temurin", "maven", "node"):
        expected = EXPECTED_TOOLS[name]
        extras = temurin_members if name == "temurin" else ()
        selected_executables = executables[name]
        if name == "node" and node_npm_symlink:
            selected_executables = ("node",)
            npm = tarfile.TarInfo(
                f"{expected['top_level_directory']}/bin/npm"
            )
            npm.type = tarfile.SYMTYPE
            npm.linkname = "../lib/node_modules/npm/bin/npm-cli.js"
            npm_target = _regular_member(
                f"{expected['top_level_directory']}/lib/node_modules/npm/bin/npm-cli.js",
                b"fixture-npm\n",
            )
            extras = (npm_target, (npm, None))
        archive = _archive_bytes(
            str(expected["archive_kind"]),
            str(expected["top_level_directory"]),
            selected_executables,
            extra_members=extras,
        )
        url = f"https://fixtures.invalid/{name}.archive"
        archives[url] = archive
        tools.append(
            {
                "archive_kind": expected["archive_kind"],
                "max_bytes": (max_bytes or {}).get(name, int(expected["max_bytes"])),
                "name": name,
                "probes": [
                    {
                        "arguments": list(arguments),
                        "contains": contains,
                        "executable": executable,
                    }
                    for executable, arguments, contains in expected["probes"]
                ],
                "sha256": hashlib.sha256(archive).hexdigest(),
                "strip_components": 1,
                "top_level_directory": expected["top_level_directory"],
                "url": url,
                "version": expected["version"],
            }
        )
    path = _write_manifest(
        tmp_path / "toolchains-fixture.json",
        {"platform": "linux/amd64", "schema_version": 1, "tools": tools},
    )
    monkeypatch.setattr(
        toolchains_module,
        "_REVIEWED_MANIFEST_SHA256",
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    return ToolchainManifest.load(path), archives


def test_production_module_exposes_no_fixture_manifest_authority() -> None:
    assert not hasattr(toolchains_module, "_TEST_MANIFEST_AUTHORITY")
    assert not hasattr(toolchains_module, "_load_test_fixture_manifest")


def test_fixture_manifest_does_not_authorize_a_production_installer(
    safe_runtime: SafeRuntimeFixture,
    tmp_path: Path,
) -> None:
    with pytest.MonkeyPatch.context() as reviewed:
        manifest, archives = _fixture_manifest(tmp_path, reviewed)
        ToolchainInstaller(
            manifest,
            ToolchainRunner(archives),
            runtime_root=safe_runtime.root / ".runtime",
        )
    with pytest.raises(DeploymentError, match="CMMS-E032"):
        ToolchainInstaller(
            manifest,
            ToolchainRunner(archives),
            runtime_root=safe_runtime.root / ".runtime",
        )


@pytest.mark.parametrize("mode", (0o755, 0o777))
def test_plan_status_validates_runtime_root_before_reporting_missing(
    tmp_path: Path,
    mode: int,
) -> None:
    runtime_root = tmp_path / f"runtime-{mode:o}"
    runtime_root.mkdir(mode=mode)
    runtime_root.chmod(mode)
    installer = ToolchainInstaller(
        ToolchainManifest.load(TOOLCHAIN_MANIFEST),
        ToolchainRunner({}),
        runtime_root=runtime_root,
    )
    with pytest.raises(DeploymentError, match="CMMS-E032"):
        installer.plan_status(runtime_root)


def test_plan_status_rejects_a_dangling_toolchains_symlink(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(mode=0o700)
    (runtime_root / "toolchains").symlink_to(tmp_path / "missing-toolchains")
    installer = ToolchainInstaller(
        ToolchainManifest.load(TOOLCHAIN_MANIFEST),
        ToolchainRunner({}),
        runtime_root=runtime_root,
    )
    with pytest.raises(DeploymentError, match="CMMS-E032"):
        installer.plan_status(runtime_root)


@dataclass
class ToolchainRunner:
    archives: dict[str, bytes]
    probe_outputs: dict[str, str] | None = None
    probe_hook: Any = None
    redirect_hops: dict[str, tuple[str, ...]] | None = None
    header_overrides: dict[str, bytes] | None = None
    substitute_download_path: bool = False
    calls: list[Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.calls = []
        self.substituted_path: Path | None = None
        self.substitution_sentinel = b"replacement-target-must-not-be-written"

    def run(self, spec: Any) -> CommandResult:
        self.calls.append(spec)
        executable = Path(spec.argv[0]).name
        if self.probe_hook is not None:
            self.probe_hook(executable)
        default = {
            "java": 'openjdk version "17.0.19"',
            "mvn": "Apache Maven 3.9.3",
            "node": "v21.6.1",
            "npm": "10.2.4",
        }[executable]
        output = (self.probe_outputs or {}).get(executable, default)
        return CommandResult(
            0,
            output if executable != "java" else "",
            output if executable == "java" else "",
        )

    def download(self, spec: Any) -> BoundedDownloadResult:
        self.calls.append(spec)
        assert Path(spec.argv[0]).name == "curl"
        assert spec.argv[spec.argv.index("--output") + 1] == "/proc/self/fd/1"
        assert spec.argv[spec.argv.index("--dump-header") + 1] == "/proc/self/fd/2"
        url = spec.argv[-1]
        if self.substitute_download_path:
            tool_name = url.rsplit("/", 1)[-1].split(".", 1)[0]
            candidates = tuple(spec.cwd.glob(f".{tool_name}-*.download"))
            assert len(candidates) == 1
            replacement = candidates[0]
            replacement.unlink()
            replacement.write_bytes(self.substitution_sentinel)
            replacement.chmod(0o600)
            self.substituted_path = replacement
        payload = self.archives[url]
        assert len(payload) <= spec.body_limit_bytes
        offset = 0
        while offset < len(payload):
            written = os.pwrite(spec.destination_fd, payload[offset:], offset)
            assert written > 0
            offset += written
        os.fsync(spec.destination_fd)
        overridden_headers = (self.header_overrides or {}).get(url)
        if overridden_headers is not None:
            assert len(overridden_headers) <= spec.header_limit_bytes
            return BoundedDownloadResult(overridden_headers)
        headers = bytearray()
        for hop in (self.redirect_hops or {}).get(url, ()):
            headers.extend(
                f"HTTP/1.1 302 Found\r\nLocation: {hop}\r\n\r\n".encode()
            )
        headers.extend(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n\r\n"
        )
        assert len(headers) <= spec.header_limit_bytes
        return BoundedDownloadResult(bytes(headers))


def _curl_calls(runner: ToolchainRunner) -> list[Any]:
    return [call for call in runner.calls if Path(call.argv[0]).name == "curl"]


def _install(
    safe_runtime: SafeRuntimeFixture,
    manifest: ToolchainManifest,
    runner: ToolchainRunner,
) -> tuple[ClaimedFixture, ToolchainInstaller, Any]:
    claimed = _claim(safe_runtime, manifest_sha256=manifest.sha256)
    installer = ToolchainInstaller(
        manifest,
        runner,
        runtime_root=safe_runtime.root / ".runtime",
    )
    return claimed, installer, installer.install_all(claimed.context)


def _invalidate_toolchain_action(
    safe_runtime: SafeRuntimeFixture,
    claimed: ClaimedFixture,
) -> None:
    safe_runtime.transition_application(
        claimed.application_path,
        claimed.context.application,
        PlanApplicationState.SUCCEEDED,
        claimed.context.application.claimed_at + timedelta(seconds=1),
    )


@pytest.mark.parametrize(
    "phase",
    (
        "staging-directory",
        "toolchains-directory",
        "temporary-directory",
        "download",
        "extraction",
        "receipt",
        "temporary-probe",
        "publication",
        "final-reprobe",
    ),
)
def test_toolchain_install_action_remains_live_through_every_install_phase(
    safe_runtime: SafeRuntimeFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    manifest, archives = _fixture_manifest(tmp_path, monkeypatch)
    runner = ToolchainRunner(archives)
    claimed = _claim(safe_runtime, manifest_sha256=manifest.sha256)
    invalidated = False

    def invalidate_once() -> None:
        nonlocal invalidated
        if not invalidated:
            invalidated = True
            _invalidate_toolchain_action(safe_runtime, claimed)

    if phase in {"staging-directory", "toolchains-directory"}:
        original_ensure_child = toolchains_module._ensure_private_child
        selected_name = (
            "cmms-staging" if phase == "staging-directory" else "toolchains"
        )

        def invalidating_ensure_child(parent: Path, name: str) -> Path:
            result = original_ensure_child(parent, name)
            if name == selected_name:
                invalidate_once()
            return result

        monkeypatch.setattr(
            toolchains_module,
            "_ensure_private_child",
            invalidating_ensure_child,
        )
    elif phase == "temporary-directory":
        original_require_private = toolchains_module._require_private_directory

        def invalidating_require_private(path: Path) -> None:
            original_require_private(path)
            if path.name.startswith(".temurin-") and path.name.endswith(".tmp"):
                invalidate_once()

        monkeypatch.setattr(
            toolchains_module,
            "_require_private_directory",
            invalidating_require_private,
        )
    elif phase == "download":
        original_download = runner.download

        def invalidating_download(spec: Any) -> BoundedDownloadResult:
            result = original_download(spec)
            invalidate_once()
            return result

        runner.download = invalidating_download  # type: ignore[method-assign]
    elif phase == "extraction":
        original_extract = toolchains_module._extract_archive

        def invalidating_extract(*args: Any, **kwargs: Any) -> None:
            original_extract(*args, **kwargs)
            invalidate_once()

        monkeypatch.setattr(
            toolchains_module,
            "_extract_archive",
            invalidating_extract,
        )
    elif phase == "receipt":
        original_write_receipt = toolchains_module._write_install_receipt

        def invalidating_write_receipt(*args: Any, **kwargs: Any) -> None:
            original_write_receipt(*args, **kwargs)
            invalidate_once()

        monkeypatch.setattr(
            toolchains_module,
            "_write_install_receipt",
            invalidating_write_receipt,
        )
    elif phase == "temporary-probe":
        runner.probe_hook = lambda _executable: invalidate_once()
    elif phase == "publication":
        original_rename = toolchains_module._rename_exclusive

        def invalidating_rename(*args: Any, **kwargs: Any) -> None:
            original_rename(*args, **kwargs)
            invalidate_once()

        monkeypatch.setattr(
            toolchains_module,
            "_rename_exclusive",
            invalidating_rename,
        )
    else:
        original_run = runner.run

        def invalidating_final_probe(spec: Any) -> CommandResult:
            result = original_run(spec)
            if spec.cwd.name == f"temurin-{manifest.require('temurin').version}":
                invalidate_once()
            return result

        runner.run = invalidating_final_probe  # type: ignore[method-assign]

    try:
        with pytest.raises(DeploymentError):
            ToolchainInstaller(
                manifest,
                runner,
                runtime_root=safe_runtime.root / ".runtime",
            ).install_all(claimed.context)
        assert invalidated is True
    finally:
        claimed.lease.close()


def test_existing_toolchain_status_probe_is_guarded_by_the_live_install_action(
    safe_runtime: SafeRuntimeFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, archives = _fixture_manifest(tmp_path, monkeypatch)
    runner = ToolchainRunner(archives)
    claimed, installer, _receipt = _install(safe_runtime, manifest, runner)
    invalidated = False

    def invalidate_once(_executable: str) -> None:
        nonlocal invalidated
        if not invalidated:
            invalidated = True
            _invalidate_toolchain_action(safe_runtime, claimed)

    runner.probe_hook = invalidate_once
    runner.calls.clear()
    try:
        with pytest.raises(DeploymentError):
            installer.install_all(claimed.context)
        assert invalidated is True
        assert _curl_calls(runner) == []
    finally:
        claimed.lease.close()


class ToolchainRunnerDescriptor:
    def __init__(self, delegate: ToolchainRunner, marker: Path) -> None:
        self.delegate = delegate
        self.marker = marker
        self.run_lookups = 0
        self.download_lookups = 0

    @property
    def run(self) -> Any:
        self.run_lookups += 1
        self.marker.write_text("runner descriptor resolved\n", encoding="ascii")
        return self.delegate.run

    @property
    def download(self) -> Any:
        self.download_lookups += 1
        self.marker.write_text("runner descriptor resolved\n", encoding="ascii")
        return self.delegate.download


def test_toolchain_runner_descriptors_are_resolved_once_only_after_action_gate(
    safe_runtime: SafeRuntimeFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, archives = _fixture_manifest(tmp_path, monkeypatch)
    marker = tmp_path / "descriptor-side-effect"
    delegate = ToolchainRunner(archives)
    runner = ToolchainRunnerDescriptor(delegate, marker)
    installer = ToolchainInstaller(
        manifest,
        runner,
        runtime_root=safe_runtime.root / ".runtime",
    )
    assert not marker.exists()
    assert (runner.run_lookups, runner.download_lookups) == (0, 0)

    claimed = _claim(safe_runtime, manifest_sha256=manifest.sha256)
    try:
        receipt = installer.install_all(claimed.context)
        assert tuple(row.name for row in receipt.installations) == (
            "temurin",
            "maven",
            "node",
        )
        assert marker.read_text(encoding="ascii") == "runner descriptor resolved\n"
        assert (runner.run_lookups, runner.download_lookups) == (1, 1)
    finally:
        claimed.lease.close()


def test_toolchain_runner_descriptor_failure_is_post_gate_and_normalized(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    manifest = ToolchainManifest.load(TOOLCHAIN_MANIFEST)

    class ExplodingDescriptor:
        @property
        def run(self) -> Any:
            raise DeploymentError("FORGED", "/secret/runner-descriptor", 99)

        @property
        def download(self) -> Any:
            raise AssertionError("run descriptor must fail first")

    installer = ToolchainInstaller(
        manifest,
        ExplodingDescriptor(),
        runtime_root=safe_runtime.root / ".runtime",
    )
    claimed = _claim(safe_runtime, manifest_sha256=manifest.sha256)
    try:
        with pytest.raises(DeploymentError) as captured:
            installer.install_all(claimed.context)
        assert captured.value.code == "CMMS-E032"
        assert "/secret/runner-descriptor" not in str(captured.value)
    finally:
        claimed.lease.close()


@pytest.mark.parametrize(
    ("method", "exception"),
    (
        ("download", RuntimeError("ambient download failure")),
        (
            "download",
            DeploymentError("FORGED", "/secret/download-failure", 99),
        ),
        ("run", RuntimeError("ambient probe failure")),
    ),
)
def test_toolchain_runner_exceptions_are_normalized_to_installation_error(
    safe_runtime: SafeRuntimeFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    exception: Exception,
) -> None:
    manifest, archives = _fixture_manifest(tmp_path, monkeypatch)
    runner = ToolchainRunner(archives)

    def explode(_spec: Any) -> Any:
        raise exception

    setattr(runner, method, explode)
    claimed = _claim(safe_runtime, manifest_sha256=manifest.sha256)
    try:
        with pytest.raises(DeploymentError) as captured:
            ToolchainInstaller(
                manifest,
                runner,
                runtime_root=safe_runtime.root / ".runtime",
            ).install_all(claimed.context)
        assert captured.value.code == "CMMS-E032"
        assert "ambient" not in str(captured.value)
        assert "/secret" not in str(captured.value)
    finally:
        claimed.lease.close()


def test_installer_rejects_missing_claimed_action_before_any_effect(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    manifest = ToolchainManifest.load(TOOLCHAIN_MANIFEST)
    claimed = _claim(
        safe_runtime,
        manifest_sha256=manifest.sha256,
        include_toolchain_action=False,
    )
    runner = ToolchainRunner({})
    effects = safe_runtime.root / ".runtime" / "toolchains"
    try:
        with pytest.raises(DeploymentError):
            ToolchainInstaller(
                manifest,
                runner,
                runtime_root=safe_runtime.root / ".runtime",
            ).install_all(claimed.context)
        assert runner.calls == []
        assert not effects.exists()
    finally:
        claimed.lease.close()


def test_installer_uses_fixed_curl_checksums_probes_and_atomic_receipts(
    safe_runtime: SafeRuntimeFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    safe_link = tarfile.TarInfo(
        f"{EXPECTED_TOOLS['temurin']['top_level_directory']}/bin/java-link"
    )
    safe_link.type = tarfile.SYMTYPE
    safe_link.linkname = "java"
    safe_link.mode = 0o777
    safe_hardlink = tarfile.TarInfo(
        f"{EXPECTED_TOOLS['temurin']['top_level_directory']}/lib/fixture-hard"
    )
    safe_hardlink.type = tarfile.LNKTYPE
    safe_hardlink.linkname = (
        f"{EXPECTED_TOOLS['temurin']['top_level_directory']}/lib/fixture-target"
    )
    safe_hardlink_target = _regular_member(
        (
            f"{EXPECTED_TOOLS['temurin']['top_level_directory']}"
            "/lib/fixture-target"
        ),
        b"safe hardlink target\n",
        mode=0o644,
    )
    manifest, archives = _fixture_manifest(
        tmp_path,
        monkeypatch,
        temurin_members=(
            (safe_link, None),
            safe_hardlink_target,
            (safe_hardlink, None),
        ),
    )
    runner = ToolchainRunner(archives)
    claimed, installer, receipt = _install(safe_runtime, manifest, runner)
    try:
        assert tuple(row.name for row in receipt.installations) == (
            "temurin",
            "maven",
            "node",
        )
        assert receipt.manifest_sha256 == manifest.sha256
        for name in ("temurin", "maven", "node"):
            installed = receipt.require(name)
            assert installed.home == (
                safe_runtime.root
                / ".runtime/toolchains"
                / f"{name}-{EXPECTED_TOOLS[name]['version']}"
            )
            receipt_path = installed.home / ".ifactory-toolchain-receipt.json"
            metadata = receipt_path.lstat()
            assert stat.S_ISREG(metadata.st_mode)
            assert stat.S_IMODE(metadata.st_mode) == 0o600
            assert json.loads(receipt_path.read_text(encoding="utf-8")) == {
                "archive_sha256": manifest.require(name).sha256,
                "manifest_sha256": manifest.sha256,
                "name": name,
                "record_type": "cmms-toolchain-installation",
                "schema_version": 1,
                "version": EXPECTED_TOOLS[name]["version"],
            }

        curls = _curl_calls(runner)
        assert len(curls) == 3
        for call in curls:
            assert call.argv[:8] == (
                "/usr/bin/curl",
                "--disable",
                "--proto",
                "=https",
                "--proto-redir",
                "=https",
                "--tlsv1.2",
                "--location",
            )
            assert call.argv[call.argv.index("--output") + 1] == "/proc/self/fd/1"
            assert call.argv[call.argv.index("--dump-header") + 1] == "/proc/self/fd/2"
            assert str(safe_runtime.root / ".runtime/cmms-staging") not in "\n".join(
                call.argv
            )

        assert (receipt.require("temurin").home / "bin/java-link").is_symlink()
        assert os.stat(
            receipt.require("temurin").home / "lib/fixture-hard"
        ).st_ino == os.stat(
            receipt.require("temurin").home / "lib/fixture-target"
        ).st_ino

        runner.calls.clear()
        statuses = installer.plan_status(safe_runtime.root / ".runtime")
        assert all(status.installed and status.ready for status in statuses)
        reused = installer.install_all(claimed.context)
        assert reused == receipt
        assert _curl_calls(runner) == []
    finally:
        claimed.lease.close()


def test_curl_disable_is_first_and_blocks_ambient_curlrc(
    tmp_path: Path,
) -> None:
    curl_home = tmp_path / "curl-home"
    curl_home.mkdir(mode=0o700)
    (curl_home / ".curlrc").write_text(
        "definitely-not-a-real-curl-option\n",
        encoding="ascii",
    )
    environment = {
        "CURL_HOME": str(curl_home),
        "HOME": str(curl_home),
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }

    def version_spec(argv: tuple[str, ...], label: str) -> CommandSpec:
        return CommandSpec(
            argv=argv,
            cwd=tmp_path,
            environment=environment,
            timeout_seconds=20,
            stdout_limit=64 * 1024,
            stderr_limit=64 * 1024,
            safe_label=label,
        )

    result = CommandRunner().run(
        version_spec(
            ("/usr/bin/curl", "--disable", "--version"),
            "curl-disable-first-test",
        )
    )
    assert result.returncode == 0
    assert result.stdout.startswith("curl ")
    assert result.stderr == ""

    for argv, label in (
        (("/usr/bin/curl", "--version"), "curl-disable-missing-test"),
        (
            ("/usr/bin/curl", "--version", "--disable"),
            "curl-disable-late-test",
        ),
    ):
        ambient = CommandRunner().run(version_spec(argv, label))
        assert ambient.returncode == 0
        assert "definitely-not-a-real-curl-option" in ambient.stderr
        assert "unknown" in ambient.stderr


@pytest.mark.parametrize(
    ("kind", "name", "linkname"),
    (
        ("traversal", "jdk-17.0.19+10/../../escape", ""),
        ("absolute", "/absolute", ""),
        ("device", "jdk-17.0.19+10/device", ""),
        ("fifo", "jdk-17.0.19+10/fifo", ""),
        ("symlink", "jdk-17.0.19+10/bin/escape-link", "../../escape"),
        ("hardlink", "jdk-17.0.19+10/bin/escape-hard", "../../escape"),
        ("second-root", "unexpected-root/file", ""),
    ),
)
def test_archive_prescan_rejects_every_unsafe_member_before_extraction(
    safe_runtime: SafeRuntimeFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    name: str,
    linkname: str,
) -> None:
    member = tarfile.TarInfo(name)
    data: bytes | None = None
    if kind in {"traversal", "absolute", "second-root"}:
        member, data = _regular_member(name, b"unsafe")
    elif kind == "device":
        member.type = tarfile.CHRTYPE
    elif kind == "fifo":
        member.type = tarfile.FIFOTYPE
    elif kind == "symlink":
        member.type = tarfile.SYMTYPE
        member.linkname = linkname
    else:
        member.type = tarfile.LNKTYPE
        member.linkname = linkname
    manifest, archives = _fixture_manifest(
        tmp_path,
        monkeypatch,
        temurin_members=((member, data),),
    )
    runner = ToolchainRunner(archives)
    claimed = _claim(safe_runtime, manifest_sha256=manifest.sha256)
    target = (
        safe_runtime.root
        / ".runtime/toolchains"
        / f"temurin-{EXPECTED_TOOLS['temurin']['version']}"
    )
    try:
        with pytest.raises(DeploymentError, match="CMMS-E031"):
            ToolchainInstaller(
                manifest,
                runner,
                runtime_root=safe_runtime.root / ".runtime",
            ).install_all(claimed.context)
        assert not target.exists()
        assert not (safe_runtime.root / "escape").exists()
    finally:
        claimed.lease.close()


def test_archive_prescan_rejects_duplicate_stripped_target(
    safe_runtime: SafeRuntimeFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    top = str(EXPECTED_TOOLS["temurin"]["top_level_directory"])
    first = _regular_member(f"{top}/duplicate", b"first")
    second = _regular_member(f"{top}/duplicate", b"second")
    manifest, archives = _fixture_manifest(
        tmp_path,
        monkeypatch,
        temurin_members=(first, second),
    )
    runner = ToolchainRunner(archives)
    claimed = _claim(safe_runtime, manifest_sha256=manifest.sha256)
    try:
        with pytest.raises(DeploymentError, match="CMMS-E031"):
            ToolchainInstaller(
                manifest,
                runner,
                runtime_root=safe_runtime.root / ".runtime",
            ).install_all(claimed.context)
    finally:
        claimed.lease.close()


def test_archive_prescan_rejects_duplicate_top_level_directory(
    safe_runtime: SafeRuntimeFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duplicate_root = tarfile.TarInfo(
        str(EXPECTED_TOOLS["temurin"]["top_level_directory"])
    )
    duplicate_root.type = tarfile.DIRTYPE
    manifest, archives = _fixture_manifest(
        tmp_path,
        monkeypatch,
        temurin_members=((duplicate_root, None),),
    )
    runner = ToolchainRunner(archives)
    claimed = _claim(safe_runtime, manifest_sha256=manifest.sha256)
    try:
        with pytest.raises(DeploymentError, match="CMMS-E031"):
            ToolchainInstaller(
                manifest,
                runner,
                runtime_root=safe_runtime.root / ".runtime",
            ).install_all(claimed.context)
    finally:
        claimed.lease.close()


def test_checked_archive_target_rejects_absolute_and_parent_paths(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extract"
    root.mkdir()
    assert checked_archive_target(root, "bin/java") == root / "bin/java"
    for name in ("/etc/passwd", "../escape", "bin/../../escape"):
        with pytest.raises(DeploymentError, match="CMMS-E031"):
            checked_archive_target(root, name)


def test_checksum_and_redirect_fail_without_publishing(
    safe_runtime: SafeRuntimeFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, archives = _fixture_manifest(tmp_path, monkeypatch)
    temurin_url = manifest.require("temurin").url
    cases = (
        ({**archives, temurin_url: b"checksum-mismatch"}, ()),
        (archives, ("https://fixtures.invalid/latest/temurin.tar.gz",)),
        (archives, ("https://fixtures.invalid/temurin.tar.gz?version=latest",)),
    )
    for index, (payloads, redirect_hops) in enumerate(cases):
        case_runtime = SafeRuntimeFixture(tmp_path / f"case-{index}")
        runner = ToolchainRunner(
            payloads,
            redirect_hops=(
                {temurin_url: redirect_hops} if redirect_hops else None
            ),
        )
        claimed = _claim(
            case_runtime,
            manifest_sha256=manifest.sha256,
            nonce=f"{index + 1:032x}",
            application_id=f"{index + 11:032x}",
        )
        try:
            with pytest.raises(DeploymentError):
                ToolchainInstaller(
                    manifest,
                    runner,
                    runtime_root=case_runtime.root / ".runtime",
                ).install_all(claimed.context)
            assert not (
                case_runtime.root
                / ".runtime/toolchains"
                / f"temurin-{EXPECTED_TOOLS['temurin']['version']}"
            ).exists()
        finally:
            claimed.lease.close()


def test_every_redirect_hop_is_validated_not_only_the_effective_url(
    safe_runtime: SafeRuntimeFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, archives = _fixture_manifest(tmp_path, monkeypatch)
    url = manifest.require("temurin").url
    runner = ToolchainRunner(
        archives,
        redirect_hops={
            url: (
                "https://fixtures.invalid/latest/temurin.tar.gz",
                url,
            )
        },
    )
    claimed = _claim(safe_runtime, manifest_sha256=manifest.sha256)
    try:
        with pytest.raises(DeploymentError):
            ToolchainInstaller(
                manifest,
                runner,
                runtime_root=safe_runtime.root / ".runtime",
            ).install_all(claimed.context)
    finally:
        claimed.lease.close()


@pytest.mark.parametrize(
    "headers",
    (
        b"HTTP/1.1 302 Found\r\nLocation: https://fixtures.invalid/mirror\r\n\r\n",
        b"HTTP/1.1 404 Not Found\r\nContent-Type: text/plain\r\n\r\n",
    ),
)
def test_redirect_chain_requires_exactly_one_final_2xx_response(
    safe_runtime: SafeRuntimeFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    headers: bytes,
) -> None:
    manifest, archives = _fixture_manifest(tmp_path, monkeypatch)
    url = manifest.require("temurin").url
    runner = ToolchainRunner(archives, header_overrides={url: headers})
    claimed = _claim(safe_runtime, manifest_sha256=manifest.sha256)
    try:
        with pytest.raises(DeploymentError):
            ToolchainInstaller(
                manifest,
                runner,
                runtime_root=safe_runtime.root / ".runtime",
            ).install_all(claimed.context)
    finally:
        claimed.lease.close()


def test_curl_download_has_transfer_time_body_and_header_limits(
    safe_runtime: SafeRuntimeFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, archives = _fixture_manifest(tmp_path, monkeypatch)
    runner = ToolchainRunner(archives)
    claimed, _installer, _receipt = _install(safe_runtime, manifest, runner)
    try:
        for call in _curl_calls(runner):
            tool = manifest.require(call.argv[-1].split("/")[-1].split(".")[0])
            assert call.body_limit_bytes == tool.max_bytes
            assert call.header_limit_bytes == 64 * 1024
    finally:
        claimed.lease.close()


def test_installer_rejects_a_runner_without_hard_cap_download_capability(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    manifest = ToolchainManifest.load(TOOLCHAIN_MANIFEST)

    class RunOnly:
        def run(self, spec: Any) -> CommandResult:
            raise AssertionError("must not run")

    with pytest.raises(DeploymentError, match="CMMS-E032"):
        ToolchainInstaller(
            manifest,
            RunOnly(),
            runtime_root=safe_runtime.root / ".runtime",
        )


def test_production_download_adapter_enforces_the_body_size_limit(
    tmp_path: Path,
) -> None:
    output = tmp_path / "oversized"
    destination_fd = os.open(
        output,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        command = CommandSpec(
            argv=(
                str(Path(sys.executable).resolve()),
                "-c",
                "import os; os.write(1, b'x' * 4096)",
            ),
            cwd=tmp_path,
            environment={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
            timeout_seconds=20,
            stdout_limit=64 * 1024,
            stderr_limit=64 * 1024,
            safe_label="bounded-download-body-test",
        )
        with pytest.raises(DeploymentError):
            BoundedToolchainRunner().download(
                BoundedDownloadSpec(
                    command=command,
                    destination_fd=destination_fd,
                    body_limit_bytes=1024,
                    header_limit_bytes=64 * 1024,
                )
            )
        assert os.fstat(destination_fd).st_size <= 1024
    finally:
        os.close(destination_fd)


def test_production_download_adapter_enforces_the_header_size_limit(
    tmp_path: Path,
) -> None:
    output = tmp_path / "bounded-body"
    destination_fd = os.open(
        output,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        command = CommandSpec(
            argv=(
                str(Path(sys.executable).resolve()),
                "-c",
                "import os; os.write(1, b'x'); os.write(2, b'h' * 4096)",
            ),
            cwd=tmp_path,
            environment={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
            timeout_seconds=20,
            stdout_limit=64 * 1024,
            stderr_limit=64 * 1024,
            safe_label="bounded-download-header-test",
        )
        with pytest.raises(DeploymentError):
            BoundedToolchainRunner().download(
                BoundedDownloadSpec(
                    command=command,
                    destination_fd=destination_fd,
                    body_limit_bytes=1024,
                    header_limit_bytes=1024,
                )
            )
        assert os.fstat(destination_fd).st_size <= 1024
    finally:
        os.close(destination_fd)


def test_production_download_adapter_streams_into_the_held_descriptor(
    tmp_path: Path,
) -> None:
    output = tmp_path / "downloaded"
    destination_fd = os.open(
        output,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    headers = b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\n\r\n"
    try:
        command = CommandSpec(
            argv=(
                str(Path(sys.executable).resolve()),
                "-c",
                (
                    "import os; os.write(1, b'archive'); "
                    f"os.write(2, {headers!r})"
                ),
            ),
            cwd=tmp_path,
            environment={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
            timeout_seconds=20,
            stdout_limit=64 * 1024,
            stderr_limit=64 * 1024,
            safe_label="bounded-download-success-test",
        )
        result = BoundedToolchainRunner().download(
            BoundedDownloadSpec(
                command=command,
                destination_fd=destination_fd,
                body_limit_bytes=1024,
                header_limit_bytes=1024,
            )
        )
        assert result == BoundedDownloadResult(headers)
        assert os.pread(destination_fd, 7, 0) == b"archive"
    finally:
        os.close(destination_fd)


def test_download_path_substitution_before_hash_is_rejected_by_inode_binding(
    safe_runtime: SafeRuntimeFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, archives = _fixture_manifest(tmp_path, monkeypatch)
    runner = ToolchainRunner(archives, substitute_download_path=True)
    claimed = _claim(safe_runtime, manifest_sha256=manifest.sha256)
    try:
        with pytest.raises(DeploymentError):
            ToolchainInstaller(
                manifest,
                runner,
                runtime_root=safe_runtime.root / ".runtime",
            ).install_all(claimed.context)
        assert runner.substituted_path is not None
        assert runner.substituted_path.read_bytes() == runner.substitution_sentinel
    finally:
        claimed.lease.close()


def test_archive_path_substitution_after_hash_cannot_change_extracted_inode(
    safe_runtime: SafeRuntimeFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, archives = _fixture_manifest(tmp_path, monkeypatch)
    runner = ToolchainRunner(archives)
    original_extract = toolchains_module._extract_archive

    def substitute(bound: Any, *args: Any, **kwargs: Any) -> Any:
        path = bound.path if hasattr(bound, "path") else Path(bound)
        original = path.with_name(f"{path.name}.original")
        payload = path.read_bytes()
        path.rename(original)
        path.write_bytes(payload)
        path.chmod(0o600)
        return original_extract(bound, *args, **kwargs)

    monkeypatch.setattr(toolchains_module, "_extract_archive", substitute)
    claimed = _claim(safe_runtime, manifest_sha256=manifest.sha256)
    try:
        with pytest.raises(DeploymentError):
            ToolchainInstaller(
                manifest,
                runner,
                runtime_root=safe_runtime.root / ".runtime",
            ).install_all(claimed.context)
    finally:
        claimed.lease.close()


@pytest.mark.parametrize(
    "corruption",
    ("receipt", "probe"),
)
def test_existing_install_requires_both_receipt_and_live_version_probe(
    safe_runtime: SafeRuntimeFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    manifest, archives = _fixture_manifest(tmp_path, monkeypatch)
    runner = ToolchainRunner(archives)
    claimed, installer, receipt = _install(safe_runtime, manifest, runner)
    try:
        runner.calls.clear()
        if corruption == "receipt":
            receipt_path = receipt.require("node").home / ".ifactory-toolchain-receipt.json"
            receipt_path.write_bytes(b"{}\n")
            receipt_path.chmod(0o600)
        else:
            runner.probe_outputs = {"node": "v26.0.0"}
        statuses = installer.plan_status(safe_runtime.root / ".runtime")
        node = next(row for row in statuses if row.name == "node")
        assert node.installed is True
        assert node.ready is False
        before = tuple(sorted(receipt.require("node").home.rglob("*")))
        with pytest.raises(DeploymentError):
            installer.install_all(claimed.context)
        assert _curl_calls(runner) == []
        assert tuple(sorted(receipt.require("node").home.rglob("*"))) == before
    finally:
        claimed.lease.close()


def test_atomic_publish_never_replaces_a_racing_target(
    safe_runtime: SafeRuntimeFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, archives = _fixture_manifest(tmp_path, monkeypatch)
    target = (
        safe_runtime.root
        / ".runtime/toolchains"
        / f"temurin-{EXPECTED_TOOLS['temurin']['version']}"
    )

    def race(executable: str) -> None:
        if executable == "java" and not target.exists():
            target.mkdir(parents=True)
            (target / "sentinel").write_text("keep", encoding="utf-8")

    runner = ToolchainRunner(archives, probe_hook=race)
    claimed = _claim(safe_runtime, manifest_sha256=manifest.sha256)
    try:
        with pytest.raises(DeploymentError):
            ToolchainInstaller(
                manifest,
                runner,
                runtime_root=safe_runtime.root / ".runtime",
            ).install_all(claimed.context)
        assert (target / "sentinel").read_text(encoding="utf-8") == "keep"
    finally:
        claimed.lease.close()


def test_version_probes_use_only_pinned_absolute_tool_paths(
    safe_runtime: SafeRuntimeFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, archives = _fixture_manifest(tmp_path, monkeypatch)
    runner = ToolchainRunner(archives)
    claimed, _installer, _receipt = _install(safe_runtime, manifest, runner)
    try:
        probes = [call for call in runner.calls if Path(call.argv[0]).name != "curl"]
        assert {Path(call.argv[0]).name for call in probes} == {
            "java",
            "mvn",
            "node",
            "npm",
        }
        for call in probes:
            executable = Path(call.argv[0])
            assert executable.is_absolute()
            assert executable.is_relative_to(
                safe_runtime.root / ".runtime/toolchains"
            )
            assert call.environment["PATH"].split(os.pathsep)[0].startswith(
                str(safe_runtime.root / ".runtime/toolchains")
            )
        flattened = "\n".join(" ".join(call.argv) for call in probes)
        assert "Java 25" not in flattened
        assert "3.6.3" not in flattened
        assert "Node 26" not in flattened
    finally:
        claimed.lease.close()


def test_safe_relative_npm_symlink_is_probed_inside_pinned_node_home(
    safe_runtime: SafeRuntimeFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, archives = _fixture_manifest(
        tmp_path,
        monkeypatch,
        node_npm_symlink=True,
    )
    runner = ToolchainRunner(archives)
    claimed, _installer, receipt = _install(safe_runtime, manifest, runner)
    try:
        npm = receipt.require("node").home / "bin/npm"
        assert npm.is_symlink()
        assert npm.resolve(strict=True).is_relative_to(receipt.require("node").home)
        npm_probe = next(
            call for call in runner.calls if Path(call.argv[0]).name == "npm"
        )
        probed_path = Path(npm_probe.argv[0])
        assert probed_path.parts[-2:] == ("bin", "npm")
        assert probed_path.is_relative_to(
            safe_runtime.root / ".runtime/toolchains"
        )
    finally:
        claimed.lease.close()


def test_fresh_node_probe_puts_its_temporary_bin_first_for_npm_shebangs(
    safe_runtime: SafeRuntimeFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, archives = _fixture_manifest(
        tmp_path,
        monkeypatch,
        node_npm_symlink=True,
    )
    runner = ToolchainRunner(archives)
    claimed, _installer, _receipt = _install(safe_runtime, manifest, runner)
    try:
        npm_probe = next(
            call
            for call in runner.calls
            if Path(call.argv[0]).name == "npm"
            and call.cwd.name.startswith(".node-")
            and call.cwd.name.endswith(".tmp")
        )
        assert npm_probe.environment["PATH"].split(os.pathsep)[0] == str(
            npm_probe.cwd / "bin"
        )
    finally:
        claimed.lease.close()


@pytest.mark.parametrize(
    ("invalid", "result"),
    (
        ("nonzero", CommandResult(7, "v21.6.1", "")),
        ("bool-returncode", CommandResult(False, "v21.6.1", "")),
        ("string-returncode", CommandResult("0", "v21.6.1", "")),
        ("bytes-stdout", CommandResult(0, b"v21.6.1", "")),
        ("bytes-stderr", CommandResult(0, "v21.6.1", b"")),
    ),
)
def test_installer_probe_requires_exact_successful_command_result_fields(
    safe_runtime: SafeRuntimeFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid: str,
    result: CommandResult,
) -> None:
    del invalid
    manifest, archives = _fixture_manifest(tmp_path, monkeypatch)
    valid_runner = ToolchainRunner(archives)
    claimed, _installer, _receipt = _install(
        safe_runtime,
        manifest,
        valid_runner,
    )
    invalid_runner = ToolchainRunner(archives)
    valid_run = invalid_runner.run

    def invalid_node_result(spec: Any) -> CommandResult:
        if Path(spec.argv[0]).name == "node":
            return result
        return valid_run(spec)

    invalid_runner.run = invalid_node_result  # type: ignore[method-assign]
    try:
        statuses = ToolchainInstaller(
            manifest,
            invalid_runner,
            runtime_root=safe_runtime.root / ".runtime",
        ).plan_status(safe_runtime.root / ".runtime")
        node = next(row for row in statuses if row.name == "node")
        assert node.installed is True
        assert node.ready is False
    finally:
        claimed.lease.close()


def test_installation_receipt_is_full_reprobed_from_every_published_home(
    safe_runtime: SafeRuntimeFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, archives = _fixture_manifest(tmp_path, monkeypatch)
    runner = ToolchainRunner(archives)
    claimed, _installer, _receipt = _install(safe_runtime, manifest, runner)
    try:
        published_probes = [
            call
            for call in runner.calls
            if Path(call.argv[0]).name != "curl"
            and call.cwd.name
            in {
                f"{tool.name}-{tool.version}"
                for tool in manifest.tools
            }
        ]
        assert tuple(Path(call.argv[0]).name for call in published_probes) == (
            "java",
            "mvn",
            "node",
            "npm",
        )
    finally:
        claimed.lease.close()


def test_final_descriptor_only_recheck_rejects_same_content_receipt_replacement(
    safe_runtime: SafeRuntimeFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, archives = _fixture_manifest(tmp_path, monkeypatch)
    runner = ToolchainRunner(archives)
    claimed = _claim(safe_runtime, manifest_sha256=manifest.sha256)
    original_verify = toolchains_module.verify_toolchain_receipt
    verify_calls = 0

    def replace_after_full_reprobe(*args: Any, **kwargs: Any) -> Any:
        nonlocal verify_calls
        verify_calls += 1
        binding = original_verify(*args, **kwargs)
        if verify_calls == 1:
            receipt_path = (
                safe_runtime.root
                / ".runtime/toolchains"
                / f"node-{manifest.require('node').version}"
                / ".ifactory-toolchain-receipt.json"
            )
            data = receipt_path.read_bytes()
            receipt_path.unlink()
            receipt_path.write_bytes(data)
            receipt_path.chmod(0o600)
        return binding

    monkeypatch.setattr(
        toolchains_module,
        "verify_toolchain_receipt",
        replace_after_full_reprobe,
    )
    try:
        with pytest.raises(DeploymentError, match="CMMS-E032"):
            ToolchainInstaller(
                manifest,
                runner,
                runtime_root=safe_runtime.root / ".runtime",
            ).install_all(claimed.context)
        assert verify_calls == 2
    finally:
        claimed.lease.close()


def test_final_descriptor_only_recheck_is_followed_by_a_live_action_gate(
    safe_runtime: SafeRuntimeFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, archives = _fixture_manifest(tmp_path, monkeypatch)
    runner = ToolchainRunner(archives)
    claimed = _claim(safe_runtime, manifest_sha256=manifest.sha256)
    original_verify = toolchains_module.verify_toolchain_receipt
    descriptor_recheck_finished = False

    def invalidate_after_descriptor_recheck(*args: Any, **kwargs: Any) -> Any:
        nonlocal descriptor_recheck_finished
        binding = original_verify(*args, **kwargs)
        if kwargs.get("reprobe") is False:
            descriptor_recheck_finished = True
            _invalidate_toolchain_action(safe_runtime, claimed)
        return binding

    monkeypatch.setattr(
        toolchains_module,
        "verify_toolchain_receipt",
        invalidate_after_descriptor_recheck,
    )
    try:
        with pytest.raises(DeploymentError):
            ToolchainInstaller(
                manifest,
                runner,
                runtime_root=safe_runtime.root / ".runtime",
            ).install_all(claimed.context)
        assert descriptor_recheck_finished is True
    finally:
        claimed.lease.close()


def test_download_action_failure_after_bound_create_closes_fd_and_path(
    safe_runtime: SafeRuntimeFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, archives = _fixture_manifest(tmp_path, monkeypatch)
    runner = ToolchainRunner(archives)
    claimed = _claim(safe_runtime, manifest_sha256=manifest.sha256)
    original_create = toolchains_module._BoundFile.create
    captured: list[Any] = []

    def create_then_invalidate(path: Path, *, max_bytes: int) -> Any:
        bound = original_create(path, max_bytes=max_bytes)
        captured.append(bound)
        _invalidate_toolchain_action(safe_runtime, claimed)
        return bound

    monkeypatch.setattr(
        toolchains_module._BoundFile,
        "create",
        staticmethod(create_then_invalidate),
    )
    try:
        with pytest.raises(DeploymentError):
            ToolchainInstaller(
                manifest,
                runner,
                runtime_root=safe_runtime.root / ".runtime",
            ).install_all(claimed.context)
        assert len(captured) == 1
        bound = captured[0]
        with pytest.raises(OSError):
            os.fstat(bound.fd)
        assert not bound.path.exists()
    finally:
        for bound in captured:
            if not bound.closed:
                bound.close()
        claimed.lease.close()


def test_install_one_action_failure_after_download_closes_archive_fd_and_path(
    safe_runtime: SafeRuntimeFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, archives = _fixture_manifest(tmp_path, monkeypatch)
    runner = ToolchainRunner(archives)
    claimed = _claim(safe_runtime, manifest_sha256=manifest.sha256)
    original_download = ToolchainInstaller._download
    captured: list[Any] = []

    def download_then_invalidate(self: Any, *args: Any, **kwargs: Any) -> Any:
        bound = original_download(self, *args, **kwargs)
        captured.append(bound)
        _invalidate_toolchain_action(safe_runtime, claimed)
        return bound

    monkeypatch.setattr(
        ToolchainInstaller,
        "_download",
        download_then_invalidate,
    )
    try:
        with pytest.raises(DeploymentError):
            ToolchainInstaller(
                manifest,
                runner,
                runtime_root=safe_runtime.root / ".runtime",
            ).install_all(claimed.context)
        assert len(captured) == 1
        archive = captured[0]
        with pytest.raises(OSError):
            os.fstat(archive.fd)
        assert not archive.path.exists()
    finally:
        for archive in captured:
            if not archive.closed:
                archive.close()
        claimed.lease.close()


def test_temporary_probe_binds_every_later_executable_before_first_runner(
    safe_runtime: SafeRuntimeFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, archives = _fixture_manifest(tmp_path, monkeypatch)
    runner = ToolchainRunner(archives)
    original_run = runner.run
    replaced_later_probe = False

    def replace_npm_during_node_probe(spec: CommandSpec) -> CommandResult:
        nonlocal replaced_later_probe
        result = original_run(spec)
        if (
            not replaced_later_probe
            and Path(spec.argv[0]).name == "node"
            and spec.cwd.name.startswith(".node-")
            and spec.cwd.name.endswith(".tmp")
        ):
            npm = spec.cwd / "bin/npm"
            payload = npm.read_bytes()
            npm.unlink()
            npm.write_bytes(payload)
            npm.chmod(0o755)
            replaced_later_probe = True
        return result

    runner.run = replace_npm_during_node_probe  # type: ignore[method-assign]
    claimed = _claim(safe_runtime, manifest_sha256=manifest.sha256)
    try:
        with pytest.raises(DeploymentError, match="CMMS-E032"):
            ToolchainInstaller(
                manifest,
                runner,
                runtime_root=safe_runtime.root / ".runtime",
            ).install_all(claimed.context)
        assert replaced_later_probe is True
    finally:
        claimed.lease.close()


def test_probe_rejects_an_intermediate_bin_symlink_outside_verified_home(
    safe_runtime: SafeRuntimeFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, archives = _fixture_manifest(tmp_path, monkeypatch)
    runner = ToolchainRunner(archives)
    claimed, installer, receipt = _install(safe_runtime, manifest, runner)
    try:
        node_home = receipt.require("node").home
        shutil.rmtree(node_home / "bin")
        ambient = tmp_path / "ambient-node-26"
        ambient.mkdir()
        for executable in ("node", "npm"):
            path = ambient / executable
            path.write_bytes(b"ambient\n")
            path.chmod(0o755)
        (node_home / "bin").symlink_to(ambient, target_is_directory=True)
        runner.calls.clear()
        node = next(
            status
            for status in installer.plan_status(safe_runtime.root / ".runtime")
            if status.name == "node"
        )
        assert node.installed is True
        assert node.ready is False
        assert not any(
            Path(call.argv[0]).is_relative_to(ambient) for call in runner.calls
        )
    finally:
        claimed.lease.close()


def test_installer_rejects_nonfixed_curl_path(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    manifest = ToolchainManifest.load(TOOLCHAIN_MANIFEST)
    with pytest.raises(DeploymentError):
        ToolchainInstaller(
            manifest,
            ToolchainRunner({}),
            runtime_root=safe_runtime.root / ".runtime",
            curl_path=Path("/opt/custom/curl"),
        )


def test_maven_settings_is_the_exact_credential_free_policy() -> None:
    assert MAVEN_SETTINGS.read_text(encoding="utf-8") == """<?xml version="1.0" encoding="UTF-8"?>
<settings xmlns="http://maven.apache.org/SETTINGS/1.2.0"
          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.2.0 https://maven.apache.org/xsd/settings-1.2.0.xsd">
  <mirrors>
    <mirror>
      <id>ifactory-central</id>
      <name>iFactory fixed Maven Central mirror</name>
      <url>https://repo.maven.apache.org/maven2</url>
      <mirrorOf>*</mirrorOf>
    </mirror>
  </mirrors>
</settings>
"""


def test_sensitive_manifest_pins_the_reviewed_cmms_source() -> None:
    manifest = SensitiveManifest.load(SENSITIVE_MANIFEST)
    assert hashlib.sha256(SENSITIVE_MANIFEST.read_bytes()).hexdigest() == (
        EXPECTED_SENSITIVE_MANIFEST_SHA256
    )
    assert manifest.sha256 == EXPECTED_SENSITIVE_MANIFEST_SHA256
    assert manifest.cmms_gitlink == "0662538e1feda84ba6288d99ecae02056b29d5d8"
    assert {row.path: row.sha256 for row in manifest.files} == EXPECTED_SENSITIVE_FILES
    assert tuple(row.path for row in manifest.files) == tuple(EXPECTED_SENSITIVE_FILES)
    assert manifest.migration_tree.path == "api/src/main/resources/db"
    assert manifest.migration_tree.sha256 == (
        "b1409e4119de782ab86a85be88efd585"
        "382f52dbb3070d6974857489f327a6f6"
    )


def test_reviewed_sensitive_baseline_matches_current_cmms_component() -> None:
    result = verify_sensitive_baseline(
        ROOT / "components/cmms",
        SensitiveManifest.load(SENSITIVE_MANIFEST),
    )
    assert result.ok is True
    assert result.code == "SENSITIVE_BASELINE_MATCHED"
    assert result.sensitive_file_count == 46
    assert result.migration_file_count == 117


def _sensitive_fixture_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, SensitiveManifest]:
    cmms_root = tmp_path / "cmms"
    files = {
        "api/pom.xml": b"reviewed-pom\n",
        "frontend/package-lock.json": b"reviewed-lock\n",
    }
    migrations = {
        "api/src/main/resources/db/V1__init.sql": b"create table example();\n",
        "api/src/main/resources/db/repeatable/R__view.sql": b"select 1;\n",
    }
    for relative, content in {**files, **migrations}.items():
        path = cmms_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    migration_lines = b"".join(
        hashlib.sha256(content).hexdigest().encode("ascii")
        + b"  "
        + relative.encode("utf-8")
        + b"\n"
        for relative, content in sorted(migrations.items())
    )
    raw = {
        "cmms_gitlink": "f" * 40,
        "files": [
            {
                "path": relative,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for relative, content in sorted(files.items())
        ],
        "migration_tree": {
            "path": "api/src/main/resources/db",
            "sha256": hashlib.sha256(migration_lines).hexdigest(),
        },
        "schema_version": 1,
    }
    manifest_path = tmp_path / "sensitive.json"
    manifest_bytes = canonical_json_bytes(raw)
    manifest_path.write_bytes(manifest_bytes)
    monkeypatch.setattr(
        source_module,
        "_REVIEWED_SENSITIVE_MANIFEST_SHA256",
        hashlib.sha256(manifest_bytes).hexdigest(),
    )
    return cmms_root, SensitiveManifest.load(manifest_path)


def test_sensitive_manifest_rejects_any_unreviewed_whole_file(
    tmp_path: Path,
) -> None:
    raw = json.loads(SENSITIVE_MANIFEST.read_text(encoding="utf-8"))
    raw["files"][0]["sha256"] = "0" * 64
    changed = tmp_path / "changed.json"
    changed.write_bytes(canonical_json_bytes(raw))
    with pytest.raises(DeploymentError):
        SensitiveManifest.load(changed)


def test_sensitive_manifest_fixture_uses_public_strict_loader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _cmms_root, manifest = _sensitive_fixture_manifest(tmp_path, monkeypatch)
    assert tuple(row.path for row in manifest.files) == (
        "api/pom.xml",
        "frontend/package-lock.json",
    )
    assert manifest.sha256 == source_module._REVIEWED_SENSITIVE_MANIFEST_SHA256


@pytest.mark.parametrize("change", ("missing", "changed", "symlink"))
def test_sensitive_file_anomaly_fails_closed_without_leaking_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    cmms_root, manifest = _sensitive_fixture_manifest(tmp_path, monkeypatch)
    target = cmms_root / "api/pom.xml"
    if change == "missing":
        target.unlink()
    elif change == "changed":
        target.write_bytes(b"TOP-SECRET-CHANGED-CONTENT\n")
    else:
        target.unlink()
        target.symlink_to("/etc/passwd")
    result = verify_sensitive_baseline(cmms_root, manifest)
    assert result.ok is False
    assert result.code == "SENSITIVE_BASELINE_CHANGED"
    assert "TOP-SECRET" not in repr(result)


@pytest.mark.parametrize("change", ("extra", "symlink", "special"))
def test_migration_tree_anomaly_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    cmms_root, manifest = _sensitive_fixture_manifest(tmp_path, monkeypatch)
    migration_root = cmms_root / "api/src/main/resources/db"
    if change == "extra":
        (migration_root / "ignored-extra.sql").write_bytes(b"select 2;\n")
    elif change == "symlink":
        (migration_root / "escape.sql").symlink_to("/etc/passwd")
    else:
        os.mkfifo(migration_root / "special.sql")
    result = verify_sensitive_baseline(cmms_root, manifest)
    assert result.ok is False
    assert result.code == "SENSITIVE_BASELINE_CHANGED"


def test_sensitive_baseline_enforces_bounded_regular_file_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cmms_root, manifest = _sensitive_fixture_manifest(tmp_path, monkeypatch)
    monkeypatch.setattr(source_module, "_MAX_BASELINE_FILE_BYTES", 4)
    result = verify_sensitive_baseline(cmms_root, manifest)
    assert result.ok is False
    assert result.code == "SENSITIVE_BASELINE_CHANGED"


def _git(cwd: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ("/usr/bin/git", *arguments),
        cwd=cwd,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C"},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        shell=False,
    )
    if completed.returncode != 0:
        raise AssertionError("local git fixture command failed")
    return completed.stdout


def _commit(cwd: Path, message: str) -> None:
    _git(
        cwd,
        "-c",
        "user.name=CMMS Source Test",
        "-c",
        "user.email=cmms-source@example.test",
        "commit",
        "-m",
        message,
    )


def _source_repositories(tmp_path: Path) -> Path:
    root = tmp_path / "platform"
    cmms = root / "components/cmms"
    cmms.mkdir(parents=True)
    _git(root, "init", "--quiet")
    (root / "root-control.txt").write_bytes(b"root-v1\n")

    _git(cmms, "init", "--quiet")
    (cmms / "api.txt").write_bytes(b"cmms-v1\n")
    _git(cmms, "add", "--", "api.txt")
    _commit(cmms, "initial cmms")

    _git(root, "add", "--", "root-control.txt", "components/cmms")
    _commit(root, "initial platform")
    return root


def test_source_capture_clean_binding_uses_framed_non_sentinel_fingerprints(
    tmp_path: Path,
) -> None:
    root = _source_repositories(tmp_path)
    binding = SourceInspector().capture(root, RuntimeProfile.ACCEPTANCE)
    assert binding.root_status.value == "CLEAN"
    assert binding.cmms_status.value == "CLEAN"
    assert binding.cmms_gitlink == binding.cmms_head
    assert binding.root_dirty_fingerprint != "0" * 64
    assert binding.cmms_dirty_fingerprint != "0" * 64
    assert binding.root_dirty_fingerprint != binding.cmms_dirty_fingerprint


@pytest.mark.parametrize("dirty_repository", ("root", "cmms"))
def test_development_capture_keeps_root_and_cmms_status_independent(
    tmp_path: Path,
    dirty_repository: str,
) -> None:
    root = _source_repositories(tmp_path)
    cmms = root / "components/cmms"
    target = root / "root-control.txt" if dirty_repository == "root" else cmms / "api.txt"
    target.write_bytes(b"same-size\n")
    binding = SourceInspector().capture(root, RuntimeProfile.DEVELOPMENT)
    assert binding.root_status.value == (
        "UNCOMMITTED" if dirty_repository == "root" else "CLEAN"
    )
    assert binding.cmms_status.value == (
        "UNCOMMITTED" if dirty_repository == "cmms" else "CLEAN"
    )
    with pytest.raises(DeploymentError):
        SourceInspector().capture(root, RuntimeProfile.ACCEPTANCE)


def test_cmms_head_mismatch_is_independent_from_both_clean_statuses(
    tmp_path: Path,
) -> None:
    root = _source_repositories(tmp_path)
    cmms = root / "components/cmms"
    (cmms / "api.txt").write_bytes(b"cmms-v2\n")
    _git(cmms, "add", "--", "api.txt")
    _commit(cmms, "advance cmms")
    binding = SourceInspector().capture(root, RuntimeProfile.DEVELOPMENT)
    assert binding.root_status.value == "CLEAN"
    assert binding.cmms_status.value == "CLEAN"
    assert binding.cmms_gitlink != binding.cmms_head
    with pytest.raises(DeploymentError):
        SourceInspector().capture(root, RuntimeProfile.ACCEPTANCE)


def test_staged_root_gitlink_change_only_dirties_root_evidence(
    tmp_path: Path,
) -> None:
    root = _source_repositories(tmp_path)
    cmms = root / "components/cmms"
    recorded_gitlink = SourceInspector().capture(
        root,
        RuntimeProfile.ACCEPTANCE,
    ).cmms_gitlink
    (cmms / "api.txt").write_bytes(b"cmms-v2\n")
    _git(cmms, "add", "--", "api.txt")
    _commit(cmms, "advance cmms")
    _git(root, "add", "--", "components/cmms")

    binding = SourceInspector().capture(root, RuntimeProfile.DEVELOPMENT)
    assert binding.root_status.value == "UNCOMMITTED"
    assert binding.cmms_status.value == "CLEAN"
    assert binding.cmms_gitlink == recorded_gitlink
    assert binding.cmms_head != recorded_gitlink


@pytest.mark.parametrize("kind", ("unstaged", "staged", "untracked"))
def test_source_fingerprint_changes_when_dirty_content_changes(
    tmp_path: Path,
    kind: str,
) -> None:
    root = _source_repositories(tmp_path)
    if kind == "untracked":
        target = root / "new-control.txt"
        target.write_bytes(b"dirty-a\n")
    else:
        target = root / "root-control.txt"
        target.write_bytes(b"dirty-a\n")
        if kind == "staged":
            _git(root, "add", "--", "root-control.txt")
    first = SourceInspector().capture(root, RuntimeProfile.DEVELOPMENT)
    target.write_bytes(b"dirty-b\n")
    if kind == "staged":
        _git(root, "add", "--", "root-control.txt")
    second = SourceInspector().capture(root, RuntimeProfile.DEVELOPMENT)
    assert first.root_dirty_fingerprint != second.root_dirty_fingerprint
    assert "dirty-a" not in repr(first)
    assert "dirty-b" not in repr(second)


def test_source_capture_rejects_untracked_symlink_and_count_overflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _source_repositories(tmp_path)
    (root / "secret-link").symlink_to("/etc/passwd")
    with pytest.raises(DeploymentError) as captured:
        SourceInspector().capture(root, RuntimeProfile.DEVELOPMENT)
    assert "/etc/passwd" not in str(captured.value)
    (root / "secret-link").unlink()
    monkeypatch.setattr(source_module, "_MAX_UNTRACKED_FILES", 1)
    (root / "one.txt").write_bytes(b"one")
    (root / "two.txt").write_bytes(b"two")
    with pytest.raises(DeploymentError):
        SourceInspector().capture(root, RuntimeProfile.DEVELOPMENT)


def test_source_capture_rejects_untracked_special_file(tmp_path: Path) -> None:
    root = _source_repositories(tmp_path)
    os.mkfifo(root / "untracked-special")
    with pytest.raises(DeploymentError):
        SourceInspector().capture(root, RuntimeProfile.DEVELOPMENT)


def test_source_capture_enforces_untracked_single_file_byte_limit(
    tmp_path: Path,
) -> None:
    root = _source_repositories(tmp_path)
    (root / "oversized.bin").touch()
    os.truncate(root / "oversized.bin", 16 * 1024 * 1024 + 1)
    with pytest.raises(DeploymentError):
        SourceInspector().capture(root, RuntimeProfile.DEVELOPMENT)


def test_source_capture_enforces_untracked_total_byte_limit(
    tmp_path: Path,
) -> None:
    root = _source_repositories(tmp_path)
    for index in range(4):
        candidate = root / f"bulk-{index}.bin"
        candidate.touch()
        os.truncate(candidate, 16 * 1024 * 1024)
    (root / "bulk-4.bin").write_bytes(b"x")
    with pytest.raises(DeploymentError):
        SourceInspector().capture(root, RuntimeProfile.DEVELOPMENT)


def test_source_capture_disables_git_replace_objects(tmp_path: Path) -> None:
    root = _source_repositories(tmp_path)
    cmms = root / "components/cmms"
    replaced_root = SourceInspector().capture(
        root,
        RuntimeProfile.ACCEPTANCE,
    ).root_sha
    (cmms / "api.txt").write_bytes(b"cmms-v2\n")
    _git(cmms, "add", "--", "api.txt")
    _commit(cmms, "advance cmms")
    _git(root, "add", "--", "components/cmms")
    _commit(root, "advance gitlink")
    current_root = _git(root, "rev-parse", "HEAD").strip().decode("ascii")
    current_cmms = _git(cmms, "rev-parse", "HEAD").strip().decode("ascii")
    _git(root, "replace", current_root, replaced_root)

    binding = SourceInspector().capture(root, RuntimeProfile.ACCEPTANCE)
    assert binding.root_sha == current_root
    assert binding.cmms_gitlink == current_cmms
    assert binding.cmms_head == current_cmms


def test_source_capture_rejects_component_that_discovers_parent_repository(
    tmp_path: Path,
) -> None:
    root = _source_repositories(tmp_path)
    cmms_git_metadata = root / "components/cmms/.git"
    cmms_git_metadata.rename(tmp_path / "detached-cmms-git-metadata")
    with pytest.raises(DeploymentError):
        SourceInspector().capture(root, RuntimeProfile.DEVELOPMENT)


def test_source_capture_ignores_ambient_git_config_and_external_diff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _source_repositories(tmp_path)
    (root / "root-control.txt").write_bytes(b"ambient-resistant\n")
    (root / "untracked-secret.txt").write_bytes(b"ambient-secret\n")
    global_config = tmp_path / "malicious-gitconfig"
    global_config.write_text(
        "[status]\n\tshowUntrackedFiles = no\n[diff]\n\texternal = /missing/external-diff\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", "/missing/environment-diff")
    monkeypatch.setenv("HOME", str(tmp_path / "ambient-home"))
    binding = SourceInspector().capture(root, RuntimeProfile.DEVELOPMENT)
    assert binding.root_status.value == "UNCOMMITTED"
    assert "ambient-secret" not in repr(binding)


def test_source_capture_rejects_a_change_between_complete_samples(
    tmp_path: Path,
) -> None:
    root = _source_repositories(tmp_path)

    class RacingInspector(SourceInspector):
        calls = 0

        def _capture_complete_once(self, source_root: Path) -> Any:
            sample = super()._capture_complete_once(source_root)
            self.calls += 1
            if self.calls == 1:
                (source_root / "root-control.txt").write_bytes(b"raced\n")
            return sample

    with pytest.raises(DeploymentError):
        RacingInspector().capture(root, RuntimeProfile.DEVELOPMENT)


@pytest.mark.parametrize("repository_name", ("root", "cmms"))
@pytest.mark.parametrize(
    "index_flag",
    ("--assume-unchanged", "--skip-worktree"),
)
def test_source_capture_rejects_every_hidden_index_flag_in_both_profiles(
    tmp_path: Path,
    repository_name: str,
    index_flag: str,
) -> None:
    root = _source_repositories(tmp_path)
    repository = root if repository_name == "root" else root / "components/cmms"
    tracked_path = "root-control.txt" if repository_name == "root" else "api.txt"
    _git(repository, "update-index", index_flag, "--", tracked_path)
    (repository / tracked_path).write_bytes(b"index-hidden-change\n")

    for profile in (RuntimeProfile.DEVELOPMENT, RuntimeProfile.ACCEPTANCE):
        with pytest.raises(DeploymentError):
            SourceInspector().capture(root, profile)


@pytest.mark.parametrize("repository_name", ("root", "cmms"))
def test_ignored_regular_file_is_hashed_and_dirties_its_repository(
    tmp_path: Path,
    repository_name: str,
) -> None:
    root = _source_repositories(tmp_path)
    repository = root if repository_name == "root" else root / "components/cmms"
    (repository / ".git/info/exclude").write_text(
        "hidden-control.bin\n",
        encoding="utf-8",
    )
    hidden = repository / "hidden-control.bin"
    hidden.write_bytes(b"ignored-secret-a\n")
    first = SourceInspector().capture(root, RuntimeProfile.DEVELOPMENT)
    hidden.write_bytes(b"ignored-secret-b\n")
    second = SourceInspector().capture(root, RuntimeProfile.DEVELOPMENT)

    if repository_name == "root":
        assert first.root_status.value == "UNCOMMITTED"
        assert first.cmms_status.value == "CLEAN"
        assert first.root_dirty_fingerprint != second.root_dirty_fingerprint
    else:
        assert first.root_status.value == "CLEAN"
        assert first.cmms_status.value == "UNCOMMITTED"
        assert first.cmms_dirty_fingerprint != second.cmms_dirty_fingerprint
    assert "ignored-secret" not in repr(first)
    assert "ignored-secret" not in repr(second)
    with pytest.raises(DeploymentError):
        SourceInspector().capture(root, RuntimeProfile.ACCEPTANCE)


@pytest.mark.parametrize("repository_name", ("root", "cmms"))
@pytest.mark.parametrize("entry_kind", ("symlink", "fifo"))
def test_ignored_non_regular_entry_is_rejected(
    tmp_path: Path,
    repository_name: str,
    entry_kind: str,
) -> None:
    root = _source_repositories(tmp_path)
    repository = root if repository_name == "root" else root / "components/cmms"
    (repository / ".git/info/exclude").write_text(
        "hidden-special\n",
        encoding="utf-8",
    )
    hidden = repository / "hidden-special"
    if entry_kind == "symlink":
        hidden.symlink_to("/etc/passwd")
    else:
        os.mkfifo(hidden)
    with pytest.raises(DeploymentError):
        SourceInspector().capture(root, RuntimeProfile.DEVELOPMENT)


@pytest.mark.parametrize("repository_name", ("root", "cmms"))
def test_repo_filter_include_is_rejected_before_filter_command_runs(
    tmp_path: Path,
    repository_name: str,
) -> None:
    root = _source_repositories(tmp_path)
    repository = root if repository_name == "root" else root / "components/cmms"
    tracked_path = "root-control.txt" if repository_name == "root" else "api.txt"
    (repository / ".gitattributes").write_text("*.txt filter=audit\n", encoding="utf-8")
    _git(repository, "add", "--", ".gitattributes")
    _commit(repository, "track filter attributes")
    marker = tmp_path / f"{repository_name}-filter-ran"
    include_file = tmp_path / f"{repository_name}-filter.inc"
    include_file.write_text(
        "[filter \"audit\"]\n"
        f"\tclean = /usr/bin/touch {marker}\n"
        f"\tsmudge = /usr/bin/touch {marker}\n"
        "\trequired = true\n",
        encoding="utf-8",
    )
    _git(repository, "config", "include.path", str(include_file))
    (repository / tracked_path).write_bytes(b"filter-trigger-change\n")
    try:
        with pytest.raises(DeploymentError):
            SourceInspector().capture(root, RuntimeProfile.DEVELOPMENT)
    finally:
        assert not marker.exists()


@pytest.mark.parametrize(
    ("config_key", "config_value"),
    (
        ("remote.origin.promisor", "true"),
        ("remote.origin.partialclonefilter", "blob:none"),
    ),
)
def test_source_capture_rejects_partial_clone_configuration(
    tmp_path: Path,
    config_key: str,
    config_value: str,
) -> None:
    root = _source_repositories(tmp_path)
    _git(root, "config", config_key, config_value)
    with pytest.raises(DeploymentError):
        SourceInspector().capture(root, RuntimeProfile.DEVELOPMENT)


def test_sensitive_fixture_loses_authority_when_reviewed_digest_is_restored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cmms_root, manifest = _sensitive_fixture_manifest(tmp_path, monkeypatch)
    monkeypatch.setattr(
        source_module,
        "_REVIEWED_SENSITIVE_MANIFEST_SHA256",
        EXPECTED_SENSITIVE_MANIFEST_SHA256,
    )
    with pytest.raises(DeploymentError):
        verify_sensitive_baseline(cmms_root, manifest)


def test_root_exact_generated_paths_do_not_enter_source_evidence(
    tmp_path: Path,
) -> None:
    root = _source_repositories(tmp_path)
    before = SourceInspector().capture(root, RuntimeProfile.ACCEPTANCE)
    (root / ".git/info/exclude").write_text(
        ".runtime/\ndeploy/cmms/.venv/\ndeploy/cmms/.pytest_cache/\n"
        "deploy/cmms/.mypy_cache/\ndeploy/cmms/.ruff_cache/\n"
        "deploy/cmms/src/ifactory_cmms_deploy/__pycache__/\n"
        "tests/.venv/\ntests/.pytest_cache/\ntests/.mypy_cache/\n"
        "tests/.ruff_cache/\ntests/__pycache__/\ntests/contract/__pycache__/\n"
        "tests/contract/phase1/__pycache__/\ntests/e2e/__pycache__/\n"
        "tests/e2e/support/__pycache__/\n",
        encoding="utf-8",
    )
    generated_files = (
        root / ".runtime/toolchain/bin/java",
        root / "deploy/cmms/.venv/bin/python",
        root / "deploy/cmms/.pytest_cache/data",
        root / "deploy/cmms/.mypy_cache/data",
        root / "deploy/cmms/.ruff_cache/data",
        root / "deploy/cmms/src/ifactory_cmms_deploy/__pycache__/module.pyc",
        root / "tests/.venv/bin/python",
        root / "tests/.pytest_cache/data",
        root / "tests/.mypy_cache/data",
        root / "tests/.ruff_cache/data",
        root / "tests/__pycache__/module.pyc",
        root / "tests/contract/__pycache__/module.pyc",
        root / "tests/contract/phase1/__pycache__/module.pyc",
        root / "tests/e2e/__pycache__/module.pyc",
        root / "tests/e2e/support/__pycache__/module.pyc",
    )
    for generated in generated_files:
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_bytes(b"generated\n")
    (root / ".runtime/toolchain/bin/java-link").symlink_to("java")

    after = SourceInspector().capture(root, RuntimeProfile.ACCEPTANCE)
    assert after == before


def test_cmms_exact_generated_paths_do_not_enter_source_evidence(
    tmp_path: Path,
) -> None:
    root = _source_repositories(tmp_path)
    cmms = root / "components/cmms"
    before = SourceInspector().capture(root, RuntimeProfile.ACCEPTANCE)
    (cmms / ".git/info/exclude").write_text(
        "api/target/\nfrontend/node_modules/\nfrontend/build/\n"
        "frontend/public/runtime-env.js\n",
        encoding="utf-8",
    )
    generated_files = (
        cmms / "api/target/app.jar",
        cmms / "frontend/node_modules/package/index.js",
        cmms / "frontend/build/index.html",
        cmms / "frontend/public/runtime-env.js",
    )
    for generated in generated_files:
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_bytes(b"generated\n")
    (cmms / "frontend/node_modules/package/link").symlink_to("index.js")

    after = SourceInspector().capture(root, RuntimeProfile.ACCEPTANCE)
    assert after == before


@pytest.mark.parametrize("repository_name", ("root", "cmms"))
def test_worktree_scope_filter_is_rejected_before_command_runs(
    tmp_path: Path,
    repository_name: str,
) -> None:
    root = _source_repositories(tmp_path)
    repository = root if repository_name == "root" else root / "components/cmms"
    tracked_path = "root-control.txt" if repository_name == "root" else "api.txt"
    (repository / ".gitattributes").write_text("*.txt filter=audit\n", encoding="utf-8")
    _git(repository, "add", "--", ".gitattributes")
    _commit(repository, "track worktree filter attributes")
    marker = tmp_path / f"{repository_name}-worktree-filter-ran"
    _git(repository, "config", "extensions.worktreeConfig", "true")
    _git(
        repository,
        "config",
        "--worktree",
        "filter.audit.clean",
        f"/usr/bin/touch {marker}",
    )
    _git(
        repository,
        "config",
        "--worktree",
        "filter.audit.smudge",
        f"/usr/bin/touch {marker}",
    )
    _git(repository, "config", "--worktree", "filter.audit.required", "true")
    (repository / tracked_path).write_bytes(b"worktree-filter-change\n")
    try:
        with pytest.raises(DeploymentError):
            SourceInspector().capture(root, RuntimeProfile.DEVELOPMENT)
    finally:
        assert not marker.exists()


@pytest.mark.parametrize(
    ("repository_name", "relative_path"),
    (
        ("root", "deploy/arbitrary/.venv/payload.py"),
        ("root", "arbitrary/__pycache__/payload.pyc"),
        ("cmms", "arbitrary/.venv/payload.py"),
        ("cmms", "arbitrary/__pycache__/payload.pyc"),
    ),
)
def test_nonapproved_cache_basename_is_bound_as_ignored_source(
    tmp_path: Path,
    repository_name: str,
    relative_path: str,
) -> None:
    root = _source_repositories(tmp_path)
    repository = root if repository_name == "root" else root / "components/cmms"
    ignored_directory = relative_path.rsplit("/", 1)[0]
    (repository / ".git/info/exclude").write_text(
        f"/{ignored_directory}/\n",
        encoding="utf-8",
    )
    hidden = repository / relative_path
    hidden.parent.mkdir(parents=True)
    hidden.write_bytes(b"nonapproved-cache-a\n")
    first = SourceInspector().capture(root, RuntimeProfile.DEVELOPMENT)
    hidden.write_bytes(b"nonapproved-cache-b\n")
    second = SourceInspector().capture(root, RuntimeProfile.DEVELOPMENT)

    if repository_name == "root":
        assert first.root_status.value == "UNCOMMITTED"
        assert first.root_dirty_fingerprint != second.root_dirty_fingerprint
    else:
        assert first.cmms_status.value == "UNCOMMITTED"
        assert first.cmms_dirty_fingerprint != second.cmms_dirty_fingerprint
    with pytest.raises(DeploymentError):
        SourceInspector().capture(root, RuntimeProfile.ACCEPTANCE)


@dataclass(frozen=True)
class ApiBuildCase:
    root: Path
    claimed: ClaimedFixture
    toolchains: ToolchainReceipt
    source: SourceBinding
    sensitive_manifest: SensitiveManifest


@dataclass
class ApiBuildRunner:
    root: Path
    artifact: bytes = b"reviewed-api-artifact\n"
    candidate_kind: str = "regular"
    outcomes: tuple[object, ...] = ()
    mutate: Any | None = None
    probe_mutate: Any | None = None
    probe_outcomes: dict[str, object] | None = None
    calls: list[CommandSpec] = None  # type: ignore[assignment]
    probe_calls: list[CommandSpec] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.calls = []
        self.probe_calls = []

    def run(self, spec: CommandSpec) -> object:
        executable_name = Path(spec.argv[0]).name
        if spec.safe_label.startswith("toolchain-"):
            self.probe_calls.append(spec)
            if self.probe_mutate is not None:
                self.probe_mutate(len(self.probe_calls), spec)
            output = {
                "java": 'openjdk version "17.0.19"',
                "mvn": "Apache Maven 3.9.3",
                "node": "v21.6.1",
                "npm": "10.2.4",
            }[executable_name]
            if self.probe_outcomes and executable_name in self.probe_outcomes:
                return self.probe_outcomes[executable_name]
            return CommandResult(
                0,
                output if executable_name != "java" else "",
                output if executable_name == "java" else "",
            )
        self.calls.append(spec)
        if spec.argv[-3:] == ("clean", "package", "-DskipTests"):
            target = self.root / "components/cmms/api/target"
            target.mkdir(parents=True, exist_ok=True)
            target.chmod(0o755)
            candidate = target / "app.jar"
            if self.candidate_kind == "symlink":
                outside = self.root / "ambient-app.jar"
                outside.write_bytes(self.artifact)
                candidate.symlink_to(outside)
            elif self.candidate_kind == "hardlink":
                outside = self.root / "ambient-app.jar"
                outside.write_bytes(self.artifact)
                os.link(outside, candidate)
            elif self.candidate_kind == "empty":
                candidate.touch()
            elif self.candidate_kind == "missing":
                pass
            else:
                candidate.write_bytes(self.artifact)
                candidate.chmod(0o644)
            if self.candidate_kind == "extra":
                (target / "competing.jar").write_bytes(b"competitor\n")
            if self.candidate_kind == "nested":
                nested = target / "nested"
                nested.mkdir()
                (nested / "competing.jar").write_bytes(b"competitor\n")
            if self.candidate_kind == "deep":
                nested = target
                for depth in range(65):
                    nested = nested / f"level-{depth:02d}"
                    nested.mkdir()
                (nested / "payload.txt").write_bytes(b"too deep\n")
        if self.mutate is not None:
            self.mutate(len(self.calls), spec)
        if self.outcomes:
            return self.outcomes[len(self.calls) - 1]
        return CommandResult(0, "", "")


def _restart_api_actions() -> tuple[PlannedAction, ...]:
    return _ordered_actions(
        ActionCode.GATEWAY_FAIL_CLOSED,
        ActionCode.PROCESS_STOP_API,
        ActionCode.BUILD_API,
        ActionCode.LICENSE_VERIFY_OFFLINE,
        ActionCode.PROCESS_CREATE_API_PERMIT,
        ActionCode.PROCESS_START_API,
        ActionCode.READINESS_REQUIRE_LOOPBACK,
        ActionCode.GATEWAY_ENABLE_DUAL,
        ActionCode.READINESS_REQUIRE_DUAL,
    )


def _api_build_case(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    *,
    operation: Operation = Operation.REPAIR,
    profile: RuntimeProfile = RuntimeProfile.DEVELOPMENT,
    include_action: bool = True,
    toolchain_snapshot_sha256: str | None = None,
    sensitive_snapshot_sha256: str | None = None,
) -> ApiBuildCase:
    root = safe_runtime.root
    manifests = root / "deploy/cmms/manifests"
    manifests.mkdir(parents=True)
    shutil.copyfile(TOOLCHAIN_MANIFEST, manifests / "toolchains.json")
    shutil.copyfile(SENSITIVE_MANIFEST, manifests / "startup-sensitive-files.json")
    settings = root / "deploy/cmms/maven-settings.xml"
    shutil.copyfile(MAVEN_SETTINGS, settings)
    api = root / "components/cmms/api"
    api.mkdir(parents=True)
    (root / "components/cmms/frontend/public").mkdir(parents=True)
    (api / "pom.xml").write_text(
        "<project><build><finalName>app</finalName></build></project>\n",
        encoding="utf-8",
    )

    manifest = ToolchainManifest.load(manifests / "toolchains.json")
    installed: list[InstalledToolchain] = []
    for definition in manifest.tools:
        home = root / ".runtime/toolchains" / f"{definition.name}-{definition.version}"
        for probe in definition.probes:
            executable = home / probe.executable
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_bytes(b"fixture executable\n")
            executable.chmod(0o755)
        home.chmod(0o700)
        receipt_path = home / ".ifactory-toolchain-receipt.json"
        receipt_path.write_bytes(
            canonical_json_bytes(
                {
                    "archive_sha256": definition.sha256,
                    "manifest_sha256": manifest.sha256,
                    "name": definition.name,
                    "record_type": "cmms-toolchain-installation",
                    "schema_version": 1,
                    "version": definition.version,
                }
            )
        )
        receipt_path.chmod(0o600)
        installed.append(
            InstalledToolchain(
                name=definition.name,
                version=definition.version,
                archive_sha256=definition.sha256,
                home=home,
            )
        )
    (root / ".runtime/toolchains").chmod(0o700)
    receipt = ToolchainReceipt(manifest.sha256, tuple(installed))
    sensitive = SensitiveManifest.load(manifests / "startup-sensitive-files.json")
    source = replace(
        safe_runtime.make_snapshot().source,
        cmms_gitlink=sensitive.cmms_gitlink,
        cmms_head=sensitive.cmms_gitlink,
    )
    snapshot = replace(
        safe_runtime.make_snapshot(),
        source=source,
        toolchain_manifest_sha256=(
            manifest.sha256
            if toolchain_snapshot_sha256 is None
            else toolchain_snapshot_sha256
        ),
        sensitive_manifest_sha256=(
            sensitive.sha256
            if sensitive_snapshot_sha256 is None
            else sensitive_snapshot_sha256
        ),
    )
    if operation is Operation.RESTART_API:
        actions = _restart_api_actions()
    else:
        actions = _repair_actions(
            *((ActionCode.BUILD_API,) if include_action else ())
        )
    if not include_action and operation is Operation.RESTART_API:
        raise AssertionError("restart-api always requires build.api")
    plan = DeploymentPlan.create(
        snapshot=snapshot,
        operation=operation,
        profile=profile,
        license_mode=LicenseMode.OFFLINE,
        bootstrap_bindings=safe_runtime.bootstrap_plan_bindings,
        actions=actions,
        now=safe_runtime.make_plan().created_at,
        plan_nonce="9" * 32,
    )
    plan_path, plan_hash = write_plan(plan, safe_runtime.plans_dir)
    reservation = reserve_plan_attempt(
        plan_path,
        plan_hash,
        plan.created_at,
        safe_runtime.plans_dir,
        application_id="8" * 32,
    )
    lease = safe_runtime.acquire_deployment_write_lease(reservation)
    confirmed = load_confirmed_plan(
        reservation,
        snapshot=plan.snapshot,
        current_bootstrap_bindings=plan.bootstrap_bindings,
        lease=lease,
    )
    evidence = safe_runtime.gateway_authority(confirmed, lease).issue()
    context = claim_plan_application(
        confirmed,
        safe_runtime.plans_dir,
        lease,
        evidence,
    )
    monkeypatch.setattr(
        SourceInspector,
        "capture",
        lambda _self, captured_root, captured_profile: (
            source
            if captured_root == root and captured_profile is profile
            else (_ for _ in ()).throw(AssertionError("unexpected source capture"))
        ),
    )
    monkeypatch.setattr(
        source_module,
        "verify_sensitive_baseline",
        lambda cmms_root, captured_manifest: SensitiveBaselineResult(
            ok=(cmms_root == root / "components/cmms" and captured_manifest == sensitive),
            code=(
                "SENSITIVE_BASELINE_MATCHED"
                if cmms_root == root / "components/cmms" and captured_manifest == sensitive
                else "SENSITIVE_BASELINE_CHANGED"
            ),
            manifest_sha256=sensitive.sha256,
            sensitive_file_count=39,
            migration_file_count=116,
        ),
    )
    return ApiBuildCase(
        root,
        ClaimedFixture(context, reservation.application_path, lease),
        receipt,
        source,
        sensitive,
    )


def test_build_artifact_record_rejects_forged_field_types(tmp_path: Path) -> None:
    source = SourceBinding(
        root_sha="1" * 40,
        root_dirty_fingerprint="2" * 64,
        root_status=source_module.SourceStatus.CLEAN,
        cmms_gitlink="3" * 40,
        cmms_head="3" * 40,
        cmms_dirty_fingerprint="4" * 64,
        cmms_status=source_module.SourceStatus.CLEAN,
    )
    valid = BuildArtifact(
        source=source,
        toolchain_manifest_sha256="5" * 64,
        sensitive_manifest_sha256="6" * 64,
        artifact_sha256="7" * 64,
        path=tmp_path / "app.jar",
        targeted_tests_passed=True,
    )
    assert valid.path == tmp_path / "app.jar"
    for forged in (
        {"source": object()},
        {"artifact_sha256": "7" * 63},
        {"path": Path("relative.jar")},
        {"targeted_tests_passed": 1},
    ):
        with pytest.raises(DeploymentError):
            replace(valid, **forged)


def test_dependency_receipt_is_strict_and_frozen() -> None:
    source = SourceBinding(
        root_sha="1" * 40,
        root_dirty_fingerprint="2" * 64,
        root_status=source_module.SourceStatus.CLEAN,
        cmms_gitlink="3" * 40,
        cmms_head="3" * 40,
        cmms_dirty_fingerprint="4" * 64,
        cmms_status=source_module.SourceStatus.CLEAN,
    )
    receipt = DependencyReceipt(
        source=source,
        toolchain_manifest_sha256="5" * 64,
        sensitive_manifest_sha256="6" * 64,
        frontend_lock_sha256="7" * 64,
    )
    assert receipt.source is source
    with pytest.raises((AttributeError, TypeError)):
        receipt.frontend_lock_sha256 = "8" * 64  # type: ignore[misc]
    for forged in (
        {"source": object()},
        {"toolchain_manifest_sha256": "5" * 63},
        {"sensitive_manifest_sha256": b"6" * 64},
        {"frontend_lock_sha256": "G" * 64},
    ):
        with pytest.raises(DeploymentError):
            replace(receipt, **forged)


@pytest.mark.parametrize(
    ("profile", "operation"),
    (
        (RuntimeProfile.DEVELOPMENT, Operation.REPAIR),
        (RuntimeProfile.ACCEPTANCE, Operation.REPAIR),
    ),
)
def test_api_build_runs_exact_targeted_tests_then_fixed_package(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
    operation: Operation,
) -> None:
    case = _api_build_case(
        safe_runtime,
        monkeypatch,
        profile=profile,
        operation=operation,
    )
    runner = ApiBuildRunner(case.root)
    try:
        artifact = build_api_artifact(case.claimed.context, case.toolchains, runner)
        maven = case.toolchains.require("maven").home
        temurin = case.toolchains.require("temurin").home
        cache = case.root / ".runtime/cmms-cache/maven"
        settings = case.root / "deploy/cmms/maven-settings.xml"
        prefix = (
            str(maven / "bin/mvn"),
            "--settings",
            str(settings),
            f"-Dmaven.repo.local={cache}",
        )
        assert [call.argv for call in runner.calls] == [
            (
                *prefix,
                "-Dtest=AssetControllerTest,WorkOrderControllerTest,AssetIntegrationServiceTest,IntegrationIdempotencyServiceTest,WorkOrderServiceTest",
                "-Dsurefire.failIfNoSpecifiedTests=true",
                "test",
            ),
            (*prefix, "clean", "package", "-DskipTests"),
        ]
        expected_env = {
            "JAVA_HOME": str(temurin),
            "LC_ALL": "C",
            "PATH": f"{maven / 'bin'}:{temurin / 'bin'}:/usr/bin:/bin",
        }
        assert all(dict(call.environment) == expected_env for call in runner.calls)
        assert all(call.cwd == case.root / "components/cmms/api" for call in runner.calls)
        digest = hashlib.sha256(runner.artifact).hexdigest()
        assert artifact == BuildArtifact(
            source=case.source,
            toolchain_manifest_sha256=case.toolchains.manifest_sha256,
            sensitive_manifest_sha256=case.sensitive_manifest.sha256,
            artifact_sha256=digest,
            path=case.root / ".runtime/cmms-builds" / f"app-{digest}.jar",
            targeted_tests_passed=True,
        )
        assert artifact.path.read_bytes() == runner.artifact
        assert stat.S_IMODE(artifact.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(cache.stat().st_mode) == 0o700
    finally:
        case.claimed.lease.close()


def test_development_restart_api_only_packages_and_marks_tests_unpassed(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _api_build_case(
        safe_runtime,
        monkeypatch,
        operation=Operation.RESTART_API,
    )
    runner = ApiBuildRunner(case.root)
    try:
        artifact = build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert len(runner.calls) == 1
        assert len(runner.probe_calls) == 8
        assert runner.calls[0].argv[-3:] == ("clean", "package", "-DskipTests")
        assert artifact.targeted_tests_passed is False
    finally:
        case.claimed.lease.close()


def test_api_build_requires_live_planned_action_before_any_effect(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _api_build_case(
        safe_runtime,
        monkeypatch,
        include_action=False,
    )
    runner = ApiBuildRunner(case.root)
    cache = case.root / ".runtime/cmms-cache"
    builds = case.root / ".runtime/cmms-builds"
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert runner.probe_calls == []
        assert runner.calls == []
        assert not cache.exists()
        assert not builds.exists()
    finally:
        case.claimed.lease.close()


def test_api_build_rejects_stale_claim_before_any_effect(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _api_build_case(safe_runtime, monkeypatch)
    runner = ApiBuildRunner(case.root)
    safe_runtime.transition_application(
        case.claimed.application_path,
        case.claimed.context.application,
        PlanApplicationState.FAILED,
        case.claimed.context.application.claimed_at + timedelta(seconds=1),
    )
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert runner.calls == []
        assert not (case.root / ".runtime/cmms-cache").exists()
        assert not (case.root / ".runtime/cmms-builds").exists()
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize("binding", ("toolchain", "sensitive", "source"))
def test_api_build_rejects_every_snapshot_binding_before_runner(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    binding: str,
) -> None:
    case = _api_build_case(
        safe_runtime,
        monkeypatch,
        toolchain_snapshot_sha256=("a" * 64 if binding == "toolchain" else None),
        sensitive_snapshot_sha256=("b" * 64 if binding == "sensitive" else None),
    )
    if binding == "source":
        changed = replace(case.source, root_dirty_fingerprint="c" * 64)
        monkeypatch.setattr(
            SourceInspector,
            "capture",
            lambda _self, _root, _profile: changed,
        )
    runner = ApiBuildRunner(case.root)
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert runner.probe_calls == []
        assert runner.calls == []
        assert not (case.root / ".runtime/cmms-cache").exists()
        assert not (case.root / ".runtime/cmms-builds").exists()
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize("corruption", ("receipt", "escaping-maven"))
def test_api_build_rejects_unbound_receipt_or_ambient_executable(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    case = _api_build_case(safe_runtime, monkeypatch)
    receipt = case.toolchains
    if corruption == "receipt":
        receipt = replace(receipt, manifest_sha256="e" * 64)
    else:
        executable = receipt.require("maven").home / "bin/mvn"
        ambient = case.root / "ambient-maven-3.6.3"
        ambient.write_bytes(b"ambient executable\n")
        ambient.chmod(0o755)
        executable.unlink()
        executable.symlink_to(ambient)
    runner = ApiBuildRunner(case.root)
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(case.claimed.context, receipt, runner)
        assert runner.calls == []
        assert not (case.root / ".runtime/cmms-cache").exists()
    finally:
        case.claimed.lease.close()


def test_api_build_rejects_a_forged_public_receipt_without_disk_authority(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _api_build_case(safe_runtime, monkeypatch)
    disk_receipt = (
        case.toolchains.require("maven").home
        / ".ifactory-toolchain-receipt.json"
    )
    disk_receipt.write_bytes(b"{}\n")
    disk_receipt.chmod(0o600)
    runner = ApiBuildRunner(case.root)
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert runner.calls == []
    finally:
        case.claimed.lease.close()


def test_api_build_missing_disk_receipt_runs_no_probe_or_maven(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _api_build_case(safe_runtime, monkeypatch)
    (
        case.toolchains.require("maven").home
        / ".ifactory-toolchain-receipt.json"
    ).unlink()
    runner = ApiBuildRunner(case.root)
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert runner.probe_calls == []
        assert runner.calls == []
        assert not (case.root / ".runtime/cmms-cache").exists()
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize("corruption", ("noncanonical", "symlink", "hardlink"))
def test_api_build_rejects_unsafe_disk_receipt_before_any_probe(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    case = _api_build_case(safe_runtime, monkeypatch)
    receipt = (
        case.toolchains.require("maven").home
        / ".ifactory-toolchain-receipt.json"
    )
    if corruption == "noncanonical":
        receipt.write_bytes(b" " + receipt.read_bytes())
    elif corruption == "symlink":
        payload = receipt.read_bytes()
        receipt.unlink()
        outside = case.root / "ambient-toolchain-receipt.json"
        outside.write_bytes(payload)
        outside.chmod(0o600)
        receipt.symlink_to(outside)
    else:
        outside = case.root / "ambient-toolchain-receipt.json"
        os.link(receipt, outside)
    runner = ApiBuildRunner(case.root)
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert runner.probe_calls == []
        assert runner.calls == []
        assert not (case.root / ".runtime/cmms-cache").exists()
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize("invalid", ("nonzero", "string-like", "bool-returncode"))
def test_api_build_requires_exact_successful_probe_result_fields(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    invalid: str,
) -> None:
    case = _api_build_case(safe_runtime, monkeypatch)

    class StringLike:
        def __str__(self) -> str:
            return "Apache Maven 3.9.3"

    if invalid == "nonzero":
        outcome = CommandResult(9, "Apache Maven 3.9.3", "")
    elif invalid == "bool-returncode":
        outcome = CommandResult(False, "Apache Maven 3.9.3", "")
    else:
        outcome = CommandResult(0, StringLike(), "")  # type: ignore[arg-type]
    runner = ApiBuildRunner(case.root, probe_outcomes={"mvn": outcome})
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert runner.calls == []
        assert not (case.root / ".runtime/cmms-cache").exists()
    finally:
        case.claimed.lease.close()


def test_api_build_probe_staleness_stops_before_the_second_probe(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _api_build_case(safe_runtime, monkeypatch)

    def invalidate(call: int, _spec: CommandSpec) -> None:
        if call == 1:
            safe_runtime.transition_application(
                case.claimed.application_path,
                case.claimed.context.application,
                PlanApplicationState.FAILED,
                case.claimed.context.application.claimed_at + timedelta(seconds=1),
            )

    runner = ApiBuildRunner(case.root, probe_mutate=invalidate)
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert len(runner.probe_calls) == 1
        assert runner.calls == []
        assert not (case.root / ".runtime/cmms-cache").exists()
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize("replacement", ("receipt", "maven"))
def test_api_build_rejects_same_content_toolchain_inode_replacement_after_tests(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    case = _api_build_case(safe_runtime, monkeypatch)

    def replace_inode(call: int, _spec: CommandSpec) -> None:
        if call != 1:
            return
        path = (
            case.toolchains.require("maven").home
            / (
                ".ifactory-toolchain-receipt.json"
                if replacement == "receipt"
                else "bin/mvn"
            )
        )
        content = path.read_bytes()
        path.unlink()
        path.write_bytes(content)
        path.chmod(0o600 if replacement == "receipt" else 0o755)

    runner = ApiBuildRunner(case.root, mutate=replace_inode)
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert len(runner.calls) == 1
        assert not (case.root / "components/cmms/api/target").exists()
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize(
    ("tool_name", "executable"),
    (("temurin", "bin/java"), ("maven", "bin/mvn")),
)
def test_api_build_rejects_toolchain_inode_replacement_after_package(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    executable: str,
) -> None:
    case = _api_build_case(safe_runtime, monkeypatch)

    def replace_inode(call: int, _spec: CommandSpec) -> None:
        if call != 2:
            return
        path = case.toolchains.require(tool_name).home / executable
        content = path.read_bytes()
        path.unlink()
        path.write_bytes(content)
        path.chmod(0o755)

    runner = ApiBuildRunner(case.root, mutate=replace_inode)
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert len(runner.calls) == 2
        assert not (case.root / ".runtime/cmms-builds").exists()
    finally:
        case.claimed.lease.close()


def test_api_build_rejects_toolchain_home_path_replacement_during_probe(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _api_build_case(safe_runtime, monkeypatch)

    def replace_home(call: int, _spec: CommandSpec) -> None:
        if call != 1:
            return
        home = case.toolchains.require("temurin").home
        held = home.with_name(f"{home.name}-held")
        home.rename(held)
        shutil.copytree(held, home)

    runner = ApiBuildRunner(case.root, probe_mutate=replace_home)
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert len(runner.probe_calls) == 1
        assert runner.calls == []
        assert not (case.root / ".runtime/cmms-cache").exists()
    finally:
        case.claimed.lease.close()


def test_api_first_toolchain_probe_cannot_write_frontend_generated_tree(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _api_build_case(
        safe_runtime,
        monkeypatch,
        operation=Operation.RESTART_API,
    )

    def mutate_probe(call_count: int, _spec: CommandSpec) -> None:
        if call_count == 1:
            payload = case.root / "components/cmms/frontend/build/probe-owned.bin"
            payload.parent.mkdir(parents=True)
            payload.write_bytes(b"forbidden probe output\n")

    runner = ApiBuildRunner(case.root, probe_mutate=mutate_probe)
    try:
        with pytest.raises(DeploymentError, match="CMMS-E035"):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert len(runner.probe_calls) == 1
        assert runner.calls == []
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize(
    "relative",
    (
        "components/cmms/api/target/probe-owned.bin",
        ".runtime/cmms-cache/maven/probe-owned.bin",
    ),
)
def test_api_initial_probe_cannot_write_owned_output_or_cache(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
) -> None:
    case = _api_build_case(
        safe_runtime,
        monkeypatch,
        operation=Operation.RESTART_API,
    )

    def mutate_probe(call_count: int, _spec: CommandSpec) -> None:
        if call_count == 1:
            payload = case.root / relative
            payload.parent.mkdir(parents=True, exist_ok=True)
            payload.write_bytes(b"forbidden initial probe output\n")

    runner = ApiBuildRunner(case.root, probe_mutate=mutate_probe)
    try:
        with pytest.raises(DeploymentError, match="CMMS-E035"):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert len(runner.probe_calls) == 1
        assert runner.calls == []
        assert not (case.root / ".runtime/cmms-builds").exists()
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize("target", ("api-output", "maven-cache"))
def test_api_final_probe_cannot_change_frozen_output_or_cache(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    case = _api_build_case(
        safe_runtime,
        monkeypatch,
        operation=Operation.RESTART_API,
    )

    def mutate_probe(call_count: int, _spec: CommandSpec) -> None:
        if call_count != 5:
            return
        if target == "api-output":
            payload = case.root / "components/cmms/api/target/app.jar"
            payload.write_bytes(b"forbidden final probe replacement\n")
        else:
            payload = (
                case.root
                / ".runtime/cmms-cache/maven/repository/probe-owned.bin"
            )
            payload.parent.mkdir(parents=True, exist_ok=True)
            payload.write_bytes(b"forbidden final probe cache output\n")

    runner = ApiBuildRunner(case.root, probe_mutate=mutate_probe)
    try:
        with pytest.raises(DeploymentError, match="CMMS-E035"):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert len(runner.probe_calls) == 5
        assert len(runner.calls) == 1
        builds = case.root / ".runtime/cmms-builds"
        assert not builds.exists()
    finally:
        case.claimed.lease.close()


def test_api_final_probe_cannot_change_existing_builds_artifact(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _api_build_case(
        safe_runtime,
        monkeypatch,
        operation=Operation.RESTART_API,
    )
    builds = case.root / ".runtime/cmms-builds"
    builds.mkdir(mode=0o700)
    existing = builds / "app-old.jar"
    existing.write_bytes(b"existing reviewed artifact\n")
    existing.chmod(0o600)

    def mutate_probe(call_count: int, _spec: CommandSpec) -> None:
        if call_count == 5:
            existing.write_bytes(b"forbidden final probe replacement\n")

    runner = ApiBuildRunner(case.root, probe_mutate=mutate_probe)
    try:
        with pytest.raises(DeploymentError, match="CMMS-E035"):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert len(runner.probe_calls) == 5
        assert len(runner.calls) == 1
        assert tuple(builds.iterdir()) == (existing,)
    finally:
        case.claimed.lease.close()


def test_api_toolchain_probe_normalizes_untrusted_deployment_error(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _api_build_case(
        safe_runtime,
        monkeypatch,
        operation=Operation.RESTART_API,
    )

    def explode(_call_count: int, _spec: CommandSpec) -> None:
        raise DeploymentError("FORGED", "/secret/ambient/path", 99)

    runner = ApiBuildRunner(case.root, probe_mutate=explode)
    try:
        with pytest.raises(DeploymentError, match="CMMS-E035") as captured:
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert "/secret/ambient/path" not in str(captured.value)
        assert len(runner.probe_calls) == 1
        assert runner.calls == []
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize(
    "relative",
    (
        "frontend/node_modules/maven-owned.bin",
        "frontend/build/maven-owned.bin",
        "frontend/public/runtime-env.js",
    ),
)
def test_api_maven_cannot_write_frontend_generated_paths(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
) -> None:
    case = _api_build_case(
        safe_runtime,
        monkeypatch,
        operation=Operation.RESTART_API,
    )

    def mutate(call_count: int, _spec: CommandSpec) -> None:
        if call_count == 1:
            payload = case.root / "components/cmms" / relative
            payload.parent.mkdir(parents=True, exist_ok=True)
            payload.write_bytes(b"forbidden maven output\n")

    runner = ApiBuildRunner(case.root, mutate=mutate)
    try:
        with pytest.raises(DeploymentError, match="CMMS-E035"):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert len(runner.calls) == 1
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize("relative", ROOT_PROTECTED_GENERATED_DIRECTORIES)
def test_api_maven_cannot_write_root_approved_generated_tree(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
) -> None:
    case = _api_build_case(
        safe_runtime,
        monkeypatch,
        operation=Operation.RESTART_API,
    )

    def mutate(call_count: int, _spec: CommandSpec) -> None:
        if call_count == 1:
            payload = case.root / relative / "maven-owned.bin"
            payload.parent.mkdir(parents=True, exist_ok=True)
            payload.write_bytes(b"forbidden maven output\n")

    runner = ApiBuildRunner(case.root, mutate=mutate)
    try:
        with pytest.raises(DeploymentError, match="CMMS-E035"):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert len(runner.calls) == 1
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize(
    "relative",
    (
        "maven-owned.bin",
        "cmms-cache/maven-owned.bin",
        "cmms-builds/maven-owned.jar",
    ),
)
def test_api_maven_cannot_write_unowned_runtime_path(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
) -> None:
    case = _api_build_case(
        safe_runtime,
        monkeypatch,
        operation=Operation.RESTART_API,
    )

    def mutate(call_count: int, _spec: CommandSpec) -> None:
        if call_count == 1:
            payload = case.root / ".runtime" / relative
            payload.parent.mkdir(parents=True, exist_ok=True)
            payload.write_bytes(b"forbidden maven output\n")

    runner = ApiBuildRunner(case.root, mutate=mutate)
    try:
        with pytest.raises(DeploymentError, match="CMMS-E035"):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert len(runner.calls) == 1
    finally:
        case.claimed.lease.close()


def test_api_maven_may_populate_only_its_owned_cache_subtree(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _api_build_case(
        safe_runtime,
        monkeypatch,
        operation=Operation.RESTART_API,
    )
    cache_entry = case.root / ".runtime/cmms-cache/maven/repository/entry.bin"

    def mutate(call_count: int, _spec: CommandSpec) -> None:
        if call_count == 1:
            cache_entry.parent.mkdir(parents=True, exist_ok=True)
            cache_entry.write_bytes(b"maven cache entry\n")

    runner = ApiBuildRunner(case.root, mutate=mutate)
    try:
        artifact = build_api_artifact(
            case.claimed.context,
            case.toolchains,
            runner,
        )
        assert artifact.path.is_file()
        assert cache_entry.read_bytes() == b"maven cache entry\n"
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize("anomaly", ("absolute", "escaping", "hardlink"))
def test_api_maven_cache_rejects_external_linkage(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    anomaly: str,
) -> None:
    case = _api_build_case(
        safe_runtime,
        monkeypatch,
        operation=Operation.RESTART_API,
    )
    cache_link = case.root / ".runtime/cmms-cache/maven/cache-link"
    external = case.root.parent / f"{case.root.name}-ambient-maven-cache"
    external.write_bytes(b"ambient maven cache payload\n")

    def mutate(call_count: int, _spec: CommandSpec) -> None:
        if call_count != 1:
            return
        cache_link.parent.mkdir(parents=True, exist_ok=True)
        if anomaly == "absolute":
            cache_link.symlink_to(external)
        elif anomaly == "escaping":
            cache_link.symlink_to("../../../../ambient-maven-cache")
        else:
            os.link(external, cache_link)

    runner = ApiBuildRunner(case.root, mutate=mutate)
    try:
        with pytest.raises(DeploymentError, match="CMMS-E035"):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert len(runner.calls) == 1
        assert not (case.root / ".runtime/cmms-builds").exists()
    finally:
        case.claimed.lease.close()
        external.unlink(missing_ok=True)


def test_api_maven_cache_accepts_contained_relative_symlink(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _api_build_case(
        safe_runtime,
        monkeypatch,
        operation=Operation.RESTART_API,
    )

    def mutate(call_count: int, _spec: CommandSpec) -> None:
        if call_count == 1:
            repository = case.root / ".runtime/cmms-cache/maven/repository"
            target = repository / "package/data.bin"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"contained cache payload\n")
            (repository / "current").symlink_to("package/data.bin")

    runner = ApiBuildRunner(case.root, mutate=mutate)
    try:
        artifact = build_api_artifact(
            case.claimed.context,
            case.toolchains,
            runner,
        )
        assert artifact.path.is_file()
    finally:
        case.claimed.lease.close()


def test_api_build_accepts_unchanged_existing_frontend_generated_layout(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _api_build_case(
        safe_runtime,
        monkeypatch,
        operation=Operation.RESTART_API,
    )
    frontend = case.root / "components/cmms/frontend"
    package = frontend / "node_modules/package/bin/tool.js"
    package.parent.mkdir(parents=True)
    package.write_bytes(b"reviewed dependency\n")
    hardlink = frontend / "node_modules/package/bin/tool-hardlink.js"
    os.link(package, hardlink)
    executable_link = frontend / "node_modules/.bin/tool"
    executable_link.parent.mkdir(parents=True)
    executable_link.symlink_to("../package/bin/tool.js")
    build = frontend / "build/index.html"
    build.parent.mkdir(parents=True)
    build.write_bytes(b"reviewed build\n")
    runtime_env = frontend / "public/runtime-env.js"
    runtime_env.write_bytes(b"reviewed runtime env\n")

    runner = ApiBuildRunner(case.root)
    try:
        artifact = build_api_artifact(
            case.claimed.context,
            case.toolchains,
            runner,
        )
        assert artifact.path.exists()
        assert executable_link.readlink() == Path("../package/bin/tool.js")
        assert hardlink.stat().st_ino == package.stat().st_ino
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize("descriptor", ("settings", "pom-duplicate", "pom-symlink"))
def test_api_build_rejects_unreviewed_or_unsafe_descriptors_before_runner(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    descriptor: str,
) -> None:
    case = _api_build_case(safe_runtime, monkeypatch)
    if descriptor == "settings":
        (case.root / "deploy/cmms/maven-settings.xml").write_bytes(b"<settings/>\n")
    else:
        pom = case.root / "components/cmms/api/pom.xml"
        if descriptor == "pom-duplicate":
            pom.write_text(
                "<project><finalName>app</finalName><finalName>other</finalName></project>\n",
                encoding="utf-8",
            )
        else:
            replacement = case.root / "replacement-pom.xml"
            replacement.write_text(
                "<project><finalName>app</finalName></project>\n",
                encoding="utf-8",
            )
            pom.unlink()
            pom.symlink_to(replacement)
    runner = ApiBuildRunner(case.root)
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert runner.calls == []
        assert not (case.root / ".runtime/cmms-cache").exists()
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize(
    "candidate_kind",
    ("missing", "symlink", "hardlink", "empty", "extra"),
)
def test_api_build_rejects_every_unsafe_direct_jar_candidate(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    candidate_kind: str,
) -> None:
    case = _api_build_case(safe_runtime, monkeypatch)
    runner = ApiBuildRunner(case.root, candidate_kind=candidate_kind)
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert len(runner.calls) == 2
        builds = case.root / ".runtime/cmms-builds"
        assert not builds.exists() or tuple(builds.iterdir()) == ()
    finally:
        case.claimed.lease.close()


def test_api_build_rejects_a_nested_competing_jar(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _api_build_case(safe_runtime, monkeypatch)
    runner = ApiBuildRunner(case.root, candidate_kind="nested")
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert len(runner.calls) == 2
        assert not (case.root / ".runtime/cmms-builds").exists()
    finally:
        case.claimed.lease.close()


def test_api_build_rejects_target_tree_beyond_fixed_depth_limit(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _api_build_case(safe_runtime, monkeypatch)
    runner = ApiBuildRunner(case.root, candidate_kind="deep")
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert len(runner.calls) == 2
        assert not (case.root / ".runtime/cmms-builds").exists()
    finally:
        case.claimed.lease.close()


def test_api_build_rejects_oversized_jar_without_copying(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _api_build_case(safe_runtime, monkeypatch)
    monkeypatch.setattr(source_module, "_MAX_API_ARTIFACT_BYTES", 4)
    runner = ApiBuildRunner(case.root, artifact=b"12345")
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert len(runner.calls) == 2
        assert not (case.root / ".runtime/cmms-builds").exists()
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize("mutation", ("source", "pom"))
def test_api_build_rejects_runner_source_or_pom_mutation_before_package(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    case = _api_build_case(safe_runtime, monkeypatch)
    current_source = [case.source]
    monkeypatch.setattr(
        SourceInspector,
        "capture",
        lambda _self, _root, _profile: current_source[0],
    )

    def mutate(_call: int, _spec: CommandSpec) -> None:
        if mutation == "source":
            current_source[0] = replace(
                case.source,
                cmms_dirty_fingerprint="d" * 64,
            )
        else:
            (case.root / "components/cmms/api/pom.xml").write_text(
                "<project><build><finalName>other</finalName></build></project>\n",
                encoding="utf-8",
            )

    runner = ApiBuildRunner(case.root, mutate=mutate)
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert len(runner.calls) == 1
        assert not (case.root / "components/cmms/api/target").exists()
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize(
    "outcome",
    (
        object(),
        CommandResult(9, "", "failed"),
        CommandResult(False, "", ""),
        CommandResult(0, object(), ""),
    ),
)
def test_api_build_rejects_malformed_or_nonzero_runner_result(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    outcome: object,
) -> None:
    case = _api_build_case(
        safe_runtime,
        monkeypatch,
        operation=Operation.RESTART_API,
    )
    runner = ApiBuildRunner(case.root, outcomes=(outcome,))
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert len(runner.calls) == 1
        assert not (case.root / ".runtime/cmms-builds").exists()
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize(
    "raised",
    (
        RuntimeError("ambient runner detail"),
        DeploymentError("CMMS-E099", "ambient deployment detail", 99),
    ),
)
def test_api_build_normalizes_every_runner_exception(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    raised: BaseException,
) -> None:
    case = _api_build_case(
        safe_runtime,
        monkeypatch,
        operation=Operation.RESTART_API,
    )

    def explode(_call: int, _spec: CommandSpec) -> None:
        raise raised

    runner = ApiBuildRunner(case.root, mutate=explode)
    try:
        with pytest.raises(DeploymentError, match="CMMS-E035") as captured:
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert "ambient" not in str(captured.value)
        assert len(runner.calls) == 1
        assert not (case.root / ".runtime/cmms-builds").exists()
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize("directory", ("cache-root", "maven-cache"))
@pytest.mark.parametrize("mutation", ("mode", "symlink"))
def test_api_maven_cannot_weaken_or_replace_private_cache_directory(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    directory: str,
    mutation: str,
) -> None:
    case = _api_build_case(
        safe_runtime,
        monkeypatch,
        operation=Operation.RESTART_API,
    )

    def mutate(_call: int, _spec: CommandSpec) -> None:
        cache_root = case.root / ".runtime/cmms-cache"
        target = cache_root if directory == "cache-root" else cache_root / "maven"
        if mutation == "mode":
            target.chmod(0o777)
            return
        if directory == "cache-root":
            (cache_root / "maven").rmdir()
        target.rmdir()
        ambient = case.root / f"ambient-{directory}"
        ambient.mkdir(mode=0o700)
        target.symlink_to(ambient, target_is_directory=True)

    runner = ApiBuildRunner(case.root, mutate=mutate)
    try:
        with pytest.raises(DeploymentError, match="CMMS-E035"):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert len(runner.calls) == 1
        assert not (case.root / ".runtime/cmms-builds").exists()
    finally:
        case.claimed.lease.close()


def test_api_stops_between_cache_directory_ensures_when_claim_expires(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _api_build_case(
        safe_runtime,
        monkeypatch,
        operation=Operation.RESTART_API,
    )
    original_ensure = source_module._ensure_private_directory
    ensure_calls = 0

    def invalidate_after_first_ensure(path: Path) -> Path:
        nonlocal ensure_calls
        result = original_ensure(path)
        ensure_calls += 1
        if ensure_calls == 1:
            safe_runtime.transition_application(
                case.claimed.application_path,
                case.claimed.context.application,
                PlanApplicationState.FAILED,
                case.claimed.context.application.claimed_at
                + timedelta(seconds=1),
            )
        return result

    monkeypatch.setattr(
        source_module,
        "_ensure_private_directory",
        invalidate_after_first_ensure,
    )
    runner = ApiBuildRunner(case.root)
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert ensure_calls == 1
        assert (case.root / ".runtime/cmms-cache").is_dir()
        assert not (case.root / ".runtime/cmms-cache/maven").exists()
        assert runner.calls == []
        assert len(runner.probe_calls) == 4
    finally:
        case.claimed.lease.close()


def test_api_build_rejects_candidate_path_replacement_during_copy(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _api_build_case(safe_runtime, monkeypatch)
    runner = ApiBuildRunner(case.root, artifact=b"candidate-race\n")
    original_pread = os.pread
    raced = False

    def racing_pread(fd: int, length: int, offset: int) -> bytes:
        nonlocal raced
        data = original_pread(fd, length, offset)
        if not raced and data:
            raced = True
            candidate = case.root / "components/cmms/api/target/app.jar"
            candidate.rename(candidate.with_name("held-original.bin"))
            candidate.write_bytes(b"racing-replacement\n")
        return data

    monkeypatch.setattr(source_module.os, "pread", racing_pread)
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert raced is True
        builds = case.root / ".runtime/cmms-builds"
        assert not builds.exists() or tuple(builds.iterdir()) == ()
    finally:
        case.claimed.lease.close()


def test_api_build_reuses_only_a_matching_existing_artifact(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _api_build_case(safe_runtime, monkeypatch)
    payload = b"already-reviewed-artifact\n"
    digest = hashlib.sha256(payload).hexdigest()
    builds = case.root / ".runtime/cmms-builds"
    builds.mkdir(mode=0o700)
    existing = builds / f"app-{digest}.jar"
    existing.write_bytes(payload)
    existing.chmod(0o600)
    before = existing.stat()
    original_fsync = os.fsync
    directory_fsyncs = 0

    def recording_fsync(fd: int) -> None:
        nonlocal directory_fsyncs
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_fsyncs += 1
        original_fsync(fd)

    monkeypatch.setattr(source_module.os, "fsync", recording_fsync)
    runner = ApiBuildRunner(case.root, artifact=payload)
    try:
        artifact = build_api_artifact(case.claimed.context, case.toolchains, runner)
        after = existing.stat()
        assert artifact.path == existing
        assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
        assert existing.read_bytes() == payload
        assert directory_fsyncs >= 1
    finally:
        case.claimed.lease.close()


def test_api_build_atomic_publish_never_overwrites_a_racing_target(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _api_build_case(safe_runtime, monkeypatch)
    payload = b"publish-race-source\n"
    digest = hashlib.sha256(payload).hexdigest()
    final = case.root / ".runtime/cmms-builds" / f"app-{digest}.jar"
    sentinel = b"racing-target-must-survive\n"
    original_link = os.link
    raced = False

    def racing_link(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal raced
        if not raced:
            raced = True
            final.write_bytes(sentinel)
            final.chmod(0o600)
        original_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(source_module.os, "link", racing_link)
    runner = ApiBuildRunner(case.root, artifact=payload)
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert raced is True
        assert final.read_bytes() == sentinel
        assert tuple(path.name for path in final.parent.iterdir()) == (final.name,)
    finally:
        case.claimed.lease.close()


def test_api_build_uses_anonymous_staging_and_only_a_procfd_link(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _api_build_case(safe_runtime, monkeypatch)
    payload = b"descriptor-bound-publish\n"
    digest = hashlib.sha256(payload).hexdigest()
    final = case.root / ".runtime/cmms-builds" / f"app-{digest}.jar"
    original_link = os.link
    link_sources: list[str] = []

    def inspect_link(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        link_sources.append(os.fspath(src))
        assert tuple(final.parent.glob(".app-*.tmp")) == ()
        original_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(source_module.os, "link", inspect_link)
    try:
        artifact = build_api_artifact(
            case.claimed.context,
            case.toolchains,
            ApiBuildRunner(case.root, artifact=payload),
        )
        assert len(link_sources) == 1
        assert link_sources[0].startswith("/proc/self/fd/")
        assert artifact.path == final
        assert final.read_bytes() == payload
        assert tuple(final.parent.glob(".app-*.tmp")) == ()
    finally:
        case.claimed.lease.close()


def test_api_build_retains_exact_candidate_when_first_post_link_stat_fails(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _api_build_case(safe_runtime, monkeypatch)
    payload = b"post-link-stat-failure\n"
    digest = hashlib.sha256(payload).hexdigest()
    builds = case.root / ".runtime/cmms-builds"
    final = builds / f"app-{digest}.jar"
    original_link = os.link
    original_stat = os.stat
    original_fsync = os.fsync
    link_sources: list[str] = []
    linked = False
    stat_failed = False
    directory_fsyncs = 0

    def recording_link(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal linked
        link_sources.append(os.fspath(src))
        original_link(src, dst, *args, **kwargs)
        linked = True

    def fail_first_final_stat(
        path: Any,
        *args: Any,
        **kwargs: Any,
    ) -> os.stat_result:
        nonlocal stat_failed
        if (
            linked
            and not stat_failed
            and os.fspath(path) == final.name
            and kwargs.get("dir_fd") is not None
            and kwargs.get("follow_symlinks") is False
        ):
            stat_failed = True
            raise OSError(errno.EIO, "injected post-link stat failure")
        return original_stat(path, *args, **kwargs)

    def recording_fsync(fd: int) -> None:
        nonlocal directory_fsyncs
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_fsyncs += 1
        original_fsync(fd)

    monkeypatch.setattr(source_module.os, "link", recording_link)
    monkeypatch.setattr(source_module.os, "stat", fail_first_final_stat)
    monkeypatch.setattr(source_module.os, "fsync", recording_fsync)
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(
                case.claimed.context,
                case.toolchains,
                ApiBuildRunner(case.root, artifact=payload),
            )
        assert linked is True
        assert stat_failed is True
        assert len(link_sources) == 1
        assert link_sources[0].startswith("/proc/self/fd/")
        assert final.read_bytes() == payload
        assert tuple(builds.iterdir()) == (final,)
        assert directory_fsyncs == 0

        artifact = build_api_artifact(
            case.claimed.context,
            case.toolchains,
            ApiBuildRunner(case.root, artifact=payload),
        )
        assert artifact.path == final
        assert final.read_bytes() == payload
        assert directory_fsyncs >= 1
    finally:
        case.claimed.lease.close()


def test_api_build_procfd_link_failure_has_no_pathname_fallback(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _api_build_case(safe_runtime, monkeypatch)
    link_sources: list[str] = []

    def unavailable_procfd_link(
        src: Any,
        _dst: Any,
        *_args: Any,
        **_kwargs: Any,
    ) -> None:
        link_sources.append(os.fspath(src))
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(source_module.os, "link", unavailable_procfd_link)
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(
                case.claimed.context,
                case.toolchains,
                ApiBuildRunner(case.root),
            )
        assert len(link_sources) == 1
        assert link_sources[0].startswith("/proc/self/fd/")
        builds = case.root / ".runtime/cmms-builds"
        assert builds.is_dir()
        assert tuple(builds.iterdir()) == ()
    finally:
        case.claimed.lease.close()


def test_api_build_fails_closed_when_anonymous_staging_is_unsupported(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _api_build_case(safe_runtime, monkeypatch)
    original_open = os.open
    original_link = os.link
    tmpfile_attempted = False
    link_sources: list[str] = []

    def unsupported_tmpfile(
        path: Any,
        flags: int,
        *args: Any,
        **kwargs: Any,
    ) -> int:
        nonlocal tmpfile_attempted
        if (flags & os.O_TMPFILE) == os.O_TMPFILE:
            tmpfile_attempted = True
            raise OSError(errno.EOPNOTSUPP, "anonymous files unsupported")
        return original_open(path, flags, *args, **kwargs)

    def recording_link(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        link_sources.append(os.fspath(src))
        original_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(source_module.os, "open", unsupported_tmpfile)
    monkeypatch.setattr(source_module.os, "link", recording_link)
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(
                case.claimed.context,
                case.toolchains,
                ApiBuildRunner(case.root),
            )
        assert tmpfile_attempted is True
        assert link_sources == []
        builds = case.root / ".runtime/cmms-builds"
        assert builds.is_dir()
        assert tuple(builds.iterdir()) == ()
    finally:
        case.claimed.lease.close()


def test_api_build_rejects_builds_mode_change_after_private_ensure(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _api_build_case(safe_runtime, monkeypatch)
    original_ensure = source_module._ensure_private_directory
    changed = False

    def change_mode_after_ensure(path: Path) -> Path:
        nonlocal changed
        result = original_ensure(path)
        if path.name == "cmms-builds":
            result.chmod(0o755)
            changed = True
        return result

    monkeypatch.setattr(
        source_module,
        "_ensure_private_directory",
        change_mode_after_ensure,
    )
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(
                case.claimed.context,
                case.toolchains,
                ApiBuildRunner(case.root),
            )
        assert changed is True
        builds = case.root / ".runtime/cmms-builds"
        assert builds.is_dir()
        assert tuple(builds.iterdir()) == ()
    finally:
        case.claimed.lease.close()


def test_api_build_preserves_external_final_replacement_after_link(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _api_build_case(safe_runtime, monkeypatch)
    payload = b"published-inode-replacement\n"
    digest = hashlib.sha256(payload).hexdigest()
    builds = case.root / ".runtime/cmms-builds"
    final = builds / f"app-{digest}.jar"
    sentinel = b"external-final-must-survive\n"
    original_link = os.link
    replaced = False

    def replace_final(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal replaced
        original_link(src, dst, *args, **kwargs)
        if not replaced:
            replaced = True
            final.rename(builds / "relocated-owned-inode.jar")
            final.write_bytes(sentinel)
            final.chmod(0o600)

    monkeypatch.setattr(source_module.os, "link", replace_final)
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(
                case.claimed.context,
                case.toolchains,
                ApiBuildRunner(case.root, artifact=payload),
            )
        assert replaced is True
        assert final.read_bytes() == sentinel
    finally:
        case.claimed.lease.close()


def test_api_build_reverifies_published_artifact_after_finalize(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _api_build_case(safe_runtime, monkeypatch)
    payload = b"published-final-verification\n"
    replacement = b"post-finalize-replacement\n"
    original_finalize = source_module._PreparedApiCandidate.finalize
    replaced = False

    def finalize_then_replace(candidate: Any) -> Any:
        nonlocal replaced
        binding = original_finalize(candidate)
        binding.path.unlink()
        binding.path.write_bytes(replacement)
        binding.path.chmod(0o600)
        replaced = True
        return binding

    monkeypatch.setattr(
        source_module._PreparedApiCandidate,
        "finalize",
        finalize_then_replace,
    )
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(
                case.claimed.context,
                case.toolchains,
                ApiBuildRunner(case.root, artifact=payload),
            )
        assert replaced is True
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize(
    "post_finalize_change",
    ("source", "sensitive", "action-during-state-check"),
)
def test_api_finalize_revalidates_full_state_and_action_before_published_check(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    post_finalize_change: str,
) -> None:
    case = _api_build_case(safe_runtime, monkeypatch)
    original_finalize = source_module._PreparedApiCandidate.finalize
    original_verify_published = source_module._PreparedApiCandidate.verify_published
    finalized = False
    verify_published_called = False
    action_invalidated = False

    def finalize_then_arm(candidate: Any) -> Any:
        nonlocal finalized
        binding = original_finalize(candidate)
        finalized = True
        return binding

    def capture_source(_self: Any, _root: Path, _profile: RuntimeProfile) -> Any:
        nonlocal action_invalidated
        if finalized and post_finalize_change == "source":
            return replace(case.source, cmms_dirty_fingerprint="a" * 64)
        if (
            finalized
            and post_finalize_change == "action-during-state-check"
            and not action_invalidated
        ):
            action_invalidated = True
            safe_runtime.transition_application(
                case.claimed.application_path,
                case.claimed.context.application,
                PlanApplicationState.FAILED,
                case.claimed.context.application.claimed_at
                + timedelta(seconds=1),
            )
        return case.source

    def capture_sensitive(_root: Path, manifest: SensitiveManifest) -> Any:
        changed = finalized and post_finalize_change == "sensitive"
        return SensitiveBaselineResult(
            not changed,
            (
                "SENSITIVE_BASELINE_CHANGED"
                if changed
                else "SENSITIVE_BASELINE_MATCHED"
            ),
            manifest.sha256,
            len(manifest.files),
            0,
        )

    def record_verify_published(candidate: Any, binding: Any) -> None:
        nonlocal verify_published_called
        verify_published_called = True
        original_verify_published(candidate, binding)

    monkeypatch.setattr(
        source_module._PreparedApiCandidate,
        "finalize",
        finalize_then_arm,
    )
    monkeypatch.setattr(SourceInspector, "capture", capture_source)
    monkeypatch.setattr(
        source_module,
        "verify_sensitive_baseline",
        capture_sensitive,
    )
    monkeypatch.setattr(
        source_module._PreparedApiCandidate,
        "verify_published",
        record_verify_published,
    )
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(
                case.claimed.context,
                case.toolchains,
                ApiBuildRunner(case.root),
            )
        assert finalized is True
        assert verify_published_called is False
        assert action_invalidated is (
            post_finalize_change == "action-during-state-check"
        )
    finally:
        case.claimed.lease.close()


def test_api_build_sets_private_modes_independently_of_ambient_umask(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _api_build_case(safe_runtime, monkeypatch)
    runner = ApiBuildRunner(case.root)
    previous_umask = os.umask(0o777)
    try:
        artifact = build_api_artifact(case.claimed.context, case.toolchains, runner)
    finally:
        os.umask(previous_umask)
        case.claimed.lease.close()
    assert stat.S_IMODE(
        (case.root / ".runtime/cmms-cache/maven").stat().st_mode
    ) == 0o700
    assert stat.S_IMODE(artifact.path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "operation",
    (Operation.REPAIR, Operation.RESTART_API),
)
def test_api_build_stops_after_a_runner_invalidates_the_live_claim(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    operation: Operation,
) -> None:
    case = _api_build_case(
        safe_runtime,
        monkeypatch,
        operation=operation,
    )
    invalidated = False

    def invalidate(_call: int, _spec: CommandSpec) -> None:
        nonlocal invalidated
        if invalidated:
            return
        invalidated = True
        safe_runtime.transition_application(
            case.claimed.application_path,
            case.claimed.context.application,
            PlanApplicationState.FAILED,
            case.claimed.context.application.claimed_at + timedelta(seconds=1),
        )

    runner = ApiBuildRunner(case.root, mutate=invalidate)
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert len(runner.calls) == 1
        assert not (case.root / ".runtime/cmms-builds").exists()
        if operation is Operation.REPAIR:
            assert not (case.root / "components/cmms/api/target").exists()
        else:
            assert (case.root / "components/cmms/api/target/app.jar").exists()
    finally:
        case.claimed.lease.close()


def test_api_build_initial_source_capture_staleness_creates_no_cache(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _api_build_case(safe_runtime, monkeypatch)
    invalidated = False

    def capture(_self: SourceInspector, _root: Path, _profile: RuntimeProfile) -> SourceBinding:
        nonlocal invalidated
        if not invalidated:
            invalidated = True
            safe_runtime.transition_application(
                case.claimed.application_path,
                case.claimed.context.application,
                PlanApplicationState.FAILED,
                case.claimed.context.application.claimed_at + timedelta(seconds=1),
            )
        return case.source

    monkeypatch.setattr(SourceInspector, "capture", capture)
    runner = ApiBuildRunner(case.root)
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert runner.calls == []
        assert not (case.root / ".runtime/cmms-cache").exists()
    finally:
        case.claimed.lease.close()


def test_api_build_final_source_capture_staleness_never_returns_artifact(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _api_build_case(safe_runtime, monkeypatch)
    capture_count = 0

    def capture(_self: SourceInspector, _root: Path, _profile: RuntimeProfile) -> SourceBinding:
        nonlocal capture_count
        capture_count += 1
        if capture_count == 4:
            safe_runtime.transition_application(
                case.claimed.application_path,
                case.claimed.context.application,
                PlanApplicationState.FAILED,
                case.claimed.context.application.claimed_at + timedelta(seconds=1),
            )
        return case.source

    monkeypatch.setattr(SourceInspector, "capture", capture)
    runner = ApiBuildRunner(case.root)
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(case.claimed.context, case.toolchains, runner)
        assert capture_count == 4
    finally:
        case.claimed.lease.close()


def test_api_build_final_source_capture_observes_published_artifact(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _api_build_case(safe_runtime, monkeypatch)
    payload = b"post-publish-integrity\n"
    digest = hashlib.sha256(payload).hexdigest()
    builds = case.root / ".runtime/cmms-builds"
    final = builds / f"app-{digest}.jar"
    capture_count = 0
    observed_unpublished = False
    observed_published = False

    def capture(
        _self: SourceInspector,
        _root: Path,
        _profile: RuntimeProfile,
    ) -> SourceBinding:
        nonlocal capture_count, observed_published, observed_unpublished
        capture_count += 1
        if final.exists():
            observed_published = True
            assert final.read_bytes() == payload
        else:
            observed_unpublished = True
        assert tuple(builds.glob(".app-*.tmp")) == ()
        return case.source

    monkeypatch.setattr(SourceInspector, "capture", capture)
    try:
        artifact = build_api_artifact(
            case.claimed.context,
            case.toolchains,
            ApiBuildRunner(case.root, artifact=payload),
        )
        assert capture_count > 0
        assert observed_unpublished is True
        assert observed_published is True
        assert artifact.path == final
        assert final.read_bytes() == payload
    finally:
        case.claimed.lease.close()


def test_api_build_never_unlinks_content_addressed_final_after_link(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _api_build_case(safe_runtime, monkeypatch)
    payload = b"cleanup-race-owned-inode\n"
    digest = hashlib.sha256(payload).hexdigest()
    builds = case.root / ".runtime/cmms-builds"
    final = builds / f"app-{digest}.jar"
    original_fsync = os.fsync
    original_link = os.link
    original_unlink = os.unlink
    linked = False
    fsync_failed = False
    final_unlinks = 0

    def recording_link(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal linked
        original_link(src, dst, *args, **kwargs)
        linked = True

    def fail_first_post_link_fsync(fd: int) -> None:
        nonlocal fsync_failed
        if linked and not fsync_failed:
            fsync_failed = True
            raise OSError(errno.EIO, "injected post-link fsync failure")
        original_fsync(fd)

    def record_final_unlink(
        path: Any,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        nonlocal final_unlinks
        if linked and os.fspath(path) == final.name:
            final_unlinks += 1
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(source_module.os, "link", recording_link)
    monkeypatch.setattr(source_module.os, "fsync", fail_first_post_link_fsync)
    monkeypatch.setattr(source_module.os, "unlink", record_final_unlink)
    try:
        with pytest.raises(DeploymentError):
            build_api_artifact(
                case.claimed.context,
                case.toolchains,
                ApiBuildRunner(case.root, artifact=payload),
            )
        assert linked is True
        assert fsync_failed is True
        assert final_unlinks == 0
        assert final.read_bytes() == payload
    finally:
        case.claimed.lease.close()


@dataclass(frozen=True)
class FrontendDependencyCase:
    root: Path
    claimed: ClaimedFixture
    toolchains: ToolchainReceipt
    source: SourceBinding
    sensitive_manifest: SensitiveManifest
    lock_sha256: str


@dataclass
class FrontendDependencyRunner:
    root: Path
    outcomes: tuple[object, ...] = ()
    mutate: Any | None = None
    probe_mutate: Any | None = None
    probe_outcomes: dict[str, object] | None = None
    calls: list[CommandSpec] = None  # type: ignore[assignment]
    probe_calls: list[CommandSpec] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.calls = []
        self.probe_calls = []

    def run(self, spec: CommandSpec) -> object:
        executable_name = Path(spec.argv[0]).name
        if spec.safe_label.startswith("toolchain-"):
            self.probe_calls.append(spec)
            if self.probe_mutate is not None:
                self.probe_mutate(len(self.probe_calls), spec)
            output = {
                "java": 'openjdk version "17.0.19"',
                "mvn": "Apache Maven 3.9.3",
                "node": "v21.6.1",
                "npm": "10.2.4",
            }[executable_name]
            if self.probe_outcomes and executable_name in self.probe_outcomes:
                return self.probe_outcomes[executable_name]
            return CommandResult(
                0,
                output if executable_name != "java" else "",
                output if executable_name == "java" else "",
            )

        self.calls.append(spec)
        frontend = self.root / "components/cmms/frontend"
        if spec.safe_label == "cmms-frontend-npm-ci":
            generated = frontend / "node_modules/fixture/index.js"
            generated.parent.mkdir(parents=True, exist_ok=True)
            generated.write_bytes(b"generated dependency\n")
        elif spec.safe_label == "cmms-frontend-build":
            build = frontend / "build/index.html"
            build.parent.mkdir(parents=True, exist_ok=True)
            build.write_bytes(b"generated build\n")
            runtime_env = frontend / "public/runtime-env.js"
            runtime_env.parent.mkdir(parents=True, exist_ok=True)
            runtime_env.write_bytes(b"generated runtime env\n")
        if self.mutate is not None:
            self.mutate(len(self.calls), spec)
        if self.outcomes:
            return self.outcomes[len(self.calls) - 1]
        return CommandResult(0, "", "")


def _frontend_dependency_case(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    *,
    include_action: bool = True,
    profile: RuntimeProfile = RuntimeProfile.DEVELOPMENT,
    toolchain_snapshot_sha256: str | None = None,
    sensitive_snapshot_sha256: str | None = None,
) -> FrontendDependencyCase:
    root = safe_runtime.root
    cmms = root / "components/cmms"
    frontend = cmms / "frontend"
    migrations = {
        "api/src/main/resources/db/V1__init.sql": b"create table example();\n",
        "api/src/main/resources/db/repeatable/R__view.sql": b"select 1;\n",
    }
    sensitive_files = {
        "api/pom.xml": b"<project/>\n",
        "frontend/package-lock.json": b'{"lockfileVersion":3}\n',
        "frontend/package.json": b'{"name":"fixture-frontend"}\n',
        "frontend/src/config.ts": b"export const api = '/api';\n",
    }
    tracked_files = {
        **sensitive_files,
        **migrations,
        "api/.gitignore": b"/target/\n",
        "frontend/src/App.tsx": b"export default function App() { return null; }\n",
        ".gitignore": (
            b"/frontend/node_modules/\n"
            b"/frontend/build/\n"
            b"/frontend/public/runtime-env.js\n"
        ),
    }
    for relative, data in tracked_files.items():
        target = cmms / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    _git(cmms, "init", "--quiet")
    _git(cmms, "add", "--all")
    _commit(cmms, "reviewed frontend fixture")
    cmms_head = _git(cmms, "rev-parse", "HEAD").strip().decode("ascii")

    migration_lines = b"".join(
        hashlib.sha256(data).hexdigest().encode("ascii")
        + b"  "
        + relative.encode("utf-8")
        + b"\n"
        for relative, data in sorted(migrations.items())
    )
    sensitive_raw = {
        "cmms_gitlink": cmms_head,
        "files": [
            {
                "path": relative,
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            for relative, data in sorted(sensitive_files.items())
        ],
        "migration_tree": {
            "path": "api/src/main/resources/db",
            "sha256": hashlib.sha256(migration_lines).hexdigest(),
        },
        "schema_version": 1,
    }
    manifests = root / "deploy/cmms/manifests"
    manifests.mkdir(parents=True)
    shutil.copyfile(TOOLCHAIN_MANIFEST, manifests / "toolchains.json")
    sensitive_bytes = canonical_json_bytes(sensitive_raw)
    (manifests / "startup-sensitive-files.json").write_bytes(sensitive_bytes)
    sensitive_sha256 = hashlib.sha256(sensitive_bytes).hexdigest()
    monkeypatch.setattr(
        source_module,
        "_REVIEWED_SENSITIVE_MANIFEST_SHA256",
        sensitive_sha256,
    )
    sensitive = SensitiveManifest.load(
        manifests / "startup-sensitive-files.json"
    )

    (root / ".gitignore").write_bytes(b"/.runtime/\n")
    _git(root, "init", "--quiet")
    _git(root, "add", "--all")
    _commit(root, "reviewed platform fixture")

    _git(cmms, "config", "extensions.worktreeConfig", "true")
    _git(cmms, "config", "--add", "core.hooksPath", ".hooks-local")
    _git(
        cmms,
        "config",
        "--worktree",
        "--add",
        "core.hooksPath",
        ".hooks-worktree",
    )
    source = SourceInspector().capture(root, profile)

    manifest = ToolchainManifest.load(manifests / "toolchains.json")
    installed: list[InstalledToolchain] = []
    for definition in manifest.tools:
        home = root / ".runtime/toolchains" / f"{definition.name}-{definition.version}"
        for probe in definition.probes:
            executable = home / probe.executable
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_bytes(b"fixture executable\n")
            executable.chmod(0o755)
        home.chmod(0o700)
        receipt_path = home / ".ifactory-toolchain-receipt.json"
        receipt_path.write_bytes(
            canonical_json_bytes(
                {
                    "archive_sha256": definition.sha256,
                    "manifest_sha256": manifest.sha256,
                    "name": definition.name,
                    "record_type": "cmms-toolchain-installation",
                    "schema_version": 1,
                    "version": definition.version,
                }
            )
        )
        receipt_path.chmod(0o600)
        installed.append(
            InstalledToolchain(
                name=definition.name,
                version=definition.version,
                archive_sha256=definition.sha256,
                home=home,
            )
        )
    (root / ".runtime/toolchains").chmod(0o700)
    toolchains = ToolchainReceipt(manifest.sha256, tuple(installed))

    snapshot = replace(
        safe_runtime.make_snapshot(),
        source=source,
        toolchain_manifest_sha256=(
            manifest.sha256
            if toolchain_snapshot_sha256 is None
            else toolchain_snapshot_sha256
        ),
        sensitive_manifest_sha256=(
            sensitive.sha256
            if sensitive_snapshot_sha256 is None
            else sensitive_snapshot_sha256
        ),
    )
    actions = _repair_actions(
        *((ActionCode.FRONTEND_VERIFY,) if include_action else ())
    )
    plan = DeploymentPlan.create(
        snapshot=snapshot,
        operation=Operation.REPAIR,
        profile=profile,
        license_mode=LicenseMode.OFFLINE,
        bootstrap_bindings=safe_runtime.bootstrap_plan_bindings,
        actions=actions,
        now=safe_runtime.make_plan().created_at,
        plan_nonce="7" * 32,
    )
    plan_path, plan_hash = write_plan(plan, safe_runtime.plans_dir)
    reservation = reserve_plan_attempt(
        plan_path,
        plan_hash,
        plan.created_at,
        safe_runtime.plans_dir,
        application_id="6" * 32,
    )
    lease = safe_runtime.acquire_deployment_write_lease(reservation)
    confirmed = load_confirmed_plan(
        reservation,
        snapshot=plan.snapshot,
        current_bootstrap_bindings=plan.bootstrap_bindings,
        lease=lease,
    )
    evidence = safe_runtime.gateway_authority(confirmed, lease).issue()
    context = claim_plan_application(
        confirmed,
        safe_runtime.plans_dir,
        lease,
        evidence,
    )
    return FrontendDependencyCase(
        root=root,
        claimed=ClaimedFixture(context, reservation.application_path, lease),
        toolchains=toolchains,
        source=source,
        sensitive_manifest=sensitive,
        lock_sha256=hashlib.sha256(
            sensitive_files["frontend/package-lock.json"]
        ).hexdigest(),
    )


class RunnerDescriptor:
    def __init__(self, delegate: Any, on_lookup: Any) -> None:
        self.delegate = delegate
        self.on_lookup = on_lookup
        self.lookups = 0

    @property
    def run(self) -> Any:
        self.lookups += 1
        self.on_lookup(self.lookups)
        return self.delegate.run


@pytest.mark.parametrize("flow", ("api", "frontend"))
def test_runner_descriptor_cannot_write_before_initial_bindings(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    flow: str,
) -> None:
    if flow == "api":
        case = _api_build_case(
            safe_runtime,
            monkeypatch,
            operation=Operation.RESTART_API,
        )
        delegate: Any = ApiBuildRunner(case.root)
        payload = case.root / "components/cmms/frontend/build/prebound.bin"
    else:
        case = _frontend_dependency_case(safe_runtime, monkeypatch)
        delegate = FrontendDependencyRunner(case.root)
        payload = case.root / "components/cmms/api/target/prebound.bin"

    def mutate(first_lookup: int) -> None:
        if first_lookup == 1:
            payload.parent.mkdir(parents=True, exist_ok=True)
            payload.write_bytes(b"forbidden descriptor output\n")

    runner = RunnerDescriptor(delegate, mutate)
    expected_code = "CMMS-E035" if flow == "api" else "CMMS-E036"
    try:
        with pytest.raises(DeploymentError, match=expected_code):
            if flow == "api":
                build_api_artifact(case.claimed.context, case.toolchains, runner)
            else:
                install_frontend_dependencies(
                    case.claimed.context,
                    case.toolchains,
                    runner,
                )
        assert runner.lookups == 1
        assert delegate.probe_calls == []
        assert delegate.calls == []
        assert payload.read_bytes() == b"forbidden descriptor output\n"
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize("flow", ("api", "frontend"))
def test_runner_descriptor_exception_is_normalized_once(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    flow: str,
) -> None:
    if flow == "api":
        case = _api_build_case(
            safe_runtime,
            monkeypatch,
            operation=Operation.RESTART_API,
        )
        delegate: Any = ApiBuildRunner(case.root)
    else:
        case = _frontend_dependency_case(safe_runtime, monkeypatch)
        delegate = FrontendDependencyRunner(case.root)

    def explode(_lookup: int) -> None:
        raise DeploymentError("FORGED", "/secret/ambient/path", 99)

    runner = RunnerDescriptor(delegate, explode)
    expected_code = "CMMS-E035" if flow == "api" else "CMMS-E036"
    try:
        with pytest.raises(DeploymentError, match=expected_code) as captured:
            if flow == "api":
                build_api_artifact(case.claimed.context, case.toolchains, runner)
            else:
                install_frontend_dependencies(
                    case.claimed.context,
                    case.toolchains,
                    runner,
                )
        assert runner.lookups == 1
        assert "/secret/ambient/path" not in str(captured.value)
        assert delegate.probe_calls == []
        assert delegate.calls == []
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize(
    "profile",
    (RuntimeProfile.DEVELOPMENT, RuntimeProfile.ACCEPTANCE),
)
def test_frontend_dependencies_run_exact_pinned_commands_and_environment(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
) -> None:
    case = _frontend_dependency_case(
        safe_runtime,
        monkeypatch,
        profile=profile,
    )
    runner = FrontendDependencyRunner(case.root)
    try:
        receipt = install_frontend_dependencies(
            case.claimed.context,
            case.toolchains,
            runner,
        )
        node_home = case.toolchains.require("node").home
        cache = case.root / ".runtime/cmms-cache/npm"
        userconfig = (
            case.root
            / ".runtime/cmms-cache"
            / f"npm-userconfig-{case.claimed.context.plan.plan_sha256}.npmrc"
        )
        expected_environment = {
            "HUSKY": "0",
            "LC_ALL": "C",
            "NPM_CONFIG_USERCONFIG": str(userconfig),
            "PATH": f"{node_home / 'bin'}:/usr/bin:/bin",
        }
        assert [call.argv for call in runner.calls] == [
            (
                str(node_home / "bin/npm"),
                "ci",
                "--legacy-peer-deps",
                "--cache",
                str(cache),
            ),
            (str(node_home / "bin/npm"), "run", "build"),
        ]
        assert [call.safe_label for call in runner.calls] == [
            "cmms-frontend-npm-ci",
            "cmms-frontend-build",
        ]
        assert all(
            call.cwd == case.root / "components/cmms/frontend"
            and dict(call.environment) == expected_environment
            for call in runner.calls
        )
        assert tuple(Path(call.argv[0]).name for call in runner.probe_calls[:4]) == (
            "java",
            "mvn",
            "node",
            "npm",
        )
        assert len(runner.probe_calls) == 8
        assert receipt == DependencyReceipt(
            source=case.source,
            toolchain_manifest_sha256=case.toolchains.manifest_sha256,
            sensitive_manifest_sha256=case.sensitive_manifest.sha256,
            frontend_lock_sha256=case.lock_sha256,
        )
        userconfig_stat = userconfig.lstat()
        assert userconfig.read_bytes() == b""
        assert stat.S_ISREG(userconfig_stat.st_mode)
        assert stat.S_IMODE(userconfig_stat.st_mode) == 0o600
        assert userconfig_stat.st_uid == os.getuid()
        assert userconfig_stat.st_nlink == 1
        for private_directory in (
            case.root / ".runtime",
            case.root / ".runtime/cmms-cache",
            cache,
        ):
            metadata = private_directory.lstat()
            assert stat.S_ISDIR(metadata.st_mode)
            assert stat.S_IMODE(metadata.st_mode) == 0o700
            assert metadata.st_uid == os.getuid()
    finally:
        case.claimed.lease.close()


def test_reviewed_frontend_lock_fits_its_descriptor_bound_reader() -> None:
    lock_path = ROOT / "components/cmms/frontend/package-lock.json"
    expected_sha256 = (
        "484fdb709b700a50daaaf252f48cc9e11"
        "fb096401b52c8f4ef6d47de9100d46b"
    )
    binding = source_module._open_frontend_lock(lock_path, expected_sha256)
    try:
        binding.verify()
        assert binding.sha256 == expected_sha256
        assert binding.size == 2_273_403
    finally:
        binding.close()


def _frontend_userconfig(case: FrontendDependencyCase) -> Path:
    return (
        case.root
        / ".runtime/cmms-cache"
        / f"npm-userconfig-{case.claimed.context.plan.plan_sha256}.npmrc"
    )


@pytest.mark.parametrize(
    ("mutation", "relative"),
    (
        ("tracked-source", "frontend/src/App.tsx"),
        ("package-lock", "frontend/package-lock.json"),
        ("untracked-husky", "frontend/.husky/pre-commit"),
    ),
)
def test_frontend_dependencies_reject_source_or_lock_change_after_npm_ci(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    relative: str,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)

    def mutate(call_count: int, _spec: CommandSpec) -> None:
        if call_count != 1:
            return
        target = case.root / "components/cmms" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"forbidden-{mutation}\n".encode("ascii"))

    runner = FrontendDependencyRunner(case.root, mutate=mutate)
    try:
        with pytest.raises(DeploymentError, match="CMMS-E03[46]"):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert len(runner.calls) == 1
        assert len(runner.probe_calls) == 4
    finally:
        case.claimed.lease.close()


def test_frontend_first_toolchain_probe_cannot_write_api_target(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)

    def mutate_probe(call_count: int, _spec: CommandSpec) -> None:
        if call_count == 1:
            payload = case.root / "components/cmms/api/target/probe-owned.bin"
            payload.parent.mkdir(parents=True)
            payload.write_bytes(b"forbidden probe output\n")

    runner = FrontendDependencyRunner(case.root, probe_mutate=mutate_probe)
    try:
        with pytest.raises(DeploymentError, match="CMMS-E036"):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert len(runner.probe_calls) == 1
        assert runner.calls == []
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize(
    "relative",
    (
        "components/cmms/frontend/node_modules/probe-owned.bin",
        "components/cmms/frontend/build/probe-owned.bin",
        "components/cmms/frontend/public/runtime-env.js",
        ".runtime/cmms-cache/npm/probe-owned.bin",
    ),
)
def test_frontend_initial_probe_cannot_write_owned_output_or_cache(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)

    def mutate_probe(call_count: int, _spec: CommandSpec) -> None:
        if call_count == 1:
            payload = case.root / relative
            payload.parent.mkdir(parents=True, exist_ok=True)
            payload.write_bytes(b"forbidden initial probe output\n")

    runner = FrontendDependencyRunner(case.root, probe_mutate=mutate_probe)
    try:
        with pytest.raises(DeploymentError, match="CMMS-E036"):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert len(runner.probe_calls) == 1
        assert runner.calls == []
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize(
    "target",
    ("node-modules", "build", "runtime-env", "npm-cache"),
)
def test_frontend_final_probe_cannot_change_frozen_output_or_cache(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)

    def mutate_probe(call_count: int, _spec: CommandSpec) -> None:
        if call_count != 5:
            return
        paths = {
            "node-modules": case.root
            / "components/cmms/frontend/node_modules/probe-owned.bin",
            "build": case.root
            / "components/cmms/frontend/build/probe-owned.bin",
            "runtime-env": case.root
            / "components/cmms/frontend/public/runtime-env.js",
            "npm-cache": case.root
            / ".runtime/cmms-cache/npm/_cacache/probe-owned.bin",
        }
        payload = paths[target]
        payload.parent.mkdir(parents=True, exist_ok=True)
        payload.write_bytes(b"forbidden final probe output\n")

    runner = FrontendDependencyRunner(case.root, probe_mutate=mutate_probe)
    try:
        with pytest.raises(DeploymentError, match="CMMS-E036"):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert len(runner.probe_calls) == 5
        assert len(runner.calls) == 2
    finally:
        case.claimed.lease.close()


def test_frontend_toolchain_probe_normalizes_untrusted_deployment_error(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)

    def explode(_call_count: int, _spec: CommandSpec) -> None:
        raise DeploymentError("FORGED", "/secret/ambient/path", 99)

    runner = FrontendDependencyRunner(case.root, probe_mutate=explode)
    try:
        with pytest.raises(DeploymentError, match="CMMS-E036") as captured:
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert "/secret/ambient/path" not in str(captured.value)
        assert len(runner.probe_calls) == 1
        assert runner.calls == []
    finally:
        case.claimed.lease.close()


def test_frontend_npm_cannot_write_api_target(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)

    def mutate(call_count: int, _spec: CommandSpec) -> None:
        if call_count == 1:
            payload = case.root / "components/cmms/api/target/npm-owned.bin"
            payload.parent.mkdir(parents=True)
            payload.write_bytes(b"forbidden npm output\n")

    runner = FrontendDependencyRunner(case.root, mutate=mutate)
    try:
        with pytest.raises(DeploymentError, match="CMMS-E036"):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert len(runner.calls) == 1
        assert len(runner.probe_calls) == 4
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize("relative", ROOT_PROTECTED_GENERATED_DIRECTORIES)
def test_frontend_npm_cannot_write_root_approved_generated_tree(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)
    (case.root / ".git/info/exclude").write_text(
        f"/{relative}/\n",
        encoding="utf-8",
    )

    def mutate(call_count: int, _spec: CommandSpec) -> None:
        if call_count == 1:
            payload = case.root / relative / "npm-owned.bin"
            payload.parent.mkdir(parents=True, exist_ok=True)
            payload.write_bytes(b"forbidden npm output\n")

    runner = FrontendDependencyRunner(case.root, mutate=mutate)
    try:
        with pytest.raises(DeploymentError, match="CMMS-E036"):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert len(runner.calls) == 1
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize(
    "relative",
    (
        "npm-owned.bin",
        "cmms-cache/npm-owned.bin",
        "cmms-builds/npm-owned.bin",
    ),
)
def test_frontend_npm_cannot_write_unowned_runtime_path(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)

    def mutate(call_count: int, _spec: CommandSpec) -> None:
        if call_count == 1:
            payload = case.root / ".runtime" / relative
            payload.parent.mkdir(parents=True, exist_ok=True)
            payload.write_bytes(b"forbidden npm output\n")

    runner = FrontendDependencyRunner(case.root, mutate=mutate)
    try:
        with pytest.raises(DeploymentError, match="CMMS-E036"):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert len(runner.calls) == 1
    finally:
        case.claimed.lease.close()


def test_frontend_npm_may_populate_only_its_owned_cache_subtree(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)
    cache_entry = case.root / ".runtime/cmms-cache/npm/_cacache/entry.bin"

    def mutate(call_count: int, _spec: CommandSpec) -> None:
        if call_count == 1:
            cache_entry.parent.mkdir(parents=True, exist_ok=True)
            cache_entry.write_bytes(b"npm cache entry\n")

    runner = FrontendDependencyRunner(case.root, mutate=mutate)
    try:
        receipt = install_frontend_dependencies(
            case.claimed.context,
            case.toolchains,
            runner,
        )
        assert receipt.frontend_lock_sha256 == case.lock_sha256
        assert cache_entry.read_bytes() == b"npm cache entry\n"
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize("anomaly", ("absolute", "escaping", "hardlink"))
def test_frontend_npm_cache_rejects_external_linkage(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    anomaly: str,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)
    cache_link = case.root / ".runtime/cmms-cache/npm/cache-link"
    external = case.root.parent / f"{case.root.name}-ambient-npm-cache"
    external.write_bytes(b"ambient npm cache payload\n")

    def mutate(call_count: int, _spec: CommandSpec) -> None:
        if call_count != 1:
            return
        cache_link.parent.mkdir(parents=True, exist_ok=True)
        if anomaly == "absolute":
            cache_link.symlink_to(external)
        elif anomaly == "escaping":
            cache_link.symlink_to("../../../../ambient-npm-cache")
        else:
            os.link(external, cache_link)

    runner = FrontendDependencyRunner(case.root, mutate=mutate)
    try:
        with pytest.raises(DeploymentError, match="CMMS-E036"):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert len(runner.calls) == 2
    finally:
        case.claimed.lease.close()
        external.unlink(missing_ok=True)


def test_frontend_npm_cache_accepts_contained_relative_symlink(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)

    def mutate(call_count: int, _spec: CommandSpec) -> None:
        if call_count == 1:
            cache = case.root / ".runtime/cmms-cache/npm/_cacache"
            target = cache / "content/data.bin"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"contained cache payload\n")
            (cache / "current").symlink_to("content/data.bin")

    runner = FrontendDependencyRunner(case.root, mutate=mutate)
    try:
        receipt = install_frontend_dependencies(
            case.claimed.context,
            case.toolchains,
            runner,
        )
        assert receipt.frontend_lock_sha256 == case.lock_sha256
    finally:
        case.claimed.lease.close()


def test_frontend_accepts_exact_existing_root_venv_symlinks_and_mode(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)
    (case.root / ".git/info/exclude").write_text(
        "/deploy/cmms/.venv/\n/tests/.venv/\n",
        encoding="utf-8",
    )
    for relative in ("deploy/cmms/.venv", "tests/.venv"):
        venv = case.root / relative
        (venv / "bin").mkdir(parents=True)
        (venv / "lib").mkdir()
        venv.chmod(0o775)
        (venv / "bin/python").symlink_to(
            "/home/vm/.local/share/uv/python/cpython/bin/python"
        )
        (venv / "lib64").symlink_to("lib")

    try:
        receipt = install_frontend_dependencies(
            case.claimed.context,
            case.toolchains,
            FrontendDependencyRunner(case.root),
        )
        assert receipt.frontend_lock_sha256 == case.lock_sha256
    finally:
        case.claimed.lease.close()


def test_frontend_accepts_unchanged_existing_api_target(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)
    target = case.root / "components/cmms/api/target"
    (target / "classes").mkdir(parents=True)
    (target / "classes/Application.class").write_bytes(b"reviewed class\n")
    (target / "app.jar").write_bytes(b"reviewed jar\n")
    runner = FrontendDependencyRunner(case.root)
    try:
        receipt = install_frontend_dependencies(
            case.claimed.context,
            case.toolchains,
            runner,
        )
        assert receipt.frontend_lock_sha256 == case.lock_sha256
        assert (target / "app.jar").read_bytes() == b"reviewed jar\n"
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize(
    "anomaly",
    (
        "oversized",
        "depth",
        "entries",
        "fifo",
        "absolute-symlink",
        "escaping-symlink",
    ),
)
def test_frontend_rejects_unbounded_or_special_existing_api_target(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    anomaly: str,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)
    target = case.root / "components/cmms/api/target"
    target.mkdir(parents=True)
    if anomaly == "oversized":
        payload = target / "oversized.bin"
        payload.touch()
        os.truncate(payload, source_module._MAX_GENERATED_FILE_BYTES + 1)
    elif anomaly == "depth":
        monkeypatch.setattr(source_module, "_MAX_GENERATED_TREE_DEPTH", 2)
        (target / "one/two/three").mkdir(parents=True)
    elif anomaly == "entries":
        monkeypatch.setattr(source_module, "_MAX_GENERATED_TREE_ENTRIES", 2)
        for index in range(3):
            (target / f"entry-{index}").write_bytes(b"entry\n")
    elif anomaly == "fifo":
        os.mkfifo(target / "special", mode=0o600)
    elif anomaly == "absolute-symlink":
        (target / "unsafe-link").symlink_to("/etc/passwd")
    else:
        (target / "unsafe-link").symlink_to("../../ambient")
    runner = FrontendDependencyRunner(case.root)
    try:
        with pytest.raises(DeploymentError, match="CMMS-E036"):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert runner.probe_calls == []
        assert runner.calls == []
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize("replacement", ("root-symlink", "root-inode"))
def test_frontend_rejects_unsafe_or_replaced_api_target_root(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)
    target = case.root / "components/cmms/api/target"
    if replacement == "root-symlink":
        outside = case.root / "ambient-api-target"
        outside.mkdir()
        target.symlink_to(outside, target_is_directory=True)
        runner = FrontendDependencyRunner(case.root)
    else:
        target.mkdir(parents=True)
        (target / "app.jar").write_bytes(b"reviewed jar\n")

        def mutate_probe(call_count: int, _spec: CommandSpec) -> None:
            if call_count == 1:
                held = target.with_name("target-held")
                target.rename(held)
                target.mkdir()
                (target / "app.jar").write_bytes(b"reviewed jar\n")

        runner = FrontendDependencyRunner(case.root, probe_mutate=mutate_probe)
    try:
        with pytest.raises(DeploymentError, match="CMMS-E036"):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert runner.calls == []
        assert len(runner.probe_calls) == (0 if replacement == "root-symlink" else 1)
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize("scope", ("--local", "--worktree"))
def test_frontend_dependencies_reject_hooks_path_change_after_npm_ci(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)
    cmms = case.root / "components/cmms"

    def mutate(call_count: int, _spec: CommandSpec) -> None:
        if call_count == 1:
            _git(
                cmms,
                "config",
                scope,
                "--replace-all",
                "core.hooksPath",
                ".hooks-changed",
            )

    runner = FrontendDependencyRunner(case.root, mutate=mutate)
    try:
        with pytest.raises(DeploymentError):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert len(runner.calls) == 1
        assert len(runner.probe_calls) == 4
    finally:
        case.claimed.lease.close()


def test_frontend_dependencies_revalidate_toolchain_after_each_npm_command(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)
    npm = case.toolchains.require("node").home / "bin/npm"

    def mutate(call_count: int, _spec: CommandSpec) -> None:
        if call_count == 1:
            npm.unlink()
            npm.write_bytes(b"replacement executable\n")
            npm.chmod(0o755)

    runner = FrontendDependencyRunner(case.root, mutate=mutate)
    try:
        with pytest.raises(DeploymentError):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert len(runner.calls) == 1
        assert len(runner.probe_calls) == 4
    finally:
        case.claimed.lease.close()


def test_frontend_dependencies_reject_userconfig_replacement_after_npm_ci(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)

    def mutate(call_count: int, spec: CommandSpec) -> None:
        if call_count == 1:
            userconfig = Path(spec.environment["NPM_CONFIG_USERCONFIG"])
            userconfig.unlink()
            userconfig.write_bytes(b"audit=false\n")
            userconfig.chmod(0o600)

    runner = FrontendDependencyRunner(case.root, mutate=mutate)
    try:
        with pytest.raises(DeploymentError):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert len(runner.calls) == 1
        assert len(runner.probe_calls) == 4
    finally:
        case.claimed.lease.close()


def test_frontend_dependencies_reuses_only_an_exact_empty_userconfig(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)
    cache_root = case.root / ".runtime/cmms-cache"
    cache_root.mkdir(mode=0o700)
    cache_root.chmod(0o700)
    userconfig = _frontend_userconfig(case)
    userconfig.touch(mode=0o600)
    userconfig.chmod(0o600)
    before = userconfig.lstat()
    runner = FrontendDependencyRunner(case.root)
    try:
        receipt = install_frontend_dependencies(
            case.claimed.context,
            case.toolchains,
            runner,
        )
        after = userconfig.lstat()
        assert receipt.frontend_lock_sha256 == case.lock_sha256
        assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
        assert userconfig.read_bytes() == b""
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize("binding", ("toolchain", "sensitive"))
def test_frontend_snapshot_mismatch_runs_no_probe_or_npm(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    binding: str,
) -> None:
    case = _frontend_dependency_case(
        safe_runtime,
        monkeypatch,
        toolchain_snapshot_sha256=(
            "a" * 64 if binding == "toolchain" else None
        ),
        sensitive_snapshot_sha256=(
            "a" * 64 if binding == "sensitive" else None
        ),
    )
    runner = FrontendDependencyRunner(case.root)
    try:
        with pytest.raises(DeploymentError, match="CMMS-E036"):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert runner.probe_calls == []
        assert runner.calls == []
        assert not (case.root / ".runtime/cmms-cache").exists()
    finally:
        case.claimed.lease.close()


def test_frontend_unsafe_initial_cache_uses_frontend_failure_code(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)
    cache_root = case.root / ".runtime/cmms-cache"
    cache_root.mkdir(mode=0o755)
    cache_root.chmod(0o755)
    runner = FrontendDependencyRunner(case.root)
    try:
        with pytest.raises(DeploymentError, match="CMMS-E036"):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert runner.calls == []
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize("claim_state", ("missing-action", "stale"))
def test_frontend_requires_live_action_before_any_new_effect(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    claim_state: str,
) -> None:
    case = _frontend_dependency_case(
        safe_runtime,
        monkeypatch,
        include_action=claim_state != "missing-action",
    )
    if claim_state == "stale":
        safe_runtime.transition_application(
            case.claimed.application_path,
            case.claimed.context.application,
            PlanApplicationState.SUCCEEDED,
            case.claimed.context.application.claimed_at + timedelta(seconds=1),
        )
    runner = FrontendDependencyRunner(case.root)
    try:
        with pytest.raises(DeploymentError):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert runner.probe_calls == []
        assert runner.calls == []
        assert not (case.root / ".runtime/cmms-cache").exists()
    finally:
        case.claimed.lease.close()


def test_frontend_stale_after_npm_ci_stops_before_build(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)

    def mutate(call_count: int, _spec: CommandSpec) -> None:
        if call_count == 1:
            safe_runtime.transition_application(
                case.claimed.application_path,
                case.claimed.context.application,
                PlanApplicationState.SUCCEEDED,
                case.claimed.context.application.claimed_at + timedelta(seconds=1),
            )

    runner = FrontendDependencyRunner(case.root, mutate=mutate)
    try:
        with pytest.raises(DeploymentError):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert len(runner.calls) == 1
    finally:
        case.claimed.lease.close()


def test_frontend_every_probe_is_guarded_by_the_frontend_action(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)

    def invalidate_first_probe(call_count: int, _spec: CommandSpec) -> None:
        if call_count == 1:
            safe_runtime.transition_application(
                case.claimed.application_path,
                case.claimed.context.application,
                PlanApplicationState.SUCCEEDED,
                case.claimed.context.application.claimed_at + timedelta(seconds=1),
            )

    runner = FrontendDependencyRunner(
        case.root,
        probe_mutate=invalidate_first_probe,
    )
    try:
        with pytest.raises(DeploymentError):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert len(runner.probe_calls) == 1
        assert runner.calls == []
    finally:
        case.claimed.lease.close()


def test_frontend_revalidates_action_after_final_toolchain_probe(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)
    final_probe_seen = False

    def invalidate_final_probe(_call_count: int, spec: CommandSpec) -> None:
        nonlocal final_probe_seen
        if (
            len(runner.calls) == 2
            and Path(spec.argv[0]).name == "npm"
        ):
            final_probe_seen = True
            safe_runtime.transition_application(
                case.claimed.application_path,
                case.claimed.context.application,
                PlanApplicationState.SUCCEEDED,
                case.claimed.context.application.claimed_at + timedelta(seconds=1),
            )

    runner = FrontendDependencyRunner(case.root, probe_mutate=invalidate_final_probe)
    try:
        with pytest.raises(DeploymentError):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert len(runner.calls) == 2
        assert final_probe_seen is True
        assert len(runner.probe_calls) == 8
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize(
    "anomaly",
    ("symlink", "hardlink", "nonempty", "wrong-mode"),
)
def test_frontend_rejects_every_unsafe_existing_userconfig(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    anomaly: str,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)
    cache_root = case.root / ".runtime/cmms-cache"
    cache_root.mkdir(mode=0o700)
    cache_root.chmod(0o700)
    userconfig = _frontend_userconfig(case)
    if anomaly == "symlink":
        outside = case.root / "ambient-npmrc"
        outside.write_bytes(b"")
        outside.chmod(0o600)
        userconfig.symlink_to(outside)
    elif anomaly == "hardlink":
        outside = cache_root / "ambient-npmrc"
        outside.write_bytes(b"")
        outside.chmod(0o600)
        os.link(outside, userconfig)
    elif anomaly == "nonempty":
        userconfig.write_bytes(b"audit=false\n")
        userconfig.chmod(0o600)
    else:
        userconfig.touch()
        userconfig.chmod(0o644)
    runner = FrontendDependencyRunner(case.root)
    try:
        with pytest.raises(DeploymentError):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert runner.calls == []
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize("anomaly", ("mode", "replacement"))
def test_frontend_rejects_npm_cache_change_after_npm_ci(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    anomaly: str,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)

    def mutate(call_count: int, _spec: CommandSpec) -> None:
        if call_count != 1:
            return
        cache_root = case.root / ".runtime/cmms-cache"
        npm_cache = cache_root / "npm"
        if anomaly == "mode":
            npm_cache.chmod(0o755)
        else:
            moved = case.root / ".runtime/cmms-cache-original"
            cache_root.rename(moved)
            cache_root.mkdir(mode=0o700)
            cache_root.chmod(0o700)
            npm_cache.mkdir(mode=0o700)
            npm_cache.chmod(0o700)

    runner = FrontendDependencyRunner(case.root, mutate=mutate)
    try:
        with pytest.raises(DeploymentError):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert len(runner.calls) == 1
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize(
    "outcome",
    (
        CommandResult(3, "", "failed"),
        CommandResult(0, 1, ""),
        object(),
    ),
)
def test_frontend_rejects_nonzero_or_malformed_runner_result(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    outcome: object,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)
    runner = FrontendDependencyRunner(case.root, outcomes=(outcome,))
    try:
        with pytest.raises(DeploymentError, match="CMMS-E036"):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert len(runner.calls) == 1
    finally:
        case.claimed.lease.close()


def test_frontend_normalizes_runner_exception_and_stops(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)

    def explode(_call_count: int, _spec: CommandSpec) -> None:
        raise RuntimeError("ambient runner detail")

    runner = FrontendDependencyRunner(case.root, mutate=explode)
    try:
        with pytest.raises(DeploymentError, match="CMMS-E036") as captured:
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert "ambient runner detail" not in str(captured.value)
        assert len(runner.calls) == 1
    finally:
        case.claimed.lease.close()


def test_frontend_rejects_failed_build_verification_result(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)
    runner = FrontendDependencyRunner(
        case.root,
        outcomes=(CommandResult(0, "", ""), CommandResult(9, "", "failed")),
    )
    try:
        with pytest.raises(DeploymentError, match="CMMS-E036"):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert len(runner.calls) == 2
    finally:
        case.claimed.lease.close()


def test_frontend_rejects_forged_receipt_before_probe_or_npm(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)
    forged_node = replace(
        case.toolchains.require("node"),
        home=case.root / ".runtime/toolchains/node-forged",
    )
    forged = ToolchainReceipt(
        case.toolchains.manifest_sha256,
        (
            case.toolchains.require("temurin"),
            case.toolchains.require("maven"),
            forged_node,
        ),
    )
    runner = FrontendDependencyRunner(case.root)
    try:
        with pytest.raises(DeploymentError):
            install_frontend_dependencies(case.claimed.context, forged, runner)
        assert runner.probe_calls == []
        assert runner.calls == []
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize("replacement", ("lock", "userconfig"))
def test_frontend_rejects_same_content_descriptor_replacement(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)

    def mutate(call_count: int, spec: CommandSpec) -> None:
        if call_count != 1:
            return
        target = (
            case.root / "components/cmms/frontend/package-lock.json"
            if replacement == "lock"
            else Path(spec.environment["NPM_CONFIG_USERCONFIG"])
        )
        data = target.read_bytes()
        mode = stat.S_IMODE(target.lstat().st_mode)
        target.unlink()
        target.write_bytes(data)
        target.chmod(mode)

    runner = FrontendDependencyRunner(case.root, mutate=mutate)
    try:
        with pytest.raises(DeploymentError):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert len(runner.calls) == 1
    finally:
        case.claimed.lease.close()


def test_frontend_initial_source_change_runs_no_probe_or_npm(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)
    (case.root / "components/cmms/frontend/src/App.tsx").write_bytes(
        b"forbidden initial source change\n"
    )
    runner = FrontendDependencyRunner(case.root)
    try:
        with pytest.raises(DeploymentError):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert runner.probe_calls == []
        assert runner.calls == []
    finally:
        case.claimed.lease.close()


def test_frontend_sensitive_baseline_is_rechecked_after_npm_ci(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)
    monkeypatch.setattr(
        SourceInspector,
        "capture",
        lambda _self, captured_root, captured_profile: (
            case.source
            if captured_root == case.root
            and captured_profile is case.claimed.context.plan.profile
            else (_ for _ in ()).throw(AssertionError("unexpected source capture"))
        ),
    )

    def mutate(call_count: int, _spec: CommandSpec) -> None:
        if call_count == 1:
            (case.root / "components/cmms/frontend/package.json").write_bytes(
                b'{"name":"forbidden-mutation"}\n'
            )

    runner = FrontendDependencyRunner(case.root, mutate=mutate)
    try:
        with pytest.raises(DeploymentError, match="CMMS-E036"):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert len(runner.calls) == 1
    finally:
        case.claimed.lease.close()


def test_frontend_hooks_capture_requires_fixed_git_binary(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)
    monkeypatch.setattr(source_module, "_GIT_PATH", Path("/bin/git"))
    runner = FrontendDependencyRunner(case.root)
    try:
        with pytest.raises(DeploymentError):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert runner.probe_calls == []
        assert runner.calls == []
    finally:
        case.claimed.lease.close()


def test_frontend_hooks_capture_accepts_an_exact_absent_value(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)
    cmms = case.root / "components/cmms"
    _git(cmms, "config", "--local", "--unset-all", "core.hooksPath")
    _git(cmms, "config", "--worktree", "--unset-all", "core.hooksPath")
    source = SourceInspector().capture(case.root, RuntimeProfile.DEVELOPMENT)
    assert source == case.source
    runner = FrontendDependencyRunner(case.root)
    try:
        receipt = install_frontend_dependencies(
            case.claimed.context,
            case.toolchains,
            runner,
        )
        assert receipt.frontend_lock_sha256 == case.lock_sha256
        assert len(runner.calls) == 2
    finally:
        case.claimed.lease.close()


def test_frontend_hooks_capture_ignores_ambient_global_config(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)
    ambient = case.root / ".runtime/ambient-gitconfig"
    ambient.write_text(
        "[core]\n\thooksPath = /ambient/forbidden-hooks\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(ambient))
    monkeypatch.setenv("HOME", str(case.root / ".runtime/ambient-home"))
    runner = FrontendDependencyRunner(case.root)
    try:
        receipt = install_frontend_dependencies(
            case.claimed.context,
            case.toolchains,
            runner,
        )
        assert receipt.frontend_lock_sha256 == case.lock_sha256
        assert all("HOME" not in call.environment for call in runner.calls)
    finally:
        case.claimed.lease.close()


def test_frontend_lock_reader_rejects_above_its_independent_bound(
    tmp_path: Path,
) -> None:
    lock = tmp_path / "package-lock.json"
    lock.touch()
    os.truncate(lock, 4 * 1024 * 1024 + 1)
    with pytest.raises(DeploymentError, match="CMMS-E036"):
        source_module._open_frontend_lock(lock, "0" * 64)


def test_frontend_initial_probes_are_followed_by_state_check_before_cache_effect(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)

    def mutate_probe(call_count: int, _spec: CommandSpec) -> None:
        if call_count == 1:
            husky = case.root / "components/cmms/frontend/.husky/pre-commit"
            husky.parent.mkdir(parents=True)
            husky.write_bytes(b"forbidden probe side effect\n")

    runner = FrontendDependencyRunner(case.root, probe_mutate=mutate_probe)
    try:
        with pytest.raises(DeploymentError):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert runner.calls == []
        assert len(runner.probe_calls) == 1
        assert not (case.root / ".runtime/cmms-cache").exists()
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize(
    "mutation",
    (
        "runtime-mode",
        "toolchains-mode",
        "later-executable",
        "later-receipt",
    ),
)
def test_frontend_first_probe_rechecks_complete_toolchain_binding_before_cache(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)

    def mutate_probe(call_count: int, _spec: CommandSpec) -> None:
        if call_count != 1:
            return
        if mutation == "runtime-mode":
            (case.root / ".runtime").chmod(0o777)
        elif mutation == "toolchains-mode":
            (case.root / ".runtime/toolchains").chmod(0o777)
        elif mutation == "later-executable":
            npm = case.toolchains.require("node").home / "bin/npm"
            npm.unlink()
            npm.write_bytes(b"unreviewed executable\n")
            npm.chmod(0o755)
        else:
            receipt = (
                case.toolchains.require("node").home
                / ".ifactory-toolchain-receipt.json"
            )
            data = receipt.read_bytes()
            receipt.unlink()
            receipt.write_bytes(data)
            receipt.chmod(0o600)

    runner = FrontendDependencyRunner(case.root, probe_mutate=mutate_probe)
    try:
        with pytest.raises(DeploymentError):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert runner.calls == []
        assert len(runner.probe_calls) == 1
        assert not (case.root / ".runtime/cmms-cache").exists()
    finally:
        case.claimed.lease.close()


def test_frontend_expected_binding_rejects_before_post_command_or_final_probes(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)

    def mutate(call_count: int, _spec: CommandSpec) -> None:
        if call_count == 1:
            npm = case.toolchains.require("node").home / "bin/npm"
            npm.unlink()
            npm.write_bytes(b"unreviewed post-npm executable\n")
            npm.chmod(0o755)

    runner = FrontendDependencyRunner(case.root, mutate=mutate)
    try:
        with pytest.raises(DeploymentError):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert len(runner.calls) == 1
        assert len(runner.probe_calls) == 4
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize("mutation", ("later-executable", "later-receipt"))
def test_frontend_precommand_expected_binding_rejects_without_extra_probe(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)

    original_open = source_module._open_frontend_userconfig

    def open_then_mutate(path: Path) -> Any:
        binding = original_open(path)
        if mutation == "later-executable":
            target = case.toolchains.require("node").home / "bin/npm"
            target.unlink()
            target.write_bytes(b"unreviewed precommand executable\n")
            target.chmod(0o755)
        else:
            target = (
                case.toolchains.require("node").home
                / ".ifactory-toolchain-receipt.json"
            )
            data = target.read_bytes()
            target.unlink()
            target.write_bytes(data)
            target.chmod(0o600)
        return binding

    monkeypatch.setattr(
        source_module,
        "_open_frontend_userconfig",
        open_then_mutate,
    )
    runner = FrontendDependencyRunner(case.root)
    try:
        with pytest.raises(DeploymentError):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert runner.calls == []
        assert len(runner.probe_calls) == 4
    finally:
        case.claimed.lease.close()


def test_frontend_final_probe_source_mutation_never_returns_receipt(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)

    def mutate_probe(call_count: int, _spec: CommandSpec) -> None:
        if call_count == 5:
            husky = case.root / "components/cmms/frontend/.husky/pre-commit"
            husky.parent.mkdir(parents=True)
            husky.write_bytes(b"forbidden final probe side effect\n")

    runner = FrontendDependencyRunner(case.root, probe_mutate=mutate_probe)
    try:
        with pytest.raises(DeploymentError):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert len(runner.calls) == 2
        assert len(runner.probe_calls) == 5
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize(
    "reader",
    (
        "frontend-lock",
        "frontend-userconfig",
        "sensitive-manifest",
        "sensitive-file",
        "untracked-file",
        "bound-toolchain-receipt",
    ),
)
def test_task3_regular_readers_reject_fifo_without_blocking(
    tmp_path: Path,
    reader: str,
) -> None:
    fixture = tmp_path / reader
    fixture.mkdir(mode=0o700)
    fifo = (
        fixture / ".ifactory-toolchain-receipt.json"
        if reader == "bound-toolchain-receipt"
        else fixture / "candidate"
    )
    os.mkfifo(fifo, mode=0o600)
    script = """
import os
import sys
from pathlib import Path

import ifactory_cmms_deploy.source as source
import ifactory_cmms_deploy.toolchains as toolchains

reader = sys.argv[1]
path = Path(sys.argv[2])
try:
    if reader == "frontend-lock":
        source._open_frontend_lock(path, "0" * 64)
    elif reader == "frontend-userconfig":
        source._open_frontend_userconfig(path)
    elif reader == "sensitive-manifest":
        source.SensitiveManifest.load(path)
    elif reader == "sensitive-file":
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            source._hash_regular_at(
                parent_fd,
                path.name,
                source._ReadBudget(1, 1024, 1024),
            )
        finally:
            os.close(parent_fd)
    elif reader == "untracked-file":
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            source._hash_untracked(
                parent_fd,
                os.fsencode(path.name),
                source._ReadBudget(1, 1024, 1024),
            )
        finally:
            os.close(parent_fd)
    elif reader == "bound-toolchain-receipt":
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            toolchains._read_bound_install_receipt(parent_fd, {})
        finally:
            os.close(parent_fd)
    else:
        raise AssertionError("unknown reader")
except BaseException:
    raise SystemExit(0)
raise SystemExit(3)
"""
    completed = subprocess.run(
        (sys.executable, "-c", script, reader, str(fifo)),
        cwd=ROOT,
        env={
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(ROOT / "deploy/cmms/src"),
        },
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=2,
        check=False,
        shell=False,
    )
    assert completed.returncode == 0


@pytest.mark.parametrize(
    "reader",
    (
        "toolchain-manifest",
        "toolchain-install-receipt",
        "api-descriptor",
        "published-artifact",
        "api-candidate",
    ),
)
def test_task3_regular_readers_reject_lstat_to_fifo_race_without_blocking(
    tmp_path: Path,
    reader: str,
) -> None:
    fixture = tmp_path / reader
    fixture.mkdir(mode=0o700)
    script = r"""
import os
import sys
from pathlib import Path

import ifactory_cmms_deploy.source as source
import ifactory_cmms_deploy.toolchains as toolchains

reader = sys.argv[1]
fixture = Path(sys.argv[2])
path = fixture / "candidate"
path.write_bytes(b"reviewed regular input\n")
path.chmod(0o600)

def replace_with_fifo() -> None:
    path.unlink()
    os.mkfifo(path, mode=0o600)

try:
    if reader in {"toolchain-manifest", "toolchain-install-receipt", "api-descriptor"}:
        original_lstat = Path.lstat
        replaced = False

        def racing_lstat(candidate):
            global replaced
            metadata = original_lstat(candidate)
            if candidate == path and not replaced:
                replaced = True
                replace_with_fifo()
            return metadata

        Path.lstat = racing_lstat
        if reader == "toolchain-manifest":
            toolchains.ToolchainManifest.load(path)
        elif reader == "toolchain-install-receipt":
            assert toolchains._read_install_receipt(path, {}) is False
            raise ValueError("receipt race rejected")
        else:
            source._read_stable_descriptor(path, 1024)
    else:
        target = fixture / "target"
        target.mkdir(mode=0o700)
        path = target / "app.jar"
        path.write_bytes(b"reviewed artifact\n")
        path.chmod(0o600)
        original_stat = os.stat
        replaced = False

        def racing_stat(candidate, *args, **kwargs):
            global replaced
            metadata = original_stat(candidate, *args, **kwargs)
            if (
                candidate == "app.jar"
                and kwargs.get("dir_fd") is not None
                and not kwargs.get("follow_symlinks", True)
                and not replaced
            ):
                replaced = True
                replace_with_fifo()
            return metadata

        source.os.stat = racing_stat
        if reader == "published-artifact":
            directory_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY)
            try:
                source._verify_artifact_in_directory(
                    directory_fd,
                    "app.jar",
                    "0" * 64,
                    len(b"reviewed artifact\n"),
                )
            finally:
                os.close(directory_fd)
        else:
            source._prepare_api_candidate(target, fixture / "builds")
except BaseException:
    raise SystemExit(0)
raise SystemExit(3)
"""
    completed = subprocess.run(
        (sys.executable, "-c", script, reader, str(fixture)),
        cwd=ROOT,
        env={
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(ROOT / "deploy/cmms/src"),
        },
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=2,
        check=False,
        shell=False,
    )
    assert completed.returncode == 0


@pytest.mark.parametrize(
    ("target_name", "expected_probe_count"),
    (
        ("frontend-lock", 0),
        ("frontend-userconfig", 0),
        ("sensitive-manifest", 0),
        ("toolchain-manifest", 0),
        ("toolchain-receipt", 0),
    ),
)
def test_frontend_dependency_flow_rejects_fifo_inputs_before_npm(
    safe_runtime: SafeRuntimeFixture,
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
    expected_probe_count: int,
) -> None:
    case = _frontend_dependency_case(safe_runtime, monkeypatch)
    if target_name == "frontend-lock":
        target = case.root / "components/cmms/frontend/package-lock.json"
    elif target_name == "frontend-userconfig":
        cache_root = case.root / ".runtime/cmms-cache"
        cache_root.mkdir(mode=0o700)
        target = _frontend_userconfig(case)
    elif target_name == "sensitive-manifest":
        target = (
            case.root / "deploy/cmms/manifests/startup-sensitive-files.json"
        )
    elif target_name == "toolchain-manifest":
        target = case.root / "deploy/cmms/manifests/toolchains.json"
    else:
        target = (
            case.toolchains.require("node").home
            / ".ifactory-toolchain-receipt.json"
        )
    if target.exists():
        target.unlink()
    os.mkfifo(target, mode=0o600)

    runner = FrontendDependencyRunner(case.root)
    try:
        with pytest.raises(DeploymentError):
            install_frontend_dependencies(
                case.claimed.context,
                case.toolchains,
                runner,
            )
        assert runner.calls == []
        assert len(runner.probe_calls) == expected_probe_count
    finally:
        case.claimed.lease.close()


@pytest.mark.parametrize(
    "capture_case",
    (
        "generated-missing",
        "generated-existing",
        "ignored-missing",
        "ignored-existing",
        "runtime-boundary",
    ),
)
def test_failed_initial_binding_verification_closes_every_open_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_case: str,
) -> None:
    root = (tmp_path / "root").resolve()
    root.mkdir(mode=0o700)
    target = root / "target"
    if capture_case == "generated-existing":
        target.write_bytes(b"generated\n")
    elif capture_case == "ignored-existing":
        target.mkdir(mode=0o700)

    original_open = source_module.os.open
    opened: list[int] = []

    def tracking_open(*args: Any, **kwargs: Any) -> int:
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def fail_generated_verify(self: Any) -> None:
        raise self.failure()

    monkeypatch.setattr(source_module.os, "open", tracking_open)
    if capture_case.startswith("generated-"):
        monkeypatch.setattr(
            source_module._GeneratedPathBinding,
            "verify",
            fail_generated_verify,
        )
        operation = lambda: source_module._capture_generated_path(
            target,
            "regular",
            source_module._build_error,
        )
    elif capture_case.startswith("ignored-"):
        monkeypatch.setattr(
            source_module._ExactIgnoredTreeBinding,
            "verify",
            fail_generated_verify,
        )
        operation = lambda: source_module._capture_exact_ignored_tree(
            root,
            "target",
            source_module._build_error,
        )
    else:
        monkeypatch.setattr(
            source_module._RuntimeBoundaryBinding,
            "verify",
            fail_generated_verify,
        )
        operation = lambda: source_module._capture_runtime_boundary(
            root,
            (source_module._RuntimeOwnedRule(("cache",), "directory"),),
            mutable_content=set(),
            creation_allowed=set(),
            controlled_paths={("cache",)},
            contained_roots=set(),
            failure=source_module._build_error,
        )

    with pytest.raises(DeploymentError):
        operation()
    assert opened
    for fd in opened:
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(fd)


@pytest.mark.parametrize("failure_point", ("descriptor-stat", "path-stat"))
def test_ignored_tree_capture_closes_untransferred_next_fd_after_stat_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    root = (tmp_path / "root").resolve()
    root.mkdir(mode=0o700)
    (root / "target").mkdir(mode=0o700)
    original_open = source_module.os.open
    original_fstat = source_module.os.fstat
    original_stat = source_module.os.stat
    target_fds: list[int] = []

    def tracking_open(path: Any, *args: Any, **kwargs: Any) -> int:
        fd = original_open(path, *args, **kwargs)
        if path == "target" and kwargs.get("dir_fd") is not None:
            target_fds.append(fd)
        return fd

    def racing_fstat(fd: int) -> os.stat_result:
        if failure_point == "descriptor-stat" and fd in target_fds:
            raise OSError(errno.EIO, "injected descriptor stat race")
        return original_fstat(fd)

    def racing_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if (
            failure_point == "path-stat"
            and path == "target"
            and kwargs.get("dir_fd") is not None
        ):
            raise OSError(errno.EIO, "injected path stat race")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(source_module.os, "open", tracking_open)
    monkeypatch.setattr(source_module.os, "fstat", racing_fstat)
    monkeypatch.setattr(source_module.os, "stat", racing_stat)
    try:
        with pytest.raises(DeploymentError):
            source_module._capture_exact_ignored_tree(
                root,
                "target",
                source_module._build_error,
            )
        assert len(target_fds) == 1
        with pytest.raises(OSError):
            original_fstat(target_fds[0])
    finally:
        for fd in target_fds:
            try:
                original_fstat(fd)
            except OSError:
                continue
            os.close(fd)


def test_open_root_directory_closes_fd_when_descriptor_stat_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    before = tuple(sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")))
    original_open = source_module.os.open
    original_fstat = source_module.os.fstat
    opened: list[int] = []

    def tracking_open(*args: Any, **kwargs: Any) -> int:
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def failing_fstat(fd: int) -> os.stat_result:
        if fd in opened:
            raise OSError(errno.EIO, "injected root descriptor stat failure")
        return original_fstat(fd)

    monkeypatch.setattr(source_module.os, "open", tracking_open)
    monkeypatch.setattr(source_module.os, "fstat", failing_fstat)

    with pytest.raises(source_module._BaselineChanged) as exc_info:
        source_module._open_root_directory(root)

    assert str(exc_info.value) == ""
    assert len(opened) == 1
    with pytest.raises(OSError) as closed:
        original_fstat(opened[0])
    assert closed.value.errno == errno.EBADF
    assert tuple(sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))) == before


def test_open_child_directory_closes_fd_when_descriptor_stat_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    child = root / "child"
    child.mkdir(parents=True, mode=0o700)
    before = tuple(sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")))
    parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    original_open = source_module.os.open
    original_fstat = source_module.os.fstat
    opened: list[int] = []

    def tracking_open(*args: Any, **kwargs: Any) -> int:
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def failing_fstat(fd: int) -> os.stat_result:
        if fd in opened:
            raise OSError(errno.EIO, "injected child descriptor stat failure")
        return original_fstat(fd)

    monkeypatch.setattr(source_module.os, "open", tracking_open)
    monkeypatch.setattr(source_module.os, "fstat", failing_fstat)
    try:
        with pytest.raises(source_module._BaselineChanged) as exc_info:
            source_module._open_child_directory(parent_fd, "child")

        assert str(exc_info.value) == ""
        assert len(opened) == 1
        with pytest.raises(OSError) as closed:
            original_fstat(opened[0])
        assert closed.value.errno == errno.EBADF
        assert (
            tuple(sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")))
            == before
        )
    finally:
        os.close(parent_fd)


def test_open_bytes_parent_closes_child_fd_when_descriptor_stat_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    child = root / "child"
    child.mkdir(parents=True, mode=0o700)
    before = tuple(sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")))
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    original_open = source_module.os.open
    original_fstat = source_module.os.fstat
    opened: list[int] = []

    def tracking_open(*args: Any, **kwargs: Any) -> int:
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def failing_fstat(fd: int) -> os.stat_result:
        if fd in opened:
            raise OSError(errno.EIO, "injected bytes descriptor stat failure")
        return original_fstat(fd)

    monkeypatch.setattr(source_module.os, "open", tracking_open)
    monkeypatch.setattr(source_module.os, "fstat", failing_fstat)
    try:
        with pytest.raises(DeploymentError) as exc_info:
            source_module._open_bytes_parent(root_fd, (b"child",))

        assert exc_info.value.code == "CMMS-E034"
        assert exc_info.value.safe_message == "CMMS source evidence is unavailable"
        assert exc_info.value.exit_code == 34
        assert "injected" not in str(exc_info.value)
        assert len(opened) == 1
        with pytest.raises(OSError) as closed:
            original_fstat(opened[0])
        assert closed.value.errno == errno.EBADF
        assert (
            tuple(sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")))
            == before
        )
    finally:
        os.close(root_fd)


def test_extract_archive_closes_dup_fd_when_fdopen_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, archives = _fixture_manifest(tmp_path, monkeypatch)
    tool = manifest.require("temurin")
    archive_path = tmp_path / "archive"
    bound = toolchains_module._BoundFile.create(
        archive_path,
        max_bytes=tool.max_bytes,
    )
    payload = archives[tool.url]
    assert os.write(bound.fd, payload) == len(payload)
    os.fsync(bound.fd)
    bound.capture_written()
    extraction_root = tmp_path / "extraction"
    extraction_root.mkdir(mode=0o700)
    before = tuple(sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")))
    original_dup = toolchains_module.os.dup
    original_fstat = os.fstat
    duplicated: list[int] = []

    def tracking_dup(fd: int) -> int:
        duplicate = original_dup(fd)
        duplicated.append(duplicate)
        return duplicate

    def failing_fdopen(*args: Any, **kwargs: Any) -> Any:
        raise OSError(errno.EIO, "injected fdopen failure")

    monkeypatch.setattr(toolchains_module.os, "dup", tracking_dup)
    monkeypatch.setattr(toolchains_module.os, "fdopen", failing_fdopen)
    try:
        with pytest.raises(DeploymentError) as exc_info:
            toolchains_module._extract_archive(bound, extraction_root, tool)

        assert exc_info.value.code == "CMMS-E031"
        assert exc_info.value.safe_message == "toolchain archive is unsafe"
        assert exc_info.value.exit_code == 31
        assert "injected" not in str(exc_info.value)
        assert len(duplicated) == 1
        with pytest.raises(OSError) as closed:
            original_fstat(duplicated[0])
        assert closed.value.errno == errno.EBADF
        assert original_fstat(bound.fd).st_size == len(payload)
        assert (
            tuple(sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")))
            == before
        )
        assert tuple(extraction_root.iterdir()) == ()
    finally:
        for fd in duplicated:
            try:
                original_fstat(fd)
            except OSError:
                continue
            os.close(fd)
        bound.close()


def test_open_parent_closes_untransferred_child_when_parent_close_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    (root / "child").mkdir(parents=True, mode=0o700)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    original_dup = source_module.os.dup
    original_open = source_module.os.open
    original_close = source_module.os.close
    original_fstat = source_module.os.fstat
    duplicated: list[int] = []
    children: list[int] = []
    failed = False

    def tracking_dup(fd: int) -> int:
        duplicate = original_dup(fd)
        duplicated.append(duplicate)
        return duplicate

    def tracking_open(path: Any, *args: Any, **kwargs: Any) -> int:
        fd = original_open(path, *args, **kwargs)
        if path == "child" and kwargs.get("dir_fd") is not None:
            children.append(fd)
        return fd

    def flaky_close(fd: int) -> None:
        nonlocal failed
        if children and duplicated and fd == duplicated[0] and not failed:
            failed = True
            raise OSError(errno.EIO, "injected parent close failure")
        original_close(fd)

    monkeypatch.setattr(source_module.os, "dup", tracking_dup)
    monkeypatch.setattr(source_module.os, "open", tracking_open)
    monkeypatch.setattr(source_module.os, "close", flaky_close)
    error: BaseException | None = None
    try:
        try:
            source_module._open_parent(root_fd, ("child",))
        except BaseException as caught:
            error = caught

        assert failed is True
        assert len(duplicated) == 1
        assert len(children) == 1
        with pytest.raises(OSError) as closed:
            original_fstat(children[0])
        assert closed.value.errno == errno.EBADF
        assert isinstance(error, source_module._BaselineChanged)
        assert str(error) == ""
    finally:
        for fd in (*children, *duplicated):
            try:
                original_fstat(fd)
            except OSError:
                continue
            original_close(fd)
        original_close(root_fd)


def test_open_bytes_parent_closes_next_fd_when_parent_close_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    (root / "child").mkdir(parents=True, mode=0o700)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    original_dup = source_module.os.dup
    original_open = source_module.os.open
    original_close = source_module.os.close
    original_fstat = source_module.os.fstat
    duplicated: list[int] = []
    children: list[int] = []
    failed = False

    def tracking_dup(fd: int) -> int:
        duplicate = original_dup(fd)
        duplicated.append(duplicate)
        return duplicate

    def tracking_open(path: Any, *args: Any, **kwargs: Any) -> int:
        fd = original_open(path, *args, **kwargs)
        if path == b"child" and kwargs.get("dir_fd") is not None:
            children.append(fd)
        return fd

    def flaky_close(fd: int) -> None:
        nonlocal failed
        if children and duplicated and fd == duplicated[0] and not failed:
            failed = True
            raise OSError(errno.EIO, "injected bytes parent close failure")
        original_close(fd)

    monkeypatch.setattr(source_module.os, "dup", tracking_dup)
    monkeypatch.setattr(source_module.os, "open", tracking_open)
    monkeypatch.setattr(source_module.os, "close", flaky_close)
    try:
        with pytest.raises(DeploymentError) as exc_info:
            source_module._open_bytes_parent(root_fd, (b"child",))

        assert exc_info.value.code == "CMMS-E034"
        assert "injected" not in str(exc_info.value)
        assert failed is True
        assert len(duplicated) == 1
        assert len(children) == 1
        with pytest.raises(OSError) as closed:
            original_fstat(children[0])
        assert closed.value.errno == errno.EBADF
    finally:
        for fd in (*children, *duplicated):
            try:
                original_fstat(fd)
            except OSError:
                continue
            original_close(fd)
        original_close(root_fd)


def test_capture_baseline_closes_tree_fds_when_parent_close_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cmms_root, manifest = _sensitive_fixture_manifest(tmp_path, monkeypatch)
    tree_parts = source_module._relative_parts(manifest.migration_tree.path)
    original_open_parent = source_module._open_parent
    original_open_child = source_module._open_child_directory
    original_close = source_module.os.close
    original_fstat = source_module.os.fstat
    tree_parents: list[int] = []
    tree_fds: list[int] = []
    failed = False

    def tracking_open_parent(root_fd: int, parts: tuple[str, ...]) -> int:
        fd = original_open_parent(root_fd, parts)
        if parts == tree_parts[:-1]:
            tree_parents.append(fd)
        return fd

    def tracking_open_child(parent_fd: int, name: str) -> int:
        fd = original_open_child(parent_fd, name)
        if tree_parents and parent_fd == tree_parents[0] and name == tree_parts[-1]:
            tree_fds.append(fd)
        return fd

    def flaky_close(fd: int) -> None:
        nonlocal failed
        if tree_fds and tree_parents and fd == tree_parents[0] and not failed:
            failed = True
            raise OSError(errno.EIO, "injected tree parent close failure")
        original_close(fd)

    monkeypatch.setattr(source_module, "_open_parent", tracking_open_parent)
    monkeypatch.setattr(source_module, "_open_child_directory", tracking_open_child)
    monkeypatch.setattr(source_module.os, "close", flaky_close)
    error: BaseException | None = None
    try:
        try:
            source_module._capture_baseline_once(cmms_root, manifest)
        except BaseException as caught:
            error = caught

        assert failed is True
        assert len(tree_parents) == 1
        assert len(tree_fds) == 1
        with pytest.raises(OSError) as closed:
            original_fstat(tree_fds[0])
        assert closed.value.errno == errno.EBADF
        assert isinstance(error, source_module._BaselineChanged)
        assert str(error) == ""
    finally:
        for fd in (*tree_fds, *tree_parents):
            try:
                original_fstat(fd)
            except OSError:
                continue
            original_close(fd)


def test_extract_archive_closes_output_fd_when_member_close_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, archives = _fixture_manifest(tmp_path, monkeypatch)
    tool = manifest.require("temurin")
    bound = toolchains_module._BoundFile.create(
        tmp_path / "archive",
        max_bytes=tool.max_bytes,
    )
    payload = archives[tool.url]
    assert os.write(bound.fd, payload) == len(payload)
    os.fsync(bound.fd)
    bound.capture_written()
    extraction_root = tmp_path / "extraction"
    extraction_root.mkdir(mode=0o700)
    original_extractfile = tarfile.TarFile.extractfile
    original_open = toolchains_module.os.open
    original_close = toolchains_module.os.close
    original_fstat = toolchains_module.os.fstat
    output_fds: list[int] = []

    class FailingCloseReader:
        def __init__(self, delegate: Any) -> None:
            self.delegate = delegate

        def read(self, size: int = -1) -> bytes:
            return self.delegate.read(size)

        def close(self) -> None:
            self.delegate.close()
            raise OSError(errno.EIO, "injected member close failure")

    def wrapping_extractfile(archive: Any, member: Any) -> Any:
        source = original_extractfile(archive, member)
        assert source is not None
        return FailingCloseReader(source)

    def tracking_open(path: Any, *args: Any, **kwargs: Any) -> int:
        fd = original_open(path, *args, **kwargs)
        if isinstance(path, Path) and path.is_relative_to(extraction_root):
            output_fds.append(fd)
        return fd

    monkeypatch.setattr(tarfile.TarFile, "extractfile", wrapping_extractfile)
    monkeypatch.setattr(toolchains_module.os, "open", tracking_open)
    try:
        with pytest.raises(DeploymentError) as exc_info:
            toolchains_module._extract_archive(bound, extraction_root, tool)

        assert exc_info.value.code == "CMMS-E031"
        assert exc_info.value.safe_message == "toolchain archive is unsafe"
        assert "injected" not in str(exc_info.value)
        assert len(output_fds) == 1
        with pytest.raises(OSError) as closed:
            original_fstat(output_fds[0])
        assert closed.value.errno == errno.EBADF
    finally:
        for fd in output_fds:
            try:
                original_fstat(fd)
            except OSError:
                continue
            original_close(fd)
        bound.close()


@pytest.mark.parametrize(
    ("capture_case", "expected_code"),
    (
        ("ensure-private", "CMMS-E035"),
        ("capture-private", "CMMS-E035"),
        ("frontend-userconfig", "CMMS-E036"),
        ("generated-path", "CMMS-E035"),
        ("ignored-tree", "CMMS-E035"),
    ),
)
def test_multi_fd_cleanup_attempts_every_owned_descriptor_after_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_case: str,
    expected_code: str,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    target = parent / "target"

    def fail_build_verify(self: Any) -> None:
        raise source_module._build_error()

    def fail_frontend_verify(self: Any) -> None:
        raise source_module._frontend_error()

    if capture_case == "ensure-private":
        operation = lambda: source_module._ensure_private_directory(target)
    elif capture_case == "capture-private":
        target.mkdir(mode=0o700)
        monkeypatch.setattr(
            source_module._PrivateDirectoryBinding,
            "verify",
            fail_build_verify,
        )
        operation = lambda: source_module._capture_private_directory(
            target,
            source_module._build_error,
        )
    elif capture_case == "frontend-userconfig":
        monkeypatch.setattr(
            source_module._EmptyUserConfigBinding,
            "verify",
            fail_frontend_verify,
        )
        operation = lambda: source_module._open_frontend_userconfig(target)
    elif capture_case == "generated-path":
        target.write_bytes(b"generated\n")
        monkeypatch.setattr(
            source_module._GeneratedPathBinding,
            "verify",
            fail_build_verify,
        )
        operation = lambda: source_module._capture_generated_path(
            target,
            "regular",
            source_module._build_error,
        )
    else:
        target.mkdir(mode=0o700)
        monkeypatch.setattr(
            source_module._ExactIgnoredTreeBinding,
            "verify",
            fail_build_verify,
        )
        operation = lambda: source_module._capture_exact_ignored_tree(
            parent,
            "target",
            source_module._build_error,
        )

    original_open = source_module.os.open
    original_close = source_module.os.close
    original_fstat = source_module.os.fstat
    opened: list[int] = []
    close_calls: list[int] = []

    def tracking_open(*args: Any, **kwargs: Any) -> int:
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def failing_first_close(fd: int) -> None:
        close_calls.append(fd)
        if len(close_calls) == 1:
            raise OSError(errno.EIO, "injected first cleanup close failure")
        original_close(fd)

    monkeypatch.setattr(source_module.os, "open", tracking_open)
    monkeypatch.setattr(source_module.os, "close", failing_first_close)
    error: BaseException | None = None
    try:
        try:
            operation()
        except BaseException as caught:
            error = caught

        assert len(opened) == 2
        assert close_calls == [opened[1], opened[0]]
        with pytest.raises(OSError) as closed:
            original_fstat(opened[0])
        assert closed.value.errno == errno.EBADF
        assert isinstance(error, DeploymentError)
        assert error.code == expected_code
        assert "injected" not in str(error)
    finally:
        for fd in opened:
            try:
                original_fstat(fd)
            except OSError:
                continue
            original_close(fd)


def test_open_bytes_parent_normalizes_initial_dup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)

    def failing_dup(fd: int) -> int:
        raise OSError(errno.EIO, "injected initial dup failure")

    monkeypatch.setattr(source_module.os, "dup", failing_dup)
    try:
        with pytest.raises(DeploymentError) as exc_info:
            source_module._open_bytes_parent(root_fd, ())

        assert exc_info.value.code == "CMMS-E034"
        assert exc_info.value.safe_message == "CMMS source evidence is unavailable"
        assert exc_info.value.exit_code == 34
        assert "injected" not in str(exc_info.value)
        assert tuple(root.iterdir()) == ()
    finally:
        os.close(root_fd)
