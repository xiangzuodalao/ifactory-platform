# Predictive Maintenance Integration Master Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the approved 20-device CMMS–ThingsBoard–PDM predictive-maintenance pilot as four independently testable vertical phases.

**Architecture:** A new `platform-integration` service owns mappings, scheduling, risk state, approvals, inbox/outbox, audit, and reconciliation in its own PostgreSQL database. ThingsBoard remains the telemetry/alarm/UI system, PDM remains the deterministic prediction/model system, and CMMS remains the asset/work-order system; all collaboration uses versioned contracts, never cross-service database access.

**Tech Stack:** Python 3.12, uv, FastAPI, Pydantic v2, httpx, SQLAlchemy 2, Alembic, psycopg 3, PostgreSQL 16, pytest, Ruff, Java 17/Spring Boot 3.2.3/Liquibase for CMMS, and the existing ThingsBoard automotive-factory Python dashboard generator.

## Global Constraints

- Start from `/home/vm/code/ifactory-platform`; after the required worktree skill creates the isolated implementation worktree, treat its `git rev-parse --show-toplevel` result as the command root. `components/*` remain independent Git repositories.
- Use feature branches rooted at the exact commits recorded by the superproject; never begin detached-submodule work from an unrelated branch.
- Before changing a component, inspect its clean status, current branch, and
  remotes, then completely read the root `AGENTS.md`, that component's
  `AGENTS.md` when present, its correctly cased README, and only the
  task-relevant build/interface docs.
- Component code is committed and pushed in the component repository before the final coordinated superproject commit records its gitlink, contracts, deployment, or platform tests. The sole Phase 1 exception is the Task 2 local, unpushed contract checkpoint for SDD review; it is not publication and must remain local until provider and consumer branches pass and the coordinated gate completes.
- The new component remote is `https://github.com/xiangzuodalao/platform-integration.git`. Before implementation, verify that this initialized repository exists; if it does not, stop and request authority to create it.
- The new service implementation assumption is Python 3.12 with FastAPI/SQLAlchemy/Alembic. A JVM requirement must be decided before Phase 1 because it changes all `platform-integration` file paths, but not the approved external contracts.
- The pilot contains exactly 20 simulated ThingsBoard devices, six device types, and one predictive measurement per device.
- Prediction runs every 15 minutes; configuration may permit 15–60 minutes, but tests and fixtures use 15 minutes.
- A ThingsBoard Alarm ACK never authorizes a CMMS work order. Only `CREATE_WORK_ORDER` through the versioned Action API does.
- `REJECT` and `CLOSE_RISK` require a non-empty reason. `CLOSE_RISK` resets risk counters but never cancels an already approved CMMS command.
- No Kafka, RabbitMQ, shared database, shared ORM model, autonomous agent service, or runtime MCP data path is introduced.
- `digital-platform` MCP remains a control plane for PDM training/operations only. Runtime telemetry, prediction scheduling, alarm delivery, and work-order creation never use MCP.
- No task in these plans trains a model. Real Informer/Autoformer replacement later requires one `$pdm-onboard-scenario` flow and one separately confirmed `$pdm-train-model` flow per scenario, with no automatic retry.
- Tests use isolated tenants, synthetic telemetry, temporary fixture artifacts, and simulated CMMS data. They never connect to a production database or create a production work order.
- Provisioning, Dashboard publication, telemetry/risk preparation, approver
  grants, work-order Actions, CMMS status changes, callback endpoint/secret
  administration, and replay each have an independent short-lived plan,
  later-turn exact hash confirmation, bounded apply, and durable or mode-`0600`
  receipt. One class's confirmation never authorizes another.
- Do not commit `.env`, credentials, connection strings, raw telemetry, checkpoints, `artifacts/`, virtual environments, dependency directories, build output, or runtime caches.
- Logs and API errors exclude tokens, raw time series, upstream stack traces, connection configuration, absolute artifact paths, and CMMS work-order body/attachments.
- Trusted service-to-service platform requests carry `tenant_id`, `correlation_id`, and domain IDs; writes also carry an idempotency key. Delegated browser Actions omit caller-supplied tenant and derive it from verified ThingsBoard identity. Provider events carry their authoritative native tenant/company ID, which integration maps to platform tenant.
- Reads and PDM calls retry at most three times within the current slot.
  External writes retry at most eight times. Each CMMS work-order attempt makes
  exactly one external-reference GET before any POST; that pre-write GET has no
  nested client retry and failure is retried only by the next outbox attempt.
