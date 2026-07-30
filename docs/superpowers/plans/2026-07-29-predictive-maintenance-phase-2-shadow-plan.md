# Predictive Maintenance Phase 2 Shadow Prediction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provision the 20 isolated pilot devices/assets and run deterministic 15-minute shadow predictions without creating a ThingsBoard PDM Alarm or exposing any maintenance Action.

**Architecture:** PDM loads six generated repeat-last fixtures from a read-only volume. `platform-integration` stores tenant/equipment/measurement mappings, creates lease-backed prediction runs, reads ThingsBoard history, invokes PDM v2, and persists only quality/risk summaries; provisioning is a separately confirmed two-step CLI.

**Tech Stack:** Python 3.12/uv/FastAPI/Pydantic/httpx/rfc8785/SQLAlchemy 2/Alembic/psycopg 3/PostgreSQL 16/pytest/Testcontainers, existing ThingsBoard automotive-factory dashboard generator, Docker Compose.

## Global Constraints

- Follow the master plan's fixed profile IDs, units, 1-minute sampling, 60-point context, 15-point horizon, scales, thresholds, artifact hashes, and `pdm-v2-pilot-1` preprocessing version.
- Phase 1 provider, consumer, contract, and fifth-submodule commits must already be integrated.
- Provisioning is an external write and requires a displayed target plan plus an exact user-confirmed plan hash before apply.
- Reserve and commit `equipment_id` plus the CMMS request digest before the first CMMS POST; every retry reuses the same payload.
- Phase 2 never creates, updates, acknowledges, or clears a `PDM_FORECAST_RISK` Alarm and never creates a work order.
- Only `SUCCEEDED` prediction runs advance risk/healthy counters. `FAILED`, `FAILED_STALE`, and `SKIPPED_DATA_QUALITY` reset both counters without changing an Alarm.
- Prediction data is supplied through PDM v2 `history`; PDM does not read a platform, ThingsBoard, or CMMS database.
- Persist request/input digests and prediction min/max/mean/exceedance counts, not raw telemetry or forecast arrays.
- Do not run any training API, training Skill, or CPU training smoke test.
- Every command block starts at the current implementation worktree's superproject root unless that block contains its own `cd`; no block relies on a previous block's working directory.

Use this exact integration settings surface throughout the phase:

```text
PLATFORM_INTEGRATION_DATABASE_URL
PLATFORM_INTEGRATION_ISOLATED_PILOT_MODE
PLATFORM_INTEGRATION_PDM_BASE_URL
PLATFORM_INTEGRATION_PDM_CREDENTIAL_REF
PLATFORM_INTEGRATION_TB_BASE_URL
PLATFORM_INTEGRATION_CMMS_BASE_URL
PLATFORM_INTEGRATION_TENANT_ALIAS
PLATFORM_INTEGRATION_TENANT_ID
PLATFORM_INTEGRATION_TB_TENANT_ID
PLATFORM_INTEGRATION_CMMS_COMPANY_ID
PLATFORM_INTEGRATION_TB_CREDENTIAL_REF
PLATFORM_INTEGRATION_CMMS_CREDENTIAL_REF
PLATFORM_INTEGRATION_CMMS_WEBHOOK_SECRET_REF
```

Base URLs and non-secret identity values may appear in the example environment file. Credential references name values supplied only by the untracked runtime environment or secret manager; the referenced credential values never appear in the tracked example.

Each credential reference is itself the exact environment-variable name matching `^[A-Z][A-Z0-9_]{2,127}$`. The provider performs a direct lookup with no prefix rewriting or interpolation and parses one strict JSON envelope:

```text
PDM:         {"kind":"opaque_bearer","value":"..."}
ThingsBoard: {"kind":"thingsboard_bearer","value":"..."}
CMMS:        {"kind":"cmms_api_key","value":"..."}
```

PDM sends `Authorization: Bearer`, ThingsBoard sends `X-Authorization: Bearer`, and CMMS sends `x-api-key`. The provider rejects a wrong kind, blank value, unknown key, or extra JSON field and redacts the complete value from logs/errors.

---

### Task 0: Resume the Exact Phase 1 Component Branches

**Files:** None.

**Interfaces:**

- Consumes: the Phase 1 superproject gitlinks and pushed feature branches
- Produces: clean attached component worktrees at those exact recorded commits

- [ ] **Step 1: Verify and attach PDM and integration branches**

Run from the superproject root:

```bash
set -Eeuo pipefail
test -z "$(git -C components/pdm-algorithm status --short)"
test -z "$(git -C components/platform-integration status --short)"
pdm_phase1_tip="$(git rev-parse HEAD:components/pdm-algorithm)"
integration_phase1_tip="$(git rev-parse HEAD:components/platform-integration)"
git -C components/pdm-algorithm fetch origin feat/pdm-prediction-v2
git -C components/platform-integration fetch origin feat/predictive-maintenance-pilot
git -C components/pdm-algorithm merge-base --is-ancestor \
  "$pdm_phase1_tip" origin/feat/pdm-prediction-v2
git -C components/platform-integration merge-base --is-ancestor \
  "$integration_phase1_tip" origin/feat/predictive-maintenance-pilot
git -C components/pdm-algorithm switch feat/pdm-prediction-v2
git -C components/platform-integration switch feat/predictive-maintenance-pilot
test "$(git -C components/pdm-algorithm rev-parse HEAD)" = "$pdm_phase1_tip"
test "$(git -C components/platform-integration rev-parse HEAD)" = \
  "$integration_phase1_tip"
```

Expected: both worktrees are attached to the Phase 1 branches, their heads equal the recorded Phase 1 gitlinks, and neither worktree is dirty. Stop instead of merging or resetting if any assertion fails.

### Task 1: Generate and Validate Six Isolated PDM Fixtures

**Files:**

- Create: `components/pdm-algorithm/src/valeo_pdm/prediction_v2/fixture_generator.py`
- Create: `components/pdm-algorithm/configs/isolated_fixture_manifest.yaml`
- Create: `components/pdm-algorithm/tests/test_prediction_v2_fixtures.py`
- Create: `components/pdm-algorithm/tests/test_prediction_v2_readiness.py`
- Modify: `components/pdm-algorithm/src/valeo_pdm/prediction_v2/catalog.py`
- Modify: `components/pdm-algorithm/src/valeo_pdm/api/app.py`
- Modify: `components/pdm-algorithm/src/valeo_pdm/cli.py`
- Modify: `components/pdm-algorithm/tests/test_api_startup.py`
- Modify: `components/pdm-algorithm/README.md`
- Modify: `components/pdm-algorithm/AGENTS.md`

**Interfaces:**

- Produces: `build_fixture_artifact(profile: ModelProfile) -> bytes`
- Produces: `generate_isolated_fixtures(manifest_path: Path, output_root: Path) -> dict[str, str]`
- Produces: `valeo-pdm prepare-isolated-fixtures --manifest configs/isolated_fixture_manifest.yaml --output .runtime/pdm-fixtures`
- Produces: `GET /readyz`
- Consumes: six fixed profile rows and expected hashes from the master plan

- [ ] **Step 1: Write fixture and readiness tests**

Tests must assert:

- output directory must not exist or must be empty;
- exactly six canonical JSON artifacts and one runtime manifest are generated;
- layout is exactly `manifest.runtime.yaml` plus `objects/{model_profile_id}.json`;
- generated bytes contain no newline and hash to the six fixed SHA-256 values;
- generator never creates `.pt`, `artifacts/`, or training manifests;
- a missing/wrong artifact causes `/readyz` to return `503`;
- all six exact hashes cause `/readyz` to return `200`;
- changing unit, value scale, request-window points, context, horizon, profile/model identity, or preprocessing metadata causes `/readyz` to return `503`;
- `VALEO_PDM_ISOLATED_FIXTURE_MODE=1` requires a non-empty `VALEO_PDM_ALLOWED_TENANT_IDS` UUID allowlist;
- isolated mode requires a non-empty
  `VALEO_PDM_PREDICTION_V2_BEARER_TOKEN`; a missing or blank token fails startup
  and `/readyz` can never return `200`;
- invalid UUIDs, `*`, and a request tenant outside that allowlist are rejected;
- a manifest declaring `fixture_mode: isolated-pilot` fails application startup unless isolated fixture mode is enabled.

