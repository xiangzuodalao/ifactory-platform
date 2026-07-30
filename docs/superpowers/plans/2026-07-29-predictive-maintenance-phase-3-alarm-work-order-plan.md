# Predictive Maintenance Phase 3 Alarm and Work Order Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn two consecutive risky prediction rounds into one stable ThingsBoard Alarm and allow an authorized maintainer to create exactly one CMMS work order through an explicit, idempotent Dashboard Action.

**Architecture:** `platform-integration` owns the risk episode, Alarm projection, delegated ThingsBoard identity check, approval state machine, and two kinds of transactional outbox message. ThingsBoard renders the current projection and forwards same-origin Actions; CMMS owns the resulting work order and enforces external-reference and request idempotency.

**Tech Stack:** Python 3.12/uv/FastAPI/Pydantic/httpx/SQLAlchemy/Alembic/PostgreSQL/pytest, Java 17/Spring Boot/JPA/Liquibase/JUnit, the existing ThingsBoard automotive-factory Python dashboard generator, Nginx, OpenAPI 3.1, JSON Schema 2020-12.

## Global Constraints

- Follow every master-plan constraint and complete the Phase 2 shadow gate first.
- This phase does not train a model, publish a model, create a Rule Chain, or modify ThingsBoard Java/Angular sources.
- Alarm type is exactly `PDM_FORECAST_RISK`; severity is exactly `WARNING`.
- A normal ThingsBoard ACK remains independent from the integration Action API and never creates a CMMS work order.
- All browser Actions use a same-origin relative path beginning `/api/v1/maintenance-alerts/`; absolute URLs, scheme-relative URLs, and other `/api` paths are rejected by dashboard generation.
- The browser sends `X-Authorization`. Only the exact maintenance-Alert prefix is rerouted to integration; all other `/api` traffic, including ThingsBoard requests that also need this header, continues to ThingsBoard. The integration service validates the token through ThingsBoard `GET /api/auth/user`, never persists it, and never logs it.
- `maintenance_alert_version` is the exact public optimistic-lock field. The request's `expected_version` must equal it for a new action.
- Alarm state messages use `SUPERSEDABLE_STATE` semantics and a stable risk-key aggregate. Approved CMMS creation uses `IMMUTABLE_COMMAND` semantics and can never be superseded.
- Before every CMMS POST retry, query by `external_source=PDM_FORECAST` and `external_ref=alert_id`.
- The approver allowlist is a write-controlled tenant resource. Adding an approver requires a displayed plan and explicit confirmation of its hash.
- Design approval, Dashboard publication, and approver grant do not authorize a
  live CMMS work order. The isolated live Action scenario requires its own
  30-minute exact plan, a hash returned in a later user turn, a single bounded
  apply helper, and a GET/HEAD-only verification receipt. Pytest has no
  allow-write flag.
- Phase tests use an isolated tenant and a fake or isolated CMMS. They do not approve or create a production work order.
- Every command block starts at the current implementation worktree's superproject root unless that block contains its own `cd`; no block relies on a previous block's working directory.
- Every transaction that writes an Alarm projection uses one lock order: action idempotency advisory lock when applicable → tenant-scoped stable `risk_evaluation_state` → `maintenance_alert` → `alarm_projection`/outbox. Risk evaluation, Actions, work-order state, and later feedback may not reverse this order.

---

### Task 0: Resume the Exact Phase 2 Component Branches

**Files:** None.

**Interfaces:**

- Consumes: Phase 2 superproject gitlinks and pushed component feature branches
- Produces: clean attached PDM, CMMS, ThingsBoard, and integration worktrees

- [ ] **Step 1: Verify every recorded tip before editing**

Run from the superproject root:

```bash
set -Eeuo pipefail
for component in pdm-algorithm cmms thingsboard platform-integration; do
  test -z "$(git -C "components/$component" status --short)"
done
pdm_tip="$(git rev-parse HEAD:components/pdm-algorithm)"
cmms_tip="$(git rev-parse HEAD:components/cmms)"
thingsboard_tip="$(git rev-parse HEAD:components/thingsboard)"
integration_tip="$(git rev-parse HEAD:components/platform-integration)"
git -C components/pdm-algorithm fetch origin feat/pdm-prediction-v2
git -C components/cmms fetch origin feat/predictive-maintenance-integration
git -C components/thingsboard fetch origin feat/predictive-maintenance-dashboard
git -C components/platform-integration fetch origin feat/predictive-maintenance-pilot
git -C components/pdm-algorithm switch feat/pdm-prediction-v2
git -C components/cmms switch feat/predictive-maintenance-integration
git -C components/thingsboard switch feat/predictive-maintenance-dashboard
git -C components/platform-integration switch feat/predictive-maintenance-pilot
test "$(git -C components/pdm-algorithm rev-parse HEAD)" = "$pdm_tip"
test "$(git -C components/cmms rev-parse HEAD)" = "$cmms_tip"
test "$(git -C components/thingsboard rev-parse HEAD)" = "$thingsboard_tip"
test "$(git -C components/platform-integration rev-parse HEAD)" = \
  "$integration_tip"
```

Expected: each component is attached to the named pushed branch at the exact gitlink recorded by Phase 2. Stop rather than merge, reset, or work detached if any check fails.

### Task 1: Freeze the Phase 3 Contracts and Executable Examples

**Files:**

- Create: `contracts/openapi/platform-integration-v1.yaml`
- Create: `contracts/json-schema/maintenance-alert-v1.json`
- Create: `tests/contract/phase3/test_contract_documents.py`
- Create: `tests/contract/phase3/test_alarm_examples.py`
- Create: `tests/contract/phase3/test_action_examples.py`
- Create: `tests/contract/phase3/test_cmms_work_order_examples.py`
- Create: `tests/contract/phase3/fixtures/maintenance-alert.json`
- Create: `tests/contract/phase3/fixtures/create-work-order-action.json`
- Create: `tests/contract/phase3/fixtures/create-work-order-response.json`
- Create: `tests/contract/phase3/fixtures/cmms-work-order-request.json`
- Create: `tests/contract/phase3/fixtures/cmms-work-order-response.json`
- Modify: `contracts/openapi/cmms-integration-v1.yaml`

**Interfaces:**

- Produces: `POST /api/v1/maintenance-alerts/{alert_id}/actions`
- Produces: CMMS `POST /api/work-orders`
- Produces: CMMS `GET /api/work-orders/by-external-ref`
- Produces: exact Alarm details consumed by ThingsBoard dashboard code
- Consumes: Phase 1 CMMS and Phase 2 mapping contracts

- [ ] **Step 1: Write the failing contract-document tests**