- Every component commit is preceded by its focused tests and `./scripts/doctor.sh`; every phase gate also runs contract tests and the phase-specific end-to-end test.
- Every planned RED command must successfully collect or compile its tests and fail only a named behavioural assertion. Import, collection, project-metadata, or Java compilation errors are invalid RED results; absent Python implementations use test-local delayed imports converted to explicit assertions, and absent Java types are exercised through compile-safe reflection or HTTP behaviour.
- Root feature commits, including a permitted local SDD checkpoint, remain unpushed until their phase's final coordination gate and final coordination commit complete. Then push only the named root feature branch; never push `main`.

## Execution Prerequisites

The plans implement and verify integration code but do not silently take over
an unrelated local service. Before any live Phase 2 gate, all of these
operator-owned prerequisites must be satisfied:

- the `platform-integration` remote and `main` branch exist and are accessible
  at the exact URL in the repository map;
- `http://127.0.0.1:8080` is an explicitly designated isolated
  automotive-factory ThingsBoard tenant containing the exact 20 simulator
  devices; if the port belongs to an unapproved legacy checkout or another
  tenant, stop and ask the user to start/authorize the intended instance
  rather than stopping or reusing it;
- `http://127.0.0.1:3000` is an explicitly designated isolated CMMS instance
  built from the current phase's CMMS component commit, with a pre-provisioned
  isolated company, valid `API_ACCESS` entitlement/plan feature, a
  least-privilege runtime API key, and a separate short-lived
  `SETTINGS`-authorized user for later callback administration;
- the CMMS and ThingsBoard identities discovered through their own APIs match
  the untracked pilot configuration, and both pass the zero-Alarm/zero-work
  order isolation baseline before the first integration write.

Starting, replacing, licensing, seeding, or reconfiguring either existing
product deployment is a separate operator action and requires explicit
authorization at execution time. If a prerequisite is absent, the phase stops;
it never falls back to a prebuilt CMMS image, a legacy ThingsBoard process, a
production tenant, direct database setup, or elevated runtime credentials.

For Phase 4 webhook mode, the isolated CMMS operator must additionally
preconfigure the exact callback base and container reachability described in
the Phase 4 plan. If that independently authorized prerequisite is absent but
the other CMMS read APIs work, Phase 4 runs polling-only degraded acceptance
and performs no endpoint/secret mutation.

## Fixed Pilot Model Profiles

These values close the implementation-level identifiers that were intentionally abstract in the approved architecture. They are fixtures for the isolated pilot, not production model claims.

| Display type | Exact simulator `device_type` | `model_profile_id` | `model_info_id` | `meas_code` | Unit | Frequency | Context | Horizon | Scale | Risk |
|---|---|---|---|---|---|---:|---:|---:|---:|---|
| CNC | `CNC` | `pilot-cnc-vibration` | `pilot-fixture-v1-cnc-vibration` | `vibration_rms` | `mm/s` | `1min` | 60 | 15 | 2 | `> 5.00` |
| Injection molding | `INJECTION_MOLDING` | `pilot-injection-pressure` | `pilot-fixture-v1-injection-pressure` | `injection_pressure` | `bar` | `1min` | 60 | 15 | 1 | `> 175.0` |
| Assembly robot | `ASSEMBLY_ROBOT` | `pilot-robot-position` | `pilot-fixture-v1-robot-position` | `position_deviation` | `mm` | `1min` | 60 | 15 | 3 | `> 1.000` |
| Tightening | `TIGHTENING` | `pilot-tightening-torque` | `pilot-fixture-v1-tightening-torque` | `torque` | `N·m` | `1min` | 60 | 15 | 2 | `< 15.00` |
| Air compressor | `AIR_COMPRESSOR` | `pilot-compressor-pressure` | `pilot-fixture-v1-compressor-pressure` | `discharge_pressure` | `MPa` | `1min` | 60 | 15 | 3 | `< 0.500` |
| EOL tester | `EOL_TESTER` | `pilot-eol-pass-rate` | `pilot-fixture-v1-eol-pass-rate` | `pass_rate` | `%` | `1min` | 60 | 15 | 2 | `< 90.00` |

All six use:

```text
preprocessing_version = pdm-v2-pilot-1
policy_version        = pilot-threshold-v1
predictor_kind        = repeat-last
request_window_points = 66
```