Run:

```bash
set -Eeuo pipefail
cd components/pdm-algorithm
uv run pytest \
  tests/test_prediction_v2_fixtures.py \
  tests/test_prediction_v2_readiness.py -v
```

Expected: FAIL because generator and readiness are absent.

- [ ] **Step 2: Add the exact manifest**

The manifest has top-level `fixture_mode: isolated-pilot`. Each entry includes:

```yaml
kind: repeat-last
sampling_frequency: 1min
request_window_points: 66
context_points: 60
horizon_points: 15
preprocessing_version: pdm-v2-pilot-1
```

Add the exact profile/model/meas/unit/scale/hash values from the master table. The manifest is tenant-agnostic. `ModelCatalog.resolve()` separately requires the exact request `tenant_id` to appear in the environment-backed UUID allowlist and rejects every other tenant. Contract and isolated E2E tests use `00000000-0000-4000-8000-000000000001`; a real deployment injects the provisioned platform tenant UUID through an untracked secret environment file.

Each runtime entry also contains `profile_config_sha256`, computed from this exact RFC 8785 projection:

```json
{"context_points":60,"horizon_points":15,"kind":"repeat-last","meas_code":"vibration_rms","model_artifact_sha256":"5feeb31058fe0521f94758faa22214afe4619e466e22cbb8c6fb7ffcf2369562","model_info_id":"pilot-fixture-v1-cnc-vibration","model_profile_id":"pilot-cnc-vibration","preprocessing_version":"pdm-v2-pilot-1","request_window_points":66,"sampling_frequency":"1min","unit":"mm/s","value_scale":2}
```

Expected profile-configuration hashes are:

```text
pilot-cnc-vibration          4db612fb9af5095479fc5ecd03e2118d848f79e5c149073fce2df10cf34c64c1
pilot-injection-pressure     9c37acf828421f27f25b32c10453ff74c0108993822af2997e5379879406b390
pilot-robot-position         a33b9a84f88c2c5c78d99a0ccd222422c145cb495e4fe8c5b289010f4e768368
pilot-tightening-torque      43c9a68832b2030051dca0a320c13bc526b8ae58d810bc0e44052872d836cac8
pilot-compressor-pressure    6c955b29105ce34724dac26280ee2ff3f428e55c5e5269f40c6e32860ccfa1e8
pilot-eol-pass-rate          61f0d0323b511999351203cc0017e5c9cd5e646fa7366a3616607c01b781650b
```

- [ ] **Step 3: Implement safe generation and readiness**

`build_fixture_artifact()` returns:

```python
rfc8785.dumps(
    {
        "kind": "repeat-last",
        "model_info_id": profile.model_info_id,
        "model_profile_id": profile.model_profile_id,
        "preprocessing_version": profile.preprocessing_version,
        "schema_version": 1,
    }
)
```

Generation uses exclusive file creation and refuses symlinks or overwrite. Runtime only reads the generated directory. `/healthz` remains process-only; `/readyz` calls `ModelCatalog.readiness()`, hashes the actual object bytes, rebuilds every profile configuration projection, checks both fixed hashes, and returns no absolute path.

The generated layout is:

```text
manifest.runtime.yaml
objects/pilot-cnc-vibration.json
objects/pilot-injection-pressure.json
objects/pilot-robot-position.json
objects/pilot-tightening-torque.json
objects/pilot-compressor-pressure.json
objects/pilot-eol-pass-rate.json
```

Run:

```bash
set -Eeuo pipefail
uv run --directory components/pdm-algorithm --frozen pytest \
  tests/test_prediction_v2_fixtures.py \
  tests/test_prediction_v2_readiness.py \
  tests/test_prediction_v2_api.py \
  tests/test_api_startup.py -v
uv run --directory components/pdm-algorithm --frozen ruff check .
uv run --directory components/pdm-algorithm --frozen ruff format --check .
```

Expected: all tests/checks pass.

- [ ] **Step 4: Commit and push the PDM fixture slice**

Run:

```bash
set -Eeuo pipefail
./scripts/doctor.sh
git -C components/pdm-algorithm add \
  AGENTS.md README.md configs/isolated_fixture_manifest.yaml \
  src/valeo_pdm/api/app.py \
  src/valeo_pdm/cli.py \
  src/valeo_pdm/prediction_v2 \
  tests/test_prediction_v2_fixtures.py \
  tests/test_prediction_v2_readiness.py \
  tests/test_api_startup.py
git -C components/pdm-algorithm commit \
  -m "feat: add isolated prediction fixtures"
git -C components/pdm-algorithm push
```

### Task 2: Add Shadow-Prediction Persistence and PostgreSQL Migrations

**Files:**

- Create: `components/platform-integration/alembic.ini`
- Create: `components/platform-integration/migrations/env.py`
- Create: `components/platform-integration/migrations/script.py.mako`
- Create: `components/platform-integration/migrations/versions/0001_shadow_foundation.py`
- Create: `components/platform-integration/src/platform_integration/db.py`
- Create: `components/platform-integration/src/platform_integration/models/__init__.py`
- Create: `components/platform-integration/src/platform_integration/models/bindings.py`
- Create: `components/platform-integration/src/platform_integration/models/prediction.py`
- Create: `components/platform-integration/src/platform_integration/models/audit.py`
- Create: `components/platform-integration/src/platform_integration/repositories/bindings.py`
- Create: `components/platform-integration/src/platform_integration/repositories/prediction_runs.py`
- Create: `components/platform-integration/tests/integration/test_shadow_migrations.py`
- Create: `components/platform-integration/tests/repositories/test_binding_constraints.py`
- Create: `components/platform-integration/tests/repositories/test_prediction_run_leases.py`
- Modify: `components/platform-integration/src/platform_integration/config.py`

**Interfaces:**

- Produces: `async_sessionmaker[AsyncSession]`
- Produces: `BindingRepository`
- Produces: `PredictionRunRepository.create_slot(*, tenant_id: UUID, equipment_id: UUID, meas_code: str, scheduled_at: datetime) -> PredictionRun`
- Produces: `PredictionRunRepository.claim(run_id, owner, now) -> PredictionRun | None`
- Produces: `PredictionRunRepository.heartbeat(run_id, owner, now) -> bool`
- Produces: `PredictionRunRepository.reconcile_stale(now) -> list[UUID]`
- Consumes: PostgreSQL 16

- [ ] **Step 1: Write PostgreSQL constraint and lease tests**

Use Testcontainers PostgreSQL, run `alembic upgrade head`, then assert:

- independent unique constraints on `tenant_id`, `tb_tenant_id`, and `cmms_company_id`, plus `tenant_binding.enabled NOT NULL`;
- unique equipment mapping by tenant/equipment, tenant/TB device, and tenant/CMMS asset;
- unique measurement binding by tenant/equipment/meas and tenant/equipment/telemetry key;
- unique prediction slot `(tenant_id,equipment_id,meas_code,scheduled_at)`;
- one worker claims a run, another cannot;
- heartbeat extends only the owner's lease;
- 10-minute expired `RUNNING` becomes `FAILED_STALE`;
- the same run identity can be reclaimed up to attempt 3 and no second slot row appears.

Run:

```bash
set -Eeuo pipefail
cd components/platform-integration
uv run pytest \
  tests/integration/test_shadow_migrations.py \
  tests/repositories/test_binding_constraints.py \
  tests/repositories/test_prediction_run_leases.py -v
```

Expected: FAIL because migrations and repositories do not exist.

- [ ] **Step 2: Implement the Phase 2 schema**

Migration `0001_shadow_foundation` creates:

```text
tenant_binding
equipment_mapping
measurement_binding
provisioning_plan
prediction_run
risk_evaluation_state
audit_event
```

Use UUID for platform tenant/equipment/run domain IDs and ThingsBoard tenant/device/user IDs. Use `BIGINT` for CMMS company, asset, and work-order IDs, matching the Java `Long` contract. Use `TIMESTAMPTZ`, PostgreSQL `JSONB` only for bounded summaries, and numeric threshold fields with declared precision. `equipment_mapping.status` is `RESERVED` or `ACTIVE`. `prediction_run.status` is `PENDING`, `RUNNING`, `SUCCEEDED`, `SKIPPED_DATA_QUALITY`, `FAILED`, or `FAILED_STALE`.

