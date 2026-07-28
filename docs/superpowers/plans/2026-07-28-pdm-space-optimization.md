# PDM Space Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reclaim unused PDM build/runtime environment space and replace the 15.58 GB CUDA-heavy image with a reproducible CPU image without losing runtime state or breaking the PDM API/MCP path.

**Architecture:** Operational cleanup is performed first against exact legacy process and cache targets while the current container remains healthy. PDM packaging is then changed in an isolated component worktree: a contract test drives a minimal production dependency set, a CPU-only uv lock, and a default non-GPU Docker/Compose contract. A separately tagged candidate image is tested on port 10022 before the current container is switched with an explicit rollback tag.

**Tech Stack:** Python 3.12, uv, PyTorch CPU wheels, FastAPI/Uvicorn, pytest, Ruff, Docker/Compose, Git submodules.

## Global Constraints

- Preserve `/home/vm/code/PDM_Algorithm/configs`, `/home/vm/code/PDM_Algorithm/data`, and `/home/vm/code/PDM_Algorithm/artifacts` byte-for-byte.
- Never run `docker system prune -a`; only prune verified unused build cache.
- Terminate only processes whose command line points to `/home/vm/code/PDM_Algorithm/integrations/pdm_mcp`.
- Do not terminate MCP processes whose command line points to `components/digital-mcp`.
- The default production image is CPU-only and must not resolve NVIDIA, CUDA, Triton, xLSTM, or TorchVision packages.
- The existing `/healthz`, `/measPredict/*`, CLI, training, prediction, PostgreSQL, and SQL Server interfaces remain unchanged.
- Default Uvicorn worker count is exactly 1.
- Long-running/full production training is out of scope; use tests and a bounded smoke path.
- Component code commits go to the PDM component branch before the platform repository updates its submodule pointer.

---

### Task 1: Reclaim verified unused Docker build cache

**Files:**
- Create: none
- Modify: none
- Test: runtime health and disk accounting commands

**Interfaces:**
- Consumes: running container `pdm_algorithm-valeo-pdm-api-1` and health endpoint `http://127.0.0.1:10021/healthz`
- Produces: before/after disk measurements with the running PDM image and container intact

- [x] **Step 1: Capture baseline container, disk, and runtime-state checksums**

Run:

```bash
PDM_AUDIT_DIR=/tmp/pdm-space-audit-20260728
test ! -e "$PDM_AUDIT_DIR"
install -d -m 0700 "$PDM_AUDIT_DIR"
docker inspect --format 'status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}not-configured{{end}} image={{.Image}}' pdm_algorithm-valeo-pdm-api-1 | tee "$PDM_AUDIT_DIR/container.before"
curl --fail --silent --show-error http://127.0.0.1:10021/healthz | tee "$PDM_AUDIT_DIR/health.before"
df -B1 / | tee "$PDM_AUDIT_DIR/df.before"
docker system df | tee "$PDM_AUDIT_DIR/docker.before"
find /home/vm/code/PDM_Algorithm/configs /home/vm/code/PDM_Algorithm/data /home/vm/code/PDM_Algorithm/artifacts -type f -print0 |
  sort -z |
  xargs -0 sha256sum > "$PDM_AUDIT_DIR/runtime.sha256.before"
```

Expected: container reports `running` and `healthy`; health returns `{"status":"ok"}`.

- [x] **Step 2: Prune only unused build cache older than 24 hours**

Run:

```bash
PDM_AUDIT_DIR=/tmp/pdm-space-audit-20260728
docker builder prune --filter 'until=24h' --force | tee "$PDM_AUDIT_DIR/builder-prune.log"
```

Expected: command exits 0 and reports reclaimed build-cache bytes; it does not remove images, containers, networks, or volumes.

- [x] **Step 3: Verify the service and runtime state after pruning**

Run:

```bash
PDM_AUDIT_DIR=/tmp/pdm-space-audit-20260728
docker inspect --format 'status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}not-configured{{end}} image={{.Image}}' pdm_algorithm-valeo-pdm-api-1 | tee "$PDM_AUDIT_DIR/container.after-prune"
curl --fail --silent --show-error http://127.0.0.1:10021/healthz | tee "$PDM_AUDIT_DIR/health.after-prune"
df -B1 / | tee "$PDM_AUDIT_DIR/df.after-prune"
docker system df | tee "$PDM_AUDIT_DIR/docker.after-prune"
find /home/vm/code/PDM_Algorithm/configs /home/vm/code/PDM_Algorithm/data /home/vm/code/PDM_Algorithm/artifacts -type f -print0 |
  sort -z |
  xargs -0 sha256sum > "$PDM_AUDIT_DIR/runtime.sha256.after-prune"
cmp "$PDM_AUDIT_DIR/runtime.sha256.before" "$PDM_AUDIT_DIR/runtime.sha256.after-prune"
```

