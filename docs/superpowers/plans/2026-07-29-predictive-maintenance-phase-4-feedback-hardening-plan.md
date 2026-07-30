# Predictive Maintenance Phase 4 Feedback and Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the CMMS feedback loop and prove that status delivery, reconciliation, restart recovery, dead letters, and security controls remain correct under duplicate, lost, delayed, forged, and out-of-order events.

**Architecture:** CMMS writes a stable status event and endpoint delivery rows in the same database transaction as each work-order status change. `platform-integration` authenticates the v2 webhook into a durable inbox, projects accepted status into feedback and the current ThingsBoard Alarm view, and independently polls non-terminal work orders every five minutes. Both sides use leases, bounded retries, explicit replay approval, and dependency-aware readiness.

**Tech Stack:** Java 17/Spring Boot/JPA/Liquibase/Quartz/JUnit/Mockito, Python 3.12/uv/FastAPI/Pydantic/httpx/SQLAlchemy/Alembic/PostgreSQL/pytest/Prometheus client, AsyncAPI 3.0, JSON Schema 2020-12, Docker Compose.

## Global Constraints

- Follow the master plan and complete the Phase 3 Alarm/work-order gate first.
- CMMS is authoritative for `cmms_company_id`, `work_order_id`, work-order status, and event version. It does not mint a platform `tenant_id`.
- `platform-integration` derives `tenant_id` through the unique enabled `tenant_binding.cmms_company_id`; mismatched or unmapped company IDs fail closed.
- Existing CMMS v1 webhooks retain body-only HMAC behavior. New integration status endpoints explicitly opt into signature version `v2`.
- Each eligible webhook delivery freezes its own v1-or-v2 wire body, canonical
  target, signature version, and payload hash; endpoint changes never retarget
  an existing delivery.
- Signature v2 is lowercase hex `HMAC-SHA256(secret, utf8(timestamp + "." + exact_request_body))`, with `X-Webhook-Signature-Version: v2`, stable `X-Webhook-Id`, `X-Webhook-Event-Version`, event type, and epoch-millisecond timestamp headers.
- The v2 consumer accepts a maximum 64 KiB body, a five-minute clock skew, constant-time signature comparison, and no redirects.
- The trusted callback allowlist is empty by default and contains exact canonical base URLs only. It has no wildcard, suffix, substring, raw-prefix, or caller-controlled bypass.
- Webhook entitlement absence is an explicit degraded mode: polling remains available, `/readyz` stays process-ready with `DEGRADED_WEBHOOK_DISABLED`, and dependency health is not represented as full health.
- A configured CMMS credential envelope must be either `cmms_api_key` or a pre-issued `cmms_bearer`. Missing/malformed configuration makes readiness false; temporary remote reachability/auth validation belongs to dependency health and does not make the process unready.
- `inbox-worker` and `reconciliation-worker` receive the same role-minimal
  runtime CMMS credential so they can perform company-scoped GET enrichment
  and reconciliation. Neither receives the SETTINGS credential or webhook
  signing secret.
- Duplicate or stale status never rewinds maintenance state, overwrites a newer Alarm projection, or creates a second feedback row.
- Work-order completion updates feedback and maintenance state but never clears predictive risk and never triggers training.
- Dead-letter replay is an external write. It always uses plan/apply with a new correlation ID, exact plan hash, named actor, and non-blank reason; it is never automatically retried after the configured limit.
- Callback configuration approval does not authorize telemetry/risk
  preparation, CMMS status changes, a work-order Action, or replay. Phase 4
  uses independent 30-minute plans and later-turn hashes for: (1) bounded
  pre-status telemetry/risk preparation, (2) the exact isolated
  `IN_PROGRESS → ON_HOLD → COMPLETE` sequence and its status-delivery probes,
  (3) one exact replay-target `CREATE_WORK_ORDER` Action and its bounded
  response-loss/reconciliation-failure setup, (4) bounded post-status risk
  recovery, and (5) replay of the resulting one dead-letter event.
  Preparation and recovery are separate plans even though both are telemetry
  writes. Each produces a separate durable receipt; one hash never authorizes
  another class or stage.
- Every command block starts at the current implementation worktree's superproject root unless it contains its own `cd`; no block inherits another block's working directory.
- Every feedback projection writer follows the Phase 3 lock order: replay/idempotency lock when applicable → tenant-scoped stable risk state → maintenance Alert → projection/outbox.

---

### Task 0: Resume the Exact Phase 3 Component Branches

**Files:** None.

**Interfaces:**

- Consumes: Phase 3 superproject gitlinks and pushed feature branches
- Produces: clean attached CMMS, ThingsBoard, PDM, and integration worktrees

- [ ] **Step 1: Verify every recorded tip**

Run from the current implementation worktree root:

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

Expected: all branches are attached at the exact Phase 3 gitlinks. Stop instead of merging or resetting on any mismatch.

### Task 1: Freeze the Status-Event and Feedback Contracts

**Files:**

- Create: `contracts/asyncapi/cmms-maintenance-events-v1.yaml`
- Create: `contracts/json-schema/cmms-work-order-status-event-v1.json`
- Create: `tests/contract/phase4/test_contract_documents.py`
- Create: `tests/contract/phase4/test_status_event_examples.py`
- Create: `tests/contract/phase4/test_signature_v2_vectors.py`
- Create: `tests/contract/phase4/fixtures/work-order-status-open.json`
- Create: `tests/contract/phase4/fixtures/work-order-status-complete.json`
- Create: `tests/contract/phase4/fixtures/work-order-status-change-request.json`
- Create: `tests/contract/phase4/fixtures/webhook-v2-vector.json`
- Modify: `contracts/openapi/cmms-integration-v1.yaml`
- Modify: `contracts/openapi/platform-integration-v1.yaml`
- Modify: `contracts/json-schema/maintenance-alert-v1.json`

**Interfaces:**

- Produces: `WORK_ORDER_STATUS_CHANGE` AsyncAPI message
- Produces: `POST /api/v1/webhooks/cmms/work-order-status`
- Produces: authenticated CMMS integration-capabilities operation
- Produces: work-order query response with `event_version`
- Produces: idempotent integration-mode
  `PATCH /api/work-orders/{id}/change-status`
- Consumes: Phase 3 external work-order identity

- [ ] **Step 1: Write failing document and signature-vector tests**

Validate AsyncAPI/OpenAPI/JSON Schema and assert the v2 vector with:

```text
secret    = pilot-webhook-test-secret
timestamp = 1785283200000
signature = 011406445215fe5d88f2762bf2faa8e4686272f756f209dc9ccfe797cabf2be0
```

The exact no-newline body is:

```json
{"cmms_company_id":42,"correlation_id":"00000000-0000-4000-8000-000000000301","event_id":"00000000-0000-4000-8000-000000000401","event_version":3,"external_ref":"00000000-0000-4000-8000-000000000201","external_source":"PDM_FORECAST","new_status":"COMPLETE","occurred_at":"2026-07-29T00:00:00Z","previous_status":"IN_PROGRESS","updated_at":"2026-07-29T00:00:00Z","work_order_id":1001}
```

The Java provider test and Python consumer test independently calculate the expected lowercase HMAC literal; neither imports the other's signing code.

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest contract/phase4 -v
```

Expected: FAIL because the documents and vector do not yet exist.

- [ ] **Step 2: Define the exact event envelope**

The payload requires:

```text
event_id                  UUID
event_version             integer >= 1
occurred_at               RFC 3339 UTC
cmms_company_id           integer >= 1
correlation_id            UUID
work_order_id             integer >= 1
external_source           PDM_FORECAST
external_ref              alert_id UUID
previous_status           OPEN | IN_PROGRESS | ON_HOLD | COMPLETE
new_status                OPEN | IN_PROGRESS | ON_HOLD | COMPLETE
updated_at                RFC 3339 UTC
```

`cmms_company_id` replaces any claimed platform tenant. `event_id`, `event_version`, and payload bytes stay unchanged across retries. `event_version` is monotonic per CMMS work order.

Define an additive integration mode for the existing status endpoint:

```text
PATCH /api/work-orders/{id}/change-status
Idempotency-Key: status:{work_order_id}:{target_status}:{expected_event_version}
X-Correlation-ID: canonical UUID
X-Confirmed-Plan-SHA256: lowercase 64-hex
X-Expected-Event-Version: integer >= 0
```

If any integration header is present, all four are required. The canonical
idempotency digest covers exactly the server-derived company ID, work-order ID,
expected event version, target status, correlation ID, and confirmed plan hash.
Same company/key/digest returns the first response even after event version
advances; same key/different digest returns `409 IDEMPOTENCY_CONFLICT`; a new
key with stale expected version returns `409 EVENT_VERSION_CONFLICT`. The
ordinary CMMS UI request with none of these headers remains backward
compatible.

- [ ] **Step 3: Define capabilities and compatibility**

Authenticated `GET /api/integration-capabilities` returns:

```json
{
  "cmms_company_id": 42,
  "api_access_entitled": true,
  "api_access_plan_enabled": true,
  "webhook_entitled": true,
  "webhook_plan_enabled": true,
  "trusted_callback_base_configured": true,
  "trusted_callback_base_sha256": "dfa0bdb45a122f110202001d041356d83018349a6c5f8bf281d9552d68aba31c",
  "integration_endpoint_ready": true,
  "webhook_signature_versions": ["v1", "v2"],
  "work_order_external_ref": true,
  "work_order_event_version": true
}
```

`trusted_callback_base_configured` means the exact expected canonical base is
present in server configuration; it does not depend on an endpoint row.
`integration_endpoint_ready` means the authenticated company already owns the
matching enabled v2 endpoint. The integration service derives effective
capabilities from these separate primitives, avoiding a clean-install
bootstrap cycle. It must accept unavailable webhook delivery as polling-only
degraded mode. Missing external-ref lookup or event version is incompatible
and makes readiness false.

Add `cmms_status`, `cmms_event_version`, and `cmms_status_updated_at` to the Alarm projection schema.

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest contract/phase4 -v
```

Expected: contract tests pass. Keep root contract changes uncommitted until Tasks 2–6 pass.

### Task 2: Harden CMMS Callback Validation and Capability Discovery

**Files:**

- Create: `components/cmms/api/src/main/java/com/grash/configuration/WebhookSecurityProperties.java`
- Create: `components/cmms/api/src/main/java/com/grash/configuration/HostResolver.java`
- Create: `components/cmms/api/src/main/java/com/grash/dto/webhook/ValidatedWebhookTarget.java`
- Create: `components/cmms/api/src/main/java/com/grash/dto/webhook/WebhookSecretIssuedDTO.java`
- Create: `components/cmms/api/src/main/java/com/grash/model/enums/webhook/WebhookSignatureVersion.java`
- Create: `components/cmms/api/src/main/java/com/grash/service/WebhookHttpClient.java`
- Create: `components/cmms/api/src/main/java/com/grash/dto/integration/IntegrationCapabilitiesShowDTO.java`
- Create: `components/cmms/api/src/main/java/com/grash/service/IntegrationCapabilitiesService.java`
- Create: `components/cmms/api/src/main/java/com/grash/controller/IntegrationCapabilitiesController.java`
- Create: `components/cmms/api/src/test/java/com/grash/utils/WebhookUrlValidatorTest.java`
- Create: `components/cmms/api/src/test/java/com/grash/service/WebhookHttpClientTest.java`
- Create: `components/cmms/api/src/test/java/com/grash/controller/IntegrationCapabilitiesControllerTest.java`
- Create: `components/cmms/api/src/test/java/com/grash/controller/WebhookEndpointControllerTest.java`
- Create: `components/cmms/api/src/test/java/com/grash/service/WebhookEndpointServiceTest.java`
- Create: `components/cmms/api/src/main/resources/db/changelog/2026_07_29_00000000003_harden_webhook_endpoint.xml`
- Modify: `components/cmms/api/src/main/java/com/grash/utils/WebhookUrlValidator.java`
- Modify: `components/cmms/api/src/main/java/com/grash/controller/WebhookEndpointController.java`
- Modify: `components/cmms/api/src/main/java/com/grash/service/WebhookEndpointService.java`
- Modify: `components/cmms/api/src/main/java/com/grash/repository/WebhookEndpointRepository.java`
- Modify: `components/cmms/api/src/main/java/com/grash/model/WebhookEndpoint.java`
- Modify: `components/cmms/api/src/main/java/com/grash/dto/webhookEndpoint/WebhookEndpointPostDTO.java`
- Modify: `components/cmms/api/src/main/java/com/grash/dto/webhookEndpoint/WebhookEndpointPatchDTO.java`
- Modify: `components/cmms/api/src/main/java/com/grash/dto/webhookEndpoint/WebhookEndpointShowDTO.java`
- Modify: `components/cmms/api/src/main/java/com/grash/mapper/WebhookEndpointMapper.java`
- Modify: `components/cmms/api/src/main/resources/db/master.xml`
- Modify: `components/cmms/api/src/main/resources/application.yml`

**Interfaces:**

- Produces: `WebhookUrlValidator.validate(String candidate, WebhookSecurityProperties properties, HostResolver resolver)`
- Produces: redirect-disabled `WebhookHttpClient.post(ValidatedWebhookTarget target, headers, body)`
- Produces: `GET /api/integration-capabilities`
- Consumes: `CMMS_WEBHOOK_TRUSTED_CALLBACK_BASES`, default empty

- [ ] **Step 1: Write SSRF, canonicalization, and redirect tests**

