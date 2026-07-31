# CMMS Hybrid Development Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改动 CMMS 业务代码、不影响现有 ThingsBoard/PDM 工作负载的前提下，实现一套“宿主机 CMMS API/前端 + 容器 PostgreSQL/MinIO/Nginx”的可复现开发部署控制面，并为后续独立授权的 CMMS bootstrap 与 Phase 2 真链路验收提供 fail-closed 入口。

**Architecture:** 总仓新增一个无第三方运行时依赖的 Python 控制项目，统一负责安全文件、规范计划、固定工具链、Compose、双阶段网关、user-systemd、许可证模式、CMMS 正式 API bootstrap 和验收回执。浏览器、宿主机探针和 Compose 调用方分别使用 `cmms.localhost:3000`、`127.0.0.1:3000` 和 `host.docker.internal:3000`，但都进入同一个 Nginx。默认测试只使用临时文件和 fake transport；真实下载、容器、systemd、身份/API Key 写入及 MinIO 写探针必须经过后续独立 plan/hash 确认。回执生成后的 opt-in live pytest 只有只读权限，并仍需单独明确授权。

**Tech Stack:** Python 3.12.13/uv、Python 标准库、pytest 9、PyYAML、Docker Compose v2、PostgreSQL 16、MinIO、Nginx 1.27、user-systemd、Eclipse Temurin JDK 17.0.19+10、Maven 3.9.3、Node.js 21.6.1。

## Global Constraints

- 以已批准设计 `docs/superpowers/specs/2026-07-31-cmms-hybrid-development-deployment-design.md` 为规范来源；发生冲突时停止并回到设计复核，不在实现中静默改写安全边界。
- 本计划只修改总仓部署控制面、测试、CI 和文档。不得修改 `components/cmms`、`components/thingsboard`、`components/pdm-algorithm`、`components/platform-integration` 或 `contracts/`。
- CMMS、ThingsBoard、PDM 和 `platform-integration` 只通过稳定 API/事件/契约协作；部署工具不得查询或修改任何服务数据库。
- 默认测试不得下载 JDK/Maven/Node、拉取镜像、启动容器、调用 systemd、访问真实 CMMS 或写 `.runtime/`。所有 OS、Docker、systemd 和 HTTP 行为都通过注入的 runner/transport 测试。
- 实际服务动作的第一项有状态操作必须把 Nginx 渲染或恢复为 loopback-only，并证明 Docker host-gateway 的 `:3000` 不可达；任何异常都保持或恢复这一状态。
- Nginx 对任何携带 `x-api-key` 的请求使用绑定当前 `platform-integration` 客户端与契约摘要的 method/path 正向白名单；Bearer bootstrap 流量不受该分支影响，白名单外 API-Key 请求在到达 CMMS 前返回稳定 `403`。
- `plan` 只生成权限 `0600` 的规范 JSON 和 SHA-256。`apply` 必须同时收到该文件及精确 `--confirm-plan-sha256`；执行者仍须在展示计划后的下一轮取得用户明确确认，CLI 参数不能代替会话确认。
- 本实施计划只授权实现控制面和离线测试，不授权第一次 live bootstrap。控制面完成后必须停止；工具链下载、Compose 启动、user-systemd 注册、许可证验证、账号/角色/API Key 创建、MinIO 临时写探针以及 Phase 2 凭据写入由新的部署计划和下一轮精确哈希确认授权。后续 live pytest 只能重验回执和只读状态；若要重复任何写探针，必须再生成新计划/哈希并重新确认。
- 不在 argv、stdout、stderr、journal、状态 JSON、plan、receipt 或 Git 中出现数据库/MinIO/JWT/license/password/API Key 值。上游响应体、堆栈和绝对产物路径不得出现在稳定错误输出。
- 真实 secret、env、plan、permit、state、budget 和 receipt 只存在于已忽略的根仓 `.runtime/` 精确白名单路径；Phase 2 envelope 保持既有 `.runtime/predictive-maintenance-shadow.env`。不新增 `.gitignore` 规则。
- secret/env/record 文件必须是当前 UID 所有、单链接、非符号链接普通文件，权限 `0600`，使用 `O_NOFOLLOW|O_NONBLOCK`、有界读取和读前/读后元数据复核。目录链必须逐层拒绝符号链接。
- API unit 固定 `Restart=no`。每个新 API MainPID 都需要最长 60 秒、单次消费的 start permit；任何退出路径都调用 `ExecStopPost` fail-closed。前端才允许带节流的 `Restart=on-failure`。
- 离线许可证模式必须同时具备合法 license file/key、systemd loopback-only IP 过滤和 30 秒 guard；任一能力不可证明就拒绝启动。在线模式每日最多写前计入 10 个受控新进程尝试，未知或耗尽预算不得创建 MainPID。
- 开发模式允许 CMMS dirty，但必须显示 `UNCOMMITTED`，且启动敏感文件不能变化；验收模式要求根仓/CMMS 干净、CMMS HEAD 等于总仓 gitlink，才可生成 live acceptance receipt。
- Node 21.6.1 已超出常规维护周期；这里仅为精确匹配当前 CMMS 前端 Dockerfile，不作为生产版本建议。升级必须单独评审。
- 每个任务先写失败测试，再实现最小代码，再运行该切片验证。每次提交前运行 `./scripts/doctor.sh`；不 push，除非用户另行明确要求。
- 所有命令块都从总仓根目录 `/home/vm/code/ifactory-platform` 开始，除非块内显式 `cd`。

## File and Responsibility Map

| Path | Responsibility |
|---|---|
| `deploy/cmms/pyproject.toml`, `.python-version`, `uv.lock` | 独立冻结的控制项目；包名 `ifactory-cmms-deploy`，命令 `cmms-development`。 |
| `deploy/cmms/src/ifactory_cmms_deploy/` | 生产部署控制逻辑；不得从 `tests/` 导入 helper。 |
| `deploy/cmms/manifests/toolchains.json` | 三个宿主机应用工具链的精确版本、官方 URL、SHA-256 和归档布局。 |
| `deploy/cmms/manifests/startup-sensitive-files.json` | 绑定 CMMS gitlink `f3cab0aaf3418638e76dc33dfa5f8b30ded2b7f0` 与启动/权限/迁移敏感源码摘要。 |
| `deploy/compose/cmms-development.yml` | 固定项目 `ifactory-cmms-dev`；只含 PostgreSQL、MinIO、Nginx。 |
| `deploy/compose/cmms-*.env.example` | 无秘密示例及 secret-file 绝对路径引用。 |
| `deploy/gateway/cmms-development-nginx.conf.template` | loopback/dual 两种固定 Nginx 配置模板。 |
| `deploy/systemd/ifactory-cmms-*.in` | API、前端、fail-closed、license guard/timer 的无秘密 user-unit 模板。 |
| `scripts/cmms-development.sh` | 只解析根目录并以 frozen uv 项目转发 CLI。 |
| `tests/e2e/support/cmms_deployment.py` | fake process/systemd/Docker/HTTP、受限测试文件工厂；不含生产逻辑。 |
| `tests/e2e/test_cmms_*.py` | 默认离线契约、安全、生命周期和许可证测试，以及 opt-in live gate。 |

生产包使用以下模块边界：

```text
ifactory_cmms_deploy/
├── __init__.py
├── __main__.py
├── api_launcher.py
├── bootstrap.py
├── cli.py
├── cmms_api.py
├── compose.py
├── config.py
├── credentials.py
├── errors.py
├── fail_closed.py
├── gateway.py
├── license.py
├── license_guard.py
├── lifecycle.py
├── planning.py
├── preflight.py
├── process.py
├── readiness.py
├── records.py
├── secure_io.py
├── source.py
├── start_gate.py
├── systemd.py
└── toolchains.py
```

公开 CLI 固定为：

```text
cmms-development status [--json]
cmms-development secret set-candidate --identity {super-admin,organization-admin,runtime-user}
cmms-development secret prepare-invitation-probe --email CANONICAL_EMAIL
cmms-development secret set-runtime --name {postgres-password,minio-root-user,minio-root-password,license-key}
cmms-development secret generate-jwt
cmms-development secret import-license-file --from-fd FD
cmms-development plan --operation {bootstrap,start,restart-api,restart-frontend,stop,repair,switch-license} --profile {development,acceptance} --license-mode {offline,online}
cmms-development apply --plan-file PATH --confirm-plan-sha256 HEX64
cmms-development internal api-launch
cmms-development internal frontend-launch
cmms-development internal start-gate
cmms-development internal fail-closed --reason CODE
cmms-development internal license-guard
cmms-development internal network-probe --loopback-port PORT
```

`internal` 子命令只供渲染后的 unit 使用，拒绝未知参数、相对运行根目录和未绑定 unit generation。

Target-host resolution currently returns both `::1` and `127.0.0.1` for `cmms.localhost`. The implementation therefore treats both as the approved loopback-only boundary and adds only the separately verified Docker bridge IPv4 in dual mode; it never adds an IPv6 wildcard or LAN listener.

Use this exact ignored runtime layout:

```text
.runtime/
├── cmms-bootstrap.env
├── cmms-development.env
├── cmms-development-state.json
├── cmms-frontend.env
├── cmms-license-online-budget.json
├── cmms-development-apply.lock
├── cmms-builds/
├── cmms-cache/
├── cmms-control/.venv/
├── cmms-nginx/cmms-development-nginx.conf
├── cmms-staging/
├── plans/cmms-development/
├── receipts/cmms-bootstrap/
├── receipts/cmms-budget-recovery/
├── receipts/cmms-development.json
├── secrets/
├── start-permits/
├── systemd/
└── toolchains/
```

---

### Task 0: Reconfirm the Exact Root and Component Baseline

**Files:** None.

**Interfaces:**

- Consumes: committed approved design/implementation plan, current root branch, CMMS gitlink
- Produces: a clean, reviewed starting point; no file or runtime mutation

- [ ] **Step 1: Re-read governing files and verify repository boundaries**

Run:

```bash
set -Eeuo pipefail
test "$(pwd -P)" = "/home/vm/code/ifactory-platform"
sed -n '1,260p' AGENTS.md
sed -n '1,520p' docs/superpowers/specs/2026-07-31-cmms-hybrid-development-deployment-design.md
sed -n '1,260p' components/cmms/README.MD
test ! -f components/cmms/AGENTS.md
```

Expected: the root convention and approved design are fully read; CMMS has no component-local `AGENTS.md`.

- [ ] **Step 2: Prove the implementation branch and submodule baseline**

Run:

```bash
set -Eeuo pipefail
test "$(git branch --show-current)" = "feat/cmms-hybrid-development-deployment"
git ls-files --error-unmatch \
  docs/superpowers/plans/2026-07-31-cmms-hybrid-development-deployment-plan.md
test -z "$(git status --porcelain)"
test -z "$(git -C components/cmms status --porcelain)"
test "$(git rev-parse HEAD:components/cmms)" = \
  "f3cab0aaf3418638e76dc33dfa5f8b30ded2b7f0"
test "$(git -C components/cmms rev-parse HEAD)" = \
  "f3cab0aaf3418638e76dc33dfa5f8b30ded2b7f0"
test "$(git -C components/cmms remote get-url origin)" = \
  "https://github.com/xiangzuodalao/cmms.git"
test "$(git -C components/cmms remote get-url upstream)" = \
  "https://github.com/grashjs/cmms.git"
```

Expected: the approved plan is already tracked in the clean implementation baseline and all assertions pass. Stop rather than switching, resetting, merging, or updating the submodule if any assertion fails.

### Task 1: Scaffold the Frozen Control Project and Secure I/O Primitives

**Files:**

- Create: `deploy/cmms/pyproject.toml`
- Create: `deploy/cmms/.python-version`
- Create: `deploy/cmms/uv.lock`
- Create: `deploy/cmms/src/ifactory_cmms_deploy/__init__.py`
- Create: `deploy/cmms/src/ifactory_cmms_deploy/__main__.py`
- Create: `deploy/cmms/src/ifactory_cmms_deploy/cli.py`
- Create: `deploy/cmms/src/ifactory_cmms_deploy/errors.py`
- Create: `deploy/cmms/src/ifactory_cmms_deploy/process.py`
- Create: `deploy/cmms/src/ifactory_cmms_deploy/secure_io.py`
- Create: `tests/e2e/support/cmms_deployment.py`
- Create: `tests/e2e/test_cmms_secure_artifacts.py`
- Modify: `tests/e2e/support/__init__.py`
- Modify: `tests/pyproject.toml`
- Modify: `tests/uv.lock`

**Interfaces:**

- Produces: `DeploymentError(code: str, safe_message: str, exit_code: int)`
- Produces: `RuntimePathPolicy(root: Path, allowed_files: frozenset[Path], allowed_directories: frozenset[Path])`
- Produces for tests only: `RuntimePathPolicy.for_test(root: Path, allowed_files: Collection[Path]) -> RuntimePathPolicy`
- Produces: `SecureSnapshot(path, fd, stat, sha256, data)` as a context-managed descriptor snapshot
- Produces: `read_secure_bytes(path: Path, *, max_bytes: int, policy: RuntimePathPolicy) -> bytes`
- Produces: `atomic_write_private(path: Path, data: bytes, *, replace: bool, policy: RuntimePathPolicy) -> None`
- Produces: immutable `CommandSpec` and `CommandResult`
- Produces: `CommandRunner.run(spec: CommandSpec) -> CommandResult`
- Produces: `collect_bounded_process_output(process: subprocess.Popen[bytes], *, input_bytes: bytes | None, deadline: float, stdout_limit: int, stderr_limit: int) -> tuple[bytes, bytes]`
- Produces: `main(argv: Sequence[str] | None = None) -> int`
- Produces for tests only: `SafeRuntimeFixture`, `CmmsSourceFixture`, `ScriptedRunner`, `ScriptedHttpTransport`
- Consumes: only current-UID local files and explicit argv/env; never ambient Docker/Compose control variables

- [ ] **Step 1: Add the package/test dependency contract**

Use Python `>=3.12,<3.13`, `.python-version` value `3.12.13`, no runtime dependency, and the console script:

```toml
[project]
name = "ifactory-cmms-deploy"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = []

[project.scripts]
cmms-development = "ifactory_cmms_deploy.cli:main"
```

Use this exact build backend:

```toml
[build-system]
requires = ["hatchling==1.27.0"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/ifactory_cmms_deploy"]
```

Add the local package to the root test project:

```toml
dependencies = [
  "ifactory-cmms-deploy",
  "jsonschema>=4.25,<5",
  "openapi-spec-validator==0.7.2",
  "python-dotenv==1.2.2",
  "pytest==9.0.2",
  "pyyaml==6.0.3",
]

[tool.uv.sources]
ifactory-cmms-deploy = { path = "../deploy/cmms", editable = true }
```

Run `uv lock --project deploy/cmms` and `uv lock --project tests`; inspect both diffs and confirm no unrelated dependency upgrade.

- [ ] **Step 2: Write failing secure-file and process tests**

Tests must cover:

- accepted current-UID, regular, single-link `0600` files;
- rejection of symlink, hardlink, FIFO, directory, wrong owner, `0640`, empty required value, NUL, over-limit input, short-read mutation and a symlink in any parent directory;
- canonical target confinement to the exact `.runtime/` paths listed above or the one explicit Phase 2 env path;
- exclusive create, atomic replace, file and parent-directory `fsync`, and post-rename revalidation;
- process execution with `shell=False`, absolute executable, explicit cwd, timeout, bounded output and a minimal environment;
- complete removal of inherited `DOCKER_*`, `COMPOSE_*`, proxy, credential, `HOME` and `XDG_*` variables;
- a sentinel secret never appearing in `DeploymentError`, captured log, stdout or stderr.

Include these exact core tests, then add parameter rows for every rejection case listed above:

```python
def test_secure_reader_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "secret"
    target.write_bytes(b"test-secret")
    target.chmod(0o600)
    link = tmp_path / "secret-link"
    link.symlink_to(target)
    policy = RuntimePathPolicy.for_test(tmp_path, allowed_files={link})

    with pytest.raises(DeploymentError) as caught:
        read_secure_bytes(link, max_bytes=64, policy=policy)

    assert caught.value.code == "CMMS-E001"
    assert "test-secret" not in caught.value.safe_message


def test_command_runner_drops_inherited_control_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOCKER_HOST", "tcp://unsafe.invalid:2375")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid")
    spec = CommandSpec(
        argv=("/usr/bin/env",),
        cwd=tmp_path,
        environment={"PATH": "/usr/bin:/bin"},
        timeout_seconds=5,
        stdout_limit=4096,
        stderr_limit=4096,
        safe_label="environment probe",
    )

    result = CommandRunner().run(spec)

    assert result.returncode == 0
    assert "DOCKER_HOST=" not in result.stdout
    assert "HTTPS_PROXY=" not in result.stdout
```

- [ ] **Step 3: Run the new tests and verify the expected failure**

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  e2e/test_cmms_secure_artifacts.py -v
```

Expected: FAIL during import because the production primitives do not exist.

- [ ] **Step 4: Implement descriptor-bound I/O and stable errors**

Open directory components with `O_DIRECTORY|O_NOFOLLOW`; open files with `O_RDONLY|O_CLOEXEC|O_NOFOLLOW|O_NONBLOCK`. Compare `fstat` before and after a bounded read, require `S_ISREG`, current UID, `st_nlink == 1`, exact `0o600`, and a stable `(dev, ino, size, mtime_ns, ctime_ns)`.

`atomic_write_private()` must create a randomized sibling with `O_CREAT|O_EXCL|O_NOFOLLOW`, mode `0600`, write all bytes, `fsync` it, rename within the same directory, `fsync` the directory, and reopen/revalidate the final path. It must never recursively create an unverified absolute parent chain.

Use this concrete read skeleton, with `open_verified_parent()` performing the component-by-component `dir_fd` walk described above:

```python
def read_secure_bytes(
    path: Path, *, max_bytes: int, policy: RuntimePathPolicy
) -> bytes:
    policy.require_allowed_file(path)
    parent_fd, name = open_verified_parent(path, policy=policy)
    fd = os.open(
        name,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        dir_fd=parent_fd,
    )
    try:
        before = os.fstat(fd)
        require_private_regular_file(before, expected_uid=os.getuid())
        data = read_bounded(fd, max_bytes=max_bytes)
        after = os.fstat(fd)
        require_stable_stat(before, after, actual_size=len(data))
        return data
    finally:
        os.close(fd)
        os.close(parent_fd)
```

Use stable codes such as:

```text
CMMS-E001 unsafe-file
CMMS-E002 invalid-config
CMMS-E003 unsafe-command
CMMS-E004 command-failed
CMMS-E005 timeout
CMMS-E006 secret-detected
```

The safe message may name a logical artifact such as `runtime environment`, but not an absolute path, upstream response body, exception repr, or secret value.

- [ ] **Step 5: Implement the bounded command runner**

`CommandSpec` contains an immutable argv tuple, absolute cwd, explicit environment, timeout, stdout/stderr byte caps, optional input bytes, and an operator-safe label. Reject shell metacharacter interpretation by never using a shell. Kill the exact process group on timeout, bound both output streams, and return only decoded bounded text.

For Docker calls, later tasks will use:

```text
PATH=/usr/bin:/bin
DOCKER_CONFIG="${empty_docker_config}"
```

For systemd calls, later tasks will use an absolute `/usr/bin/systemctl` and no secret environment. Add a minimal `cli.py` whose `--help` succeeds and whose other commands return stable `CMMS-E020 command-not-available`; later tasks replace commands one slice at a time.

Construct the child explicitly:

```python
process = subprocess.Popen(
    spec.argv,
    cwd=spec.cwd,
    env=dict(spec.environment),
    stdin=subprocess.PIPE if spec.input_bytes is not None else subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    shell=False,
    start_new_session=True,
)
try:
    stdout, stderr = collect_bounded_process_output(
        process,
        input_bytes=spec.input_bytes,
        deadline=time.monotonic() + spec.timeout_seconds,
        stdout_limit=spec.stdout_limit,
        stderr_limit=spec.stderr_limit,
    )
except (ProcessDeadlineExceeded, ProcessOutputLimitExceeded):
    os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=5)
    raise DeploymentError("CMMS-E005", f"{spec.safe_label} did not complete safely", 5) from None
```

Implement `collect_bounded_process_output()` with nonblocking pipes and `selectors.DefaultSelector`; stop reading and raise as soon as either cap or deadline is crossed, then decode only the accepted bytes. Never include `spec.environment`, `stdout` or `stderr` in an unsafe diagnostic.

- [ ] **Step 6: Run the secure foundation tests**

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  e2e/test_cmms_secure_artifacts.py -v
uv run --project deploy/cmms --frozen --no-dev \
  python -m compileall -q deploy/cmms/src
uv run --project deploy/cmms --frozen --no-dev \
  cmms-development --help >/dev/null
git diff --check
./scripts/doctor.sh
```

Expected: both pytest and compile/help checks pass; doctor reports no failure.

- [ ] **Step 7: Commit the secure foundation**

Run:

```bash
set -Eeuo pipefail
git add \
  deploy/cmms \
  tests/pyproject.toml tests/uv.lock \
  tests/e2e/support/__init__.py \
  tests/e2e/support/cmms_deployment.py \
  tests/e2e/test_cmms_secure_artifacts.py
git commit -m "feat: add secure CMMS deployment control foundation"
```

Expected: tests and doctor pass; the commit contains no `.runtime`, venv, credential, or component change.

### Task 2: Add Strict Configuration, Canonical Records, and the Confirmation Gate

**Files:**

- Create: `deploy/cmms/src/ifactory_cmms_deploy/config.py`
- Create: `deploy/cmms/src/ifactory_cmms_deploy/records.py`
- Modify: `deploy/cmms/src/ifactory_cmms_deploy/cli.py`
- Create: `deploy/compose/cmms-development.env.example`
- Create: `deploy/compose/cmms-bootstrap.env.example`
- Create: `deploy/compose/cmms-frontend.env.example`
- Create: `scripts/cmms-development.sh`
- Create: `tests/e2e/test_cmms_deployment_records.py`
- Modify: `tests/e2e/support/cmms_deployment.py`
- Modify: `tests/e2e/test_cmms_secure_artifacts.py`

**Interfaces:**

- Produces: `RuntimeConfig.load(root: Path) -> RuntimeConfig`
- Produces: `BootstrapConfig.load(root: Path) -> BootstrapConfig`
- Produces: recursive JSON aliases `JsonScalar` and `JsonValue`
- Produces: `SourceBinding(root_sha, root_dirty_fingerprint, root_status, cmms_gitlink, cmms_head, cmms_dirty_fingerprint, cmms_status)`
- Produces: `DeploymentSnapshot(source: SourceBinding, config_sha256, toolchain_manifest_sha256, sensitive_manifest_sha256, unit_generation, state_generation, state_sha256)`
- Produces: `SecureFileStatBinding(logical_file, dev, ino, size, mtime_ns, ctime_ns)`
- Produces: `CredentialPlanBinding(identity, canonical_email, current_file, candidate_file)`
- Produces: `InvitationProbePlanBinding(slot_id, canonical_email, descriptor_file, password_file)`
- Produces: `ApiKeyCapturePlanBinding(attempt_id, api_key_id, label, runtime_user_id, company_id, captured_file)`
- Produces: `ApiKeyCleanupPlanBinding(attempt_id, api_key_id, terminal_outcome, historical_captured_file, observed_captured_file, phase2_env_file)`
- Produces: `BootstrapPlanBindings(credentials, invitation_probe, api_key_capture, api_key_cleanup, role_external_id, api_key_label, phase2_env_logical_id, phase2_env_file)`
- Produces: enums `Operation`, `RuntimeProfile`, `LicenseMode`, `GatewayMode`, `SourceStatus`, `PlanApplicationState`, `ApplicationResultCode`
- Produces: central `ActionCode`, `ActionTargetKind`, targeted `PlannedAction` and `ActionRegistry`
- Produces: `ActionRegistry.validate(operation: Operation, profile: RuntimeProfile, license_mode: LicenseMode, bootstrap_bindings: BootstrapPlanBindings | None, actions: Sequence[PlannedAction]) -> None`
- Produces: `DeploymentPlan.create(snapshot: DeploymentSnapshot, operation, profile, license_mode, bootstrap_bindings: BootstrapPlanBindings | None, actions, now, plan_nonce) -> DeploymentPlan`
- Produces: `canonical_json_bytes(value: JsonValue) -> bytes`
- Produces: `strict_canonical_json_loads(data: bytes, *, max_bytes: int, max_depth: int) -> JsonValue`
- Produces: `write_plan(plan, plans_dir) -> tuple[Path, str]`
- Produces: opaque capability `ConfirmedDeploymentPlan`, constructible only by the record loader after all confirmation checks
- Produces: opaque `PlanAttemptReservation`, `DeploymentWriteLease`, `FailClosedEvidence` and post-claim `ClaimedApplyContext`; all public constructors raise
- Produces: `reserve_plan_attempt(path, confirmed_sha256, now, plans_dir) -> PlanAttemptReservation`
- Produces: `acquire_deployment_write_lease(lock_path, reservation: PlanAttemptReservation) -> DeploymentWriteLease`
- Produces: `load_confirmed_plan(reservation: PlanAttemptReservation, snapshot, current_bootstrap_bindings: BootstrapPlanBindings | None, lease: DeploymentWriteLease) -> ConfirmedDeploymentPlan`
- Produces: `claim_plan_application(plan: ConfirmedDeploymentPlan, plans_dir, lease: DeploymentWriteLease, fail_closed: FailClosedEvidence) -> ClaimedApplyContext`
- Produces: canonical typed `PlanApplicationRecord` and `StateRecord`
- Produces: `FixedRecordSchema(record_name, required_fields, fixed_values)` plus exact structural schemas for `StartPermit`, `BudgetLedger`, `BudgetRecoveryReceipt`, `BootstrapReceipt` and `AcceptanceReceipt`
- Produces: `require_exact_record_fields(value: Mapping[str, JsonValue], schema: FixedRecordSchema) -> None`
- Produces: `PlanApplicationRecord.attempted(plan_sha256: str, now: datetime, application_id: str) -> PlanApplicationRecord`
- Produces: `PlanApplicationRecord.transition(state: PlanApplicationState, now: datetime, secondary_result_codes: Sequence[ApplicationResultCode] = ()) -> PlanApplicationRecord`

- [ ] **Step 1: Write failing strict-config and record tests**

Tests must assert:

- env parsing rejects duplicate/unknown keys, `export`, interpolation, NUL, multiline values and inherited environment fallback;
- all three bootstrap identity emails must already be trimmed ASCII lowercase; uppercase or noncanonical input is rejected rather than silently rebound;
- invitation-probe email/slot is prepared before planning; changing its canonical email, random slot ID or secure-file stat binding invalidates the plan before HTTP;
- bootstrap/repair/start-capable plan records contain the complete non-secret credential/probe binding projection required by their actions; changing any canonical email, logical slot, descriptor/password stat, role/key label or Phase 2 env logical ID changes/rejects the plan;
- continuation/publication plans require the receipt-anchored captured-file stat and current Phase 2 env stat; cleanup-only repair instead requires its anchored terminal outcome, historical stat and exact currently present-or-absent observation; omission or inode/stat substitution fails before HTTP, unlink or secret unwrap;
- `STOP` serializes `bootstrap_bindings=null` and neither constructs nor loads credential/probe slots;
- runtime config requires exactly one canonical `ALLOWED_ORGANIZATION_ADMINS` email; missing, comma-separated or bootstrap/runtime drift is rejected and changes the plan hash;
- all secret references are absolute paths below the approved `.runtime` secret directory;
- ports, origins, project name, invitation/mail/SSO/LDAP values and timezone cannot be changed by env;
- offline requires both license key and license file references; online forbids license file and requires the key;
- `RuntimeProfile.ACCEPTANCE` rejects a dirty root, dirty CMMS, detached gitlink mismatch or uncommitted sensitive manifest;
- canonical JSON rejects duplicate keys, floats, `NaN`, `Infinity`, excessive depth, excessive size, non-UTF-8 and noncanonical bytes;
- plan lifetime is exactly 30 minutes and permit lifetime is at most 60 seconds;
- `plan_nonce` is a fresh cryptographically random 128-bit lowercase hex value per public plan invocation, is non-secret and injectable in tests; regenerating after any consumed application hash must produce a distinct hash even when all evidence and timestamp resolution are otherwise unchanged;
- wrong hash, expired plan, changed source/config/unit generation, reused plan application, and plan/action mismatch fail before a runner call;
- direct `DeploymentPlan.create()` with an unregistered, misordered or branch-invalid tuple fails because the constructor itself invokes `ActionRegistry.validate`; the external planning pipeline is not the only enforcement point;
- wrong hash/noncanonical bytes/expiry fail before an attempt record; a statically valid `apply` exclusively creates and fsyncs one `ATTEMPTED` application reservation before competing for the effect lease, so contention, crash or later drift consumes that exact plan hash;
- `PlanApplicationRecord` accepts exactly the approved fixed fields and UTC timestamp encoding, requires its `plan_sha256` to match the filename, rejects every unknown/missing field and invalid state/generation/nullable/result-code combination, and permits only `ATTEMPTED -> CONTENDED|REJECTED|IN_PROGRESS` plus `IN_PROGRESS -> SUCCEEDED|FAILED`;
- application IDs are injectable 128-bit lowercase hex values in tests; application/result fields remain immutable across transitions, time is monotonic, result codes are ordered/unique/bounded stable enums, and terminal records cannot transition again;
- `REJECTED` is available only for a deterministic post-lease/pre-service-effect confirmation failure; uncertain fail-close leaves `ATTEMPTED`, while uncertain post-claim effect/compensation/terminal persistence leaves `IN_PROGRESS`;
- plans bind the exact absent/present `StateRecord` generation and canonical SHA; a stale plan loaded after another apply is rejected even if source/config are unchanged;
- two distinct valid plans cannot hold the deployment write lease concurrently; the loser durably transitions only its own reservation `ATTEMPTED -> CONTENDED`, has zero gateway/HTTP/receipt/State/service effects and must be regenerated;
- direct construction of `ConfirmedDeploymentPlan` fails; a successful loader returns the capability wrapper and exposes the immutable payload only as `.plan`;
- direct construction/import of production `PlanAttemptReservation`/`DeploymentWriteLease`/`FailClosedEvidence`/`ClaimedApplyContext` mints fails; only the tests-support fixture can create fake capabilities;
- claiming without gateway-produced fail-closed evidence, with evidence for another plan/generation, or before an `IN_PROGRESS` fsync/reopen is rejected;
- registry completeness tests require the frozen rank to contain every `ActionCode` exactly once, verify same-code target ordering, cover every row of the branch-cardinality table and reject one-at-a-time missing/extra/duplicate actions;
- registry tests cover every private tuple class—active/stopped start,
  discovery, all four bootstrap-repair subgrammars, budget-recovery and
  capture-cleanup repair—and the canonical action grammar for restart, stop,
  bootstrap and license switch; they reject non-contiguous completion suffixes,
  a readiness-only tuple containing any mutation, revoke/create/publish
  mixtures, alternative topological ordering, cross-class mixture or
  contextual binding/profile/license mismatch;
- cleanup-only repair accepts exactly `gateway.fail-closed` followed by targeted `repair.finalize-api-key-capture-cleanup`; claim and local preflight are verified as implicit lifecycle barriers, not serialized actions, and no startup/license/authentication/HTTP/raw-unwrap/readiness/dual action is accepted;
- plan/state records and every fixed record schema reject secret-like keys and a test sentinel value;
- parameterized structural-schema tests delete every required field, add one unknown field and mutate every fixed literal for `StartPermit`, `BudgetLedger`, `BudgetRecoveryReceipt`, `BootstrapReceipt` and `AcceptanceReceipt`; receipt schemas require distinct fixed `record_type` discriminators and reject canonical bytes for either other receipt type;
- `StateRecord` rejects missing/unknown fields, has a monotonic generation, cannot roll back, and supplies every binding required by active-start, permit and readiness evidence;
- plan generation prints a bounded action summary and one lowercase SHA-256, never env values.

Include:

```python
def test_confirmed_plan_rejects_changed_source_before_effect(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    plan = safe_runtime.make_plan(operation=Operation.START)
    plan_path, plan_hash = write_plan(plan, safe_runtime.plans_dir)
    changed_source = replace(plan.snapshot.source, cmms_head="1" * 40)
    changed = replace(plan.snapshot, source=changed_source)

    with pytest.raises(DeploymentError) as caught:
        reservation = reserve_plan_attempt(
            plan_path,
            confirmed_sha256=plan_hash,
            now=plan.created_at + timedelta(minutes=1),
            plans_dir=safe_runtime.plans_dir,
        )
        lease = safe_runtime.acquire_deployment_write_lease(reservation)
        load_confirmed_plan(
            reservation,
            snapshot=changed,
            current_bootstrap_bindings=safe_runtime.bootstrap_plan_bindings,
            lease=lease,
        )

    assert caught.value.code == "CMMS-E012"
    assert safe_runtime.runner.calls == []


def test_canonical_json_rejects_duplicate_keys() -> None:
    payload = b'{"schema_version":1,"schema_version":1}\n'
    with pytest.raises(DeploymentError) as caught:
        strict_canonical_json_loads(payload, max_bytes=1024, max_depth=8)
    assert caught.value.code == "CMMS-E011"


def test_plan_application_record_enforces_generation_and_state_graph() -> None:
    attempted_at = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    attempted = PlanApplicationRecord.attempted(
        plan_sha256="1" * 64,
        now=attempted_at,
        application_id="a" * 32,
    )
    claimed = attempted.transition(
        PlanApplicationState.IN_PROGRESS,
        now=attempted_at + timedelta(seconds=1),
    )
    succeeded = claimed.transition(
        PlanApplicationState.SUCCEEDED,
        now=attempted_at + timedelta(seconds=2),
    )

    assert attempted.application_generation == 1
    assert claimed.application_generation == 2
    assert succeeded.application_generation == 3

    with pytest.raises(DeploymentError) as caught:
        attempted.transition(
            PlanApplicationState.SUCCEEDED,
            now=attempted_at + timedelta(seconds=1),
        )
    assert caught.value.code == "CMMS-E011"
```

- [ ] **Step 2: Run the new record tests and verify the expected failure**

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  e2e/test_cmms_deployment_records.py \
  e2e/test_cmms_secure_artifacts.py -v