`measurement_binding` contains exact columns for:

```text
tenant_id
equipment_id
meas_code
telemetry_key
unit
sampling_frequency
request_window_points
context_points
horizon_points
value_scale
model_profile_id
model_info_id
preprocessing_version
policy_version
risk_direction
risk_threshold
enabled
```

Use `ABOVE` or `BELOW` for `risk_direction`; store `risk_threshold` as a fixed-precision numeric matching the profile scale.

`provisioning_plan` stores the immutable canonical plan/hash, tenant/company
snapshot, actor, 30-minute expiry, nullable applied timestamp, bounded
per-target result hashes/codes, and terminal result. Unique
`(tenant_id,plan_hash)` plus a row lock makes apply single-use. This
secret-free durable row is the provisioning receipt and read-back source.

`tenant_binding` contains `tenant_id`, unique `alias`, unique `tb_tenant_id`, unique `cmms_company_id`, `pdm_credential_ref`, `tb_credential_ref`, `cmms_credential_ref`, nullable future `cmms_webhook_secret_ref`, and `enabled NOT NULL`. Store only secret reference names; actual strict credential envelopes come from the environment or a secret manager.

- [ ] **Step 3: Implement lease-safe repositories**

Claim with `SELECT ... FOR UPDATE SKIP LOCKED`, set:

```text
lease_owner
lease_expires_at = now + 10 minutes
heartbeat_at = now
attempt = attempt + 1
```

Heartbeat every 30 seconds. Stale reconciliation updates the existing row; it never inserts a replacement slot row.

Run the three tests again; expected PASS.

- [ ] **Step 4: Verify migration reversibility**

Run:

```bash
set -Eeuo pipefail
uv run --directory components/platform-integration --frozen pytest \
  tests/integration/test_shadow_migrations.py -v
```

Expected: the Testcontainers test gives Alembic only its temporary PostgreSQL URL, runs `upgrade head → downgrade base → upgrade head`, and passes. Never run a bare migration command against an ambient `DATABASE_URL`.

### Task 3: Show Platform Mapping Attributes in the ThingsBoard Pilot Dashboard

**Files:**

- Create: `components/thingsboard/dev/automotive-factory/factory_simulator/dashboard_publication.py`
- Create: `components/thingsboard/dev/automotive-factory/tests/test_dashboard_publication.py`
- Modify: `components/thingsboard/dev/automotive-factory/dev.sh`
- Modify: `components/thingsboard/dev/automotive-factory/.env.example`
- Modify: `components/thingsboard/dev/automotive-factory/factory_simulator/cli.py`
- Modify: `components/thingsboard/dev/automotive-factory/factory_simulator/dashboard.py`
- Modify: `components/thingsboard/dev/automotive-factory/tests/test_simulator.py`
- Modify: `components/thingsboard/dev/automotive-factory/README.md`

**Interfaces:**

- Produces: read-only device table columns `equipment_id` and `cmms_asset_id`
- Produces: `./dev.sh dashboard-plan` and separately confirmed
  `./dev.sh dashboard-apply`
- Consumes: ThingsBoard `SERVER_SCOPE` attributes written later by confirmed provisioning

- [ ] **Step 1: Branch from the customized ThingsBoard line**

Run:

```bash
set -Eeuo pipefail
git -C components/thingsboard fetch origin dev
git -C components/thingsboard merge-base --is-ancestor \
  172547d7193a3082984b2a5f5b595fce947020fc \
  origin/dev
git -C components/thingsboard switch -c \
  feat/predictive-maintenance-dashboard \
  172547d7193a3082984b2a5f5b595fce947020fc
```

Expected: branch starts at the automotive-factory customization, not upstream.

- [ ] **Step 2: Write the failing dashboard test**

Extend the current dashboard test to assert:

```python
assert ("equipment_id", "attribute") in table_keys
assert ("cmms_asset_id", "attribute") in table_keys
assert alarm_widget["config"]["actions"] == {}
```

Add publication tests proving:

- `dashboard-plan` performs only GET requests;
- the canonical 30-minute plan binds the server-derived tenant UUID, existing
  dashboard ID/version and body hash when present, exact desired body hash,
  actor, generated correlation ID, and expiry without credentials;
- `dashboard-apply` requires a later confirmed hash, rechecks tenant/current
  dashboard/body/version and refuses any drift before POST;
- an already matching desired dashboard succeeds without POST;
- a response-loss retry first GETs the exact dashboard and treats only the
  matching desired hash as success;
- apply writes a mode-`0600` non-secret receipt and cannot be bypassed by the
  legacy `dashboard` command when predictive-maintenance publication is in
  managed mode.

Run:

```bash
set -Eeuo pipefail
components/thingsboard/dev/automotive-factory/dev.sh prepare
cd components/thingsboard/dev/automotive-factory
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

Expected: FAIL because mapping columns and confirmed publication do not exist.
The explicit prepare step creates the ignored local virtual environment before
the first Python command in the isolated implementation worktree.

- [ ] **Step 3: Add attribute columns and confirmed publication without generating IDs**

Modify `_device_table_config()` to add Server Attribute keys. Do not change `api.py::provision_devices()` and do not let the simulator generate `equipment_id` or `cmms_asset_id`.

`dashboard-plan` builds the desired payload locally, authenticates only to
derive the current tenant and dashboard snapshot, writes an owner-only plan,
and prints its canonical SHA-256. `dashboard-apply` requires both the saved
plan hash and the identical later user-confirmed hash, revalidates every
snapshot field, then performs at most one save call. It records the returned
dashboard ID/version, plan hash, correlation ID, actor, response hash, and
timestamp in an owner-only receipt. Neither file contains a token.

The existing `dashboard` command remains available only for unmanaged
pre-pilot local use. Once `TB_PDM_DASHBOARD_MANAGED_PUBLICATION=true`, it
refuses to write and directs the operator to plan/apply. The tracked
`.env.example` defaults this flag to `true` for the pilot and includes an empty
`TB_PDM_EXPECTED_TENANT_ID=`. Both plan and apply require that field to be a
canonical UUID equal to the authenticated ThingsBoard tenant; the operator
copies the read-only discovery result into the untracked component `.env`.

- [ ] **Step 4: Run dashboard tests**

Run the unittest command again; expected all tests PASS, Alarm actions remain
empty, and the publication tests make no uncontrolled writes.

- [ ] **Step 5: Commit and push the mapping display**

Run:

```bash
set -Eeuo pipefail
./scripts/doctor.sh
git -C components/thingsboard add \
  dev/automotive-factory/.env.example \
  dev/automotive-factory/dev.sh \
  dev/automotive-factory/factory_simulator/cli.py \
  dev/automotive-factory/factory_simulator/dashboard.py \
  dev/automotive-factory/factory_simulator/dashboard_publication.py \
  dev/automotive-factory/tests/test_dashboard_publication.py \
  dev/automotive-factory/tests/test_simulator.py \
  dev/automotive-factory/README.md
git -C components/thingsboard commit \
  -m "feat: add managed predictive maintenance dashboard"
git -C components/thingsboard push -u origin \
  feat/predictive-maintenance-dashboard