Use an injected fake resolver; tests never perform live DNS. Assert:

- normal public HTTPS endpoints still pass existing validation;
- private/loopback/link-local/metadata targets fail when allowlist is empty;
- one exact configured integration base may resolve private;
- scheme, IDNA-normalized host, and effective port must equal the configured base;
- canonical candidate path segments must begin with the complete configured base segment list;
- `/api/v1/webhooks/cmms` does not authorize `/api/v1/webhooks/cmms-evil`;
- user info, fragment, query in the configured base, encoded slash/backslash, dot segments, double encoding, and control characters fail;
- DNS result is checked for every dispatch;
- HTTP 30x is an error and is never followed;
- an untrusted `Host` or `X-Forwarded-Host` cannot alter validation;
- endpoint list/read/create/update/delete/rotate-secret requires `SETTINGS`
  permission and resolves by `(id,current_company_id)`; an ordinary
  same-company `ROLE_CLIENT` cannot view endpoint configuration or rotate a
  secret;
- a user from company A cannot read, update, delete, rotate, or replay company B's endpoint even when the numeric ID is known;
- `trusted_callback_base_configured` and its SHA-256 reflect only the exact
  canonical server configuration, independent of endpoint rows;
- `integration_endpoint_ready` is true only when the authenticated company
  owns an enabled v2 endpoint whose canonical URL is that trusted target;
- list/show/capability/log responses never contain a webhook secret; only
  create and rotate return a dedicated one-time secret response;
- concurrent rotate requests bind the expected `secret_generation`, and exactly
  one SETTINGS-authorized caller can advance it.

Run:

```bash
set -Eeuo pipefail
mvn -f components/cmms/api/pom.xml \
  -Dtest='WebhookUrlValidatorTest,WebhookHttpClientTest,IntegrationCapabilitiesControllerTest,WebhookEndpointControllerTest,WebhookEndpointServiceTest' test
```

Expected: FAIL before properties, injected resolution, and capability endpoint exist.

- [ ] **Step 2: Implement exact base matching**

Parse and canonicalize configured bases at startup. Compare:

```text
lowercase scheme
IDNA ASCII lowercase host without trailing dot
effective port (80 for HTTP, 443 for HTTPS)
normalized decoded path segment list
```

Compare path segments structurally; do not use raw-string `startsWith`. Preserve the exact validated URI for a redirect-disabled call. A trusted base bypasses only the private-address rejection for that exact scheme/host/port/path boundary; metadata destinations, redirects, and malformed paths remain blocked.

`HostResolver` is an injectable interface that returns every resolved address for a canonical hostname; production uses the platform resolver and tests use a fake. `ValidatedWebhookTarget` is an immutable DTO containing only the canonical URI and the already-validated addresses used for the immediate redirect-disabled call.

- [ ] **Step 3: Add authenticated capability discovery**

Add `signature_version` and `secret_generation` to `WebhookEndpoint`; existing
rows become `v1` with generation one, while new integration endpoints may
explicitly request `v2`. Derive capabilities
from the current authenticated company, global license entitlement,
subscription features, deployed signature support, and that company's enabled
endpoints. All endpoint list/read/create/update/delete/rotate-secret
service/controller paths require `SETTINGS` and use company-scoped repository
methods. Delete is a soft delete that disables the endpoint but preserves the
row and secret while any historical delivery references it; no endpoint API
can retarget or purge an existing delivery. Do not trust query parameters
naming a company. Return no license keys, subscription payload, API key,
webhook secret, or endpoint URL. Remove the secret from
`WebhookEndpointShowDTO`; create/rotate return it exactly once through
`WebhookSecretIssuedDTO` with endpoint ID, generation, and rotation time.
Rotation uses a company-scoped pessimistic lock plus expected generation, and
both controller and service enforce `SETTINGS`.

Do not derive callback-base configuration from endpoint existence. A
SETTINGS-authorized bootstrap helper may create the first endpoint only when
webhook entitlement, plan feature, v2 support, and the exact trusted base are
already present; `integration_endpoint_ready=false` is then the reason to
create, not a reason to skip.

Run:

```bash
set -Eeuo pipefail
mvn -f components/cmms/api/pom.xml \
  -Dtest='WebhookUrlValidatorTest,WebhookHttpClientTest,IntegrationCapabilitiesControllerTest,WebhookEndpointControllerTest,WebhookEndpointServiceTest' test
mvn -f components/cmms/api/pom.xml -DskipTests compile
```

Expected: tests and compilation pass.

### Task 3: Make CMMS Work-Order Status Delivery Durable

**Files:**

- Create: `components/cmms/api/src/main/java/com/grash/model/WebhookOutboxEvent.java`
- Create: `components/cmms/api/src/main/java/com/grash/model/WebhookOutboxDelivery.java`
- Create: `components/cmms/api/src/main/java/com/grash/model/WebhookOutboxReplayAudit.java`
- Create: `components/cmms/api/src/main/java/com/grash/model/WebhookOutboxReplayPlan.java`
- Create: `components/cmms/api/src/main/java/com/grash/model/enums/webhook/WebhookOutboxDeliveryStatus.java`
- Create: `components/cmms/api/src/main/java/com/grash/dto/webhook/WorkOrderStatusChangedPayload.java`
- Create: `components/cmms/api/src/main/java/com/grash/dto/webhook/WebhookOutboxReplayPlanDTO.java`
- Create: `components/cmms/api/src/main/java/com/grash/dto/webhook/WebhookOutboxReplayRequestDTO.java`
- Create: `components/cmms/api/src/main/java/com/grash/repository/WebhookOutboxEventRepository.java`
- Create: `components/cmms/api/src/main/java/com/grash/repository/WebhookOutboxDeliveryRepository.java`
- Create: `components/cmms/api/src/main/java/com/grash/repository/WebhookOutboxReplayAuditRepository.java`
- Create: `components/cmms/api/src/main/java/com/grash/repository/WebhookOutboxReplayPlanRepository.java`
- Create: `components/cmms/api/src/main/java/com/grash/service/WorkOrderStatusOutboxService.java`
- Create: `components/cmms/api/src/main/java/com/grash/service/WebhookOutboxReplayService.java`
- Create: `components/cmms/api/src/main/java/com/grash/job/WebhookOutboxDispatchJob.java`
- Create: `components/cmms/api/src/main/java/com/grash/configuration/WebhookOutboxQuartzConfig.java`
- Create: `components/cmms/api/src/main/java/com/grash/controller/WebhookOutboxAdminController.java`
- Create: `components/cmms/api/src/main/resources/db/changelog/2026_07_29_00000000004_create_webhook_outbox.xml`
- Create: `components/cmms/api/src/test/java/com/grash/service/WorkOrderStatusOutboxServiceTest.java`
- Create: `components/cmms/api/src/test/java/com/grash/job/WebhookOutboxDispatchJobTest.java`
- Create: `components/cmms/api/src/test/java/com/grash/controller/WebhookOutboxAdminControllerTest.java`
- Create: `components/cmms/api/src/test/java/com/grash/integration/WebhookOutboxIntegrationTest.java`
- Create: `components/cmms/api/src/test/java/com/grash/integration/WorkOrderStatusIdempotencyTest.java`
- Modify: `components/cmms/api/src/main/java/com/grash/model/enums/IntegrationOperation.java`
- Modify: `components/cmms/api/src/main/java/com/grash/service/IntegrationIdempotencyService.java`
- Modify: `components/cmms/api/src/main/java/com/grash/model/WorkOrder.java`
- Modify: `components/cmms/api/src/main/java/com/grash/dto/workOrder/WorkOrderShowDTO.java`
- Modify: `components/cmms/api/src/main/java/com/grash/mapper/WorkOrderMapper.java`
- Modify: `components/cmms/api/src/main/java/com/grash/repository/WorkOrderRepository.java`
- Modify: `components/cmms/api/src/main/java/com/grash/service/WorkOrderService.java`
- Modify: `components/cmms/api/src/main/java/com/grash/controller/WorkOrderController.java`
- Modify: `components/cmms/api/src/main/java/com/grash/service/WebhookDispatchService.java`
- Modify: `components/cmms/api/src/main/resources/db/master.xml`
- Modify: `components/cmms/api/src/main/resources/application.yml`
- Modify: `components/cmms/api/src/test/resources/application-test.yml`
- Modify: `components/cmms/api/src/test/java/com/grash/service/WorkOrderServiceTest.java`

**Interfaces:**

- Produces: `WorkOrderStatusOutboxService.record(workOrder, previousStatus, newStatus) -> WebhookOutboxEvent`
- Produces: `WebhookOutboxDispatchJob.execute(JobExecutionContext context)`
- Produces: exact-target replay plan/apply operations for one CMMS delivery
- Produces: stable v2 webhook headers and exact payload
- Produces: idempotent confirmed integration status transitions
- Consumes: the current WorkOrder status transaction and eligible webhook endpoints

- [ ] **Step 1: Write transaction, ordering, lease, and retry tests**

Tests assert:

- status change and event insertion commit or roll back together;
- assigning the current status again creates no event or version increment;
- integration status mode requires all four confirmation/idempotency headers;
  same key/digest replays the first response, different digest conflicts, a new
  stale expected version conflicts, and ordinary headerless UI status change
  remains compatible;
- `WorkOrder` is selected with `PESSIMISTIC_WRITE` for a status transition;
- event version begins at one and increases once per committed status change;
- unique `(work_order_id,event_version)` and unique `event_id`;
- every event persists authoritative `company_id`, and every claim/replay is scoped to that company;
- one event may have independent endpoint delivery rows;
- endpoint filters and `serialize` behavior are evaluated in the status
  transaction; each eligible delivery freezes its canonical target URL,
  signature version, exact payload bytes, and payload SHA-256;
- retry/replay preserves event ID, event version, target URL, body bytes,
  payload hash, and signature version; an endpoint URL/signature-version
  change or soft deletion never redirects or sends that historical delivery
  and instead produces a permanent target-state dead letter;
- rotating only the endpoint secret lets an old delivery use the current
  secret while retaining frozen target/body/version;
- a payload/hash mismatch makes no HTTP call and dead-letters permanently;
- v1 delivery bytes remain the current camelCase
  `workOrderId`/`workOrderTitle`/`previousStatus`/`newStatus` envelope with
  `occurredAt`, `companyId`, and endpoint-specific nullable
  `changedWorkOrder`; its signature remains body-only HMAC;
- v2 delivery bytes are the new fixed snake_case status-event envelope and use
  timestamp-dot-body HMAC;
- `SELECT FOR UPDATE SKIP LOCKED` prevents double claim;
- delivery transaction ends before the HTTP request begins;
- lease expiry allows restart recovery;
- a late finalize with an old lease token or replay generation changes no row;
- success marks only that endpoint delivery `DELIVERED`;
- eight failures mark that delivery `DEAD_LETTER`;
- eight failures are counted within one replay generation; a confirmed replay
  increments `replay_generation`, resets only `attempt_in_generation`, and
  retains monotonic `lifetime_attempt`;
- entitlement-disabled delivery pauses without consuming an attempt;
- replay planning rejects missing reason/new correlation ID, a non-dead-letter
  state, or any non-replayable integrity/identity/target error;
- replay apply rejects a wrong/expired/consumed plan hash, different target or
  generation, and every permanent error even when a user confirms it;
- replay persists actor, reason, original/new correlation IDs, confirmed plan hash, timestamp, and result in an immutable audit row;
- a user cannot plan or apply replay for another company's delivery;
- `GET /api/work-orders/{id}` maps database `integration_event_version` to JSON `event_version`;
- the Quartz dispatcher is disabled by default in the test profile and cannot race integration tests;
- ordinary `WORK_ORDER_CHANGE` behavior remains unchanged;
- `WORK_ORDER_STATUS_CHANGE` no longer depends on process-local `@Async`.

Run:

```bash
set -Eeuo pipefail
mvn -f components/cmms/api/pom.xml \
  -Dtest='WorkOrderStatusIdempotencyTest,WorkOrderStatusOutboxServiceTest,WebhookOutboxDispatchJobTest,WebhookOutboxAdminControllerTest,WebhookOutboxIntegrationTest,WorkOrderServiceTest' test
```

Expected: FAIL before the outbox schema and job exist.

- [ ] **Step 2: Add event version and outbox schema**

Add `integration_event_version BIGINT NOT NULL DEFAULT 0` to `work_order` and its Envers audit representation. Map it through `WorkOrderShowDTO`/`WorkOrderMapper` as JSON `event_version`. Create event, delivery, and replay-audit tables with:

```text
event_id
company_id
work_order_id
event_version
event_type
previous_status
new_status
occurred_at
created_at

delivery_id
event_id
webhook_endpoint_id
canonical_target_url
signature_version
payload_text
payload_sha256
status
replay_generation
attempt_in_generation
lifetime_attempt
next_attempt_at
lease_owner
lease_token
lease_expires_at
last_error_code
delivered_at

replay_audit_id
company_id
delivery_id
actor_user_id
reason
original_correlation_id
new_correlation_id
confirmed_plan_hash
applied_at
result_code

replay_plan_id
company_id
delivery_id
canonical_plan_json
plan_sha256
expected_replay_generation
expected_attempt_in_generation
reason
new_correlation_id
created_by_user_id
expires_at
consumed_at
```