`context_points=60` is the minimum finite input count. Each Phase 2+ request covers 66 expected one-minute buckets and may omit at most 6 observed values; `history` contains only finite observed records, while PDM reconstructs missing buckets from the explicit half-open request window.

Provisioning matches only the six exact simulator `device_type` keys above. Display labels are not lookup keys, and an unknown or differently cased type fails the plan before any write.

The generated artifacts are these exact no-newline RFC 8785 JSON objects:

```json
{"kind":"repeat-last","model_info_id":"pilot-fixture-v1-cnc-vibration","model_profile_id":"pilot-cnc-vibration","preprocessing_version":"pdm-v2-pilot-1","schema_version":1}
{"kind":"repeat-last","model_info_id":"pilot-fixture-v1-injection-pressure","model_profile_id":"pilot-injection-pressure","preprocessing_version":"pdm-v2-pilot-1","schema_version":1}
{"kind":"repeat-last","model_info_id":"pilot-fixture-v1-robot-position","model_profile_id":"pilot-robot-position","preprocessing_version":"pdm-v2-pilot-1","schema_version":1}
{"kind":"repeat-last","model_info_id":"pilot-fixture-v1-tightening-torque","model_profile_id":"pilot-tightening-torque","preprocessing_version":"pdm-v2-pilot-1","schema_version":1}
{"kind":"repeat-last","model_info_id":"pilot-fixture-v1-compressor-pressure","model_profile_id":"pilot-compressor-pressure","preprocessing_version":"pdm-v2-pilot-1","schema_version":1}
{"kind":"repeat-last","model_info_id":"pilot-fixture-v1-eol-pass-rate","model_profile_id":"pilot-eol-pass-rate","preprocessing_version":"pdm-v2-pilot-1","schema_version":1}
```

The six expected SHA-256 values are:

```text
pilot-cnc-vibration          5feeb31058fe0521f94758faa22214afe4619e466e22cbb8c6fb7ffcf2369562
pilot-injection-pressure     6e3faef72b69bbd7e9562e871f405a38286075461d1b0c3a5d170c72d7e0f1a9
pilot-robot-position         11f162e58f9958ed405a8c67dcbb780900321385d8c2eb45122367b11ed5016e
pilot-tightening-torque      dae2043bc142ad6bb984e296d2cff21f6a93ccba49f1e42c02ca26b6e413d014
pilot-compressor-pressure    5e2959d7617adb4fad4da45702bbef75f11832f8f7f9818c7f2e662e83bccf38
pilot-eol-pass-rate          5fe0b2a40d796cbbd5e42d67cce20d195c2c67a75795cc9f4390848f1c119377
```

## Repository and Branch Map

| Repository | Start point | Feature branch |
|---|---|---|
| Superproject | `feat/predictive-maintenance-integration-design` | `feat/predictive-maintenance-integration` |
| PDM | recorded gitlink `e4dd47bb28002bc83be8a4e2d13906a2295ae678` | `feat/pdm-prediction-v2` |
| CMMS | recorded gitlink `3d9b0765f26d83c0f1eb511717563b59084f8bd8` | `feat/predictive-maintenance-integration` |
| ThingsBoard | recorded gitlink `172547d7193a3082984b2a5f5b595fce947020fc` | `feat/predictive-maintenance-dashboard` |
| `platform-integration` | full `origin/main` SHA captured and audited in Phase 1 Task 1 | `feat/predictive-maintenance-pilot` |
| `digital-mcp` | unchanged | no branch |

PDM, CMMS, and ThingsBoard branch from these full recorded gitlink commits after verifying the expected remote branch contains them. PDM must not branch from its stale `main`; ThingsBoard must not commit from detached HEAD.

## Plan Suite and Delivery Gates

### Phase 1: Contract and Component Foundation

Detailed plan: [Phase 1 foundation](2026-07-29-predictive-maintenance-phase-1-foundation-plan.md)

Produces:

- the fifth submodule and service skeleton;
- PDM `POST /api/v2/predictions` with real `history` consumption;
- CMMS `equipment_id` and idempotent asset creation/query;
- provider/consumer contract tests held in a local SDD-review checkpoint and finalized with the coordinated first real contracts;
- no live cross-system write.