```

Expected: FAIL because config, record and CLI types are absent.

- [ ] **Step 3: Implement the immutable runtime configuration surface**

Hard-code and validate these non-overridable values:

```text
compose_project          ifactory-cmms-dev
public_browser_origin    http://cmms.localhost:3000
host_health_origin       http://127.0.0.1:3000
container_origin         http://host.docker.internal:3000
api_bind                 127.0.0.1:8082
frontend_bind            127.0.0.1:3001
postgres_bind            127.0.0.1:5433
minio_api_bind           127.0.0.1:9000
minio_console_bind       127.0.0.1:9001
DB_URL                   127.0.0.1:5433/atlas
PUBLIC_API_URL           http://cmms.localhost:3000/api
PUBLIC_FRONT_URL         http://cmms.localhost:3000
PUBLIC_MINIO_ENDPOINT    http://cmms.localhost:3000/storage
STORAGE_TYPE             MINIO
MINIO_BUCKET             atlas-bucket
MINIO_ENDPOINT           http://127.0.0.1:9000
MINIO_REGION_NAME        us-east-1
sigv4_service            s3
MAIL_RECIPIENTS          ""
INTERCOM_TOKEN           ""
INVITATION_VIA_EMAIL     true
ENABLE_EMAIL_NOTIFICATIONS false
ENABLE_MAIL_HEALTH_CHECK false
ENABLE_CORS              false
RATE_LIMIT_ENABLED       true
ENABLE_SSO               false
LDAP_ENABLED             false
CLOUD_VERSION            false
LICENSE_FINGERPRINT_REQUIRED true
TZ                       Asia/Shanghai
```

The runtime env contains PostgreSQL username/database, the single canonical organization-admin email bound as `ALLOWED_ORGANIZATION_ADMINS`, and absolute references for PostgreSQL password, MinIO username/password, JWT, license key and optional offline license file. The bootstrap env contains the same organization-admin identity for cross-checking, the runtime-user identity, role/key labels and current/candidate file references; it is never a systemd `EnvironmentFile`. Require the two organization-admin values to match exactly and reject missing or multi-value input. Reject any identity email unless it is nonempty ASCII, has no surrounding whitespace and equals its own `casefold()` value, because CMMS persists signup email in lowercase while exact `eq` search is case-sensitive. The frontend env contains only `API_URL=/api`, the fixed bind, and non-secret UI switches.

Represent fixed values as frozen fields rather than env defaults:

```python
@dataclass(frozen=True)
class RuntimeConfig:
    compose_project: str = "ifactory-cmms-dev"
    public_browser_origin: str = "http://cmms.localhost:3000"
    host_health_origin: str = "http://127.0.0.1:3000"
    container_origin: str = "http://host.docker.internal:3000"
    api_bind: str = "127.0.0.1:8082"
    frontend_bind: str = "127.0.0.1:3001"
    postgres_bind: str = "127.0.0.1:5433"
    minio_api_bind: str = "127.0.0.1:9000"
    minio_console_bind: str = "127.0.0.1:9001"
```

`load()` parses only the documented mutable keys, then constructs this type; it never passes fixed fields to the env parser.

- [ ] **Step 4: Implement strict canonical records**

Serialize with UTF-8, sorted keys, separators `(",", ":")`, one trailing newline, no floats and explicit schema versions. Compute a plan hash over the canonical body without `plan_sha256`, then store that hash in the final object and verify it on every read.

Use these exact operation values:

```text
bootstrap
start
restart-api
restart-frontend
stop
repair
switch-license
```

Use one registry for every plan-visible effect; Task 8 state transitions reuse these values rather than defining a second enum:

```python
class ActionCode(StrEnum):
    GATEWAY_FAIL_CLOSED = "gateway.fail-closed"
    RUNTIME_INSTALL_CONTROL = "runtime.install-control"
    TOOLCHAIN_INSTALL = "toolchain.install"
    IMAGES_PULL_EXACT = "images.pull-exact"
    COMPOSE_CREATE_STATE_GATEWAY = "compose.create-state-gateway"
    COMPOSE_START_STATE_GATEWAY = "compose.start-state-gateway"
    COMPOSE_STOP_STATE_GATEWAY = "compose.stop-state-gateway"
    BUILD_API = "build.api"
    FRONTEND_VERIFY = "frontend.verify"
    SYSTEMD_INSTALL_UNITS = "systemd.install-units"
    LICENSE_VERIFY_OFFLINE = "license.verify-offline"
    LICENSE_DEBIT_ONLINE_START = "license.debit-online-start"
    LICENSE_RECOVER_UNKNOWN_BUDGET = "license.recover-unknown-budget"
    LICENSE_SWITCH_MODE = "license.switch-mode"
    LICENSE_STOP_GUARD = "license.stop-guard"
    PROCESS_STOP_API = "process.stop-api"
    PROCESS_STOP_FRONTEND = "process.stop-frontend"
    PROCESS_CREATE_API_PERMIT = "process.create-api-permit"
    PROCESS_START_API = "process.start-api"
    PROCESS_START_FRONTEND = "process.start-frontend"
    CMMS_INITIALIZE_FRESH_DATABASE = "cmms.initialize-fresh-database"
    BOOTSTRAP_ROTATE_SUPER_ADMIN = "bootstrap.rotate-super-admin"
    BOOTSTRAP_CREATE_ORGANIZATION = "bootstrap.create-organization"
    BOOTSTRAP_CREATE_ROLE = "bootstrap.create-role"
    BOOTSTRAP_PROBE_INVITATION = "bootstrap.probe-invitation-enforcement"
    BOOTSTRAP_CREATE_INVITATION = "bootstrap.create-invitation"
    BOOTSTRAP_CREATE_RUNTIME_IDENTITY = "bootstrap.create-runtime-identity"
    BOOTSTRAP_CREATE_API_KEY = "bootstrap.create-api-key"
    BOOTSTRAP_FINALIZE_ROLE = "bootstrap.finalize-role"
    BOOTSTRAP_PUBLISH_PHASE2_KEY = "bootstrap.publish-phase2-api-key"
    REPAIR_CAPTURE_BOOTSTRAP_DISCOVERY = "repair.capture-bootstrap-discovery"
    REPAIR_REVOKE_UNCAPTURED_API_KEY = "repair.revoke-uncaptured-api-key"
    REPAIR_FINALIZE_API_KEY_CAPTURE_CLEANUP = "repair.finalize-api-key-capture-cleanup"
    REPAIR_DISCARD_REJECTED_CANDIDATE = "repair.discard-rejected-candidate"
    REPAIR_STOP_LOOPBACK_RUNTIME = "repair.stop-loopback-runtime"
    READINESS_REQUIRE_API_LOOPBACK = "readiness.require-api-loopback"
    READINESS_REQUIRE_LOOPBACK = "readiness.require-loopback"
    GATEWAY_ENABLE_DUAL = "gateway.enable-dual"
    READINESS_REQUIRE_DUAL = "readiness.require-dual"
    READINESS_PROBE_MINIO_ROUTE = "readiness.probe-minio-route"


class ActionTargetKind(StrEnum):
    IDENTITY = "identity"
    INVITATION_PROBE_SLOT = "invitation-probe-slot"
    ROLE_EXTERNAL_ID = "role-external-id"
    API_KEY_LABEL = "api-key-label"
    API_KEY_ID = "api-key-id"
    PHASE2_ENV = "phase2-env"
    COMPOSE_RESOURCE_SET = "compose-resource-set"
    RECEIPT = "receipt"


@dataclass(frozen=True)
class PlannedAction:
    code: ActionCode
    target_kind: ActionTargetKind | None = None
    target_id: str | None = None
```

Targets are canonical, non-secret identifiers already bound elsewhere in the plan: one of the three identity enum values, invitation-probe slot UUID, stable role external ID, stable API-Key label, receipt-bound API-Key ID, the fixed Phase 2 env logical ID, deterministic Compose resource-set ID, receipt logical ID, or `null` for an untargeted infrastructure action. An `API_KEY_ID` target is the canonical decimal representation of a positive Java `Long` (`1..9223372036854775807`), never a UUID, label, signed value or leading-zero decimal; the CMMS client parses and range-checks it again at the HTTP boundary. Registry metadata declares whether a code requires, forbids or permits multiple distinct targets. Bootstrap rotate/create actions themselves authorize promotion only for their one bound identity; role/API-Key creation use their stable semantic targets; revoke requires an already discovered live API-Key ID; capture-cleanup finalization requires `receipt:cmms-bootstrap`; the discard repair code requires an explicit identity target.

`ActionRegistry` maps each enum value to exactly one handler, mutation class,
target policy and allowed operations. Its public validation input is only the
already planned `operation`, `profile`, `license_mode`, `bootstrap_bindings`
and ordered actions; it never reads State, files, processes or HTTP.
`DeploymentPlan.create()` always invokes this validation internally after
checking its scalar fields, so a caller cannot bypass the registry by calling
the constructor directly.

The registry rejects unknown actions, duplicate `(code,target)` rows and
illegal/missing targets. Fail-close is first; stop precedes replacement;
infrastructure install/create precedes start; exactly one mode-appropriate license
prerequisite precedes a permit; permit precedes a new API PID. The distinct
`readiness.require-api-loopback` action proves only the current API
PID/socket, fixed loopback health endpoint and license barrier after a start;
it performs no CMMS authentication or domain reconciliation. It is mandatory
before discovery reads or bootstrap writes. The later
`readiness.require-loopback` is the complete composite pre-open check after
the planned bootstrap state is present, whether produced by same-plan
mutations or by an exact State-selected completed publication. Dual enable
follows that composite check, dual readiness follows enable, and the
acceptance MinIO probe follows dual readiness. Online debit and unknown-budget
recovery are mutually exclusive.

The registry uses a module-private tuple classifier and never serializes or exports a second `PlanBranch` value:

```text
START_ACTIVE
START_STOPPED
REPAIR_DISCOVERY
REPAIR_BOOTSTRAP_MUTATION
    REPAIR_BOOTSTRAP_MUTATION_COMPLETION
    REPAIR_BOOTSTRAP_READINESS_ONLY
    REPAIR_BOOTSTRAP_MUTATION_REVOKE_ONLY
    REPAIR_BOOTSTRAP_MUTATION_DISCARD_ONLY
REPAIR_BUDGET_RECOVERY
REPAIR_CAPTURE_CLEANUP
```

Canonical order is not inferred from a dependency topological sort. It is this
one frozen rank, from first to last:

```text
gateway.fail-closed
process.stop-frontend
process.stop-api
license.stop-guard
compose.stop-state-gateway
runtime.install-control
toolchain.install
images.pull-exact
compose.create-state-gateway
compose.start-state-gateway
build.api
frontend.verify
systemd.install-units
license.switch-mode
license.verify-offline
license.debit-online-start
license.recover-unknown-budget
process.create-api-permit
process.start-api
cmms.initialize-fresh-database
process.start-frontend
readiness.require-api-loopback
repair.capture-bootstrap-discovery
repair.revoke-uncaptured-api-key
repair.discard-rejected-candidate
bootstrap.rotate-super-admin
bootstrap.create-organization
bootstrap.create-role
bootstrap.probe-invitation-enforcement
bootstrap.create-invitation
bootstrap.create-runtime-identity
bootstrap.create-api-key
bootstrap.finalize-role
bootstrap.publish-phase2-api-key
repair.stop-loopback-runtime
readiness.require-loopback
gateway.enable-dual
readiness.require-dual
readiness.probe-minio-route
repair.finalize-api-key-capture-cleanup
```

`cmms.initialize-fresh-database` is not dispatched as a second initializer.
For fresh bootstrap, the `process.start-api` handler first requires that later
row to be present as explicit authorization for the reviewed
`ApplicationInitializer` startup side effect. After the new API PID returns,
the initializer row is dispatched only as a post-start verification of the
expected source-defined company/settings/role/user/plans/bucket result. It
performs no additional write and must pass before frontend start or API
loopback readiness.

For multiple rows with the same code, registry metadata must explicitly allow
multiple distinct targets; their secondary rank is the ASCII tuple
`(target_kind.value, target_id)`. Untargeted rows sort before targeted rows.
The input tuple must already equal that exact ordering. A dependency-equivalent
alternative order is invalid.

Use these cardinality shorthands:

```text
F = exactly one gateway.fail-closed
N = exactly one process.create-api-permit + one process.start-api
L = exactly one license.verify-offline in offline mode, or exactly one
    license.debit-online-start in online mode
B = exactly one readiness.require-api-loopback
P = exactly one readiness.require-loopback + one gateway.enable-dual +
    one readiness.require-dual
I_BOOTSTRAP = each of runtime.install-control, toolchain.install,
    images.pull-exact, build.api, frontend.verify and systemd.install-units
    has cardinality 0..1
I_REPAIR_API = each of runtime.install-control, toolchain.install,
    images.pull-exact, compose.start-state-gateway, build.api and
    systemd.install-units has cardinality 0..1
I_REPAIR_FULL = I_REPAIR_API plus frontend.verify with cardinality 0..1
C_FULL = the nine correctly targeted bootstrap.* rows from
    bootstrap.rotate-super-admin through bootstrap.publish-phase2-api-key,
    in frozen-rank order and including the invitation probe
C_PROBE_RESOLVED = C_FULL with only
    bootstrap.probe-invitation-enforcement removed
C = exactly one suffix of C_FULL or C_PROBE_RESOLVED that ends in
    bootstrap.publish-phase2-api-key
R = exactly one untargeted repair.stop-loopback-runtime
```

Task 8 derives one strict invitation-probe disposition from the
State-selected receipt's `action_attempts`; it does not add another top-level
wire field. The only values visible to planning are:

```text
NOT_ATTEMPTED
PENDING_OR_UNCERTAIN
ENFORCED
UNKNOWN_NO_USER
UNEXPECTED_USER_OR_INVALID
```

For a `bootstrap.probe-invitation-enforcement` attempt, the only wire
`result_code` values are `ATTEMPT_PENDING`,
`INVITATION_PROBE_ENFORCED`, `INVITATION_PROBE_UNKNOWN_NO_USER`,
`INVITATION_PROBE_UNEXPECTED_USER` and
`INVITATION_PROBE_DETERMINISTIC_FAILURE`. No row maps to `NOT_ATTEMPTED`;
`ATTEMPT_PENDING` maps to `PENDING_OR_UNCERTAIN`; the next two same-named
terminal codes map to `ENFORCED` and `UNKNOWN_NO_USER`; the final two, any
duplicate probe attempt not justified by the table, or any invalid
slot/user/result cross-product map to `UNEXPECTED_USER_OR_INVALID`.

The fold over the append-only probe-attempt history is also closed. Zero rows
produce `NOT_ATTEMPTED`. One row produces the disposition mapped from its
current result. A second row is legal only when the first row terminally
resolved `INVITATION_PROBE_UNKNOWN_NO_USER`, the new row belongs to a later
confirmed plan, and its slot ID/email hash are both distinct from the first
row and match that plan's `NEW_DISTINCT` binding. While that second row is
pending, it alone is the effective `PENDING_OR_UNCERTAIN` row and discovery
must bind its slot; when terminal, its result alone is the effective
disposition. The first row remains immutable history used only to prove
distinctness. A second `UNKNOWN_NO_USER` is a safe terminal but exhausted
outcome: it authorizes no third probe or completion tuple. A third row, two
pending rows, a row after `ENFORCED`/unexpected/deterministic failure,
interleaved targets, non-monotonic timestamps, reused attempt/slot/email or
any other sequence is `UNEXPECTED_USER_OR_INVALID`. Thus the table's
`UNKNOWN_NO_USER + NEW_DISTINCT` row applies only to an exact one-row history.

`ENFORCED` means the exact planned probe received the reviewed `406`, the
bounded follow-up search proved zero matching users, that terminal result was
receipt-first/State-anchored and the owned probe slot was retired.
`UNKNOWN_NO_USER` means a separately confirmed discovery-only repair proved
zero users but could not prove the historical response; it also State-anchored
that result and retired the old slot. `PENDING_OR_UNCERTAIN` retains and binds
the exact old slot. An `UNUSED` slot has no ID in any prior probe attempt and
matches its current descriptor/password stats; a `NEW_DISTINCT` slot is
`UNUSED` and additionally differs from the retired
`UNKNOWN_NO_USER` slot. `NONE` means no slot is bound.

At plan/preflight entry, the contextual planner and apply-time preflight
freeze this complete
`(receipt state, probe disposition, slot class) -> C language/start` mapping.
It does not reclassify the immutable same-apply tuple after one of its actions:

| State-selected receipt state | Probe disposition | Required slot | Only legal completion body |
|---|---|---|---|
| no receipt / `UNINITIALIZED` | `NOT_ATTEMPTED` | `UNUSED` | all of `C_FULL`, starting at rotate-super-admin |
| `ADMIN_ROTATED` | `NOT_ATTEMPTED` | `UNUSED` | `C_FULL` suffix starting at create-organization |
| `COMPANY_CREATED` | `NOT_ATTEMPTED` | `UNUSED` | `C_FULL` suffix starting at create-role |
| `ROLE_CREATED` | `NOT_ATTEMPTED` | `UNUSED` | `C_FULL` suffix starting at probe-invitation-enforcement |
| `ROLE_CREATED` | `UNKNOWN_NO_USER` from the exact one-row history | `NEW_DISTINCT` | `C_FULL` suffix starting at probe-invitation-enforcement |
| `ROLE_CREATED` | `ENFORCED` | `NONE` | `C_PROBE_RESOLVED` suffix starting at create-invitation |
| `ROLE_CREATED` | `PENDING_OR_UNCERTAIN` | exact old slot | no `C`; discovery-only repair of that slot |
| `INVITATION_CREATED` | `ENFORCED` | `NONE` | `C_PROBE_RESOLVED` suffix starting at create-runtime-identity |
| `RUNTIME_IDENTITY_CREATED` | `ENFORCED` | `NONE` | `C_PROBE_RESOLVED` suffix starting at create-api-key |
| `API_KEY_CAPTURED` | `ENFORCED` | `NONE` | `C_PROBE_RESOLVED` suffix starting at finalize-role |
| `FINAL_PERMISSIONS_VERIFIED` | `ENFORCED` | `NONE` | publish-only `C_PROBE_RESOLVED` suffix, but only with the exact anchored pre-publication capture lineage |
| `FINAL_PERMISSIONS_VERIFIED` or `GATEWAY_ENABLED` | `ENFORCED` | `NONE` | empty `C` and readiness-only repair, but only with the exact completed-publication predicate below |

For any row from `INVITATION_CREATED` onward, the receipt must also satisfy
Task 8's exact state-specific identity, invitation, API-Key capture and publish
cross-products. A published result whose capture cleanup is still pending
selects capture-cleanup-only first; after that new State-selected generation,
a new plan may select readiness-only. `UNEXPECTED_USER_OR_INVALID`, a
state/result/slot mismatch, a reused retired slot, a pending non-probe action
or any unlisted combination is `UNKNOWN` and produces no completion tuple.
An unexpected created probe user remains blocked on its separately designed
cleanup and is never treated as candidate discard.

Every branch contains `F` and then obeys this closed cardinality table:

The registry enforces every numeric range and every condition derivable from
its five public inputs. Conditions marked `iff` that depend on live
`PlanningAssessment` evidence are structurally `0..1` here; Task 9 must make
their inclusion exact and apply-time preflight must replay that decision.
Every ordinary row containing `N` also lists mandatory `L`: offline means
exactly one offline verification and zero debit, online means exactly one
ordinary debit and zero offline verification. Budget recovery is the sole
`N` branch that forbids ordinary `L` and substitutes its one 10-slot recovery
row.

| Branch | Required exactly | Conditional | Forbidden highlights |
|---|---|---|---|
| `BOOTSTRAP` | `compose.create-state-gateway`, `L`, `N`, `cmms.initialize-fresh-database`, `process.start-frontend`, `B`, `C_FULL`, `P` | `I_BOOTSTRAP`; MinIO probe exactly once iff the acceptance predicate holds | every repair sentinel and stop/recovery action |
| `START_ACTIVE` | `P` | `process.start-frontend` at most once iff the managed frontend is stopped | every license debit/verify/recovery, permit, API start/stop and mutation |
| `START_STOPPED` | `compose.start-state-gateway`, `L`, `N`, `process.start-frontend`, `P` | none | install/build/mutation/recovery and API stop |
| `RESTART_API` | `process.stop-api`, `build.api`, `L`, `N`, `P` | `license.stop-guard` at most once and included iff source mode is offline | every bootstrap/repair mutation and Compose create |
| `RESTART_FRONTEND` | `process.stop-frontend`, `process.start-frontend`, `P` | none | every API stop/start, license debit/recovery and mutation |
| `STOP` | `process.stop-frontend`, `process.stop-api`, `compose.stop-state-gateway` | `license.stop-guard` at most once and included iff source mode is offline | every start, mutation and readiness/dual action |
| `REPAIR_DISCOVERY` | `L`, `N`, `B`, one targeted `repair.capture-bootstrap-discovery`, `R` | `I_REPAIR_API` | every CMMS write, frontend start/verify, composite `P`, dual and MinIO probe |
| `REPAIR_BOOTSTRAP_MUTATION_COMPLETION` | `L`, `N`, `process.start-frontend`, `B`, one legal `C`, `P` | `I_REPAIR_FULL`; MinIO probe exactly once iff the acceptance predicate holds | discovery, revoke, discard, budget recovery, runtime stop and capture cleanup |
| `REPAIR_BOOTSTRAP_READINESS_ONLY` | `L`, `N`, `process.start-frontend`, `B`, `P` | `I_REPAIR_FULL`; MinIO probe exactly once iff the acceptance predicate holds | every bootstrap/repair mutation, discovery, budget recovery, runtime stop and capture cleanup |
| `REPAIR_BOOTSTRAP_MUTATION_REVOKE_ONLY` | `L`, `N`, `B`, one targeted `repair.revoke-uncaptured-api-key`, `R` | `I_REPAIR_API` | every bootstrap.* row, frontend start/verify, composite `P`, dual, MinIO, discovery/discard/recovery/cleanup |
| `REPAIR_BOOTSTRAP_MUTATION_DISCARD_ONLY` | `L`, `N`, `B`, one targeted `repair.discard-rejected-candidate`, `R` | `I_REPAIR_API` | every bootstrap.* row, frontend start/verify, composite `P`, dual, MinIO, discovery/revoke/recovery/cleanup |
| `REPAIR_BUDGET_RECOVERY` | `license.recover-unknown-budget`, `N`, `process.start-frontend`, `P` | `I_REPAIR_FULL`; online mode only | ordinary `L`, `B`, every bootstrap mutation/discovery/cleanup sentinel |
| `REPAIR_CAPTURE_CLEANUP` | only the exact two rows below | none | every other action |
| `SWITCH_LICENSE` | `process.stop-api`, `license.switch-mode`, target-mode `L`, `N`, `P` | `license.stop-guard` at most once and included iff the source mode is offline; `process.start-frontend` at most once iff stopped | recovery, bootstrap/repair mutation and Compose create |

Task 9 includes an `I_BOOTSTRAP`/`I_REPAIR_API`/`I_REPAIR_FULL` row exactly when
`PreflightAssessment` labels that exact code/target
`SATISFIABLE_BY_ACTION`; it rejects a required finding whose code is outside
the branch allowlist. Thus one assessment determines one action set, the
frozen rank determines one order, and neither a caller nor the registry picks
among optional alternatives. The registry recognizes both finite `C`
languages structurally. Task 9 chooses the language and suffix start only from
the frozen receipt/probe/slot mapping above, never from a caller choice or from
`NEXT_STATE` alone.

`START_ACTIVE` excludes API debit/recovery, permit and start actions;
`START_STOPPED` contains exactly one permit/start path and the
license-mode-appropriate prerequisite. Discovery requires targeted
`repair.capture-bootstrap-discovery`, excludes every CMMS write,
composite-readiness/dual/acceptance action and terminates loopback-only.
The bootstrap/API-Key repair family is exactly one of contiguous completion,
readiness-only, revoke-only or candidate-discard-only; those subgrammars are
mutually exclusive and exclude all other repair sentinel classes.
Budget recovery contains only the recovery sentinel plus its exact
startup/readiness prerequisites and excludes bootstrap identity/API-Key
writes. Capture cleanup has exactly the following plan-visible tuple:

```text
gateway.fail-closed
repair.finalize-api-key-capture-cleanup
    target_kind = receipt
    target_id = receipt:cmms-bootstrap
```

`repair.stop-loopback-runtime` is untargeted and legal only as the final row of
discovery, revoke-only or discard-only. After the preceding receipt/State
anchor is reopened, it stops and proves inactive the exact API,
frontend-if-present and offline guard/timer user units, clears their process
identities in State and leaves PostgreSQL, MinIO and loopback-only Nginx
intact. It performs no CMMS request or receipt mutation. An uncertain stop or
State update leaves the application `IN_PROGRESS`; the next plan cannot assume
either active or stopped state.

For capture cleanup, application claim and local preflight are lifecycle
barriers inserted between those two plan-visible rows; they are not
`ActionCode` values. The tuple excludes Compose/systemd startup, license,
permit, authentication, HTTP, raw unwrap, publish/revoke, readiness and
dual-gateway actions. The MinIO route probe is structurally allowed only for
acceptance bootstrap, bootstrap-mutation completion or readiness-only repair;
Task 9's contextual planner and exact preflight replay decide when it is
mandatory. `STOP`, both start classes, each restart, bootstrap, all four
high-level repair forms and license switch therefore have distinct grammar
rather than relying on the broad operation name.

The completed-publication predicate for
`REPAIR_BOOTSTRAP_READINESS_ONLY` is exact and conjunctive:

- the operation is `REPAIR`, and the common repair precondition proves the
  managed API stopped with no active/consuming permit;
- current `StateRecord.bootstrap_receipt_sha256` selects and reopens the typed
  receipt used by planning; State's bootstrap state matches the receipt state,
  and the exact State generation/SHA plus receipt `last_plan_sha256` are bound
  independently;
- receipt state is `FINAL_PERMISSIONS_VERIFIED` or `GATEWAY_ENABLED`, its
  invitation probe disposition is `ENFORCED`, and no action attempt is pending
  or uncertain;
- `phase2_publish_status=PUBLISHED`,
  `api_key_capture_status=ANCHORED_RAW`,
  `api_key_capture_cleared=true`, the historical capture binding is retained,
  the capture path is proven absent, and the current Phase 2 env stat exactly
  matches the State-selected published binding;
- capture cleanup is not pending, there is no discovery/revoke/discard target,
  and the exact source/config/identity bindings required by `B` and `P` are
  current;
- completion reconciliation is still required because the selected state is
  not yet `GATEWAY_ENABLED`, the application named by the receipt's
  `last_plan_sha256` is nonterminal, or an otherwise eligible
  acceptance-profile completion lacks its exact valid acceptance receipt.

If all completion evidence is already terminal and consistent, `repair` is
rejected and ordinary `status`/idempotent `start` applies. Readiness-only never
contains a bootstrap or repair mutation. It starts a fresh stopped-runtime
bundle, performs API-loopback proof, formal read-only reconciliation,
composite pre-open, dual enable/post-open and, when contextually required, the
one planned MinIO probe; success may State-anchor `GATEWAY_ENABLED` and the
acceptance receipt. Apply-time preflight reopens and exact-replays every
predicate above before any startup, debit, permit or HTTP effect.

Tests iterate the enum and require one handler, one immutable `allowed_operations` policy and one target policy per value. They cover each private tuple class and every operation's canonical grammar, then reject unknown, duplicate, target substitution, repeated identity, wrong-operation, missing-dependency, alternative topological ordering, cross-class mixture, profile/license mismatch and action-dependent binding mismatch. The allowed-operation policy may contain both `BOOTSTRAP` and `REPAIR` for a bootstrap action; it is never interpreted as “exactly one Operation.” Planner output serializes each code only through `ActionCode.value` plus canonical `target_kind`/`target_id`; appliers dispatch the complete `PlannedAction` through the same registry.

Every plan binds a fresh non-secret `plan_nonce`, root SHA, CMMS gitlink/head/dirty fingerprint, config digests, toolchain manifest digest, startup-sensitive manifest digest, target profile/mode, expected unit generation, exact StateRecord generation/SHA and an ordered tuple of `PlannedAction`. Its explicit `bootstrap_bindings` field is a canonical, secret-free projection of the inputs required by that action graph: three canonical identity labels/emails with current/candidate logical-file stat bindings, optional invitation-probe UUID/email plus descriptor/password stat bindings, optional receipt-anchored API-Key capture attempt/ID/owner/company plus captured-file stat, optional cleanup-only terminal outcome plus historical/current-presence stats, stable role/API-Key labels, the fixed Phase 2 env logical ID and its current file stat. It contains no absolute path, password/API-Key bytes or content digest. `CredentialSlots.to_plan_bindings()`, `InvitationProbeSlot.to_plan_binding()` and the strict State-selected receipt/capture/cleanup projection are the only constructors.

Fresh bootstrap requires all three candidates and its probe binding; its initial `api_key_capture` and `api_key_cleanup` are `null`, but it binds the current Phase 2 env file and the semantic API-Key target. Results created later in that same claimed apply advance only through Task 8's State-anchored `ApiKeyFileLineage`, never by mutating the immutable plan. Any start/restart/switch/repair action graph that will authenticate identities or perform readiness requires the relevant current/candidate bindings. `invitation_probe` is also mandatory when the receipt contains an unresolved probe attempt and a discovery-only repair must reconcile that exact old slot; a resolved-probe API-Key repair keeps it `null`. Any later-plan continuation/finalization/publication action that will unwrap an already captured API Key requires the exact anchored `api_key_capture`; an unanchored capture cannot be projected into a plan.

Targeted `repair.finalize-api-key-capture-cleanup` instead requires `api_key_capture=null` and exact `api_key_cleanup` built from an already State-selected publish or revoke generation with `api_key_capture_cleared=false`. Its `observed_captured_file` is either the same full stat as `historical_captured_file` or `null` for exact path absence; any third state is invalid. It also binds the current Phase 2 env stat when the terminal outcome is published. It performs no authentication/HTTP/raw unwrap and may only unlink the exact still-present owned file if needed, prove absence, then append/State-anchor the cleanup generation. `STOP` requires `bootstrap_bindings=null` and never opens the bootstrap env or slots. `load_confirmed_plan()` independently rebuilds the required projection from current descriptors and exact-compares it before the first effect. ActionRegistry tests enforce binding presence/absence and action-dependent probe/capture/cleanup requirements, so a semantic `PlannedAction` cannot be detached from the concrete files it authorizes.

A fresh plan does not invent `company_id`, `company_settings_id`, user IDs or role IDs that CMMS has not created yet: those live IDs are receipt-first apply results and later actions consume them only after same-identity readback. Acceptance profile requires `cmms_head == cmms_gitlink` and both worktrees clean.

After static canonical/hash/expiry validation and before competing for the effect lease, public `apply` exclusively creates/fsyncs/reopens that plan hash's `ATTEMPTED` application record and receives an opaque `PlanAttemptReservation`. This append-only per-plan audit reservation is the sole intentional write outside the effect lease. Any existing application record makes the plan non-reusable.

`PlanApplicationRecord` has this exact schema:

```text
schema_version = 1
record_type = cmms-plan-application
application_id
application_generation
plan_sha256
state
attempted_at
claimed_at
terminal_at
safe_result_codes
```

`application_id` is an injectable non-secret 128-bit lowercase hexadecimal
value. `plan_sha256` is lowercase hex64 and the exact basename must be
`{plan_sha256}.application.json`. Timestamps use fixed-six-digit UTC
`YYYY-MM-DDTHH:MM:SS.ffffffZ`; immutable timestamps are monotonic.
Unknown/missing fields are rejected. Valid combinations are:

| State | Generation | `claimed_at` | `terminal_at` | First result code |
|---|---:|---|---|---|
| `ATTEMPTED` | 1 | `null` | `null` | `null` |
| `CONTENDED` | 2 | `null` | required | `APPLY_CONTENDED` |
| `REJECTED` | 2 | `null` | required | `APPLY_REJECTED` |
| `IN_PROGRESS` | 2 | required | `null` | `null` |
| `SUCCEEDED` | 3 | required | required | `APPLY_SUCCEEDED` |
| `FAILED` | 3 | required | required | `APPLY_FAILED` |

`safe_result_codes` is an ordered, unique tuple of at most 32 stable
`ApplicationResultCode` values; each value matches
`[A-Z][A-Z0-9_.-]{0,63}`. Task 2 defines exactly:

```python
class ApplicationResultCode(StrEnum):
    APPLY_CONTENDED = "APPLY_CONTENDED"
    APPLY_REJECTED = "APPLY_REJECTED"
    APPLY_SUCCEEDED = "APPLY_SUCCEEDED"
    APPLY_FAILED = "APPLY_FAILED"