```

### Task 4: Implement Confirmed, Recoverable Pilot Provisioning

**Files:**

- Create: `components/platform-integration/src/platform_integration/clients/thingsboard.py`
- Create: `components/platform-integration/src/platform_integration/services/tenant_bindings.py`
- Create: `components/platform-integration/src/platform_integration/services/provisioning.py`
- Create: `components/platform-integration/src/platform_integration/commands/provision.py`
- Create: `components/platform-integration/tests/clients/test_thingsboard_client.py`
- Create: `components/platform-integration/tests/services/test_tenant_bindings.py`
- Create: `components/platform-integration/tests/services/test_provisioning.py`
- Create: `components/platform-integration/tests/integration/test_provisioning_recovery.py`
- Modify: `components/platform-integration/src/platform_integration/credentials.py`
- Modify: `components/platform-integration/src/platform_integration/clients/cmms.py`
- Modify: `components/platform-integration/src/platform_integration/cli.py`
- Modify: `components/platform-integration/tests/clients/test_cmms_client.py`

**Interfaces:**

- Produces: `ProvisioningService.build_plan(tenant_id, actor) -> ProvisioningPlan`
- Produces: `ProvisioningService.apply(plan_hash, confirmed_hash, actor) -> ProvisioningResult`
- Produces: `platform-integration provision-plan`
- Produces: `platform-integration provision-apply` and read-only
  `platform-integration provision-verify`
- Consumes: ThingsBoard device/attribute REST and Phase 1 CMMS asset API

- [ ] **Step 1: Write plan/apply and response-loss tests**

Tests must assert:

- plan contains exactly 20 device names/IDs, proposed asset names, profile bindings, and no token;
- all devices match one of the exact case-sensitive simulator keys `CNC`, `INJECTION_MOLDING`, `ASSEMBLY_ROBOT`, `TIGHTENING`, `AIR_COMPRESSOR`, or `EOL_TESTER`; display labels and unknown types fail before writes;
- alias `ifactory-pilot` resolves only to configured platform tenant UUID `00000000-0000-4000-8000-000000000001` in the isolated deployment, and any alias/UUID mismatch fails before writes;
- plan hash changes if any target/payload changes;
- bootstrap rejects any configured TB tenant or CMMS company ID that differs from the authenticated identity discovered from ThingsBoard and CMMS;
- before any tenant-binding, plan, reservation, asset, or attribute write, the
  discovered isolated TB tenant has zero active `PDM_FORECAST_RISK` Alarms
  across the exact 20 target devices and the discovered CMMS company has zero
  total work orders; a non-zero baseline aborts and instructs the operator to
  use a clean isolated environment, never to auto-delete records;
- apply rejects an absent, expired, already applied, or non-matching confirmed hash;
- `equipment_id` and normalized CMMS request digest are committed as `RESERVED` before POST;
- a CMMS write-response timeout is followed by GET by `equipment_id`, never blind POST;
- a ThingsBoard attribute timeout is followed by attribute readback;
- rerun reuses the exact UUID/payload and finishes `ACTIVE`;
- apply upserts the fixed profile's complete measurement binding, including `request_window_points=66`, and a rerun rejects any conflicting persisted field instead of silently replacing it;
- apply atomically records a durable, secret-free receipt with the confirmed
  plan hash, IDs, result hashes/codes, actor, and terminal result;
- `provision-verify` reads only the exact tenant/hash receipt and proves all 20
  targets completed; an expired/drifted/concurrent apply performs no partial
  writes;
- MockTransport proves that PDM accepts only `opaque_bearer` and sends
  `Authorization: Bearer`, ThingsBoard accepts only `thingsboard_bearer` and
  sends `X-Authorization: Bearer`, and CMMS accepts only `cmms_api_key` and
  sends `x-api-key`; wrong kinds fail before I/O and no secret appears in logs
  or exceptions.

Run:

```bash
set -Eeuo pipefail
cd components/platform-integration
uv run pytest \
  tests/clients/test_thingsboard_client.py \
  tests/services/test_tenant_bindings.py \
  tests/services/test_provisioning.py \
  tests/integration/test_provisioning_recovery.py -v
```

Expected: FAIL because provisioning does not exist.

- [ ] **Step 2: Implement exact credential-bound client primitives**

Use exact endpoints:

```text
GET  /api/tenant/devices?pageSize=100&page=0
POST /api/plugins/telemetry/DEVICE/{tb_device_id}/attributes/SERVER_SCOPE
GET  /api/plugins/telemetry/DEVICE/{tb_device_id}/values/attributes/SERVER_SCOPE
     ?keys=equipment_id,cmms_asset_id
```

Extend the Phase 1 credential provider with the three exact envelope kinds from
Global Constraints. The ThingsBoard client accepts only
`thingsboard_bearer` and sends `X-Authorization: Bearer {value}`. The CMMS
client accepts only `cmms_api_key` and sends `x-api-key: {value}`. Retrieve the
credential for each call so rotation does not require reconstructing a client;
never persist or log an envelope, header, or secret.

- [ ] **Step 3: Implement two-step provisioning**

Before storing a plan, authenticate through exact read-only endpoints:

```text
ThingsBoard GET /api/auth/user -> strict UUID response tenantId.id
CMMS         GET /api/auth/me   -> strict positive BIGINT response companyId
```

Require 2xx responses and require those identities to equal the configured TB tenant UUID and CMMS company ID. Upsert the enabled `tenant_binding` only after both checks pass. `build_plan()` sorts devices by immutable TB UUID, maps their device type through the fixed master profile table, and persists a canonical plan for 30 minutes. `apply()` requires `confirmed_hash == plan_hash`.

Before that upsert or plan persistence, perform the read-only isolation
baseline gate against the identities just discovered:

```text
for each of the exact 20 target TB device UUIDs:
  GET /api/v2/alarm/DEVICE/{id}
      ?pageSize=1&page=0&statusList=ACTIVE&typeList=PDM_FORECAST_RISK
  require totalElements == 0

CMMS POST /api/work-orders/search
  body={"filterFields":[],"direction":"ASC","pageNum":0,
        "pageSize":1,"sortField":"id"}
  require totalElements == 0
```

Both responses are tenant/company-scoped by the authenticated credentials.
Any non-zero result aborts before all local or external provisioning writes,
emits only counts and canonical non-secret identity, and requires a different
clean isolated environment. It must never clear Alarms, delete work orders, or
offer an automatic reset.

For each target:

```text
transaction: reserve/reuse UUID + request digest
CMMS GET equipment_id
CMMS POST only when absent, Idempotency-Key=pilot-asset:{tb_device_id}
ThingsBoard write equipment_id/cmms_asset_id
read both systems
transaction: upsert/verify the exact measurement binding, mark mapping ACTIVE, and audit
```

The measurement-binding payload contains `meas_code`, telemetry key, unit, sampling frequency, request-window/context/horizon points, value scale, model profile/info IDs, preprocessing/policy versions, risk direction, threshold, and enabled state from the fixed master table. Do not use a database distributed transaction.

- [ ] **Step 4: Verify and commit provisioning**

Run:

```bash
set -Eeuo pipefail
uv run --directory components/platform-integration --frozen pytest \
  tests/clients/test_thingsboard_client.py \
  tests/services/test_tenant_bindings.py \
  tests/services/test_provisioning.py \
  tests/integration/test_provisioning_recovery.py -v
uv run --directory components/platform-integration --frozen ruff check .
uv run --directory components/platform-integration --frozen ruff format --check .
```

Expected: all tests pass.

Commit:

```bash
set -Eeuo pipefail
./scripts/doctor.sh
git -C components/platform-integration add \
  alembic.ini migrations pyproject.toml uv.lock src tests
git -C components/platform-integration commit \
  -m "feat: add confirmed pilot provisioning"
git -C components/platform-integration push
```

### Task 5: Run Lease-Backed Shadow Predictions and Persist Risk State

**Files:**

- Create: `components/platform-integration/src/platform_integration/services/data_quality.py`
- Create: `components/platform-integration/src/platform_integration/services/prediction_requests.py`
- Create: `components/platform-integration/src/platform_integration/services/prediction_runs.py`
- Create: `components/platform-integration/src/platform_integration/services/risk.py`
- Create: `components/platform-integration/src/platform_integration/services/shadow_summary.py`
- Create: `components/platform-integration/src/platform_integration/workers/scheduler.py`
- Create: `components/platform-integration/src/platform_integration/workers/prediction.py`
- Create: `components/platform-integration/tests/services/test_data_quality.py`
- Create: `components/platform-integration/tests/services/test_prediction_requests.py`
- Create: `components/platform-integration/tests/services/test_risk.py`
- Create: `components/platform-integration/tests/services/test_shadow_summary.py`
- Create: `components/platform-integration/tests/workers/test_scheduler.py`
- Create: `components/platform-integration/tests/workers/test_prediction_worker.py`
- Create: `components/platform-integration/tests/test_image_contract.py`
- Modify: `components/platform-integration/tests/test_cli.py`
- Modify: `components/platform-integration/Dockerfile`
- Modify: `components/platform-integration/src/platform_integration/clients/thingsboard.py`
- Modify: `components/platform-integration/src/platform_integration/cli.py`

**Interfaces:**

- Produces: `floor_slot(now: datetime, interval_minutes: int = 15) -> datetime`
- Produces: `PredictionRequestBuilder.build(binding, points, correlation_id) -> PredictionRequestV2`
- Produces: `RiskEvaluator.evaluate(binding, forecast) -> RoundRisk`
- Produces: `platform-integration migrate`, `platform-integration discover-identities`, `platform-integration scheduler`, `platform-integration prediction-worker`, and `platform-integration shadow-summary` console roles
- Consumes: ThingsBoard historical telemetry and PDM v2

- [ ] **Step 1: Write data-quality and request tests**

Use the exact ThingsBoard query:

```text
GET /api/plugins/telemetry/DEVICE/{tb_device_id}/values/timeseries
    ?keys={telemetry_key}
    &startTs={start_ms}
    &endTs={end_ms}
    &interval=60000
    &agg=AVG
    &orderBy=ASC