The event row stores authoritative status facts, not one universal wire body.
Each delivery's `payload_text` is PostgreSQL `TEXT` containing that endpoint's
exact serialized string; it is never `JSONB` and is never
deserialized/reserialized for delivery. `canonical_target_url` is the complete
validated URI frozen at event creation. `webhook_endpoint_id` has a restrictive
foreign key to a soft-deletable endpoint row so its secret remains available;
dispatch never substitutes a mutable URL, signature version, filters, or
serialize flag. It reloads the company-scoped endpoint only to verify that it
is still enabled, that its current canonical URL and signature version equal
the frozen values, and to obtain the current secret. URL/version drift or soft
deletion permanently dead-letters the delivery without an HTTP request;
secret rotation alone is allowed. Endpoint changes affect the payload/target
of future events only. The platform integration endpoint is explicitly
created as `v2`.

Every claim creates a fresh UUID `lease_token`. Finalize updates must match
delivery ID, `IN_FLIGHT` status, lease token, and replay generation; a late
response from an expired lease or older generation is a no-op and cannot
overwrite replay state. Before signing, recompute and compare
`payload_sha256`; corruption becomes a permanent dead letter and no HTTP call
is made.

Delivery states are exactly `PENDING`, `IN_FLIGHT`, `RETRY_WAIT`,
`PAUSED_ENTITLEMENT`, `DELIVERED`, and `DEAD_LETTER`. Retryable failure with
fewer than eight attempts enters `RETRY_WAIT`; attempt eight or a permanent
integrity/target error enters `DEAD_LETTER`. Entitlement loss enters
`PAUSED_ENTITLEMENT` without incrementing either attempt counter and returns to
`PENDING` only after capability recovery. `DELIVERED` has no exit, and only a
confirmed replay can move a replayable `DEAD_LETTER` to `PENDING`.
`RETRY_EXHAUSTED` and explicitly classified transient delivery exhaustion are
replayable. Payload/hash corruption, company/identity mismatch, endpoint
deletion, and target/signature-version drift are permanent; plan and apply
both reject them, so confirmation can never revive a security conflict.

- [ ] **Step 3: Build a truthful immutable payload**

The provider gets `cmms_company_id` from `workOrder.company.id`; it never accepts platform tenant input. For integrated work orders, copy the Phase 3 correlation and external identity from the stored WorkOrder.

For each eligible v1 endpoint, execute the current
`WORK_ORDER_STATUS_CHANGE` filter and serializer behavior inside the status
transaction and serialize its legacy camelCase map once with the configured
`ObjectMapper`. Preserve the endpoint's `serialize=false` behavior as a JSON
`null` `changedWorkOrder`. A golden regression fixture captured from the
pre-change implementation must compare exact bytes and body-only HMAC.

For each eligible v2 endpoint, serialize the fixed snake_case contract envelope
once. Store each endpoint-specific exact string and SHA-256 on its delivery row
and always reuse those UTF-8 bytes.

For non-PDM work orders, the durable event may contain nullable external fields for existing consumers. Only an event with `external_source=PDM_FORECAST` and a UUID external ref is sent to the platform integration endpoint.

Add `CHANGE_WORK_ORDER_STATUS` to `IntegrationOperation` and route only the
all-four-header integration mode through `IntegrationIdempotencyService`.
Resolve company/work-order under the authenticated user, compute the exact
contract digest before mutation, return an existing same-digest record before
checking the now-advanced version, and otherwise require
`integration_event_version == X-Expected-Event-Version`. Store the first HTTP
status/response atomically with the status transition and durable event.
Persist correlation and confirmed plan hash only in bounded integration audit
metadata; neither becomes mutable work-order free text or webhook secret data.

- [ ] **Step 4: Implement claim/send/finalize without holding a database transaction over HTTP**

Claim a bounded batch and commit the leases. For each delivery:

1. load immutable stored payload;
2. validate endpoint again;
3. create timestamp and v1 or v2 signature;
4. send with redirects disabled;
5. finalize success or schedule bounded exponential retry in a new transaction.

For v2 set:

```text
X-Webhook-Signature-Version: v2
X-Webhook-Signature: lowercase hex HMAC
X-Webhook-Timestamp: epoch milliseconds
X-Webhook-Id: stable event UUID
X-Webhook-Event: WORK_ORDER_STATUS_CHANGE
X-Webhook-Event-Version: stable work-order event version
```

For v1 retain the pre-change body-only
`HMAC-SHA256(secret, exact_request_body)` behavior and existing header/body
names. A timestamp header may remain for compatibility but is not part of the
v1 signature. For both versions, validate the frozen
`canonical_target_url` immediately before each redirect-disabled send. Reload
the endpoint through `(webhook_endpoint_id,event.company_id)`, require it to be
enabled and its current canonical URL/signature version to equal the frozen
values, and read only its current secret. Never substitute the endpoint's
current URL. Payload corruption, target/version drift, cross-company mismatch,
or a deleted endpoint makes no HTTP request and becomes a permanent
`DEAD_LETTER`; entitlement loss alone becomes `PAUSED_ENTITLEMENT`.

Run:

```bash
set -Eeuo pipefail
mvn -f components/cmms/api/pom.xml \
  -Dtest='WorkOrderStatusIdempotencyTest,WorkOrderStatusOutboxServiceTest,WebhookOutboxDispatchJobTest,WebhookOutboxAdminControllerTest,WebhookOutboxIntegrationTest,WorkOrderServiceTest' test
mvn -f components/cmms/api/pom.xml -DskipTests compile
```

Expected: focused tests and compilation pass.

- [ ] **Step 5: Require explicit confirmation for a CMMS delivery replay**

`POST /api/webhook-outbox/deliveries/{delivery_id}/replay-plan` accepts a
non-blank reason and new canonical correlation ID, derives the actor from the
authenticated user, and persists/returns the exact plan UUID,
event/delivery/endpoint IDs, safe error code, body/target hashes, generation,
attempt counters, reason, correlation ID, 30-minute expiry, and canonical plan
hash. `POST /api/webhook-outbox/deliveries/{delivery_id}/replay` accepts only
that plan UUID plus the exact confirmed hash; reason/correlation cannot be
changed at apply time.

Only a SETTINGS-authorized user from the delivery's company may plan or apply.
The durable plan includes expected `DEAD_LETTER` status, replay generation,
`attempt_in_generation`, lifetime attempts, frozen-target hash, payload hash,
signature version, safe error code, reason, actor, and new correlation ID.
Apply locks the company-scoped unexpired plan, revalidates every field, consumes
it once, and changes the same delivery from
`DEAD_LETTER` to `PENDING`, increments `replay_generation`, resets
`attempt_in_generation` to zero, retains `lifetime_attempt`, clears lease and
next-attempt metadata including `lease_token`, and inserts the immutable
replay-audit row in one
transaction. It does not create another event, change payload bytes/target,
change endpoint/signature version, or send synchronously. The next eight
failures dead-letter that new generation; replay is never automatic. The plan
expires after 30 minutes and can be applied once. The immutable plan row and
replay-audit row provide read-back evidence keyed by the confirmed hash.

Gate `WebhookOutboxQuartzConfig` with `cmms.webhook-outbox.dispatch.enabled`. Production defaults true; `application-test.yml` sets false, and job tests invoke the job directly with controlled clocks and leases.

### Task 4: Add API-Key Expiry Without Breaking Legacy Keys Abruptly

**Files:**

- Create: `components/cmms/api/src/main/resources/db/changelog/2026_07_29_00000000005_add_api_key_expiry.xml`
- Create: `components/cmms/api/src/main/java/com/grash/configuration/ClockConfiguration.java`
- Create: `components/cmms/api/src/test/java/com/grash/security/ApiKeyExpiryTest.java`
- Create: `components/cmms/api/src/test/java/com/grash/integration/ApiKeyExpiryMigrationTest.java`
- Modify: `components/cmms/api/src/main/java/com/grash/model/ApiKey.java`
- Modify: `components/cmms/api/src/main/java/com/grash/dto/apiKey/ApiKeyPostDTO.java`
- Modify: `components/cmms/api/src/main/java/com/grash/dto/apiKey/ApiKeyShowDTO.java`
- Modify: `components/cmms/api/src/main/java/com/grash/service/ApiKeyService.java`
- Modify: `components/cmms/api/src/main/java/com/grash/security/ApiKeyAuthFilter.java`
- Modify: `components/cmms/api/src/main/resources/db/master.xml`
- Modify: `components/cmms/api/src/main/resources/application.yml`

**Interfaces:**

- Produces: non-null `expires_at` for every existing and new API key
- Consumes: an injected application `Clock`

- [ ] **Step 1: Write creation, expiry, and legacy-migration tests**

Inject clock `2026-07-29T00:00:00Z`. Tests assert:

- new key without an explicit expiry defaults to `2026-10-27T00:00:00Z`;
- requested expiry must be after now and no more than 365 days ahead;
- expired key never authenticates;
- the migration gives every legacy key 90 days from migration execution and then makes the column non-null;
- API responses include expiry but never return a stored hash;
- `lastUsed` is not updated for a rejected key.

Run:

```bash
set -Eeuo pipefail
mvn -f components/cmms/api/pom.xml \
  -Dtest='ApiKeyExpiryTest,ApiKeyExpiryMigrationTest' test
```

Expected: FAIL before expiry exists.

- [ ] **Step 2: Implement the migration and clock-safe checks**

Add the nullable column, update every legacy row to `CURRENT_TIMESTAMP + INTERVAL '90 days'`, and then add the non-null constraint in the same migration. New keys default to 90 days and may request at most 365 days.

Provide a production `Clock.systemUTC()` bean in `ClockConfiguration` and override it with the fixed test clock. Do not compare dates in controllers. Return a stable unauthorized response without revealing whether a key was absent or expired.

`ApiKeyExpiryMigrationTest` uses the project's PostgreSQL/Testcontainers integration-test support, inserts a legacy key before applying migration `00005`, verifies the 90-day backfill and non-null constraint, rolls back, and reapplies. The unit test alone is not accepted as migration evidence.

- [ ] **Step 3: Run CMMS security and webhook regression**

Run:

```bash
set -Eeuo pipefail
mvn -f components/cmms/api/pom.xml \
  -Dtest='ApiKeyExpiryTest,ApiKeyExpiryMigrationTest,WebhookUrlValidatorTest,WebhookHttpClientTest,IntegrationCapabilitiesControllerTest,WebhookEndpointControllerTest,WebhookEndpointServiceTest,WorkOrderStatusIdempotencyTest,WorkOrderStatusOutboxServiceTest,WebhookOutboxDispatchJobTest,WebhookOutboxAdminControllerTest,WebhookOutboxIntegrationTest,WorkOrderServiceTest' test
mvn -f components/cmms/api/pom.xml -DskipTests compile
```

Expected: all focused tests and compilation pass.

- [ ] **Step 4: Commit and push CMMS hardening**

Run:

```bash
set -Eeuo pipefail
./scripts/doctor.sh
git -C components/cmms add \
  api/src/main/java/com/grash/configuration \
  api/src/main/java/com/grash/controller \
  api/src/main/java/com/grash/dto \
  api/src/main/java/com/grash/job \
  api/src/main/java/com/grash/model \
  api/src/main/java/com/grash/repository \
  api/src/main/java/com/grash/security \
  api/src/main/java/com/grash/service \
  api/src/main/java/com/grash/utils/WebhookUrlValidator.java \
  api/src/main/resources/application.yml \
  api/src/main/resources/db \
  api/src/test/java/com/grash
git -C components/cmms commit \
  -m "feat: deliver durable work order status events"
git -C components/cmms push
```

Expected: no secret, endpoint credential, or local environment file is staged.

### Task 5: Consume Signed Status into a Durable Inbox

**Files:**

- Create: `components/platform-integration/migrations/versions/0003_feedback_inbox.py`
- Create: `components/platform-integration/src/platform_integration/api/cmms_webhooks.py`
- Create: `components/platform-integration/src/platform_integration/auth/webhooks.py`
- Create: `components/platform-integration/src/platform_integration/models/feedback.py`
- Create: `components/platform-integration/src/platform_integration/repositories/inbox.py`
- Create: `components/platform-integration/src/platform_integration/repositories/feedback.py`
- Create: `components/platform-integration/src/platform_integration/services/feedback.py`
- Create: `components/platform-integration/src/platform_integration/workers/inbox.py`
- Create: `components/platform-integration/tests/api/test_cmms_webhooks.py`
- Create: `components/platform-integration/tests/auth/test_webhook_signatures.py`
- Create: `components/platform-integration/tests/integration/test_feedback_migrations.py`
- Create: `components/platform-integration/tests/services/test_feedback.py`
- Create: `components/platform-integration/tests/workers/test_inbox_worker.py`
- Modify: `components/platform-integration/src/platform_integration/app.py`
- Modify: `components/platform-integration/src/platform_integration/models/__init__.py`
- Modify: `components/platform-integration/src/platform_integration/models/bindings.py`
- Modify: `components/platform-integration/src/platform_integration/models/maintenance.py`
- Modify: `components/platform-integration/src/platform_integration/repositories/bindings.py`
- Modify: `components/platform-integration/src/platform_integration/repositories/maintenance_alerts.py`
- Modify: `components/platform-integration/src/platform_integration/services/alarm_projection.py`
- Modify: `components/platform-integration/src/platform_integration/cli.py`
- Modify: `components/platform-integration/tests/test_cli.py`

**Interfaces:**