Validate both OpenAPI documents and the JSON Schema. Also assert that the Action operation requires `X-Authorization`, `Idempotency-Key`, `action`, and `expected_version`.

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  contract/phase3/test_contract_documents.py -v
```

Expected: FAIL because the Phase 3 document and schema do not exist.

- [ ] **Step 2: Define the exact Alarm projection**

The contract requires these Alarm details:

```json
{
  "alert_id": "018f7fd0-fb58-7a44-8f98-d74477403fd1",
  "risk_key": "03bbf5564917ba9258169a888137ab14caaca8d9bf7ea38cd804ce76fcaa4d6f",
  "equipment_id": "4aa338a6-c854-4f97-ab6f-1df3633d81fe",
  "meas_code": "vibration_rms",
  "prediction_run_id": "018f7fd0-fb58-7a44-8f98-d74477403fd2",
  "model_profile_id": "pilot-cnc-vibration",
  "model_info_id": "pilot-fixture-v1-cnc-vibration",
  "policy_version": "pilot-threshold-v1",
  "forecast_summary": {
    "min": "5.01",
    "max": "5.20",
    "mean": "5.10",
    "threshold_crossing_count": 15
  },
  "threshold": {
    "direction": "ABOVE",
    "value": "5.00",
    "unit": "mm/s"
  },
  "risk_state": "ACTIVE",
  "maintenance_state": "PENDING_APPROVAL",
  "maintenance_alert_version": 1,
  "cmms_work_order_id": null,
  "cmms_work_order_url": null,
  "correlation_id": "018f7fd0-fb58-7a44-8f98-d74477403fd3",
  "policy_evaluation_pending": false
}
```

This example's `risk_key` is the stable SHA-256 calculated from tenant `00000000-0000-4000-8000-000000000001`, the displayed equipment ID, and `vibration_rms`; its outbox aggregate ID is the literal prefix `risk-alarm:` concatenated with that digest. All decimal fields remain strings. `maintenance_alert_version` is an integer greater than or equal to one and refers only to `maintenance_alert.version`, not the Alarm aggregate or outbox version. `cmms_work_order_url` must be either `null` or a configured application link; the service must never derive it from an untrusted upstream `Host` header.

- [ ] **Step 3: Define Action and CMMS work-order behavior**

Action request:

```json
{"action":"CREATE_WORK_ORDER","reason":null,"expected_version":1}
```

Action response requires `action_id`, `alert_id`, `maintenance_state`, `maintenance_alert_version`, `correlation_id`, and nullable `cmms_work_order_id`/`cmms_work_order_url`. All three newly accepted actions return `202`; an idempotent replay returns the stored first status and body.

The CMMS request requires:

```text
external_source = PDM_FORECAST
external_ref    = alert_id
correlation_id
equipment_id
tb_alarm_id
model_profile_id
model_info_id
policy_version
```

Contract rules:

- same authenticated tenant, idempotency key, and canonical request digest return the first response;
- the canonical Action digest covers exactly normalized `alert_id`, `action`, trimmed-or-null `reason`, and `expected_version`;
- the same key with a different request returns `409 IDEMPOTENCY_CONFLICT`;
- stale `expected_version` returns `409 ALERT_VERSION_CONFLICT`;
- `REJECT` and `CLOSE_RISK` without a non-blank reason return `422 REASON_REQUIRED`;
- unauthorized user returns `403 MAINTENANCE_APPROVER_REQUIRED`;
- unresolved external-ref query returns `404 WORK_ORDER_NOT_FOUND`;
- errors contain stable codes and no token, stack trace, or CMMS body.

Declare `X-Authorization` as an OpenAPI `apiKey` security scheme in header location; it is deliberately not the standard `Authorization` bearer header. Include nullable `cmms_status`, `cmms_event_version`, and `cmms_status_updated_at` in the v1 Alarm schema now so Phase 4 adds values without reshaping the contract.

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest contract/phase3 -v
```

Expected: contract documents and examples pass. Keep the root contract changes uncommitted until Tasks 2–7 pass on both provider and consumer sides.

### Task 2: Add Alert, Approval, Projection, and Outbox Persistence

**Files:**

- Create: `components/platform-integration/migrations/versions/0002_alarm_approval_outbox.py`
- Create: `components/platform-integration/src/platform_integration/models/maintenance.py`
- Create: `components/platform-integration/src/platform_integration/models/outbox.py`
- Create: `components/platform-integration/src/platform_integration/repositories/maintenance_alerts.py`
- Create: `components/platform-integration/src/platform_integration/repositories/alert_actions.py`
- Create: `components/platform-integration/src/platform_integration/repositories/approvers.py`
- Create: `components/platform-integration/src/platform_integration/repositories/outbox.py`
- Create: `components/platform-integration/src/platform_integration/services/alarm_projection.py`
- Create: `components/platform-integration/tests/integration/test_alarm_migrations.py`
- Create: `components/platform-integration/tests/repositories/test_maintenance_alert_constraints.py`
- Create: `components/platform-integration/tests/repositories/test_outbox_ordering.py`
- Create: `components/platform-integration/tests/services/test_alarm_projection.py`
- Modify: `components/platform-integration/src/platform_integration/models/__init__.py`
- Modify: `components/platform-integration/src/platform_integration/models/prediction.py`
- Modify: `components/platform-integration/src/platform_integration/services/risk.py`

**Interfaces:**

- Produces: `AlarmProjectionService.apply_round(result: RoundRisk) -> ProjectionTransition`
- Produces: `MaintenanceAlertRepository.lock_active(tenant_id, risk_key) -> MaintenanceAlert | None`
- Produces: `OutboxRepository.claim_due(tenant_id, delivery_semantics, event_type, owner, now, limit) -> Sequence[OutboxEvent]`
- Produces: `OutboxRepository.supersede_obsolete_state(tenant_id, aggregate_type, aggregate_id, current_version, delivery_semantics) -> int`
- Consumes: Phase 2 `risk_evaluation_state`

- [ ] **Step 1: Write schema, uniqueness, and ordering tests**

Tests must assert:

- at most one non-terminal `maintenance_alert` per `(tenant_id,equipment_id,meas_code)`;
- at most one active-work-order state across `APPROVED`, `WORK_ORDER_CREATING`, `WORK_ORDER_CREATE_FAILED`, and `WORK_ORDER_OPEN` for the same risk key;
- unique `(tenant_id,idempotency_key)` for `alert_action`;
- unique `(tenant_id,tb_user_id)` for `maintenance_approver`;
- exactly one current `alarm_projection` per `(tenant_id,risk_key)`;
- `alert_action` persists the first response status and response body;
- `alarm_projection` and `outbox_event` both persist explicit `tenant_id`;
- unique `(tenant_id,aggregate_type,aggregate_id,aggregate_version)` for outbox events;
- one delivery lease per aggregate;
- the database rejects `IMMUTABLE_COMMAND` with status `SUPERSEDED`;
- supersede operations require matching aggregate type, aggregate ID, and `SUPERSEDABLE_STATE`;
- `alarm_aggregate_version` increases across clear and later new episodes;
- an older pending Alarm state becomes `SUPERSEDED`;
- an `IMMUTABLE_COMMAND` never becomes `SUPERSEDED`.

Run:

```bash
set -Eeuo pipefail
cd components/platform-integration
uv run pytest \
  tests/integration/test_alarm_migrations.py \
  tests/repositories/test_maintenance_alert_constraints.py \
  tests/repositories/test_outbox_ordering.py -v
```

Expected: FAIL because migration `0002` and repositories are absent.

- [ ] **Step 2: Implement exact database invariants**

Migration `0002_alarm_approval_outbox` creates:

```text
maintenance_alert
maintenance_approver
maintenance_approver_plan
alert_action
alarm_projection
outbox_event
```

Add `alarm_aggregate_version` and the current `alert_id` relationship to the existing `risk_evaluation_state` model in `models/prediction.py`; do not redeclare that table in `models/maintenance.py`. Use a partial unique index for one active risk episode and another partial unique index for the active-work-order states. Add unique `(tenant_id,tb_user_id)` for approvers and `(tenant_id,risk_key)` for the current projection. Every projection/outbox foreign key, query, claim, supersede operation, and unique key includes `tenant_id`. Use integer `version` on `maintenance_alert` and reject update counts other than one.

`maintenance_approver_plan` stores an immutable canonical plan projection,
plan UUID/hash, tenant/user identity, actor, reason, created/expiry times, and
nullable applied time/result. Apply locks it by `(tenant_id,plan_id)`, requires
the unexpired exact hash and actor, revalidates ThingsBoard identity, and can
consume it once. This durable row survives separate `run --rm` containers and
is the audit/read-back source; it contains no token.

Keep the three versions separate:

```text
maintenance_alert.version     browser optimistic lock; changes only for business-visible Alert state
alarm_aggregate_version       ordered desired-state projection version
outbox aggregate_version      copied from the appropriate aggregate at event creation
```

Lease/attempt/delivery state and the first returned `tb_alarm_id` are transport metadata and change none of these business versions. A per-aggregate delivery lease remains owned across the external HTTP call, with heartbeat/expiry recovery, so two state writes cannot reach ThingsBoard out of order.

Stable Alarm aggregate identity is:

```python
projection = {
    "tenant_id": str(tenant_id),
    "equipment_id": str(equipment_id),
    "meas_code": meas_code,
}
risk_key = sha256(rfc8785.dumps(projection)).hexdigest()
aggregate_id = "risk-alarm:" + risk_key
```

Use `alert-action:{action_id}` for the immutable CMMS aggregate.

- [ ] **Step 3: Implement risk-to-projection transitions**

In the same transaction:

1. lock `risk_evaluation_state`;
2. consume each successful prediction run once;
3. create/update/clear the risk episode;
4. increment `maintenance_alert.version` and `alarm_aggregate_version`;
5. update `alarm_projection`;
6. insert the corresponding Alarm state outbox event.