```

A later task may add a named secondary value only to this central enum with
strict-loader and transition tests; no loader accepts an arbitrary matching
string. `transition()` itself derives and prepends the state-mandatory primary
code; callers can supply only declared secondary enum values, never the
primary or an arbitrary string. In the current v1 implementation there are no
secondary values, so every terminal tuple contains exactly its one primary
code. Detailed operation/compensation codes remain in the in-memory
`ApplyResult` and redacted CLI output rather than being copied into this audit
record.

The tuple never contains paths, PIDs, exception text, stacks or upstream
responses. `application_id`, `plan_sha256` and `attempted_at` never change.
Only `ATTEMPTED -> CONTENDED|REJECTED|IN_PROGRESS` and
`IN_PROGRESS -> SUCCEEDED|FAILED` are legal; every transition atomically
replaces, fsyncs, reopens and exact-checks the prior generation, and terminal
records are immutable. `REJECTED` is only a deterministic
post-lease/pre-service-effect confirmation failure. Uncertain fail-close
remains `ATTEMPTED`; uncertain post-claim effect, compensation or terminal
persistence remains `IN_PROGRESS`.

Public `apply` then securely opens—or on first use exclusively creates—the exact zero-length current-UID `0600` `.runtime/cmms-development-apply.lock` through its verified parent `dir_fd`, using `O_RDWR|O_CLOEXEC|O_NOFOLLOW` and requiring a regular single-link file. It takes a nonblocking exclusive `flock`; lock-file contents are never used as evidence. On contention it atomically changes only the losing reservation to `CONTENDED`, fsyncs/reopens it and returns busy before gateway/HTTP/receipt/State/service effects. Thus the losing hash is mechanically consumed even when the owner later fails before changing State.

The opaque `DeploymentWriteLease` owns the live lock FD/process identity and is held across current snapshot capture, confirmation load, pre-claim fail-close, claim, every local/remote action, receipt/State anchors, compensation and terminal application update. Process death releases the OS lock but leaves `ATTEMPTED` or `IN_PROGRESS` evidence. A second plan cannot wait and then continue on old evidence; it must be regenerated. `plan`, `status` and `secret` do not acquire this effect lease: planning uses stable before/after local generation/SHA reads and aborts on drift, while every apply-time secret/env open is descriptor/stat-lineage bound and aborts on a concurrent secret change. This is an apply-effect single-writer lock, not a blanket lock on every `.runtime` file. Emergency monotonic fail-close remains the safety exception.

`ConfirmedDeploymentPlan` wraps the immutable `DeploymentPlan`, its exact live `PlanAttemptReservation`, the lease identity and a module-private authorization token. Its public constructor raises; only `load_confirmed_plan()` may mint it under that lease after checking the reservation, current snapshot/state generation+SHA and current bootstrap bindings. Static CLI hash/canonical bytes/expiry were already checked by `reserve_plan_attempt`; either stage's failure leaves or terminally rejects the consumed reservation and never makes the hash reusable. It authorizes only the pre-claim fail-close/claim sequence. Task 2 fully implements the opaque `FailClosedEvidence`/`ClaimedApplyContext` data types and validation protocol. `claim_plan_application()` requires the same live lease plus authentic gateway evidence, atomically transitions/reopens that reservation from `ATTEMPTED` to `IN_PROGRESS`, then returns the context containing that exact plan/application/lease/evidence binding. Every post-claim controller that can authenticate, widen access or mutate non-safety state accepts that stronger capability, never a plain `DeploymentPlan` or bare confirmed wrapper. The sole capability-free exception is monotonic `emergency_fail_closed`: it may only remove the external listener or stop Nginx, cannot mint claim evidence and cannot write identity/receipt/domain state.

Tasks 3–8 unit-test their adapters with `SafeRuntimeFixture.claimed_context()`, a tests-only factory that supplies fake lease/fail-closed/application evidence and cannot be imported by the production package. Task 4 supplies the production gateway evidence factory; Task 9 merely orchestrates the already implemented lease/claim path and enables public `apply`. Contract tests inspect production exports and reject any public/test backdoor constructor.

Reserve a statically valid apply with exclusive creation of `.runtime/plans/cmms-development/{plan_sha256}.application.json` in state `ATTEMPTED`. Its strict state machine is `ATTEMPTED -> CONTENDED|REJECTED|IN_PROGRESS`; only `IN_PROGRESS -> SUCCEEDED|FAILED`, and unknown pre-claim/claimed outcomes remain `ATTEMPTED`/`IN_PROGRESS`. Lifecycle first obtains the effect lease, fully confirms the reservation/current snapshot, fail-closes the gateway and proves the external listener absent, then transitions/fsyncs/reopens `IN_PROGRESS` before any other service effect. An existing record in any state makes that plan non-reusable. A crash at either nonterminal state consumes the hash, and recovery starts with a newly generated reconciliation plan.

The remaining receipt/permit/budget paragraphs in this step are frozen
downstream contracts tied to `StateRecord`; Task 2 implements their structural
schemas only. Their typed loaders, constructors and persistence are delivered
by the owning tasks named below and are not hidden Task 2 work.

Task 8 persists `BootstrapReceipt` only as immutable `0600` generations at `.runtime/receipts/cmms-bootstrap/{receipt_sha256}.json` below a current-UID `0700` directory; derive the filename only from a recomputed lowercase canonical SHA-256. `StateRecord.bootstrap_receipt_sha256` is nullable and is the sole authority for the current generation. Never select the newest filename, mtime or highest embedded generation by scanning. Task 10 persists the distinct clean-profile `AcceptanceReceipt` only at `.runtime/receipts/cmms-development.json`. Each owning typed loader requires its own schema discriminator and rejects the other record type; neither path aliases `cmms-development-state.json`.

Task 6 budget recovery uses the exact private directory `.runtime/receipts/cmms-budget-recovery/` mode `0700` and one file `${plan_sha256}.json` mode `0600`. Its `record_type=cmms-budget-recovery-receipt` typed loader derives the path from an already confirmed lowercase plan hash; arbitrary paths are impossible. Exclusive create writes `PENDING` before quarantine/ledger effects, then the same record may transition once to `SUCCEEDED` or `FAILED`. A pre-existing file in any state makes the plan non-reusable.

Define one complete `StateRecord` rather than growing ad hoc fields:

```text
schema_version = 1
record_type = cmms-development-state
generation
root_sha
root_dirty_fingerprint
root_status
cmms_gitlink
cmms_head
cmms_dirty_fingerprint
cmms_status
config_sha256
toolchain_manifest_sha256
sensitive_manifest_sha256
api_artifact_sha256
frontend_lock_sha256
controller_entrypoint_sha256
controller_package_sha256
unit_generation
compose_project
postgres_volume_name
postgres_volume_identity
minio_volume_name
minio_volume_identity
api_main_pid
api_process_start_ticks
frontend_main_pid
frontend_process_start_ticks
docker_gateway_ipv4
loopback_gateway_sha256
gateway_generation
gateway_mode
license_mode
license_guard_generation
online_budget_ledger_sha256
latest_budget_debit_id
latest_budget_sequence
latest_budget_local_date
bootstrap_receipt_sha256
bootstrap_state
last_plan_sha256
last_operation
last_transition_code
updated_at
```

Task 2 freezes only the exact top-level structural envelope for each record
owned by a later task. `FixedRecordSchema` is not a typed loader and cannot
authorize an operation: it checks the complete keyset, the fixed
literal values shown with `=`, the recursive secret-field ban and canonical
JSON shape. The owning task must build the typed loader,
bounded enum/nested-row validators, legal nullability combinations and
constructors before that record can be read as evidence. Later tasks may not
add, remove, rename or silently default a top-level field.

`StartPermit`:

```text
schema_version = 1
nonce
plan_sha256
created_at
expires_at
uid
root_sha
cmms_source_fingerprint
api_artifact_sha256
controller_entrypoint_sha256
controller_package_sha256
unit_generation
loopback_gateway_sha256
docker_gateway_ipv4
license_mode
budget_debit_id
```

`BudgetLedger`:

```text
schema_version = 1
zone = Asia/Shanghai
local_date
controlled_limit = 10
source_limit = 20
attempts
previous_ledger_sha256
continuity_state
```

Task 6 owns the exact bounded `BudgetDebit` row inside `attempts`, the initial
empty-ledger values and all continuity/date semantics. Task 2 does not return a
typed `BudgetLedger`.

`BudgetRecoveryReceipt`:

```text
schema_version = 1
record_type = cmms-budget-recovery-receipt
status
plan_sha256
local_date
unknown_reason_code
original_bytes_present
original_ledger_sha256
recovery_debit_id
api_main_pid
api_process_start_ticks
safe_result_code
created_at
updated_at
```

Task 6 owns the typed recovery-receipt loader, status/nullability state machine
and constructors. Task 2 validates only this fixed structural envelope.

`BootstrapReceipt`:

```text
schema_version = 1
record_type = cmms-bootstrap-receipt
receipt_generation
origin_plan_sha256
last_plan_sha256
state
super_admin_user_id
company_id
company_settings_id
organization_admin_user_id
role_id
invitation_email_hash
runtime_user_id
api_key_id
api_key_label
api_key_capture_attempt_id
api_key_capture_status
api_key_capture_file
phase2_publish_status
phase2_env_file
api_key_capture_cleared
revoked_api_key_ids
action_attempts
probe_user_id
action_result_codes
created_at
updated_at
```

Task 8 owns the exact bounded action-attempt row, capture/publish state
cross-products, typed loader/store and receipt transition graph. There is no
empty wire generation: Task 8 creates generation 1 only when it can include
the pending attempt for the first planned non-idempotent action, then anchors
that immutable generation through `StateRecord` before sending the request.
Task 2 neither constructs nor returns a typed `BootstrapReceipt`.

`AcceptanceReceipt`:

```text
schema_version = 1
record_type = cmms-acceptance-receipt
plan_sha256
root_sha
cmms_gitlink
cmms_head
api_artifact_sha256
controller_entrypoint_sha256
controller_package_sha256
unit_generation
api_main_pid
api_process_start_ticks
gateway_ipv4
gateway_generation
license_mode
company_id
organization_admin_user_id
runtime_user_id
role_id
api_key_id
asset_total
work_order_total
preopen_report_sha256
preopen_check_codes
postopen_report_sha256
postopen_check_codes
minio_probe_result_sha256
minio_probe_check_codes
minio_cleanup_code
checked_at
```

Task 10 owns the typed loader/constructor, fixes `asset_total=0`,
`work_order_total=0`, `minio_cleanup_code=CLEAN` and the bounded readiness-code
vocabulary. Task 2 only rejects unknown/missing top-level fields and
cross-record discriminators; it cannot manufacture or interpret an acceptance
receipt.

Nullable process/guard/budget fields are explicit JSON `null`, never omitted. Volume identity is a bounded digest over the exact Compose volume name, labels and creation timestamp, not an absolute Docker path or inspect object. Every state change locks, reopens the current canonical record, requires the expected prior generation, writes `generation + 1`, fsyncs and reopens the result; an older generation or mismatched last-plan transition is rollback/conflict.

Write fields only from post-effect evidence. Starting/restarting sets the verified source/artifact/controller/unit/process bindings; frontend restart changes only frontend identity plus transition metadata; dual enable changes gateway digest/generation/mode only after listener proof; license switch changes mode/guard and new start bindings. Every successful logical mutation of `BootstrapReceipt`—under `BOOTSTRAP` or `REPAIR`, including discovery capture, revoke and candidate discard—uses one immutable-generation receipt-first anchor protocol: load the exact prior generation named by `StateRecord.bootstrap_receipt_sha256`; construct the next monotonic `receipt_generation`; compute its canonical SHA; exclusively create/fsync/reopen `{sha}.json` without replacing any file; then CAS-update `StateRecord.bootstrap_receipt_sha256` plus `last_plan_sha256`/transition metadata (and `bootstrap_state` only when the formal state actually changed). Finally reopen the State-selected generation and cross-check both before the plan application may become `SUCCEEDED`. A crash before CAS leaves the old pointer and bytes valid and the new file an unauthorized orphan; a crash after atomic CAS selects the complete new immutable file. Directory scans, orphan files and mtimes can never authorize state. Discovery keeps `bootstrap_state` unchanged; revoke returns it to `RUNTIME_IDENTITY_CREATED` as specified in Task 8.

A crash or conflict between receipt generation publication, State pointer CAS and terminal application update is `UNKNOWN` for that application, but it never destroys the previously selected receipt. The consumed application remains `IN_PROGRESS`; a new confirmed discovery/reconciliation repair loads only the generation selected by the current State pointer, reproduces any additional live evidence, writes another immutable generation and anchors it before normal planning resumes. Task 8 applies a stricter rule to one-time API-Key raw capture: an unselected/orphan receipt generation is ignored completely and authorizes nothing. Discovery may observe only the owned unanchored capture-file state plus fresh company-scoped live metadata; it must write a new `REVOKE_REQUIRED` generation and make State select it, after which a separate new plan may authorize exact revoke/cleanup—never publication. `stop` and bootstrap/repair compensation clear current process identities and set gateway mode to `LOOPBACK` or `STOPPED` from observed evidence, while retaining volume, build, source, bootstrap and budget history. An idempotent active `start` must match every non-null source/artifact/controller/unit/API process/gateway-address/license/budget binding before it may retain the same PID. Tests cover each transition, stale expected generation, crash at every receipt/State/application boundary, orphan generations, unknown/missing fields and forbidden clearing of durable anchors.

Use one canonical encoder for every record:

```python
type JsonScalar = None | bool | int | str
type JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


def canonical_json_bytes(value: JsonValue) -> bytes:
    reject_floats_nonfinite_depth_and_secret_fields(value, max_depth=16)
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def plan_sha256(body: Mapping[str, JsonValue]) -> str:
    unsigned = dict(body)
    unsigned.pop("plan_sha256", None)
    return hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()
```

- [ ] **Step 5: Add the thin wrapper and records-only CLI**

`scripts/cmms-development.sh` must resolve the repository root without using `$HOME`, change to it, and execute:

```bash
exec uv run --project deploy/cmms --frozen --no-dev \
  cmms-development "$@"
```

At this task, `status`, `secret`, `plan`, `apply` and `internal` return a stable `CMMS-E020 command-not-available` code. The tests exercise `DeploymentPlan.create()`, `write_plan()`, `reserve_plan_attempt()`, lease acquisition and `load_confirmed_plan()` directly with a complete fixture snapshot. Public `plan` cannot be enabled until Task 9 can compute source, preflight and exact operation actions; do not fabricate a partial plan or successful apply.

- [ ] **Step 6: Run the plan/config tests**

Run:

```bash
set -Eeuo pipefail
chmod +x scripts/cmms-development.sh
uv run --directory tests --frozen pytest \
  e2e/test_cmms_deployment_records.py \
  e2e/test_cmms_secure_artifacts.py -v
```

Expected: PASS.

- [ ] **Step 7: Run slice verification**

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  e2e/test_cmms_deployment_records.py \
  e2e/test_cmms_secure_artifacts.py -v
test -x scripts/cmms-development.sh
git diff --check
./scripts/doctor.sh
```

Expected: both pytest files pass, the wrapper is executable and doctor has zero failures.

- [ ] **Step 8: Commit the plan/confirmation slice**

Run:

```bash
set -Eeuo pipefail
git add \
  deploy/cmms/src/ifactory_cmms_deploy/config.py \
  deploy/cmms/src/ifactory_cmms_deploy/records.py \
  deploy/cmms/src/ifactory_cmms_deploy/cli.py \
  deploy/compose/cmms-development.env.example \
  deploy/compose/cmms-bootstrap.env.example \
  deploy/compose/cmms-frontend.env.example \
  scripts/cmms-development.sh \
  tests/e2e/support/cmms_deployment.py \
  tests/e2e/test_cmms_deployment_records.py \
  tests/e2e/test_cmms_secure_artifacts.py
git commit -m "feat: add CMMS deployment plan confirmation gate"
```

Expected: the commit is root-only and no real plan has been applied.

### Task 3: Pin Application Toolchains and the Reviewed CMMS Source Baseline

**Files:**

- Create: `deploy/cmms/manifests/toolchains.json`
- Create: `deploy/cmms/manifests/startup-sensitive-files.json`
- Create: `deploy/cmms/maven-settings.xml`
- Create: `deploy/cmms/src/ifactory_cmms_deploy/toolchains.py`
- Create: `deploy/cmms/src/ifactory_cmms_deploy/source.py`
- Create: `tests/e2e/test_cmms_toolchains_and_source.py`
- Modify: `deploy/cmms/src/ifactory_cmms_deploy/records.py`

**Interfaces:**

- Produces: `ToolchainManifest.load(path) -> ToolchainManifest`
- Produces: `SensitiveManifest.load(path) -> SensitiveManifest`
- Produces: `ToolchainInstaller.plan_status(runtime_root) -> Sequence[ToolchainStatus]`
- Produces: `ToolchainInstaller.install_all(context: ClaimedApplyContext) -> ToolchainReceipt`
- Produces: `SourceInspector.capture(root: Path, profile: RuntimeProfile) -> SourceBinding`
- Produces: `verify_sensitive_baseline(cmms_root, manifest) -> SensitiveBaselineResult`
- Produces: `build_api_artifact(context: ClaimedApplyContext, toolchains, runner) -> BuildArtifact`
- Produces: `install_frontend_dependencies(context: ClaimedApplyContext, toolchains, runner) -> DependencyReceipt`

- [ ] **Step 1: Write failing manifest, extraction and source tests**

Tests must assert:

- manifest schema, platform and every URL/version/hash below are exact;
- URLs are HTTPS and neither URL nor redirect may select `latest`;
- Maven uses only the tracked no-credential settings file and a task-local repository, never user `~/.m2/settings.xml`;
- download uses a temporary regular file, fixed `curl --proto =https --proto-redir =https --tlsv1.2 --location`, checksum before extraction, safe relative archive members and atomic publish;
- archive traversal, device entry, absolute path, escaping symlink/hardlink, duplicate target and checksum mismatch fail without replacing an installed tool;
- installed tools are accepted only when both archive receipt and executable version checks match;
- ambient Java 25, Maven 3.6.3 or Node 26 are never selected as fallback;
- source snapshot independently distinguishes root/CMMS cleanliness, binds both dirty fingerprints, hashes bounded untracked files without storing their contents, and rejects changed sensitive files in both profiles;
- API build uses only fixed Maven/JDK paths and copies exactly `target/app.jar`, matching the reviewed `<finalName>app</finalName>`, into a fingerprinted runtime build artifact;
- confirmed source verification runs API tests before packaging;
- frontend install uses exactly `npm ci --legacy-peer-deps`, runs a build verification, and rejects a `package-lock.json` hash change;
- frontend install sets `HUSKY=0`, preserves the component-local `core.hooksPath`, and cannot create an untracked `.husky`;
- no unit test performs network or executes Maven/npm.

Include:

```python
ROOT = Path(__file__).resolve().parents[2]
TOOLCHAIN_MANIFEST = ROOT / "deploy/cmms/manifests/toolchains.json"
SENSITIVE_MANIFEST = ROOT / "deploy/cmms/manifests/startup-sensitive-files.json"


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


def test_sensitive_change_blocks_development_gateway(
    cmms_fixture: CmmsSourceFixture,
) -> None:
    cmms_fixture.modify("api/src/main/java/com/grash/ApplicationInitializer.java")
    result = verify_sensitive_baseline(
        cmms_fixture.root,
        SensitiveManifest.load(SENSITIVE_MANIFEST),
    )
    assert result.ok is False
    assert result.code == "SENSITIVE_BASELINE_CHANGED"
```

- [ ] **Step 2: Run the toolchain/source tests and verify the expected failure**

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  e2e/test_cmms_toolchains_and_source.py -v
```

Expected: FAIL because manifests and implementations are absent.

- [ ] **Step 3: Add the exact `linux/amd64` toolchain manifest**

The manifest must contain these immutable rows:

| Tool | Version | Official archive | SHA-256 |
|---|---|---|---|
| Temurin JDK | `17.0.19+10` | `https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.19%2B10/OpenJDK17U-jdk_x64_linux_hotspot_17.0.19_10.tar.gz` | `400fc5b6d000c158d5ee7937543faa06b6bda8408caa2444a9c947c21472fde0` |
| Maven | `3.9.3` | `https://archive.apache.org/dist/maven/maven-3/3.9.3/binaries/apache-maven-3.9.3-bin.tar.gz` | `e1e13ac0c42f3b64d900c57ffc652ecef682b8255d7d354efbbb4f62519da4f1` |
| Node.js | `21.6.1` | `https://nodejs.org/dist/v21.6.1/node-v21.6.1-linux-x64.tar.xz` | `c65cbf7342260df8e59dd2fe2e06dc1f36ac46c9d433a64cd84521fd4915c291` |

Record archive kind, one top-level directory, strip-components count `1`, and expected executable/version probes:

```text
bin/java  -> openjdk version "17.0.19"
bin/mvn   -> Apache Maven 3.9.3
bin/node  -> v21.6.1
bin/npm   -> 10.2.4
```

The control Python is separately pinned by `.python-version` and the frozen control project. A later approved systemd installation must use a prepared `.runtime` venv and bind its interpreter/entrypoint SHA into the unit generation; units must not call `uv run`.

- [ ] **Step 4: Add the credential-free Maven settings**

Use this complete repository policy:

```xml
<?xml version="1.0" encoding="UTF-8"?>
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
```

The file contains no proxy, server credential, local repository or active profile. The command supplies the task-local repository path explicitly.

- [ ] **Step 5: Add the exact startup-sensitive baseline**

Set `cmms_gitlink` to `f3cab0aaf3418638e76dc33dfa5f8b30ded2b7f0`. Use the following file digests:

```text
2c4922c1a567fa97935890c30b271c0a0097ad247f7976ab3623f4c6e741f7ca  api/pom.xml
5050d82e0f4ac79c2fd121b411017d315afa15ebdfe3ad14bff659293cc54146  api/Dockerfile
1da45ceb2b360601dcd707c3e8e36fc1e385ecc9d9bfc30a34a002e3ed6e0279  api/src/main/java/com/grash/ApplicationInitializer.java
8ede079994781c58604420e52233b730c37eda5f07724e5ae0e4f58f9b85d357  api/src/main/java/com/grash/advancedsearch/SearchCriteria.java
cd43cd577c7d41dbea2ba2a62a5d0c8fa67544d77ee30ef25b8abf8453d33fec  api/src/main/java/com/grash/configuration/WebSecurityConfig.java
bcabf88bce0c081a0ae020401bb2bade1bc9bfd9d55046342774fe3037e1ab22  api/src/main/java/com/grash/controller/AssetController.java
f51824a7fce40209c0f6fca215b3ed20d63b7866685399ad01ee0271ae76858e  api/src/main/java/com/grash/controller/AuthController.java
faa8ccf1ab316162ac7f784fab7713a06ff11bc87251e7890993761b61c969f3  api/src/main/java/com/grash/controller/CompanyController.java
e08202378a5d9b5df6f7f33c081844bd2472c9338e3cfa8d2c53fc7f27869716  api/src/main/java/com/grash/controller/UserController.java
b8e04e05a507a5a342e631e48de3d9c3debb27a5738f2407acd41b8707939080  api/src/main/java/com/grash/controller/RoleController.java
376c622f7546ad9d629d61ae0f0ef943f2a75de23f19e23cdf73eb57732eea66  api/src/main/java/com/grash/controller/ApiKeyController.java
8bd6e0c661615f8d4af41fc6225b9757a69b6ae88f09b7ce2abead004f559eab  api/src/main/java/com/grash/controller/LicenseController.java
c2dba0f35f89922552d5fd427e9eeafb3923dab7ddcdd30b0388e864d50473cf  api/src/main/java/com/grash/controller/WorkOrderController.java
af733cf19c5c8e319a244b4d2e6ce6dac434713854d987b5797c6f2f36c4110e  api/src/main/java/com/grash/dto/CompanyShowDTO.java
abd3377431cc5b353e949b8ef5a3fca601dbe8c05882460fe1baecff7e2885dd  api/src/main/java/com/grash/dto/UserInvitationDTO.java
6b373999ee908b302609c034ccfd6e12ee9d00816113803d20b4871b537af2e7  api/src/main/java/com/grash/dto/UserResponseDTO.java
2dc6acba56edbd94e85c31659397a495d218d53105a19030ef39e41e767fe352  api/src/main/java/com/grash/dto/UserSignupRequest.java
a8dec36ff6573a86a93926b96feba89630f3c2e10837e327dfe694b9bf78070e  api/src/main/java/com/grash/dto/UpdatePasswordRequest.java
04a367d6d4b6222f164f898444a8fe264186bd5b24909b254d6b96a9c6a440d9  api/src/main/java/com/grash/dto/RolePatchDTO.java
ba2b686409c323452076a59d31f22c0ac5308182e08c3f8ee52450c6ca0d6ad0  api/src/main/java/com/grash/dto/apiKey/ApiKeyPostDTO.java
c08052f66e34cc68fbda2ef1aa7f8067bf6a4866d41b3ef919bda4bd3e2cd213  api/src/main/java/com/grash/dto/apiKey/ApiKeyShowDTO.java
08c4c0ae5654aeff8c93fe4e179a3c0b1d424334f7be7c216f0d1666a7ea699e  api/src/main/java/com/grash/dto/license/LicensingState.java
630cd292f808ce3a2d438c2e907ec29ec23514401b27641daca6005e8be4681d  api/src/main/java/com/grash/mapper/ApiKeyMapper.java
31b3f8c657bb262eb17b3e62f436a3ede84887ff244dccae5fc9fd1daea59d3a  api/src/main/java/com/grash/model/Role.java
17ac2e499dac9f79e9016d30e08dc2a733dedb5442500b8bed6841a11d781377  api/src/main/java/com/grash/model/enums/PermissionEntity.java
be52c0e0c30e84c3dea7e1d01265ccee6c4c0e25c402ef257f79240d0440d59d  api/src/main/java/com/grash/repository/UserInvitationRepository.java
ecad771d33deb5e2151984300eb66078445c249d35329faf9df4e5507389e9f7  api/src/main/java/com/grash/security/ApiKeyAuthFilter.java
1f2209c72aaa90ea0530c3f31cb72779e8471d4a662d14b865d5e2db98a6aa04  api/src/main/java/com/grash/service/ApiKeyService.java
72cc60854dc7ff30e279dd687f2a652cd45605bbc86270c83318ebdd58c2df02  api/src/main/java/com/grash/service/AssetIntegrationService.java
431b89ad6fbb035f93409abbc313150da30d27836db823c9685945ed5db697a6  api/src/main/java/com/grash/service/UserService.java
2d81452647053d42bc9b8a2e22f3d77c815ce37f52e236897887172c6bcdef2d  api/src/main/java/com/grash/service/RoleService.java
e3c4df47ed6439128b1caa6e29a33cbbb1c29a8e878bca736df1be1023d42c07  api/src/main/java/com/grash/service/LicenseService.java
9e564aff418a77e96104ea4c70cbefe80fa5c3230479aa4823afd90910558abf  api/src/main/java/com/grash/service/IntercomService.java
9753a44ccb36be08c8a6fc2170794fd3090f030cbd4a584f4e6bbbe395f59b07  api/src/main/java/com/grash/service/MinioService.java
8b71fac66625d3c43b23edacd944e398eba7c59dde022d6dd56ef35c577181ea  api/src/main/resources/application.yml
68f67c990a6dccfbb9ee77325d68d20b6f0aa9954fc6e7c65a27d3d868b2abe1  frontend/Dockerfile
b52191055ea0ef64c0dae83ad50fc6923d32202678bd314e7dc6b38db4f1f669  frontend/package.json
484fdb709b700a50daaaf252f48cc9e11fb096401b52c8f4ef6d47de9100d46b  frontend/package-lock.json
853c40596c8f9f01691dd0e8dc12e77be6d05577951abf7b3ea736578beabfca  frontend/src/config.ts
```

Also record the tree digest `9f76c157b2d1953d89c32494f8a173ec7acc40b038d66decdbc16ba0d714a331` for `api/src/main/resources/db`. Define that tree digest as SHA-256 over sorted tracked-file lines:

```text
sha256(file_bytes) + "  " + cmms_relative_path + "\n"
```

Any intentional change to one of these files or the migration tree requires a reviewed manifest update tied to a new CMMS gitlink before the gateway may open.

- [ ] **Step 6: Implement safe installation and source/build receipts**

Download only inside a confirmed apply. Stream to an exclusive file below `.runtime/cmms-staging/`, cap Temurin at 256 MiB, Maven at 32 MiB and Node at 64 MiB, verify SHA-256, safely extract into a temporary directory, run the version probes with fixed paths, then atomically rename to `.runtime/toolchains/${tool}-${version}`. Existing matching installs are reused; existing mismatches fail without overwrite.

For source snapshots:

- clean profile: independently bind root SHA/status, root-recorded gitlink, CMMS HEAD/status and require both worktrees clean;
- development profile: independently hash each repository's porcelain-v2 status, tracked diff, index diff and bounded untracked-file hashes, but never persist diff bytes;
- both profiles: verify every startup-sensitive digest before build or permit creation.

The root dirty fingerprint covers every tracked/untracked root control file that can influence this deployment, including `deploy/cmms`, Compose/Nginx/systemd templates, the wrapper script and the root test/control lockfiles. A development plan may bind a dirty root, but its plan loader, control-runtime installation, unit generation, permit and readiness all require the same root fingerprint. An acceptance plan rejects either dirty repository. A clean root with a dirty CMMS, or the inverse, is represented explicitly rather than collapsed into one ambiguous status.

The confirmed bootstrap and acceptance-profile API verification/build commands are:

```text
"${maven_home}/bin/mvn" --settings "${root}/deploy/cmms/maven-settings.xml" "-Dmaven.repo.local=${maven_cache}" "-Dtest=AssetControllerTest,WorkOrderControllerTest,AssetIntegrationServiceTest,IntegrationIdempotencyServiceTest,WorkOrderServiceTest" -Dsurefire.failIfNoSpecifiedTests=true test
"${maven_home}/bin/mvn" --settings "${root}/deploy/cmms/maven-settings.xml" "-Dmaven.repo.local=${maven_cache}" clean package -DskipTests
```

Run both with exact cwd `${root}/components/cmms/api`, `JAVA_HOME="${temurin_home}"` and a fixed PATH. The first command deliberately selects the five reviewed Mockito/MockMvc tests and excludes `ApiApplicationTests` plus `com.grash.integration.*`, all of which inherit `AbstractTestContainer` and otherwise select the unpinned `postgres:16-alpine` tag plus Testcontainers helper images. `surefire.failIfNoSpecifiedTests=true` prevents a typo from silently running zero tests. Both commands must pass. The controller does not run, rewrite, retag or claim coverage from that Docker-backed suite; running the full upstream suite requires a separate reviewed action that pins every application/helper image and is outside this root-only deployment plan. A development-profile `restart-api` may run only the second command for fast feedback, but its state remains `UNCOMMITTED` and cannot produce acceptance evidence. Require the exact regular file `target/app.jar`, reject unexpected competing JAR candidates, hash it, copy it atomically to `.runtime/cmms-builds/`, and bind that hash into state/unit generation.

The frontend dependency command is:

```text
"${node_home}/bin/npm" ci --legacy-peer-deps --cache "${npm_cache}"
"${node_home}/bin/npm" run build
```

Run both in `components/cmms/frontend`, with `HUSKY=0`, the npm cache below `.runtime/cmms-cache/`, and `NPM_CONFIG_USERCONFIG` pointing to a verified task-specific empty file instead of inheriting user npm config. Compare component-local `core.hooksPath`, `package-lock.json` and Git status before/after; only already-ignored `node_modules`, `build` and `public/runtime-env.js` may appear. Development HMR still uses `npm start`; the build is a source/lock verification, not the served artifact.

Implement archive path validation before extracting any member:

```python
def checked_archive_target(root: Path, member_name: str) -> Path:
    member = PurePosixPath(member_name)
    if member.is_absolute() or ".." in member.parts:
        raise DeploymentError("CMMS-E031", "toolchain archive is unsafe", 31)
    target = root.joinpath(*member.parts).resolve(strict=False)
    if not target.is_relative_to(root.resolve()):
        raise DeploymentError("CMMS-E031", "toolchain archive is unsafe", 31)
    return target
```

Reject device/FIFO entries and validate relative symlink/hardlink targets with the same function. Extract only after every member passes the complete pre-scan.

- [ ] **Step 7: Run the toolchain/source slice verification**

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  e2e/test_cmms_toolchains_and_source.py \
  e2e/test_cmms_deployment_records.py -v
git diff --check
./scripts/doctor.sh
```

Expected: tests pass without network/Maven/npm, manifests match the current CMMS gitlink and doctor has zero failures.

- [ ] **Step 8: Commit the toolchain/source slice**

Run:

```bash
set -Eeuo pipefail
git add \
  deploy/cmms/manifests \
  deploy/cmms/maven-settings.xml \
  deploy/cmms/src/ifactory_cmms_deploy/toolchains.py \
  deploy/cmms/src/ifactory_cmms_deploy/source.py \
  deploy/cmms/src/ifactory_cmms_deploy/records.py \
  tests/e2e/test_cmms_toolchains_and_source.py
git commit -m "feat: pin CMMS host toolchains and source baseline"
```

Expected: all tests pass without network, tool downloads or component build output.

### Task 4: Add the Isolated Compose Stack and Two-Stage Nginx Gateway

**Files:**

- Create: `deploy/compose/cmms-development.yml`
- Create: `deploy/gateway/cmms-development-nginx.conf.template`
- Create: `deploy/cmms/manifests/runtime-api-key-routes.json`
- Create: `deploy/cmms/src/ifactory_cmms_deploy/compose.py`
- Create: `deploy/cmms/src/ifactory_cmms_deploy/gateway.py`
- Create: `tests/e2e/test_cmms_deployment_contract.py`
- Create: `tests/e2e/test_cmms_nginx_contract.py`

**Interfaces:**

- Produces: `DockerCompose.config_quiet(snapshot) -> None`
- Produces: `DockerCompose.pull_exact_images(context: ClaimedApplyContext) -> ImageReceipt`
- Produces: `DockerCompose.up_state_and_gateway(context: ClaimedApplyContext) -> None`
- Produces: `DockerCompose.stop_preserving_volumes(context: ClaimedApplyContext) -> None`
- Produces: `resolve_default_bridge_gateway(runner) -> IPv4Address`
- Produces: `GatewayEvidencePurpose` with `PRECLAIM`, `CLAIMED_COMPENSATION`, `EMERGENCY`
- Produces: `Gateway.render(mode, gateway_ip, unit_generation) -> RenderedGateway`
- Produces: `Gateway.begin_fail_closed(plan: ConfirmedDeploymentPlan, reason) -> GatewayEvidence`
- Produces: `Gateway.require_external_listener_absent(evidence: GatewayEvidence) -> FailClosedEvidence`
- Produces: `Gateway.fail_closed_claimed(context: ClaimedApplyContext, reason) -> GatewayEvidence`
- Produces: `Gateway.emergency_fail_closed(reason) -> GatewayEvidence` as a monotonic safety-only path
- Produces: `Gateway.enable_dual(context: ClaimedApplyContext, expected) -> GatewayEvidence`
- Produces: `RuntimeApiKeyRoutes.load(path) -> RuntimeApiKeyRoutes`

- [ ] **Step 1: Write failing Compose and Nginx contract tests**

The Compose test must parse YAML and assert:

- top-level `name: ifactory-cmms-dev`;
- service set is exactly `postgres`, `minio`, `nginx`;
- no `container_name`, external volume, ThingsBoard network, CMMS API image or frontend image;
- every image is tag plus exact `linux/amd64` digest;
- image pull is an explicit plan action; service creation uses `--no-build --pull never`;
- PostgreSQL and MinIO publish only `127.0.0.1` ports and use `_FILE` secrets;
- volume targets are exactly `postgres_data:/var/lib/postgresql/data` and `minio_data:/data`;
- PostgreSQL receives only validated database/user/password-file settings; MinIO receives only its two credential-file settings, fixed region and exact shell-free server command;
- only Nginx has `network_mode: host`, and it has no `ports`;
- all three services have bounded health checks; Nginx health validates the exact rendered config;
- Nginx mounts a runtime config directory read-only, so atomic file replacement remains visible;
- the non-secret Nginx runtime directory/config modes are exactly `0755`/`0444`, allowing container UID `101` to read but never write them;
- named volumes are exactly `postgres_data` and `minio_data`;
- exact resource limits are present for CPU, memory and pids;
- stop code can issue `compose stop`, but contains no `down -v`, `volume rm` or wildcard cleanup.

The Nginx test must assert:

- loopback mode listens on `127.0.0.1:3000` and `[::1]:3000`;
- dual mode adds only the verified Docker bridge gateway IPv4 at `:3000`;
- neither mode contains `0.0.0.0`, `[::]`, LAN addresses or an operator-supplied listen directive;
- `/` proxies to `127.0.0.1:3001` with WebSocket upgrade;
- `/api/` proxies to `127.0.0.1:8082/` and strips `/api`;
- `/storage/` proxies to `127.0.0.1:9000/`, strips `/storage`, disables request buffering, sets `Host 127.0.0.1:9000`, and emits neither access nor error lines that could contain query arguments;
- requests carrying `x-api-key` reach CMMS only for the four exact current Phase 2 method/path shapes; every other path returns gateway `403` before proxying;
- API-Key `POST /api/assets` additionally requires an `Idempotency-Key` matching the current Phase 2 UUID pattern;
- every policy denial is exact JSON `{"success":false,"message":"API key route denied"}` with `Content-Type: application/json` and `X-iFactory-CMMS-Policy: api-key-route-denied`; default Nginx HTML is forbidden;
- the allowlist manifest binds platform-integration gitlink/client SHA-256 and CMMS OpenAPI contract SHA-256, so drift fails offline tests instead of widening access;
- response header `X-iFactory-CMMS-Gateway` equals the bound unit generation;
- a config validation/reload failure stops Nginx rather than retaining dual mode.
- `FailClosedEvidence` is produced only after the rendered loopback generation is loaded and the exact Docker-gateway listener is proven absent; wrong generation/address evidence and public construction are rejected;
- `claim_plan_application` accepts that evidence once for the same plan/gateway snapshot and rejects stale/replayed/cross-plan evidence.

Include:

```python
ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "deploy/compose/cmms-development.yml"
TEMPLATE = ROOT / "deploy/gateway/cmms-development-nginx.conf.template"


