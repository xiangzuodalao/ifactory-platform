"""Test-only fakes and private runtime-file factories for CMMS deployment."""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Collection

from ifactory_cmms_deploy.records import (
    ActionCode,
    ActionTargetKind,
    BootstrapPlanBindings,
    CredentialIdentity,
    CredentialPlanBinding,
    DeploymentPlan,
    DeploymentSnapshot,
    LicenseMode,
    Operation,
    PlannedAction,
    RuntimeProfile,
    SecureFileStatBinding,
    SourceBinding,
    SourceStatus,
    _create_gateway_fail_closed_authority_parts,
    acquire_deployment_write_lease,
    claim_plan_application,
    load_confirmed_plan,
    reserve_plan_attempt,
    write_plan,
)


@dataclass(frozen=True)
class SafeRuntimeFixture:
    root: Path
    runner: ScriptedRunner = field(default_factory=lambda: ScriptedRunner())

    def _safe_path(self, relative: str | Path) -> Path:
        raw = os.fspath(relative)
        components = raw.split(os.sep)
        candidate = Path(raw)
        if (
            not raw
            or candidate.is_absolute()
            or any(component in {"", ".", ".."} for component in components)
        ):
            raise ValueError("unsafe test fixture path")
        root = Path(os.path.abspath(self.root))
        path = Path(os.path.abspath(root / candidate))
        if path == root or not path.is_relative_to(root):
            raise ValueError("unsafe test fixture path")
        return path

    def private_directory(self, relative: str | Path) -> Path:
        path = self._safe_path(relative)
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        path.chmod(0o700)
        return path

    def private_file(self, relative: str | Path, data: bytes) -> Path:
        path = self._safe_path(relative)
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        path.parent.chmod(0o700)
        path.write_bytes(data)
        path.chmod(0o600)
        return path

    @property
    def runtime_dir(self) -> Path:
        return self.private_directory(".runtime")

    @property
    def plans_dir(self) -> Path:
        return self.private_directory(".runtime/plans/cmms-development")

    @property
    def lock_path(self) -> Path:
        return self.runtime_dir / "cmms-development-apply.lock"

    @staticmethod
    def stat_binding(logical_file: str, seed: int) -> SecureFileStatBinding:
        return SecureFileStatBinding(
            logical_file=logical_file,
            dev=10,
            ino=seed,
            size=32,
            mtime_ns=1_000_000_000 + seed,
            ctime_ns=2_000_000_000 + seed,
        )

    @property
    def bootstrap_plan_bindings(self) -> BootstrapPlanBindings:
        identities = (
            (
                CredentialIdentity.SUPER_ADMIN,
                "superadmin@test.com",
                101,
            ),
            (
                CredentialIdentity.ORGANIZATION_ADMIN,
                "orgadmin@example.test",
                201,
            ),
            (
                CredentialIdentity.RUNTIME_USER,
                "runtime@example.test",
                301,
            ),
        )
        credentials = tuple(
            CredentialPlanBinding(
                identity=identity,
                canonical_email=email,
                current_file=self.stat_binding(
                    f"credential:{identity.value}:current",
                    seed,
                ),
                candidate_file=None,
            )
            for identity, email, seed in identities
        )
        return BootstrapPlanBindings(
            credentials=credentials,
            invitation_probe=None,
            api_key_capture=None,
            api_key_cleanup=None,
            role_external_id="ifactory-pdm-runtime",
            api_key_label="ifactory-pdm-runtime",
            phase2_env_logical_id="predictive-maintenance-shadow.env",
            phase2_env_file=self.stat_binding("phase2:env", 401),
        )

    def make_snapshot(
        self,
        *,
        state_generation: int = 7,
        state_sha256: str | None = None,
    ) -> DeploymentSnapshot:
        return DeploymentSnapshot(
            source=SourceBinding(
                root_sha="1" * 40,
                root_dirty_fingerprint="0" * 64,
                root_status=SourceStatus.CLEAN,
                cmms_gitlink="2" * 40,
                cmms_head="2" * 40,
                cmms_dirty_fingerprint="0" * 64,
                cmms_status=SourceStatus.CLEAN,
            ),
            config_sha256="3" * 64,
            toolchain_manifest_sha256="4" * 64,
            sensitive_manifest_sha256="5" * 64,
            unit_generation="6" * 64,
            state_generation=state_generation,
            state_sha256=state_sha256 or "7" * 64,
        )

    def make_plan(
        self,
        *,
        operation: Operation = Operation.START,
        profile: RuntimeProfile = RuntimeProfile.DEVELOPMENT,
        license_mode: LicenseMode = LicenseMode.OFFLINE,
        actions: tuple[PlannedAction, ...] | None = None,
        bootstrap_bindings: BootstrapPlanBindings | None = None,
        now: datetime | None = None,
        plan_nonce: str = "a" * 32,
    ) -> DeploymentPlan:
        selected_actions = actions
        selected_bindings = bootstrap_bindings
        if selected_actions is None:
            if operation is Operation.STOP:
                selected_actions = (
                    PlannedAction(ActionCode.GATEWAY_FAIL_CLOSED),
                    PlannedAction(ActionCode.PROCESS_STOP_FRONTEND),
                    PlannedAction(ActionCode.PROCESS_STOP_API),
                    PlannedAction(
                        ActionCode.COMPOSE_STOP_STATE_GATEWAY,
                        ActionTargetKind.COMPOSE_RESOURCE_SET,
                        "compose:ifactory-cmms-dev",
                    ),
                )
                selected_bindings = None
            else:
                selected_actions = (
                    PlannedAction(ActionCode.GATEWAY_FAIL_CLOSED),
                    PlannedAction(ActionCode.READINESS_REQUIRE_LOOPBACK),
                    PlannedAction(ActionCode.GATEWAY_ENABLE_DUAL),
                    PlannedAction(ActionCode.READINESS_REQUIRE_DUAL),
                )
                if selected_bindings is None:
                    selected_bindings = self.bootstrap_plan_bindings
        return DeploymentPlan.create(
            snapshot=self.make_snapshot(),
            operation=operation,
            profile=profile,
            license_mode=license_mode,
            bootstrap_bindings=selected_bindings,
            actions=selected_actions,
            now=now or datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc),
            plan_nonce=plan_nonce,
        )

    def acquire_deployment_write_lease(self, reservation: Any) -> Any:
        return acquire_deployment_write_lease(self.lock_path, reservation)

    def gateway_authority(
        self,
        confirmed: Any,
        lease: Any,
    ) -> GatewayAuthorityTestAdapter:
        return GatewayAuthorityTestAdapter(confirmed, lease)

    def claimed_context(
        self,
        *,
        plan_nonce: str = "b" * 32,
        application_id: str = "c" * 32,
    ) -> Any:
        plan = self.make_plan(plan_nonce=plan_nonce)
        plan_path, plan_hash = write_plan(plan, self.plans_dir)
        reservation = reserve_plan_attempt(
            plan_path,
            confirmed_sha256=plan_hash,
            now=plan.created_at,
            plans_dir=self.plans_dir,
            application_id=application_id,
        )
        lease = self.acquire_deployment_write_lease(reservation)
        confirmed = load_confirmed_plan(
            reservation,
            snapshot=plan.snapshot,
            current_bootstrap_bindings=plan.bootstrap_bindings,
            lease=lease,
        )
        evidence = self.gateway_authority(confirmed, lease).issue()
        return claim_plan_application(
            confirmed,
            self.plans_dir,
            lease,
            evidence,
        )