Expected: health remains `ok`, checksums match, and free disk space is not lower than the baseline.

---

### Task 2: Retire legacy MCP processes and Python environments

**Files:**
- Delete: `/home/vm/code/_legacy/pre-ifactory-platform-20260728/PDM_Algorithm/.venv`
- Delete: `/home/vm/code/_legacy/pre-ifactory-platform-20260728/PDM_Algorithm/integrations/pdm_mcp/.venv`
- Modify: global uv cache only through `uv cache prune`
- Test: process inventory, new MCP smoke, runtime-state checksums

**Interfaces:**
- Consumes: legacy MCP command prefix and the project `digital-platform` MCP configuration
- Produces: zero legacy MCP processes, intact new MCP processes, and no legacy PDM virtual environments

- [x] **Step 1: Record exact old and new MCP process sets**

Run:

```bash
PDM_AUDIT_DIR=/tmp/pdm-space-audit-20260728
ps -eo pid=,comm=,args= |
  awk '$2 == "uv" || $2 == "pdm-mcp"' |
  awk 'index($0, "/home/vm/code/PDM_Algorithm/integrations/pdm_mcp")' |
  tee "$PDM_AUDIT_DIR/legacy-mcp.before"
ps -eo pid=,comm=,args= |
  awk '$2 == "uv" || $2 == "pdm-mcp"' |
  awk 'index($0, "components/digital-mcp")' |
  tee "$PDM_AUDIT_DIR/digital-mcp.before"
```

Expected: old process list contains only `uv`/`pdm-mcp` commands under the legacy integration path; new list contains project `components/digital-mcp` commands.

- [x] **Step 2: Send SIGTERM only to exact legacy uv parent processes**

Run:

```bash
mapfile -t LEGACY_UV_PIDS < <(
  ps -eo pid=,comm=,args= |
    awk '$2 == "uv" &&
         index($0, "/home/vm/code/PDM_Algorithm/integrations/pdm_mcp") {
           print $1
         }'
)
if ((${#LEGACY_UV_PIDS[@]})); then
  kill -TERM "${LEGACY_UV_PIDS[@]}"
fi
for _ in $(seq 1 20); do
  if ! ps -eo pid=,comm=,args= |
    awk '($2 == "uv" || $2 == "pdm-mcp") &&
         index($0, "/home/vm/code/PDM_Algorithm/integrations/pdm_mcp")' |
    grep -q .; then
    break
  fi
  sleep 0.25
done
mapfile -t LEGACY_REMAINING_PIDS < <(
  ps -eo pid=,comm=,args= |
    awk '($2 == "uv" || $2 == "pdm-mcp") &&
         index($0, "/home/vm/code/PDM_Algorithm/integrations/pdm_mcp") {
           print $1
         }'
)
if ((${#LEGACY_REMAINING_PIDS[@]})); then
  kill -KILL "${LEGACY_REMAINING_PIDS[@]}"
fi
```

Expected: only exact legacy-path processes receive signals.

- [x] **Step 3: Verify the old MCP is gone and the new PDM control path remains usable**

Run:

```bash
PDM_AUDIT_DIR=/tmp/pdm-space-audit-20260728
if ps -eo pid=,comm=,args= |
  awk '($2 == "uv" || $2 == "pdm-mcp") &&
       index($0, "/home/vm/code/PDM_Algorithm/integrations/pdm_mcp")' |
  grep -q .; then
  exit 1
fi
ps -eo pid=,comm=,args= |
  awk '($2 == "uv" || $2 == "pdm-mcp") &&
       index($0, "components/digital-mcp")' |
  tee "$PDM_AUDIT_DIR/digital-mcp.after"
test -s "$PDM_AUDIT_DIR/digital-mcp.after"
curl --fail --silent --show-error http://127.0.0.1:10021/healthz
```

Then run an ephemeral Codex MCP probe from the platform root:

```bash
codex exec --ephemeral --skip-git-repo-check -C /home/vm/code/ifactory-platform \
  'Call the digital-platform pdm_health_check tool exactly once and return only its JSON result.'
```

Expected: no legacy process matches; at least one new MCP process remains; the tool result reports healthy.

- [x] **Step 4: Resolve and validate the two exact deletion targets**

Run:

```bash
LEGACY_PDM_VENV=/home/vm/code/_legacy/pre-ifactory-platform-20260728/PDM_Algorithm/.venv
LEGACY_MCP_VENV=/home/vm/code/_legacy/pre-ifactory-platform-20260728/PDM_Algorithm/integrations/pdm_mcp/.venv
test "$(realpath -m "$LEGACY_PDM_VENV")" = "$LEGACY_PDM_VENV"
test "$(realpath -m "$LEGACY_MCP_VENV")" = "$LEGACY_MCP_VENV"
test -d "$LEGACY_PDM_VENV"
test -d "$LEGACY_MCP_VENV"
```

Expected: both paths resolve exactly to the approved legacy archive and are directories.

- [x] **Step 5: Delete only the approved legacy environments**

Run:

```bash
LEGACY_PDM_VENV=/home/vm/code/_legacy/pre-ifactory-platform-20260728/PDM_Algorithm/.venv
LEGACY_MCP_VENV=/home/vm/code/_legacy/pre-ifactory-platform-20260728/PDM_Algorithm/integrations/pdm_mcp/.venv
rm -rf -- "$LEGACY_PDM_VENV"
rm -rf -- "$LEGACY_MCP_VENV"
test ! -e "$LEGACY_PDM_VENV"
test ! -e "$LEGACY_MCP_VENV"
```

Expected: both environments are absent; legacy source, Git data, configs, data, and artifacts remain.

- [x] **Step 6: Prune dangling uv cache entries and verify runtime state**

Run:

```bash
PDM_AUDIT_DIR=/tmp/pdm-space-audit-20260728
du -x -s -B1 /home/vm/.cache/uv | tee "$PDM_AUDIT_DIR/uv-cache.before-prune"
uv cache prune | tee "$PDM_AUDIT_DIR/uv-cache-prune.log"
du -x -s -B1 /home/vm/.cache/uv | tee "$PDM_AUDIT_DIR/uv-cache.after-prune"
find /home/vm/code/PDM_Algorithm/configs /home/vm/code/PDM_Algorithm/data /home/vm/code/PDM_Algorithm/artifacts -type f -print0 |
  sort -z |
  xargs -0 sha256sum > "$PDM_AUDIT_DIR/runtime.sha256.after-env-clean"
cmp "$PDM_AUDIT_DIR/runtime.sha256.before" "$PDM_AUDIT_DIR/runtime.sha256.after-env-clean"
curl --fail --silent --show-error http://127.0.0.1:10021/healthz
```

Expected: uv reports pruned entries, checksums match, and PDM remains healthy.

---

### Task 3: Create the isolated PDM component worktree

**Files:**
- Create worktree: `/home/vm/.config/superpowers/worktrees/PDM_Algorithm/pdm-cpu-image`
- Create branch: `chore/pdm-cpu-image`
- Modify: none
- Test: component baseline tests in the new worktree

**Interfaces:**
- Consumes: component commit `9593c485c1a1b8488af7753ac8352e225d4e445b`
- Produces: named, isolated component branch for Tasks 4–7

- [x] **Step 1: Use `superpowers:using-git-worktrees` to create the worktree**

Run the skill-prescribed repository detection, then:

```bash
mkdir -p /home/vm/.config/superpowers/worktrees/PDM_Algorithm
git -C /home/vm/code/ifactory-platform/components/pdm-algorithm \
  worktree add \
  /home/vm/.config/superpowers/worktrees/PDM_Algorithm/pdm-cpu-image \
  -b chore/pdm-cpu-image \
  9593c485c1a1b8488af7753ac8352e225d4e445b
```

Expected: the worktree is on `chore/pdm-cpu-image`, and the platform submodule remains detached at its original gitlink.

- [x] **Step 2: Confirm clean worktree and component instructions**

Run:

```bash
git -C /home/vm/.config/superpowers/worktrees/PDM_Algorithm/pdm-cpu-image status --short --branch
sed -n '1,260p' /home/vm/.config/superpowers/worktrees/PDM_Algorithm/pdm-cpu-image/AGENTS.md
```

Expected: clean named branch with the same component rules already captured in this plan.

---

### Task 4: Drive the minimal CPU dependency contract with TDD

**Files:**
- Create: `/home/vm/.config/superpowers/worktrees/PDM_Algorithm/pdm-cpu-image/tests/test_distribution_contract.py`
- Modify: `/home/vm/.config/superpowers/worktrees/PDM_Algorithm/pdm-cpu-image/pyproject.toml`
- Modify: `/home/vm/.config/superpowers/worktrees/PDM_Algorithm/pdm-cpu-image/uv.lock`

**Interfaces:**
- Consumes: Python `tomllib` and the approved CPU dependency design
- Produces: `runtime_dependency_names() -> set[str]`, CPU Torch uv source metadata, minimal runtime dependencies, and dev-only pytest/Ruff