def test_compose_contains_only_state_and_gateway_services() -> None:
    document = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    assert document["name"] == "ifactory-cmms-dev"
    assert set(document["services"]) == {"postgres", "minio", "nginx"}
    assert all(
        "container_name" not in service
        for service in document["services"].values()
    )
    assert document["services"]["nginx"]["network_mode"] == "host"
    assert "ports" not in document["services"]["nginx"]


def test_dual_gateway_adds_only_verified_bridge_address() -> None:
    rendered = Gateway(template=TEMPLATE, runner=ScriptedRunner()).render(
        mode=GatewayMode.DUAL,
        gateway_ip=IPv4Address("172.17.0.1"),
        unit_generation="a" * 64,
    )
    assert "listen 127.0.0.1:3000;" in rendered.text
    assert "listen [::1]:3000;" in rendered.text
    assert "listen 172.17.0.1:3000;" in rendered.text
    assert "0.0.0.0" not in rendered.text
    assert "listen [::]:3000;" not in rendered.text
```

- [ ] **Step 2: Run the Compose/Nginx tests and verify the expected failure**

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  e2e/test_cmms_deployment_contract.py \
  e2e/test_cmms_nginx_contract.py -v
```

Expected: FAIL because the deployment assets and controllers do not exist.

- [ ] **Step 3: Add the exact image and infrastructure contract**

Use:

```text
postgres:16-alpine@sha256:7a396fd264a2067788b6551122b50f162bf6136312c7fc9d74381cb92c648382
minio/minio:RELEASE.2025-04-22T22-12-26Z@sha256:3f97c5651cb6662b880c787a232b6b34fec8d8922e08d6617b25d241a21164bb
nginx:1.27.0-alpine@sha256:a377278b7dde3a8012b25d141d025a88dbf9f5ed13c5cdf21ee241e7ec07ab57
platform: linux/amd64
```

PostgreSQL maps `127.0.0.1:5433:5432`, MinIO maps `127.0.0.1:9000:9000` and `127.0.0.1:9001:9001`. Configure health checks without embedding passwords. Use Compose secrets sourced from the already-validated absolute secret-file references:

```text
POSTGRES_PASSWORD_FILE=/run/secrets/postgres_password
MINIO_ROOT_USER_FILE=/run/secrets/minio_root_user
MINIO_ROOT_PASSWORD_FILE=/run/secrets/minio_root_password
```

Mount `postgres_data:/var/lib/postgresql/data` and `minio_data:/data`. PostgreSQL receives exactly validated `POSTGRES_USER`, `POSTGRES_DB` and its `_FILE` path. MinIO receives exactly the two `_FILE` paths plus `MINIO_REGION_NAME=us-east-1`, and its command is the exec-form array `["server", "/data", "--console-address", ":9001"]`; reject a shell token, extra endpoint or alternate data path.

Use `pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"` for PostgreSQL, `curl -f http://127.0.0.1:9000/minio/health/live` for MinIO, and `/usr/sbin/nginx -t -q -c /etc/ifactory-cmms/nginx.conf` for Nginx. The readiness signer uses the same MinIO region and service `s3`. Give every health check bounded interval, timeout, start period and retry values. Set:

```text
postgres  cpus=1.0  mem_limit=1g    pids_limit=256
minio     cpus=1.0  mem_limit=1g    pids_limit=256
nginx     cpus=0.5  mem_limit=256m  pids_limit=128
```

Nginx runs as numeric UID/GID `101:101`, read-only with all capabilities dropped, `no-new-privileges`, one `/tmp:uid=101,gid=101,mode=0700` tmpfs, and the dedicated `.runtime/cmms-nginx/` directory mounted read-only. This non-secret directory is current-UID-owned mode `0755`; the complete generated config is mode `0444`, contains no env/credential value, and is atomically replaced only by the controller. Private runtime directories and secret/env/record files retain `0700`/`0600`. Define a bounded access-log format from `$request_method $uri $status` only—never `$request`, `$request_uri`, `$args` or headers—and set both `access_log off` and location-scoped `error_log /dev/null crit` inside `/storage/`; upstream timeout/error diagnostics for this one secret-bearing route are intentionally reduced to controller-safe status codes. The remaining locations log to stderr/stdout and pid/client/proxy temp files remain below `/tmp`. Contract tests inject a signed-query sentinel into success, upstream refusal and timeout paths and require it absent from Nginx/Docker logs. Bypass the image mutation entrypoint with `entrypoint: ["/usr/sbin/nginx"]` and use `command: ["-g","daemon off;","-c","/etc/ifactory-cmms/nginx.conf"]`.

- [ ] **Step 4: Implement fixed Docker invocation and gateway rendering**

Every Docker command must begin:

```text
/usr/bin/docker --host unix:///var/run/docker.sock compose
--project-name ifactory-cmms-dev
--env-file "/proc/${controller_pid}/fd/${sealed_env_fd}"
-f "${root}/deploy/compose/cmms-development.yml"
```

The environment snapshot is copied to a sealed memfd after descriptor-bound validation. The child gets a fresh `0700` empty `DOCKER_CONFIG` and fixed PATH only. Real config checks use `config -q`, never rendered config stdout.

Render the API-Key gateway policy from this exact immutable allowlist:

```text
GET   /api/auth/me
POST  /api/work-orders/search
GET   ^/api/assets/by-equipment-id/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$
POST  /api/assets
```

The asset POST additionally requires `Idempotency-Key` matching `^pilot-asset:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`. The manifest binds platform-integration gitlink `b7ca04bf56e5e98493128f59619832c817baac1e`, client digest `ed2306132a6538e07810e966872e15d1f3cb00dbf093f479e3b7d6a6e3edefa5`, and contract digest `ff3166e9d217d3ae072d59b5b167500a9b8e3285d5db8fac94b0551b8c56e33b`.

Use Nginx `map` over the exact request method and normalized `$uri`; when `x-api-key` is nonempty and no row matches, set `default_type application/json`, add `X-iFactory-CMMS-Policy: api-key-route-denied always`, and return exact bounded body `{"success":false,"message":"API key route denied"}` with `403` before `proxy_pass`. Reject API-Key asset POST with missing/noncanonical idempotency header the same way. Contract tests require that default Nginx HTML, redirects and an upstream call are impossible on this branch.

Do not inspect JSON bodies or treat this gateway rule as authorization for the later asset write. In particular, possession of the API Key plus a syntactically valid idempotency header does not prove that the body contains `equipment_id`; current CMMS intentionally permits the legacy create shape without it. The bound Phase 2 client/contract and a separately confirmed Phase 2 provisioning plan—not Nginx alone—must supply and verify that body. Record this as an explicit local trusted-client residual; do not claim the header allowlist makes arbitrary key possession fully idempotent.

If a pinned image is absent, the generated plan must include one visible `images.pull-exact` action. Apply runs Compose `pull`, inspects the resulting `linux/amd64` repo digest, and only then uses `up --detach --no-build --pull never`. A start plan for already-created services uses `start`; it does not silently pull or recreate.

Resolve the default bridge gateway with fixed Docker inspection, parse one IPv4, prove the address is configured on the local Docker bridge, and reject wildcard, loopback, multicast, link-local and nonlocal addresses. A container smoke later must prove `host-gateway` resolves to that same IPv4.

Render a complete Nginx candidate to a private temporary file, validate it against the pinned Nginx image, atomically publish it, reload the existing container, then inspect exact listeners. Pre-claim `Gateway.begin_fail_closed()` first requires the confirmed plan's first row to be the untargeted `gateway.fail-closed` action and binds that plan hash/gateway generation into its evidence. It renders loopback-only first; if render, validation, reload or proof is uncertain, it stops the Nginx service and proves the gateway IPv4 listener is gone. Only the subsequent exact absence proof may mint the opaque `FailClosedEvidence` consumed by `claim_plan_application`.

Post-claim compensation calls `fail_closed_claimed(context, reason)`, which validates the same plan/action but returns purpose `CLAIMED_COMPENSATION`, never claim-capable evidence. systemd `ExecStopPost`, guard failures and crashes without a recoverable context call `emergency_fail_closed(reason)`, whose evidence purpose is `EMERGENCY`. That emergency entrypoint has a deliberately tiny monotonic surface: validate/render loopback-only or stop the exact Nginx service, prove the external listener absent, emit one safe code, and never start/reload any other service, enable dual mode, claim a plan or change receipt/identity/domain state. `require_external_listener_absent` accepts only a fresh `PRECLAIM` result bound to the same plan hash/generation. Tests prove neither post-claim path can feed `claim_plan_application`.

Generate listener lines only from the enum and parsed address:

```python
def gateway_listen_lines(
    mode: GatewayMode, gateway_ip: IPv4Address | None
) -> Sequence[str]:
    lines = ("listen 127.0.0.1:3000;", "listen [::1]:3000;")
    if mode is GatewayMode.LOOPBACK:
        if gateway_ip is not None:
            raise DeploymentError("CMMS-E042", "loopback gateway input is invalid", 42)
        return lines
    if gateway_ip is None or not verified_docker_bridge_address(gateway_ip):
        raise DeploymentError("CMMS-E042", "Docker gateway is not verified", 42)
    return lines + (f"listen {gateway_ip}:3000;",)
```

- [ ] **Step 5: Run the offline contract tests**

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  e2e/test_cmms_deployment_contract.py \
  e2e/test_cmms_nginx_contract.py \
  e2e/test_cmms_secure_artifacts.py -v
```

Expected: PASS with fake runners only. Confirm no Docker container or volume was created.

- [ ] **Step 6: Run Compose/gateway slice verification**

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  e2e/test_cmms_deployment_contract.py \
  e2e/test_cmms_nginx_contract.py \
  e2e/test_cmms_secure_artifacts.py -v
git diff --check
./scripts/doctor.sh
```

Expected: tests pass and doctor has zero failures.

- [ ] **Step 7: Commit the Compose/gateway slice**

Run:

```bash
set -Eeuo pipefail
git add \
  deploy/compose/cmms-development.yml \
  deploy/gateway/cmms-development-nginx.conf.template \
  deploy/cmms/manifests/runtime-api-key-routes.json \
  deploy/cmms/src/ifactory_cmms_deploy/compose.py \
  deploy/cmms/src/ifactory_cmms_deploy/gateway.py \
  tests/e2e/test_cmms_deployment_contract.py \
  tests/e2e/test_cmms_nginx_contract.py
git commit -m "feat: add isolated CMMS state stack and gateway"
```

Expected: the Compose file is static and secret-free; no live Compose command has run.

### Task 5: Add user-systemd Units, API Start Permits, and Host Launchers

**Files:**

- Create: `deploy/systemd/ifactory-cmms-api.service.in`
- Create: `deploy/systemd/ifactory-cmms-frontend.service.in`
- Create: `deploy/systemd/ifactory-cmms-fail-closed.service.in`
- Create: `deploy/systemd/ifactory-cmms-license-guard.service.in`
- Create: `deploy/systemd/ifactory-cmms-license-guard.timer.in`
- Create: `deploy/cmms/src/ifactory_cmms_deploy/systemd.py`
- Create: `deploy/cmms/src/ifactory_cmms_deploy/start_gate.py`
- Create: `deploy/cmms/src/ifactory_cmms_deploy/fail_closed.py`
- Create: `deploy/cmms/src/ifactory_cmms_deploy/api_launcher.py`
- Create: `tests/e2e/test_cmms_systemd_and_start_gate.py`
- Modify: `deploy/cmms/src/ifactory_cmms_deploy/records.py`

**Interfaces:**

- Produces: `SystemdRenderer.render(snapshot, mode, controller_entrypoint) -> UnitGeneration`
- Produces: `UserSystemdEnvironment.resolve(uid: int) -> Mapping[str, str]`
- Produces: `SystemdUser.install_and_reload(context: ClaimedApplyContext, generation) -> UnitEvidence`
- Produces: `StartEvidence` bound to source, artifact, controller, unit, gateway, mode and budget
- Produces: `create_start_permit(context: ClaimedApplyContext, evidence, budget_debit, now) -> StartPermit`
- Produces: `StartGate.consume(now: datetime) -> StartPermit`
- Produces: `launch_api(config, artifact, toolchains) -> NoReturn`
- Produces: `launch_frontend(config, toolchains) -> NoReturn`
- Produces: `fail_closed(reason, gateway) -> None`, delegating only to `Gateway.emergency_fail_closed`

- [ ] **Step 1: Write failing unit, permit and launcher tests**

Tests must prove:

- API template contains `Restart=no`, `ExecStartPre`, `ExecStart`, `ExecStopPost`, `OnFailure`, and no automatic restart directive;
- frontend alone uses `Restart=on-failure`, `RestartSec=2s` and start-rate limits;
- unit templates contain no secret values and the bootstrap env is never an `EnvironmentFile`;
- rendered units call an absolute prepared `.runtime` venv entrypoint, never `uv`, `python` from PATH or a source-tree module;
- unit generation binds the interpreter, entrypoint, installed package tree, project source and lockfile digests;
- every `systemctl --user`/`systemd-run --user` command rejects inherited bus variables and reconstructs a verified current-UID user-bus environment;
- direct API `start`/`restart` with no valid permit cannot reach the launcher and invokes fail-closed;
- permits reject expiry, reuse, wrong UID, wrong plan/root/source/artifact/controller entrypoint/controller package/unit/gateway digest/gateway IPv4/mode/budget and dual gateway state;
- a parameterized test mutates each `StartPermit` security-binding field one at a time and requires rejection before Java;
- an offline permit is rejected unless the loaded API generation binds the expected guard timer and the same systemd start transaction makes that timer active before `ExecStartPre`;
- permit consumption uses an exclusive lock and atomic same-directory rename from `active.json` to `consuming.json`, then to a nonce-named consumed record or content-hash-named rejected record;
- `ExecStopPost` is represented for clean exit, failed `ExecStartPre`, failed `ExecStart`, explicit stop/restart and signal exit;
- API launcher exports only CMMS API runtime variables and four secret classes: DB password, MinIO access/secret, JWT and license key; it never reads identity passwords;
- API launcher sets `INTERCOM_TOKEN` to the exact empty value and cannot inherit a user-manager token;
- API launcher exports exactly one plan-bound canonical `ALLOWED_ORGANIZATION_ADMINS` value; missing, multi-value or config-hash drift fails before Java;
- API argv is exact Java `--add-opens` plus the fingerprinted jar;
- frontend argv uses the pinned npm, `HOST=127.0.0.1`, `PORT=3001`, fixed frontend env and no API/database/license secret;
- logs and fake journal never contain sentinel secrets.

Include:

```python
ROOT = Path(__file__).resolve().parents[2]
API_UNIT_TEMPLATE = ROOT / "deploy/systemd/ifactory-cmms-api.service.in"


def test_api_unit_cannot_start_without_permit(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    gate = StartGate(
        runtime=safe_runtime.paths,
        evidence=safe_runtime.actual_start_evidence(),
        fail_closed=safe_runtime.fail_closed,
    )

    with pytest.raises(DeploymentError) as caught:
        gate.consume(now=safe_runtime.now)

    assert caught.value.code == "CMMS-E061"
    assert safe_runtime.events == ["gateway.fail_closed:permit-invalid"]
    assert safe_runtime.launcher_calls == []


def test_api_template_forbids_restart() -> None:
    template = API_UNIT_TEMPLATE.read_text(encoding="utf-8")
    assert "Restart=no" in template
    assert "ExecStartPre=@CONTROL_ENTRYPOINT@ internal start-gate" in template
    assert "ExecStopPost=@CONTROL_ENTRYPOINT@ internal fail-closed" in template
    assert "Restart=always" not in template
    assert "Restart=on-failure" not in template
```

- [ ] **Step 2: Run the unit/start-gate tests and verify the expected failure**

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  e2e/test_cmms_systemd_and_start_gate.py -v
```

Expected: FAIL because units and systemd helpers are absent.

- [ ] **Step 3: Implement the API and frontend unit contracts**

The rendered API service must include:

```ini
[Unit]
OnFailure=ifactory-cmms-fail-closed.service

[Service]
Type=simple
Restart=no
EnvironmentFile=@RUNTIME_ENV@
ExecStartPre=@CONTROL_ENTRYPOINT@ internal start-gate
ExecStart=@CONTROL_ENTRYPOINT@ internal api-launch
ExecStopPost=@CONTROL_ENTRYPOINT@ internal fail-closed --reason api-ended
```

The rendered frontend unit has its own non-secret env file and launcher. Both units use absolute working directories and executable paths, `NoNewPrivileges=yes`, `PrivateTmp=yes`, `UMask=0077`, `KillMode=control-group`, `LimitNOFILE=65536` and `TimeoutStopSec=30s`. Use `TimeoutStartSec=180s` for API and `120s` for frontend. Do not enable either unit at boot.

Prepare the unit entrypoint during a later confirmed apply with:

```bash
UV_PROJECT_ENVIRONMENT="${runtime_control_venv}" \
UV_PYTHON_DOWNLOADS=never \
uv sync --project deploy/cmms --frozen --no-dev --no-editable --python 3.12.13
```

Verify the installed package resolves inside `.runtime/cmms-control/.venv`, never from the checkout. Hash the venv interpreter, `cmms-development` entrypoint, installed `ifactory_cmms_deploy` package tree, project source tree and `uv.lock` into `UnitGeneration`. Render into `.runtime/systemd/`, run `systemctl --user link` on exact files, and `daemon-reload`. A globally degraded user manager is reported but does not trigger changes to unrelated ThingsBoard units; every CMMS unit is checked independently.

Do not inherit `XDG_RUNTIME_DIR` or `DBUS_SESSION_BUS_ADDRESS`. `UserSystemdEnvironment.resolve(os.getuid())` constructs `/run/user/${uid}` and `/run/user/${uid}/bus`, walks both without following symlinks, requires the runtime directory to be a current-UID-owned directory with no group/world write and the bus to be a current-UID-owned Unix socket, and returns only:

```text
PATH=/usr/bin:/bin
XDG_RUNTIME_DIR=/run/user/${uid}
DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus
```

Every user-systemd command, including the offline `systemd-run --user` capability probe, uses that explicit mapping. Missing or mismatched metadata is a fatal preflight result; commands are never retried with inherited session variables.

- [ ] **Step 4: Implement single-use permit validation and fail-closed hooks**

`StartPermit` uses the exact Task 2 top-level schema. This task adds its bounded
field validators and comparison semantics; it does not extend the wire record.

`start-gate` exclusively creates `permit.lock`, rejects any stale `consuming.json`, atomically renames `active.json` to `consuming.json`, and reads/revalidates that stable file. A parsed permit moves to `consumed/{nonce}.json`; malformed bytes move to `rejected/{content_sha256}.json`. Missing, invalid, expired or already consumed permits fail and call fail-closed. A direct `systemctl --user start/restart` therefore succeeds only during the short window of an already confirmed, matching plan and consumes that authorization once.

Use one field comparison table:

```python
def permit_mismatches(
    permit: StartPermit, actual: StartEvidence, now: datetime
) -> Sequence[str]:
    checks = {
        "expired": not (permit.created_at <= now <= permit.expires_at),
        "uid": permit.uid != os.getuid(),
        "plan": permit.plan_sha256 != actual.plan_sha256,
        "root": permit.root_sha != actual.root_sha,
        "source": permit.cmms_source_fingerprint != actual.cmms_source_fingerprint,
        "artifact": permit.api_artifact_sha256 != actual.api_artifact_sha256,
        "controller_entrypoint": (
            permit.controller_entrypoint_sha256
            != actual.controller_entrypoint_sha256
        ),
        "controller_package": (
            permit.controller_package_sha256
            != actual.controller_package_sha256
        ),
        "unit": permit.unit_generation != actual.unit_generation,
        "gateway_digest": (
            permit.loopback_gateway_sha256
            != actual.loopback_gateway_sha256
        ),
        "gateway_ipv4": permit.docker_gateway_ipv4 != actual.docker_gateway_ipv4,
        "mode": permit.license_mode != actual.license_mode,
        "budget": permit.budget_debit_id != actual.budget_debit_id,
    }
    return tuple(name for name, failed in checks.items() if failed)
```

`fail_closed` never starts an API or rewrites identity state. It only tries a validated loopback-only reload, otherwise stops Nginx, then records a secret-free reason code.

Strict record parsing separately verifies `schema_version`, nonce syntax/uniqueness, timestamp order and the 60-second maximum lifetime. The parameterized mutation test covers every field in the comparison table, including `plan_sha256`, `root_sha`, `controller_entrypoint_sha256` and `docker_gateway_ipv4`; adding a future permit field without a corresponding actual-evidence comparison must fail a completeness test.

- [ ] **Step 5: Implement exact host process environments**

The API launcher reads validated secret files into process memory, then replaces itself with:

```text
"${temurin_home}/bin/java"
--add-opens=java.base/java.lang=ALL-UNNAMED
-jar
"${fingerprinted_runtime_api_jar}"
```

It sets exact Spring/CMMS values including `SERVER_ADDRESS=127.0.0.1`, `SERVER_PORT=8082`, `DB_URL=127.0.0.1:5433/atlas`, `MINIO_ENDPOINT=http://127.0.0.1:9000`, `MINIO_BUCKET=atlas-bucket`, `MAIL_RECIPIENTS=`, `INTERCOM_TOKEN=`, the one plan-bound `ALLOWED_ORGANIZATION_ADMINS` email and all fixed public/MinIO/invitation/license settings from Task 2. It unwraps the validated MinIO username/password files only in process memory and exports them to Java as `MINIO_ACCESS_KEY` and `MINIO_SECRET_KEY`; neither may be omitted or inherited. It removes inherited Intercom/admin-list/MinIO values rather than accepting user-manager state. Offline alone sets `LICENSE_FILE_PATH`; online omits it. It sets `TZ=Asia/Shanghai`.

The frontend launcher replaces itself with pinned npm `start` from `components/cmms/frontend`. It uses only:

```text
HOST=127.0.0.1
PORT=3001
API_URL=/api
INVITATION_VIA_EMAIL=true
CLOUD_VERSION=false
ENABLE_SSO=false
LDAP_ENABLED=false
NODE_ENV=development
```

- [ ] **Step 6: Run the process-control tests**

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  e2e/test_cmms_systemd_and_start_gate.py \
  e2e/test_cmms_secure_artifacts.py \
  e2e/test_cmms_deployment_records.py -v
```

Expected: unit, permit, launcher and secret-redaction tests pass.

- [ ] **Step 7: Run process-control slice verification**

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  e2e/test_cmms_systemd_and_start_gate.py \
  e2e/test_cmms_secure_artifacts.py \
  e2e/test_cmms_deployment_records.py -v
git diff --check
./scripts/doctor.sh
```

Expected: tests pass and doctor has zero failures.

- [ ] **Step 8: Commit the process-control slice**

Run:

```bash
set -Eeuo pipefail
git add \
  deploy/systemd \
  deploy/cmms/src/ifactory_cmms_deploy/systemd.py \
  deploy/cmms/src/ifactory_cmms_deploy/start_gate.py \
  deploy/cmms/src/ifactory_cmms_deploy/fail_closed.py \
  deploy/cmms/src/ifactory_cmms_deploy/api_launcher.py \
  deploy/cmms/src/ifactory_cmms_deploy/records.py \
  tests/e2e/test_cmms_systemd_and_start_gate.py
git commit -m "feat: enforce fail-closed CMMS host processes"
```

Expected: offline tests pass and no user unit is linked or started.

### Task 6: Enforce Offline License Isolation and the Conservative Online Budget

**Files:**

- Create: `deploy/cmms/src/ifactory_cmms_deploy/license.py`
- Create: `deploy/cmms/src/ifactory_cmms_deploy/license_guard.py`
- Create: `tests/e2e/test_cmms_license_modes.py`
- Modify: `deploy/systemd/ifactory-cmms-api.service.in`
- Modify: `deploy/systemd/ifactory-cmms-license-guard.service.in`
- Modify: `deploy/systemd/ifactory-cmms-license-guard.timer.in`
- Modify: `deploy/cmms/src/ifactory_cmms_deploy/systemd.py`
- Modify: `deploy/cmms/src/ifactory_cmms_deploy/records.py`

**Interfaces:**

- Produces: `OfflineLicenseEvidence.verify(context: ClaimedApplyContext, config, runner, transport) -> OfflineLicenseEvidence`
- Produces: immutable typed `BudgetLedger`, `BudgetDebit`, `BudgetRecoveryReceipt`, `BudgetStatus` and `GuardResult`, all constrained by Task 2's fixed structural schemas
- Produces: `OnlineBudget.debit(context: ClaimedApplyContext, now, fresh_volume_evidence) -> BudgetDebit`
- Produces: `OnlineBudget.recover_unknown(context: ClaimedApplyContext, now, existing_volume_evidence) -> BudgetDebit`
- Produces: `OnlineBudget.status(now) -> BudgetStatus`
- Produces: `LicenseGuard.check(expected, actual_unit_state) -> GuardResult`
- Produces: `verify_mode_transition(source_mode, target_mode, evidence) -> None`

- [ ] **Step 1: Write failing offline/online license tests**

Tests must cover:

- offline unit profile has `IPAddressDeny=any`, `IPAddressAllow=127.0.0.0/8` and `IPAddressAllow=::1/128`;
- API unit has `BindsTo=` and `After=` the guard timer only in offline mode;
- timer has `OnUnitActiveSec=30s`, `AccuracySec=1s`, `PartOf=ifactory-cmms-api.service` and `StopWhenUnneeded=yes`;
- guard service has the same loopback-only IP policy, `OnFailure=ifactory-cmms-fail-closed.service`, and never restarts API;
- offline preflight proves a transient user unit can reach a temporary loopback listener and receives policy denial for a non-loopback socket;
- license file metadata/content hash change, missing file, invalid `/api/license/state`, inactive timer, unexpected egress or guard exception invokes fail-closed;
- an API still activating may defer the timer check only while gateway is loopback-only; a manual valid guard check is mandatory before dual mode;
- online unit contains no offline IP directives or guard dependency, and online start refuses any active/residual guard;
- budget debit is durable before permit creation, counts failed starts, never refunds, allows at most 10 attempts per API `LocalDate` in `Asia/Shanghai`, and leaves 10 of the source limit 20 unallocated;
- missing/corrupt/rolled-back ledger on an existing volume is `UNKNOWN`; a demonstrably never-created Compose volume may initialize at zero;
- replacing the current ledger with an older but internally canonical copy is detected by the independent StateRecord anchor;
- existing-volume missing/corrupt/rolled-back ledger can recover only through exact action `license.recover-unknown-budget`; ordinary start cannot synthesize it;
- recovery durably consumes all 10 controlled slots before authorizing one and only one new MainPID, never refunds on failure, and a second start that day returns `CMMS-E073`;
- date change, DST-independent timezone use, concurrent debit, process crash and exhausted budget are deterministic;
- a 12-hour license-cache revalidation is documented as unobservable internal consumption and cannot be counted as a successful controlled start;
- offline-to-online and online-to-offline transitions require gateway closed, API stopped, guard stopped/inactive, regenerated units and a new mode-bound permit.

Include:

```python
def test_online_budget_refuses_eleventh_controlled_start(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    safe_runtime.write_empty_online_budget_ledger()
    budget = OnlineBudget(
        path=safe_runtime.paths.online_budget,
        policy=safe_runtime.path_policy,
    )
    for index in range(10):
        debit = budget.debit(
            context=safe_runtime.claimed_context(
                safe_runtime.confirm_plan(
                    safe_runtime.make_online_restart_plan(
                        plan_nonce=f"restart-{index}",
                    )
                ),
                required_action=ActionCode.LICENSE_DEBIT_ONLINE_START,
            ),
            now=safe_runtime.now + timedelta(minutes=index),
            fresh_volume_evidence=safe_runtime.existing_volume,
        )
        assert debit.sequence == index + 1

    with pytest.raises(DeploymentError) as caught:
        budget.debit(
            context=safe_runtime.claimed_context(
                safe_runtime.confirm_plan(
                    safe_runtime.make_online_restart_plan(
                        plan_nonce="restart-10",
                    )
                ),
                required_action=ActionCode.LICENSE_DEBIT_ONLINE_START,
            ),
            now=safe_runtime.now + timedelta(minutes=10),
            fresh_volume_evidence=safe_runtime.existing_volume,
        )

    assert caught.value.code == "CMMS-E073"


def test_online_generation_has_no_offline_network_policy(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    rendered = SystemdRenderer(safe_runtime.paths).render(
        safe_runtime.snapshot,
        LicenseMode.ONLINE,
        safe_runtime.controller_entrypoint,
    )
    api = rendered.files["ifactory-cmms-api.service"]
    assert "IPAddressDeny=" not in api
    assert "ifactory-cmms-license-guard.timer" not in api


def test_online_budget_detects_canonical_ledger_rollback(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    older = safe_runtime.write_anchored_online_budget(sequence=1)
    safe_runtime.write_anchored_online_budget(sequence=2)
    safe_runtime.replace_budget_bytes(older)

    status = OnlineBudget(
        path=safe_runtime.paths.online_budget,
        policy=safe_runtime.path_policy,
    ).status(safe_runtime.now)

    assert status.continuity_state == "UNKNOWN"
    assert status.code == "ONLINE_BUDGET_ROLLBACK"
```

- [ ] **Step 2: Run the license-mode tests and verify the expected failure**

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  e2e/test_cmms_license_modes.py -v
```

Expected: FAIL because license policy code is absent.

- [ ] **Step 3: Render exact offline and online unit generations**

Offline API generation adds:

```ini
[Unit]
BindsTo=ifactory-cmms-license-guard.timer
After=ifactory-cmms-license-guard.timer

[Service]
IPAddressDeny=any
IPAddressAllow=127.0.0.0/8
IPAddressAllow=::1/128
```

The timer unit adds:

```ini
[Unit]
PartOf=ifactory-cmms-api.service
StopWhenUnneeded=yes

[Timer]
OnUnitActiveSec=30s
AccuracySec=1s
Unit=ifactory-cmms-license-guard.service
```

Online generation omits all five offline relationships/directives. Mode switch stops API and guard/timer, proves all inactive, renders the target generation, runs `daemon-reload`, verifies the loaded unit properties, and only then permits a new start.

- [ ] **Step 4: Implement the offline capability probe and guard**

Within a confirmed apply and after gateway fail-close, create two current-process-owned temporary listeners on random ports: one on `127.0.0.1` and one on the already verified local Docker bridge IPv4. Before invoking systemd, the unrestricted controller must successfully connect to both exact listeners, proving the bridge target is routed and listening. Then use `systemd-run --user --wait --collect` with the exact IP policy to run `internal network-probe`: require loopback success and immediate `EPERM`/`EACCES` for the same bridge listener. The probe never contacts DNS, LAN or public Internet. In `finally`, close both exact listener FDs and prove their ports absent. Target-not-listening, routing failure, timeout or a different errno does not prove enforcement; tests cover all of those false-positive paths and listener cleanup.

The guard reopens the offline license file through secure I/O, compares owner/type/link/mode/size/SHA-256, proves the API unit MainPID owns `127.0.0.1:8082`, and calls fixed `http://127.0.0.1:3000/api/license/state` with no proxy or redirect. Require `hasLicense=true`, `valid=true`, and entitlements containing `API_ACCESS` and `CUSTOM_ROLES`.

If API is `activating`, the periodic guard may exit successfully only after proving gateway mode is loopback-only. Before dual gateway, lifecycle must execute one full guard check while API is active.

- [ ] **Step 5: Implement the online budget ledger**

Use canonical JSON with a compare-and-replace lock discipline and the exact
Task 2 `BudgetLedger` top-level schema. This task adds the bounded
`BudgetDebit` row type inside `attempts`; it does not extend the ledger record.

The only accepted continuity values are `CONTINUOUS` and
`RECOVERED_UNKNOWN`. A demonstrably fresh volume creates exactly:

```text
schema_version = 1
zone = Asia/Shanghai
local_date = controlled_local_date(now) encoded YYYY-MM-DD
controlled_limit = 10
source_limit = 20
attempts = []
previous_ledger_sha256 = null
continuity_state = CONTINUOUS
```

A normal local-date rollover sets `previous_ledger_sha256` to the immediately
prior State-anchored ledger hash and starts an empty `CONTINUOUS` day. A
recovery ledger instead uses `RECOVERED_UNKNOWN`, retains the quarantined
original hash only in its recovery receipt and starts with the one
10-slot recovery debit; no accepted ledger persists an `UNKNOWN` continuity
value.

Each `BudgetDebit` row has this exact nested schema:

```text
debit_id
sequence
plan_sha256
attempted_at
local_date
source_fingerprint
action_code
consumed_slots
result_category
```

`debit_id` is an injectable non-secret 128-bit lowercase hexadecimal value;
`attempted_at` is the same fixed-six-digit UTC timestamp used by Task 2;
`local_date` is the controlled `Asia/Shanghai` date; `sequence` starts at 1
per date. `result_category` is exactly `CONTROLLED_START` for action
`license.debit-online-start` or `UNKNOWN_BUDGET_RECOVERY` for action
`license.recover-unknown-budget`. Each attempt stores no key, response or
license payload. `StateRecord` independently stores
`online_budget_ledger_sha256`, `latest_budget_debit_id`,
`latest_budget_sequence` and `latest_budget_local_date`. On every read, require
the ledger bytes and latest row to match all four anchor fields; an older
self-consistent ledger therefore cannot be replayed.

Each ordinary attempt has `consumed_slots=1`. The only other accepted value is `consumed_slots=10` on a recovery debit tied to exact action `license.recover-unknown-budget`; budget checks sum slots, not row count.

Write and fsync a new debit first, then atomically update/reopen the StateRecord anchor, and only then create a permit. A crash between the two files is conservatively `UNKNOWN` and requires an explicit repair plan; it never refunds the debit. A failed/unknown API start remains consumed. Missing ledger initializes only when Docker inspection proves the project PostgreSQL volume did not exist before this confirmed bootstrap; otherwise status is `UNKNOWN` and apply stops.

Use one local-date calculation and reject before creating a debit when the limit is reached:

```python
def controlled_local_date(now: datetime) -> date:
    return now.astimezone(ZoneInfo("Asia/Shanghai")).date()


def append_budget_attempt(
    ledger: BudgetLedger, *, context: ClaimedApplyContext, now: datetime
) -> tuple[BudgetLedger, BudgetDebit]:
    context.require_action(ActionCode.LICENSE_DEBIT_ONLINE_START)
    today = controlled_local_date(now)
    todays = tuple(item for item in ledger.attempts if item.local_date == today)
    consumed = sum(item.consumed_slots for item in todays)
    if consumed >= 10:
        raise DeploymentError("CMMS-E073", "online start budget is exhausted", 73)
    debit = BudgetDebit.create(
        sequence=len(todays) + 1,
        consumed_slots=1,
        plan=context.plan,
        now=now,
    )
    return replace(ledger, attempts=ledger.attempts + (debit,)), debit
```

The caller holds the exclusive ledger lock, atomically persists and reopens the returned ledger, and only then returns the debit for permit creation.

For an existing-volume `UNKNOWN` ledger, no normal operation may call `debit()`. A confirmed `repair` plan containing only the exact recovery/start prerequisites plus `license.recover-unknown-budget` may proceed while API/gateway are stopped:

The recovery receipt uses the exact Task 2 top-level schema. This task fixes
`status` to `PENDING | SUCCEEDED | FAILED`, defines each nullable transition
and does not extend the wire record.

Nullable fields are explicit and become non-null only after their corresponding durable effect; terminal records are immutable.

1. Immediately after the application claim and before touching the ledger, exclusively create the plan-derived `BudgetRecoveryReceipt` as `PENDING` with plan hash, local date and safe unknown reason.
2. Securely snapshot the unknown ledger bytes/metadata, move the original to the exact private quarantine `.runtime/cmms-staging/budget-recovery/${plan_sha256}/original.bin`, then atomically enrich/reopen the pending receipt with only its SHA-256/reason (or explicit no-bytes flag).
3. Under the same lock, create a new canonical ledger for today's `Asia/Shanghai` date with one recovery debit `consumed_slots=10`, update/reopen the independent StateRecord anchor, and add the debit ID to the still-pending receipt. This happens before permit creation.
4. Bind that debit into one permit and attempt exactly one new API MainPID. Request-not-sent, failed start, timeout or crash still leaves all 10 slots consumed and requires offline mode or the next local date; never retry.
5. Only after the new PID and operational loopback readiness pass may the receipt become `SUCCEEDED`. Its remaining fields are PID start identity and safe result code—never quarantined bytes. A caught failure transitions it to `FAILED`; a crash leaves `PENDING` for reconciliation.

Missing ledger on a demonstrably fresh never-created volume still initializes normally at zero and cannot use recovery. A corrupt/rollback finding with no intact bytes records only the stable reason and an explicit “no original bytes” flag. Recovery is unavailable when the API is already active, in offline mode, without exact existing-volume ownership, or when the action list contains bootstrap identity writes. Tests inject crashes before/after pending-receipt creation, quarantine, ledger write, StateRecord anchor, permit and PID proof; every material effect observes an existing `PENDING` receipt, and every outcome is either pre-effect or exhausted/reconcilable, never a reusable partial authorization.

- [ ] **Step 6: Run the license slice tests**

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  e2e/test_cmms_license_modes.py \
  e2e/test_cmms_systemd_and_start_gate.py \
  e2e/test_cmms_deployment_records.py -v
```

Expected: all offline/online tests pass with fake systemd and HTTP.

- [ ] **Step 7: Run license slice verification**

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  e2e/test_cmms_license_modes.py \
  e2e/test_cmms_systemd_and_start_gate.py \
  e2e/test_cmms_deployment_records.py -v
git diff --check
./scripts/doctor.sh
```

Expected: tests pass and doctor has zero failures.

- [ ] **Step 8: Commit the license slice**

Run:

```bash
set -Eeuo pipefail
git add \
  deploy/cmms/src/ifactory_cmms_deploy/license.py \
  deploy/cmms/src/ifactory_cmms_deploy/license_guard.py \
  deploy/cmms/src/ifactory_cmms_deploy/systemd.py \
  deploy/cmms/src/ifactory_cmms_deploy/records.py \
  deploy/systemd/ifactory-cmms-api.service.in \
  deploy/systemd/ifactory-cmms-license-guard.service.in \
  deploy/systemd/ifactory-cmms-license-guard.timer.in \
  tests/e2e/test_cmms_license_modes.py
git commit -m "feat: enforce CMMS license runtime modes"
```

Expected: policy and budget tests pass with fake systemd/HTTP; no license service is contacted.

### Task 7: Add the Fixed-Origin CMMS API Client and Credential-Slot Protocol

**Files:**

- Create: `deploy/cmms/src/ifactory_cmms_deploy/cmms_api.py`
- Create: `deploy/cmms/src/ifactory_cmms_deploy/credentials.py`
- Create: `tests/e2e/test_cmms_api_and_credentials.py`
- Modify: `deploy/cmms/src/ifactory_cmms_deploy/cli.py`
- Modify: `deploy/cmms/src/ifactory_cmms_deploy/config.py`

**Interfaces:**

- Produces: `CmmsApi(transport, base_url="http://127.0.0.1:3000/api")`
- Produces: secret wrappers `SecretText`, `BearerToken`, `RawApiKey` with redacted `repr`/`str`
- Produces: enums `Identity`, `LoginType`, `AuthenticationResult`, `CredentialDecision`
- Produces: `CredentialProbe(candidate: AuthenticationResult, current: AuthenticationResult)`
- Produces: `CredentialSlots` for the three validated current/candidate identity pairs
- Produces: `CredentialSlotTransition(plan_sha256, action, identity, before_current, before_candidate, after_current, candidate_absent, result_code)`
- Produces: `CredentialLineage` that resolves the current expected file binding after each planned transition
- Produces: one-use `InvitationProbeSlot(slot_id, canonical_email, password_file_binding)` outside the three durable identity pairs
- Produces: `CredentialSlots.to_plan_bindings() -> Sequence[CredentialPlanBinding]` as an immutable tuple
- Produces: `InvitationProbeSlot.to_plan_binding() -> InvitationProbePlanBinding`
- Produces: `ApiCredential.bearer(token: BearerToken)` and `ApiCredential.api_key(key: RawApiKey)`
- Produces: strict methods `sign_in`, `me`, `update_password`, `signup`, `search_users_exact_email`, `list_users_bounded`, `invite`, `recent_invitations`, `list_roles`, `get_role`, `create_role`, `patch_role`, `search_api_keys`, `get_api_key`, `create_api_key`, `delete_api_key`, `company`, `license_state`, `get_asset_by_equipment_id`, `search_assets`, `search_work_orders`
- Produces: `probe_password_slots(identity, candidate, current) -> CredentialProbe`
- Produces: `reconcile_password_change(identity, probe) -> CredentialDecision`
- Produces: interactive `secret set-candidate`, generated `secret prepare-invitation-probe`, `secret set-runtime`, `secret generate-jwt` and descriptor-only `secret import-license-file`

- [ ] **Step 1: Write failing HTTP and credential-state tests**

Tests must assert:

- transport accepts only `http://127.0.0.1:3000/api`, disables environment proxies and redirects, caps headers/body, requires JSON media type and strictly parses JSON;
- no URL can change scheme, host, port or escape `/api`;
- JWT uses `Authorization: Bearer`, API Key uses `x-api-key`, and neither appears in an exception/log;
- `POST /auth/signin` uses `type=SUPER_ADMIN` for the source superadmin and `type=CLIENT` for both company identities;
- exact-email user search and bounded isolated-user inventory have distinct request builders; neither can silently substitute for the other;
- connection error, timeout, malformed JSON, `429` and `5xx` are `UNKNOWN`, never a deterministic authentication failure;
- signin is `AUTHENTICATED` only for successful strict `AuthResponse`; it is `REJECTED` only for exact HTTP `403`, JSON media type and `SuccessResponse(false, "Invalid credentials")`; every other `4xx`/body/header drift is `UNKNOWN`;
- only exact `403` plus `X-iFactory-CMMS-Policy: api-key-route-denied`, JSON media type and the bounded gateway `SuccessResponse` is classified as `GATEWAY_POLICY_DENIED`; missing/wrong header, default HTML, redirect or body drift is `UNKNOWN`;
- candidate-only authentication promotes candidate atomically; current-only preserves current and removes candidate only under a confirmed repair action; both/neither/unknown retain both and stop;
- after promotion, the new current must preserve the held candidate FD's `dev`/`ino`, size and `mtime_ns`; rename may advance `ctime_ns`, so the new full stat is recorded as lineage, the candidate logical path must be absent, and subsequent same-plan opens use that recorded value;
- a real temporary-directory rename test holds the candidate FD across `rename`, accepts only monotonic `ctime_ns`, and rejects a different inode or changed bytes/size/mtime;
- external current/candidate replacement before or after any one of the three identity promotions fails before the next HTTP call; all three legal promotions continue through the same confirmed bootstrap plan;
- a password change response loss always probes candidate then current before deciding;
- candidate equal to current is rejected without printing either value;
- `secret set-candidate` reads twice from `/dev/tty` or one caller-supplied file descriptor, enforces CMMS password length 6–50, writes one exact no-newline UTF-8 secret and never accepts a password argument;
- `secret prepare-invitation-probe` accepts only a canonical email argument, generates a high-entropy password internally, and atomically creates a random slot descriptor/password without printing either password or absolute path;
- `secret set-runtime` uses the same no-argv/no-output protocol for PostgreSQL password, MinIO username/password and license key; `generate-jwt` writes 32 random bytes as Base64; `import-license-file` accepts only a bounded descriptor and validates `-----BEGIN LICENSE FILE-----` plus `-----END LICENSE FILE-----` before atomic write;
- initial runtime-secret commands use exclusive creation and refuse an existing target; database, MinIO, JWT or license rotation requires a future coordinated plan and cannot be smuggled through this bootstrap helper;
- response fields that can contain tokens, especially signup `message`, API Key `code` and file/image/audio URLs carrying `X-Amz-*` queries, are secret-classified before any log/record conversion;
- malformed work-order/asset/company/user projections containing a presigned-query sentinel fail without putting raw JSON, URL, credential or signature into errors/evidence/repr.

Include:

```python
@pytest.mark.parametrize(
    ("candidate", "current", "expected"),
    (
        (
            AuthenticationResult.AUTHENTICATED,
            AuthenticationResult.REJECTED,
            CredentialDecision.PROMOTE_CANDIDATE,
        ),
        (
            AuthenticationResult.REJECTED,
            AuthenticationResult.AUTHENTICATED,
            CredentialDecision.CURRENT_ONLY,
        ),
        (
            AuthenticationResult.AUTHENTICATED,
            AuthenticationResult.AUTHENTICATED,
            CredentialDecision.AMBIGUOUS,
        ),
        (
            AuthenticationResult.UNKNOWN,
            AuthenticationResult.REJECTED,
            CredentialDecision.AMBIGUOUS,
        ),
    ),
)
def test_password_slot_decision(
    candidate: AuthenticationResult,
    current: AuthenticationResult,
    expected: CredentialDecision,
) -> None:
    probe = CredentialProbe(candidate=candidate, current=current)
    assert reconcile_password_change(Identity.SUPER_ADMIN, probe) is expected


def test_cmms_client_never_redirects_credentials() -> None:
    transport = ScriptedHttpTransport.redirect(
        location="http://evil.invalid/collect",
    )
    api = CmmsApi(transport=transport)
    with pytest.raises(DeploymentError) as caught:
        api.me(ApiCredential.api_key(RawApiKey("test-only-key")))
    assert caught.value.code == "CMMS-E081"
    assert transport.followed_redirects == []
    assert "test-only-key" not in caught.value.safe_message
```

- [ ] **Step 2: Run the API/credential tests and verify the expected failure**

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  e2e/test_cmms_api_and_credentials.py -v
```

Expected: FAIL because the API and credential modules do not exist.

- [ ] **Step 3: Implement the exact official API shapes**

Use these public-gateway routes and Python request projections; password variables are bytes read from validated descriptors and decoded only in process memory:

```python
sign_in_body = {
    "email": identity.email,
    "password": password_from_descriptor,
    "type": identity.login_type,
}
update_password_body = {
    "oldPassword": current_password_from_descriptor,
    "newPassword": candidate_password_from_descriptor,
}
organization_signup_body = {
    "email": organization.email,
    "password": organization_candidate_from_descriptor,
    "firstName": "iFactory",
    "lastName": "Admin",
    "phone": "",
    "companyName": "iFactory CMMS Development",
    "employeesCount": 1,
    "timeZone": "Asia/Shanghai",
}
runtime_signup_body = organization_signup_body | {
    "email": runtime_identity.email,
    "password": runtime_candidate_from_descriptor,
    "firstName": "iFactory",
    "lastName": "Runtime",
    "companyName": None,
    "role": {"id": role_id},
}
invite_body = {
    "role": {"id": role_id},
    "emails": [runtime_identity.email],
    "disableSendingEmail": True,
}
api_key_body = {"label": "ifactory-pdm-runtime"}
search_body = {
    "filterFields": [],
    "direction": "ASC",
    "pageNum": 0,
    "pageSize": 1,
    "sortField": "id",
}


def user_search_body(email: str) -> dict[str, JsonValue]:
    if not email.isascii() or email != email.strip() or email != email.casefold():
        raise DeploymentError(
            "CMMS-E082",
            "CMMS identity email is not canonical lowercase",
            82,
        )
    return {
        "filterFields": [
            {
                "field": "email",
                "value": email,
                "operation": "eq",
                "values": [],
            }
        ],
        "direction": "ASC",
        "pageNum": 0,
        "pageSize": 2,
        "sortField": "id",
    }


isolated_user_inventory_body = {
    "filterFields": [],
    "direction": "ASC",
    "pageNum": 0,
    "pageSize": 4,
    "sortField": "id",
}
```

Send these projections to `POST /auth/signin`, `POST /auth/updatepwd`, `POST /auth/signup`, `POST /users/invite`, `POST /users/search?enabledOnly=false`, `POST /api-keys`, `POST /api-keys/search?page=0&size=100`, `POST /assets/search` and `POST /work-orders/search` as applicable. `search_users_exact_email` uses one exact email filter and requires `totalElements` to be `0` or `1`. `list_users_bounded` is superadmin-only, sends the fixed unfiltered inventory body twice with no write between, requires stable `totalElements <= 3`, exact `len(content) == totalElements`, stable strictly increasing unique IDs and identical projected users; page size `4` proves that every possible planned user is returned in one bounded page. Any fourth/unexpected user fails without further paging. API-Key search uses `{}`; asset/work-order search uses `search_body`. Use exact `GET /api-keys/{id}` for safe metadata denial probes and `GET /assets/by-equipment-id/{canonical_uuid}` for company-scoped lookup.

Use `GET /auth/me`, `GET /license/state`, `GET /companies/{companyId}`, `GET /roles`, `GET /roles/{id}` and `GET /users/invitations/last-week` for readback. Never infer public access from `@PreAuthorize("permitAll()")`; provide auth except for the explicitly public health/license endpoints.

Do not use `GET /auth/{username}` to prove absence: the reviewed implementation calls `Optional.get()` and turns a missing user into `5xx`, which this client correctly classifies as `UNKNOWN`. Only a successful, strictly parsed `POST /users/search?enabledOnly=false` with the exact email filter may prove zero or one matching user.

Resolve every endpoint against the fixed origin without `urljoin` host replacement:

```python
def fixed_api_url(path: str) -> str:
    if not path.startswith("/") or path.startswith("//"):
        raise DeploymentError("CMMS-E080", "CMMS API path is invalid", 80)
    parsed = urlsplit(path)
    decoded_path = unquote(parsed.path)
    if (
        parsed.scheme
        or parsed.netloc
        or "\\" in decoded_path
        or ".." in PurePosixPath(decoded_path).parts
    ):
        raise DeploymentError("CMMS-E080", "CMMS API path is invalid", 80)
    return f"http://127.0.0.1:3000/api{path}"
```

- [ ] **Step 4: Implement strict typed response projections**

Parse only bounded fields needed for the workflow:

```text
AuthResponse.accessToken
SuccessResponse.success/message
IntegrationErrorResponse.code/message
UserResponseDTO.id/email/enabled/enabledInSubscription/companyId/companySettingsId plus the complete Role projection below
SignupSuccessResponse.success/user; classify message as secret
LicenseState.hasLicense/valid/planName/entitlements/expirationDate/usersCount
Company.subscription.subscriptionPlan.features
Role.id/name/description/externalId/roleType/code/paid and five permission collections
UserInvitationMiniDTO.email/roleId/roleName
ApiKeyShowDTO.id/label/user.id/code; classify code as secret
Page.content/totalElements
```

`SuccessResponse.message` is retained only in bounded process memory for exact operation-specific comparison; signup messages remain secret-classified. `IntegrationErrorResponse` is accepted only for reviewed integration routes; a nonexistent canonical equipment lookup must be exact `404`, `code=ASSET_NOT_FOUND`, `message="Asset was not found."`. `UserResponseDTO.role` reuses the complete Role projection so runtime `/auth/me`, including API-Key authentication, proves all five final permission sets and absence of `SETTINGS`. `ApiKeyShowDTO.user` is a `UserMiniDTO` and supplies only the owner ID needed to match the bootstrap receipt; company ownership is separately proven by company-scoped search.

The transport recognizes the gateway policy branch before ordinary endpoint projection, but only after checking all four invariants: status `403`, exact policy header, JSON media type and exact parsed `SuccessResponse(False, "API key route denied")`. It then returns a dedicated `GATEWAY_POLICY_DENIED` result and discards the body. A CMMS-origin `403`, default Nginx HTML, a missing policy header or any malformed/error body remains `UNKNOWN`; none may be used as proof that a request was blocked before upstream.

Reject wrong types, duplicate IDs, unexpected identity/company/role, duplicate labels and overlarge collections. `UserResponseDTO.id` must fit Java `Integer`; entity/company/role/API-Key IDs must fit their declared Java `Long`, then may be represented as Python `int`. Normalize `HashSet`-backed role permissions, license entitlements and company subscription-plan features by rejecting duplicates then comparing sorted tuples or frozen sets; never compare provider array order. Match roles, invitations and API-Key collections by stable ID/email/external ID, never response position.

CMMS mappers can embed newly presigned MinIO URLs in otherwise read-only work-order, asset, company and user JSON. Treat the entire bounded raw body as secret until projection succeeds, retain only required IDs/counts/status/permission fields, and drop every file/image/audio/path/URL field. Projection exceptions use endpoint/result codes only and never include a body fragment. Tests inject `X-Amz-Credential`, `X-Amz-Signature` and full-query sentinels into valid and malformed nested responses, then assert none reaches logs, exceptions, receipts or fake evidence.

- [ ] **Step 5: Implement current/candidate lifecycle**

Support three durable identity slots:

```text
super-admin
organization-admin
runtime-user
```

The source default superadmin password is an implicit, memory-only first current and is never copied into `.runtime`. For a not-yet-created company/runtime identity, current may be absent. Every remote password write requires a pre-existing candidate file; promotion is an atomic same-directory rename only after candidate-only authentication.

Bind the reviewed source defaults exactly as `superadmin@test.com`, `pls_change_me` and login type `SUPER_ADMIN`. Treat the default password as secret-classified despite being public upstream: use it only while gateway is loopback-only and never emit it.

If both credentials authenticate, neither authenticates, or either probe is uncertain, preserve both exact files, fail-close gateway, and require a new repair plan.

Implement the decision table directly:

```python
def reconcile_password_change(
    identity: Identity, probe: CredentialProbe
) -> CredentialDecision:
    pair = (probe.candidate, probe.current)
    if pair == (
        AuthenticationResult.AUTHENTICATED,
        AuthenticationResult.REJECTED,
    ):
        return CredentialDecision.PROMOTE_CANDIDATE
    if pair == (
        AuthenticationResult.REJECTED,
        AuthenticationResult.AUTHENTICATED,
    ):
        return CredentialDecision.CURRENT_ONLY
    return CredentialDecision.AMBIGUOUS
```

Only a confirmed identity-write `PlannedAction` may promote the candidate for
its exact bound target after successful authentication; it cannot promote
another identity. Candidate removal requires a discard-only repair containing
`repair.discard-rejected-candidate` with that exact identity target followed
by `repair.stop-loopback-runtime`; it cannot share a plan with another
bootstrap mutation. The decision function is pure, and target substitution or
a missing planned action fails before file rename.

The immutable plan binds each slot's starting lineage. Promotion opens the candidate with `O_NOFOLLOW`, retains that FD, verifies both logical paths against starting/current-lineage stats, and performs a same-directory atomic rename. After rename it fsyncs the directory, requires the candidate path absent, reopens current and requires the held candidate/current to have the same `dev`/`ino`, unchanged size/`mtime_ns` and stable bytes; `after.ctime_ns` must be greater than or equal to the pre-rename value because POSIX rename may advance ctime. The newly observed complete stat—not the stale pre-rename ctime—is appended in `CredentialSlotTransition` and becomes the lineage before any later identity action. Candidate discard similarly requires the plan-bound/lineage current to remain unchanged and records only the exact candidate disappearance.

`CredentialLineage` folds the immutable starting bindings plus receipt-recorded transitions for the current claimed plan. Every secret open, signin and later promotion checks the expected current/candidate stat from that fold; it never falls back to “whatever file is now at current.” This lets superadmin, organization-admin and runtime-user promotions continue safely in one bootstrap while rejecting an external swap. A crash after rename but before transition persistence is `UNKNOWN` and requires a new confirmed reconciliation repair; it cannot infer authorization from the path alone.

Invitation enforcement uses a separate one-use slot. `prepare-invitation-probe` generates a UUID slot ID plus 32 random bytes encoded within CMMS's 6–50 character limit, writes a `0600` descriptor containing the exact canonical email/logical password filename and a separate `0600` password, and refuses an existing active slot with that email. Plan generation securely snapshots both and binds the descriptor's email/slot ID plus password-file stat identity, never its bytes/hash. Apply must re-open the same metadata before HTTP. A confirmed exact `406` plus zero-user readback securely unlinks only that owned slot and fsyncs its directory; response loss or created-user evidence preserves descriptor/password for repair.

- [ ] **Step 6: Run the API/credential tests**

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  e2e/test_cmms_api_and_credentials.py \
  e2e/test_cmms_secure_artifacts.py -v
```

Expected: all fixed-origin, projection, secret and slot tests pass.

- [ ] **Step 7: Run API/credential slice verification**

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  e2e/test_cmms_api_and_credentials.py \
  e2e/test_cmms_secure_artifacts.py -v
git diff --check
./scripts/doctor.sh
```

Expected: tests pass and doctor has zero failures.

- [ ] **Step 8: Commit the API/credential slice**

Run:

```bash
set -Eeuo pipefail
git add \
  deploy/cmms/src/ifactory_cmms_deploy/cmms_api.py \
  deploy/cmms/src/ifactory_cmms_deploy/credentials.py \
  deploy/cmms/src/ifactory_cmms_deploy/cli.py \
  deploy/cmms/src/ifactory_cmms_deploy/config.py \
  tests/e2e/test_cmms_api_and_credentials.py
git commit -m "feat: add CMMS bootstrap API and credential protocol"
```

Expected: fake HTTP tests pass; no real credential is read or API called.

### Task 8: Implement Idempotent Bootstrap and Explicit Repair Planning

**Files:**

- Create: `deploy/cmms/src/ifactory_cmms_deploy/bootstrap.py`
- Create: `tests/e2e/test_cmms_bootstrap_state_machine.py`
- Modify: `deploy/cmms/src/ifactory_cmms_deploy/records.py`
- Modify: `deploy/cmms/src/ifactory_cmms_deploy/cmms_api.py`
- Modify: `deploy/cmms/src/ifactory_cmms_deploy/config.py`

**Interfaces:**

- Produces: enums `BootstrapState` and `InvitationProbeDisposition`, typed
  `BootstrapReceipt` and the `BOOTSTRAP_ACTIONS` subset of central
  `ActionCode`, constrained by Task 2's fixed structural schema
- Produces: `BootstrapReceiptStore.load_current(state: StateRecord) -> BootstrapReceipt | None`
- Produces: `BootstrapReceiptStore.append_and_anchor(context: ClaimedApplyContext, previous_state: StateRecord, next_receipt: BootstrapReceipt) -> tuple[BootstrapReceipt, StateRecord]`
- Produces: `BootstrapTargetBindings.from_plan_bindings(bindings: BootstrapPlanBindings) -> BootstrapTargetBindings`
- Produces: `BootstrapAssessment(state, live_ids, target_bindings, evidence_codes, repair_required)`
- Produces: `BootstrapAssessment.from_receipt(snapshot, receipt, target_bindings) -> BootstrapAssessment` with no HTTP
- Produces: `BootstrapReconciler.inspect(context: ClaimedApplyContext, inspection_action: PlannedAction, snapshot: DeploymentSnapshot, receipt: BootstrapReceipt | None, api: CmmsApi, slots: CredentialSlots, invitation_probe: InvitationProbeSlot | None) -> BootstrapAssessment`
- Produces: `BootstrapPlanner.plan(assessment: BootstrapAssessment) -> Sequence[PlannedAction]` as an immutable tuple
- Produces: `BootstrapApplier.apply(context: ClaimedApplyContext, api: CmmsApi, slots: CredentialSlots, invitation_probe: InvitationProbeSlot | None, phase2_env: Path) -> BootstrapReceipt`
- Produces: secret `ApiKeyCaptureEnvelope(schema_version, record_type, status, plan_sha256, attempt_id, action_code, target_kind, target_id, api_key_id, label, runtime_user_id, company_id, raw_api_key)`
- Produces: `ApiKeyCaptureStore.reserve/capture/load/clear` under the exact attempt-derived private path
- Produces: opaque immutable `ApiKeyFileLineage`, `AnchoredApiKeyCapture` and `PublishedApiKeyCredential`; only a successful `BootstrapReceiptStore.append_and_anchor` for the same `ClaimedApplyContext` can advance them
- Persists: immutable `.runtime/receipts/cmms-bootstrap/{receipt_sha256}.json` generations selected only by `StateRecord.bootstrap_receipt_sha256`
- Produces for tests only: `BootstrapFixture`
- Produces: bootstrap state sequence through `FINAL_PERMISSIONS_VERIFIED`; lifecycle alone records `GATEWAY_ENABLED` after composite readiness and dual-listener proof
- Consumes: only CMMS formal API and the verified Phase 2 env file; never SQL/ORM

- [ ] **Step 1: Write failing bootstrap/recovery tests**

Use scripted fake HTTP and crash injection. Include these two core tests, then add parameter rows for each non-idempotent write and crash boundary:

```python
def test_lost_api_key_response_plans_verified_revoke_before_recreate(
    bootstrap_fixture: BootstrapFixture,
) -> None:
    existing_key_id = "42"
    receipt = bootstrap_fixture.receipt_with_discovered_uncaptured_api_key(
        api_key_id=existing_key_id,
        label="ifactory-pdm-runtime",
        owner="runtime-user",
    )
    assessment = BootstrapAssessment.from_receipt(
        bootstrap_fixture.snapshot,
        receipt,
        bootstrap_fixture.target_bindings,
    )
    actions = BootstrapPlanner().plan(assessment)

    assert actions == (
        PlannedAction(
            ActionCode.REPAIR_REVOKE_UNCAPTURED_API_KEY,
            ActionTargetKind.API_KEY_ID,
            existing_key_id,
        ),
    )
    assert bootstrap_fixture.http.calls == []


def test_api_key_is_published_only_after_final_role_readback(
    bootstrap_fixture: BootstrapFixture,
) -> None:
    plan = bootstrap_fixture.confirmed_bootstrap_plan()
    receipt = BootstrapApplier().apply(
        bootstrap_fixture.claimed_context(plan),
        bootstrap_fixture.api,
        bootstrap_fixture.credential_slots,
        bootstrap_fixture.invitation_probe,
        bootstrap_fixture.phase2_env,
    )

    events = bootstrap_fixture.events
    assert events.index("api-key.capture-private") < events.index("role.patch-final")
    assert events.index("role.readback-final") < events.index("phase2.publish")
    assert receipt.state is BootstrapState.FINAL_PERMISSIONS_VERIFIED
```

Cover:

- fresh bootstrap happy path and exact write/readback order;
- no empty bootstrap receipt generation exists: the first persisted generation is `receipt_generation=1`, contains the pending `action_attempt` for the exact first planned non-idempotent action, has `origin_plan_sha256=last_plan_sha256` equal to the claimed plan and is State-selected before that request;
- `readiness.require-api-loopback` is consumed once before the first discovery read/bootstrap write and proves only API PID/socket/fixed loopback health plus the license barrier; the complete `readiness.require-loopback` action occurs only after the planned bootstrap mutations and final reconciliation;
- request not sent, deterministic rejection, response loss and process crash after every non-idempotent write;
- superadmin, company admin and runtime user candidate/current promotion rules;
- existing company/user/role/key metadata must match planned email/company/externalId/label and receipt IDs;
- before any bootstrap write, license `usersCount` must be at least `2`, the superadmin-only bounded inventory must prove the isolated database contains exactly the planned partial user set, and bounded asset/work-order searches must both prove `totalElements=0`;
- no action repeats a confirmed password change, signup, invitation, role creation or API Key creation;
- stale receipt with replacement/empty database fails rather than trusting local state;
- invitation reconciliation uses only `/users/invitations/last-week`; if the user does not exist and a receipt is older than seven days, status is `UNRESOLVABLE_OLD_INVITATION` and no reinvite occurs;
- uninvited signup probe must receive HTTP `406` with the exact reviewed invitation-guard message, and superadmin-authenticated exact email search must then return `totalElements=0`; any other `406` fails;
- lost/timeout probe response plus a zero-user search is `UNKNOWN`, not proof that the invitation guard rejected the request; preserve the probe credential, fail closed and require a fresh email in a new plan;
- unexpected probe-user creation retains its generated probe password candidate and stops for repair;
- replacing the plan-bound probe email/slot metadata fails before HTTP; crash recovery uses the pending slot descriptor for exact search and never resends that probe;
- role creation body is full and company-bound; sparse permission PATCH is rejected;
- API Key is created by the runtime user while its role temporarily contains `SETTINGS`;
- the Phase 2 env target and an exclusive private capture reservation are validated before the API-Key POST; raw API Key is captured exactly once, but is not published to the Phase 2 env until `SETTINGS` has been removed and verified;
- capture reservation/envelope binds the creating plan, action attempt, canonical Java `Long` key ID, label, runtime-user/company IDs and raw key; every mismatch is fail-closed and can never publish;
- the receipt loader accepts only the enumerated capture-status/publish-status/file-binding/cleanup-flag combinations and rejects every invalid cross-product;
- the atomically reopened `CAPTURED` envelope's complete `SecureFileStatBinding` is stored in `BootstrapReceipt`, whose canonical SHA is then anchored in `StateRecord`; only that complete chain proves which one-time raw key belongs to which returned ID;
- a fresh plan keeps `api_key_capture=null`; after the create action, only the same claimed apply's `ApiKeyFileLineage` may derive `AnchoredApiKeyCapture` from the newly State-selected immutable receipt generation and feed later finalize/publish actions;
- an orphan receipt generation, plain response object, file path, naked stat or capability from another context/plan cannot advance the lineage; a failed transition leaves downstream actions unable to unwrap or publish;
- replacing the capture envelope after its receipt/State anchor, even with the same metadata or another valid raw key, fails its plan-bound stat lineage before secret unwrap or HTTP;
- crash before capture, after capture, after receipt update and before/after state anchoring produces the specified discovery/revoke path without a second API-Key create; a capture not yet anchored by `StateRecord` is never promoted into trusted evidence;
- response loss with an existing label but no captured raw key produces a repair action to search, ownership-check and delete the exact key before a separately confirmed recreate;
- CMMS search/readback exposes only the mapper-masked API-Key code and therefore can never reconstruct or prove the raw-key-to-ID relationship;
- Phase 2 publication records and State-anchors the published env file stat before capture unlink, then separately records and anchors capture cleanup; crashes at every publication/cleanup boundary are reconciled without a second create or an unanchored publication;
- cleanup-only repair is planned only when an already anchored publish/revoke result says cleanup is pending and the exact capture path is either still the historical file or absent; it performs zero authentication/HTTP/secret unwrap, unlinks only the exact present stat, proves absence, and rejects substitution/reappearance or a changed Phase 2 stat before receipt mutation;
- bootstrap-mutation completion is exactly the suffix selected by Task 2's
  frozen `(BootstrapState, probe disposition, slot class)` table and ends in
  Phase 2 publish; tests cover every row, especially `ROLE_CREATED` with an
  unused slot, terminal `ENFORCED`, `UNKNOWN_NO_USER` plus a distinct new slot,
  and pending/uncertain plus the exact old slot, then reject a
  skipped/interleaved state action, an unjustified omitted/repeated probe and
  every revoke/create/publish or discard/completion mixture;
- probe-history fold tests cover zero/one rows and the sole legal two-row
  chain `UNKNOWN_NO_USER -> distinct new attempt`; while the second is pending
  only its slot can be discovered, its terminal `ENFORCED` becomes effective,
  a second `UNKNOWN_NO_USER` exhausts retry without authorizing a third, and
  every reused/interleaved/third/post-terminal attempt is invalid;
- a State-selected completed publication with capture cleanup proven complete
  selects an empty-`C` readiness-only repair when completion reconciliation
  remains; crash tests cover immediately after the publish anchor, capture
  unlink, cleanup anchor, composite pre-open, dual enable, `GATEWAY_ENABLED`
  State anchor, acceptance-receipt write and application terminal write, and
  prove that cleanup-only (when needed) precedes a new readiness-only plan
  without replaying publish or any bootstrap mutation;
- discovery, revoke-only and discard-only each end with the planned untargeted `repair.stop-loopback-runtime`; tests inject failures before/after receipt anchor, unit stop, listener proof and State process clearing and require the application to remain `IN_PROGRESS` unless the proven stopped state is durable;
- every planned bootstrap/repair action has the exact required target kind/ID; target substitution, a naked `ActionCode`, or a revoke without a company-scoped discovered API-Key ID is rejected;
- API-Key targets reject UUIDs, zero, signs, leading zeros and values above Java `Long.MAX_VALUE`; the client sends only the parsed positive `Long`;
- every reconciler entry receives explicit `CredentialSlots`; no planner or repair path reads identity credentials from ambient state;
- reconciler/applier reject a missing/forged `ClaimedApplyContext` or an inspection action absent from that context before transport; public plan/status tests observe zero calls;
- existing Phase 2 env update validates keys against `deploy/compose/predictive-maintenance-shadow.env.example`, preserves every other validated assignment, changes only `PILOT_CMMS_CREDENTIAL`, and serializes the captured value as the strict `cmms_api_key` envelope without logging it;
- final API Key can call only the gateway allowlist: `/auth/me`, work-order search, canonical equipment lookup and a later separately authorized idempotent asset POST; its embedded role lacks `SETTINGS`; safe non-allowlisted reads receive gateway `403` before reaching CMMS;
- final asset and work-order totals are both zero.

- [ ] **Step 2: Run the bootstrap tests and verify the expected failure**

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  e2e/test_cmms_bootstrap_state_machine.py -v
```

