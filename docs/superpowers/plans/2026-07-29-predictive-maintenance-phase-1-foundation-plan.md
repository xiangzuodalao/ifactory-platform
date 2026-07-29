# Predictive Maintenance Phase 1 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish the fifth component, the first real contracts, deterministic PDM v2 prediction from supplied history, and CMMS idempotent asset identity without enabling live cross-system writes.

**Architecture:** The first vertical slice creates provider and consumer implementations together with their contracts. PDM v2 is isolated from the legacy prediction path, CMMS adds an optional integration identity compatible with existing clients, and the new integration service initially exposes only health and typed HTTP clients.

**Tech Stack:** Python 3.12/uv/FastAPI/Pydantic/httpx/rfc8785/pytest/Ruff, Java 17/Spring Boot 3.2.3/JPA/Liquibase/JUnit 5/Mockito/Testcontainers, OpenAPI 3.1, JSON Schema 2020-12.

## Global Constraints

- Follow all constraints and fixed pilot values in `docs/superpowers/plans/2026-07-29-predictive-maintenance-master-plan.md`.
- Begin implementation with `superpowers:using-git-worktrees`. This planning commit already ignores `.worktrees/`; create `.worktrees/predictive-maintenance-integration` from `feat/predictive-maintenance-integration-design` with branch `feat/predictive-maintenance-integration`, then run `git submodule update --init` there before Task 1. If the execution environment has already supplied an isolated worktree on that exact branch, validate it instead of creating a second one.
- Do not train a model or run `components/pdm-algorithm/tests/test_cpu_train_predict_smoke.py`.
- Do not call a live ThingsBoard, CMMS, PDM, or production database in Phase 1.
- Keep contracts uncommitted until provider and consumer branches have passed their tests and been pushed.
- Existing `/measPredict/predict` and ordinary CMMS asset creation remain backward compatible.
- Phase 1 does not create CMMS assets, write ThingsBoard attributes, create Alarms, or expose Dashboard Actions.
- Every command block starts at the implementation worktree's superproject root unless the block contains its own `cd`; no block inherits another block's working directory.
- PDM v2 requires an opaque service Bearer token in `Authorization`. The PDM process receives it only as `VALEO_PDM_PREDICTION_V2_BEARER_TOKEN`; the integration service stores only `PLATFORM_INTEGRATION_PDM_CREDENTIAL_REF` and resolves the referenced secret at runtime. The tenant allowlist is an additional authorization boundary, not authentication.

---

### Task 1: Scaffold `platform-integration` and Register the Fifth Component

**Files:**

- Create: `components/platform-integration/AGENTS.md`
- Create: `components/platform-integration/.gitignore`
- Create: `components/platform-integration/.dockerignore`
- Create: `components/platform-integration/README.md`
- Create: `components/platform-integration/pyproject.toml`
- Create: `components/platform-integration/uv.lock`
- Create: `components/platform-integration/Dockerfile`
- Create: `components/platform-integration/src/platform_integration/__init__.py`
- Create: `components/platform-integration/src/platform_integration/app.py`
- Create: `components/platform-integration/src/platform_integration/config.py`
- Create: `components/platform-integration/src/platform_integration/cli.py`
- Create: `components/platform-integration/tests/test_app.py`
- Create: `components/platform-integration/tests/test_cli.py`
- Modify: `.gitmodules`
- Modify: `AGENTS.md`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/git-workflow.md`
- Modify: `docs/local-development.md`
- Modify: `scripts/bootstrap.sh`
- Modify: `scripts/doctor.sh`
- Modify: `.github/workflows/workspace-check.yml`

**Interfaces:**

- Produces: `platform_integration.app.create_app(settings: Settings | None = None) -> FastAPI`
- Produces: `GET /healthz -> {"status": "ok"}`
- Produces: `platform-integration serve --host 0.0.0.0 --port 8080` console entry point
- Produces: a fifth initialized submodule at `components/platform-integration`
- Consumes: no business API

- [ ] **Step 1: Verify the worktree, remote, and exact component branches**

Run:

```bash
set -Eeuo pipefail
test "$(git branch --show-current)" = feat/predictive-maintenance-integration
git merge-base --is-ancestor \
  feat/predictive-maintenance-integration-design HEAD
test -z "$(git status --short)"
git submodule update --init \
  components/cmms \
  components/digital-mcp \
  components/pdm-algorithm \
  components/thingsboard
platform_remote=https://github.com/xiangzuodalao/platform-integration.git
platform_main_record="$(
  git ls-remote --exit-code "$platform_remote" refs/heads/main
)"
test "$(printf '%s\n' "$platform_main_record" | wc -l)" -eq 1
platform_main_sha="$(printf '%s\n' "$platform_main_record" | cut -f1)"
printf '%s' "$platform_main_sha" | rg -q '^[0-9a-f]{40}$'
printf 'platform-integration start SHA: %s\n' "$platform_main_sha"
git submodule add \
  "$platform_remote" \
  components/platform-integration
git -C components/platform-integration fetch origin "$platform_main_sha"
git -C components/platform-integration switch -c \
  feat/predictive-maintenance-pilot "$platform_main_sha"
test "$(git -C components/platform-integration rev-parse HEAD)" = \
  "$platform_main_sha"