- [x] **Step 1: Write the failing dependency contract test**

Create `tests/test_distribution_contract.py` with:

```python
from __future__ import annotations

import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def project_config() -> dict[str, object]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def dependency_name(specifier: str) -> str:
    return re.split(r"[\s<>=!~;\[]", specifier, maxsplit=1)[0].lower()


def runtime_dependency_names() -> set[str]:
    dependencies = project_config()["project"]["dependencies"]
    return {dependency_name(specifier) for specifier in dependencies}


def test_runtime_dependencies_are_cpu_only_and_first_party_needs_only() -> None:
    names = runtime_dependency_names()
    forbidden = {
        "debugpy",
        "ipykernel",
        "ipython",
        "jupyter-client",
        "jupyter-core",
        "mlstm-kernels",
        "pytest",
        "ruff",
        "torchvision",
        "transformers",
        "triton",
        "xlstm",
    }
    assert "torch" in names
    assert not names.intersection(forbidden)
    assert not any(name.startswith("nvidia-") for name in names)


def test_torch_uses_the_explicit_cpu_index() -> None:
    config = project_config()
    assert config["tool"]["uv"]["sources"]["torch"] == {"index": "pytorch-cpu"}
    indices = {index["name"]: index for index in config["tool"]["uv"]["index"]}
    assert indices["pytorch-cpu"] == {
        "name": "pytorch-cpu",
        "url": "https://download.pytorch.org/whl/cpu",
        "explicit": True,
    }


def test_test_and_lint_tools_are_dev_dependencies() -> None:
    config = project_config()
    runtime = runtime_dependency_names()
    dev = {dependency_name(specifier) for specifier in config["dependency-groups"]["dev"]}
    assert {"pytest", "ruff"} <= dev
    assert not {"pytest", "ruff"}.intersection(runtime)
```

- [x] **Step 2: Run the contract test and verify RED**

Run:

```bash
cd /home/vm/.config/superpowers/worktrees/PDM_Algorithm/pdm-cpu-image
uvx --from pytest==9.0.2 pytest tests/test_distribution_contract.py -v
```

Expected: failures show forbidden runtime packages and missing `tool.uv.sources`/CPU index.

- [x] **Step 3: Replace the production dependency block with the minimal direct set**

Set `[project].dependencies` to:

```toml
dependencies = [
    "fastapi==0.125.0",
    "filelock==3.25.2",
    "httpx==0.28.1",
    "matplotlib==3.10.8",
    "numpy==2.3.5",
    "pandas==2.3.3",
    "psycopg2-binary==2.9.11",
    "pydantic==2.12.5",
    "pyodbc==5.3.0",
    "pyyaml==6.0.3",
    "requests==2.32.5",
    "torch==2.10.0",
    "tqdm==4.67.1",
    "urllib3==2.6.3",
    "uvicorn==0.38.0",
]
```

Add:

```toml
[dependency-groups]
dev = [
    "pytest==9.0.2",
    "ruff==0.14.9",
]

[tool.uv.sources]
torch = { index = "pytorch-cpu" }

[[tool.uv.index]]
name = "pytorch-cpu"
url = "https://download.pytorch.org/whl/cpu"
explicit = true
```

- [x] **Step 4: Regenerate the lock and verify it contains no GPU stack**

Run:

```bash
uv lock
if rg -n 'name = "(nvidia-|triton|xlstm|mlstm-kernels|torchvision|transformers)' uv.lock; then
  exit 1
fi
```

Expected: lock succeeds and the forbidden-package search returns no matches.

- [x] **Step 5: Run the contract test and verify GREEN**

Run:

```bash
uvx --from pytest==9.0.2 pytest tests/test_distribution_contract.py -v
```

Expected: all three tests pass.

- [x] **Step 6: Commit the dependency contract**

Run:

```bash
git add pyproject.toml uv.lock tests/test_distribution_contract.py
git commit -m "build: lock PDM to CPU runtime dependencies"
```

---

### Task 5: Drive the CPU Docker and Compose contract with TDD

**Files:**
- Modify: `/home/vm/.config/superpowers/worktrees/PDM_Algorithm/pdm-cpu-image/tests/test_distribution_contract.py`
- Modify: `/home/vm/.config/superpowers/worktrees/PDM_Algorithm/pdm-cpu-image/Dockerfile`
- Modify: `/home/vm/.config/superpowers/worktrees/PDM_Algorithm/pdm-cpu-image/docker-compose.yml`
- Modify: `/home/vm/.config/superpowers/worktrees/PDM_Algorithm/pdm-cpu-image/README.md`
- Modify: `/home/vm/.config/superpowers/worktrees/PDM_Algorithm/pdm-cpu-image/docs/docker-deploy.md`