Policy changes keep the current `alert_id`, reset both consecutive counters, set `policy_evaluation_pending=true`, and supersede older Alarm state versions. A recovered or manually closed episode does not cancel an already committed immutable command.

Run:

```bash
set -Eeuo pipefail
uv run --directory components/platform-integration --frozen pytest \
  tests/integration/test_alarm_migrations.py \
  tests/repositories/test_maintenance_alert_constraints.py \
  tests/repositories/test_outbox_ordering.py \
  tests/services/test_alarm_projection.py -v
```

Expected: all tests pass. The migration test gives Alembic only a Testcontainers PostgreSQL URL and performs `upgrade head → downgrade 0001 → upgrade head`; never invoke Alembic against an ambient database URL.

### Task 3: Project Stable Risk State to ThingsBoard

**Files:**

- Create: `components/platform-integration/src/platform_integration/services/outbox_delivery.py`
- Create: `components/platform-integration/src/platform_integration/workers/outbox.py`
- Create: `components/platform-integration/tests/clients/test_thingsboard_alarm_client.py`
- Create: `components/platform-integration/tests/workers/test_alarm_outbox_worker.py`
- Modify: `components/platform-integration/src/platform_integration/clients/thingsboard.py`
- Modify: `components/platform-integration/src/platform_integration/cli.py`

**Interfaces:**

- Produces: `ThingsBoardClient.find_active_pdm_alarm(device_id, risk_key, alert_id) -> Alarm | None`
- Produces: `ThingsBoardClient.upsert_alarm(projection, existing_alarm_id) -> Alarm`
- Produces: `ThingsBoardClient.clear_alarm(alarm_id) -> None`
- Produces: `AlarmOutboxWorker.run_once(now, owner) -> DeliverySummary`
- Produces: `platform-integration alarm-outbox-worker [--once]`
- Consumes: ThingsBoard Alarm REST and `SUPERSEDABLE_STATE` outbox rows

- [ ] **Step 1: Write client and delivery failure tests**

Use these ThingsBoard operations:

```text
GET  /api/alarm/{tb_alarm_id}
GET  /api/alarm/info/{tb_alarm_id}
GET  /api/alarm/DEVICE/{tb_device_id}
     ?pageSize=100&page=0&searchStatus=ACTIVE
     &sortProperty=startTs&sortOrder=DESC
POST /api/alarm
POST /api/alarm/{tb_alarm_id}/clear
```

Client filtering requires `type=PDM_FORECAST_RISK` and exact detail values for `risk_key`, current-episode `alert_id`, `equipment_id`, and `meas_code`; it must not reuse a simulator fault Alarm.

Tests cover:

- first risky transition creates one `WARNING` Alarm;
- another update edits the same Alarm;
- every upsert carries the exact stable `risk_key` in details;
- updating an ACKed Alarm preserves its ACK state;
- clear uses the recorded Alarm ID;
- a write-after-server-commit timeout queries by `risk_key` plus current `alert_id` before another POST;
- two workers cannot deliver the same aggregate concurrently;
- old upsert after a newer clear is `SUPERSEDED`;
- old clear after a newer episode upsert is `SUPERSEDED`;
- eight failed writes become `DEAD_LETTER` with only a safe error code;
- payload/logs contain no raw history, forecast array, or credential.

Run:

```bash
set -Eeuo pipefail
cd components/platform-integration
uv run pytest \
  tests/clients/test_thingsboard_alarm_client.py \
  tests/workers/test_alarm_outbox_worker.py -v
```

Expected: FAIL before client and worker implementation.

- [ ] **Step 2: Implement desired-state delivery**

Before sending, the worker locks the aggregate, reloads its newest projection/version, and marks any older pending state `SUPERSEDED`. It reconstructs Alarm details from `alarm_projection`; it does not forward an old event payload.

Delivery uses an exponential backoff capped at one hour and a total of eight attempts. On unknown write outcome, reconcile by active `risk_key` plus current-episode `alert_id`. Store the returned `tb_alarm_id` and delivery metadata in one local transaction after the external call. This bookkeeping must not increment `maintenance_alert.version` or change public `maintenance_alert_version`; otherwise a freshly rendered Dashboard would immediately hold a stale action version.

Phase 3 readiness also rejects a configuration with two Alarm-producing measurement bindings enabled for one ThingsBoard device. The fixed type `PDM_FORECAST_RISK` is safe only because the pilot enables one predictive measurement per device.

- [ ] **Step 3: Verify and commit the Alarm slice**

Run:

```bash
set -Eeuo pipefail
uv run --directory components/platform-integration --frozen pytest \
  tests/services/test_alarm_projection.py \
  tests/clients/test_thingsboard_alarm_client.py \
  tests/workers/test_alarm_outbox_worker.py -v
uv run --directory components/platform-integration --frozen ruff check .
uv run --directory components/platform-integration --frozen ruff format --check .
```

Expected: all tests/checks pass.

Commit:

```bash
set -Eeuo pipefail
./scripts/doctor.sh
git -C components/platform-integration add migrations src tests
git -C components/platform-integration commit \
  -m "feat: project predictive risk alarms"
git -C components/platform-integration push
```

### Task 4: Add Idempotent PDM Work Orders to CMMS

**Files:**

- Create: `components/cmms/api/src/main/java/com/grash/model/enums/ExternalSource.java`
- Create: `components/cmms/api/src/main/java/com/grash/service/WorkOrderIntegrationService.java`
- Create: `components/cmms/api/src/main/resources/db/changelog/2026_07_29_00000000002_add_work_order_integration_identity.xml`
- Create: `components/cmms/api/src/test/java/com/grash/service/WorkOrderIntegrationServiceTest.java`
- Modify: `components/cmms/api/src/main/java/com/grash/model/WorkOrder.java`
- Modify: `components/cmms/api/src/main/java/com/grash/dto/workOrder/WorkOrderPostDTO.java`
- Modify: `components/cmms/api/src/main/java/com/grash/dto/workOrder/WorkOrderShowDTO.java`
- Modify: `components/cmms/api/src/main/java/com/grash/repository/WorkOrderRepository.java`
- Modify: `components/cmms/api/src/main/java/com/grash/service/WorkOrderService.java`
- Modify: `components/cmms/api/src/main/java/com/grash/controller/WorkOrderController.java`
- Modify: `components/cmms/api/src/main/resources/db/master.xml`
- Modify: `components/cmms/api/src/test/java/com/grash/controller/WorkOrderControllerTest.java`
- Modify: `components/cmms/api/src/test/java/com/grash/integration/WorkOrderIntegrationTest.java`

**Interfaces:**

- Produces: `WorkOrderShowDTO WorkOrderIntegrationService.create(WorkOrderPostDTO request, User user, String idempotencyKey)`
- Produces: `Optional<WorkOrder> findByCompany_IdAndExternalSourceAndExternalRef(Long companyId, ExternalSource source, String externalRef)`
- Produces: `GET /api/work-orders/by-external-ref?source=PDM_FORECAST&ref={alert_id}`
- Consumes: `Idempotency-Key: wo:{alert_id}` and Phase 1 `IntegrationIdempotencyService`

- [ ] **Step 1: Write controller, concurrency, and compatibility tests**

Tests must assert:

- ordinary work-order POST remains backward compatible;
- presence of any integration field requires the complete integration field set and idempotency header;
- an integration request without an idempotency key returns `400 IDEMPOTENCY_KEY_REQUIRED`;
- the same company/key/body returns the original status/body and one work order;
- the same key/different body returns `409 IDEMPOTENCY_CONFLICT`;
- `(company_id,external_source,external_ref)` is unique;
- two concurrent identical requests create one row;
- query is company-scoped and cannot reveal another company's work order;
- a `PDM_FORECAST` request references an Asset with the same `equipment_id`;
- all correlation/model/Alarm fields round-trip through `WorkOrderShowDTO`.

Run:

```bash
set -Eeuo pipefail
cd components/cmms/api
mvn -Dtest='WorkOrderControllerTest,WorkOrderIntegrationServiceTest,WorkOrderIntegrationTest' test
```

Expected: FAIL because the integration fields and service do not exist.

- [ ] **Step 2: Add integration identity and audit-safe schema**

Add nullable columns:

```text
external_source
external_ref
correlation_id
equipment_id
tb_alarm_id
model_profile_id
model_info_id
policy_version
```