- Produces: `POST /api/v1/webhooks/cmms/work-order-status`
- Produces: `WebhookVerifier.verify(headers, exact_body, now) -> VerifiedCmmsEvent`
- Produces: `FeedbackService.accept(event, source) -> FeedbackDisposition`
- Produces: `TenantBindingRepository.get_by_cmms_company_id(cmms_company_id: int) -> TenantBinding | None`
- Produces: `InboxWorker.run_once(now, owner) -> InboxSummary`
- Produces: `platform-integration inbox-worker [--once] --owner OWNER`
- Consumes: CMMS v2 signature and status-event contract

- [ ] **Step 1: Write signature, duplicate, and ordering tests**

Tests cover:

- committed cross-language v2 signature vector;
- wrong version, missing header, forged signature, old/future timestamp, body mutation, oversized body, and malformed JSON rejection;
- secret selected by `cmms_company_id` mapping and secret reference, not by a caller-provided tenant;
- unmapped/disabled company rejection;
- unmapped company and invalid signature return the same external authentication response;
- same `(tenant_id,source_system,event_id)`, event version, exact-body SHA-256,
  and normalized payload returns success without processing twice and only
  increments bounded duplicate metadata;
- the same event ID with a different signed body/version returns
  `409 EVENT_ID_PAYLOAD_CONFLICT`, records only a bounded security audit code, and
  never changes the original inbox row or feedback;
- even when a test digest function forces the same SHA-256 for two different
  normalized payloads, full normalized-payload comparison detects the conflict;
- inbox claim leases recover after expiry and cannot be owned by two workers;
- one `(tenant_id,work_order_id)` has exactly one feedback row;
- lower/equal `event_version` with a different event ID is recorded as stale and does not regress state;
- `OPEN`, `IN_PROGRESS`, and `ON_HOLD` map to `WORK_ORDER_OPEN`;
- `COMPLETE` maps to `WORK_ORDER_COMPLETE`;
- a current active suppressed risk returns to `PENDING_APPROVAL` only after the old work order becomes terminal;
- feedback does not clear risk and never calls PDM/training.
- `inbox-worker --once` drains all eligible rows present at cycle start up to a
  hard 1,000-claim cap, exits non-zero if eligible rows remain, and a
  long-running worker shuts down cleanly without abandoning its owned lease.

Run:

```bash
set -Eeuo pipefail
cd components/platform-integration
uv run pytest \
  tests/auth/test_webhook_signatures.py \
  tests/api/test_cmms_webhooks.py \
  tests/integration/test_feedback_migrations.py \
  tests/services/test_feedback.py \
  tests/workers/test_inbox_worker.py -v
```

Expected: FAIL before inbox and feedback support exist.

- [ ] **Step 2: Implement bounded verify-before-mutate handling**

Read at most 65,537 bytes and reject anything over 64 KiB. Parse only enough bounded JSON to obtain `cmms_company_id`, resolve its secret reference, verify the exact raw body and timestamp with `hmac.compare_digest`, cross-check event ID/version headers against the payload, then validate the full strict Pydantic payload.

Resolve `tenant_binding.cmms_webhook_secret_ref` as an exact environment-variable name. Its value is an absolute container secret-file path; read at most 4 KiB from a regular, non-symlink file owned/readable by the service, reject blank content, and never log the path or bytes. The isolated deployment sets the ref to `PILOT_CMMS_WEBHOOK_SECRET_FILE` and mounts `.runtime/secrets/cmms-webhook-v2` read-only at `/run/secrets/cmms-webhook-v2`.

Migration `0003_feedback_inbox` adds `cmms_status`, `cmms_event_version`, and `cmms_status_updated_at` to `maintenance_alert`, and creates:

```text
integration_inbox:
  tenant_id, source_system, event_id, event_version, payload_sha256
  normalized_payload_json, status, attempt
  lease_owner, lease_expires_at, next_attempt_at
  received_at, processed_at, duplicate_count, last_duplicate_at, safe_error_code
  UNIQUE (tenant_id, source_system, event_id)

maintenance_feedback:
  tenant_id, alert_id, work_order_id
  source_system, source_event_id, source_event_version
  completed_at, result_code, completion_projection_sha256
  UNIQUE (tenant_id, work_order_id)

work_order_reconciliation:
  tenant_id, alert_id, work_order_id, poll_slot
  status, attempt, lease_owner, lease_expires_at, next_attempt_at
  UNIQUE (tenant_id, work_order_id, poll_slot)

replay_audit:
  tenant_id, dead_letter_event_id, actor, reason
  original_correlation_id, new_correlation_id
  confirmed_plan_hash, result_code, created_at
```

All foreign keys, repository methods, claims, and indexes retain `tenant_id`. `normalized_payload_json` stores only the strict validated event fields needed by the worker, never raw headers or a second unverified body.

The unique-key conflict path locks and reads the existing inbox metadata:
matching `payload_sha256`, event version, and complete
`normalized_payload_json` is an idempotent duplicate; any difference is a
security collision. It never overwrites the first digest/payload/status or
acknowledges a collision as a duplicate. `payload_sha256` is constrained to
lowercase 64-hex but is not unique and cannot identify another event.
Webhook rows hash the verified exact raw body; polling rows hash the RFC 8785
normalized observation bytes.

The HTTP handler performs only:

1. bounded body read;
2. mapping and secret resolution;
3. header/body cross-check and signature verification;
4. strict payload validation;
5. idempotent inbox insertion with status `RECEIVED`;
6. `202 ACCEPTED` or a duplicate acknowledgement.

It never calls CMMS or ThingsBoard. A lease-backed inbox worker uses three phases:

1. in transaction A, claim the inbox row, set a bounded lease/attempt, and commit;
2. outside every database transaction and lease-locking statement, fetch the company-scoped CMMS work order only when completion enrichment is needed;
3. in transaction B, re-lock/reload the inbox row, verify owner/lease and source version, then lock risk state → Alert in the global order, reject stale versions, update CMMS status/version, upsert feedback, update the Alarm projection/aggregate version, insert its desired-state outbox event, and mark the inbox `PROCESSED` or `STALE`.

If enrichment fails, transaction B records only bounded retry metadata; no partial feedback or Alarm projection is applied. Store no work-order description, attachment, raw secret, or full webhook request headers.

Add the exact CLI role:

```text
platform-integration inbox-worker [--once] --owner OWNER
```

`--owner` is required to be a stable non-secret deployment identity. Without
`--once` it polls with bounded backoff and handles `SIGTERM`; with `--once` it
drains eligible rows under the fixed 1,000-claim cap.
Inbox transitions are exactly
`RECEIVED → IN_FLIGHT → PROCESSED|STALE|RETRY_WAIT|DEAD_LETTER`; a dead-letter
row is not automatically reclaimed.

- [ ] **Step 3: Hash a bounded completion projection**

For `COMPLETE`, the inbox worker fetches the company-scoped CMMS work order and hashes RFC 8785 bytes containing only:

```text
work_order_id
external_ref
equipment_id
status
completed_on
work_order_category_id
```

CMMS returns `category` as an object; obtain `work_order_category_id` from `category.id` after strict response validation. Save the hash and normalized result code `COMPLETE`; do not copy free text or attachments into the integration database.

Run:

```bash
set -Eeuo pipefail
uv run --directory components/platform-integration --frozen pytest \
  tests/auth/test_webhook_signatures.py \
  tests/api/test_cmms_webhooks.py \
  tests/integration/test_feedback_migrations.py \
  tests/services/test_feedback.py \
  tests/workers/test_inbox_worker.py -v
```

Expected: all tests pass. The Testcontainers migration test alone receives its temporary PostgreSQL URL and performs `upgrade head → downgrade 0002 → upgrade head`; never run a bare Alembic command against an ambient `DATABASE_URL`.

### Task 6: Add Polling Reconciliation, Capability Preflight, and Recovery Controls

**Files:**

- Create: `components/platform-integration/src/platform_integration/services/capabilities.py`
- Create: `components/platform-integration/src/platform_integration/services/reconciliation.py`
- Create: `components/platform-integration/src/platform_integration/services/replay.py`
- Create: `components/platform-integration/src/platform_integration/workers/reconciliation.py`
- Create: `components/platform-integration/src/platform_integration/commands/replay.py`
- Create: `components/platform-integration/src/platform_integration/repositories/reconciliation.py`
- Create: `components/platform-integration/src/platform_integration/repositories/replay_plan.py`
- Create: `components/platform-integration/src/platform_integration/repositories/replay_audit.py`
- Create: `components/platform-integration/migrations/versions/0004_replay_controls.py`
- Create: `components/platform-integration/tests/services/test_capabilities.py`
- Create: `components/platform-integration/tests/services/test_reconciliation.py`
- Create: `components/platform-integration/tests/services/test_replay.py`
- Create: `components/platform-integration/tests/integration/test_replay_migrations.py`
- Create: `components/platform-integration/tests/workers/test_reconciliation_worker.py`
- Modify: `components/platform-integration/src/platform_integration/clients/cmms.py`
- Modify: `components/platform-integration/src/platform_integration/contracts/cmms.py`
- Modify: `components/platform-integration/src/platform_integration/credentials.py`
- Modify: `components/platform-integration/src/platform_integration/models/feedback.py`
- Modify: `components/platform-integration/src/platform_integration/workers/work_orders.py`
- Modify: `components/platform-integration/src/platform_integration/cli.py`
- Modify: `components/platform-integration/src/platform_integration/config.py`
- Modify: `components/platform-integration/tests/workers/test_work_order_outbox_worker.py`

**Interfaces:**

- Produces: `CapabilityService.preflight(tenant_binding) -> CapabilityStatus`
- Produces: `ReconciliationWorker.run_once(now, owner) -> ReconciliationSummary`
- Produces: `platform-integration reconciliation-worker [--once] [--now RFC3339] --owner OWNER`
- Produces: `platform-integration replay-plan`, `replay-apply`, and read-only
  `replay-verify`
- Extends: `platform-integration work-order-worker --once --event-id UUID --now RFC3339 --owner OWNER` for exact-event isolated acceptance only
- Produces: read-only `platform-integration work-order-worker-state --tenant-alias STRING --format json`
- Consumes: CMMS capabilities and five-minute work-order polling

- [ ] **Step 1: Write polling and degraded-mode tests**

Tests assert:

- every non-terminal PDM work order is polled at least once per five-minute bucket;
- unique poll slot and lease prevent duplicate reconciliation;
- webhook loss is repaired from work-order query;
- webhook then poll and poll then webhook converge to the highest event version;
- polling emits a deterministic inbox source identity and cannot create two feedback rows;
- expired poll leases recover, while unique `(tenant_id,work_order_id,poll_slot)` prevents a second slot row;
- `webhook_delivery=false` plus working API is degraded but ready for polling;
- a configured `cmms_bearer` envelope works when API-key access is unavailable;
- neither API key nor Bearer capability is unready;
- capability response with no external-ref/event-version support is unready;
- tokens and capability response internals are absent from logs.
- replay plan/apply lookup always uses `(tenant_id,event_id)`, includes the
  derived CMMS company in the hash, and rejects a disabled/drifted binding;
- a durable replay plan survives process/container restart, expires in 30
  minutes, is consumed once, and cannot change actor, reason, or new
  correlation at apply;
- `reconciliation-worker --once` processes the exact due five-minute slot and
  drains at most 1,000 eligible rows; `--now` is accepted only in isolated
  pilot mode together with `--once`, and long-running mode has clean `SIGTERM`
  lease handling;
- replay generation resets its per-generation retry budget while lifetime
  attempts and every audit generation remain monotonic;
- replaying an absent reconciled CMMS command transitions
  `WORK_ORDER_CREATE_FAILED → APPROVED`, after which the worker's first claim
  performs the existing `APPROVED → WORK_ORDER_CREATING`; both transitions
  increment Alert/projection versions. A matching external-ref hit transitions
  directly to `WORK_ORDER_OPEN`; identity mismatch remains terminal.
- finalization requires the current lease token and replay generation, so a
  timed-out response from an older generation cannot overwrite new state.
- work-order `--event-id`/`--now` requires `--once`, a canonical UUID, a unique
  owner, and `PLATFORM_INTEGRATION_ISOLATED_PILOT_MODE=1`; it rejects normal
  deployments, a non-eligible event, a time before the row's retry boundary,
  and any claim of another event. Long-running workers expose neither option.
- `work-order-worker-state` is tenant-scoped, read-only, and emits bounded
  canonical JSON with eligible/leased/dead-letter counts plus non-secret event
  IDs, lease expiries, generations, and payload/target hashes; tests reject
  cross-tenant rows and any credential or CMMS body field.

Run:

```bash
set -Eeuo pipefail
cd components/platform-integration
uv run pytest \
  tests/services/test_capabilities.py \
  tests/services/test_reconciliation.py \
  tests/workers/test_reconciliation_worker.py -v
```

Expected: FAIL before preflight and polling exist.

- [ ] **Step 2: Implement deterministic polling events**

Poll:

```text
GET /api/work-orders/by-external-ref?source=PDM_FORECAST&ref={alert_id}
```

Build the polling observation digest from:

```python
poll_projection = {
    "cmms_company_id": cmms_company_id,
    "work_order_id": work_order_id,
    "event_version": event_version,
    "status": status,
    "updated_at": updated_at,
}
name = sha256(rfc8785.dumps(poll_projection)).hexdigest()
event_id = uuid5(UUID("8f0e0569-e702-555b-997e-de18d9e2851a"), name)
```