```

Tests cover stable data IDs, Decimal scale, unit matching, an exact 66-bucket request window, duplicate raw rows, 60–66 distinct finite buckets after timestamp aggregation, at most 6 missing buckets, no more than two consecutive missing buckets, no future timestamp, request digest equality with PDM, and absence of raw history in the persisted summary.

Run:

```bash
set -Eeuo pipefail
uv run --directory components/platform-integration --frozen pytest \
  tests/services/test_data_quality.py \
  tests/services/test_prediction_requests.py -v
```

Expected: FAIL because the services are absent.

- [ ] **Step 2: Implement request preparation**

For a run scheduled at `scheduled_at`, query the half-open 66-bucket window `[scheduled_at - 66 minutes, scheduled_at)` by setting `startTs` to the first bucket and `endTs` to `scheduled_at - 1 millisecond`. Generate `data_id` from RFC 8785 bytes containing tenant, TB device, telemetry key, epoch milliseconds, canonical value, and unit. UUIDs use lowercase hyphenated JSON strings on both sides; Python `UUID` objects are never passed directly to `rfc8785.dumps()`.

Send every observed finite raw record as string-valued `history`, together with the exact integer `window_start` and exclusive `window_end`; duplicate timestamps mean history length may exceed 66. `request_digest` covers every stably sorted raw record. Never send a synthetic `"null"` value. PDM independently aggregates duplicate timestamps, requires 60–66 distinct finite buckets, rebuilds all 66 expected buckets, and inserts real JSON `null` values into its normalized-input digest. A quality failure returns a stable code such as `MISSING_RATIO_EXCEEDED`; never call PDM in that branch.

- [ ] **Step 3: Write scheduler, run-recovery, and risk tests**

Tests assert:

- repeated scheduler calls create one row per binding/slot;
- 20 active bindings create 20 runs;
- worker refreshes a 10-minute lease every 30 seconds;
- a stale run reuses the row and stops after attempt 3;
- PDM transient errors retry at most 3 times and not beyond the slot;
- at least two consecutive future threshold crossings mark the round risky;
- two risky successful rounds produce an internal active risk state;
- two healthy successful rounds clear it;
- failed/skipped rounds reset both counters;
- Phase 2 never invokes an Alarm mutation (create/update/ACK/clear); the only
  Alarm calls are the exact tenant-scoped baseline/final-zero v2 GETs, and tests
  fail on any other method or path.
- `migrate` exits non-zero on migration failure; `scheduler --once --now <RFC3339>` creates the complete slot batch; `prediction-worker --once` drains every eligible run present for that batch (20 in the pilot) up to a hard cap of 1,000 claims, exits non-zero if eligible rows remain at the cap, and otherwise exits only when no row is claimable; long-running roles handle `SIGTERM` without abandoning an owned lease.
- `--now` is rejected unless `PLATFORM_INTEGRATION_ISOLATED_PILOT_MODE=1`; production scheduling always uses the injected system clock.
- `shadow-summary --tenant-alias ifactory-pilot --scheduled-at <RFC3339> --format json`
  is read-only and emits bounded canonical JSON containing the tenant/slot,
  status counts, the 20 mapping identity tuples, and the distinct
  `(model_profile_id, model_artifact_sha256)` pairs, but no telemetry,
  forecasts, credentials, or database URL.

Run:

```bash
set -Eeuo pipefail
uv run --directory components/platform-integration --frozen pytest \
  tests/services/test_risk.py \
  tests/services/test_shadow_summary.py \
  tests/workers/test_scheduler.py \
  tests/workers/test_prediction_worker.py -v
```

Expected: FAIL before worker implementation.

- [ ] **Step 4: Implement shadow workers**

Risk counter, last processed `prediction_run_id`, and current internal risk state update in one transaction. Save:

```text
request_digest
input_digest
model_artifact_sha256
forecast_min
forecast_max
forecast_mean
threshold_crossing_count
quality_summary
```

Do not save `history` or the full forecast array. Do not create an `outbox_event` in Phase 2.

Add exact CLI roles:

```text
platform-integration migrate
platform-integration discover-identities --format env
platform-integration scheduler [--once] [--now RFC3339]
platform-integration prediction-worker [--once]
platform-integration shadow-summary --tenant-alias ALIAS \
  --scheduled-at RFC3339 --format json
```

`migrate` runs `alembic upgrade head`. `discover-identities` is read-only, permits the two expected identity settings to be absent, authenticates to ThingsBoard and CMMS, and prints only their non-secret canonical IDs as env assignments. Without `--once`, scheduler and worker use bounded polling loops and clean `SIGTERM` shutdown. Scheduler `--once` completes one slot; worker `--once` repeatedly claims and completes all eligible rows until none remain, subject to the fixed 1,000-claim safety cap described above. `shadow-summary` queries only the integration service's own tenant-scoped tables, sorts every list, enforces a 100-row output bound, and fails if alias/UUID or requested slot is not exact. All roles use the same `PLATFORM_INTEGRATION_*` Settings schema.

Update the runtime image so `alembic.ini`, the complete `migrations/` tree, package source, and locked runtime dependencies are present and readable by its non-root user. `test_image_contract.py` builds the image, starts temporary PostgreSQL, and proves the image command `platform-integration migrate` reaches Alembic `head`; an image that copies only `src/` must fail the test.

- [ ] **Step 5: Verify and commit shadow execution**

Run:

```bash
set -Eeuo pipefail
uv run --directory components/platform-integration --frozen pytest \
  tests/services \
  tests/repositories \
  tests/workers \
  tests/test_cli.py \
  tests/test_image_contract.py \
  tests/integration/test_shadow_migrations.py -v
uv run --directory components/platform-integration --frozen ruff check .
uv run --directory components/platform-integration --frozen ruff format --check .
uv sync --directory components/platform-integration --frozen
```

Expected: all tests/checks pass.

Commit:

```bash
set -Eeuo pipefail
./scripts/doctor.sh
git -C components/platform-integration add \
  Dockerfile src tests migrations
git -C components/platform-integration commit \
  -m "feat: run lease-backed shadow predictions"
git -C components/platform-integration push
```

### Task 6: Add the Isolated Shadow Deployment and Phase Gate

**Files:**

- Create: `deploy/compose/predictive-maintenance-shadow.yml`
- Create: `deploy/compose/predictive-maintenance-discovery.yml`
- Create: `deploy/compose/predictive-maintenance-shadow.env.example`
- Create: `deploy/compose/scripts/prepare-pdm-fixtures.sh`
- Create: `deploy/compose/scripts/reset-pdm-fixtures.sh`
- Create: `tests/e2e/conftest.py`
- Create: `tests/e2e/test_shadow_seed.py`
- Create: `tests/e2e/test_shadow_prediction.py`
- Create: `tests/e2e/support/pilot_api.py`
- Create: `tests/e2e/support/shadow_seed.py`
- Modify: `tests/pyproject.toml`
- Modify: `tests/uv.lock`
- Modify: `deploy/README.md`
- Modify: `tests/README.md`
- Modify: `.github/workflows/workspace-check.yml`
- Modify: `components/pdm-algorithm` gitlink
- Modify: `components/thingsboard` gitlink
- Modify: `components/platform-integration` gitlink

**Interfaces:**

- Produces: isolated PDM fixture volume mounted read-only
- Produces: a separate read-only identity-discovery Compose service
- Produces: integration API, scheduler, worker, and PostgreSQL services
- Produces: opt-in `pilot_e2e` test marker
- Produces: separately confirmed `shadow-seed` plan/apply/receipt
- Consumes: running local automotive-factory ThingsBoard and isolated CMMS

- [ ] **Step 1: Write the failing shadow E2E**

Helper tests prove `shadow_seed plan` is GET-only, freezes all 1,320 exact
telemetry target/timestamp/value tuples plus per-request body hashes, and
requires a later exact hash for a bounded apply. Apply revalidates tenant,
device identities, existing window state, and every body hash before its first
POST, recovers response loss by exact readback, writes a mode-`0600`
secret-free receipt, and refuses any partial drift or completed receipt.

The prediction test consumes that receipt, uses an accelerated clock, performs
no telemetry mutation itself, and asserts:

```python
assert summary.total_bindings == 20
assert summary.succeeded_runs == 20
assert summary.active_pdm_alarms == 0
assert summary.cmms_work_orders == 0
```

It also verifies all 20 reversible mappings and six artifact hashes.

Run:

```bash
set -Eeuo pipefail
uv run --directory tests pytest \
  e2e/test_shadow_seed.py -v