**Interfaces:**
- Consumes: Task 4 CPU uv lock
- Produces: single frozen production sync, no default GPU reservation, one-worker CPU image, and documented CPU deployment behavior

- [x] **Step 1: Add failing Docker/Compose contract tests**

Append:

```python
def test_dockerfile_has_one_frozen_sync_and_no_second_torch_install() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert dockerfile.count("uv sync") == 1
    assert "uv sync --frozen --no-dev --python 3.12" in dockerfile
    assert "uv pip install torch" not in dockerfile
    assert '"--workers", "1"' in dockerfile


def test_default_compose_does_not_request_gpu() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "driver: nvidia" not in compose
    assert "capabilities: [ gpu ]" not in compose
```

- [x] **Step 2: Run the two new tests and verify RED**

Run:

```bash
uvx --from pytest==9.0.2 pytest \
  tests/test_distribution_contract.py::test_dockerfile_has_one_frozen_sync_and_no_second_torch_install \
  tests/test_distribution_contract.py::test_default_compose_does_not_request_gpu \
  -v
```

Expected: Dockerfile test fails on two sync commands/extra Torch install/two workers; Compose test fails on NVIDIA reservation.

- [x] **Step 3: Make Dockerfile use the CPU lock exactly once**

Keep the existing Ubuntu/ODBC installation, but replace the dependency and application-copy section with:

```dockerfile
# Copy the application only after system dependencies are ready. .dockerignore
# excludes virtual environments, secrets, data, artifacts, and local integrations.
COPY . /app

# The frozen uv lock selects the explicit PyTorch CPU index from pyproject.toml.
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    UV_HTTP_TIMEOUT=120 \
    uv sync --frozen --no-dev --python 3.12
```

Delete both original `uv sync` blocks and the standalone
`uv pip install torch torchvision` command. Change the final command to:

```dockerfile
CMD ["uvicorn", "valeo_pdm.api.app:app", "--host", "0.0.0.0", "--port", "10021", "--workers", "1"]
```

- [x] **Step 4: Remove only the default NVIDIA reservation from Compose**

Delete:

```yaml
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [ gpu ]
```

Keep ports, mounts, health check, ODBC configuration, `ipc: host`, and restart policy unchanged.

- [x] **Step 5: Document CPU default and future GPU boundary**

Update `README.md` and `docs/docker-deploy.md` to state:

```text
The default Docker image uses CPU-only PyTorch and does not request an NVIDIA
device. It preserves training and prediction APIs, but production-sized
training may be slower. A GPU image must use a separate Dockerfile/profile and
exactly one CUDA/PyTorch index; do not add CUDA packages to the default lock.
```

Use the surrounding document language (Chinese) while preserving these exact technical requirements.

- [x] **Step 6: Run contract tests and verify GREEN**

Run:

```bash
uvx --from pytest==9.0.2 pytest tests/test_distribution_contract.py -v
```

Expected: all five contract tests pass.

- [x] **Step 7: Commit the Docker contract**

Run:

```bash
git add Dockerfile docker-compose.yml README.md docs/docker-deploy.md tests/test_distribution_contract.py
git commit -m "build: make PDM container CPU-only by default"
```

---

### Task 6: Verify Python behavior and resolve only existing lint blockers

**Files:**
- Modify if Ruff still reports the known three issues: `/home/vm/.config/superpowers/worktrees/PDM_Algorithm/pdm-cpu-image/src/valeo_pdm/transformer/models/informer.py`
- Test: complete default pytest suite, complete Ruff check, format check

**Interfaces:**
- Consumes: Task 4 CPU environment and Task 5 image contract
- Produces: a clean Python test/lint baseline before image construction

- [x] **Step 1: Synchronize the complete CPU development environment**

Run:

```bash
uv sync --frozen --all-groups
```

Expected: CPU Torch installs without NVIDIA/CUDA/Triton packages.

- [x] **Step 2: Run the complete default test suite**

Run:

```bash
uv run pytest -q
```

Expected: all default tests pass and the `integration_db` test remains deselected.

- [x] **Step 3: Run full Ruff and capture actual blockers**

Run:

```bash
uv run ruff check .
uv run ruff format --check .
```

Expected: if the previously observed E741/E712 issues remain, only those three unchanged-source findings fail.

- [x] **Step 4: Fix the exact Ruff findings without changing model behavior**

In `informer.py`, rename the ambiguous local loop variable `l` to `layer` and replace boolean comparisons of the form `mask == True` with the equivalent boolean mask expression accepted by Ruff. Do not alter tensor shapes, attention math, model defaults, or public interfaces.