The namespace is the fixed UUIDv5 for `urn:ifactory:cmms-poll:v1`. Pass the normalized event through `FeedbackService.accept()` so webhook and polling share state/version logic.

The Phase 2 credential provider continues direct env-reference lookup. CMMS accepts exactly `{"kind":"cmms_api_key","value":"..."}` or `{"kind":"cmms_bearer","value":"..."}`; the latter is a pre-issued, externally rotated least-privilege service token sent as standard `Authorization: Bearer`. This phase does not store a username/password or invent a token-acquisition endpoint. PDM and ThingsBoard credential kinds remain unchanged.

Add the exact CLI role:

```text
platform-integration reconciliation-worker \
  [--once] [--now RFC3339] --owner OWNER
```

Its once/continuous bounds and isolated-clock rule match the tests above.
Each reconciliation slot transitions
`PENDING → IN_FLIGHT → OBSERVED|RETRY_WAIT|DEAD_LETTER`; a dead-letter old slot
does not prevent creation of the next five-minute slot.

- [ ] **Step 3: Add explicit dead-letter replay plan/apply**

`replay-plan` accepts one exact tenant alias plus local dead-letter event ID,
performs tenant/company-scoped external reconciliation, persists an immutable
secret-free plan, and prints tenant, bound CMMS company, aggregate, target
system, original/new correlation IDs, last safe error code, actor, reason,
expiry, and plan hash. Its exact CLI shape is:

```text
platform-integration replay-plan \
  --tenant-alias STRING --event-id UUID \
  --actor STRING --reason STRING \
  --new-correlation-id UUID

platform-integration replay-apply \
  --tenant-alias STRING \
  --event-id UUID --plan-hash SHA256 --confirmed-hash SHA256 \
  --actor STRING

platform-integration replay-verify \
  --tenant-alias STRING --event-id UUID --plan-hash SHA256 \
  [--format json]
```

Without `--format`, verify prints a bounded human summary. JSON mode emits the
canonical receipt object used by the final gate.

Migration `0004_replay_controls` replaces the old single outbox attempt counter
with `replay_generation`, `attempt_in_generation`, `lifetime_attempt`, and a
fresh UUID `lease_token`, backfilling existing attempts into both attempt
counters with generation zero. Its Testcontainers test performs
`upgrade 0003 → upgrade 0004 → downgrade 0003 → upgrade 0004`. Every actual
delivery failure increments the latter two counters; the eight-attempt limit
applies to `attempt_in_generation`. Claim/finalize always bind the lease token
and replay generation.

The same migration creates `replay_plan` with tenant ID, bound CMMS company,
event ID, canonical plan JSON/hash, actor, reason, original/new correlation
IDs, expected generation/attempt/payload/target hashes, created/expiry times,
and nullable consumed time/result. Unique `(tenant_id,event_id,plan_hash)` and
a row lock make apply single-use. Plan/apply rederive the enabled tenant
binding and require the same company before any mutation.

`replay-apply` requires the exact confirmed hash from a later user turn,
requires the CLI actor to equal the durable plan actor, and loads
actor/reason/new correlation from that plan, then inserts a new
immutable `replay_audit` row with the
actor/reason/correlation/generation/result. After a fresh external-reference
reconciliation and under the global risk-state → Alert → projection/outbox
lock order:

- a matching existing CMMS work order marks the immutable command delivered,
  transitions `WORK_ORDER_CREATE_FAILED → WORK_ORDER_OPEN`, stores the work
  order identity, increments Alert/projection versions once, and emits the
  current Alarm desired state;
- an absent work order increments `replay_generation`, resets
  `attempt_in_generation=0`, retains `lifetime_attempt`, clears lease/retry
  metadata, changes the command to `PENDING`, transitions
  `WORK_ORDER_CREATE_FAILED → APPROVED`, increments
  Alert/projection versions once, and emits the current Alarm desired state;
- the next worker claim performs `APPROVED → WORK_ORDER_CREATING` with the
  existing second version/projection update before any external call;
- an identity mismatch, payload/hash corruption, or target drift remains
  `DEAD_LETTER` and records a terminal audited conflict without consuming a
  replay generation; these permanent errors cannot be replayed even with a
  confirmed hash.

It never edits immutable payload or approval identity and never supports a
wildcard or batch target. A later eight failures applies only to the new
generation and returns the Alert to `WORK_ORDER_CREATE_FAILED`.

- [ ] **Step 4: Verify and commit feedback/recovery**

Run:

```bash
set -Eeuo pipefail
uv run --directory components/platform-integration --frozen pytest \
  tests/auth/test_webhook_signatures.py \
  tests/api/test_cmms_webhooks.py \
  tests/integration/test_replay_migrations.py \
  tests/services/test_feedback.py \
  tests/services/test_capabilities.py \
  tests/services/test_reconciliation.py \
  tests/services/test_replay.py \
  tests/workers/test_inbox_worker.py \
  tests/workers/test_reconciliation_worker.py \
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
git -C components/platform-integration add migrations src tests
git -C components/platform-integration commit \
  -m "feat: reconcile predictive maintenance feedback"
git -C components/platform-integration push
```

### Task 7: Add Metrics, Dependency Readiness, and Redaction Tests

**Files:**

- Create: `components/platform-integration/src/platform_integration/observability/metrics.py`
- Create: `components/platform-integration/src/platform_integration/observability/redaction.py`
- Create: `components/platform-integration/src/platform_integration/api/readiness.py`
- Create: `components/platform-integration/src/platform_integration/api/dependency_health.py`
- Create: `components/platform-integration/tests/observability/test_metrics.py`
- Create: `components/platform-integration/tests/observability/test_redaction.py`
- Create: `components/platform-integration/tests/api/test_readiness.py`
- Create: `components/platform-integration/tests/api/test_dependency_health.py`
- Modify: `components/platform-integration/src/platform_integration/app.py`
- Modify: `components/platform-integration/src/platform_integration/config.py`
- Modify: `components/platform-integration/pyproject.toml`
- Modify: `components/platform-integration/uv.lock`

**Interfaces:**

- Produces: `GET /readyz`
- Produces: internal `GET /dependency-health`
- Produces: internal `GET /metrics`
- Consumes: process, database, PDM, ThingsBoard, CMMS, fixture, backlog, and capability state

- [ ] **Step 1: Write readiness and bounded-cardinality tests**

Assert:

- `/healthz` remains process-only;
- `/readyz` reports local/static gates with stable codes and no URL/credential;
- missing database/migration, invalid PDM fixture/config digest, missing enabled tenant mapping, or missing/malformed required credential envelopes makes readiness false;
- temporary ThingsBoard, PDM, or CMMS reachability failure leaves `/readyz` process-ready and appears as unhealthy only in `/dependency-health`;
- `/dependency-health` reports each upstream/capability with timestamp, stable safe code, and no URL/credential;
- polling-only mode is ready with overall `degraded`;
- outbox/inbox backlog age and dead-letter count are exposed;
- metric labels use service, operation, event type, status, dependency, and safe error code only;
- equipment, Alert, correlation, event, and work-order IDs never become metric labels;
- redaction removes `Authorization`, `X-Authorization`, API keys, secrets, raw telemetry, full forecast, CMMS description, attachments, stack trace, and absolute path.

Run:

```bash
set -Eeuo pipefail
uv run --directory components/platform-integration --frozen pytest \
  tests/observability/test_metrics.py \
  tests/observability/test_redaction.py \
  tests/api/test_readiness.py \
  tests/api/test_dependency_health.py -v
```

Expected: FAIL before observability support exists.

- [ ] **Step 2: Implement health semantics and metrics**

`/readyz` answers whether this process can safely take work given its local schema, fixture/config integrity, tenant bindings, and credential-reference shape. It never performs a synchronous upstream probe. `/dependency-health` exposes cached bounded probes for ThingsBoard, PDM, CMMS API/capability, webhook entitlement, and backlog/dead-letter state. A remote timeout or 5xx changes dependency health and metrics, not liveness/readiness; a locally missing credential reference or invalid fixture digest remains a readiness failure.

Expose:

```text
prediction runs by terminal status
prediction latency
quality skip count
active risk count
outbox delivery/retry/dead-letter count and oldest age
webhook accepted/duplicate/stale/rejected/collision count
polling discrepancy count
Action result count
```

Protect `/metrics` with the deployment's internal network; do not expose it through the browser gateway route.

- [ ] **Step 3: Verify the whole integration component**

Run:

```bash
set -Eeuo pipefail
uv run --directory components/platform-integration --frozen pytest tests -v
uv run --directory components/platform-integration --frozen ruff check .
uv run --directory components/platform-integration --frozen ruff format --check .
uv sync --directory components/platform-integration --frozen
```

Expected: all component tests/checks pass.

Commit:

```bash
set -Eeuo pipefail
./scripts/doctor.sh
git -C components/platform-integration add pyproject.toml uv.lock src tests
git -C components/platform-integration commit \
  -m "feat: expose integration reliability health"
git -C components/platform-integration push
```

### Task 8: Run Full Failure-Injection and 20-Device Acceptance

**Files:**

- Create: `tests/e2e/test_feedback_reconciliation.py`
- Create: `tests/e2e/test_restart_recovery.py`
- Create: `tests/e2e/test_failure_injection.py`
- Create: `tests/e2e/test_twenty_device_acceptance.py`
- Create: `tests/e2e/support/cmms_admin.py`
- Modify: `tests/e2e/conftest.py`
- Modify: `tests/e2e/support/failure_proxy.py`
- Modify: `tests/e2e/support/pilot_writes.py`
- Modify: `tests/e2e/test_pilot_writes.py`
- Modify: `deploy/gateway/predictive-maintenance.conf`
- Modify: `deploy/compose/predictive-maintenance-shadow.yml`
- Create: `deploy/compose/predictive-maintenance-webhook.yml`
- Modify: `deploy/compose/predictive-maintenance-shadow.env.example`
- Modify: `deploy/README.md`
- Modify: `tests/README.md`
- Modify: `.github/workflows/workspace-check.yml`
- Modify: `components/cmms` gitlink
- Modify: `components/platform-integration` gitlink

**Interfaces:**

- Produces: complete isolated pilot acceptance evidence
- Produces: separately confirmed `phase4-risk-preparation`,
  `phase4-status-feedback`, `phase4-replay-target`, and
  `phase4-risk-recovery` plan/apply receipts
- Produces: one later separately confirmed exact-event replay receipt
- Produces: read-only `pilot_writes receipt-field --env-file ... --receipt ... --recovery-receipt ... --field dead_letter_event_id`
- Consumes: all four phase contracts and component implementations

- [ ] **Step 1: Write the final acceptance scenarios**

The live pytest nodes are prepare/read-only-verify tests; they may query state
and inspect receipts, but their HTTP transport rejects every mutating method
and they never restart a service. Extend the Phase 3 `pilot_writes` helper so
the only Phase 4 business mutations occur in four independently confirmed
applies—`phase4-risk-preparation`, `phase4-status-feedback`, and
`phase4-replay-target`, then `phase4-risk-recovery`—followed by the separately
confirmed exact-event replay. Cover:

1. all 20 reversible mappings and six verified fixture hashes;
2. one accelerated prediction round for all 20 within a 15-minute slot budget;
3. no Alarm below threshold;
4. two risky rounds produce exactly one Alarm for each prepared
   device/episode and never duplicate an episode;
5. ACK produces no work order;
6. the Phase 3 duplicate/concurrent approval plus CMMS response-loss case has
   produced exactly its one work order;
7. `OPEN → IN_PROGRESS → ON_HOLD → COMPLETE` reaches ThingsBoard in order;
8. duplicate and out-of-order webhook cannot regress state;
9. forged/stale webhook is rejected;
10. dropped webhook is repaired by five-minute polling;
11. old Alarm clear cannot clear a newer episode;
12. the approved immutable command and its later dead-letter state survive risk
    recovery and restart without payload/version drift;
13. worker restart recovers expired prediction/outbox/reconciliation leases;
14. one dedicated replay-target Action creates an immutable command, the first
    CMMS create succeeds behind a lost response, eight bounded attempts
    dead-letter the unresolved command once, and no duplicate work order is
    created;
15. a separately confirmed single-event replay reconciles the already-created
    second work order, is audited, issues no duplicate POST, and leaves exactly
    two pilot work orders in total;
16. two healthy rounds clear risk while work-order completion alone does not;
17. no training API or MCP runtime path is called.

The `phase4-risk-preparation` plan freezes the exact Phase 3 receipt hash,
isolated tenant/company, every bounded risky-telemetry target and body hash,
the existing Phase 3 Alert identity, and a second exact device plus stable
risk key reserved for replay acceptance. The second Alert does not exist at
plan time: its expected type/origin/result are frozen, while apply GET-verifies
and records the materialized Alert UUID/version in the receipt. Apply may run
only the prediction and Alarm workers needed to materialize and retain those
risky states. It performs no healthy recovery round, work-order Action, CMMS
status write, callback administration, or replay.