Add unique `(company_id,external_source,external_ref)`. Because `WorkOrder` is Envers-audited, add matching audit-table columns and modified flags required by the current audit strategy, or explicitly mark each integration-only field `@NotAudited` and test that choice. Include exact rollback and include the changelog once at the end of `db/master.xml`.

- [ ] **Step 3: Reuse the Phase 1 idempotency transaction**

Canonicalize the request with `CanonicalRequestDigestService`. `WorkOrderIntegrationService.create()` calls:

```java
integrationIdempotencyService.execute(
    user.getCompany(),
    IntegrationOperation.CREATE_WORK_ORDER,
    idempotencyKey,
    requestDigest,
    WorkOrderShowDTO.class,
    () -> createAndMap(request, user)
);
```

The idempotency path must serialize concurrent requests with the Phase 1 advisory-lock or row-lock strategy and return the stored first response. Do not add a second idempotency implementation.

Run:

```bash
set -Eeuo pipefail
mvn -f components/cmms/api/pom.xml \
  -Dtest='WorkOrderControllerTest,WorkOrderIntegrationServiceTest,WorkOrderIntegrationTest' test
mvn -f components/cmms/api/pom.xml -DskipTests compile
```

Expected: focused tests and compilation pass.

- [ ] **Step 4: Commit and push CMMS**

Run:

```bash
set -Eeuo pipefail
./scripts/doctor.sh
git -C components/cmms add \
  api/src/main/java/com/grash/model \
  api/src/main/java/com/grash/dto/workOrder \
  api/src/main/java/com/grash/repository/WorkOrderRepository.java \
  api/src/main/java/com/grash/service/WorkOrderIntegrationService.java \
  api/src/main/java/com/grash/service/WorkOrderService.java \
  api/src/main/java/com/grash/controller/WorkOrderController.java \
  api/src/main/resources/db \
  api/src/test/java/com/grash/controller/WorkOrderControllerTest.java \
  api/src/test/java/com/grash/integration/WorkOrderIntegrationTest.java \
  api/src/test/java/com/grash/service/WorkOrderIntegrationServiceTest.java
git -C components/cmms commit \
  -m "feat: add idempotent predictive work orders"
git -C components/cmms push
```

### Task 5: Authenticate and Apply Immutable Maintenance Actions

**Files:**

- Create: `components/platform-integration/src/platform_integration/api/actions.py`
- Create: `components/platform-integration/src/platform_integration/auth/thingsboard.py`
- Create: `components/platform-integration/src/platform_integration/contracts/maintenance.py`
- Create: `components/platform-integration/src/platform_integration/services/actions.py`
- Create: `components/platform-integration/src/platform_integration/services/approvers.py`
- Create: `components/platform-integration/src/platform_integration/commands/approvers.py`
- Create: `components/platform-integration/tests/api/test_actions.py`
- Create: `components/platform-integration/tests/auth/test_thingsboard_identity.py`
- Create: `components/platform-integration/tests/contracts/test_maintenance_contract.py`
- Create: `components/platform-integration/tests/services/test_actions.py`
- Create: `components/platform-integration/tests/services/test_approvers.py`
- Create: `components/platform-integration/tests/integration/test_projection_writer_concurrency.py`
- Modify: `components/platform-integration/src/platform_integration/app.py`
- Modify: `components/platform-integration/src/platform_integration/cli.py`
- Modify: `components/platform-integration/src/platform_integration/clients/thingsboard.py`

**Interfaces:**

- Produces: `POST /api/v1/maintenance-alerts/{alert_id}/actions`
- Produces: `ThingsBoardIdentityVerifier.verify(x_authorization) -> VerifiedIdentity`
- Produces: `ThingsBoardIdentityVerifier.verify_alarm_access(identity, tb_alarm_id, alert_id, risk_key) -> None`
- Produces: `ActionService.apply(command, identity, idempotency_key) -> ActionResponse`
- Produces: `platform-integration approver-plan --tb-user-id-env NAME` and
  `approver-apply`/`approver-verify`
- Consumes: ThingsBoard `GET /api/auth/user`

- [ ] **Step 1: Write authentication-order and action-state tests**

Tests assert:

- missing/malformed token is rejected before any Alert or idempotency lookup;
- a verified ThingsBoard tenant is mapped to platform `tenant_id`; request/body tenant input is never trusted;
- a same-tenant user without ThingsBoard visibility of the target Alarm is rejected before idempotency lookup;
- a disabled/unlisted user using a new idempotency key returns `403`;
- same authorized identity/key/body returns the first response even when its expected version is now old;
- after target visibility succeeds, an existing same-key/same-digest replay returns its stored first status/body before evaluating current approver membership, version, or state;
- same key/different body returns `409`;
- a new stale-version action returns `409`;
- `CREATE_WORK_ORDER` changes `PENDING_APPROVAL` to `APPROVED`, writes one Alarm desired-state outbox and one immutable CMMS command, and never calls CMMS inside the transaction;
- `REJECT` writes one Alarm desired-state outbox and no CMMS command;
- `CLOSE_RISK` clears counters and writes only an Alarm desired-state outbox;
- every accepted action increments `maintenance_alert.version` and `alarm_aggregate_version` exactly once;
- action after `CLEARED` or `MANUALLY_CLOSED` returns `409`;
- an approved command remains pending after later clear/close;
- concurrent same-key/same-digest clicks return identical status/body and produce one `alert_action` and one CMMS command;
- concurrent prediction versus `CREATE_WORK_ORDER` or `CLOSE_RISK` completes without deadlock and preserves monotonic public/aggregate versions.

Run:

```bash
set -Eeuo pipefail
cd components/platform-integration
uv run pytest \
  tests/auth/test_thingsboard_identity.py \
  tests/contracts/test_maintenance_contract.py \
  tests/services/test_approvers.py \
  tests/services/test_actions.py \
  tests/integration/test_projection_writer_concurrency.py \
  tests/api/test_actions.py -v
```

Expected: FAIL before the route and services exist.

- [ ] **Step 2: Implement delegated identity without token persistence**

Forward the exact `X-Authorization` value only to:

```text
GET /api/auth/user
GET /api/alarm/info/{tb_alarm_id}
```

Accept the identity only when its tenant UUID matches an enabled `tenant_binding`. After a tenant-scoped local Alert lookup, use the same token to fetch Alarm info and cross-check `details.alert_id` and `details.risk_key`. This preserves ThingsBoard customer-user visibility rules instead of treating tenant equality as sufficient.

Copy only `user_id`, `tb_tenant_id`, display label, and role summary into the request-scoped identity. Redaction tests must prove the token is absent from logs, exception text, audit JSON, outbox JSON, and database rows.

- [ ] **Step 3: Add explicitly confirmed approver provisioning**

`approver-plan --tb-user-id-env NAME` validates `NAME` against
`^[A-Z][A-Z0-9_]{2,127}$`, directly reads that container environment variable,
and requires a canonical ThingsBoard user UUID. It prints tenant, ThingsBoard
user UUID/label, grant reason, immutable plan UUID, 30-minute expiry, and
canonical SHA-256, and persists that exact secret-free plan in
`maintenance_approver_plan`, with unique `(tenant_id,plan_hash)`.
`approver-apply` requires the tenant alias and exact confirmed hash,
locks/revalidates/consumes that row once, and records grantor,
reason, applied time, and result. `approver-verify` is read-only and returns
only those immutable audit fields for the exact tenant/plan/hash. Apply cannot
be combined with plan generation. Tests cover
missing/blank/malformed variables and prove no credential value is consulted or
logged, a new ephemeral container can apply the prior plan, expiry/drift
performs zero writes, and concurrent apply grants once. Do not add a general
role-management UI.

- [ ] **Step 4: Implement idempotency ordering and state transitions**

Process every request in this order:

```text
verify ThingsBoard identity
derive tenant and tenant-scoped lookup target Alert
verify ThingsBoard Alarm visibility and detail identity
acquire transaction advisory lock for (tenant_id,idempotency_key)
re-read the tenant-scoped idempotency key inside that lock
return/reject an existing replay
for a new key, verify current approver membership
lock stable risk_evaluation_state, then Alert, in the global projection-writer order
check expected_version
validate risk/maintenance state and action
insert immutable action and audit
update Alert version/state
update current Alarm projection and aggregate version
insert required Alarm state and/or immutable command outbox
commit
```