Expected: FAIL because the state machine is absent.

- [ ] **Step 3: Add the explicit state and transition table**

Define every durable state and every plan-visible action:

```python
class BootstrapState(StrEnum):
    UNINITIALIZED = "UNINITIALIZED"
    ADMIN_ROTATED = "ADMIN_ROTATED"
    COMPANY_CREATED = "COMPANY_CREATED"
    ROLE_CREATED = "ROLE_CREATED"
    INVITATION_CREATED = "INVITATION_CREATED"
    RUNTIME_IDENTITY_CREATED = "RUNTIME_IDENTITY_CREATED"
    API_KEY_CAPTURED = "API_KEY_CAPTURED"
    FINAL_PERMISSIONS_VERIFIED = "FINAL_PERMISSIONS_VERIFIED"
    GATEWAY_ENABLED = "GATEWAY_ENABLED"


BOOTSTRAP_ACTIONS = frozenset(
    {
        ActionCode.BOOTSTRAP_ROTATE_SUPER_ADMIN,
        ActionCode.BOOTSTRAP_CREATE_ORGANIZATION,
        ActionCode.BOOTSTRAP_CREATE_ROLE,
        ActionCode.BOOTSTRAP_PROBE_INVITATION,
        ActionCode.BOOTSTRAP_CREATE_INVITATION,
        ActionCode.BOOTSTRAP_CREATE_RUNTIME_IDENTITY,
        ActionCode.BOOTSTRAP_CREATE_API_KEY,
        ActionCode.BOOTSTRAP_FINALIZE_ROLE,
        ActionCode.BOOTSTRAP_PUBLISH_PHASE2_KEY,
        ActionCode.REPAIR_CAPTURE_BOOTSTRAP_DISCOVERY,
        ActionCode.REPAIR_REVOKE_UNCAPTURED_API_KEY,
        ActionCode.REPAIR_FINALIZE_API_KEY_CAPTURE_CLEANUP,
        ActionCode.REPAIR_DISCARD_REJECTED_CANDIDATE,
    }
)


NEXT_STATE = {
    (BootstrapState.UNINITIALIZED, ActionCode.BOOTSTRAP_ROTATE_SUPER_ADMIN):
        BootstrapState.ADMIN_ROTATED,
    (BootstrapState.ADMIN_ROTATED, ActionCode.BOOTSTRAP_CREATE_ORGANIZATION):
        BootstrapState.COMPANY_CREATED,
    (BootstrapState.COMPANY_CREATED, ActionCode.BOOTSTRAP_CREATE_ROLE):
        BootstrapState.ROLE_CREATED,
    (BootstrapState.ROLE_CREATED, ActionCode.BOOTSTRAP_CREATE_INVITATION):
        BootstrapState.INVITATION_CREATED,
    (
        BootstrapState.INVITATION_CREATED,
        ActionCode.BOOTSTRAP_CREATE_RUNTIME_IDENTITY,
    ): BootstrapState.RUNTIME_IDENTITY_CREATED,
    (BootstrapState.RUNTIME_IDENTITY_CREATED, ActionCode.BOOTSTRAP_CREATE_API_KEY):
        BootstrapState.API_KEY_CAPTURED,
    (BootstrapState.API_KEY_CAPTURED, ActionCode.BOOTSTRAP_FINALIZE_ROLE):
        BootstrapState.FINAL_PERMISSIONS_VERIFIED,
}


def advance_state(
    current: BootstrapState,
    action: PlannedAction,
) -> BootstrapState:
    ActionRegistry.require_bootstrap_target(action)
    try:
        return NEXT_STATE[(current, action.code)]
    except KeyError:
        raise DeploymentError(
            "CMMS-E088",
            "bootstrap transition is not permitted",
            88,
        ) from None
```

The invitation-enforcement probe, Phase 2 key publication, discovery-only repair, revocation repair and capture-cleanup finalization action never advance the bootstrap state. Each requires its own confirmed targeted `PlannedAction` and durable pending attempt or exact local cleanup transition. The probe uses the exact fresh email and `InvitationProbeSlot` already bound into the confirmed plan; apply cannot generate or substitute a target. It can create a real user if the upstream guard is broken. Before the POST, atomically record slot ID and email hash in the pending attempt. Unexpected creation adds the returned/read-back user ID plus email hash to the receipt, retains the descriptor/password, leaves the gateway fail-closed and requires a separate cleanup design.

Only a received exact `406` message followed by exact email search
`totalElements=0` resolves the probe as enforced and permits cleanup in the
original apply. If the response is lost, a zero-user search cannot distinguish
a committed guard rejection from another uncertain outcome: retain the
credential and pending attempt, stop, and never resend the same write. The next
discovery-only repair must bind the old `InvitationProbeSlot` from that
attempt, reject omission/slot substitution before HTTP, and use it only for
authentication/search reconciliation. If that confirmed discovery again
proves exact zero users, it records `UNKNOWN_NO_USER`, securely retires the old
owned slot and still does not mark the guard enforced; only a later plan with a
different pre-created slot/email may send a new probe. If a user exists or the
read is uncertain, preserve the old descriptor/password for the separately
designed cleanup.

After exact revoke/readback, append and State-select a new `BootstrapReceipt`
generation that resolves the targeted attempt, adds the ID to bounded
`revoked_api_key_ids`, clears the active `api_key_id`, and retains/returns
state `RUNTIME_IDENTITY_CREATED`. The same plan must then execute
`repair.stop-loopback-runtime` and durably clear/prove the API/guard process
identities before its `PlanApplicationRecord` may become terminal. No separate
undefined repair-receipt type is created. Recreating the API Key requires a
separately generated and confirmed stopped-state plan. `GATEWAY_ENABLED` is
intentionally absent from this transition table because Task 9 lifecycle owns
the final readiness, dual-listener proof and receipt update.

`NEXT_STATE` is only the state-transition validator. It is not the repair
suffix selector because the invitation probe and Phase 2 publication do not
advance `BootstrapState`. `BootstrapAssessment.from_receipt` must derive the
strict probe disposition from the exact action-attempt rows and apply Task 2's
frozen receipt/probe/slot table. In particular, `ROLE_CREATED` with a required
new probe starts at `BOOTSTRAP_PROBE_INVITATION`, while terminal `ENFORCED`
starts at `BOOTSTRAP_CREATE_INVITATION`; a pending/uncertain old probe emits
discovery-only, and `UNKNOWN_NO_USER` can probe again only with a distinct
unused slot under the sole legal one-row history. The strict fold selects the
second attempt when present and never authorizes a third. No generic “next
state” fallback is allowed.

- [ ] **Step 4: Implement receipt-first reconciliation**

The bootstrap receipt uses only the exact Task 2 top-level schema. This task
adds the bounded action-attempt row, capture/publish cross-product and
transition validators; it does not extend the wire record.

`receipt_generation` starts at `1` and increments exactly once from the State-selected immutable predecessor; it is not chosen by scanning filenames. `origin_plan_sha256` is immutable; `last_plan_sha256` advances only with a claimed bootstrap/repair plan. `action_attempts` is a bounded tuple of `plan_sha256`, `action_code`, `target_kind`, `target_id`, random `attempt_id`, `started_at`, safe `result_code`, optional `credential_transition`, and optional invitation-probe `slot_id`/`email_hash`. Atomically append the pending row by creating and State-anchoring a new immutable receipt generation before each non-idempotent request, then resolve that same row in another immutable generation only after deterministic readback. The strict loader validates the referenced plan hash, target policy and credential-lineage transition for every row; a repair may append a row under its new plan hash but may never rewrite an earlier attempt or substitute its target. Tests mutate the plan/target bindings and every before/after slot stat independently and require rejection before HTTP. This timestamp is the only local input allowed when deciding whether an uncertain invitation attempt has crossed the API's seven-day visibility window. `probe_user_id` is non-null only when an unexpected created user was formally read back; receipt never stores probe email/password plaintext.

`api_key_capture_status` is exactly `NONE`, `ANCHORED_RAW` or `REVOKE_REQUIRED`; `phase2_publish_status` is exactly `NOT_PUBLISHED` or `PUBLISHED`. `api_key_capture_file` and `phase2_env_file` are nullable secret-free `SecureFileStatBinding` values, and `api_key_capture_cleared` is a boolean. The strict loader enforces valid combinations: only an `ANCHORED_RAW` capture whose receipt hash is the current `StateRecord.bootstrap_receipt_sha256`, whose cleanup flag is false and whose file still has the exact stat can produce `ApiKeyCapturePlanBinding`; `REVOKE_REQUIRED` can authorize only discovery/exact revoke/owned-file cleanup; `PUBLISHED` requires an anchored resulting Phase 2 env stat and never authorizes another API-Key create. If an anchored publish/revoke says cleanup is pending, only `repair.finalize-api-key-capture-cleanup` may carry `ApiKeyCleanupPlanBinding`; the observed capture must be either the exact historical stat or exact absence. The receipt retains no raw key or content digest.

The valid lifecycle combinations are explicit: initial/fully revoked cleanup is `NONE`, no active capture file binding, `NOT_PUBLISHED`, and `api_key_capture_cleared=true`; trusted pre-publication capture is `ANCHORED_RAW`, exact historical/current capture stat, `NOT_PUBLISHED`, false; untrusted discovered capture is `REVOKE_REQUIRED`, exact cleanup stat, `NOT_PUBLISHED`, false; publication pending cleanup is `ANCHORED_RAW`, historical/current capture stat, `PUBLISHED`, exact Phase 2 env stat, false; completed publication retains the historical capture stat and published env stat but sets cleared true. A revoke generation adds the ID to `revoked_api_key_ids`, clears active `api_key_id`, retains the cleanup stat and false until cleanup; its cleanup generation changes to `NONE`, null active capture binding and true. Every other cross-product is rejected.

API-Key capture is the sole secret-bearing exception and is not a canonical receipt. Before `POST /api-keys`, derive `.runtime/secrets/api-key-captures/{attempt_id}.json` only from the already persisted random attempt ID, exclusively create a `0600` `PENDING` envelope with exact `schema_version`, `record_type=cmms-api-key-capture-secret`, confirmed plan hash, attempt ID, action code, `target_kind`, `target_id`, label and receipt-bound runtime-user/company IDs, then reopen it. A successful create response must contain a canonical Java `Long` ID, same label/owner and one raw code; atomically replace/reopen the envelope as `CAPTURED` with that exact ID and raw key, then obtain its complete stable `SecureFileStatBinding`. The strict secret-envelope loader rejects an unknown field or any plan/action/target mismatch before unwrapping the raw key.

After capture, append/reopen an immutable receipt generation with the same `api_key_id`, `api_key_capture_attempt_id`, `api_key_capture_status=ANCHORED_RAW`, the exact captured-file stat and `api_key_capture_cleared=false`, then use the common State pointer CAS protocol. Only after reopening and cross-checking the capture, State-selected receipt generation and State pointer is the raw-key-to-ID proof durable. A later continuation plan binds that exact receipt-anchored capture stat and current Phase 2 env stat; apply checks both before secret unwrap or HTTP. On reconciliation, require exact equality among envelope plan/attempt/ID/label/user/company, receipt/action attempt and a fresh company-scoped API-Key search. The reviewed `ApiKeyMapper` masks `code` on every later show/search response, so live API metadata can confirm ID/label/owner/company but cannot reconstruct or independently prove which raw key belongs to that ID.

For a fresh plan, `ApiKeyFileLineage` starts with the plan-bound Phase 2 env stat and no capture. It is private to the exact `ClaimedApplyContext`. `advance_capture()` accepts only the receipt and State returned by `BootstrapReceiptStore.append_and_anchor()` for that context, proves the State pointer selects the generation containing the just-observed capture stat, then returns `AnchoredApiKeyCapture`. Final-role verification and publication require this opaque value, not `context.plan.bootstrap_bindings.api_key_capture`. `advance_publication()` similarly accepts only the State-selected `PUBLISHED` generation and returns `PublishedApiKeyCredential`; `advance_cleanup()` accepts only the State-selected cleanup generation. For a continuation plan, the lineage may start at `AnchoredApiKeyCapture` only from the exact plan-bound capture plus current State-selected receipt. An orphan generation, response DTO, path/stat tuple or another context's capability cannot advance it. This dynamic lineage is apply evidence under an already planned semantic action; it does not mutate or broaden the immutable action list.

A crash before the receipt and State anchor are both durable makes any `CAPTURED` bytes untrusted, even when their embedded metadata and live search match. A confirmed discovery repair may record the company-scoped live ID and exact owned file metadata as `REVOKE_REQUIRED`, but it must not unwrap, publish, re-anchor as `ANCHORED_RAW` or continue finalization; the next new confirmed plan may only revoke that exact ID. If the capture receipt and State anchor are complete but the original application remains `IN_PROGRESS`, a new confirmed reconciliation plan may revalidate that exact stat/metadata/live ownership and continue without another create. Missing raw capture, identity/stat mismatch or uncertain search likewise can never publish or recreate.

Phase 2 publication is a two-anchor protocol. The targeted publisher requires either the same-apply `AnchoredApiKeyCapture` or a continuation lineage initialized from the plan-bound capture, reopens that exact capture and current Phase 2 env, atomically writes/reopens the env, and compares its strict credential envelope with the captured raw value only in memory. It then appends `phase2_publish_status=PUBLISHED` with the resulting `phase2_env_file` stat while `api_key_capture_cleared=false`, CAS-selects that immutable generation in `StateRecord`, and advances/reopens/cross-checks the lineage. Only then may the same planned publish action unlink the exact lineage-bound capture and fsync the directory; it finally appends `api_key_capture_cleared=true`, CAS-selects that generation and advances cleanup. A crash after env write but before the first publish anchor can only be reconciled by a new plan that binds the current env and still-State-anchored capture and proves their raw values equal; a crash between the publish anchor, unlink and cleanup anchor uses the retained immutable published generation to run only cleanup.

Exact revoke uses the same ordering: after company-scoped target verification, revoke/readback is recorded in an immutable State-selected generation before the owned capture is removed; capture removal and its final receipt generation are separately anchored. If cleanup is still pending after an anchored publish/revoke, a new plan emits exactly two plan-visible rows—untargeted `gateway.fail-closed`, then `repair.finalize-api-key-capture-cleanup` targeted at `receipt:cmms-bootstrap`—with `ApiKeyCleanupPlanBinding`. After the first row proves the external listener absent, lifecycle performs the implicit application claim and zero-HTTP local preflight before dispatching the second row; neither barrier is serialized as a fake action. Apply requires the observed file to be the historical exact stat or absent; if present it opens without unwrapping, rechecks/unlinks/fsyncs by descriptor identity, otherwise it proves continued absence. It then appends/selects only the cleanup generation, with zero startup, license, permit, authentication, HTTP, raw unwrap, publish/revoke, readiness or dual-gateway action. A substituted/reappearing file or changed published Phase 2 stat is `UNKNOWN`. A crash anywhere preserves enough `PENDING`/`CAPTURED`, attempt, immutable receipt and State evidence to choose reconciliation, never to infer success from a label or file existence.

Never store email plaintext if a stable label/hash is sufficient, and never store JWT/password/raw API Key. Every reconciliation entry that selects or validates a remote action authenticates through current/candidate slots and reads live metadata before selecting missing actions; a receipt cannot prove a remote fact by itself. The cleanup-only finalizer is the deliberate exception: it selects no remote action and reads only the State-selected local receipt lineage plus exact capture/Phase 2 file presence stats, with zero signin or HTTP.

`BootstrapReconciler.inspect` is an apply-time interface whose first argument
must be the opaque `ClaimedApplyContext`. Lifecycle must already have consumed
the distinct planned `readiness.require-api-loopback` row immediately before
calling this interface; the fixed transport rechecks the bound loopback
gateway generation and current PID/start identity before each send. The
reconciler verifies that `inspection_action` is an exact row in that context's
confirmed plan and that ActionRegistry marks the row as permitting
reconciliation for the current operation; discovery uses only
`repair.capture-bootstrap-discovery`, composite ordinary readiness uses its
planned `readiness.require-loopback` row, and a bootstrap write may use only
its exact next targeted row. A forged/missing context, stale API-loopback
binding or wrong row fails before transport. Signin/readback may update
CMMS-owned `lastLogin`; public `plan` and `status` cannot construct the
capability and never call it. Planning uses only
`BootstrapAssessment.from_receipt` plus local descriptor/config/source
evidence. A missing live target therefore selects discovery-only repair
instead of pre-confirmation authentication.

- [ ] **Step 5: Implement identity, company, role, and invitation actions**

Apply only actions already enumerated in the confirmed plan:

1. Authenticate the current/candidate superadmin with `type=SUPER_ADMIN` without changing either slot. Read `/license/state`; require `hasLicense=true`, `valid=true`, entitlements `API_ACCESS` and `CUSTOM_ROLES`, and `usersCount >= 2`.
2. Before the first write, call `list_users_bounded` and require the exact live set to equal `{superadmin}` plus only organization/runtime identities already bound by the partial receipt. Reject disabled, subscription-disabled, wrong-role/company, duplicate or unexpected users. The intended final paid-user count is exactly `2` and must not exceed license `usersCount`.
3. Before the first write, run bounded superadmin asset and work-order searches and require both `totalElements=0`; repeat both zero checks after finalization. A dirty domain baseline therefore fails before password, signup, role, invitation or API-Key mutation.
4. Rotate the source superadmin through `/auth/updatepwd`, probe candidate/current, then promote.
5. Under the semantic target `identity:organization-admin`, signup the one `ALLOWED_ORGANIZATION_ADMINS` identity without a role; probe candidate/current and verify `/auth/me`. A fresh plan binds the canonical email/config target, not nonexistent live IDs. Resolve the pending attempt only after atomically recording the returned/read-back `organization_admin_user_id`, `company_id` and `company_settings_id` in the bootstrap receipt.
6. Reopen that receipt, authenticate the same candidate/current identity, require `/auth/me` to reproduce all three IDs, then read `/companies/{companyId}` and require subscription features `API_ACCESS` and `ROLE`.
7. Under the semantic target `role-external-id:ifactory-pdm-runtime`, create the integration role with the receipt-bound `/auth/me.companySettingsId`, `roleType=ROLE_CLIENT`, `code=USER_CREATED`, stable external ID and full permission sets. Reject a missing receipt ID, a different company/settings ID, or a live ID sourced only from an uncommitted response.
8. Run the separately planned invitation-enforcement probe only after role and capacity checks; require `406` plus an in-memory exact comparison with `You are not invited to this organization for this role`, then prove by exact superadmin email search that the probe identity was not created. Never log the signup response body.
9. Invite the runtime email with exact role ID and `disableSendingEmail=true`; accept the documented `company.invitedUsers=true` first-invitation flag, while fixed `INTERCOM_TOKEN=` guarantees the asynchronous Intercom event is a local no-op in both license modes.
10. Signup the invited runtime identity, promote its candidate and verify company/role.

Before every action, enforce membership in the immutable confirmed plan:

```python
def require_planned_action(
    context: ClaimedApplyContext,
    code: ActionCode,
    *,
    target_kind: ActionTargetKind | None,
    target_id: str | None,
) -> PlannedAction:
    expected = PlannedAction(code, target_kind, target_id)
    context.require_action(expected)
    ActionRegistry.require_allowed(context.plan.operation, expected)
    return expected
```

Before each write, durably record its current `plan_sha256`, exact action code and target in the pending attempt. After the write, perform its formal API readback/authentication probe and atomically resolve the attempt plus new secret-free state before considering the transition complete.

- [ ] **Step 6: Implement API-Key capture and permission finalization**

Continue the confirmed sequence:

11. Under targeted action `bootstrap.create-api-key`, validate and stat-bind the existing Phase 2 env, persist the pending action attempt, exclusively create/reopen its bound `PENDING` capture envelope, then login as runtime identity and create one API Key while temporary role permissions include `SETTINGS`.
12. Immediately atomically capture/reopen the one-time raw key plus returned ID/label/user/company in that reservation, record its complete stable stat with the same ID/attempt in a new immutable `BootstrapReceipt` generation, CAS-select that receipt SHA in `StateRecord`, and advance the same-context `ApiKeyFileLineage`; do not publish it to the Phase 2 env yet. If this full anchor is interrupted, the orphan generation authorizes nothing: a new confirmed discovery plan must independently read the company-scoped live ID and State-select a `REVOKE_REQUIRED` generation, and only another new plan may revoke that exact ID.
13. Reconcile the `AnchoredApiKeyCapture` (derived from same-apply lineage or an exact continuation-plan binding), State-selected receipt and company-scoped API-Key search exactly. Treat the API's masked `code` as metadata only. Then login as company admin, PATCH all role fields to final permissions without `SETTINGS`; verify `/auth/me` embeds that exact final role. With the reconciled captured key, allow only `/auth/me`, work-order search and one canonical nonexistent equipment lookup; require gateway `403` plus the policy header for safe probes to `/roles`, `GET /api-keys/{verifiedId}` and `POST /api-keys/search?page=0&size=100`, and prove the fake/upstream transport was not called.
14. Never send a negative-test API-Key create/delete or asset-create request because an authorization regression could make that probe mutate state. Bind the allowlist manifest to the current Phase 2 client/contract digests instead.
15. Only targeted action `bootstrap.publish-phase2-api-key` may atomically update/reopen the plan-bound Phase 2 env using the fully reconciled `AnchoredApiKeyCapture`. It must prove the asset/work-order baseline is zero, append/State-select the resulting env stat, advance publication lineage, then securely clear only that lineage-bound capture envelope and append/State-select the cleanup generation. A crash or mismatch at any capture/receipt/State/publication/cleanup boundary stops for confirmed discovery/reconciliation and never repeats create or publishes by label.

This deployment never calls `POST /assets`. A later Phase 2 provisioning plan remains responsible for supplying `equipment_id` plus `Idempotency-Key` on every asset create; the bootstrap/API-Key approval does not authorize that write.

Removing `SETTINGS` does not by itself close every reviewed CMMS controller. The gateway therefore enforces the exact runtime API-Key allowlist from Task 4 before proxying, while the role remains the backend authorization layer. `POST /api/assets` stays allowlisted only because Phase 2 requires it; Nginx enforces the idempotency-key shape, and the separately confirmed Phase 2 plan plus CMMS contract remain responsible for the request body and actual write.

- [ ] **Step 7: Use exact temporary and final role permissions**

Create the role with:

```python
temporary_role_body = {
    "name": "iFactory Integration",
    "description": (
        "Least-privilege CMMS role for the iFactory predictive-maintenance integration"
    ),
    "externalId": "ifactory-pdm-runtime",
    "roleType": "ROLE_CLIENT",
    "code": "USER_CREATED",
    "companySettings": {"id": organization_me.company_settings_id},
    "createPermissions": ["ASSETS"],
    "viewPermissions": ["ASSETS", "WORK_ORDERS", "SETTINGS"],
    "viewOtherPermissions": ["ASSETS", "WORK_ORDERS"],
    "editOtherPermissions": [],
    "deleteOtherPermissions": [],
}
final_role_patch = {
    "name": temporary_role_body["name"],
    "description": temporary_role_body["description"],
    "externalId": temporary_role_body["externalId"],
    "createPermissions": ["ASSETS"],
    "viewPermissions": ["ASSETS", "WORK_ORDERS"],
    "viewOtherPermissions": ["ASSETS", "WORK_ORDERS"],
    "editOtherPermissions": [],
    "deleteOtherPermissions": [],
}
```

`organization_me.company_settings_id` must be the live `/auth/me.companySettingsId` value recorded receipt-first by the completed organization action and reproduced by a fresh same-identity readback immediately before role creation. It is deliberately absent from a fresh pre-signup plan; that plan instead binds the canonical organization identity/config and role external ID. The final PATCH sends name, description, external ID and all five permission collections; it omits `companySettings`, `roleType` and `code` because `RolePatchDTO` has no such fields.

Because `companySettings` is write-only in responses, use exact request binding, successful company-authenticated `GET /roles/{id}`, and the later full PATCH/readback as evidence. Do not claim the response itself returned `companySettings`.

- [ ] **Step 8: Implement explicit uncertain-result repairs**

For each uncertain response, first perform deterministic readback/auth probes. If the result remains ambiguous, stop with gateway loopback-only and preserve all local evidence. A repair plan contains only named missing/revocation actions and a fresh plan hash.

An invitation older than seven days with no created runtime user cannot be proven through the available formal API. The tool must require a new planned runtime email or a separately reviewed CMMS API enhancement; it must not blindly reinvite.

For an API Key label found after lost response, search within the current company, require exact user/label ownership, and plan deletion by that ID. Since `GET/DELETE /api-keys/{id}` lacks a company ownership check, never use an ID not obtained and verified through company-scoped search in the same plan.

If compensation stopped the API and neither the receipt nor current read-only
discovery contains the required live target ID, the planner must not guess or
authorize a label-based delete. Its repair body contains only targeted
`repair.capture-bootstrap-discovery` followed by
`repair.stop-loopback-runtime`: restore the proven existing API in loopback
mode, authenticate through explicit slots, perform formal API reads,
atomically record the discovered IDs/evidence under that plan hash, then stop
and clear the runtime before every CMMS write and before dual gateway. A later
`plan` invocation may bind the exact discovered ID only from that proven
stopped state and must produce a new hash for the mutating repair.

- [ ] **Step 9: Run the bootstrap slice tests**

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  e2e/test_cmms_bootstrap_state_machine.py \
  e2e/test_cmms_api_and_credentials.py \
  e2e/test_cmms_deployment_records.py -v
```

Expected: all failure-injection tests pass; no real CMMS write occurs.

- [ ] **Step 10: Run slice verification**

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  e2e/test_cmms_bootstrap_state_machine.py \
  e2e/test_cmms_api_and_credentials.py \
  e2e/test_cmms_deployment_records.py -v
git diff --check
./scripts/doctor.sh
```

Expected: tests pass and doctor reports zero failures.

- [ ] **Step 11: Commit the bootstrap slice**

Run:

```bash
set -Eeuo pipefail
git add \
  deploy/cmms/src/ifactory_cmms_deploy/bootstrap.py \
  deploy/cmms/src/ifactory_cmms_deploy/records.py \
  deploy/cmms/src/ifactory_cmms_deploy/cmms_api.py \
  deploy/cmms/src/ifactory_cmms_deploy/config.py \
  tests/e2e/test_cmms_bootstrap_state_machine.py
git commit -m "feat: add resumable CMMS bootstrap state machine"
```

Expected: the commit contains only bootstrap control code and offline tests; no live CMMS call occurred.

### Task 9: Compose the Fail-Closed Lifecycle and Complete the Public CLI

**Files:**

- Create: `deploy/cmms/src/ifactory_cmms_deploy/preflight.py`
- Create: `deploy/cmms/src/ifactory_cmms_deploy/planning.py`
- Create: `deploy/cmms/src/ifactory_cmms_deploy/lifecycle.py`
- Create: `tests/e2e/test_cmms_lifecycle.py`
- Modify: `deploy/cmms/src/ifactory_cmms_deploy/cli.py`
- Modify: `deploy/cmms/src/ifactory_cmms_deploy/compose.py`
- Modify: `deploy/cmms/src/ifactory_cmms_deploy/gateway.py`
- Modify: `deploy/cmms/src/ifactory_cmms_deploy/systemd.py`
- Modify: `deploy/cmms/src/ifactory_cmms_deploy/license.py`
- Modify: `deploy/cmms/src/ifactory_cmms_deploy/bootstrap.py`

**Interfaces:**

- Produces: `PlanningRequest(operation, profile, license_mode)`
- Produces: `Preflight.discover(request: PlanningRequest, snapshot: DeploymentSnapshot, current_state: StateRecord | None) -> PreflightAssessment`
- Produces: `PlanningAssessment(preflight, bootstrap, bootstrap_bindings, target_bindings, projected_bootstrap_state, phase2_seed_receipt_absent, evidence_codes)`
- Produces: `PlanDiscovery.discover(request, snapshot, current_state, preflight, slots: CredentialSlots | None, invitation_probe: InvitationProbeSlot | None) -> PlanningAssessment`
- Produces: `OperationPlanner.plan(assessment: PlanningAssessment) -> Sequence[PlannedAction]` as an immutable tuple
- Produces: `PlanBuilder.build(request: PlanningRequest, snapshot: DeploymentSnapshot, current_state: StateRecord | None, slots: CredentialSlots | None, invitation_probe: InvitationProbeSlot | None, now, plan_nonce) -> DeploymentPlan`
- Produces: `Preflight.inspect(plan: DeploymentPlan, current_state: StateRecord | None) -> PreflightReport`
- Produces: `PreflightReport.require_exact(plan: DeploymentPlan, expected_actions: Sequence[PlannedAction]) -> None`; the report owns the rebuilt `PlanningAssessment` used for profile/license/binding comparison
- Produces: immutable `PreflightReport`, `ApplyResult` and `StatusReport`
- Produces: `IndeterminateCommitError` and immutable `CompensationResult(known_safe, safe_codes)`
- Consumes: Task 2 `claim_plan_application(plan, plans_dir, lease, fail_closed) -> ClaimedApplyContext` as the sole production post-claim capability path; all four arguments must carry the same live reservation/plan/lease/gateway generation
- Produces: immutable `ApiLoopbackEvidence`, `LoopbackReadinessEvidence` and `LifecycleReadinessEvidence(safe_codes, acceptance_receipt)`
- Produces: `ReadinessPort` protocol plus a conservative `UnavailableReadinessPort`; Task 10 supplies the live-capable implementation
- Produces: `LifecycleDependencies(plans_dir, gateway, preflight, planner, compose, systemd, license, bootstrap, readiness, records)`
- Produces: `Lifecycle.apply(lease: DeploymentWriteLease, confirmed_plan: ConfirmedDeploymentPlan) -> ApplyResult`
- Produces: `Lifecycle.status() -> StatusReport`
- Produces: lifecycle-owned transition from `FINAL_PERMISSIONS_VERIFIED` to `GATEWAY_ENABLED` only after readiness and dual-listener evidence
- Produces: ordered handlers for all seven `Operation` values
- Produces for tests only: `LifecycleFixture`
- Consumes: prior modules only through explicit objects; no global mutable runner or ambient env

- [ ] **Step 1: Write failing lifecycle ordering and CLI tests**

Include the exact first-effect assertion for every operation:

```python
@pytest.mark.parametrize("operation", tuple(Operation))
def test_first_service_effect_is_fail_closed(
    lifecycle_fixture: LifecycleFixture,
    operation: Operation,
) -> None:
    lifecycle = lifecycle_fixture.successful_lifecycle()
    plan = lifecycle_fixture.confirmed_plan(operation)

    lifecycle.apply(lifecycle_fixture.deployment_write_lease, plan)

    assert lifecycle_fixture.effects[0] == "gateway.fail_closed"


def test_preflight_failure_never_creates_api_permit(
    lifecycle_fixture: LifecycleFixture,
) -> None:
    lifecycle = lifecycle_fixture.lifecycle_failing_at("preflight.inspect")
    plan = lifecycle_fixture.confirmed_plan(Operation.START)

    with pytest.raises(DeploymentError):
        lifecycle.apply(lifecycle_fixture.deployment_write_lease, plan)

    assert lifecycle_fixture.effects[0] == "gateway.fail_closed"
    assert "start-gate.create-permit" not in lifecycle_fixture.effects
    assert lifecycle_fixture.external_gateway_is_absent()
```

Tests must assert:

- public `plan` calls read-only discovery before `DeploymentPlan.create`, produces the ordered targeted actions from `OperationPlanner`, and never requires a partially constructed plan;
- public `plan` performs zero HTTP/authentication calls and therefore cannot update CMMS `lastLogin`/`lastUsed`; a fake transport sentinel must remain untouched for every operation;
- public construction of `ClaimedApplyContext` fails; bootstrap reconciliation with only `DeploymentPlan`/`ConfirmedDeploymentPlan`, a wrong inspection row or an unclaimed application is rejected before HTTP;
- the canonical plan contains the exact `BootstrapPlanBindings` projection supplied by `PlanningAssessment`; changing/omitting any action-required binding invalidates its hash or fails registry validation before write;
- repeating discovery over unchanged evidence produces byte-identical action/binding projections; with the same injected clock/nonce the canonical hash is identical, while every public regeneration uses a fresh nonce/new hash and any changed finding, target or credential-slot stat also changes/refuses the plan before apply;
- one immutable `PlanningAssessment` always produces one canonically ranked
  tuple; callers cannot inject a serialized branch or arbitrary actions, and
  tests exercise active/stopped start, all four high-level repair forms and
  completion/readiness-only/revoke-only/discard-only bootstrap subgrammars
  through their distinguishing evidence;
- fresh bootstrap planning works without a live CMMS API and binds semantic identity/role/key targets rather than fabricated company/user/settings/role IDs;
- repair planning returns mutating targeted actions only when every required live target is bound by an earlier confirmed discovery receipt; otherwise its only repair body is targeted `repair.capture-bootstrap-discovery` followed by `repair.stop-loopback-runtime`, with no CMMS write or dual gateway;
- `stop` builds from exact local ownership with `slots=None` and `invitation_probe=None`, even when bootstrap env/credential files are corrupt; bootstrap/identity repair requiring signin rejects missing `CredentialSlots`, while fresh bootstrap, an action tuple containing `BOOTSTRAP_PROBE_INVITATION`, or discovery of a receipt-bound unresolved probe requires the exact `InvitationProbeSlot`;
- a receipt with the invitation probe already resolved/cleaned and a later lost API-Key response can plan the exact targeted revoke with `invitation_probe=None`;
- unresolved-probe discovery rejects an omitted/substituted old slot before HTTP, never resends signup, and only after terminal zero-user reconciliation may a later plan bind a new slot;
- every bootstrap/repair receipt change is receipt-first then CAS-anchored into `StateRecord`; crash injection before/after each receipt, state and application write leaves planning `UNKNOWN` and permits only a new discovery/reconciliation repair;
- `repair.finalize-api-key-capture-cleanup` is emitted only for an anchored publish/revoke with cleanup pending and an observed capture equal to the historical exact stat or absent; its only plan-visible rows are fail-close then targeted cleanup, while claim and local preflight occur as implicit barriers between them; present-file tests require descriptor-bound unlink, directory fsync and proven absence, absent-file tests require continued absence, and both contain no startup/license/permit/API/readiness/dual/raw-unwrap action and exact-compare the Phase 2 stat when applicable before their sole receipt/State update;
- acceptance-profile fresh bootstrap, bootstrap-mutation completion and
  completed-publication readiness-only repair plan exactly one MinIO probe only
  when the Phase 2 seed receipt is absent, the State-selected/projected state
  is `FINAL_PERMISSIONS_VERIFIED` or `GATEWAY_ENABLED` and the complete composite
  pre-open/dual/post-open rows are present; an ineligible acceptance bootstrap
  is rejected, while development bootstrap and discovery/revoke-only/
  discard-only/budget-recovery/cleanup repair contain no acceptance action or
  receipt path;