The independent `phase4-status-feedback` plan consumes the risk-preparation
receipt and freezes the existing Phase 3 work order/Alert/equipment identities,
current CMMS status/event version, the three ordered CMMS transitions
`OPEN → IN_PROGRESS → ON_HOLD → COMPLETE`, signed
duplicate/out-of-order/forged webhook probes, bounded status-delivery fault
modes, ephemeral inbox/reconciliation worker cycles, and service restarts.
Each status operation is the exact
`PATCH /api/work-orders/{id}/change-status` body for `IN_PROGRESS`,
`ON_HOLD`, or `COMPLETE`, plus its unique `Idempotency-Key`, canonical
`X-Correlation-ID`, expected predecessor `X-Expected-Event-Version`, and the
derived confirmed-plan-hash header. The canonical plan omits any self-hash
field; after hashing, apply sets `X-Confirmed-Plan-SHA256` to that result, so
there is no circular digest. This hash authorizes only these three ordered
writes to this one work order plus their explicitly enumerated delivery probes
and worker/restart controls; it authorizes no telemetry, Action, other work
order, callback administration, or replay.

Apply requires the later user-confirmed plan hash, revalidates all identities
and predecessor versions before the first mutation, then before each status
transition. On response loss it GETs the company-scoped work order and event
version; a same-key retry is permitted only through CMMS's integration
idempotency record and it never issues a new key blindly. A rerun may resume only exact
already-achieved plan ordinals from a mode-`0600` receipt and aborts on any
other drift. The receipt stores non-secret IDs, hashes, versions, result codes,
and worker summaries.

The third `phase4-replay-target` plan consumes the risk-preparation receipt and
freezes exactly one `CREATE_WORK_ORDER` Action against the reserved second
Alert. It also freezes the exact Compose project/service/image identity and
running/stopped pre-state of the normal `work-order-worker`, a zero-active-lease
and zero-other-eligible-command state hash from `work-order-worker-state`, and
one narrow failure-proxy script. Apply quiesces only that exact normal worker
before issuing the Action and rechecks zero active leases. The proxy passes the
first external-reference GET as absent; the first create then reaches CMMS and
commits, its response is dropped, and the next seven attempts can run only the
mandatory external-reference reconciliation, which returns the planned
retryable failure before any POST. After attempt eight, apply restores the
proxy, performs a host-side GET to prove exactly one matching CMMS work order
exists, leaves the immutable integration command in
`DEAD_LETTER`. It performs no telemetry, status transition, callback
administration, or replay. Its mode-`0600` receipt binds the isolated
tenant/company, Alert/action version, immutable command event ID, external
reference, payload/target hashes, eight attempts in generation zero, the
current `WORK_ORDER_CREATE_FAILED` maintenance state/version, and the single
matching CMMS work-order ID. This makes the later integration replay target
both reachable and duplicate-safe.

The fourth `phase4-risk-recovery` plan consumes the status and replay-target
receipts and freezes only the healthy telemetry body hashes and prediction /
Alarm-worker cycles needed after `COMPLETE` and after the second immutable
command has dead-lettered. It proves completion alone did not clear predictive
risk, then clears the first device's risk after two healthy rounds and clears
the second device's predictive risk without deleting, replacing, or reopening
its immutable command. It performs no Action, CMMS status write, callback
administration, proxy change, or replay.

`tests/e2e/test_pilot_writes.py` must prove that the four scenario schemas are
disjoint, that each apply rejects another scenario's hash or receipt, that the
replay target is not the Phase 3 Alert/device, and that the proxy script permits
one initial upstream external-reference GET returning absent, exactly one
upstream POST with a dropped response, and seven GET-only failed
reconciliations. It also tests mandatory proxy restoration on every exception,
rejects a replay-target receipt unless the integration event is still
generation-zero `DEAD_LETTER`, the bound Alert is exactly
`WORK_ORDER_CREATE_FAILED` at the expected version, and exactly one CMMS work
order matches the external reference, and proves risk recovery cannot mutate
that command. Compose/helper tests also require replay-target apply to stop
only the plan-bound normal worker, reject any other active lease or eligible
command, and restore the exact pre-plan running/stopped state on both success
and every failure path.
`receipt-field` tests reject a missing/wrong env file, tenant/company drift,
an absent or unrelated recovery receipt, any command/event/hash/work-order
change, or an Alert change not exactly proven by that recovery receipt; its
only stdout on success is the canonical UUID.

Extend the Phase 3 fault helper with a `serve-replay-target` role and define a
`cmms-failure-proxy` service behind the Compose profile `fault-injection`.
It reuses the non-root integration image only as a Python runtime, mounts the
helper and exact `phase4-replay-target.json` read-only, joins only the private
integration network, publishes no port, has a read-only filesystem, drops all
capabilities, and receives no database, CMMS, ThingsBoard, PDM, SETTINGS, or
webhook-secret environment. On Linux it has the explicit
`host.docker.internal:host-gateway` extra-host mapping. Its sole upstream is fixed to
`http://host.docker.internal:3000`; it forwards the worker's authorization
header in memory but never logs or persists headers/bodies. It starts only
after validating the plan and later-confirmed hash passed by apply, allows the
first exact external-reference GET upstream and requires its validated result
to be absent, allows the one exact planned POST upstream and drops that
response, then returns the planned retryable status to the next seven exact
external-reference GETs and rejects every other method/path/body. Its final
summary must bind eight GETs total—one verified absent plus seven planned
failures—one POST, and the corresponding request digests. The work-order
client's pre-write reconciliation budget is exactly one HTTP request per
outbox attempt; the 503 response is normalized into one outer retry and is
never retried inside the client.

The Compose contract test asserts the private-only network, no published
ports, the exact extra-host mapping/upstream, security options, read-only
mounts, empty credential environment, and absence from every default or
`continuous` profile render.

Replay-target apply starts that service, runs each ephemeral work-order worker
with only
`PLATFORM_INTEGRATION_CMMS_BASE_URL=http://cmms-failure-proxy:8080`, checks the
proxy's non-secret count/digest summary, and removes the exact proxy container
in its mandatory cleanup path. Before starting the proxy it uses a bounded
graceful Compose stop for the exact plan-bound normal `work-order-worker`,
waits until `work-order-worker-state` reports no active lease, and refuses to
proceed if any unplanned eligible command exists. Its outermost finally block
removes the proxy and restores that service to exactly its frozen pre-plan
running/stopped state, then verifies the restored process state and container
image identity. Normal and continuous workers retain
`http://host.docker.internal:3000`; no tracked or untracked shared env file is
rewritten to redirect them.

- [ ] **Step 2: Route and explicitly configure the feedback callback**

Add an exact gateway route for `/api/v1/webhooks/cmms/work-order-status`;
leave integration admin, metrics, replay, and provisioning paths inaccessible
from the public gateway. The one canonical container-reachable callback is:

```text
http://predictive-maintenance-gateway/api/v1/webhooks/cmms/work-order-status
```

Its exact UTF-8 SHA-256 is
`dfa0bdb45a122f110202001d041356d83018349a6c5f8bf281d9552d68aba31c`.
Before this plan starts, the separately authorized isolated CMMS operator must
have configured that exact value in `CMMS_WEBHOOK_TRUSTED_CALLBACK_BASES`,
attached only the CMMS API container to the existing external Docker network
`ifactory-pilot-shared`, rebuilt/restarted CMMS from the exact Phase 4
component commit, and provided read-only evidence through
`GET /api/integration-capabilities` that
`trusted_callback_base_configured=true` with that hash. This implementation
plan never edits an untracked CMMS deployment env or restarts an unowned CMMS.
If this prerequisite is absent, select the documented polling-only branch and
perform no endpoint or secret write.

Add non-secret `PILOT_WEBHOOK_CALLBACK_URL` and
`PILOT_CMMS_HOST_BASE_URL=http://127.0.0.1:3000` to the isolated untracked
environment and empty assignments to its tracked example. The callback helper
uses only the host base for its CMMS requests; it never tries to connect from
the host to `host.docker.internal`. It rejects any callback URL, company,
query/fragment, path, or hash variant.

The untracked host env also contains a separate short-lived
`PILOT_CMMS_SETTINGS_CREDENTIAL` bearer envelope. Only `cmms_admin.py` may
resolve it, and it must call `/api/auth/me` first and require the returned
company to equal the discovered exact company. No Compose service receives
that variable or a reference to it; the long-lived
`PILOT_CMMS_CREDENTIAL` remains least privilege and cannot administer webhook
endpoints.

Extend the main Compose file with `inbox-worker` and
`reconciliation-worker` services behind the `continuous` profile. Both run as
the non-root integration image with unique fixed owners:

```text
platform-integration inbox-worker --owner inbox-worker-1
platform-integration reconciliation-worker --owner reconciliation-worker-1
```

Their startup chain is PostgreSQL healthy → `integration-migrate` completed
successfully → worker start. Neither has a Compose dependency on remote
CMMS/ThingsBoard health. Before a reconciliation claim, the worker checks the
cached CMMS gate and records a bounded retry when unavailable; a remote outage
never kills the process. Neither exposes a port.

Both `reconciliation-worker` and `inbox-worker` explicitly receive only
`PLATFORM_INTEGRATION_CMMS_CREDENTIAL_REF` and
`PILOT_CMMS_CREDENTIAL: ${PILOT_CMMS_CREDENTIAL:?required}`.
The inbox worker uses that role-minimal credential only for the
company-scoped `COMPLETE` enrichment GET; it has no CMMS mutation path.
Deterministic acceptance does not enable the continuous profile. It invokes
ephemeral `--once` instances with unique `e2e-*` owners after each relevant
webhook/status step and requires zero exit. Compose contract tests prove both
services have exactly their required database/runtime-CMMS-credential
environment but no SETTINGS credential, ThingsBoard credential, webhook
secret mount, `PILOT_CMMS_WEBHOOK_SECRET_FILE`, or resolution of
`PLATFORM_INTEGRATION_CMMS_WEBHOOK_SECRET_REF`.

`tests/e2e/support/cmms_admin.py` builds a canonical plan containing the
isolated company ID, exact callback URL, event `WORK_ORDER_STATUS_CHANGE`,
signature version `v2`, endpoint code `IFACTORY_PDM_STATUS_V1`, current
endpoint ID/generation or expected absence, the exact operation
`CREATE_ENDPOINT` or `ROTATE_SECRET`, SETTINGS actor, and 30-minute expiry.
Display its SHA-256 and wait for explicit confirmation in a later turn before
calling a mutating CMMS endpoint.

The helper first checks the separate capability primitives. Missing webhook
entitlement/plan feature, v2 support, or exact trusted-base configuration
selects `POLLING_ONLY`; endpoint readiness by itself does not. That no-op mode
performs no endpoint/secret write and proceeds to the degraded verification
branch below.

For webhook mode, list endpoints with the SETTINGS credential and reuse only
an exact company/code/URL/event/version match. If absent, the plan binds the
natural key and expected absence and authorizes exactly one create. A
create-response loss is reconciled by list: if the exact endpoint now exists
but its one-time secret was lost, apply stops with
`UNCERTAIN_SECRET_REQUIRES_NEW_PLAN`; it never rotates under the absent-state
plan. The next read-only plan binds that exact endpoint ID/current generation
and proposes one separately confirmed rotation.

If the endpoint already exists, a local secret is reusable only when the
mode-`0600` metadata sidecar binds the same endpoint ID, remote
`secret_generation`, callback hash, signature version, and secret SHA-256.
Missing or mismatched metadata produces an exact rotation plan; generation
drift before apply aborts with zero writes. Create/rotate apply stores the
one-time secret at `.runtime/secrets/cmms-webhook-v2`, metadata at
`.runtime/secrets/cmms-webhook-v2.metadata.json`, and a non-secret result at
`.runtime/receipts/cmms-webhook-v2.json`, all mode `0600`. The receipt binds
plan hash, endpoint ID/generation, company, operation, actor, and result hash.
The integration database stores only the credential reference. Never print,
commit, or include secret bytes in an artifact.

After all Phase 4 component commits are pushed and the separately managed CMMS
has been rebuilt from its exact commit, build the current integration image,
rerun migrations `0003` and `0004`, and recreate the non-secret roles before
callback planning:

```bash
set -Eeuo pipefail
docker compose \
  --env-file .runtime/predictive-maintenance-shadow.env \
  -f deploy/compose/predictive-maintenance-shadow.yml \
  build integration-migrate integration-api \
  inbox-worker reconciliation-worker \
  alarm-outbox-worker work-order-worker prediction-worker scheduler
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
curl --fail --silent http://127.0.0.1:18080/healthz
callback_probe_status="$(
  docker run --rm --network ifactory-pilot-shared \
    curlimages/curl:8.14.1 \
    --silent --output /dev/null --write-out '%{http_code}' \
    --request POST \
    --header 'Content-Type: application/json' \
    --data '{}' \
    http://predictive-maintenance-gateway/api/v1/webhooks/cmms/work-order-status
)"
test "$callback_probe_status" = 401
```

Image-label and migration checks must match the pushed Phase 4 integration SHA
and Alembic head `0004`; a Phase 3 image or schema aborts the gate. The final
unsigned probe must resolve through the shared network and be rejected by the
fresh integration route before inbox insertion.

Use these exact separate-turn commands:

```bash
set -Eeuo pipefail
install -d -m 0700 .runtime/plans .runtime/receipts .runtime/secrets
test "$(realpath .runtime/plans)" = \
  "$(realpath .)/.runtime/plans"
test "$(realpath .runtime/receipts)" = \
  "$(realpath .)/.runtime/receipts"
test "$(realpath .runtime/secrets)" = \
  "$(realpath .)/.runtime/secrets"
uv run --directory tests --frozen python -m e2e.support.cmms_admin plan \
  --env-file ../.runtime/predictive-maintenance-shadow.env \
  --host-base-url-env PILOT_CMMS_HOST_BASE_URL \
  --settings-credential-env PILOT_CMMS_SETTINGS_CREDENTIAL \
  --actor codex-isolated-pilot-operator \
  --output ../.runtime/plans/cmms-webhook-v2.json
```