```

Expected: the execution worktree is already on the exact target branch with
initialized recorded submodules, and the audit prints the one full
`platform-integration` start SHA used even if remote `main` moves during the
clone. If any check fails, stop; do not create another root branch, reset a
component, or create business code directly in the superproject.

- [ ] **Step 2: Write the failing service health test**

Add:

```python
from fastapi.testclient import TestClient

from platform_integration.app import create_app


def test_healthz_is_process_only() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

Run:

```bash
set -Eeuo pipefail
uv run --directory components/platform-integration pytest \
  tests/test_app.py tests/test_cli.py -v
```

Expected: FAIL because the package and project metadata do not yet exist.

- [ ] **Step 3: Add the focused package and locked dependencies**

Use this project shape:

```toml
[build-system]
requires = ["uv_build>=0.11.30,<0.12.0"]
build-backend = "uv_build"

[project]
name = "platform-integration"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "alembic>=1.17,<2",
  "fastapi==0.125.0",
  "httpx==0.28.1",
  "psycopg[binary,pool]>=3.2,<4",
  "pydantic==2.12.5",
  "pydantic-settings>=2.11,<3",
  "rfc8785==0.1.4",
  "sqlalchemy>=2.0,<3",
  "uvicorn==0.38.0",
]

[project.scripts]
platform-integration = "platform_integration.cli:main"

[dependency-groups]
dev = [
  "pytest==9.0.2",
  "pytest-asyncio>=1.2,<2",
  "ruff==0.14.9",
  "testcontainers[postgres]>=4,<5",
]

[tool.pytest.ini_options]
addopts = "-q"
asyncio_mode = "auto"

[tool.ruff]
line-length = 100
target-version = "py312"
```

Implement the initial application exactly at the dependency boundary:

```python
from fastapi import FastAPI

from platform_integration.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    application = FastAPI(title="iFactory Platform Integration", version="0.1.0")
    application.state.settings = settings or Settings()

    @application.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()
```

`Settings` must use `env_prefix="PLATFORM_INTEGRATION_"`, reject unknown fields, and contain no default credentials.

The component-level `.gitignore` excludes `.venv/`, Python caches, pytest/Ruff
caches, coverage, local `.env*`, runtime plans/receipts, logs, and build
artifacts without hiding tracked examples. The `.dockerignore` excludes the
same material plus `.git/`, tests' runtime output, credentials, and model/data
artifacts while retaining source, migrations, lockfile, and metadata required
for a reproducible build. Tests create sentinel files and inspect both ignore
rules and the Docker build context so local secrets/caches cannot enter the
independent submodule or image.

Implement `serve` as the only runtime role in this phase. It passes the
explicit CLI host/port to Uvicorn; the Docker image creates and runs as fixed
non-root UID:GID `10001:10001` and has:

```dockerfile
CMD ["platform-integration", "serve", "--host", "0.0.0.0", "--port", "8080"]
```

`test_cli.py` asserts the parser default is port `8080`, an explicit port overrides it, and no credential is printed in help or startup logging.

Run:

```bash
set -Eeuo pipefail
cd components/platform-integration
uv lock
uv sync --frozen
uv run pytest tests/test_app.py tests/test_cli.py -v
uv run ruff check .
uv run ruff format --check .
```

Expected: one test passes; lock and format checks pass.

- [ ] **Step 4: Demonstrate the four-component governance failure**

Run from the superproject after the submodule is present:

```bash
set -Eeuo pipefail
./scripts/doctor.sh
```

Expected: FAIL because current governance declares exactly four components.

- [ ] **Step 5: Update governance to five declared components**

Add `components/platform-integration` to the component arrays in both scripts. Change doctor output from a hard-coded English number to the computed `${#COMPONENTS[@]}` value. Update root documentation, `AGENTS.md`, directory trees, CI service smoke test, and bootstrap behavior to name the fifth component without adding an `upstream` remote.

Add this CI step:

```yaml
- name: Test platform-integration skeleton
  run: >
    uv run --directory components/platform-integration --frozen
    pytest tests/test_app.py -v
```

Run:

```bash
set -Eeuo pipefail
./scripts/doctor.sh
```

Expected: zero failures; dirty-worktree warnings are expected until commits are made.

- [ ] **Step 6: Commit and push the component before the superproject**

Run:

```bash
set -Eeuo pipefail
./scripts/doctor.sh
git -C components/platform-integration add \
  .gitignore .dockerignore \
  AGENTS.md README.md pyproject.toml uv.lock Dockerfile src tests
git -C components/platform-integration commit \
  -m "chore: scaffold platform integration service"
git -C components/platform-integration push -u origin \
  feat/predictive-maintenance-pilot

git add .gitmodules AGENTS.md README.md docs scripts \
  .github/workflows/workspace-check.yml components/platform-integration
git diff --cached --check
./scripts/doctor.sh
git commit -m "chore: register platform integration component"
```

Expected: the component SHA is remotely reachable before the superproject records it; doctor then reports a clean fifth submodule.

### Task 2: Freeze Phase 1 Contracts and Examples Without Publishing Them

**Files:**

- Create: `contracts/openapi/pdm-prediction-v2.yaml`
- Create: `contracts/openapi/cmms-integration-v1.yaml`
- Create: `contracts/json-schema/equipment-mapping-v1.json`
- Create: `tests/pyproject.toml`
- Create: `tests/uv.lock`
- Create: `tests/contract/conftest.py`
- Create: `tests/contract/phase1/test_contract_documents.py`
- Create: `tests/contract/phase1/test_phase1_examples.py`
- Create: `tests/contract/phase1/fixtures/pdm-prediction-request.json`
- Create: `tests/contract/phase1/fixtures/pdm-prediction-response.json`
- Create: `tests/contract/phase1/fixtures/cmms-asset-request.json`
- Create: `tests/contract/phase1/fixtures/cmms-asset-response.json`