- readiness-only planning is rejected unless every conjunct of Task 2's
  completed-publication predicate reopens exactly, and tests cover publish
  cleanup completed before readiness, cleanup-only followed by readiness-only,
  crash after each pre-open/dual/State/receipt/application boundary, no
  bootstrap mutation or second publish, and rejection of an already terminal
  healthy deployment in favor of ordinary status/start;
- preflight reports exact port conflicts without killing or replacing a process;
- fresh bootstrap may proceed with missing toolchains/images/services only when the confirmed action list contains each exact install/pull/create action;
- start/restart rejects the same missing prerequisite instead of silently adding an action;
- stop ignores corrupt application secrets after proving exact managed unit/Compose ownership, so it can still stop those targets while preserving volumes;
- `cmms.localhost` must resolve only to `127.0.0.1` and/or `::1`, and both are supported by loopback gateway mode;
- host architecture is `x86_64`, Docker socket/Compose v2 and user-systemd are available, while unrelated degraded units are reported only;
- a confirmed plan can be consumed once and only by its matching operation handler;
- two different valid plan hashes racing in separate processes yield exactly one lease owner; the loser writes/reopens only its own `CONTENDED` application tombstone, has zero gateway/HTTP/receipt/State/service calls, and the owner's lease remains held through compensation plus terminal update;
- every valid apply hash is first consumed by an `ATTEMPTED` reservation; contention, pre-claim crash/rejection and claimed crash therefore make that exact hash non-reusable even if the owner's State generation/SHA never changed;
- after gateway fail-close is proven, the application claim is durably `IN_PROGRESS` before any other effect, and a crash consumes that plan so recovery requires a newly inspected plan;
- the first service-affecting event for every apply is `gateway.fail_closed`;
- no API permit is created before source/toolchain/config/Compose/unit/license/budget evidence passes;
- fresh bootstrap `process.start-api` refuses a plan missing the later `cmms.initialize-fresh-database` authorization row; the row is dispatched after API start only to verify the initializer side effect, performs zero new write and must pass before frontend start/API-loopback readiness;
- no dual gateway occurs before API PID/readiness, identity reconciliation and operation-specific checks pass;
- no receipt reaches `GATEWAY_ENABLED` unless dual gateway activation and its listener/path proof both succeeded;
- `start`, `restart-api`, `restart-frontend`, `stop` and `switch-license` contain zero signup/password/invite/role/API-Key writes;
- only `bootstrap` and explicit `repair` may call those write methods, and only actions listed in the plan;
- cold start with stale receipt/replaced database stops before dual gateway;
- `start` with an already healthy, exactly bound API preserves the same MainPID/start ticks, creates no permit, performs no `systemctl start` for API and consumes zero online budget; stopped `start` alone may debit/create/consume one permit;
- an active-but-mismatched API, an orphan `active.json`/`consuming.json` permit, or unverifiable historical online debit is not treated as idempotent and stops for repair rather than silently restarting;
- `restart-api` closes gateway, stops API, rebuilds current source, performs budget/guard checks, creates one permit and produces exactly one new MainPID;
- `restart-frontend` closes gateway, restarts only frontend, rechecks current API/identity, then reopens; ordinary source edits rely on HMR and need no CLI action;
- `stop` closes/stops gateway first, stops frontend/API/guard, then runs Compose `stop`, preserving volumes and current credential slots;
- any injected failure at each event leaves gateway loopback-only or stopped and produces no automatic API retry;
- any bootstrap/repair failure additionally stops API, frontend, guard/timer and Nginx, proves them inactive/listener-free, and preserves PostgreSQL/MinIO plus all recovery evidence;
- `status` is read-only, redacted and separately reports Compose services, units, listeners, HTTP, source status, license mode/guard/budget and bootstrap state;
- `apply` rejects confirmation from an environment variable or state file; only the exact CLI argument is accepted.

- [ ] **Step 2: Run the lifecycle tests and verify the expected failure**

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  e2e/test_cmms_lifecycle.py -v
```

Expected: FAIL because preflight/lifecycle are absent.

- [ ] **Step 3: Implement read-only preflight**

Inspect without mutation:

```text
root/CMMS git state and sensitive manifest
fixed toolchain install state
runtime env/secret metadata
ports 3000, 3001, 5433, 8082, 9000, 9001
cmms.localhost resolution
/var/run/docker.sock and Docker Compose v2
default bridge gateway and local interface ownership
user-systemd accessibility and CMMS unit properties
Compose project/volume/container state
gateway config/listeners
license mode/guard/budget continuity
current MainPID, /proc start ticks/cwd/cmdline and API socket ownership
bootstrap receipt integrity and locally owned state; no formal API authentication
```

For a stopped first deployment, fixed ports must be unused. For an idempotent managed start/status, an occupied port is accepted only when it is proven to belong to the expected Compose container or exact user-unit MainPID from current state. Never use `pkill`, `kill`, container replacement or an inferred owner.

Classify every finding as fatal, satisfiable-by-exact-action, or irrelevant-to-operation. `bootstrap` may accept a satisfiable missing artifact only if its exact targeted action is already present in the confirmed plan. `start` and both restart operations require their prerequisites present. `stop` requires only exact target ownership plus usable Docker/systemd controls; its plan path does not load the bootstrap env, `CredentialSlots` or `InvitationProbeSlot`, so broken runtime/bootstrap secrets, toolchains or readiness do not prevent shutdown of proven CMMS targets. No operation may turn a newly discovered finding into an unconfirmed action.

The public `plan` command uses this acyclic pipeline:

```text
PlanningRequest + immutable config/source/slot snapshots + current state
  -> Preflight.discover(request, snapshot, state)    # read-only
  -> PlanDiscovery.discover(
       request, snapshot, state, preflight, slots, invitation_probe
     )                                               # local/receipt only; zero HTTP
  -> immutable PlanningAssessment
  -> OperationPlanner.plan(assessment)               # exact PlannedAction tuple
  -> ActionRegistry.validate(
       operation, profile, license_mode, bootstrap_bindings, actions
     )
  -> DeploymentPlan.create(
       snapshot, operation, profile, license_mode,
       bootstrap_bindings, actions, now, fresh_plan_nonce
     )
  -> canonical write + SHA-256 + stop
```

`PlanDiscovery` never opens HTTP and never calls `BootstrapReconciler.inspect`.
It consumes the exact immutable `PreflightAssessment` plus local
config/descriptor snapshots; when applicable it loads only the immutable
bootstrap receipt generation selected by the current State pointer and calls
`BootstrapAssessment.from_receipt`. It projects the typed slots/probe plus
action-dependent `ApiKeyCapturePlanBinding` or `ApiKeyCleanupPlanBinding` into
`PlanningAssessment.bootstrap_bindings`, and `PlanBuilder` passes that exact
value to `DeploymentPlan.create`; none is left as transient planner state.
When preflight proves the Compose project/owned volume is genuinely
absent/new, it creates the well-known fresh assessment from the canonical
identity/role/key/probe targets and reviewed initializer manifest; this path
requires no fabricated live ID and both API-Key file bindings are null.
Bootstrap/identity repair that will authenticate requires explicit
`CredentialSlots`. `InvitationProbeSlot` is required for fresh bootstrap, for
an action tuple containing `BOOTSTRAP_PROBE_INVITATION`, and for
discovery-only repair of a receipt-bound unresolved probe, where it must be the
exact old slot and cannot authorize another signup. After a resolved or
terminally retired probe has been safely deleted, an unrelated API-Key or
candidate repair accepts `invitation_probe=None`; only a later new-probe plan
binds a new slot. `STOP` requires both typed inputs and `bootstrap_bindings` to
be `None`. If a required live deletion/revocation target is absent from the
confirmed-discovery receipt, the only legal output is the exact startup plus
targeted `repair.capture-bootstrap-discovery` and final
`repair.stop-loopback-runtime` sequence.

`OperationPlanner` is the sole contextual branch selector. It starts with the
exact required row set from Task 2's branch-cardinality table, unions every
branch-allowed `(code,target)` whose immutable finding is exactly
`SATISFIABLE_BY_ACTION`, rejects every required finding outside that allowlist,
then sorts once by the frozen rank and same-code target key. It never chooses
between alternatives. For one immutable `PlanningAssessment`, these inclusion
rules produce exactly one action tuple. The private Task 2 classifier
recognizes active/stopped start; discovery,
completion/readiness-only/revoke-only/discard-only bootstrap repair;
budget-recovery; and capture-cleanup repair. No branch value enters the plan
schema. Active process state, owned-volume
freshness, State-selected receipt facts and projected bootstrap state are
inputs to the assessment, not to `ActionRegistry`. Callers may request an
operation/profile/license mode but cannot supply a branch or arbitrary action
tuple.

For fresh `BOOTSTRAP`, `REPAIR_BOOTSTRAP_MUTATION_COMPLETION` and
`REPAIR_BOOTSTRAP_READINESS_ONLY`, the planner includes exactly one MinIO route
probe iff profile is `ACCEPTANCE`, the Phase 2 seed receipt is absent, the
State-selected/projected bootstrap state is
`FINAL_PERMISSIONS_VERIFIED` or `GATEWAY_ENABLED`, and the tuple contains the
complete composite
pre-open, dual-enable and post-open sequence. Readiness-only additionally must
satisfy every conjunct of Task 2's completed-publication predicate. A fresh
acceptance bootstrap that does not satisfy all four common conditions is
rejected; it is not silently planned as operational. Development bootstrap
and discovery/revoke-only/discard-only/budget-recovery/capture-cleanup repair
contain no acceptance action or receipt path. Task 10 executes only a probe
row already produced here and cannot add one.

Only that separately confirmed discovery-only apply may invoke
`BootstrapReconciler.inspect` for unrestricted target discovery: after
fail-close, application claim, local preflight replay, exact startup actions
and API-loopback proof, its handler signs in, performs formal reads, records
the safe IDs/evidence, State-anchors them, executes the planned
`repair.stop-loopback-runtime` row and proves the API/guard runtime stopped
before returning. It performs no CMMS write or dual gateway. The next public
plan consumes that receipt without HTTP and emits a new hash from a proven
stopped state.

`Preflight.inspect` is the apply-time local/receipt replay of the same zero-HTTP
discovery rules after fail-close and application claim. It receives the
completed immutable plan only to validate its bound snapshots, never to
synthesize actions. It recomputes the current `PlanningAssessment` and reruns
`OperationPlanner`. The resulting `PreflightReport` owns that rebuilt
assessment; `require_exact(plan, expected_actions)` exact-compares actions,
targets, request operation/profile/license, bootstrap bindings and the
assessment-derived acceptance predicates. It rejects a missing, extra,
reordered or target-substituted row before toolchain installation, Compose,
systemd, budget debit, permit or HTTP. The always-first fail-close row is
normalized in both planning and replay so the deliberate gateway state change
does not create false drift. For cleanup-only repair, claim and this local
replay are implicit barriers between its two plan-visible rows. Operation
handlers perform any plan-authorized formal API authentication only after this
exact-action gate.

`FailClosedEvidence` binds the verified gateway generation/address/listener result and is not serializable. Lifecycle passes that evidence, the still-current `ConfirmedDeploymentPlan`, the fixed plans directory and the same live `DeploymentWriteLease` into Task 2's four-argument `claim_plan_application()` API; the function verifies the plan's embedded reservation/lease identity, transitions/fsyncs/reopens `IN_PROGRESS` and directly returns the bound context. There is no second lifecycle mint. The context exposes its application handle and an action-checking method but not its private token. All handler, BootstrapReconciler, bootstrap applier, license debit, permit, Compose/systemd and receipt-mutation adapters receive this context or a still narrower capability derived from it. Public plan/status code has no importable constructor path.

- [ ] **Step 4: Implement the common fail-closed apply skeleton**

Use one dispatch table so an unknown or mismatched operation cannot fall through:

```python
class Lifecycle:
    def apply(
        self,
        lease: DeploymentWriteLease,
        confirmed_plan: ConfirmedDeploymentPlan,
    ) -> ApplyResult:
        lease.require_live_for(confirmed_plan)
        gateway_evidence = self.dependencies.gateway.begin_fail_closed(
            confirmed_plan,
            "APPLY_START",
        )
        fail_closed = (
            self.dependencies.gateway.require_external_listener_absent(
                gateway_evidence
            )
        )
        context = self.dependencies.records.claim_plan_application(
            confirmed_plan,
            self.dependencies.plans_dir,
            lease,
            fail_closed,
        )
        application = context.application
        try:
            preflight = self.dependencies.preflight.inspect(
                confirmed_plan.plan,
                self.dependencies.records.read_state(),
            )
            expected_actions = self.dependencies.planner.plan(
                preflight.planning_assessment
            )
            preflight.require_exact(
                confirmed_plan.plan,
                expected_actions,
            )
            handlers = {
                Operation.BOOTSTRAP: self._bootstrap,
                Operation.START: self._start,
                Operation.RESTART_API: self._restart_api,
                Operation.RESTART_FRONTEND: self._restart_frontend,
                Operation.STOP: self._stop,
                Operation.REPAIR: self._repair,
                Operation.SWITCH_LICENSE: self._switch_license,
            }
            result = handlers[confirmed_plan.plan.operation](context)
            self.dependencies.records.mark_plan_terminal(
                application,
                PlanApplicationState.SUCCEEDED,
            )
            return result
        except IndeterminateCommitError:
            self._compensate_failure(context)
            # Durable outcome is unknown: leave application IN_PROGRESS.
            raise
        except BaseException:
            compensation = self._compensate_failure(context)
            if not compensation.known_safe:
                # Compensation or its proof is unknown: leave IN_PROGRESS.
                raise IndeterminateCommitError(
                    "CMMS-E099",
                    "deployment outcome requires reconciliation",
                    99,
                ) from None
            self.dependencies.records.mark_plan_terminal(
                application,
                PlanApplicationState.FAILED,
            )
            raise
```

`_compensate_failure()` attempts every safe exact-target cleanup even if an earlier cleanup fails, returns `CompensationResult`, and never retries the failed action. It calls `Gateway.fail_closed_claimed(context, "APPLY_FAILED")`; it never reuses `begin_fail_closed` or creates another claim evidence token. For bootstrap/repair it then stops the exact API/frontend/guard/timer units, stops only the Nginx Compose service, and proves those units/listeners absent; PostgreSQL/MinIO and all evidence remain. Other operations at minimum fail-close and prove the external listener absent. An indeterminate remote result, receipt/State CAS boundary, terminal-record write, or compensation proof always leaves the application `IN_PROGRESS`; only a fully known pre-effect/handled failure with known-safe compensation becomes `FAILED`.

Public `apply` uses:

```text
1. Statically validate the exact plan/hash/expiry and durably reserve it as ATTEMPTED; any later outcome consumes the hash.
2. Acquire the nonblocking apply-effect lease; contention records only CONTENDED and returns before service effects.
3. Under the lease, capture current snapshot/bindings and mint the exact confirmed-plan capability.
4. Fail-close gateway; prove Docker gateway listener absent.
5. Atomically transition the reserved application to IN_PROGRESS; a claimed plan is never reusable.
6. Run read-only preflight against current state.
7. Execute only the ordered targeted `PlannedAction` rows in the plan; dispatch receives the complete row, not a naked code.
8. After every state transition, write a secret-free canonical result record.
9. On any exception, fail-close again and stop without retrying a non-idempotent write.
10. For bootstrap/repair, also stop the exact API/frontend/guard/timer/Nginx targets and prove inactive while preserving PostgreSQL, MinIO, credentials, attempts and receipts.
11. Mark the local application `SUCCEEDED`, or `FAILED` only for a known failure with fully proven safe compensation, exactly once; an indeterminate commit/compensation/terminal write or crash leaves `IN_PROGRESS` and forces a new reconciliation plan.
```

If initial fail-close cannot safely reload, stop Nginx. Failure to prove the gateway listener absent aborts before every other state action.

At Task 9, lifecycle depends only on the `ReadinessPort` protocol. Offline tests inject a scripted implementation; the public CLI wires `UnavailableReadinessPort`, which always leaves gateway fail-closed with `CMMS-E095 readiness-not-implemented`. Task 10 replaces that binding with `LifecycleReadinessAdapter` backed by `ReadinessChecker`; Task 9 must not fabricate a passing readiness result.

```python
class ReadinessPort(Protocol):
    def require_api_loopback(
        self,
        context: ClaimedApplyContext,
        state: StateRecord,
    ) -> ApiLoopbackEvidence:
        raise NotImplementedError

    def require_loopback(
        self,
        context: ClaimedApplyContext,
        state: StateRecord,
    ) -> LoopbackReadinessEvidence:
        raise NotImplementedError

    def require_dual(
        self,
        context: ClaimedApplyContext,
        state: StateRecord,
        preopen: LoopbackReadinessEvidence,
    ) -> LifecycleReadinessEvidence:
        raise NotImplementedError


class UnavailableReadinessPort:
    def require_api_loopback(
        self,
        context: ClaimedApplyContext,
        state: StateRecord,
    ) -> ApiLoopbackEvidence:
        raise DeploymentError(
            "CMMS-E095",
            "readiness implementation is not available",
            95,
        )

    def require_loopback(
        self,
        context: ClaimedApplyContext,
        state: StateRecord,
    ) -> LoopbackReadinessEvidence:
        raise DeploymentError(
            "CMMS-E095",
            "readiness implementation is not available",
            95,
        )

    def require_dual(
        self,
        context: ClaimedApplyContext,
        state: StateRecord,
        preopen: LoopbackReadinessEvidence,
    ) -> LifecycleReadinessEvidence:
        return self.require_loopback(context, state)
```

Fresh bootstrap and the two repair classes that perform discovery/bootstrap
API work call `require_api_loopback()` immediately before their first formal
read or write. It requires the exact planned
`readiness.require-api-loopback` row, binds the current API PID/start ticks,
socket owner, fixed loopback gateway generation, `/api/health-check` result and
license-mode barrier, and performs no authentication or domain query. A PID,
socket or gateway-generation change invalidates the evidence and aborts before
HTTP. It is never reused as composite pre-open evidence.

Each start-capable handler keeps the returned pre-open evidence as a local value, enables dual gateway, passes that exact value into `require_dual()`, and persists `acceptance_receipt` only when the returned evidence contains one. Task 9 scripted ports always return `acceptance_receipt=None`; Task 10 constructs the typed receipt. Lifecycle—not the readiness adapter—owns the sole atomic receipt write, so no hidden cache or partial readiness result can become durable evidence.

- [ ] **Step 5: Implement bootstrap and start operation sequences**

`bootstrap`:

```text
fail-close -> preflight -> install control runtime/toolchains ->
explicit pinned image pull if planned -> Compose up PostgreSQL/MinIO/Nginx(loopback) ->
build API/npm ci ->
render/link units -> license capability/budget ->
offline: verify license file, network-policy capability and loaded API/timer relationships ->
online: durably debit the controlled-start budget ->
permit -> start API (offline dependency activates timer before start-gate) ->
verify the plan-authorized ApplicationInitializer startup result ->
start frontend -> API active -> offline full guard ->
API-loopback prerequisite (PID/socket/fixed health; no domain auth) ->
bootstrap state machine -> composite pre-open readiness ->
dual gateway -> post-open dual listeners/paths ->
atomically record GATEWAY_ENABLED; acceptance receipt only for a confirmed
acceptance tuple containing the already planned MinIO probe
```

`start` first selects exactly one of two branches after fail-close and preflight:

```text
managed API already active:
  prove current MainPID + start ticks + socket owner + source/root fingerprints +
  artifact + controller entrypoint/package + loaded unit generation + license mode +
  guard or historical online debit anchor all match current state;
  require no active/consuming permit -> zero debit -> zero permit ->
  do not invoke systemctl start/restart for API -> retain the same MainPID

managed API stopped:
  Compose start -> verify existing build/dependencies -> render/verify units ->
  offline: verify license file, network-policy capability and loaded API/timer relationships;
  online: durably debit the controlled-start budget ->
  create permit -> start API (offline dependency activates timer before start-gate) ->
  require one new MainPID and consumed permit

both branches:
  start/verify frontend -> offline full guard when applicable ->
  read-only bootstrap reconciliation -> pre-open readiness ->
  dual gateway -> post-open dual listeners/paths ->
  atomically retain/record GATEWAY_ENABLED
```

The active branch is the approved idempotent `start`: it never spends online budget merely to re-open a verified gateway and cannot leave a newly minted permit behind while systemd short-circuits an already-active unit. If any active-process binding is uncertain, `start` fails; only an explicit `restart-api` plan may stop it and create a new MainPID. Tests assert zero budget/permit/API-systemctl effects, identical PID/start ticks and absence of `start-permits/active.json` for the active branch, while the stopped branch consumes exactly one debit in online mode and exactly one permit in either mode.

The offline timer cannot perform its full API check while no API MainPID exists. Before permit creation, validate the license file, network-policy capability, rendered/loaded API-to-timer dependency and timer definition. `systemctl --user start ifactory-cmms-api.service` then activates the bound timer in the same transaction; `start-gate` proves it active before Java executes. After the API becomes active, require a successful full guard execution before dual gateway. An unavailable timer, failed guard or unverifiable IP restriction leaves the gateway fail-closed.

- [ ] **Step 6: Implement restart, stop, repair, and mode-switch sequences**

`restart-api` rebuilds and starts exactly one new API MainPID; in offline mode it validates the timer/dependency before the permit, lets the API start transaction activate the timer before `ExecStartPre`, and runs the full guard after API activation. `restart-frontend` restarts only the frontend after closing the gateway. `stop` closes/stops Nginx first, stops frontend/API/guard/timer, and then uses Compose `stop`, preserving volumes and credential slots. `switch-license` closes the gateway, stops API and guard/timer, removes the old loaded profile, verifies the target unit/network/budget control, then follows the API start sequence with one new permit.

`repair` is not allowed to assume a live API after bootstrap compensation. It has four disjoint, plan-hashed forms selected deterministically from `PlanningAssessment`:

Except for capture-cleanup, every repair subgrammar requires planning and
apply-time preflight to prove the managed API stopped with no active/consuming
permit before its mandatory `L`/`N` startup bundle. An active or uncertain API
is rejected; it is never silently reused or restarted. Discovery,
revoke-only and discard-only close that bundle again with the planned `R`
action, so any later repair is also generated from proven stopped state.

```text
bootstrap target discovery:
  fail-close and claim -> verify/start PostgreSQL + MinIO + loopback Nginx ->
  verify/install the exact existing control/toolchain/build/unit prerequisites ->
  stopped-API license path -> one permit -> one API MainPID ->
  API-loopback prerequisite (PID/socket/fixed health; no domain auth) ->
  authenticate with explicit current/candidate slots -> formal API reads only ->
  execute repair.capture-bootstrap-discovery against receipt:cmms-bootstrap ->
  atomically capture safe live IDs/evidence -> CAS-anchor receipt SHA in StateRecord ->
  reopen/cross-check receipt + state ->
  execute repair.stop-loopback-runtime -> prove API/frontend/guard/timer stopped
  while PostgreSQL/MinIO and loopback Nginx remain -> halt;
  no CMMS write, API-Key delete/create, dual gateway or acceptance receipt

bootstrap/API-Key repair:
  fail-close and claim -> verify/start PostgreSQL + MinIO + loopback Nginx ->
  verify/install the exact existing control/toolchain/build/unit prerequisites ->
  stopped-API license path (offline capability, or one ordinary online debit) ->
  one permit -> one API MainPID -> exactly one of:

  contiguous completion:
    start/verify frontend ->
    API-loopback prerequisite (PID/socket/fixed health; no domain auth) ->
    formal API reconciliation -> execute only the frozen
    receipt/probe/slot-matched contiguous bootstrap suffix ending in
    Phase 2 publish ->
    final reconciliation -> composite pre-open evidence -> dual ->
    post-open evidence ->
    acceptance receipt only for acceptance profile when Phase 2 seed receipt
    is absent, projected state is FINAL_PERMISSIONS_VERIFIED and the complete
    pre-open/dual/post-open plus MinIO-probe tuple was already planned

  completed-publication readiness-only:
    require the exact State-selected PUBLISHED + capture-cleared receipt,
    current Phase 2 env stat, terminal ENFORCED probe and completion gap ->
    start/verify frontend ->
    API-loopback prerequisite (PID/socket/fixed health; no domain auth) ->
    perform formal read-only identity/API-Key/license/domain reconciliation
    with no bootstrap or repair mutation ->
    composite pre-open evidence -> dual -> post-open evidence ->
    State-anchor GATEWAY_ENABLED and, only when the confirmed acceptance tuple
    already contains its MinIO probe, persist the acceptance receipt;
    no password/signup/invitation/role/API-Key create/finalize/publish,
    discovery, revoke, discard, runtime-stop or capture-cleanup action

  revoke-only:
    API-loopback prerequisite -> company-scoped exact-ID verification ->
    execute only repair.revoke-uncaptured-api-key -> readback ->
    immutable receipt + State anchor returning to RUNTIME_IDENTITY_CREATED ->
    repair.stop-loopback-runtime -> prove runtime stopped -> halt;
    no API-Key create/publish, frontend, composite readiness, dual or acceptance

  candidate-discard-only:
    API-loopback prerequisite -> re-prove the exact candidate rejection and
    unchanged current slot -> execute only repair.discard-rejected-candidate ->
    immutable receipt + State anchor -> repair.stop-loopback-runtime ->
    prove runtime stopped -> halt;
    no other bootstrap action, frontend, composite readiness, dual or acceptance

online-budget recovery:
  fail-close and claim -> require API stopped/existing owned volume ->
  verify/start PostgreSQL + MinIO + loopback Nginx and exact build/unit prerequisites ->
  execute only license.recover-unknown-budget ->
  one recovery-bound permit -> one API MainPID -> operational readiness ->
  dual/post-open; no bootstrap identity/API-Key write

capture-cleanup finalization:
  plan-visible gateway.fail-closed ->
  implicit listener-absence proof + claim + zero-HTTP local preflight ->
  plan-visible repair.finalize-api-key-capture-cleanup
    target receipt:cmms-bootstrap ->
  exact present-or-absent cleanup -> immutable receipt generation ->
  State pointer CAS -> terminal application update;
  no Compose/systemd startup, license, permit, authentication, HTTP, raw unwrap,
  publish/revoke, readiness, dual gateway or acceptance receipt
```

Every potentially missing prerequisite must already appear under its exact action code in the repair plan—`compose.start-state-gateway`, `runtime.install-control`, `toolchain.install`, `build.api`, `frontend.verify`, `systemd.install-units`, `license.debit-online-start` or `license.recover-unknown-budget`, `process.create-api-permit`, `process.start-api`, `process.start-frontend`, and the applicable readiness/gateway actions. A finding cannot add one during apply. Online bootstrap repair uses one ordinary debit exactly once; budget recovery uses its single `consumed_slots=10` debit and cannot coexist with another debit action. Cleanup finalization is the exception that permits none of those prerequisites and has exactly its fixed two-row plan-visible tuple.

The discovery form is mandatory when an exact live target required by a future
mutation is unavailable while API is stopped. Its successful local receipt
update is evidence for planning only, never authorization: the planned runtime
stop must complete, then the operator must run `plan` again and confirm the new
mutating plan hash. If exact receipt-bound targets already exist, the planner
may omit discovery and generate the targeted bootstrap/API-Key repair
directly. Revoke-only and discard-only likewise stop after their one anchored
mutation; any subsequent create/publish/completion requires a fresh
stopped-state assessment, plan hash and confirmation.

Failure-injection tests begin from each compensation end-state (all host
units/Nginx stopped with PostgreSQL/MinIO either healthy or stopped), prove a
current API PID and loopback gateway exist before any formal API repair write,
and prove every second failure re-runs compensation while preserving evidence.
They prove discovery/revoke-only/discard-only cannot report success before the
planned runtime stop and State clearing are durable; uncertain stop leaves
`IN_PROGRESS`. They also prove repair cannot invoke an unlisted startup action,
cannot spend two online debits, cannot mix budget recovery with bootstrap
mutation, cannot mix revoke/discard with a completion suffix, and cannot attach
startup/readiness/acceptance work to cleanup-only repair. Readiness-only tests
prove exact receipt/State/application/Phase 2 stat replay, zero bootstrap
mutation and zero second publication, and require a cleanup-only plan first
whenever capture cleanup remains pending.

On a proven fresh PostgreSQL volume under a confirmed bootstrap,
`ApplicationInitializer` necessarily creates the source-defined superadmin
company/settings, `SUPER_ADMIN` role, initial invitation, default-password
superadmin user, and the `FREE`/`STARTER`/`PROFESSIONAL`/`BUSINESS`
subscription plans. The plan lists this as
`cmms.initialize-fresh-database`, binds the reviewed initializer/migration
hashes, and accepts it only when preflight proves the new owned volume; it is
not attributed to `BootstrapApplier`. `process.start-api` requires that later
authorization row before Java starts, and dispatching the row after start only
verifies the initializer result without another write. Initialization may
also create the configured empty `atlas-bucket`, update default roles, usage
counters and temporary timezone fields; sign-in/API-Key authentication may
update `lastLogin`/`lastUsed`; online license mode may update its internal
tracker. Bucket creation must not create a CMMS `File` record or object.

There is one explicit source residual: on an ordinary start/restart, if the superadmin company's users were externally deleted, current `ApplicationInitializer` can recreate the default-password superadmin before formal readiness can detect the drift. With the “no direct DB and no CMMS business-code change” boundary, the controller cannot prevent that pre-readiness write. Therefore “zero identity writes” for ordinary operations means zero identity API calls by the orchestrator, not an absolute claim about initializer behavior. Post-start reconciliation must compare receipt-bound superadmin company/user IDs and candidate/current authentication; any recreated/default credential or identity drift immediately fail-closes, stops API/frontend/Nginx, preserves evidence and requires an explicit repair/security response. It never opens dual gateway or silently rotates/deletes the recreated identity. Tests simulate this initializer drift. Assets and work orders must still remain stable across ordinary start/restart.

- [ ] **Step 7: Complete CLI dispatch and stable output**

`plan` runs only the acyclic read-only discovery/planner pipeline, double-checks the local State/receipt/config/secret stat generations did not change during discovery, then displays mode/profile, source status, ordered action code plus non-secret target, online budget projection and exact plan hash. It refuses a mutating repair when a required live target is unknown and offers only the discovery-only repair form. `apply` statically validates the path/hash/expiry, durably reserves that hash, attempts the deployment write lease, captures current state/bindings under the lease, calls `load_confirmed_plan`, and keeps the lease through lifecycle terminal/unknown handling; it displays only safe action/result codes and final state. `status --json` emits canonical secret-free JSON and reports a non-authoritative transitional/busy flag when an apply lease is held; human status uses the same projection.

Exit nonzero on `DEGRADED`, `UNKNOWN`, fail-closed or unmet readiness. Never print full env, HTTP body, journal tail, Docker inspect object, `/proc` cmdline or an absolute artifact/secret path.

- [ ] **Step 8: Run the lifecycle slice tests**

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  e2e/test_cmms_lifecycle.py \
  e2e/test_cmms_bootstrap_state_machine.py \
  e2e/test_cmms_license_modes.py \
  e2e/test_cmms_systemd_and_start_gate.py -v
```

Expected: all lifecycle tests pass with fake dependencies and no live service mutation.

- [ ] **Step 9: Run slice verification**

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  e2e/test_cmms_lifecycle.py \
  e2e/test_cmms_bootstrap_state_machine.py \
  e2e/test_cmms_license_modes.py \
  e2e/test_cmms_systemd_and_start_gate.py -v
git diff --check
./scripts/doctor.sh
```

Expected: tests pass and doctor reports zero failures.

- [ ] **Step 10: Commit lifecycle orchestration**

Run:

```bash
set -Eeuo pipefail
git add \
  deploy/cmms/src/ifactory_cmms_deploy/preflight.py \
  deploy/cmms/src/ifactory_cmms_deploy/planning.py \
  deploy/cmms/src/ifactory_cmms_deploy/lifecycle.py \
  deploy/cmms/src/ifactory_cmms_deploy/cli.py \
  deploy/cmms/src/ifactory_cmms_deploy/compose.py \
  deploy/cmms/src/ifactory_cmms_deploy/gateway.py \
  deploy/cmms/src/ifactory_cmms_deploy/systemd.py \
  deploy/cmms/src/ifactory_cmms_deploy/license.py \
  deploy/cmms/src/ifactory_cmms_deploy/bootstrap.py \
  tests/e2e/test_cmms_lifecycle.py
git commit -m "feat: orchestrate CMMS development lifecycle"
```

Expected: the commit contains orchestration and offline tests only; no live listener, unit, container or volume changed.

### Task 10: Add Composite Readiness, Signed MinIO Routing, and the Opt-In Live Gate

**Files:**

- Create: `deploy/cmms/src/ifactory_cmms_deploy/readiness.py`
- Create: `tests/e2e/test_cmms_readiness.py`
- Create: `tests/e2e/test_cmms_deployment_runtime.py`
- Modify: `deploy/cmms/src/ifactory_cmms_deploy/lifecycle.py`
- Modify: `deploy/cmms/src/ifactory_cmms_deploy/records.py`
- Modify: `deploy/cmms/src/ifactory_cmms_deploy/cli.py`
- Modify: `tests/e2e/conftest.py`
- Modify: `tests/pyproject.toml`
- Modify: `tests/uv.lock`

**Interfaces:**

- Produces: `ReadinessReport(ok: bool, check_codes: Sequence[str], failure_codes: Sequence[str], evidence: Mapping[str, JsonValue])`
- Produces: `MinioProbeResult(ok: bool, check_codes: Sequence[str], cleanup_code: str)`
- Produces: enum `ReadinessIntent` with `BOOTSTRAP_ACCEPTANCE` and `OPERATIONAL`
- Produces: immutable `ExpectedRuntime`, `AdministrativeReadinessCredentials`, `MinioCredentials` and `ReadinessCheck`
- Produces: secret-bearing `PresignedUrl` with redacted `repr`/`str` and transport-only unwrapping
- Produces: `ReadinessChecker.check_api_loopback(expected: ExpectedRuntime, api: CmmsApi, runner: CommandRunner) -> ReadinessReport`, restricted to the fixed unauthenticated health call and no CMMS domain checks
- Produces: `ReadinessChecker.check_preopen(expected: ExpectedRuntime, api: CmmsApi, runner: CommandRunner, credentials: AdministrativeReadinessCredentials, intent: ReadinessIntent) -> ReadinessReport`
- Produces: `ReadinessChecker.check_postopen(expected: ExpectedRuntime, api: CmmsApi, runner: CommandRunner, api_key: RawApiKey, intent: ReadinessIntent) -> ReadinessReport`
- Produces: `ReadinessChecker.check_live_acceptance(expected: ExpectedRuntime, api: CmmsApi, runner: CommandRunner, api_key: RawApiKey) -> ReadinessReport`
- Produces: `LifecycleReadinessAdapter(ReadinessPort)` implementing `require_api_loopback`, `require_loopback` and `require_dual`
- Produces: `MinioProbe.run(expected: ExpectedRuntime, credentials: MinioCredentials) -> MinioProbeResult`
- Produces: typed `AcceptanceReceipt` constrained by Task 2's fixed structural schema
- Produces: `AcceptanceReceipt.from_readiness(context: ClaimedApplyContext, preopen: ReadinessReport, postopen: ReadinessReport, minio: MinioProbeResult) -> AcceptanceReceipt`
- Persists: `.runtime/receipts/cmms-development.json`
- Produces for tests only: `ReadinessFixture` and `MinioProbeFixture`
- Consumes in lifecycle only: current administrator slots for pre-open reconciliation
- Consumes in live acceptance: confirmed deployment receipt and runtime API Key only; never bootstrap identity or MinIO credentials

- [ ] **Step 1: Write failing composite-readiness tests**

Include PID-reuse and SigV4 origin-rewrite tests:

```python
def test_readiness_rejects_reused_api_pid(
    readiness_fixture: ReadinessFixture,
) -> None:
    expected = readiness_fixture.expected_runtime(api_main_pid=4127)
    readiness_fixture.proc_stat(
        pid=4127,
        start_ticks=expected.api_process_start_ticks + 1,
    )

    report = ReadinessChecker().check_preopen(
        expected,
        readiness_fixture.api,
        readiness_fixture.runner,
        credentials=readiness_fixture.administrative_credentials,
        intent=ReadinessIntent.OPERATIONAL,
    )

    assert report.ok is False
    assert "API_PID_REUSED" in report.failure_codes