Gate:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest contract/phase1 -v
./scripts/doctor.sh
```

Expected: all Phase 1 contract tests pass and doctor reports zero failures.

### Phase 2: Provisioning and Shadow Prediction

Detailed plan: [Phase 2 shadow prediction](2026-07-29-predictive-maintenance-phase-2-shadow-plan.md)

Produces:

- six isolated read-only PDM fixtures with verified hashes;
- integration database mappings, leases, scheduler, quality checks, and risk counters;
- explicitly confirmed provisioning of 20 CMMS assets and ThingsBoard attributes;
- 15-minute shadow prediction with no ThingsBoard PDM Alarm and no work-order button.

Gate:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  e2e/test_shadow_seed.py -v
uv run --directory tests --frozen pytest \
  -m pilot_e2e e2e/test_shadow_prediction.py \
  --confirmed-seed-receipt ../.runtime/receipts/phase2-shadow-seed.json -v
./scripts/doctor.sh
```

Run the detailed plan's independent provisioning, Dashboard, and telemetry-seed
plan/apply gates first; each hash must come from its own later user turn.
Expected: the receipt-bound 20 bindings finish one accelerated slot and neither
a PDM Alarm nor a CMMS work order exists.

### Phase 3: Alarm, Approval, and Idempotent Work Order

Detailed plan: [Phase 3 alarm and work order](2026-07-29-predictive-maintenance-phase-3-alarm-work-order-plan.md)

Produces:

- stable-risk-key ThingsBoard Alarm projection and ordered outbox;
- same-origin Dashboard Actions with `maintenance_alert_version`;
- delegated ThingsBoard identity verification and approver allowlist;
- immutable approval command and idempotent CMMS work-order creation/query;
- no work order before explicit approval and at most one after retries/concurrency.

Gate:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest contract/phase3 -v
uv run --directory tests --frozen pytest e2e/test_pilot_writes.py -v
```

First complete the detailed plan's separately confirmed Action-enabled
Dashboard publication and approver grant. Then render/confirm/apply the exact
risk-preparation plan; only its receipt may be used to render the independent
work-order plan. Stop for that later user-confirmed work-order hash and run the
single bounded apply. Only after both preparation and work-order receipts
exist, run:

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

Expected: two consecutive risk rounds create one Alarm; ACK creates no work order; repeated `CREATE_WORK_ORDER` creates exactly one.

### Phase 4: Feedback, Reconciliation, and Hardening

Detailed plan: [Phase 4 feedback and hardening](2026-07-29-predictive-maintenance-phase-4-feedback-hardening-plan.md)

Produces:

- durable CMMS status-event outbox with stable ID/version;
- signed integration inbox, polling reconciliation, and maintenance feedback;
- dead-letter/replay controls, metrics, dependency health, and restart recovery;
- full 20-device end-to-end and failure-injection suite.

Gate:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest contract -v
uv run --directory tests --frozen pytest e2e/test_pilot_writes.py -v
```

Then create the separately confirmed callback and execute four independent
Phase 4 plans, each stopped for its own later user-confirmed hash: pre-status
telemetry/risk preparation, the exact three CMMS status transitions, one
dedicated replay-target work-order Action whose first CMMS create succeeds
behind a deliberately lost response and whose subsequent reconciliations fail
until the immutable command dead-letters, and post-status risk recovery. Each
apply writes its own durable receipt; preparation/recovery are independently
bounded even though both belong to the telemetry/risk write class, and no
receipt authorizes another class or stage. Receipt-bound acceptance is
GET/HEAD-only. Finally generate one exact replay plan for the dead-letter ID
bound by the replay-target receipt, stop for another later user-confirmed hash,
apply only that event, run the replay-verification node, and run doctor exactly
as defined in the Phase 4 plan. Expected: all acceptance cases pass, including
webhook loss, write-after-timeout, stale outbox versions, lease expiry, service
restart, and the separately confirmed audited replay.

If webhook entitlement, v2 support, or the separately preconfigured exact
callback base is unavailable, run only the detailed polling-only degraded gate.
That evidence is useful but does not satisfy this full Phase 4 or final-master
acceptance; do not create/mount a secret or run webhook/replay assertions in
that branch.

## Cross-Repository Commit Protocol

For every phase:

- [ ] **Step 1: Inspect exact targets before edits**