**Interfaces:**

- Produces: OpenAPI operation `POST /api/v2/predictions`
- Produces: CMMS operations `POST /api/assets` and `GET /api/assets/by-equipment-id/{equipment_id}`
- Produces: exact snake_case JSON examples used by provider and consumer tests
- Consumes: fixed pilot profile table from the master plan

- [ ] **Step 1: Create the test project and failing document test**

Use a `tests/pyproject.toml` with Python `>=3.12` and these locked dev dependencies:

```toml
"jsonschema>=4.25,<5"
"openapi-spec-validator==0.7.2"
"pytest==9.0.2"
"pyyaml==6.0.3"
```

The first test must load both paths and call `validate_spec()`:

```python
from pathlib import Path

import yaml
from openapi_spec_validator import validate_spec


ROOT = Path(__file__).parents[3]


def test_phase1_openapi_documents_are_valid() -> None:
    for relative in (
        "contracts/openapi/pdm-prediction-v2.yaml",
        "contracts/openapi/cmms-integration-v1.yaml",
    ):
        document = yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))
        validate_spec(document)
```

Run:

```bash
set -Eeuo pipefail
uv lock --project tests
uv run --directory tests pytest contract/phase1/test_contract_documents.py -v
```

Expected: FAIL because the OpenAPI documents do not yet exist.

- [ ] **Step 2: Write the PDM v2 contract and fixed examples**

The request schema must require:

```yaml
required:
  - tenant_id
  - correlation_id
  - equipment_id
  - model_profile_id
  - model_info_id
  - meas_code
  - unit
  - sampling_frequency
  - window_start
  - window_end
  - request_digest
  - history
```

`history[].timestamp` and `forecast[].timestamp` are strict integer UTC epoch
milliseconds. `history[].value` and `forecast[].value` are strings.
Every occurrence of `tenant_id`, `correlation_id`, or `equipment_id` uses one
shared schema with `type: string`, `format: uuid`, and this exact pattern;
`tenant_id` appears in the request only and is not added to the response:

```yaml
pattern: '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
```

Contract examples/tests cover canonical success plus uppercase, compact, braced,
numeric, and malformed failures. `model_info_id` is non-empty. Response requires
`model_artifact_sha256`, `request_digest`, and `input_digest`. Error examples
use stable codes and contain no filesystem path or upstream exception. Declare
an OpenAPI HTTP Bearer security scheme with
`bearerFormat: opaque-service-token`; every v2 operation requires it.

The valid request example uses `pilot-cnc-vibration`, `pilot-fixture-v1-cnc-vibration`, `vibration_rms`, `mm/s`, `1min`, and 66 history points. Generate its digest with the same literal example in both PDM and integration tests; do not hand-wave or omit it.

Use these fixed identities and normalized history:

```text
tenant_id       = 00000000-0000-4000-8000-000000000001
correlation_id  = 00000000-0000-4000-8000-000000000102
equipment_id    = 00000000-0000-4000-8000-000000000101
first timestamp = 1785283740000
last timestamp  = 1785287640000
window_start    = 1785283740000
window_end      = 1785287700000
data_id         = fixture-000 through fixture-065
value           = 4.00 for all 66 points
```

The request-digest projection is exactly this JSON shape before RFC 8785 encoding; array order is the stable `(timestamp,data_id)` order, every UUID is a lowercase hyphenated JSON string, and no other key is included:

```json
{
  "tenant_id": "00000000-0000-4000-8000-000000000001",
  "equipment_id": "00000000-0000-4000-8000-000000000101",
  "model_profile_id": "pilot-cnc-vibration",
  "model_info_id": "pilot-fixture-v1-cnc-vibration",
  "meas_code": "vibration_rms",
  "unit": "mm/s",
  "sampling_frequency": "1min",
  "window_start": 1785283740000,
  "window_end": 1785287700000,
  "history": [
    {
      "data_id": "fixture-000",
      "timestamp": 1785283740000,
      "value": "4.00",
      "unit": "mm/s"
    }
  ]
}
```

The displayed `history` member shows the first element; the committed fixture contains all 66 elements `fixture-000` through `fixture-065` at one-minute timestamps with the same value and unit. Its canonical SHA-256 is:

```text
067096fc115546164d8608cddadc56b185fefea9dffab4c7462c8d28011cdfbd
```

The normalized-input projection uses `buckets` entries containing only integer `timestamp` and fixed-scale `value`, plus `model_profile_id`, `model_info_id`, `meas_code`, and `preprocessing_version`. Its SHA-256 is:

```text
c67fe58c14f2e8a1ef252e6ff47b4877c0da8fa52664772dd3c08ae3d6552609
```

Provider and consumer tests must independently construct these projections and compare both literals; they must not import the other component's digest implementation.

For telemetry without a stable source ID, the `data_id` input projection is exactly:

```json
{
  "tenant_id": "00000000-0000-4000-8000-000000000001",
  "tb_device_id": "lowercase-hyphenated-thingsboard-device-uuid",
  "telemetry_key": "vibration_rms",
  "timestamp": 1785283740000,
  "value": "4.00",
  "unit": "mm/s"
}
```