The idempotency reservation/lock is held through commit. A second concurrent request cannot fall through to version checking after the first commits; it re-reads and returns the first response. Existing replay handling still requires valid ThingsBoard authentication and target-Alarm visibility, but deliberately precedes current allowlist/version/state checks.

For `CREATE_WORK_ORDER`, the approval transaction freezes this immutable command snapshot before commit:

```text
target_cmms_company_id
target_cmms_asset_id
equipment_id
tb_alarm_id
alert_id
action_id
approver user ID
approval timestamp
correlation_id
model_profile_id
model_info_id
policy_version
CMMS idempotency key
exact canonical CMMS request body
CMMS request SHA-256
```

Later mapping or policy changes cannot alter the target or body. Return `202` for a newly approved work-order command and the stored first response for replay.

Run:

```bash
set -Eeuo pipefail
uv run --directory components/platform-integration --frozen pytest \
  tests/auth/test_thingsboard_identity.py \
  tests/contracts/test_maintenance_contract.py \
  tests/services/test_approvers.py \
  tests/services/test_actions.py \
  tests/integration/test_projection_writer_concurrency.py \
  tests/api/test_actions.py -v
uv run --directory components/platform-integration --frozen ruff check .
uv run --directory components/platform-integration --frozen ruff format --check .
```

Expected: all tests/checks pass.

### Task 6: Add Safe Same-Origin Dashboard Actions

**Files:**

- Create: `components/thingsboard/dev/automotive-factory/factory_simulator/maintenance_actions.py`
- Create: `components/thingsboard/dev/automotive-factory/tests/test_dashboard_actions.py`
- Modify: `components/thingsboard/dev/automotive-factory/factory_simulator/config.py`
- Modify: `components/thingsboard/dev/automotive-factory/factory_simulator/dashboard.py`
- Modify: `components/thingsboard/dev/automotive-factory/factory_simulator/dashboard_publication.py`
- Modify: `components/thingsboard/dev/automotive-factory/config.yml`
- Modify: `components/thingsboard/dev/automotive-factory/.env.example`
- Modify: `components/thingsboard/dev/automotive-factory/tests/test_dashboard_publication.py`
- Modify: `components/thingsboard/dev/automotive-factory/README.md`

**Interfaces:**

- Produces: Alarm widget buttons for `CREATE_WORK_ORDER`, `REJECT`, and `CLOSE_RISK`
- Produces: `build_maintenance_actions(config: PredictiveMaintenanceConfig) -> dict[str, Any]`
- Consumes: Alarm details and same-origin integration Action API

- [ ] **Step 1: Write dashboard-generation safety tests**

Tests assert:

- actions are absent when `TB_PDM_MAINTENANCE_ACTIONS_ENABLED` is false or missing;
- enabled actions are `actionCellButton`/`customPretty` actions with stable UUIDs;
- `CREATE_WORK_ORDER` and `REJECT` appear only for `risk_state=ACTIVE` and `maintenance_state=PENDING_APPROVAL`;
- `CLOSE_RISK` appears for any maintenance state only while `risk_state=ACTIVE`;
- `CREATE_WORK_ORDER` is absent for `SUPPRESSED_ACTIVE_WORK_ORDER`;
- ACK remains enabled and contains no integration POST; native Clear is disabled;
- every action shows confirmation; trimmed reason is required for `REJECT`/`CLOSE_RISK`, and cancel performs no request;
- generated JavaScript uses `details.maintenance_alert_version`;
- generated JavaScript uses only `/api/v1/maintenance-alerts/`;
- absolute URLs, `//`, encoded path escape, and arbitrary `/api/` base paths are rejected;
- tokens and credentials are absent from generated JSON.
- with Actions enabled, managed `dashboard-plan` binds the new desired payload
  hash and `dashboard-apply` retains the Phase 2 drift/hash/receipt gate;
- the legacy `dashboard` command still refuses publication in managed mode.

Run:

```bash
set -Eeuo pipefail
cd components/thingsboard/dev/automotive-factory
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

Expected: FAIL because maintenance Actions do not exist.

- [ ] **Step 2: Generate the exact same-origin Action request**

The Action JavaScript must perform:

```javascript
widgetContext.http.post(
  '/api/v1/maintenance-alerts/' + encodeURIComponent(details.alert_id) + '/actions',
  {
    action: actionName,
    reason: reasonValue,
    expected_version: details.maintenance_alert_version
  },
  {
    headers: {
      'Idempotency-Key':
        'alert-action:' + details.alert_id + ':' + actionName
    }
  }
)
```

Each generated descriptor contains:

```text
actions.actionCellButton[]
type = customPretty
customHtml
customCss = ""
customResources = []
useShowWidgetActionFunction = true
showWidgetActionFunction(widgetContext, data)
customFunction
```

`customFunction` obtains the current Alarm from `additionalParams.alarm` before reading `details`. The ThingsBoard Angular HTTP interceptor supplies `X-Authorization` for this same-origin `/api` request. Do not read browser storage, embed a token, or add custom authentication JavaScript.

Set:

```yaml
predictive_maintenance:
  actions_enabled_env: TB_PDM_MAINTENANCE_ACTIONS_ENABLED
  gateway_base_path: /api/v1
```

Default actions to disabled in both config and `.env.example`.

Configure the Alarm widget with:

```text
alarmSearchStatus = ACTIVE
type filter = PDM_FORECAST_RISK
allowAcknowledgment = true
allowClear = false
reserveSpaceForHiddenAction = false
```

- [ ] **Step 3: Run dashboard and core authentication regressions**

Run:

```bash
set -Eeuo pipefail
components/thingsboard/dev/automotive-factory/dev.sh prepare
components/thingsboard/dev/automotive-factory/.venv/bin/python \
  -m unittest discover \
  -s components/thingsboard/dev/automotive-factory/tests \
  -p 'test_*.py' -v
mvn -f components/thingsboard/pom.xml \
  -s components/thingsboard/dev/automotive-factory/maven-settings.xml test \
  -pl application \
  -Dtest='org.thingsboard.server.controller.AlarmControllerTest,org.thingsboard.server.controller.AuthControllerTest' \
  -Dpkg.skip=true
```

Expected: Python dashboard tests and existing ThingsBoard Alarm/auth tests pass; no Java/Angular production file changes.

- [ ] **Step 4: Commit and push ThingsBoard**

Run:

```bash
set -Eeuo pipefail
./scripts/doctor.sh
git -C components/thingsboard add dev/automotive-factory
git -C components/thingsboard commit \
  -m "feat: add predictive maintenance alarm actions"
git -C components/thingsboard push
```

### Task 7: Deliver Approved Work Orders and Reconcile Unknown Results

**Files:**

- Create: `components/platform-integration/src/platform_integration/services/work_orders.py`
- Create: `components/platform-integration/src/platform_integration/workers/work_orders.py`
- Create: `components/platform-integration/tests/clients/test_cmms_work_order_client.py`
- Create: `components/platform-integration/tests/services/test_work_orders.py`
- Create: `components/platform-integration/tests/workers/test_work_order_outbox_worker.py`
- Modify: `components/platform-integration/src/platform_integration/clients/cmms.py`
- Modify: `components/platform-integration/src/platform_integration/contracts/cmms.py`
- Modify: `components/platform-integration/src/platform_integration/cli.py`
- Modify: `components/platform-integration/src/platform_integration/services/alarm_projection.py`

**Interfaces:**

- Produces: `CmmsWorkOrderClient.find_by_external_ref(alert_id) -> CmmsWorkOrder | None`
- Produces: `CmmsWorkOrderClient.create(command, idempotency_key) -> CmmsWorkOrder`
- Produces: `WorkOrderOutboxWorker.run_once(now, owner) -> DeliverySummary`
- Produces: `platform-integration work-order-worker [--once]`
- Consumes: `IMMUTABLE_COMMAND` outbox and CMMS Phase 3 API

- [ ] **Step 1: Write timeout, concurrency, and suppression tests**

Tests assert:

- worker queries external ref before every POST attempt;
- each claimed delivery attempt makes exactly one external-ref GET; that
  pre-write reconciliation disables inner HTTP retries, so timeout/5xx returns
  control to the outbox backoff and cannot multiply GETs or reach POST;
- absent external ref leads to one POST with `Idempotency-Key: wo:{alert_id}`;
- an external-ref GET timeout or 5xx causes no POST in that run;
- response loss after CMMS commit is resolved by the next external-ref query;
- an external-ref hit whose company scope, external source/ref, CMMS asset/equipment, TB Alarm, correlation, model profile/info, or policy version differs from the frozen command becomes terminal `RECONCILIATION_IDENTITY_CONFLICT`/`DEAD_LETTER`;
- concurrent workers create at most one CMMS work order;
- a later Alarm clear or policy update does not supersede the command;
- success changes `APPROVED/WORK_ORDER_CREATING` to `WORK_ORDER_OPEN`;
- the first claim changes `APPROVED` to `WORK_ORDER_CREATING`, increments Alert/projection versions, and writes one Alarm state outbox; retries do not repeat that transition;
- success stores CMMS ID/link and writes a newer Alarm projection state;
- a second active risk episode is `SUPPRESSED_ACTIVE_WORK_ORDER`;
- eight unresolved failures become `WORK_ORDER_CREATE_FAILED` plus `DEAD_LETTER`, increment the Alert/projection versions, and publish the failed state to ThingsBoard;
- no automatic replay occurs after `DEAD_LETTER`.

Run:

```bash
set -Eeuo pipefail
cd components/platform-integration
uv run pytest \
  tests/clients/test_cmms_work_order_client.py \
  tests/services/test_work_orders.py \
  tests/workers/test_work_order_outbox_worker.py -v