```bash
set -Eeuo pipefail
git status --short --branch
git -C components/cmms status --short --branch
git -C components/thingsboard status --short --branch
git -C components/pdm-algorithm status --short --branch
if test -e components/platform-integration/.git; then
  git -C components/platform-integration status --short --branch
fi
for component in cmms thingsboard pdm-algorithm; do
  git -C "components/$component" branch --show-current
  for remote_name in $(
    git -C "components/$component" remote
  ); do
    remote_url="$(
      git -C "components/$component" config --get \
        "remote.$remote_name.url"
    )"
    if printf '%s' "$remote_url" |
      rg -q '^https?://[^/@]+@'; then
      printf 'credentialed remote refused: %s/%s\n' \
        "$component" "$remote_name" >&2
      exit 1
    fi
    printf '%s %s %s\n' "$component" "$remote_name" "$remote_url"
  done
done
if test -e components/platform-integration/.git; then
  git -C components/platform-integration branch --show-current
  platform_origin="$(
    git -C components/platform-integration config --get remote.origin.url
  )"
  if printf '%s' "$platform_origin" |
    rg -q '^https?://[^/@]+@'; then
    printf 'credentialed platform-integration origin refused\n' >&2
    exit 1
  fi
  printf 'platform-integration origin %s\n' "$platform_origin"
fi
cat AGENTS.md
cat components/cmms/README.MD
cat components/thingsboard/README.md
cat components/pdm-algorithm/AGENTS.md
cat components/pdm-algorithm/README.md
if test -f components/platform-integration/AGENTS.md; then
  cat components/platform-integration/AGENTS.md
fi
if test -f components/platform-integration/README.md; then
  cat components/platform-integration/README.md
fi
```

Expected: only the phase's known work is present, branches/remotes match the
phase map, and instructions are fresh in context; stop on unrelated changes or
an unexpected upstream.

- [ ] **Step 2: Run each component's focused red/green cycle**

Use the exact commands in the phase plan. Each RED command must first collect or
compile successfully and then fail a named behaviour assertion; repair test
harness import, collection, metadata, and compilation errors before continuing.
A component commit is forbidden if its focused suite or format check is failing.

- [ ] **Step 3: Verify the superproject before component commits**

```bash
set -Eeuo pipefail
./scripts/doctor.sh
```

Expected: zero failures. Dirty component warnings are expected while implementation is in progress and must be cleared by component commits.

- [ ] **Step 4: Commit and push component branches**

Use only the exact `git add`, commit message, and branch commands written in that phase's component task. Then verify every component touched by the phase with:

```bash
set -Eeuo pipefail
git -C components/cmms status --short --branch
git -C components/thingsboard status --short --branch
git -C components/pdm-algorithm status --short --branch
git -C components/platform-integration status --short --branch
```

Expected: each touched component is clean and its pushed SHA is reachable from its component remote. External push requires the user's authorization at execution time.

- [ ] **Step 5: Commit the coordinated superproject slice**

Use the exact root `git add` list and phase integration commit message in the final task of the phase. A Task 2-style local reviewer checkpoint is permitted only
when the detailed plan says so; it is not pushed or published. Always run:

```bash
set -Eeuo pipefail
git diff --cached --check
git diff --cached --submodule=log
./scripts/doctor.sh
```

Expected: no dirty submodule is recorded, contract/provider/consumer changes are in the same delivery slice, and the superproject can reproduce every component SHA.
Only after these checks and the final coordination commit succeed, push the named
root feature branch. Never push `main`.

## Final Acceptance

- [ ] All 20 devices have unique reversible `equipment_id ↔ tb_device_id ↔ cmms_asset_id` mappings.
- [ ] All six fixed pilot profiles pass PDM readiness hash verification.
- [ ] One accelerated 20-device prediction round fits inside a 15-minute slot.
- [ ] Risk requires two consecutive successful risky rounds; failed/skipped runs reset both counters.
- [ ] Recovery requires two consecutive healthy rounds; work-order completion alone never clears risk.
- [ ] There is at most one active PDM Alarm per stable risk key across policy and episode changes.
- [ ] ACK produces no Action API call and no CMMS work order.
- [ ] Repeated scheduling, response loss, concurrent clicks, restart, and replay produce at most one CMMS work order per approved alert.
- [ ] Approved CMMS commands are immutable and cannot be superseded by later Alarm versions.
- [ ] CMMS status reaches ThingsBoard through webhook or polling reconciliation.
- [ ] `correlation_id` traces prediction digest, artifact hash, policy, Alarm, actor, command, work order, and feedback.
- [ ] No runtime component reads another component's database.
- [ ] No runtime path uses MCP for telemetry, prediction scheduling, Alarm delivery, or work-order creation.
- [ ] No automated or batch training occurs.
- [ ] `./scripts/doctor.sh` reports zero failures and zero unexpected warnings from a clean worktree.