- [x] **Step 5: Re-run focused and full verification**

Run:

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
git diff --check
```

The full-tree format command is an audit of the inherited source tree, not a
zero-exit gate.  Extract every `Would reformat: <path>` line, sort the paths,
and compare them with this exact accepted pre-existing baseline:

```text
src/valeo_pdm/__init__.py
src/valeo_pdm/__main__.py
src/valeo_pdm/api/__init__.py
src/valeo_pdm/api/app.py
src/valeo_pdm/api/router.py
src/valeo_pdm/cli.py
src/valeo_pdm/db/__init__.py
src/valeo_pdm/gateway/gateway.py
src/valeo_pdm/gateway/mock_backend.py
src/valeo_pdm/training/__init__.py
src/valeo_pdm/training/plan.py
src/valeo_pdm/training/run_control.py
src/valeo_pdm/transformer/models/__init__.py
src/valeo_pdm/transformer/models/autoformer.py
src/valeo_pdm/transformer/scripts/__init__.py
src/valeo_pdm/transformer/train_testmodel.py
tests/test_training_api_mvp.py
```

Expected: pytest, Ruff lint, and `git diff --check` exit 0.  The full-tree
format audit exits 1 with exactly the 17 sorted paths above—no more and no
fewer—and every Python file changed from the component base commit passes an
individual `uv run ruff format --check <paths...>` invocation.

- [x] **Step 6: Commit only if the lint file changed**

Run:

```bash
if ! git diff --quiet -- src/valeo_pdm/transformer/models/informer.py; then
  git add src/valeo_pdm/transformer/models/informer.py
  git commit -m "style: clear PDM model lint blockers"
fi
```

---

### Task 7: Build and smoke-test the candidate CPU image

**Files:**
- Create: Docker image tag `valeo-pdm:cpu-candidate`
- Create temporarily: container `valeo-pdm-cpu-candidate`
- Create temporarily: `/tmp/pdm-candidate-artifacts.*`
- Modify: none
- Test: image imports, CPU Torch, API health, model list, image size

**Interfaces:**
- Consumes: verified component worktree from Task 6
- Produces: independently tested candidate image and measured size

- [x] **Step 1: Build the candidate image**

Run:

```bash
cd /home/vm/.config/superpowers/worktrees/PDM_Algorithm/pdm-cpu-image
docker build --progress=plain -t valeo-pdm:cpu-candidate .
```

Expected: build exits 0 using the frozen CPU lock.

- [x] **Step 2: Verify dependency and size properties inside the image**

Run:

```bash
PDM_AUDIT_DIR=/tmp/pdm-space-audit-20260728
docker run --rm --entrypoint python valeo-pdm:cpu-candidate -c '
import torch
import fastapi
import pandas
import pyodbc
import psycopg2
from valeo_pdm.api.app import app
assert torch.cuda.is_available() is False
print(torch.__version__)
print(app.title)
'
docker image inspect valeo-pdm:cpu-candidate --format '{{.Size}}' | tee "$PDM_AUDIT_DIR/candidate.image-size"
docker run --rm --entrypoint du valeo-pdm:cpu-candidate -sb /app/.venv | tee "$PDM_AUDIT_DIR/candidate.venv-size"
```

Expected: imports succeed, CUDA is false, and image/venv measurements are materially below the baseline.

- [x] **Step 3: Start the candidate on isolated port 10022**

Run:

```bash
PDM_AUDIT_DIR=/tmp/pdm-space-audit-20260728
CANDIDATE_ARTIFACTS=$(mktemp -d /tmp/pdm-candidate-artifacts.XXXXXX)
chmod 0777 "$CANDIDATE_ARTIFACTS"
printf '%s\n' "$CANDIDATE_ARTIFACTS" > "$PDM_AUDIT_DIR/candidate-artifacts.path"
docker run -d --rm \
  --name valeo-pdm-cpu-candidate \
  -p 127.0.0.1:10022:10021 \
  -v /home/vm/code/PDM_Algorithm/configs:/app/configs:ro \
  -v /home/vm/code/PDM_Algorithm/data:/app/data:ro \
  -v "$CANDIDATE_ARTIFACTS":/app/artifacts \
  valeo-pdm:cpu-candidate
```

- [x] **Step 4: Verify candidate health and models**

Run:

```bash
PDM_AUDIT_DIR=/tmp/pdm-space-audit-20260728
for _ in $(seq 1 60); do
  CANDIDATE_HEALTH=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' valeo-pdm-cpu-candidate)
  if [ "$CANDIDATE_HEALTH" = healthy ] &&
    curl --fail --silent http://127.0.0.1:10022/healthz >/dev/null; then
    break
  fi
  if [ "$CANDIDATE_HEALTH" = unhealthy ]; then
    docker logs valeo-pdm-cpu-candidate
    exit 1
  fi
  sleep 1