The untracked env must already contain the discovered
`PLATFORM_INTEGRATION_CMMS_COMPANY_ID`, exact canonical callback and host base,
and the host-only SETTINGS credential. The helper rejects absent,
non-isolated, or cross-company values.

If the plan reports `POLLING_ONLY`, do not ask for a write hash and do not run
callback apply or any secret command. Verify the explicit degraded branch:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen python -m e2e.support.cmms_admin \
  verify-degraded \
  --env-file ../.runtime/predictive-maintenance-shadow.env \
  --plan ../.runtime/plans/cmms-webhook-v2.json \
  --host-base-url-env PILOT_CMMS_HOST_BASE_URL \
  --settings-credential-env PILOT_CMMS_SETTINGS_CREDENTIAL
docker compose \
  --env-file .runtime/predictive-maintenance-shadow.env \
  -f deploy/compose/predictive-maintenance-shadow.yml \
  --profile continuous run --rm reconciliation-worker \
  platform-integration reconciliation-worker \
  --once --owner e2e-reconciliation-degraded
uv run --directory tests --frozen pytest \
  -m pilot_e2e \
  e2e/test_feedback_reconciliation.py::test_polling_only_degraded_read_only \
  --pilot-read-only -v
```

Expected: dependency health is exactly `DEGRADED_WEBHOOK_DISABLED`, polling is
ready, this branch created/rotated/deleted no endpoint, generated or mounted no
local secret, and ran no webhook-only test. If the read-only plan observed an
existing remote endpoint, `verify-degraded` requires the same endpoint ID,
generation, enabled/paused state, and secret generation afterward and records
that it is not healthy delivery evidence; it never deletes or repairs it.
This is degraded evidence only; Phase 4's full webhook acceptance remains
blocked until the separately authorized CMMS callback prerequisite is
available.

Otherwise stop after showing the target and plan hash. In a later turn, assign
`USER_CONFIRMED_WEBHOOK_PLAN_SHA256` to the literal lowercase 64-character hash
copied from that current user message, validate it, and run:

```bash
set -Eeuo pipefail
test -n "${USER_CONFIRMED_WEBHOOK_PLAN_SHA256:-}"
printf '%s' "$USER_CONFIRMED_WEBHOOK_PLAN_SHA256" |
  rg -q '^[0-9a-f]{64}$'
uv run --directory tests --frozen python -m e2e.support.cmms_admin apply \
  --env-file ../.runtime/predictive-maintenance-shadow.env \
  --plan ../.runtime/plans/cmms-webhook-v2.json \
  --plan-hash "$USER_CONFIRMED_WEBHOOK_PLAN_SHA256" \
  --confirmed-hash "$USER_CONFIRMED_WEBHOOK_PLAN_SHA256" \
  --settings-credential-env PILOT_CMMS_SETTINGS_CREDENTIAL \
  --actor codex-isolated-pilot-operator \
  --secret-output ../.runtime/secrets/cmms-webhook-v2 \
  --metadata-output ../.runtime/secrets/cmms-webhook-v2.metadata.json \
  --receipt ../.runtime/receipts/cmms-webhook-v2.json
```

The plan file contains no secret and is mode `0600`. For a confirmed
successful create/rotate, validate the exact secret, metadata,
and receipt files, then make only the secret read-only by the integration
image's UID `10001` without printing it:

```bash
set -Eeuo pipefail
secret_file="$(realpath .runtime/secrets/cmms-webhook-v2)"
metadata_file="$(realpath .runtime/secrets/cmms-webhook-v2.metadata.json)"
callback_receipt="$(realpath .runtime/receipts/cmms-webhook-v2.json)"
test "$secret_file" = \
  "$(realpath .)/.runtime/secrets/cmms-webhook-v2"
test "$metadata_file" = \
  "$(realpath .)/.runtime/secrets/cmms-webhook-v2.metadata.json"
test "$callback_receipt" = \
  "$(realpath .)/.runtime/receipts/cmms-webhook-v2.json"
test -f "$secret_file"
test -f "$metadata_file"
test -f "$callback_receipt"
test ! -L "$secret_file"
test ! -L "$metadata_file"
test ! -L "$callback_receipt"
test "$(stat -c '%a' "$metadata_file")" = 600
test "$(stat -c '%a' "$callback_receipt")" = 600
host_secret_uid="$(stat -c '%u' "$secret_file")"
test "$host_secret_uid" = "$(id -u)"
printf '%s' "$host_secret_uid" | rg -q '^[0-9]+$'
uv run --directory tests --frozen python -m e2e.support.cmms_admin verify \
  --env-file ../.runtime/predictive-maintenance-shadow.env \
  --host-base-url-env PILOT_CMMS_HOST_BASE_URL \
  --settings-credential-env PILOT_CMMS_SETTINGS_CREDENTIAL \
  --metadata ../.runtime/secrets/cmms-webhook-v2.metadata.json \
  --receipt ../.runtime/receipts/cmms-webhook-v2.json
integration_image="$(
  docker compose \
    --env-file .runtime/predictive-maintenance-shadow.env \
    -f deploy/compose/predictive-maintenance-shadow.yml \
    images -q integration-api
)"
test -n "$integration_image"
docker run --rm --user 0:0 --entrypoint /bin/sh \
  --env HOST_SECRET_UID="$host_secret_uid" \
  --mount "type=bind,src=$secret_file,dst=/secret" \
  "$integration_image" \
  -eu -c \
  'chown "$HOST_SECRET_UID":10001 /secret && chmod 0440 /secret'
test "$(stat -c '%u:%g:%a' "$secret_file")" = \
  "$host_secret_uid:10001:440"
test -r "$secret_file"
```

Do not put `PILOT_CMMS_WEBHOOK_SECRET_FILE` in the shared env file. The separate
`deploy/compose/predictive-maintenance-webhook.yml` override mounts the exact
host file read-only and injects both the secret-file environment name and
`PLATFORM_INTEGRATION_CMMS_WEBHOOK_SECRET_REF=PILOT_CMMS_WEBHOOK_SECRET_FILE`
into `integration-api` only, with the target path fixed to
`/run/secrets/cmms-webhook-v2`. The image runs as UID:GID `10001:10001`;
group-read plus a read-only bind lets the API read while preserving host-owner
read access needed for later metadata/hash verification. Recreate only that
API with both Compose files:

```bash
set -Eeuo pipefail
docker compose \
  --env-file .runtime/predictive-maintenance-shadow.env \
  -f deploy/compose/predictive-maintenance-shadow.yml \
  -f deploy/compose/predictive-maintenance-webhook.yml \
  up -d --no-deps --force-recreate integration-api
docker compose \
  --env-file .runtime/predictive-maintenance-shadow.env \
  -f deploy/compose/predictive-maintenance-shadow.yml \
  -f deploy/compose/predictive-maintenance-webhook.yml \
  exec -T integration-api \
  sh -eu -c \
  'test -r /run/secrets/cmms-webhook-v2; test ! -w /run/secrets/cmms-webhook-v2'
curl --fail --silent http://127.0.0.1:18080/readyz
```

Do not mount or inject the webhook secret into inbox, reconciliation,
scheduler, prediction, Alarm-outbox, or work-order workers. Compose/image
contract tests inspect every role. Verify `/readyz` before enabling CMMS
dispatch. No secret value is copied into the integration database or Compose
YAML.

- [ ] **Step 3: Run CMMS focused and full regression**

Run:

```bash
set -Eeuo pipefail
mvn -f components/cmms/api/pom.xml \
  -Dtest='AssetControllerTest,IntegrationIdempotencyServiceTest,AssetIntegrationTest,WorkOrderControllerTest,WorkOrderIntegrationServiceTest,WorkOrderIntegrationTest,ApiKeyExpiryTest,ApiKeyExpiryMigrationTest,WebhookUrlValidatorTest,WebhookHttpClientTest,IntegrationCapabilitiesControllerTest,WebhookEndpointControllerTest,WebhookEndpointServiceTest,WorkOrderStatusIdempotencyTest,WorkOrderStatusOutboxServiceTest,WebhookOutboxDispatchJobTest,WebhookOutboxAdminControllerTest,WebhookOutboxIntegrationTest,WorkOrderServiceTest' test
mvn -f components/cmms/api/pom.xml -DskipTests compile
MAVEN_OPTS='-Xmx1536m' mvn -f components/cmms/api/pom.xml test
```

Expected: focused integration/security tests and the full CMMS suite pass.

- [ ] **Step 4: Run ThingsBoard and PDM regressions**

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
uv run --directory components/pdm-algorithm --frozen pytest \
  --ignore=tests/test_cpu_train_predict_smoke.py \
  tests/test_prediction_v2_normalization.py \
  tests/test_prediction_v2_catalog.py \
  tests/test_prediction_v2_service.py \
  tests/test_prediction_v2_api.py \
  tests/test_prediction_v2_fixtures.py \
  tests/test_prediction_v2_readiness.py -v
```

Expected: dashboard/TB core and non-training PDM suites pass.

- [ ] **Step 5: Run contract and isolated end-to-end suites**

Provisioning, Dashboard publication, approver grants, the Phase 3 work-order
plan, callback creation, Phase 4 risk preparation, Phase 4 status feedback, the
replay-target Action, Phase 4 risk recovery, and replay retain their own
explicit confirmations. A non-interactive pytest command never manufactures
or assumes them.
`tests/e2e/conftest.py` registers `--pilot-read-only`,
`--confirmed-preparation-receipt`, `--confirmed-status-receipt`,
`--confirmed-replay-target-receipt`, `--confirmed-recovery-receipt`,
`--confirmed-replay-receipt`, and `--ignore-replay-apply`; read-only mode
rejects every upstream HTTP method other than GET/HEAD and provides no
allow-write override.

The commands below are the full webhook-mode gate and require the verified
`.runtime/receipts/cmms-webhook-v2.json` plus matching metadata. If Step 2
selected `POLLING_ONLY`, the executable degraded gate is exactly the
`verify-degraded`, one reconciliation cycle, and read-only polling test already
shown in Step 2. Stop there: do not render any plan below, do not create a
substitute callback receipt, and do not claim the full Phase 4 or final-master
acceptance.

First run contracts/helper tests, prepare the read-only context, and render
only the telemetry/risk-preparation plan:

```bash
set -Eeuo pipefail
install -d -m 0700 .runtime/plans .runtime/receipts
uv run --directory tests --frozen pytest contract -v
uv run --directory tests --frozen pytest \
  e2e/test_pilot_writes.py -v
uv run --directory tests --frozen pytest \
  -m pilot_e2e \
  e2e/test_feedback_reconciliation.py::test_prepare_phase4_write_context \
  --pilot-read-only -v
uv run --directory tests --frozen python -m e2e.support.pilot_writes plan \
  --scenario phase4-risk-preparation \
  --env-file ../.runtime/predictive-maintenance-shadow.env \
  --phase3-receipt ../.runtime/receipts/phase3-work-order.json \
  --actor codex-isolated-pilot-operator \
  --output ../.runtime/plans/phase4-risk-preparation.json
```

This read-only plan prints only its exact telemetry bodies, bounded worker
cycles, the existing Phase 3 Alert identity, the second target device/stable
risk key and expected Alarm result, expiry, and canonical SHA-256. It does not
invent the not-yet-created second Alert UUID. Stop the turn. In a later turn,
assign
`USER_CONFIRMED_PHASE4_RISK_PREPARATION_SHA256` to the literal lowercase hash
copied from that current user message, validate it, and apply once:

```bash
set -Eeuo pipefail
test -n "${USER_CONFIRMED_PHASE4_RISK_PREPARATION_SHA256:-}"
printf '%s' "$USER_CONFIRMED_PHASE4_RISK_PREPARATION_SHA256" |
  rg -q '^[0-9a-f]{64}$'
uv run --directory tests --frozen python -m e2e.support.pilot_writes apply \
  --scenario phase4-risk-preparation \
  --env-file ../.runtime/predictive-maintenance-shadow.env \
  --plan ../.runtime/plans/phase4-risk-preparation.json \
  --plan-hash "$USER_CONFIRMED_PHASE4_RISK_PREPARATION_SHA256" \
  --confirmed-hash "$USER_CONFIRMED_PHASE4_RISK_PREPARATION_SHA256" \
  --actor codex-isolated-pilot-operator \
  --receipt ../.runtime/receipts/phase4-risk-preparation.json
```

The apply writes no Action or CMMS state. It GET-verifies both exact Alerts and
writes a mode-`0600` receipt. Next render the independent status plan:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen python -m e2e.support.pilot_writes plan \
  --scenario phase4-status-feedback \
  --env-file ../.runtime/predictive-maintenance-shadow.env \
  --phase3-receipt ../.runtime/receipts/phase3-work-order.json \
  --preparation-receipt \
  ../.runtime/receipts/phase4-risk-preparation.json \
  --callback-receipt ../.runtime/receipts/cmms-webhook-v2.json \
  --actor codex-isolated-pilot-operator \
  --output ../.runtime/plans/phase4-status-feedback.json