The resulting `data_id` is lowercase hex SHA-256 of the RFC 8785 bytes. It does not include `equipment_id`, correlation, array index, or retrieval time.

- [ ] **Step 3: Write the CMMS asset contract and examples**

The integration asset fields are:

```yaml
equipment_id:
  type: string
  format: uuid
```

Rules:

- ordinary asset POST remains valid without `equipment_id` or `Idempotency-Key`;
- an asset POST containing `equipment_id` requires `Idempotency-Key`;
- same company/key/request digest returns the original `201` response;
- same company/key with a different body returns `409 IDEMPOTENCY_CONFLICT`;
- `GET /api/assets/by-equipment-id/{equipment_id}` is company-scoped and returns `404 ASSET_NOT_FOUND`.

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest contract/phase1 -v
```

Expected: contract syntax and examples pass. Do not commit yet; Tasks 3–5 must implement both sides first.

### Task 3: Implement Deterministic PDM Prediction v2

**Files:**

- Create: `components/pdm-algorithm/src/valeo_pdm/prediction_v2/__init__.py`
- Create: `components/pdm-algorithm/src/valeo_pdm/prediction_v2/models.py`
- Create: `components/pdm-algorithm/src/valeo_pdm/prediction_v2/normalization.py`
- Create: `components/pdm-algorithm/src/valeo_pdm/prediction_v2/catalog.py`
- Create: `components/pdm-algorithm/src/valeo_pdm/prediction_v2/predictors.py`
- Create: `components/pdm-algorithm/src/valeo_pdm/prediction_v2/service.py`
- Create: `components/pdm-algorithm/src/valeo_pdm/prediction_v2/auth.py`
- Create: `components/pdm-algorithm/src/valeo_pdm/api/prediction_v2.py`
- Create: `components/pdm-algorithm/tests/test_prediction_v2_normalization.py`
- Create: `components/pdm-algorithm/tests/test_prediction_v2_catalog.py`
- Create: `components/pdm-algorithm/tests/test_prediction_v2_service.py`
- Create: `components/pdm-algorithm/tests/test_prediction_v2_api.py`
- Create: `components/pdm-algorithm/tests/fixtures/prediction_v2/manifest.yaml`
- Modify: `components/pdm-algorithm/src/valeo_pdm/api/app.py`
- Modify: `components/pdm-algorithm/src/valeo_pdm/api/router.py`
- Modify: `components/pdm-algorithm/pyproject.toml`
- Modify: `components/pdm-algorithm/uv.lock`
- Modify: `components/pdm-algorithm/tests/test_api_startup.py`
- Modify: `components/pdm-algorithm/tests/test_distribution_contract.py`

**Interfaces:**

- Produces: `PredictionV2Service.predict(request: PredictionRequestV2, *, now: datetime) -> PredictionResponseV2`
- Produces: `POST /api/v2/predictions`
- Produces: `calculate_request_digest(request: PredictionRequestV2) -> str`
- Produces: `ModelCatalog.resolve(tenant_id, model_profile_id, model_info_id, meas_code) -> ResolvedModel`
- Consumes: `rfc8785==0.1.4` and a read-only fixture/catalog

- [ ] **Step 1: Branch from the recorded PDM gitlink**

Run:

```bash
set -Eeuo pipefail
git -C components/pdm-algorithm fetch origin chore/pdm-cpu-image
git -C components/pdm-algorithm merge-base --is-ancestor \
  e4dd47bb28002bc83be8a4e2d13906a2295ae678 \
  origin/chore/pdm-cpu-image
git -C components/pdm-algorithm switch -c \
  feat/pdm-prediction-v2 \
  e4dd47bb28002bc83be8a4e2d13906a2295ae678
```

Expected: the branch contains the CPU/safety work recorded by the superproject. Do not branch from stale `main`.

- [ ] **Step 2: Write normalization tests and verify RED**

Tests must assert fixed literals for:

```python
assert canonical_decimal("-0.000", scale=2) == "0.00"
assert canonical_decimal("1.245", scale=2) == "1.24"
assert canonical_decimal("1.255", scale=2) == "1.26"
```

Also cover exponent/NaN/infinity rejection, Unicode NFC units, UTC epoch milliseconds, `(timestamp, data_id)` ordering, exclusion of `correlation_id` and `request_digest`, duplicate timestamp HALF_EVEN mean, explicit `null` buckets, 10% missing limit, two-consecutive-missing limit, future timestamps, and exact `request_digest`/`input_digest` literals.

Run:

```bash
set -Eeuo pipefail
cd components/pdm-algorithm
uv run pytest tests/test_prediction_v2_normalization.py -v
```

Expected: FAIL because `valeo_pdm.prediction_v2` does not exist.

- [ ] **Step 3: Implement strict models and normalization**

Use strict Pydantic models. JSON represents UUIDs as strings, so keep the model
strict for every other field but opt the UUID fields into parsing only after a
`mode="before"` validator has proved that the input is a canonical lowercase,
hyphenated UUID string:

```python
CANONICAL_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$"
)
CanonicalUuid = Annotated[UUID, Field(strict=False)]