done
curl --fail --silent --show-error http://127.0.0.1:10022/healthz
curl --fail --silent --show-error http://127.0.0.1:10022/measPredict/models > "$PDM_AUDIT_DIR/candidate.models.json"
test -s "$PDM_AUDIT_DIR/candidate.models.json"
docker inspect --format '{{.State.Health.Status}}' valeo-pdm-cpu-candidate
```

Expected: health is `healthy`, health JSON is `ok`, and model response is non-empty.

- [x] **Step 5: Stop and remove candidate smoke resources**

Run:

```bash
CANDIDATE_ARTIFACTS=$(sed -n '1p' /tmp/pdm-space-audit-20260728/candidate-artifacts.path)
test "$(realpath -m "$CANDIDATE_ARTIFACTS")" = "$CANDIDATE_ARTIFACTS"
case "$CANDIDATE_ARTIFACTS" in
  /tmp/pdm-candidate-artifacts.*) ;;
  *) exit 1 ;;
esac
docker stop valeo-pdm-cpu-candidate
rm -rf -- "$CANDIDATE_ARTIFACTS"
```

Expected: the `--rm` candidate container disappears; only the image tag remains.

---

### Task 8: Switch the running PDM container with rollback protection

**Files:**
- Create temporarily: Docker tag `valeo-pdm:rollback-20260728`
- Retag: `valeo-pdm:cpu-candidate` to `valeo-pdm:latest`
- Modify: running container `pdm_algorithm-valeo-pdm-api-1`
- Test: health, MCP, checksums, image ID, disk accounting

**Interfaces:**
- Consumes: candidate image from Task 7 and current legacy-path runtime mounts
- Produces: running CPU image on port 10021 with an immediately available rollback path until final verification passes

- [x] **Step 1: Create rollback and latest tags**

Run:

```bash
PDM_AUDIT_DIR=/tmp/pdm-space-audit-20260728
OLD_PDM_IMAGE_ID=$(docker inspect --format '{{.Image}}' pdm_algorithm-valeo-pdm-api-1)
printf '%s\n' "$OLD_PDM_IMAGE_ID" > "$PDM_AUDIT_DIR/old-image-id"
docker tag "$OLD_PDM_IMAGE_ID" valeo-pdm:rollback-20260728
docker tag valeo-pdm:cpu-candidate valeo-pdm:latest
```

Expected: rollback tag resolves to the old image ID and latest resolves to the candidate image ID.

- [x] **Step 2: Recreate the current service without rebuilding**

Run:

```bash
docker compose \
  --project-directory /home/vm/code/PDM_Algorithm \
  -f /home/vm/code/PDM_Algorithm/docker-compose.yml \
  -f /home/vm/code/PDM_Algorithm/data/compose.cpu.yml \
  up -d --no-build --force-recreate valeo-pdm-api
```

- [x] **Step 3: Verify the switched service**

Run:

```bash
PDM_AUDIT_DIR=/tmp/pdm-space-audit-20260728
for _ in $(seq 1 60); do
  LIVE_HEALTH=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' pdm_algorithm-valeo-pdm-api-1)
  if [ "$LIVE_HEALTH" = healthy ] &&
    curl --fail --silent http://127.0.0.1:10021/healthz >/dev/null; then
    break
  fi
  if [ "$LIVE_HEALTH" = unhealthy ]; then
    docker logs pdm_algorithm-valeo-pdm-api-1
    exit 1
  fi
  sleep 1
done
docker inspect --format 'status={{.State.Status}} health={{.State.Health.Status}} image={{.Image}}' pdm_algorithm-valeo-pdm-api-1
curl --fail --silent --show-error http://127.0.0.1:10021/healthz
curl --fail --silent --show-error http://127.0.0.1:10021/measPredict/models > "$PDM_AUDIT_DIR/live.models.json"
test -s "$PDM_AUDIT_DIR/live.models.json"
```

Expected: container is running/healthy on the candidate image and model response is non-empty.

- [x] **Step 4: Verify MCP and immutable runtime state**

Run:

```bash
PDM_AUDIT_DIR=/tmp/pdm-space-audit-20260728
codex exec --ephemeral --skip-git-repo-check -C /home/vm/code/ifactory-platform \
  'Call the digital-platform pdm_health_check tool exactly once and return only its JSON result.'