uv run --directory tests pytest \
  -m pilot_e2e e2e/test_shadow_prediction.py -v
```

Expected: FAIL because the deployment and services are not configured.

- [ ] **Step 2: Add fixture preparation and read-only mounts**

The preparation script first verifies the existing root `.gitignore` already excludes `.runtime/`, proves the repository root is the expected canonical path, rejects a symlinked `.runtime`, safely creates it with owner-only permissions when absent, requires `.runtime/pdm-fixtures` to be absent, generates into an exact temporary directory, and atomically renames it to `.runtime/pdm-fixtures`. It never overwrites an existing fixture directory. Its generation sequence is:

```bash
set -Eeuo pipefail
repo_root="$(git rev-parse --show-toplevel)"
test "$(pwd -P)" = "$(realpath "$repo_root")"
rg -q '^\.runtime/$' .gitignore
test ! -L .runtime
install -d -m 0700 .runtime
test "$(realpath .runtime)" = "$(realpath "$repo_root")/.runtime"
test ! -e .runtime/pdm-fixtures
fixture_staging_dir="$(mktemp -d .runtime/pdm-fixtures.XXXXXX)"
uv run --project components/pdm-algorithm --frozen \
  valeo-pdm prepare-isolated-fixtures \
  --manifest components/pdm-algorithm/configs/isolated_fixture_manifest.yaml \
  --output "$fixture_staging_dir"
find "$fixture_staging_dir" -type f -exec chmod 0444 {} +
find "$fixture_staging_dir" -type d -exec chmod 0555 {} +
mv -- "$fixture_staging_dir" .runtime/pdm-fixtures
```

Tests verify the finished manifest and all objects remain readable by a process with UID `10001` and are not writable there. The container still mounts the directory `:ro`.

The isolated environment example fixes:

```text
INTEGRATION_POSTGRES_DB=ifactory_integration
INTEGRATION_POSTGRES_USER=ifactory_integration
INTEGRATION_POSTGRES_PASSWORD=
PLATFORM_INTEGRATION_TENANT_ALIAS=ifactory-pilot
PLATFORM_INTEGRATION_TENANT_ID=00000000-0000-4000-8000-000000000001
PLATFORM_INTEGRATION_ISOLATED_PILOT_MODE=1
PLATFORM_INTEGRATION_PDM_BASE_URL=http://pdm:10021
PLATFORM_INTEGRATION_TB_BASE_URL=http://host.docker.internal:8080
PLATFORM_INTEGRATION_CMMS_BASE_URL=http://host.docker.internal:3000
# After read-only discovery, append PLATFORM_INTEGRATION_TB_TENANT_ID.
# After read-only discovery, append PLATFORM_INTEGRATION_CMMS_COMPANY_ID.
PLATFORM_INTEGRATION_PDM_CREDENTIAL_REF=PILOT_PDM_CREDENTIAL
PLATFORM_INTEGRATION_TB_CREDENTIAL_REF=PILOT_TB_CREDENTIAL
PLATFORM_INTEGRATION_CMMS_CREDENTIAL_REF=PILOT_CMMS_CREDENTIAL
PLATFORM_INTEGRATION_CMMS_WEBHOOK_SECRET_REF=PILOT_CMMS_WEBHOOK_SECRET_FILE
VALEO_PDM_ALLOWED_TENANT_IDS=00000000-0000-4000-8000-000000000001
```

Use a separate `predictive-maintenance-discovery.yml` containing only the
buildable `identity-discovery` service. It uses the integration image and exact
TB/CMMS/PDM base URLs and credential references, but its environment omits both
discovered identity keys entirely:

```yaml
environment:
  PLATFORM_INTEGRATION_DATABASE_URL: postgresql+psycopg://${INTEGRATION_POSTGRES_USER:?required}:${INTEGRATION_POSTGRES_PASSWORD:?required}@integration-db:5432/${INTEGRATION_POSTGRES_DB:?required}
```

The discovery command constructs Settings but never connects to that hostname.
Before discovery, neither identity name appears in the untracked env file or
discovery service environment, so both keys are absent rather than empty. The
main shadow Compose file does not use bare pass-through or empty defaults; every
runtime/migration role receives:

```yaml
environment:
  PLATFORM_INTEGRATION_TB_TENANT_ID: ${PLATFORM_INTEGRATION_TB_TENANT_ID:?run identity discovery first}
  PLATFORM_INTEGRATION_CMMS_COMPANY_ID: ${PLATFORM_INTEGRATION_CMMS_COMPANY_ID:?run identity discovery first}
```

After the operator appends the exact two discovery assignments to the
untracked env, the main stack can resolve. Compose contract tests prove the
discovery service environment lacks both keys, the main file fails
configuration before they are appended, and a temporary non-secret valid pair
appears exactly after assignment.

The untracked runtime environment supplies a URL-safe random-hex
`INTEGRATION_POSTGRES_PASSWORD`,
`PILOT_PDM_CREDENTIAL`, `PILOT_TB_CREDENTIAL`, and
`PILOT_CMMS_CREDENTIAL` using the strict envelopes above, plus
`VALEO_PDM_PREDICTION_V2_BEARER_TOKEN`; the PDM envelope value must equal the
PDM process token. The tracked example contains only reference names and empty
secret assignments.

Compose must explicitly inject each referenced credential; `--env-file` is
only Compose interpolation input and does not put arbitrary names in a
container. Use guarded, role-minimal mappings:

```yaml
# predictive-maintenance-discovery.yml: identity-discovery
environment:
  PILOT_TB_CREDENTIAL: ${PILOT_TB_CREDENTIAL:?required}
  PILOT_CMMS_CREDENTIAL: ${PILOT_CMMS_CREDENTIAL:?required}

# predictive-maintenance-shadow.yml: integration-api
environment:
  PILOT_TB_CREDENTIAL: ${PILOT_TB_CREDENTIAL:?required}
  PILOT_CMMS_CREDENTIAL: ${PILOT_CMMS_CREDENTIAL:?required}

# predictive-maintenance-shadow.yml: prediction-worker
environment:
  PILOT_PDM_CREDENTIAL: ${PILOT_PDM_CREDENTIAL:?required}
  PILOT_TB_CREDENTIAL: ${PILOT_TB_CREDENTIAL:?required}
```

Each role also receives only the corresponding
`PLATFORM_INTEGRATION_*_CREDENTIAL_REF` names. `scheduler` and
`integration-migrate` receive none of these credential values. Compose
contract tests prove every required value is non-empty, the ref resolves
inside the intended role, and no unused credential appears in another role.

Compose supplies the same guarded PostgreSQL values to the database service and
constructs this service-internal value for every integration role, including
the no-dependency discovery command:

```text
PLATFORM_INTEGRATION_DATABASE_URL=postgresql+psycopg://${INTEGRATION_POSTGRES_USER:?required}:${INTEGRATION_POSTGRES_PASSWORD:?required}@integration-db:5432/${INTEGRATION_POSTGRES_DB:?required}
```

The PDM service explicitly receives both:

```yaml
environment:
  VALEO_PDM_PREDICTION_V2_BEARER_TOKEN: ${VALEO_PDM_PREDICTION_V2_BEARER_TOKEN:?required}
  VALEO_PDM_ALLOWED_TENANT_IDS: ${VALEO_PDM_ALLOWED_TENANT_IDS:?required}