```

Expected: FAIL before the client and worker exist.

- [ ] **Step 2: Implement immutable command delivery**

Read the exact CMMS body, target company/asset, request digest, and idempotency key frozen in the approval outbox snapshot. The worker must not rebuild the command from the current equipment mapping, current Alarm projection, or current policy. It may change delivery metadata but never rewrites action, approver, target, Alert ID, or request body.

The state path is:

```text
APPROVED
  -> WORK_ORDER_CREATING once, when the worker first claims the command
  -> WORK_ORDER_OPEN or WORK_ORDER_CREATE_FAILED
```

The first `APPROVED → WORK_ORDER_CREATING` claim is its own local transaction: update maintenance state, increment `maintenance_alert.version` and `alarm_aggregate_version`, refresh the projection, and insert one Alarm desired-state event before external reconciliation. A retry that claims an already-creating command changes delivery lease/attempt metadata only.

For every attempt:

```text
GET external ref
if present: validate every frozen integration-identity field and accept only an exact match
if absent: POST exact stored body
if GET is unavailable: do not POST
if outcome unknown: leave retryable without a second POST in this run
```

The GET executes with credentials scoped to `target_cmms_company_id` and must match `external_source`, `external_ref`, `target_cmms_asset_id`, `equipment_id`, `tb_alarm_id`, `correlation_id`, `model_profile_id`, `model_info_id`, and `policy_version`. Any mismatch is a terminal reconciliation conflict; it must never be accepted as this command's work order.

On success or terminal failure, update the local maintenance state, increment the public Alert and Alarm aggregate versions, update the current projection, and write a new Alarm desired-state outbox in one transaction. Never clear risk because a work order exists or completes.

- [ ] **Step 3: Verify and commit Action/work-order delivery**

Run:

```bash
set -Eeuo pipefail
uv run --directory components/platform-integration --frozen pytest \
  tests/api/test_actions.py \
  tests/services/test_actions.py \
  tests/services/test_work_orders.py \
  tests/workers/test_alarm_outbox_worker.py \
  tests/workers/test_work_order_outbox_worker.py -v
uv run --directory components/platform-integration --frozen ruff check .
uv run --directory components/platform-integration --frozen ruff format --check .
uv sync --directory components/platform-integration --frozen
```

Expected: all tests/checks pass.

Commit:

```bash
set -Eeuo pipefail
./scripts/doctor.sh
git -C components/platform-integration add src tests
git -C components/platform-integration commit \
  -m "feat: approve and deliver predictive work orders"
git -C components/platform-integration push
```

### Task 8: Add the Same-Origin Gateway and Phase 3 End-to-End Gate

**Files:**

- Create: `deploy/gateway/predictive-maintenance.conf`
- Create: `tests/e2e/test_alarm_work_order.py`
- Create: `tests/e2e/test_dashboard_action_browser.py`
- Create: `tests/e2e/test_pilot_writes.py`
- Create: `tests/e2e/support/failure_proxy.py`
- Create: `tests/e2e/support/pilot_writes.py`
- Modify: `tests/e2e/conftest.py`
- Modify: `deploy/compose/predictive-maintenance-shadow.yml`
- Modify: `deploy/compose/predictive-maintenance-shadow.env.example`
- Modify: `deploy/README.md`
- Modify: `tests/pyproject.toml`
- Modify: `tests/uv.lock`
- Modify: `.github/workflows/workspace-check.yml`
- Modify: `components/cmms` gitlink
- Modify: `components/thingsboard` gitlink
- Modify: `components/platform-integration` gitlink

**Interfaces:**

- Produces: same-origin gateway for ThingsBoard UI and the precise integration Action path
- Produces: isolated browser and API acceptance tests
- Produces: `python -m e2e.support.pilot_writes plan|apply` with a
  separate-turn exact-hash gate
- Consumes: all Phase 3 component providers and root contracts

- [ ] **Step 1: Write the failing API and browser acceptance tests**

Split the live API scenario into two separately confirmed apply stages and
read-only verification. The read-only
`pilot_writes plan --scenario phase3-risk-preparation` freezes the exact
isolated tenant, 20 target devices, risky telemetry timestamp/value bodies,
two scheduler slots, prediction/Alarm-worker invocations, expected stable risk
keys, and expected one-Alarm outcomes. Its apply is the only code allowed to
perform this preparation:

1. injects risky telemetry for one isolated device;
2. runs two accelerated successful prediction slots;
3. asserts exactly one active `PDM_FORECAST_RISK` Alarm;
4. writes no ACK, Action, CMMS work order, or CMMS status mutation;
5. writes a mode-`0600` receipt containing only target/body hashes,
   worker-result hashes, and the exact Alarm/Alert/version precondition needed
   by the next plan.

This preparation requires its own 30-minute plan and a hash returned in a
later user turn; Phase 2 confirmations cannot authorize it. The subsequent
read-only `pilot_writes plan --scenario phase3-work-order` requires that exact
preparation receipt and then freezes one bounded scenario containing the exact
isolated tenant/company, TB user/Alarm, Alert/version, target asset/equipment,
Action bodies and idempotency keys:

1. ACK the Alarm and verify it creates no integration Action/work order;
2. pause the work-order worker;
3. submit one `CREATE_WORK_ORDER`, its same-key replay, one same-key/different
   body conflict probe, and one new-key/stale-version conflict probe;
4. submit the exact `CLOSE_RISK` body only after the immutable approval exists;
5. enable one response-loss fault and resume/restart the worker so the approved
   command survives close/recovery and creates exactly one CMMS work order.

This is one CMMS-create authorization: replay/conflict probes cannot create a
second command. The canonical 30-minute plan includes every target, method,
body SHA-256, idempotency key, expected result code, proxy fault, actor, and
expiry. It contains no token or credential.

`pilot_writes apply` is the only live test code allowed to issue those exact
mutations. It requires the later user-confirmed plan hash, revalidates every
current identity/version and the isolated tenant/company before the first
write, refuses partial drift, executes once, and writes a mode-`0600` receipt
containing only plan hash, IDs, result codes, and response hashes. Apply is
idempotent by the frozen keys and refuses an already completed receipt.

The final API verification test permits only GET/HEAD through its transport and
asserts exactly one work order, one immutable approval, the documented conflict
results, and the post-close delivery result.
It requires the exact mode-`0600` preparation and work-order receipts through
`--confirmed-preparation-receipt` and `--confirmed-write-receipt`;
`--pilot-read-only` rejects every upstream mutation and there is no allow-write
option.

The browser test intercepts and aborts the Action request before it reaches the
gateway, then inspects it and asserts:

- URL is the gateway's same origin;
- `X-Authorization` is present but its value is never printed or saved;
- body uses the displayed `maintenance_alert_version`;
- ACK emits no integration Action request;
- native Clear is absent;
- the listener records only whether `X-Authorization` exists and never stores its value.

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen playwright install chromium
uv run --directory tests --frozen pytest \
  e2e/test_pilot_writes.py \
  e2e/test_dashboard_action_browser.py -v
```