class GatewayAuthorityTestAdapter:
    """Tests-only socket-free driver for the internal gateway authority pair."""

    def __init__(self, confirmed: Any, lease: Any) -> None:
        self.confirmed = confirmed
        self.lease = lease
        self.parts = _create_gateway_fail_closed_authority_parts()
        self.gateway_generation = "8" * 64
        self.loopback_gateway_sha256 = "9" * 64
        self.gateway_ipv4 = "172.17.0.1"

    def challenge(self) -> Any:
        return self.parts.challenge_issuer.begin(
            self.confirmed,
            self.lease,
            self.gateway_generation,
            self.loopback_gateway_sha256,
            self.gateway_ipv4,
        )

    def prove(self, challenge: Any) -> Any:
        return self.parts.listener_proof_mint.mint_absent(
            challenge,
            loaded_generation=self.gateway_generation,
            loaded_sha256=self.loopback_gateway_sha256,
            checked_ipv4=self.gateway_ipv4,
            checked_port=3000,
            listener_count=0,
        )

    def issue(self) -> Any:
        challenge = self.challenge()
        proof = self.prove(challenge)
        return self.parts.evidence_issuer.issue(
            self.confirmed,
            self.lease,
            proof,
        )


@dataclass(frozen=True)
class CmmsSourceFixture:
    root: Path
    head: str
    dirty_paths: tuple[str, ...] = ()

    @property
    def is_clean(self) -> bool:
        return not self.dirty_paths


@dataclass
class ScriptedRunner:
    outcomes: deque[Any] = field(default_factory=deque)
    calls: list[Any] = field(default_factory=list)

    @classmethod
    def from_outcomes(cls, outcomes: Collection[Any]) -> ScriptedRunner:
        return cls(deque(outcomes))

    def run(self, spec: Any) -> Any:
        self.calls.append(spec)
        if not self.outcomes:
            raise AssertionError("unexpected process invocation")
        outcome = self.outcomes.popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@dataclass
class ScriptedHttpTransport:
    outcomes: deque[Any] = field(default_factory=deque)
    calls: list[tuple[str, str, bytes | None, dict[str, str]]] = field(
        default_factory=list
    )

    @classmethod
    def from_outcomes(cls, outcomes: Collection[Any]) -> ScriptedHttpTransport:
        return cls(deque(outcomes))

    def request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        self.calls.append((method, url, body, dict(headers or {})))
        if not self.outcomes:
            raise AssertionError("unexpected HTTP invocation")
        outcome = self.outcomes.popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def make_private_file(path: Path, data: bytes) -> Path:
    """Small compatibility helper for tests that do not need a fixture object."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.chmod(path, 0o600)
    return path