def test_public_minio_url_is_redacted_and_preserves_signed_internal_host(
    minio_fixture: MinioProbeFixture,
) -> None:
    signed = minio_fixture.sign_get(
        internal_origin="http://127.0.0.1:9000",
        bucket="atlas-bucket",
        object_key=".ifactory-readiness/probe-001",
    )
    assert minio_fixture.last_canonical_headers == (
        "host:127.0.0.1:9000\n",
    )

    public: PresignedUrl = rewrite_signed_minio_origin(
        signed,
        public_origin="http://cmms.localhost:3000/storage",
    )

    assert str(public) == "<redacted-presigned-url>"
    assert repr(public) == "PresignedUrl(<redacted>)"
    assert public.safe_origin == "http://cmms.localhost:3000"
    assert public.safe_path == (
        "/storage/atlas-bucket/.ifactory-readiness/probe-001"
    )
    assert public.safe_query_names == (
        "X-Amz-Algorithm",
        "X-Amz-Credential",
        "X-Amz-Date",
        "X-Amz-Expires",
        "X-Amz-Signature",
        "X-Amz-SignedHeaders",
    )
```

`check_api_loopback()` is a separate pre-bootstrap barrier. It checks only the
State-bound API unit/MainPID/start ticks, ownership of `127.0.0.1:8082`, the
fixed loopback-only gateway generation, the exact unauthenticated
`/api/health-check` projection and the mode-appropriate license guard/budget
anchor. It receives no credentials, and tests prove the supplied `CmmsApi`
records only that health call and zero authentication/company/user/role/
API-Key/asset/work-order calls. It cannot produce an acceptance receipt or
authorize dual gateway.

Pre-open tests must assert readiness fails when any of these is absent or mismatched:

- PostgreSQL/MinIO/Nginx Compose container identity and health;
- API/frontend user-unit ActiveState and expected unit generation;
- API unit MainPID, `/proc/${api_main_pid}/stat` start ticks, cwd, Java argv, artifact hash and ownership of `127.0.0.1:8082`;
- frontend MainPID/process tree ownership of `127.0.0.1:3001`;
- exact loopback-only Nginx listeners and generation header, with Docker host-gateway `:3000` proven absent;
- host `127.0.0.1` and browser host `cmms.localhost` reaching the loopback gateway;
- `/api/health-check`, license/company/user/role/API-Key reconciliation and intent-specific work-order checks using current administrator slots only inside the confirmed lifecycle apply;
- state-aware invitation evidence: require the recent invitation before runtime signup, but after signup require the exact runtime user/company/role plus the durable invitation action receipt because the formal recent-invitation query intentionally excludes emails that already have users;
- offline guard/IP evidence or online budget/mode evidence;
- acceptance source cleanliness and gitlink equality.

Post-open tests run only after `Gateway.enable_dual` and must assert the exact Docker bridge listener, isolated-container `host.docker.internal` and `cmms.localhost` paths, runtime API-Key scope, and gateway generation. MinIO probe tests must cover a fixed-clock SigV4 vector, canonical host `127.0.0.1:9000`, RFC 3986 path/query encoding, transformed public path `/storage/`, host/container download equality, cleanup, and fail-closed behavior for a failed cleanup or Host rewrite.

The live-acceptance scope must use only the runtime API Key for CMMS reads. Its three successful CMMS shapes are exact `GET /auth/me`, `POST /work-orders/search`, and one `GET /assets/by-equipment-id/{canonical_nonexistent_uuid}` that must return exact `404 ASSET_NOT_FOUND`. Its three safe negative shapes are `GET /roles`, `GET /api-keys/{verifiedId}` and the read-semantics `POST /api-keys/search?page=0&size=100`; each must return the exact gateway policy denial. Offline fake tests require upstream call count `0`. Live checks instead bind the loaded Nginx config hash/generation to the validated template/allowlist and require that its exact policy `return` precedes any `proxy_pass`; the current CMMS exposes no reliable per-request upstream counter, so do not claim a dynamic live zero-call measurement. It never sends asset search/create, API-Key create/delete, administrator user/invitation mutations or role mutations, and it does not claim to re-prove the historical invitation.

`BOOTSTRAP_ACCEPTANCE` is used only before Phase 2 provisioning and requires `asset_total == 0`, `work_order_total == 0` and absence of the exact Phase 2 seed receipt `.runtime/receipts/phase2-shadow-seed.json`; only this intent may create `.runtime/receipts/cmms-development.json`. `OPERATIONAL` is used for daily start/restart/switch-license after the chain may contain real data: it requires bounded asset/work-order search to succeed and, when lifecycle captured pre-operation totals from the same volume, requires both post-operation totals to match, but never requires zero. The CMMS deployment live receipt/gate is therefore a one-shot pre-Phase-2 acceptance artifact; later end-to-end checks use the separately authorized Phase 2 receipts and markers.

- [ ] **Step 2: Run the readiness tests and verify the expected failure**

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  e2e/test_cmms_readiness.py \
  e2e/test_cmms_deployment_runtime.py \
  -m "not cmms_deployment_e2e" --strict-markers -v
```

Expected: FAIL because readiness and marker plumbing are absent.

- [ ] **Step 3: Implement PID, socket and gateway identity proofs**

Read unit properties through fixed `systemctl --user show` fields. Open `/proc` files with bounded reads, bind PID plus start ticks to avoid PID reuse, and compare working directory, executable/argv and artifact. Parse fixed `ss -H -ltnp` output and require the API/frontend listeners belong to the expected process tree.

During pre-open, make only these host-loopback HTTP requests without proxy/redirect:

```text
http://127.0.0.1:3000/api/health-check
http://cmms.localhost:3000/api/health-check
```

Pre-open also proves the verified Docker bridge address has no `:3000` listener and that a one-shot container cannot connect to `host.docker.internal:3000`. It never expects a container gateway request to succeed while Nginx is loopback-only.

After lifecycle enables dual mode, run a new one-shot container from the already-pinned Nginx image with exact `host.docker.internal:host-gateway` and `cmms.localhost:host-gateway` mappings. Require both container URLs to resolve to the verified bridge address, reach the gateway and return the state-bound `X-iFactory-CMMS-Gateway` generation. Any post-open check failure immediately raises through lifecycle, whose exception path restores loopback-only. Do not treat `/health-check` alone as readiness because the CMMS health indicator does not validate PostgreSQL, MinIO or license.

- [ ] **Step 4: Implement composite readiness aggregation**

Evaluate every named read-only check and retain only bounded, secret-free evidence:

```python
def combine_readiness(
    checks: Sequence[ReadinessCheck],
) -> ReadinessReport:
    check_codes = tuple(check.code for check in checks)
    failure_codes = tuple(
        check.code
        for check in checks
        if not check.ok
    )
    evidence = {
        check.code: check.safe_evidence
        for check in checks
    }
    return ReadinessReport(
        ok=not failure_codes,
        check_codes=check_codes,
        failure_codes=failure_codes,
        evidence=evidence,
    )
```

Call the Compose identity/health, unit/PID/socket, gateway path, formal API identity/scope, license control and source-cleanliness checks in a fixed order. Readiness succeeds only if every check is true.

Implement and wire `LifecycleReadinessAdapter` in this task; remove the public
CLI's Task 9 `UnavailableReadinessPort` binding. The adapter derives intent
only from the immutable confirmed plan produced by Task 9. It selects
`BOOTSTRAP_ACCEPTANCE` iff profile is `ACCEPTANCE`, operation is `BOOTSTRAP`
or `REPAIR`, and the already registry-valid tuple contains exactly one
`readiness.probe-minio-route`. Task 2's grammar proves a repair tuple with that
row is either bootstrap-mutation completion or completed-publication
readiness-only, and Task 9 planning plus apply-time preflight already proved
seed absence, State-selected/projected state
`FINAL_PERMISSIONS_VERIFIED` or `GATEWAY_ENABLED` and the complete composite
pre-open/dual/post-open rows. Those assessment-only facts are not read by this
adapter and need not be serialized as a second branch.
Discovery, budget-recovery and capture-cleanup repair never use
`BOOTSTRAP_ACCEPTANCE`; every `start`, restart and mode switch uses
`OPERATIONAL`. The adapter rejects any env/CLI override and cannot add a
missing action.

`require_api_loopback()` exact-checks its distinct planned row, calls only
`check_api_loopback()` and returns `ApiLoopbackEvidence`; it cannot call or
substitute `check_preopen()`. `require_loopback()` calls `check_preopen()` with
current administrator slots and returns a typed `LoopbackReadinessEvidence`
that binds the report, plan hash, state generation and API process start ticks.
Lifecycle keeps that value locally, enables dual mode and passes it into
`require_dual()`, which rejects a binding mismatch and runs
`check_postopen()`. For `BOOTSTRAP_ACCEPTANCE`, `require_dual()` exact-checks
the already planned `readiness.probe-minio-route`, runs `MinioProbe` exactly
once, and returns a `LifecycleReadinessEvidence` containing an in-memory
acceptance receipt only after pre-open, post-open and cleanup all succeed. For
`OPERATIONAL`, it returns no receipt and performs no MinIO or domain write.
Lifecycle alone atomically persists a returned receipt. Any adapter exception
propagates through the existing lifecycle compensation path, which restores
loopback-only and cannot emit a receipt.

The CLI constructs the adapter with validated descriptors and passes it into
`LifecycleDependencies`. Offline tests keep using a scripted port. Add
ordering tests for pre-open → dual enable → post-open → one MinIO probe →
receipt, plus failure injection at each boundary, negative tests for
discovery/revoke-only/discard-only/budget-recovery/capture-cleanup, positive
tests for both completion and readiness-only acceptance repair, and a test
proving operational readiness performs zero probe writes.

- [ ] **Step 5: Implement SigV4 signing and public-origin rewriting**

Use standard-library HMAC/SHA-256, an injected UTC clock and these fixed values:

```text
internal_origin  http://127.0.0.1:9000
canonical_host   127.0.0.1:9000
region           us-east-1
service          s3
presign_expiry   60 seconds
```

Canonical URI encoding is UTF-8 RFC 3986 with uppercase percent hex and safe characters `A-Z a-z 0-9 - . _ ~`; preserve `/` only as the segment separator and encode every segment exactly once. Canonical query encoding uses the same rules, sorts by encoded key and then encoded value, preserves duplicate pairs, and never uses `quote_plus` or `+` for spaces. Reject pre-encoded ambiguity, control characters, dot segments, empty bucket/key, fragments, userinfo and any origin/host drift.

For the presigned GET, use payload hash `UNSIGNED-PAYLOAD`, canonical header `host:127.0.0.1:9000\n`, signed headers `host`, and exactly these query fields before computing the signature:

```text
X-Amz-Algorithm=AWS4-HMAC-SHA256
X-Amz-Credential={access_key}/{yyyymmdd}/us-east-1/s3/aws4_request
X-Amz-Date={yyyymmdd}T{hhmmss}Z
X-Amz-Expires=60
X-Amz-SignedHeaders=host
```

Append `X-Amz-Signature` only after hashing the canonical request/query without that field. PUT uses the exact payload SHA-256; DELETE and final HEAD use the empty-payload SHA-256. Those direct internal requests use header authentication with lowercase canonical headers `host`, `x-amz-content-sha256`, `x-amz-date` and an Authorization value whose exact grammar is `AWS4-HMAC-SHA256 Credential={access_key}/{yyyymmdd}/us-east-1/s3/aws4_request, SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature={lowercase_hex64}`. HEAD must return exact object absence after DELETE; transport/timeouts/`5xx` are uncertain cleanup, never absence.

Derive the signing key exactly:

```python
def sigv4_signing_key(
    secret: bytes,
    date_stamp: str,
    region: str,
    service: str,
) -> bytes:
    date_key = hmac.new(
        b"AWS4" + secret,
        date_stamp.encode("ascii"),
        hashlib.sha256,
    ).digest()
    region_key = hmac.new(
        date_key,
        region.encode("ascii"),
        hashlib.sha256,
    ).digest()
    service_key = hmac.new(
        region_key,
        service.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return hmac.new(
        service_key,
        b"aws4_request",
        hashlib.sha256,
    ).digest()


def rewrite_signed_minio_origin(
    signed_url: PresignedUrl,
    *,
    public_origin: str,
) -> PresignedUrl:
    signed = split_transport_only(signed_url)
    public = urllib.parse.urlsplit(public_origin)
    if signed.scheme != "http" or signed.netloc != "127.0.0.1:9000":
        raise DeploymentError(
            "CMMS-E100",
            "signed MinIO origin is not the fixed internal origin",
            100,
        )
    path = public.path.rstrip("/") + signed.path
    return PresignedUrl._from_signer(
        urllib.parse.urlunsplit(
            (public.scheme, public.netloc, path, signed.query, "")
        )
    )
```

`PresignedUrl` is secret-bearing because `X-Amz-Credential` contains the access-key identity and the query contains a signature. Its constructor is private to the signer, `str()`/`repr()` are redacted, exceptions expose only safe origin/path/query-field names, and only the fixed HTTP transport may unwrap it immediately before writing request bytes. Never convert it to a plain string in an assertion, receipt, event, command argument, environment, label or log. Nginx must restore the exact signed Host before proxying, and Task 4's `/storage/` location must keep access logging disabled.

Add a frozen-clock vector at `2015-08-30T12:36:00Z` with fixed test-only credentials, bucket and key; assert the complete canonical URI, canonical query, canonical request hash, string-to-sign and final signature against checked-in literals. Separately vary spaces, Unicode, `%`, `+`, repeated query keys and path separators. Redaction tests inject failure at signing, origin rewrite, host download, container transport and cleanup, then assert the access key, secret, `X-Amz-Signature` value and complete query never appear in safe errors or captured output.

Use a dedicated `http.client`-based MinIO transport, never `urllib.request`: it accepts only the fixed internal origin or the approved `cmms.localhost:3000/storage` origin, constructs direct connections itself, disables ambient proxy behavior by construction, never follows redirects, and revalidates scheme/host/port plus normalized path immediately before send. `cmms.localhost` must resolve only to the approved loopback addresses; the transport connects to one of those addresses while sending the signed public Host. Apply fixed connect/read deadlines and bounded status/header/body sizes. Every `3xx`, unexpected authority/path, proxy sentinel, malformed response or cap breach is `UNKNOWN` and fail-closed; tests set `HTTP_PROXY`/`HTTPS_PROXY`, return redirects and prove neither credentials nor query reach the proxy/redirect target.

- [ ] **Step 6: Implement the ephemeral MinIO route probe**

Using the validated MinIO secret files:

1. Address MinIO internally as `http://127.0.0.1:9000`.
2. With header-auth SigV4, PUT one random payload of 1–4096 bytes at the one exact UTF-8 key `.ifactory-readiness/space + percent% 零件/${nonce}` in `atlas-bucket` and require an exact successful status. Its request path must encode the fixed tricky segment as `space%20%2B%20percent%25%20%E9%9B%B6%E4%BB%B6`, proving Nginx prefix stripping preserves the same SigV4 canonical path used by realistic CMMS filenames.
3. Generate one 60-second SigV4 GET URL whose signed `Host` is exactly `127.0.0.1:9000`.
4. Replace only the origin with `http://cmms.localhost:3000/storage`.
5. Pass the `PresignedUrl` wrapper directly to the host transport and require exact payload equality.
6. Before the container download, require the pinned-image receipt and prove the exact name `ifactory-cmms-probe-${plan_prefix}-${nonce}` does not exist. The actual fixed-entrypoint run both exercises the image's BusyBox `nc` applet and performs the download; a missing applet is a controlled failure followed by exact object cleanup. Run this complete shell-free shape (with the already-fixed Docker host prefix and exact pinned image reference):

   ```text
   docker run --rm --interactive
     --name ifactory-cmms-probe-${plan_prefix}-${nonce}
     --network bridge
     --log-driver none
     --label io.ifactory.project=ifactory-cmms-dev
     --label io.ifactory.purpose=cmms-readiness
     --label io.ifactory.plan-sha256=${plan_sha256}
     --label io.ifactory.nonce=${nonce}
     --add-host host.docker.internal:host-gateway
     --add-host cmms.localhost:host-gateway
     --entrypoint /bin/busybox
     nginx:1.27.0-alpine@sha256:a377278b7dde3a8012b25d141d025a88dbf9f5ed13c5cdf21ee241e7ec07ab57
     nc cmms.localhost 3000
   ```

   `--interactive` is mandatory so `CommandSpec.input_bytes` reaches `nc`; overriding the entrypoint prevents `/docker-entrypoint.sh` logs from corrupting the response. Send the complete bounded HTTP request—including the secret query—only through stdin; never place it in Docker argv, env, labels, name or image `Config.Cmd`. Stdout must begin with a valid HTTP status line, then parse a bounded `Connection: close` response and require exact payload equality.
7. In a `finally` path, header-auth DELETE only that exact key, then send one header-auth HEAD for the same key and require that immediate bounded response to prove `404`; never retry a write or list/delete a prefix. Inspect the exact helper name. A residue may be force-removed only if name, all four labels, pinned image ID and bridge network match this plan/nonce; a same-name foreign or mismatched container is reported but never removed. Prove the owned helper absent afterward.

No CMMS `File` record is created. The raw URL/request bytes live only in process memory and stdin; command results, cleanup receipts and Nginx logs contain no query. If object or container cleanup is uncertain, keep gateway fail-closed, emit only a stable cleanup code and do not create an acceptance receipt. Tests cover missing `--interactive`, entrypoint noise, non-HTTP first bytes, timeout, malformed response, output cap, owned/foreign container residue, DELETE response loss and HEAD ambiguity; a same-name mismatched container produces zero `rm` calls. Every failure test proves cleanup errors redact all credential/signature material.

- [ ] **Step 7: Add acceptance receipt and pytest marker gate**

Register:

```toml
"cmms_deployment_e2e: opt-in live CMMS deployment acceptance requiring a confirmed deployment receipt"
```

Add required live options `--cmms-deployment-receipt` and `--cmms-phase2-env-file` to `tests/e2e/conftest.py`. The path policy accepts only `../.runtime/receipts/cmms-development.json`, the absence-check target `../.runtime/receipts/phase2-shadow-seed.json`, and `../.runtime/predictive-maintenance-shadow.env` after canonical resolution. The latter is not the CMMS runtime env: parse its strict known-key schema and unwrap only `PILOT_CMMS_CREDENTIAL` when its envelope type is exactly `cmms_api_key`. Without both options, collection marks every `cmms_deployment_e2e` item skipped before fixtures can touch Docker/HTTP/systemd.

The receipt is canonical `0600` JSON, contains no credentials and uses the
exact Task 2 top-level schema. This task fixes `asset_total=0`,
`work_order_total=0`, `minio_cleanup_code=CLEAN` and the bounded readiness-code
vocabulary; it does not extend the wire record.

The readiness hashes cover canonical secret-free reports. The bounded check tuples must include exact dual-listener generation plus the `host.docker.internal` and `cmms.localhost` container-path codes from the same post-open apply. The MinIO fields bind only canonical secret-free result codes from the single write probe run inside that apply; no field contains an object key, URL, access key, signature or payload. Development dirty runs can produce state/readiness reports but never this acceptance receipt.

- [ ] **Step 8: Add a collection-time skip regression test**

Use pytest's `pytester` fixture to prove the live item is skipped before its fixture body can run:

```python
def test_live_marker_skips_before_fixture_without_receipt(
    pytester: pytest.Pytester,
) -> None:
    touched = pytester.path / "fixture-touched"
    pytester.makeini(
        "[pytest]\n"
        "markers =\n"
        "    cmms_deployment_e2e: opt-in CMMS deployment acceptance\n"
    )
    pytester.makepyfile(
        test_live=f"""
import pytest

@pytest.fixture
def live_runtime():
    open({str(touched)!r}, "wb").close()

@pytest.mark.cmms_deployment_e2e
def test_live(live_runtime):
    raise AssertionError("live body must not run")
"""
    )

    result = pytester.runpytest(
        "-p",
        "e2e.conftest",
        "--strict-markers",
    )

    result.assert_outcomes(skipped=1)
    assert touched.exists() is False
```

Enable pytest's built-in `pytester` plugin only for this test module. Load the repository's real `e2e.conftest` plugin explicitly in the isolated pytester run and register the marker in its temporary ini. The actual collection hook requires a secure confirmed receipt before any live fixture resolves.

- [ ] **Step 9: Implement the live test without enabling it**

`test_cmms_deployment_runtime.py` must:

- load and revalidate the exact receipt and runtime env through secure descriptors;
- require root/CMMS current state still matches the receipt;
- require `.runtime/receipts/phase2-shadow-seed.json` is still absent; once Phase 2 provisioning begins this one-shot deployment marker is no longer valid;
- call only `ReadinessChecker.check_live_acceptance`; never call `MinioProbe` or load MinIO credentials;
- revalidate existing Compose identity/health with `compose ps`/`docker inspect`, API/frontend PID/socket/gateway generation, the host unsigned MinIO health path, receipt-bound container-path/MinIO probe result codes and current source state without creating or deleting an object or container;
- perform no signup, password change, invitation, role mutation, API Key creation, asset creation, work-order creation, telemetry, Alarm, Dashboard or training action;
- use the API Key only for successful `/auth/me`, work-order search and canonical nonexistent equipment lookup, plus the three exact gateway-denied safe probes documented in Step 1;
- fail on any nonzero receipt-bound asset/work-order baseline or an existing Phase 2 seed receipt.

The receipt authorizes evidence validation, not a second write probe. Running this read-only marker still requires a separate explicit operator decision. Repeating MinIO PUT/DELETE, even for smoke, requires a newly generated deployment plan, a new exact hash and another confirmation turn.

The live-marker fake runner asserts every Docker argv is read-only `compose ps` or `inspect`; `run`, `create`, `start`, `restart`, `exec`, `rm`, `pull`, `up` and `stop` are forbidden. Container-path success is revalidated from the same apply's signed receipt evidence rather than by starting a new helper container.

- [ ] **Step 10: Run the offline readiness and marker tests**

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  e2e/test_cmms_readiness.py \
  e2e/test_cmms_deployment_runtime.py \
  -m "not cmms_deployment_e2e" --strict-markers -v
```

Expected: offline readiness tests pass and live runtime tests are deselected or skipped before any fixture touches Docker, HTTP or systemd.

- [ ] **Step 11: Run slice verification**

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  e2e/test_cmms_readiness.py \
  e2e/test_cmms_deployment_runtime.py \
  -m "not cmms_deployment_e2e" --strict-markers -v
git diff --check
./scripts/doctor.sh
```

Expected: tests pass and doctor reports zero failures.

- [ ] **Step 12: Commit readiness and live-gate code**

Run:

```bash
set -Eeuo pipefail
git add \
  deploy/cmms/src/ifactory_cmms_deploy/readiness.py \
  deploy/cmms/src/ifactory_cmms_deploy/lifecycle.py \
  deploy/cmms/src/ifactory_cmms_deploy/records.py \
  deploy/cmms/src/ifactory_cmms_deploy/cli.py \
  tests/e2e/conftest.py \
  tests/e2e/test_cmms_readiness.py \
  tests/e2e/test_cmms_deployment_runtime.py \
  tests/pyproject.toml tests/uv.lock
git commit -m "test: add opt-in CMMS deployment acceptance gate"
```

Expected: the commit adds the gate but does not fabricate a receipt or access the live environment.

### Task 11: Document the Runbook and Add Offline CI Coverage

**Files:**

- Create: `deploy/cmms/README.md`
- Modify: `README.md`
- Modify: `deploy/README.md`
- Modify: `tests/README.md`
- Modify: `docs/local-development.md`
- Modify: `docs/superpowers/specs/2026-07-31-cmms-hybrid-development-deployment-design.md`
- Modify: `.github/workflows/workspace-check.yml`

**Interfaces:**

- Produces: operator runbook for plan/status/secret/bootstrap/start/restart/stop/repair/mode switch/live acceptance
- Produces: CI coverage for all offline CMMS deployment tests
- Preserves: lightweight `doctor.sh`, `bootstrap.sh`, `.gitignore`, component repos and live opt-in boundary

- [ ] **Step 1: Write the deployment runbook**

Document:

- topology, fixed origins/ports and why Nginx listens on IPv4 plus IPv6 loopback;
- exact `.runtime/` layout and `0600` creation rules;
- immutable SHA-addressed bootstrap receipt generations, the StateRecord current pointer, and why orphan generations never authorize recovery;
- toolchain bootstrap, API controlled rebuild/restart and frontend HMR;
- offline versus online license limitations and mode switch;
- candidate/current password preparation using `secret set-candidate`, never chat or argv;
- deployment/bootstrap `plan` output, separate-turn hash confirmation and `apply`;
- the deployment-root-scoped apply-effect single-writer lock: a valid apply first writes its lease-external `ATTEMPTED` audit reservation, concurrent loser records only `CONTENDED` before gateway/HTTP/receipt/State/service effects, and that consumed plan must be regenerated rather than waited or retried;
- daily `start`, `restart-api`, `restart-frontend`, `stop`, `status` and explicit `repair`;
- data preservation and rollback; no `down -v`;
- bootstrap API routes, invitation seven-day reconciliation limitation, API-Key capture/receipt/State lineage, publish-before-unlink ordering and lost API Key repair;
- development `UNCOMMITTED` versus acceptance clean receipt;
- this deployment prepares CMMS and its Phase 2 API Key, but does not itself run Phase 2 provisioning, seed, training, Alarm or work-order writes.

Update the design status to “书面设计已批准；控制面实现中/完成，live bootstrap 待独立确认” as appropriate at implementation time, and link this plan.

- [ ] **Step 2: Document default and live test commands**

Default offline command:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  e2e/test_cmms_secure_artifacts.py \
  e2e/test_cmms_deployment_records.py \
  e2e/test_cmms_toolchains_and_source.py \
  e2e/test_cmms_deployment_contract.py \
  e2e/test_cmms_nginx_contract.py \
  e2e/test_cmms_systemd_and_start_gate.py \
  e2e/test_cmms_license_modes.py \
  e2e/test_cmms_api_and_credentials.py \
  e2e/test_cmms_bootstrap_state_machine.py \
  e2e/test_cmms_lifecycle.py \
  e2e/test_cmms_readiness.py \
  e2e/test_cmms_deployment_runtime.py \
  -m "not pilot_e2e and not cmms_deployment_e2e" \
  --strict-markers -v
```

Separately authorized live gate:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  -m cmms_deployment_e2e \
  e2e/test_cmms_deployment_runtime.py \
  --cmms-deployment-receipt \
  ../.runtime/receipts/cmms-development.json \
  --cmms-phase2-env-file \
  ../.runtime/predictive-maintenance-shadow.env \
  --strict-markers -v
```

State clearly that the second command is invalid before a confirmed deployment apply produces the receipt.

- [ ] **Step 3: Add only offline tests to `workspace-check`**

Extend executable validation with `scripts/cmms-development.sh`. Add one CMMS offline test step using the command from Step 2 and excluding both live markers. Keep the final no-mutation checks:

```yaml
- name: Validate script modes
  run: >
    test -x scripts/bootstrap.sh
    -a -x scripts/doctor.sh
    -a -x scripts/run-digital-mcp.sh
    -a -x scripts/cmms-development.sh

- name: Validate CMMS deployment control offline
  run: >
    uv run --directory tests --frozen
    pytest e2e/test_cmms_secure_artifacts.py
    e2e/test_cmms_deployment_records.py
    e2e/test_cmms_toolchains_and_source.py
    e2e/test_cmms_deployment_contract.py
    e2e/test_cmms_nginx_contract.py
    e2e/test_cmms_systemd_and_start_gate.py
    e2e/test_cmms_license_modes.py
    e2e/test_cmms_api_and_credentials.py
    e2e/test_cmms_bootstrap_state_machine.py
    e2e/test_cmms_lifecycle.py
    e2e/test_cmms_readiness.py
    e2e/test_cmms_deployment_runtime.py
    -m "not pilot_e2e and not cmms_deployment_e2e"
    --strict-markers -v
```

Retain:

```bash
git diff --check
test -z "$(git status --porcelain)"
git submodule foreach --recursive 'test -z "$(git status --porcelain)"'
```

Do not add toolchain downloads, Maven/npm builds, Docker pulls, Compose up, systemd or live HTTP to CI.

- [ ] **Step 4: Verify docs and workflow changes**

Run:

```bash
set -Eeuo pipefail
rg -n \
  "cmms-development|cmms_deployment_e2e|host.docker.internal:3000|UNCOMMITTED" \
  README.md deploy tests docs/local-development.md .github/workflows/workspace-check.yml
git diff --check
./scripts/doctor.sh
```

Expected: docs describe authorization boundaries accurately, the workflow remains offline, and doctor reports zero failures.

- [ ] **Step 5: Commit the runbook and CI coverage**

Run:

```bash
set -Eeuo pipefail
git add \
  README.md \
  deploy/README.md deploy/cmms/README.md \
  tests/README.md docs/local-development.md \
  docs/superpowers/specs/2026-07-31-cmms-hybrid-development-deployment-design.md \
  .github/workflows/workspace-check.yml
git commit -m "docs: add CMMS hybrid deployment runbook"
```

Expected: the commit includes only the runbook, approved-design status update and offline workflow coverage.

### Task 12: Run Full Offline Verification and Stop Before Live Deployment

**Files:** None unless a verification failure requires a separately reviewed fix.

**Interfaces:**

- Consumes: all control-plane commits from Tasks 1–11
- Produces: evidence that implementation is complete but CMMS remains undeployed

- [ ] **Step 1: Invoke the verification-before-completion skill**

Read and follow `superpowers:verification-before-completion` before making any passing/completion statement. If a test fails, use `superpowers:systematic-debugging`; do not weaken or skip the assertion.

- [ ] **Step 2: Run the entire new offline CMMS suite**

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  e2e/test_cmms_secure_artifacts.py \
  e2e/test_cmms_deployment_records.py \
  e2e/test_cmms_toolchains_and_source.py \
  e2e/test_cmms_deployment_contract.py \
  e2e/test_cmms_nginx_contract.py \
  e2e/test_cmms_systemd_and_start_gate.py \
  e2e/test_cmms_license_modes.py \
  e2e/test_cmms_api_and_credentials.py \
  e2e/test_cmms_bootstrap_state_machine.py \
  e2e/test_cmms_lifecycle.py \
  e2e/test_cmms_readiness.py \
  e2e/test_cmms_deployment_runtime.py \
  -m "not pilot_e2e and not cmms_deployment_e2e" \
  --strict-markers -v
```

Expected: all non-live tests pass; live items are deselected or skipped before touching runtime.

- [ ] **Step 3: Re-run existing platform regression gates**

Run:

```bash
set -Eeuo pipefail
uv run --directory tests --frozen pytest \
  test_workspace_governance.py contract/phase1 -v
uv run --directory tests --frozen pytest \
  e2e/test_fixture_scripts.py \
  e2e/test_shadow_deployment_contract.py \
  e2e/test_shadow_seed.py \
  e2e/test_shadow_prediction.py \
  -m "not pilot_e2e and not cmms_deployment_e2e" \
  --strict-markers -v
./scripts/doctor.sh
```

Expected: all existing governance, Phase 1 and Phase 2 offline tests pass.

- [ ] **Step 4: Prove implementation did not mutate components or runtime**

Run:

```bash
set -Eeuo pipefail
test -z "$(git -C components/cmms status --porcelain)"
test "$(git -C components/cmms rev-parse HEAD)" = \
  "f3cab0aaf3418638e76dc33dfa5f8b30ded2b7f0"
test "$(git rev-parse HEAD:components/cmms)" = \
  "f3cab0aaf3418638e76dc33dfa5f8b30ded2b7f0"
git diff --check
test -z "$(git status --porcelain)"
```

Expected: CMMS and all other submodules are unchanged; root status contains no uncommitted implementation file. The existence of pre-existing unrelated runtime state is not modified or used as proof.

- [ ] **Step 5: Report the mandatory live-deployment halt**

Report:

```text
Control plane implemented and offline-verified.
CMMS services have not been deployed.
No toolchain/image was downloaded by the default tests.
No account, role, API Key, asset or work order was created.
The next action is to prepare local secret candidates, generate one bootstrap plan,
display its exact SHA-256, and stop for a new user confirmation.
```

Do not run `secret set-candidate`, `plan --operation bootstrap`, `apply`, the live pytest marker or Phase 2 discovery merely because Tasks 1–12 passed.

## Post-Implementation Operational Gate (Not Authorized by This Plan)

After control-plane implementation is merged and the operator has placed a valid license and candidates locally:

1. Run `status`; prepare PostgreSQL/MinIO/license secrets through `secret set-runtime`, create JWT through `secret generate-jwt`, import the offline license through a file descriptor, and prepare each identity password through `secret set-candidate`. Never transmit values through chat or argv.
2. Run `plan --operation bootstrap --profile acceptance --license-mode offline` (preferred) or explicitly choose online fallback.
3. Present the complete safe action summary, online budget projection if applicable, and exact plan SHA-256 to the user.
4. Stop. Only a later user response confirming that exact hash authorizes `apply`.
5. After apply produces a clean acceptance receipt, separately authorize and run `cmms_deployment_e2e`.
6. Only then use the existing Phase 2 identity-discovery/provisioning/seed confirmation gates. CMMS deployment approval does not authorize Phase 2 assets, telemetry, Dashboard, training, Alarm or work-order writes.