Expected: FAIL before the gateway, plan/apply helper, and coordinated services
are configured; MockTransport and browser interception guarantee this RED run
issues no upstream mutation.

- [ ] **Step 2: Configure the narrow gateway route**

Route this prefix before the general ThingsBoard `/api` route:

```nginx
location ^~ /api/v1/maintenance-alerts/ {
    proxy_pass http://integration-api:8080;
    proxy_set_header X-Authorization $http_x_authorization;
    proxy_set_header Authorization "";
    proxy_set_header Cookie "";
}
```

Proxy all other HTTP/WebSocket traffic to ThingsBoard using the existing deployment origin. Do not expose integration admin, replay, or provisioning routes through this browser path.

The integration API container listens on `0.0.0.0:8080`, matching the Phase 1 CLI/Docker contract. Add a gateway smoke test that reaches integration `/healthz` internally and proves the Action prefix does not return `502`.

Extend Compose with a `gateway` service pinned to `nginx:1.27-alpine`. It
mounts `deploy/gateway/predictive-maintenance.conf` read-only, publishes only
`127.0.0.1:18081:80`, and defines
`host.docker.internal:host-gateway`. The exact Action prefix proxies to
`integration-api:8080`; all other HTTP/WebSocket traffic proxies to the
current isolated ThingsBoard origin. The browser acceptance base is exactly
`http://127.0.0.1:18081`. A Compose contract test asserts these upstreams, the
loopback bind, and that no other integration/admin path is routed.

The stack also creates the named Docker network
`ifactory-pilot-shared`; `gateway` joins it with the unique alias
`predictive-maintenance-gateway` while retaining the default integration
network. No integration API/worker joins the shared network directly. This
gives the separately managed isolated CMMS container a callback path in Phase
4 without exposing the browser port beyond loopback.

```yaml
services:
  gateway:
    networks:
      default: {}
      pilot-shared:
        aliases: [predictive-maintenance-gateway]
networks:
  pilot-shared:
    name: ifactory-pilot-shared
```

The separately managed CMMS Compose declares that same name as
`external: true`; it does not create a competing network.

Extend Compose with `alarm-outbox-worker` and `work-order-worker`, running the
exact CLI roles from Tasks 3 and 7 with unique lease-owner IDs and the shared
integration database. PostgreSQL healthy → the current rebuilt
`integration-migrate` image completes migration `0002` successfully → API,
gateway, and both workers may start. The Alarm worker additionally requires
ThingsBoard dependency health and explicitly receives only
`PLATFORM_INTEGRATION_TB_CREDENTIAL_REF` plus
`PILOT_TB_CREDENTIAL: ${PILOT_TB_CREDENTIAL:?required}`. The work-order worker
additionally requires CMMS dependency health and receives only
`PLATFORM_INTEGRATION_CMMS_CREDENTIAL_REF` plus
`PILOT_CMMS_CREDENTIAL: ${PILOT_CMMS_CREDENTIAL:?required}`. Neither worker
exposes an HTTP port, and contract tests reject missing envelopes or any
unneeded credential.

Inject the selected approver identity into `integration-api` explicitly; a CLI
`--env-file` does not automatically create arbitrary container variables:

```yaml
environment:
  PILOT_TB_USER_ID: ${PILOT_TB_USER_ID:?set canonical isolated ThingsBoard test user UUID}
```

Do not pass it to workers that do not use it. A Compose contract test proves
the stack fails configuration when the value is absent, accepts only a
canonical UUID through the CLI validator, and exposes the exact non-secret UUID
to `approver-plan`.

Add Playwright to the locked root test project and cache its pinned Chromium download in CI. Browser traces, videos, and screenshots stay in ignored runtime output and must not capture token values.

- [ ] **Step 3: Apply the isolated Dashboard and approver grant with separate confirmations**

Before any live action, the operator-owned isolated CMMS must have been
separately rebuilt/restarted from the exact pushed Phase 3 CMMS commit and
prove the new external-ref/idempotency contract through read-only checks. Stop
if it is absent or stale; this plan does not replace an unowned CMMS process.

Before Compose parses its guarded environment, update the untracked component
file `components/thingsboard/dev/automotive-factory/.env` with
`TB_PDM_DASHBOARD_MANAGED_PUBLICATION=true`,
`TB_PDM_MAINTENANCE_ACTIONS_ENABLED=true`, and the exact retained
`TB_PDM_EXPECTED_TENANT_ID` discovered in Phase 2. Put
`PILOT_TB_USER_ID=<canonical-isolated-user-uuid>` in the root untracked shadow
env. Read both files with strict dotenv parsing, never by sourcing shell text,
and require the canonical tenant/user UUIDs before the first Compose command.

Rebuild the integration image, rerun the new migration, recreate every changed
role, then bring up and verify the gateway:

```bash
set -Eeuo pipefail
docker compose \
  --env-file .runtime/predictive-maintenance-shadow.env \
  -f deploy/compose/predictive-maintenance-shadow.yml \
  build integration-migrate integration-api \
  alarm-outbox-worker work-order-worker
docker compose \
  --env-file .runtime/predictive-maintenance-shadow.env \
  -f deploy/compose/predictive-maintenance-shadow.yml \
  up -d --no-deps --force-recreate integration-migrate
migrator_id="$(docker compose \
  --env-file .runtime/predictive-maintenance-shadow.env \
  -f deploy/compose/predictive-maintenance-shadow.yml \
  ps -a -q integration-migrate)"
test -n "$migrator_id"
test "$(docker wait "$migrator_id")" = 0
docker compose \
  --env-file .runtime/predictive-maintenance-shadow.env \
  -f deploy/compose/predictive-maintenance-shadow.yml \
  up -d --force-recreate \
  integration-api gateway alarm-outbox-worker work-order-worker
docker compose \
  --env-file .runtime/predictive-maintenance-shadow.env \
  -f deploy/compose/predictive-maintenance-shadow.yml \
  exec -T gateway \
  wget -qO- http://integration-api:8080/healthz
curl --fail --silent http://127.0.0.1:18081/
```

Expected: migration `0002` exits zero, all four long-running roles use the
fresh image, the gateway can reach integration by the Compose DNS name, and
the loopback gateway serves the approved isolated ThingsBoard origin.

The tracked Phase 3 environment example contains
`TB_PDM_MAINTENANCE_ACTIONS_ENABLED=false` and an empty
`PILOT_TB_USER_ID=` only; it never contains the selected user UUID.

Render the Dashboard update plan for the exact isolated tenant:

```bash
set -Eeuo pipefail
install -d -m 0700 .runtime/plans .runtime/receipts
pilot_root="$(pwd -P)"
test "$pilot_root" = "$(git rev-parse --show-toplevel)"
components/thingsboard/dev/automotive-factory/dev.sh dashboard-plan \
  --actor codex-isolated-pilot-operator \
  --output "$pilot_root/.runtime/plans/phase3-dashboard-actions.json"
```

Stop after displaying the tenant, current/desired payload hashes, and canonical
plan SHA-256. The Phase 2 Dashboard confirmation cannot authorize this changed
Action payload. In a later turn, assign
`USER_CONFIRMED_PHASE3_DASHBOARD_PLAN_SHA256` from the literal lowercase hash
in the user's current message, validate it, and apply exactly once:

```bash
set -Eeuo pipefail
test -n "${USER_CONFIRMED_PHASE3_DASHBOARD_PLAN_SHA256:-}"
printf '%s' "$USER_CONFIRMED_PHASE3_DASHBOARD_PLAN_SHA256" |
  rg -q '^[0-9a-f]{64}$'
pilot_root="$(pwd -P)"
test "$pilot_root" = "$(git rev-parse --show-toplevel)"
components/thingsboard/dev/automotive-factory/dev.sh dashboard-apply \
  --plan "$pilot_root/.runtime/plans/phase3-dashboard-actions.json" \
  --plan-hash "$USER_CONFIRMED_PHASE3_DASHBOARD_PLAN_SHA256" \
  --confirmed-hash "$USER_CONFIRMED_PHASE3_DASHBOARD_PLAN_SHA256" \
  --actor codex-isolated-pilot-operator \
  --receipt "$pilot_root/.runtime/receipts/phase3-dashboard-actions.json"
```