class HistoryPointV2(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    data_id: str
    timestamp: int
    value: str
    unit: str


class PredictionRequestV2(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tenant_id: CanonicalUuid
    correlation_id: CanonicalUuid
    equipment_id: CanonicalUuid
    model_profile_id: str
    model_info_id: str
    meas_code: str
    unit: str
    sampling_frequency: str
    window_start: int
    window_end: int
    request_digest: str
    history: list[HistoryPointV2]

    @field_validator(
        "tenant_id", "correlation_id", "equipment_id", mode="before"
    )
    @classmethod
    def require_canonical_uuid_string(cls, value: object) -> object:
        if isinstance(value, UUID):
            return value
        if type(value) is not str or CANONICAL_UUID_RE.fullmatch(value) is None:
            raise ValueError("canonical lowercase hyphenated UUID required")
        return value


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    model_profile_id: str
    model_info_id: str
    meas_code: str
    unit: str
    value_scale: int
    sampling_frequency: str
    request_window_points: int
    context_points: int
    horizon_points: int
    preprocessing_version: str
```

For every pilot profile, `request_window_points=66`, `context_points=60`, and `horizon_points=15`.
Provider tests must submit actual JSON string UUIDs and receive `200`; uppercase
text, compact text, braced text, numeric JSON values, and non-UUID strings must
be rejected at the HTTP boundary. The model also accepts an existing `UUID`
instance so the service can construct a response from an already validated
request without stringifying and reparsing it. Apply the same validator to UUID
fields in every v2 response model.

Implement Decimal normalization before any float conversion. Require aligned integer bounds with `window_start < window_end`, require exactly `profile.request_window_points` intervals, and reject history outside `[window_start, window_end)`. Convert UUID fields to lowercase hyphenated JSON strings before `rfc8785.dumps(projection)`; never pass Python `UUID` objects from `model_dump(mode="python")` to the canonicalizer. Compute `request_digest` from the documented projection, including both window bounds, and compute `input_digest` only after duplicate aggregation and insertion of every expected bucket, including leading/trailing `null` buckets. Never call legacy `clean_and_resample_timeseries()` for digest construction.

Run:

```bash
set -Eeuo pipefail
uv add --project components/pdm-algorithm rfc8785==0.1.4
uv run --directory components/pdm-algorithm --frozen pytest \
  tests/test_prediction_v2_normalization.py -v
```

Expected: all normalization tests pass and `uv.lock` changes with the direct dependency.

- [ ] **Step 4: Write catalog and repeat-last service tests**

Tests must use a temporary real manifest/artifact and assert:

- exact tenant/profile/model/meas match;
- duplicate identities, absolute paths, `..`, escaping symlinks, missing files, and wrong hashes fail closed;
- the predictor repeats the last non-null normalized value for 15 future one-minute buckets;
- changing the last valid history value changes forecast;
- identical artifact/input produces identical forecast and digest;
- no `.pt`, trainer, DB, CSV, or network access occurs.

Run:

```bash
set -Eeuo pipefail
uv run --directory components/pdm-algorithm --frozen pytest \
  tests/test_prediction_v2_catalog.py \
  tests/test_prediction_v2_service.py -v
```

Expected: FAIL because catalog, predictor, and service are absent.

- [ ] **Step 5: Implement catalog, predictor, and service boundaries**

Use these signatures:

```python
class RepeatLastFixturePredictor:
    def predict(
        self,
        normalized: NormalizedInput,
        profile: ModelProfile,
    ) -> tuple[ForecastPointV2, ...]:
        last_value = next(value for value in reversed(normalized.values) if value is not None)
        return tuple(
            ForecastPointV2(
                timestamp=normalized.last_bucket_ms + profile.interval_ms * index,
                value=last_value,
                unit=profile.unit,
            )
            for index in range(1, profile.horizon_points + 1)
        )
```

`ModelCatalog.resolve()` rejects any non-exact tenant/profile/model/meas combination. `PredictionV2Service.predict()` canonicalizes raw timestamp/unit/Decimal fields and verifies `request_digest` before duplicate aggregation, bucket reconstruction, or resampling. It returns both digests and artifact hash and never returns a risk level or maintenance recommendation.

Run the two tests again; expected PASS.

- [ ] **Step 6: Write API provider tests and verify RED**

Test:

- valid root contract-shaped request returns `200`;
- missing, malformed, or wrong service Bearer token returns the same `401` response and does not resolve a tenant/model;
- malformed decimal and digest mismatch return stable `422`/`409` codes;
- unknown exact model returns `404 MODEL_NOT_FOUND`;
- service exceptions never expose an absolute path;
- monkeypatched DB/CSV readers fail the test if called;
- legacy `/measPredict/predict` remains present.

Run:

```bash
set -Eeuo pipefail
uv run --directory components/pdm-algorithm --frozen pytest \
  tests/test_prediction_v2_api.py tests/test_api_startup.py -v
```

Expected: `/api/v2/predictions` returns 404.

- [ ] **Step 7: Add the router and sanitize legacy errors**

Create a separate `APIRouter(prefix="/api/v2")`, include it in `app.py`, and inject `PredictionV2Service` through a dependency that tests can override. Authenticate `Authorization: Bearer` with a constant-time comparison against the non-empty `VALEO_PDM_PREDICTION_V2_BEARER_TOKEN` before resolving tenant/profile; do not log or return the token. Keep `/healthz` process-only. When no runtime catalog is configured, v2 returns `503 PREDICTION_CATALOG_NOT_READY`.

Replace legacy responses containing checkpoint paths or `str(exc)` with stable safe codes while retaining status behavior.

Run:

```bash
set -Eeuo pipefail
uv run --directory components/pdm-algorithm --frozen pytest \
  --ignore=tests/test_cpu_train_predict_smoke.py \
  tests/test_prediction_v2_normalization.py \
  tests/test_prediction_v2_catalog.py \
  tests/test_prediction_v2_service.py \
  tests/test_prediction_v2_api.py \
  tests/test_api_startup.py \
  tests/test_training_api_mvp.py \
  tests/test_data_pipeline.py \
  tests/test_config_resolution.py \
  tests/test_training_registry.py -v
uv run --directory components/pdm-algorithm --frozen ruff check .
uv run --directory components/pdm-algorithm --frozen ruff format --check .
uv run --directory components/pdm-algorithm --frozen pytest \
  tests/test_distribution_contract.py -v
uv sync --directory components/pdm-algorithm --frozen
```

Expected: all listed tests/checks pass without starting training.

- [ ] **Step 8: Commit and push PDM**

Run:

```bash
set -Eeuo pipefail
./scripts/doctor.sh
git -C components/pdm-algorithm add \
  pyproject.toml uv.lock \
  src/valeo_pdm/api \
  src/valeo_pdm/prediction_v2 \
  tests/test_prediction_v2_normalization.py \
  tests/test_prediction_v2_catalog.py \
  tests/test_prediction_v2_service.py \
  tests/test_prediction_v2_api.py \
  tests/test_api_startup.py \
  tests/test_distribution_contract.py \
  tests/fixtures/prediction_v2
git -C components/pdm-algorithm commit \
  -m "feat: add deterministic history prediction API"
git -C components/pdm-algorithm push -u origin feat/pdm-prediction-v2
```

Expected: a remotely reachable PDM provider commit; no checkpoint or artifact is staged.

### Task 4: Add CMMS Equipment Identity and Idempotent Asset Creation

**Files:**

- Create: `components/cmms/api/src/main/java/com/grash/model/IntegrationIdempotencyRecord.java`
- Create: `components/cmms/api/src/main/java/com/grash/model/enums/IntegrationOperation.java`
- Create: `components/cmms/api/src/main/java/com/grash/repository/IntegrationIdempotencyRecordRepository.java`
- Create: `components/cmms/api/src/main/java/com/grash/service/CanonicalRequestDigestService.java`
- Create: `components/cmms/api/src/main/java/com/grash/service/IntegrationIdempotencyService.java`
- Create: `components/cmms/api/src/main/java/com/grash/service/AssetIntegrationService.java`
- Create: `components/cmms/api/src/test/java/com/grash/controller/AssetControllerTest.java`
- Create: `components/cmms/api/src/test/java/com/grash/integration/AssetIntegrationTest.java`
- Create: `components/cmms/api/src/test/java/com/grash/service/AssetIntegrationServiceTest.java`
- Create: `components/cmms/api/src/test/java/com/grash/service/IntegrationIdempotencyServiceTest.java`
- Create: `components/cmms/api/src/main/resources/db/changelog/2026_07_29_00000000001_add_asset_integration_identity.xml`
- Modify: `components/cmms/api/src/main/resources/db/master.xml`
- Modify: `components/cmms/api/src/main/java/com/grash/model/Asset.java`
- Modify: `components/cmms/api/src/main/java/com/grash/dto/AssetPostDTO.java`
- Modify: `components/cmms/api/src/main/java/com/grash/dto/AssetShowDTO.java`
- Modify: `components/cmms/api/src/main/java/com/grash/repository/AssetRepository.java`
- Modify: `components/cmms/api/src/main/java/com/grash/service/AssetService.java`
- Modify: `components/cmms/api/src/main/java/com/grash/controller/AssetController.java`

**Interfaces:**

- Produces: `Optional<Asset> findByEquipmentIdAndCompany_Id(UUID equipmentId, Long companyId)`
- Produces: `AssetShowDTO AssetIntegrationService.create(AssetPostDTO request, User user, @Nullable String idempotencyKey)`
- Produces: `<T> T IntegrationIdempotencyService.execute(Company company, IntegrationOperation operation, String key, String requestDigest, Class<T> responseType, Supplier<T> command)`
- Produces: `GET /api/assets/by-equipment-id/{equipment_id}`
- Produces: idempotent integration-mode `POST /api/assets`
- Consumes: `Idempotency-Key: pilot-asset:{tb_device_id}`

- [ ] **Step 1: Branch CMMS and write controller/service tests**

Run:

```bash
set -Eeuo pipefail
git -C components/cmms fetch origin main
git -C components/cmms merge-base --is-ancestor \
  3d9b0765f26d83c0f1eb511717563b59084f8bd8 \
  origin/main
git -C components/cmms switch -c \
  feat/predictive-maintenance-integration \
  3d9b0765f26d83c0f1eb511717563b59084f8bd8
```

Tests must assert:

- existing asset POST without integration fields remains valid;
- `equipment_id` without the header returns `400 IDEMPOTENCY_KEY_REQUIRED`;
- same company/key/body returns the same status/body and one asset;
- same key/different body returns `409 IDEMPOTENCY_CONFLICT`;
- write-response loss followed by retry returns the existing asset;
- same `equipment_id` in another company is allowed;
- duplicate `equipment_id` inside one company fails;
- GET is company-scoped and cannot reveal another company's asset.

Run:

```bash
set -Eeuo pipefail
cd components/cmms/api
mvn -Dtest='AssetControllerTest,AssetIntegrationTest,AssetIntegrationServiceTest,IntegrationIdempotencyServiceTest' test
```

Expected: FAIL because fields, endpoint, migration, and service do not exist.

- [ ] **Step 2: Add the Liquibase schema**

The migration must:

```xml
<addColumn tableName="asset">
    <column name="equipment_id" type="uuid"/>
</addColumn>
<addUniqueConstraint
    tableName="asset"
    columnNames="company_id,equipment_id"
    constraintName="uk_asset_company_equipment"/>
```

Create `integration_idempotency_record` with company, operation, idempotency key, request SHA-256, HTTP status, response JSON, entity type/ID, timestamps, and a unique `(company_id, operation, idempotency_key)` constraint. Add a rollback that drops the new table, unique constraint, and asset column. Include the migration once at the end of `db/master.xml`.

- [ ] **Step 3: Implement the company-scoped identity**

Add to `Asset`:

```java
@Column(name = "equipment_id")
@JsonProperty("equipment_id")
private UUID equipmentId;
```

Add:

```java
Optional<Asset> findByEquipmentIdAndCompany_Id(UUID equipmentId, Long companyId);
```

The GET controller resolves the authenticated user's company first and maps absence to `404 ASSET_NOT_FOUND`.

- [ ] **Step 4: Implement idempotency and recovery ordering**

Use stable operations `CREATE_ASSET` and later `CREATE_WORK_ORDER`. Canonicalize request JSON with alphabetic property/map ordering and SHA-256. The asset integration flow is:

```text
authenticate and authorize
→ validate key and request digest
→ lookup company + equipment_id
→ replay/repair idempotency record when asset already exists
→ reserve company + operation + key
→ create asset and persist original serialized response atomically
```

Serialize concurrent requests with a PostgreSQL transaction advisory lock derived from company, operation, and key, or a locked pre-existing record plus the database unique constraint. A plain find-then-save sequence is forbidden. On a concurrent unique-key conflict, open a new read transaction and return the completed original response; never invoke `AssetService.create()` a second time. An existing key with another digest returns 409. Ordinary asset requests bypass this integration wrapper.

`AssetIntegrationTest` uses the project's PostgreSQL/Testcontainers support to
apply the real Liquibase change, exercise company-scoped create/replay/conflict
and response-loss recovery over HTTP, and verify the unique constraint. Run
the focused Maven test again; expected PASS.

- [ ] **Step 5: Run CMMS regression and commit**

Run:

```bash
set -Eeuo pipefail
cd components/cmms/api
mvn -Dtest='AssetControllerTest,AssetIntegrationTest,AssetIntegrationServiceTest,IntegrationIdempotencyServiceTest,WorkOrderControllerTest,WorkOrderServiceTest,WorkOrderIntegrationTest' test
cd ../../..
./scripts/doctor.sh
```

Expected: all selected tests pass; doctor has zero failures.

Commit and push:

```bash
set -Eeuo pipefail
git -C components/cmms add api/src/main api/src/test
git -C components/cmms commit \
  -m "feat: add idempotent external asset identity"
git -C components/cmms push -u origin feat/predictive-maintenance-integration
```

### Task 5: Implement Phase 1 Integration Consumers

**Files:**

- Create: `components/platform-integration/src/platform_integration/contracts/__init__.py`
- Create: `components/platform-integration/src/platform_integration/contracts/canonical.py`
- Create: `components/platform-integration/src/platform_integration/contracts/pdm.py`
- Create: `components/platform-integration/src/platform_integration/contracts/cmms.py`
- Create: `components/platform-integration/src/platform_integration/clients/__init__.py`
- Create: `components/platform-integration/src/platform_integration/clients/pdm.py`
- Create: `components/platform-integration/src/platform_integration/clients/cmms.py`
- Create: `components/platform-integration/src/platform_integration/credentials.py`
- Create: `components/platform-integration/tests/contracts/test_pdm_contract.py`
- Create: `components/platform-integration/tests/contracts/test_cmms_asset_contract.py`
- Create: `components/platform-integration/tests/clients/test_pdm_client.py`
- Create: `components/platform-integration/tests/clients/test_cmms_client.py`

**Interfaces:**

- Produces: `PdmClient.predict(request: PredictionRequestV2) -> PredictionResponseV2`
- Produces: `CmmsClient.find_asset_by_equipment_id(equipment_id: UUID) -> CmmsAsset | None`
- Produces: `CmmsClient.create_asset(request: CmmsAssetCreate, *, idempotency_key: str) -> CmmsAsset`
- Consumes: Phase 1 PDM and CMMS OpenAPI operations

- [ ] **Step 1: Write typed client tests with `httpx.MockTransport`**

PDM tests assert exact snake_case JSON, request digest preservation, `Authorization: Bearer` from the configured credential reference, timeout mapping to `PDM_UNAVAILABLE`, rejection of response digest/model identity mismatch, and absence of the token from exceptions/logs.

CMMS tests assert GET-before-POST, exact `Idempotency-Key`, same-body replay, `409` conflict mapping, and that a POST timeout causes the caller-visible `CMMS_WRITE_RESULT_UNKNOWN` error instead of an automatic second POST.

Run:

```bash
set -Eeuo pipefail
cd components/platform-integration
uv run pytest tests/contracts tests/clients -v
```

Expected: FAIL because contract models and clients are absent.

- [ ] **Step 2: Implement exact models and canonical request building**

Define Pydantic models with `extra="forbid"` and strict values. Mirror the
provider's `CanonicalUuid = Annotated[UUID, Field(strict=False)]` plus
`mode="before"` validator for every UUID field: it accepts existing `UUID`
instances for internal construction and otherwise requires a canonical
lowercase-hyphenated string; this is the sole field-level exception to strict
parsing. Contract and MockTransport tests must validate real JSON string UUIDs
and reject uppercase, compact, braced, numeric JSON, and malformed values. In particular,
`PredictionResponseV2.model_validate(response.json())` must accept the real JSON
string UUIDs returned by PDM. Build the same request projection as PDM from
JSON-mode values so every UUID is a lowercase hyphenated string before
`rfc8785.dumps()`. Verify response `correlation_id`, `equipment_id`,
`model_profile_id`, `model_info_id`, `meas_code`, and `request_digest` identity
before returning it to a caller. The response does not contain `tenant_id`.

`Settings.pdm_credential_ref` is required for PDM use. A credential reference must match `^[A-Z][A-Z0-9_]{2,127}$` and is the exact environment-variable name; `EnvironmentCredentialProvider.get(ref)` performs one direct, non-interpolating environment lookup and parses a strict no-extra-fields JSON envelope. For PDM the only accepted shape is `{"kind":"opaque_bearer","value":"non-empty secret"}`. `PdmClient` injects `value` into `Authorization: Bearer` without persisting or logging either the envelope or token.

Use:

```python
class PdmClient:
    async def predict(self, request: PredictionRequestV2) -> PredictionResponseV2:
        credential = self._credentials.get(self._pdm_credential_ref)
        response = await self._http.post(
            "/api/v2/predictions",
            headers={"Authorization": f"Bearer {credential.value.get_secret_value()}"},
            json=request.model_dump(mode="json"),
        )
        response.raise_for_status()
        result = PredictionResponseV2.model_validate(response.json())
        request.assert_matching_response(result)
        return result
```

The CMMS client never retries POST internally. Its caller must reconcile through `find_asset_by_equipment_id()` first.

- [ ] **Step 3: Verify, commit, and push the consumer**

Run:

```bash
set -Eeuo pipefail
uv run --directory components/platform-integration --frozen pytest \
  tests/test_app.py tests/contracts tests/clients -v
uv run --directory components/platform-integration --frozen ruff check .
uv run --directory components/platform-integration --frozen ruff format --check .
uv sync --directory components/platform-integration --frozen
```

Expected: all tests and checks pass.

Commit:

```bash
set -Eeuo pipefail
./scripts/doctor.sh
git -C components/platform-integration add src tests pyproject.toml uv.lock
git -C components/platform-integration commit \
  -m "feat: add PDM and CMMS contract clients"
git -C components/platform-integration push
```

### Task 6: Complete the Coordinated Phase 1 Gate

**Files:**

- Modify: `components/pdm-algorithm` gitlink
- Modify: `components/cmms` gitlink
- Modify: `components/platform-integration` gitlink
- Modify: `contracts/README.md`
- Modify: `tests/README.md`
- Modify: `.github/workflows/workspace-check.yml`

**Interfaces:**

- Consumes: pushed provider and consumer SHAs from Tasks 3–5
- Produces: one reproducible superproject commit containing contracts, examples, tests, and gitlinks

- [ ] **Step 1: Add contract tests to CI**

Add:

```yaml
- name: Validate Phase 1 integration contracts
  run: >
    uv run --directory tests --frozen
    pytest contract/phase1 -v
```

Document contract owners and compatibility:

```text
pdm-prediction-v2: provider=PDM, consumer=platform-integration, additive v2
cmms-integration-v1: provider=CMMS, consumer=platform-integration, additive fields/endpoints
```

- [ ] **Step 2: Run fresh provider, consumer, and platform verification**

Run:

```bash
set -Eeuo pipefail
uv run --directory components/pdm-algorithm --frozen pytest \
  --ignore=tests/test_cpu_train_predict_smoke.py \
  tests/test_prediction_v2_normalization.py \
  tests/test_prediction_v2_catalog.py \
  tests/test_prediction_v2_service.py \
  tests/test_prediction_v2_api.py -v

mvn -f components/cmms/api/pom.xml \
  -Dtest='AssetControllerTest,IntegrationIdempotencyServiceTest,AssetIntegrationTest' test

uv run --directory components/platform-integration --frozen pytest \
  tests/test_app.py tests/contracts tests/clients -v

uv run --directory tests --frozen pytest contract/phase1 -v
./scripts/doctor.sh
```

Expected: every command exits zero. No command invokes training or a live service.

- [ ] **Step 3: Verify exact staged scope and commit Phase 1**

Run:

```bash
set -Eeuo pipefail
git add \
  components/pdm-algorithm \
  components/cmms \
  components/platform-integration \
  contracts/openapi/pdm-prediction-v2.yaml \
  contracts/openapi/cmms-integration-v1.yaml \
  contracts/json-schema/equipment-mapping-v1.json \
  contracts/README.md \
  tests/pyproject.toml \
  tests/uv.lock \
  tests/contract/phase1 \
  tests/README.md \
  .github/workflows/workspace-check.yml
git diff --cached --check
git diff --cached --submodule=log
./scripts/doctor.sh
git commit -m "feat: add predictive maintenance contract foundation"
```

Expected: three clean gitlinks point to pushed commits; Phase 1 contains no deployment that can perform a live write.