find /home/vm/code/PDM_Algorithm/configs /home/vm/code/PDM_Algorithm/data /home/vm/code/PDM_Algorithm/artifacts -type f -print0 |
  sort -z |
  xargs -0 sha256sum > "$PDM_AUDIT_DIR/runtime.sha256.after-cutover"
cmp "$PDM_AUDIT_DIR/runtime.sha256.before" "$PDM_AUDIT_DIR/runtime.sha256.after-cutover"
```

Expected: MCP reports healthy and checksums match.

- [x] **Step 5: Roll back immediately if any Task 8 verification fails**

Run only on failure:

```bash
docker tag valeo-pdm:rollback-20260728 valeo-pdm:latest
docker compose \
  --project-directory /home/vm/code/PDM_Algorithm \
  -f /home/vm/code/PDM_Algorithm/docker-compose.yml \
  -f /home/vm/code/PDM_Algorithm/data/compose.cpu.yml \
  up -d --no-build --force-recreate valeo-pdm-api
curl --fail --silent --show-error http://127.0.0.1:10021/healthz
```

Expected: original image is restored and healthy; stop implementation and report the candidate failure.

- [x] **Step 6: Remove rollback/candidate tags only after all verification passes**

Run:

```bash
PDM_AUDIT_DIR=/tmp/pdm-space-audit-20260728
OLD_PDM_IMAGE_ID=$(sed -n '1p' "$PDM_AUDIT_DIR/old-image-id")
test "$OLD_PDM_IMAGE_ID" = "$(docker image inspect valeo-pdm:rollback-20260728 --format '{{.Id}}')"
docker image rm valeo-pdm:rollback-20260728
docker image rm valeo-pdm:cpu-candidate
docker image rm "$OLD_PDM_IMAGE_ID"
df -B1 / | tee "$PDM_AUDIT_DIR/df.final"
docker system df | tee "$PDM_AUDIT_DIR/docker.final"
```

Expected: `valeo-pdm:latest` remains in use, the old unreferenced image is removed, and final free space is recorded.

---

### Task 9: Update the platform gitlink and complete review

**Files:**
- Modify: `/home/vm/code/ifactory-platform/components/pdm-algorithm` gitlink
- Modify: `/home/vm/code/ifactory-platform/docs/superpowers/plans/2026-07-28-pdm-space-optimization.md` only to mark completed checkboxes
- Test: component verification, platform doctor, clean worktrees, independent code review

**Interfaces:**
- Consumes: final component commit on `chore/pdm-cpu-image`
- Produces: platform branch `chore/pdm-space-optimization` pinned to the reviewed component commit

- [x] **Step 1: Run fresh final component verification**

Run in the component worktree:

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
git diff --check
git status --short --branch
```

Repeat the exact Task 6 Step 5 path-set comparison for the full-tree format
audit and run the format checker separately over every Python file changed
from the component base commit.

Expected: tests, Ruff lint, changed-file format checks, and diff checks pass;
the full-tree format audit matches the exact accepted 17-path baseline; and
the component worktree is clean.

- [x] **Step 2: Request an independent code review**

Use `superpowers:requesting-code-review` with:

```text
DESCRIPTION: CPU-only PDM dependency lock, Docker/Compose contract, docs, and distribution tests
PLAN_OR_REQUIREMENTS: docs/superpowers/specs/2026-07-28-pdm-space-optimization-design.md and this plan
BASE_SHA: 9593c485c1a1b8488af7753ac8352e225d4e445b
HEAD_SHA: output of git rev-parse HEAD in the component worktree
```

Fix every Critical or Important issue, then repeat Task 9 Step 1.

- [x] **Step 3: Point the platform submodule at the component commit**

Run:

```bash
PDM_HEAD=$(git -C /home/vm/.config/superpowers/worktrees/PDM_Algorithm/pdm-cpu-image rev-parse HEAD)
git -C /home/vm/code/ifactory-platform/components/pdm-algorithm checkout --detach "$PDM_HEAD"
git -C /home/vm/code/ifactory-platform add components/pdm-algorithm docs/superpowers/plans/2026-07-28-pdm-space-optimization.md
git -C /home/vm/code/ifactory-platform commit -m "build: pin slim CPU PDM service"
```

- [x] **Step 4: Run fresh platform verification**

Run:

```bash
cd /home/vm/code/ifactory-platform
./scripts/doctor.sh
git diff --check origin/main...HEAD
git status --short --branch
curl --fail --silent --show-error http://127.0.0.1:10021/healthz
```

Expected: doctor reports 0 failures/0 warnings, branch is clean, and PDM is healthy.

- [x] **Step 5: Use `superpowers:finishing-a-development-branch`**

Present the exact integration menu for the platform branch and preserve both the component worktree and branches unless the user selects an integration option.