```

Stop again. Only in a later turn assign
`USER_CONFIRMED_PHASE4_STATUS_FEEDBACK_SHA256` from the literal lowercase hash
in the user's current message, validate it, and apply:

```bash
set -Eeuo pipefail
test -n "${USER_CONFIRMED_PHASE4_STATUS_FEEDBACK_SHA256:-}"
printf '%s' "$USER_CONFIRMED_PHASE4_STATUS_FEEDBACK_SHA256" |
  rg -q '^[0-9a-f]{64}$'
uv run --directory tests --frozen python -m e2e.support.pilot_writes apply \
  --scenario phase4-status-feedback \
  --env-file ../.runtime/predictive-maintenance-shadow.env \
  --plan ../.runtime/plans/phase4-status-feedback.json \
  --plan-hash "$USER_CONFIRMED_PHASE4_STATUS_FEEDBACK_SHA256" \
  --confirmed-hash "$USER_CONFIRMED_PHASE4_STATUS_FEEDBACK_SHA256" \
  --actor codex-isolated-pilot-operator \
  --receipt ../.runtime/receipts/phase4-status-feedback.json
```

For every Compose subprocess, status apply supplies the root's main and
webhook override files plus the exact env file. It runs bounded ephemeral
`inbox-worker --once --owner e2e-inbox-phase4` and
`reconciliation-worker --once --now 2026-07-29T01:20:00Z --owner
e2e-reconciliation-phase4` cycles at the plan's fixed ordinals. No pytest node
executes those writes. The receipt contains no replay target.

Now render the independently confirmed replay-target Action:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen python -m e2e.support.pilot_writes plan \
  --scenario phase4-replay-target \
  --env-file ../.runtime/predictive-maintenance-shadow.env \
  --phase3-receipt ../.runtime/receipts/phase3-work-order.json \
  --preparation-receipt \
  ../.runtime/receipts/phase4-risk-preparation.json \
  --status-receipt ../.runtime/receipts/phase4-status-feedback.json \
  --dashboard-receipt \
  ../.runtime/receipts/phase3-dashboard-actions.json \
  --actor codex-isolated-pilot-operator \
  --output ../.runtime/plans/phase4-replay-target.json
```

The plan identifies a different exact Alert/device from the completed Phase 3
work order, the one Action request, the normal work-order service/image and
running pre-state, the zero-active-lease/zero-other-eligible summary hash, the
initial absent reconciliation GET, the lost-response POST, seven failed
reconciliations, eight exact event-scoped worker owners/timestamps that each
meet the preceding persisted retry boundary, and the expected generation-zero
dead letter plus `WORK_ORDER_CREATE_FAILED` Alert state/version. Stop. In
another later turn assign
`USER_CONFIRMED_PHASE4_REPLAY_TARGET_SHA256` from the user's literal hash,
validate it, and apply:

```bash
set -Eeuo pipefail
test -n "${USER_CONFIRMED_PHASE4_REPLAY_TARGET_SHA256:-}"
printf '%s' "$USER_CONFIRMED_PHASE4_REPLAY_TARGET_SHA256" |
  rg -q '^[0-9a-f]{64}$'
uv run --directory tests --frozen python -m e2e.support.pilot_writes apply \
  --scenario phase4-replay-target \
  --env-file ../.runtime/predictive-maintenance-shadow.env \
  --plan ../.runtime/plans/phase4-replay-target.json \
  --plan-hash "$USER_CONFIRMED_PHASE4_REPLAY_TARGET_SHA256" \
  --confirmed-hash "$USER_CONFIRMED_PHASE4_REPLAY_TARGET_SHA256" \
  --actor codex-isolated-pilot-operator \
  --receipt ../.runtime/receipts/phase4-replay-target.json
```

Replay-target apply first validates that no work order uses the planned
external reference, gracefully stops only the exact plan-bound normal
work-order service when its frozen state was running, and requires
`work-order-worker-state` to show zero active leases and no other eligible
command. It then starts the healthy confirmed proxy script, issues the one
Action, and runs the eight exact
`work-order-worker --once --event-id ... --now ... --owner ...` cycles, and
uses an outermost finally path to remove the proxy and restore the exact
normal-worker pre-state. After each cycle it reads back the persisted
attempt/retry boundary before allowing the next fixed time. The first cycle's
GET proves absence, its POST commits one CMMS work order but loses the
response, and the next seven cycles each make one failing reconciliation GET
and issue no POST. After cleanup, a host-side GET proves exactly one matching
work order while the integration command remains generation-zero
`DEAD_LETTER` and the bound Alert is `WORK_ORDER_CREATE_FAILED` at the planned
version. Any service/image/lease/proxy-state/count/target/hash/identity
mismatch aborts without writing a successful receipt; cleanup and exact
service-state restoration still run.

Now render only the post-status telemetry/risk recovery:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen python -m e2e.support.pilot_writes plan \
  --scenario phase4-risk-recovery \
  --env-file ../.runtime/predictive-maintenance-shadow.env \
  --preparation-receipt \
  ../.runtime/receipts/phase4-risk-preparation.json \
  --status-receipt ../.runtime/receipts/phase4-status-feedback.json \
  --replay-target-receipt \
  ../.runtime/receipts/phase4-replay-target.json \
  --actor codex-isolated-pilot-operator \
  --output ../.runtime/plans/phase4-risk-recovery.json
```

The plan freezes only healthy telemetry, exact worker ordinals, expected risk
counter transitions, and current immutable-command digest. Stop. In a later
turn assign `USER_CONFIRMED_PHASE4_RISK_RECOVERY_SHA256` from the user's
literal hash, validate it, and apply:

```bash
set -Eeuo pipefail
test -n "${USER_CONFIRMED_PHASE4_RISK_RECOVERY_SHA256:-}"
printf '%s' "$USER_CONFIRMED_PHASE4_RISK_RECOVERY_SHA256" |
  rg -q '^[0-9a-f]{64}$'
uv run --directory tests --frozen python -m e2e.support.pilot_writes apply \
  --scenario phase4-risk-recovery \
  --env-file ../.runtime/predictive-maintenance-shadow.env \
  --plan ../.runtime/plans/phase4-risk-recovery.json \
  --plan-hash "$USER_CONFIRMED_PHASE4_RISK_RECOVERY_SHA256" \
  --confirmed-hash "$USER_CONFIRMED_PHASE4_RISK_RECOVERY_SHA256" \
  --actor codex-isolated-pilot-operator \
  --receipt ../.runtime/receipts/phase4-risk-recovery.json
```

Apply GET-verifies that the first work order is still complete, the second
command is still the same generation-zero dead letter, no work-order count
changed, and only then writes its mode-`0600` receipt.

Now run the receipt-bound GET/HEAD-only verification suite:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  -m pilot_e2e \
  e2e/test_feedback_reconciliation.py \
  e2e/test_restart_recovery.py \
  e2e/test_failure_injection.py \
  e2e/test_twenty_device_acceptance.py \
  --pilot-read-only \
  --confirmed-preparation-receipt \
  ../.runtime/receipts/phase4-risk-preparation.json \
  --confirmed-status-receipt \
  ../.runtime/receipts/phase4-status-feedback.json \
  --confirmed-replay-target-receipt \
  ../.runtime/receipts/phase4-replay-target.json \
  --confirmed-recovery-receipt \
  ../.runtime/receipts/phase4-risk-recovery.json \
  --ignore-replay-apply -v
```

The read-only `receipt-field` command validates the mode-`0600`
replay-target and recovery receipts, their confirmed plan hashes, and the
current isolated integration database selected by the exact env/Compose
context before printing only the non-secret exact dead-letter UUID. It permits
only the Alert/risk version changes proven by the recovery receipt and still
requires the same event ID, generation-zero `DEAD_LETTER`,
`WORK_ORDER_CREATE_FAILED` maintenance state, payload/target hashes, external
reference, command digest, and existing CMMS work-order ID. Use that
receipt-bound ID to render the durable replay plan:

```bash
set -Eeuo pipefail
DEAD_LETTER_EVENT_ID="$(
  uv run --directory tests --frozen python \
    -m e2e.support.pilot_writes receipt-field \
    --env-file ../.runtime/predictive-maintenance-shadow.env \
    --receipt ../.runtime/receipts/phase4-replay-target.json \
    --recovery-receipt \
    ../.runtime/receipts/phase4-risk-recovery.json \
    --field dead_letter_event_id
)"
printf '%s' "$DEAD_LETTER_EVENT_ID" |
  rg -q '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
docker compose \
  --env-file .runtime/predictive-maintenance-shadow.env \
  -f deploy/compose/predictive-maintenance-shadow.yml \
  exec -T integration-api \
  platform-integration replay-plan \
  --tenant-alias ifactory-pilot \
  --event-id "$DEAD_LETTER_EVENT_ID" \
  --actor codex-isolated-pilot-operator \
  --reason "isolated phase-4 replay acceptance" \
  --new-correlation-id 00000000-0000-4000-8000-000000000501
```

The replay plan performs only GET/reconciliation reads. It must freeze the
generation-zero dead-letter metadata and the one already-existing matching
CMMS work order from the replay-target receipt, then print the canonical hash.
Stop and wait for the user to return that exact hash in a later turn. Assign
`USER_CONFIRMED_REPLAY_PLAN_SHA256` from that current user message, reload and
revalidate the receipt-bound event ID, validate the hash, and apply exactly
once:

```bash
set -Eeuo pipefail
DEAD_LETTER_EVENT_ID="$(
  uv run --directory tests --frozen python \
    -m e2e.support.pilot_writes receipt-field \
    --env-file ../.runtime/predictive-maintenance-shadow.env \
    --receipt ../.runtime/receipts/phase4-replay-target.json \
    --recovery-receipt \
    ../.runtime/receipts/phase4-risk-recovery.json \
    --field dead_letter_event_id
)"
printf '%s' "$DEAD_LETTER_EVENT_ID" |
  rg -q '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
test -n "${USER_CONFIRMED_REPLAY_PLAN_SHA256:-}"
printf '%s' "$USER_CONFIRMED_REPLAY_PLAN_SHA256" |
  rg -q '^[0-9a-f]{64}$'
docker compose \
  --env-file .runtime/predictive-maintenance-shadow.env \
  -f deploy/compose/predictive-maintenance-shadow.yml \
  exec -T integration-api \
  platform-integration replay-apply \
  --tenant-alias ifactory-pilot \
  --event-id "$DEAD_LETTER_EVENT_ID" \
  --plan-hash "$USER_CONFIRMED_REPLAY_PLAN_SHA256" \
  --confirmed-hash "$USER_CONFIRMED_REPLAY_PLAN_SHA256" \
  --actor codex-isolated-pilot-operator
install -d -m 0700 .runtime/receipts
umask 077
test ! -L .runtime/receipts/phase4-replay.json
docker compose \
  --env-file .runtime/predictive-maintenance-shadow.env \
  -f deploy/compose/predictive-maintenance-shadow.yml \
  exec -T integration-api \
  platform-integration replay-verify \
  --tenant-alias ifactory-pilot \
  --event-id "$DEAD_LETTER_EVENT_ID" \
  --plan-hash "$USER_CONFIRMED_REPLAY_PLAN_SHA256" \
  --format json \
  > .runtime/receipts/phase4-replay.json
chmod 0600 .runtime/receipts/phase4-replay.json
test ! -L .runtime/receipts/phase4-replay.json
test "$(stat -c '%a' .runtime/receipts/phase4-replay.json)" = 600
uv run --directory tests --frozen pytest \
  -m pilot_e2e \
  e2e/test_failure_injection.py::test_confirmed_single_event_replay \
  --pilot-read-only \
  --confirmed-preparation-receipt \
  ../.runtime/receipts/phase4-risk-preparation.json \
  --confirmed-status-receipt \
  ../.runtime/receipts/phase4-status-feedback.json \
  --confirmed-replay-target-receipt \
  ../.runtime/receipts/phase4-replay-target.json \
  --confirmed-recovery-receipt \
  ../.runtime/receipts/phase4-risk-recovery.json \
  --confirmed-replay-receipt ../.runtime/receipts/phase4-replay.json -v
./scripts/doctor.sh
```

Expected: contracts and helper tests pass before any Phase 4 business write;
after the four independently confirmed applies, GET/HEAD-only acceptance
passes; after the still-later confirmed replay, the single replay verification
and doctor pass. Replay reconciliation must find the one receipt-bound CMMS
work order, transition the immutable command and Alert to delivered/open
without another POST, and record result `RECONCILED_EXISTING`. The replay
receipt is generated from the durable consumed plan and immutable replay audit,
and binds tenant/company/event/generation/external work-order/result to the
confirmed replay hash; none of the four earlier receipts can substitute for
it. No batch replay is possible.

- [ ] **Step 6: Commit the coordinated Phase 4 slice**

After CMMS and integration branches are pushed:

```bash
set -Eeuo pipefail
git add \
  components/cmms \
  components/platform-integration \
  contracts/asyncapi \
  contracts/json-schema/cmms-work-order-status-event-v1.json \
  contracts/json-schema/maintenance-alert-v1.json \
  contracts/openapi/cmms-integration-v1.yaml \
  contracts/openapi/platform-integration-v1.yaml \
  deploy/compose \
  deploy/gateway/predictive-maintenance.conf \
  deploy/README.md \
  tests/contract/phase4 \
  tests/e2e \
  tests/README.md \
  .github/workflows/workspace-check.yml
git diff --cached --check
git diff --cached --submodule=log
./scripts/doctor.sh
git commit -m "feat: close predictive maintenance feedback loop"
```

Expected: all providers, consumers, contracts, deployment, and failure evidence are committed together with clean pushed component SHAs.