Apply rechecks the authenticated tenant and Phase 2 Dashboard snapshot,
publishes the Task 6 payload at most once, then GET-verifies its hash and writes
a mode-`0600` receipt. The legacy `./dev.sh dashboard` command remains blocked
in managed mode.

Separately run the following inside the configured integration container, display its exact target and SHA-256, and stop again:

```bash
set -Eeuo pipefail
docker compose \
  --env-file .runtime/predictive-maintenance-shadow.env \
  -f deploy/compose/predictive-maintenance-shadow.yml \
  run --rm --no-deps integration-api \
  platform-integration approver-plan \
  --tenant-alias ifactory-pilot \
  --tb-user-id-env PILOT_TB_USER_ID \
  --actor codex-isolated-pilot-operator \
  --reason "isolated phase-3 approver acceptance"
```

Only after the user returns that exact hash in a later turn, assign
`USER_CONFIRMED_APPROVER_PLAN_SHA256` to the literal lowercase hash copied
verbatim from that current user message, validate it, and run:

```bash
set -Eeuo pipefail
test -n "${USER_CONFIRMED_APPROVER_PLAN_SHA256:-}"
printf '%s' "$USER_CONFIRMED_APPROVER_PLAN_SHA256" |
  rg -q '^[0-9a-f]{64}$'
docker compose \
  --env-file .runtime/predictive-maintenance-shadow.env \
  -f deploy/compose/predictive-maintenance-shadow.yml \
  run --rm --no-deps integration-api \
  platform-integration approver-apply \
  --tenant-alias ifactory-pilot \
  --plan-hash "$USER_CONFIRMED_APPROVER_PLAN_SHA256" \
  --confirmed-hash "$USER_CONFIRMED_APPROVER_PLAN_SHA256" \
  --actor codex-isolated-pilot-operator
docker compose \
  --env-file .runtime/predictive-maintenance-shadow.env \
  -f deploy/compose/predictive-maintenance-shadow.yml \
  run --rm --no-deps integration-api \
  platform-integration approver-verify \
  --tenant-alias ifactory-pilot \
  --plan-hash "$USER_CONFIRMED_APPROVER_PLAN_SHA256"
```

The hash must come from that later user message, never from shell history or the
plan-producing turn. The verify command is read-only and must return one
consumed durable plan row whose user/reason/actor/hash match the reviewed plan.
Neither confirmation authorizes a production work order; all targets must
resolve to the isolated tenant/CMMS before apply.

- [ ] **Step 4: Prepare, separately confirm, apply, and verify the live work-order scenario**

First run only contracts/helper tests and render the risk-preparation plan:

```bash
set -Eeuo pipefail
install -d -m 0700 .runtime/plans .runtime/receipts
uv run --directory tests --frozen pytest contract/phase3 -v
uv run --directory tests --frozen pytest \
  e2e/test_pilot_writes.py -v
uv run --directory tests --frozen python -m e2e.support.pilot_writes plan \
  --scenario phase3-risk-preparation \
  --env-file ../.runtime/predictive-maintenance-shadow.env \
  --phase2-seed-receipt ../.runtime/receipts/phase2-shadow-seed.json \
  --actor codex-isolated-pilot-operator \
  --output ../.runtime/plans/phase3-risk-preparation.json
```

Stop for a later exact hash. In that later turn assign
`USER_CONFIRMED_PHASE3_RISK_PREPARATION_SHA256` from the literal lowercase
hash in the user's current message, validate it, and apply only the
preparation:

```bash
set -Eeuo pipefail
test -n "${USER_CONFIRMED_PHASE3_RISK_PREPARATION_SHA256:-}"
printf '%s' "$USER_CONFIRMED_PHASE3_RISK_PREPARATION_SHA256" |
  rg -q '^[0-9a-f]{64}$'
uv run --directory tests --frozen python -m e2e.support.pilot_writes apply \
  --scenario phase3-risk-preparation \
  --env-file ../.runtime/predictive-maintenance-shadow.env \
  --plan ../.runtime/plans/phase3-risk-preparation.json \
  --plan-hash "$USER_CONFIRMED_PHASE3_RISK_PREPARATION_SHA256" \
  --confirmed-hash "$USER_CONFIRMED_PHASE3_RISK_PREPARATION_SHA256" \
  --actor codex-isolated-pilot-operator \
  --receipt ../.runtime/receipts/phase3-risk-preparation.json
```

The helper GET-verifies the resulting single Alarm and stores its exact
Alert/version in the receipt. Preparation performs no ACK, Action, work order,
or CMMS status write. Now render the independent work-order plan:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen python -m e2e.support.pilot_writes plan \
  --scenario phase3-work-order \
  --env-file ../.runtime/predictive-maintenance-shadow.env \
  --preparation-receipt ../.runtime/receipts/phase3-risk-preparation.json \
  --actor codex-isolated-pilot-operator \
  --output ../.runtime/plans/phase3-work-order.json
```

Both plan commands are read-only and print canonical target summaries and
SHA-256 values. Each stop requires a hash from a later user message; Dashboard,
approver, Phase 2 seed, and risk-preparation confirmations do not authorize the
work-order apply.

In a later turn, assign
`USER_CONFIRMED_PHASE3_WORK_ORDER_PLAN_SHA256` to the literal lowercase hash
copied from that current user message, validate it, and apply exactly once:

```bash
set -Eeuo pipefail
test -n "${USER_CONFIRMED_PHASE3_WORK_ORDER_PLAN_SHA256:-}"
printf '%s' "$USER_CONFIRMED_PHASE3_WORK_ORDER_PLAN_SHA256" |
  rg -q '^[0-9a-f]{64}$'
uv run --directory tests --frozen python -m e2e.support.pilot_writes apply \
  --scenario phase3-work-order \
  --env-file ../.runtime/predictive-maintenance-shadow.env \
  --plan ../.runtime/plans/phase3-work-order.json \
  --plan-hash "$USER_CONFIRMED_PHASE3_WORK_ORDER_PLAN_SHA256" \
  --confirmed-hash "$USER_CONFIRMED_PHASE3_WORK_ORDER_PLAN_SHA256" \
  --actor codex-isolated-pilot-operator \
  --receipt ../.runtime/receipts/phase3-work-order.json
```

Then run only read-only verification; the browser test aborts its intercepted
Action request:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  -m pilot_e2e \
  e2e/test_alarm_work_order.py::test_phase3_results_read_only \
  e2e/test_dashboard_action_browser.py \
  --pilot-read-only \
  --confirmed-preparation-receipt \
  ../.runtime/receipts/phase3-risk-preparation.json \
  --confirmed-write-receipt \
  ../.runtime/receipts/phase3-work-order.json -v
./scripts/doctor.sh
```

Expected: apply uses only the confirmed plan, read-only verification passes,
and doctor reports zero failures. The hash must come from the later user
message, never the plan-producing turn.

- [ ] **Step 5: Commit the coordinated Phase 3 slice**

After the three component branches are pushed:

```bash
set -Eeuo pipefail
git add \
  components/cmms \
  components/thingsboard \
  components/platform-integration \
  contracts/openapi/cmms-integration-v1.yaml \
  contracts/openapi/platform-integration-v1.yaml \
  contracts/json-schema/maintenance-alert-v1.json \
  deploy/gateway \
  deploy/compose \
  deploy/README.md \
  tests/contract/phase3 \
  tests/e2e/conftest.py \
  tests/e2e/test_alarm_work_order.py \
  tests/e2e/test_dashboard_action_browser.py \
  tests/e2e/test_pilot_writes.py \
  tests/e2e/support/failure_proxy.py \
  tests/e2e/support/pilot_writes.py \
  tests/pyproject.toml \
  tests/uv.lock \
  tests/README.md \
  .github/workflows/workspace-check.yml
git diff --cached --check
git diff --cached --submodule=log
./scripts/doctor.sh
git commit -m "feat: add approved predictive work order flow"
```

Expected: root contracts, both providers, consumers, gateway, and acceptance tests land in one reproducible delivery slice.