```

Compose contract tests prove any missing/empty database name, user, password,
PDM token, or canonical non-empty tenant allowlist fails
configuration/startup without echoing a value. Discovery constructs Settings
but does not connect to PostgreSQL.

The PDM container receives the generated directory read-only from the Compose-file-relative source `../../.runtime/pdm-fixtures:/fixtures:ro`, `VALEO_PDM_ISOLATED_FIXTURE_MODE=1`, and the fixture manifest/root settings. Configure its container health check against `/readyz`. The integration API listens on container port `8080` and maps only `127.0.0.1:18080:8080`, avoiding the local ThingsBoard port `8080`. The integration database uses its own PostgreSQL volume and credentials injected from a local untracked environment file.

For the documented local topology, add `host.docker.internal:host-gateway` to integration roles. Use exactly `http://host.docker.internal:8080` for local ThingsBoard, `http://host.docker.internal:3000` for the CMMS Nginx API origin, and the PDM image's actual internal port `http://pdm:10021` for PDM service DNS and health checks. Runtime control commands execute inside `integration-api` through Compose so they use the same env file, network, secret references, and database URL; do not run host `uv` commands against container-only DNS names.

Add a one-shot `integration-migrate` service that runs `platform-integration migrate`. Its dependency chain is exact: PostgreSQL healthy → migration completes successfully → integration API may start. Scheduler and prediction-worker service definitions additionally depend on migration success and PDM `/readyz` healthy, but are placed behind the `continuous` Compose profile and are not started during deterministic acceptance. A migration failure prevents every integration runtime role from starting.

Use these exact PDM mount settings:

```text
VALEO_PDM_PREDICTION_V2_MANIFEST=/fixtures/manifest.runtime.yaml
VALEO_PDM_PREDICTION_V2_OBJECT_ROOT=/fixtures/objects
```

For a repeatable reset, `reset-pdm-fixtures.sh --confirm-path .runtime/pdm-fixtures` first proves the canonical target is exactly the repository's `.runtime/pdm-fixtures`, is not a symlink, and stays under `.runtime`. It also verifies the isolated Compose project is down and no active mount references that exact canonical source; otherwise it refuses because moving a live bind source would leave the container reading the old inode. It reports manifest/hash status and prints the exact source/destination before moving the directory into `.runtime/recycle/` with a UTC timestamp suffix. A corrupt or unreadable fixture additionally requires `--confirm-corrupt`; this permits recovery from readiness failures without weakening target checks. It never recursively deletes the path.

- [ ] **Step 3: Run explicit provisioning and shadow acceptance**

First satisfy the master's operator-owned service prerequisites. Read-only
preflight must prove that port `8080` is the explicitly approved isolated
automotive-factory tenant and that port `3000` is the isolated CMMS built from
the current Phase 1 CMMS commit with effective `API_ACCESS`, a company-scoped
runtime API key, and zero existing work orders. If either process is absent,
stale, unlicensed, belongs to an unapproved checkout/tenant, or cannot prove
the new Phase 1 asset contract, stop and request the separate deployment
authority; do not start a prebuilt image, stop another process, or seed a
database from this gate.

Create the untracked env from the tracked example with credential values and
reachable base URLs, leaving the two discovered identity fields unset. Build
and run only the separate read-only discovery service before resolving or
starting the write-capable main file:

```bash
set -Eeuo pipefail
docker compose \
  --env-file .runtime/predictive-maintenance-shadow.env \
  -f deploy/compose/predictive-maintenance-discovery.yml \
  build identity-discovery
docker compose \
  --env-file .runtime/predictive-maintenance-shadow.env \
  -f deploy/compose/predictive-maintenance-discovery.yml \
  run --rm --no-deps identity-discovery \
  platform-integration discover-identities --format env
```

Expected: discovery calls ThingsBoard `GET /api/auth/user` and reads `tenantId.id` as a UUID, then calls CMMS `GET /api/auth/me` and reads `companyId` as a positive integer. Output contains only canonical `PLATFORM_INTEGRATION_TB_TENANT_ID=...` and `PLATFORM_INTEGRATION_CMMS_COMPANY_ID=...` lines. Copy those exact non-secret values into the untracked env; do not guess or commit them.
Also copy the exact discovered ThingsBoard tenant UUID into
`components/thingsboard/dev/automotive-factory/.env` as
`TB_PDM_EXPECTED_TENANT_ID` and explicitly set
`TB_PDM_DASHBOARD_MANAGED_PUBLICATION=true`. Strict dotenv read-back must
confirm both values because an earlier `dev.sh prepare` preserves an existing
untracked `.env` rather than recopying the later `.env.example`; the managed
Dashboard publisher refuses any different tenant or missing managed flag.

Prepare fixtures, start the isolated stack without the `continuous` profile, and verify service health:

```bash
set -Eeuo pipefail
./deploy/compose/scripts/prepare-pdm-fixtures.sh
docker compose \
  --env-file .runtime/predictive-maintenance-shadow.env \
  -f deploy/compose/predictive-maintenance-shadow.yml \
  up -d --build --wait --wait-timeout 180
docker compose \
  --env-file .runtime/predictive-maintenance-shadow.env \
  -f deploy/compose/predictive-maintenance-shadow.yml \
  ps
migrator_id="$(docker compose \
  --env-file .runtime/predictive-maintenance-shadow.env \
  -f deploy/compose/predictive-maintenance-shadow.yml \
  ps -a -q integration-migrate)"
pdm_id="$(docker compose \
  --env-file .runtime/predictive-maintenance-shadow.env \
  -f deploy/compose/predictive-maintenance-shadow.yml \
  ps -q pdm)"
integration_id="$(docker compose \
  --env-file .runtime/predictive-maintenance-shadow.env \
  -f deploy/compose/predictive-maintenance-shadow.yml \
  ps -q integration-api)"
test -n "$migrator_id"
test -n "$pdm_id"
test -n "$integration_id"
test "$(docker inspect -f '{{.State.ExitCode}}' "$migrator_id")" = 0
test "$(docker inspect -f '{{.State.Health.Status}}' "$pdm_id")" = healthy
test "$(docker inspect -f '{{.State.Health.Status}}' "$integration_id")" = healthy
curl --fail --silent http://127.0.0.1:18080/healthz
```

Expected: fixture preparation succeeds once, `integration-migrate` exits zero, and PostgreSQL/PDM/integration API are healthy. Scheduler/prediction worker are intentionally absent so provisioning and telemetry seeding cannot race a real-time slot. If startup fails, inspect only redacted service logs. Recover with:

```bash
set -Eeuo pipefail
docker compose \
  --env-file .runtime/predictive-maintenance-shadow.env \
  -f deploy/compose/predictive-maintenance-shadow.yml \
  down
./deploy/compose/scripts/reset-pdm-fixtures.sh \
  --confirm-path .runtime/pdm-fixtures
```

`down` deliberately omits `-v`; neither database volumes nor fixtures are deleted implicitly.

Then produce and display the provisioning plan:

```bash
set -Eeuo pipefail
docker compose \
  --env-file .runtime/predictive-maintenance-shadow.env \
  -f deploy/compose/predictive-maintenance-shadow.yml \
  exec -T integration-api \
  platform-integration provision-plan \
  --tenant-alias ifactory-pilot \
  --actor codex-isolated-pilot-operator
```

Stop the turn after printing the canonical plan and SHA-256. In a later turn, after the user explicitly supplies that exact hash, first assign `USER_CONFIRMED_PROVISION_PLAN_SHA256` to the literal 64-character lowercase hash copied verbatim from that current user message; do not source it from the plan-producing turn. Then run:

```bash
set -Eeuo pipefail
test -n "${USER_CONFIRMED_PROVISION_PLAN_SHA256:-}"
printf '%s' "$USER_CONFIRMED_PROVISION_PLAN_SHA256" |
  rg -q '^[0-9a-f]{64}$'
docker compose \
  --env-file .runtime/predictive-maintenance-shadow.env \
  -f deploy/compose/predictive-maintenance-shadow.yml \
  exec -T integration-api \
  platform-integration provision-apply \
  --tenant-alias ifactory-pilot \
  --plan-hash "$USER_CONFIRMED_PROVISION_PLAN_SHA256" \
  --confirmed-hash "$USER_CONFIRMED_PROVISION_PLAN_SHA256" \
  --actor codex-isolated-pilot-operator
docker compose \
  --env-file .runtime/predictive-maintenance-shadow.env \
  -f deploy/compose/predictive-maintenance-shadow.yml \
  exec -T integration-api \
  platform-integration provision-verify \
  --tenant-alias ifactory-pilot \
  --plan-hash "$USER_CONFIRMED_PROVISION_PLAN_SHA256"
```

`USER_CONFIRMED_PROVISION_PLAN_SHA256` must be copied from the later user
message, not inferred, prompted, or exported during the plan-producing turn.
The final command is read-only and must return one consumed durable receipt
whose 20 target results and actor match the confirmed plan.

Provisioning confirmation does not authorize Dashboard publication. Render the
exact isolated Dashboard plan next:

```bash
set -Eeuo pipefail
install -d -m 0700 .runtime/plans .runtime/receipts
pilot_root="$(pwd -P)"
test "$pilot_root" = "$(git rev-parse --show-toplevel)"
components/thingsboard/dev/automotive-factory/dev.sh dashboard-plan \
  --actor codex-isolated-pilot-operator \
  --output "$pilot_root/.runtime/plans/phase2-dashboard.json"
```

Stop again after displaying the tenant, current/desired Dashboard hashes, and
canonical plan SHA-256. In a later turn, copy the exact hash from the user's
current message into `USER_CONFIRMED_PHASE2_DASHBOARD_PLAN_SHA256`, validate
it, and apply once:

```bash
set -Eeuo pipefail
test -n "${USER_CONFIRMED_PHASE2_DASHBOARD_PLAN_SHA256:-}"
printf '%s' "$USER_CONFIRMED_PHASE2_DASHBOARD_PLAN_SHA256" |
  rg -q '^[0-9a-f]{64}$'
pilot_root="$(pwd -P)"
test "$pilot_root" = "$(git rev-parse --show-toplevel)"
components/thingsboard/dev/automotive-factory/dev.sh dashboard-apply \
  --plan "$pilot_root/.runtime/plans/phase2-dashboard.json" \
  --plan-hash "$USER_CONFIRMED_PHASE2_DASHBOARD_PLAN_SHA256" \
  --confirmed-hash "$USER_CONFIRMED_PHASE2_DASHBOARD_PLAN_SHA256" \
  --actor codex-isolated-pilot-operator \
  --receipt "$pilot_root/.runtime/receipts/phase2-dashboard.json"
```

The apply command rechecks `TB_PDM_EXPECTED_TENANT_ID`, the authenticated
tenant, current Dashboard ID/version/body hash, and the desired payload hash
before its only possible POST. The receipt is mode `0600`. The confirmed hash
must come from that later user message, not from the plan-producing turn.

Dashboard confirmation does not authorize telemetry mutation. The host-side
helper uses `http://127.0.0.1:8080` for ThingsBoard and never the
container-only `host.docker.internal` name. Render a third exact plan that
sends one 66-element array per device to:

```text
POST /api/plugins/telemetry/DEVICE/{tb_device_id}/timeseries/ANY
```

Each element is `{"ts": <epoch-ms>, "values": {<telemetry_key>: <number>}}` for `1785283740000` through `1785287640000`. Use these healthy values with the profile's declared scale: CNC `4.00`, injection molding `170.0`, assembly robot `0.500`, tightening `16.00`, air compressor `0.600`, and EOL tester `95.00`. Read the same 66-point half-open window back through the historical query and require 66 exact timestamps/values for every device.

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest e2e/test_shadow_seed.py -v
uv run --directory tests --frozen python -m e2e.support.shadow_seed plan \
  --env-file ../.runtime/predictive-maintenance-shadow.env \
  --actor codex-isolated-pilot-operator \
  --output ../.runtime/plans/phase2-shadow-seed.json
```

Stop after displaying all target/body hashes and the canonical plan SHA-256.
In a later turn, copy the exact user-returned hash into
`USER_CONFIRMED_PHASE2_SHADOW_SEED_SHA256`, validate it, and apply once:

```bash
set -Eeuo pipefail
test -n "${USER_CONFIRMED_PHASE2_SHADOW_SEED_SHA256:-}"
printf '%s' "$USER_CONFIRMED_PHASE2_SHADOW_SEED_SHA256" |
  rg -q '^[0-9a-f]{64}$'
uv run --directory tests --frozen python -m e2e.support.shadow_seed apply \
  --env-file ../.runtime/predictive-maintenance-shadow.env \
  --plan ../.runtime/plans/phase2-shadow-seed.json \
  --plan-hash "$USER_CONFIRMED_PHASE2_SHADOW_SEED_SHA256" \
  --confirmed-hash "$USER_CONFIRMED_PHASE2_SHADOW_SEED_SHA256" \
  --actor codex-isolated-pilot-operator \
  --receipt ../.runtime/receipts/phase2-shadow-seed.json
```

Only after exact readback and a mode-`0600` receipt,
`test_shadow_prediction.py` invokes Compose to run ephemeral `scheduler
--once --now 2026-07-29T01:15:00Z` and `prediction-worker --once`, waits for
both zero exits, then runs:

```text
platform-integration shadow-summary --tenant-alias ifactory-pilot
  --scheduled-at 2026-07-29T01:15:00Z --format json
```

inside `integration-api`. It parses that bounded output to assert all 20
successful runs, all 20 identity mappings, and all six exact artifact hashes;
it does not connect pytest directly to the integration database.

After parsing the summary, host-side `pilot_api.py` repeats the exact read-only
isolation gate: 20 ThingsBoard
`GET /api/v2/alarm/DEVICE/{id}?pageSize=1&page=0&statusList=ACTIVE&typeList=PDM_FORECAST_RISK`
calls plus one company-scoped CMMS `POST /api/work-orders/search` with the
one-row empty criteria body. It requires every `totalElements` to remain zero
and supplies those two zero counts to the final E2E assertions. Failure tests
inject a non-zero response on each side and prove acceptance fails without
issuing any mutation.

Because `uv --directory tests` makes the test process start under `tests/`, the
helper at `tests/e2e/support/pilot_api.py` resolves the superproject as
`Path(__file__).resolve().parents[3]` and
passes that absolute path as `cwd` to every Compose subprocess. Every Compose
subprocess supplies that root's exact
`--env-file .runtime/predictive-maintenance-shadow.env` and
`-f deploy/compose/predictive-maintenance-shadow.yml`.
`conftest.py` uses the locked `python-dotenv` package to read that same
untracked file without interpolation, extracts only the exact identity/base-URL
and credential-envelope keys needed by the host clients, strictly validates the
envelopes, and never prints values. Missing, duplicate, malformed, or mismatched
identity/credential values fail before the seed apply's telemetry writes. The
receipt-bound prediction E2E issues no telemetry POST and must not
enable the `continuous` profile, wait 66 real minutes, or merely advance an
in-process pytest clock.

Then run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  -m pilot_e2e e2e/test_shadow_prediction.py \
  --confirmed-seed-receipt ../.runtime/receipts/phase2-shadow-seed.json -v
./scripts/doctor.sh
```

Expected: test passes; doctor has zero failures; no PDM Alarm or work order exists.

- [ ] **Step 4: Commit the coordinated Phase 2 slice**

After all three component branches are pushed:

```bash
set -Eeuo pipefail
git add \
  components/pdm-algorithm \
  components/thingsboard \
  components/platform-integration \
  deploy/compose \
  deploy/README.md \
  tests/e2e \
  tests/pyproject.toml \
  tests/uv.lock \
  tests/README.md \
  .github/workflows/workspace-check.yml
git diff --cached --check
git diff --cached --submodule=log
./scripts/doctor.sh
git commit -m "feat: add predictive maintenance shadow pipeline"
```

Expected: the superproject records clean, pushed component SHAs and an opt-in isolated shadow environment.
