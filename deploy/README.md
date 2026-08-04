# 部署目录

此目录预留给数字化平台的统一编排和部署配置。当前阶段没有可直接用于生产的 Compose、Kubernetes 或网关清单，也不承诺四个组件能够通过单条命令完整启动。

计划中的目录职责：

```text
deploy/
├── compose/   # 本地联调与可复现开发环境
├── k8s/       # 生产/预生产部署资源
└── gateway/   # TLS、认证、授权、限流和路由
```

部署配置应遵守以下边界：

- CMMS、ThingsBoard、PDM Algorithm 和 `digital-mcp` 保持独立进程、独立配置和独立数据所有权。
- 不通过共享数据库完成服务集成；服务间只使用有版本的 API 或事件契约。
- 密钥由环境或密钥管理系统注入，只提交无密钥的示例文件。
- `digital-mcp` 默认仅以 STDIO 供当前 Codex 会话使用。若未来启用 HTTP 入口，必须置于提供 TLS、认证、授权和限流的网关后。
- 本项目不部署独立 Agent 服务。Codex 是开发和运维入口，Skills 定义受控工作流，MCP 只暴露边界清晰的工具。

新增部署清单时，应同时提供健康检查、资源限制、持久化策略、回滚步骤和最小权限说明。

## Phase 2 预测性维护 Shadow 环境

`compose/predictive-maintenance-shadow.yml` 是隔离试点编排，不是生产部署清单。它把
`platform-integration`、PDM 的只读 fixture 运行模式和独立 PostgreSQL 串联起来，
并只通过稳定 HTTP API 读取本机隔离的 ThingsBoard 与 CMMS。它不会直连组件数据库，
也不会启动训练任务。

此环境有两个明确前提：

- 本机 `8080` 和 `3000` 必须分别是经核对的隔离 ThingsBoard automotive-factory
  租户与隔离 CMMS；缺失或身份不匹配时停止，不自行启动或替换现有服务。
- Docker 构建、启动、身份发现以及任何 apply 都需要对应的单独授权。仅有代码实现或
  测试通过，不代表实际链路已经部署或验收。

将 `compose/predictive-maintenance-shadow.env.example` 复制到未跟踪的
`.runtime/predictive-maintenance-shadow.env`，设置为当前用户所有且权限 `0600`。
示例文件只保存引用名；凭据值、数据库密码和发现到的租户/公司 ID 只能放在这个
未跟踪文件中。首次发现只使用独立的
`compose/predictive-maintenance-discovery.yml`，其服务环境刻意不包含尚未发现的
身份字段。

PDM fixture 通过以下脚本准备：

```bash
./deploy/compose/scripts/prepare-pdm-fixtures.sh
```

脚本只创建 `.runtime/pdm-fixtures`，不会覆盖已有目录；主编排以只读方式挂载它。
需要回收时，必须先停止隔离 Compose 项目，再使用精确路径确认：

```bash
./deploy/compose/scripts/reset-pdm-fixtures.sh \
  --confirm-path .runtime/pdm-fixtures
```

回收脚本把目录移动到 `.runtime/recycle/`，不会递归删除。fixture 损坏或不可读时还需
显式提供 `--confirm-corrupt`。完成 descriptor-bound 校验后，helper 会在 move 前
输出并 flush manifest/hash 状态、精确 canonical source、预先确定的 destination 和
`pending` 状态；最终成功行只会在同一进程完成并复验 rename 后出现。

### 写入门与最终验收

Phase 2 有三个互不继承授权的写入门：

1. `provision-plan` 生成的集成配置计划；
2. ThingsBoard Dashboard 发布计划；
3. 20 台设备、每台 66 点的合成遥测 seed 计划。

每个 plan 都必须先展示规范 SHA-256，并在后续轮次由用户原样返回同一哈希后才能
apply。一个门的确认不能授权另一个门；不得从本地文件或前一轮输出自行推断确认值。

遥测 apply 完成精确读回并产生权限 `0600`、无凭据的回执后，才允许执行 opt-in
验收：

```bash
uv run --directory tests --frozen pytest \
  -m pilot_e2e e2e/test_shadow_prediction.py \
  --confirmed-seed-receipt ../.runtime/receipts/phase2-shadow-seed.json -v
```

该测试使用默认的
`../.runtime/predictive-maintenance-shadow.env`，通过 Compose 依次运行临时
`scheduler --once --now 2026-07-29T01:15:00Z`、
`prediction-worker --once --now 2026-07-29T01:15:00Z`，并在
`integration-api` 内读取 bounded shadow summary。两条单次命令共享同一验收时钟；
它不会启用 `continuous` profile，也不会写遥测；最终只查询 20 个设备的活动 PDM
告警和公司范围工单搜索，两侧都必须为零。

如显式传入 `--pilot-env-file`，host 校验与 Compose 必须使用该同一绝对路径。
验收会从单个安全文件描述符读取环境快照，核对隔离 tenant/mode/URL/credential-ref
等固定设置，并在每条 Compose 命令前重验文件元数据与内容指纹。核对成功的精确字节
会复制到只读且完全 seal 的匿名 `memfd`。Python 验收进程保持描述符至命令返回，
第二层 Compose plugin 通过 `/proc/<Python PID>/fd/<fd>` 消费同一快照，不依赖第一层
Docker CLI 转交额外描述符，随后关闭描述符。当前 Linux 环境已用真实两跳子进程模型
验证该 procfs 权限；启用阻止同用户子进程读取父 fd 的 `hidepid` 或 dumpable 策略时
会 fail closed。命令固定为
`docker --host unix:///var/run/docker.sock compose --project-name
predictive-maintenance-shadow`；子进程不继承调用进程的运行设置、凭据、Compose/
Docker/HOME/XDG 变量，只得到固定 `PATH` 和单次使用、权限 `0700` 的空
`DOCKER_CONFIG`。最终只读 provider transport 不使用环境代理并拒绝重定向。

seed 回执同样只从一个 `O_NOFOLLOW|O_NONBLOCK` 描述符进行 64 KiB 有界短读，并核对
读取前后元数据与实际长度；FIFO、读取期间变更、非规范或过深 JSON 都以稳定错误
fail closed。

## 闭环试点环境

`compose/closed-loop-pilot.yml` 在 Phase 2 shadow 基础上加入隔离的 CMMS、告警投递、
工单投递、状态轮询和同源 Dashboard 网关。它仍不是生产清单，闭环默认只服务于固定
试点租户；20 台设备均可预测，但只有配置的
`LINE-A-CNC-01 / vibration_rms` 设备可以批准真实工单。

ThingsBoard 后端与 UI 继续在宿主机 `8080/4200` 热更新运行。Compose 内的 CMMS、
PDM、platform-integration、PostgreSQL 和 MinIO 均不发布端口；唯一宿主机入口是
`127.0.0.1:18081` 同源网关。网关只把精确的工单 preview/action 路由转给
platform-integration，其他 `/api` 和 WebSocket 请求仍转给 ThingsBoard。Linux 容器
不直接访问宿主机回环地址：一个无 TCP 监听的 host-network Nginx 把宿主
`127.0.0.1:8080/4200` 转成专属卷内的 Unix socket，第二个非 root Nginx 再把它接入
provider 网络。两层 relay 与浏览器网关均保留一小时 WebSocket/HMR 空闲连接，健康
检查实际穿透宿主 HTTP，而不是只检查 socket 文件存在。

### 安全运行目录

先把主配置和派生状态两个示例分别复制到权限 `0600` 的目标文件。`.runtime`、
`.runtime/secrets`、`.runtime/closed-loop`、`.runtime/closed-loop/state` 和
`.runtime/closed-loop/state/receipts` 必须是当前用户拥有的真实
目录、权限 `0700`；五个 secret file
必须位于 `.runtime/secrets` 中、是当前用户
拥有的单链接普通文件且权限 `0600`。环境文件中的 `PILOT_RUNTIME_DIR` 必须是当前
worktree 的精确绝对 `.runtime` 路径，UID/GID 必须等于 `id -u`/`id -g`，不能使用
符号链接或其他目录；`PILOT_STATE_DIR` 必须精确指向
`.runtime/closed-loop/state`；`PILOT_STATE_LOCK_FILE` 必须精确指向同级控制目录内的
`.runtime/closed-loop/.state.lock`。主 env 含 DB/PDM/TB 等运行凭据，永不挂入 runner；
专属 state 目录只含固定五键的 `closed-loop-generated.env` 和无凭据回执，可整体挂给
需要原子更新派生状态的 bootstrap/provision runner。共享 state lock 位于 runner 可写
目录之外，仅以单文件只读挂载暴露；runner 因此不能删除或替换其 inode。它们无法读取
主 env、宿主 `.runtime/secrets` 或 PDM fixture。所有非 Dashboard 命令都会清空调用者
ambient 环境，并持有控制目录内独立的 host operation lock，防止 Compose 插值覆盖和
并发 reset。

```bash
install -d -m 0700 .runtime .runtime/secrets \
  .runtime/closed-loop .runtime/closed-loop/state \
  .runtime/closed-loop/state/receipts
(umask 077; set -o noclobber; : > .runtime/closed-loop/.state.lock)
cp deploy/compose/closed-loop-pilot.env.example \
  .runtime/closed-loop/closed-loop-pilot.env
cp deploy/compose/closed-loop-generated.env.example \
  .runtime/closed-loop/state/closed-loop-generated.env
chmod 0600 .runtime/closed-loop/closed-loop-pilot.env \
  .runtime/closed-loop/state/closed-loop-generated.env \
  .runtime/closed-loop/.state.lock
./deploy/compose/scripts/prepare-pdm-fixtures.sh
```

`.state.lock` 只在首次初始化时创建；上述 `noclobber` 会在路径已存在时故意失败。启动后
不要删除、重命名或替换该文件，重建运行目录时也必须先确认闭环项目已完全停止。

不要把示例中的空值当成可运行默认值，也不要把 token、密码或连接串提交到仓库。
`PILOT_PDM_CREDENTIAL` 必须是 `opaque_bearer` envelope，并与 PDM 服务端 token 完全
一致；TB/CMMS 分别使用 `thingsboard_bearer`/`cmms_bearer` envelope。

### 启动、bootstrap 与建档

先启动宿主机 ThingsBoard 后端、UI、20 台模拟设备，并确认 `8080/4200` 只绑定本机：

```bash
./scripts/closed-loop-pilot.sh dashboard up
./scripts/closed-loop-pilot.sh dashboard verify
./scripts/closed-loop-pilot.sh config
```

下面每个 apply 都必须在展示 plan 后的另一次明确确认中使用原样 SHA-256；一个计划的
批准不能代替另一个计划：

```bash
./scripts/closed-loop-pilot.sh bootstrap-plan
./scripts/closed-loop-pilot.sh bootstrap-apply <displayed-sha256>
./scripts/closed-loop-pilot.sh provision-plan <actor>
./scripts/closed-loop-pilot.sh provision-apply <displayed-sha256> <actor>
./scripts/closed-loop-pilot.sh provision-verify <displayed-sha256>
./scripts/closed-loop-pilot.sh up
```

`bootstrap-plan` 会只读 ThingsBoard `/api/auth/user`，因此计划哈希同时绑定真实 TB
tenant/user；apply 会在任何 CMMS 写入前重读并拒绝身份漂移。CMMS bootstrap 只通过
内部网络创建独立的 `iFactory Closed Loop Pilot` 公司。密码只从 Docker secret
读取；回写的 CMMS Bearer 不出现在计划或回执中。

`provision-apply` 精确创建/读回 20 组 TB device、equipment UUID 和 CMMS asset 映射，
随后把固定试点设备写入安全环境及 `closed-loop-provision-v1` 回执。如果外部建档已
成功但本地设备选择中断，可安全恢复；命令会先在 integration DB 验证原计划已成功，
不会接受任意哈希：

```bash
./scripts/closed-loop-pilot.sh provision-select-equipment <provision-sha256>
```

`up` 使用 Compose `--wait`；PDM fixture、宿主 TB relay、CMMS、数据库或三个 provider
身份就绪检查任一失败时，不会把启动报告为成功。Bearer 过期后对应 worker 会变为
unhealthy，系统不会用管理员密码静默续签。

### Dashboard 发布与完整生命周期验收

Dashboard 仍使用 ThingsBoard automotive-factory 自己的受管 plan/apply。发布前需在其
未跟踪 `.env` 中设置 `TB_PDM_MAINTENANCE_ACTIONS_ENABLED=true`，然后可通过
`closed-loop-pilot.sh dashboard dashboard-plan ...` 和 `dashboard-apply ...` 调用。
计划、回执必须写到安全绝对路径；发布 apply 也需要独立的后续哈希确认。

```bash
./scripts/closed-loop-pilot.sh dashboard fault LINE-A-CNC-01 HIGH_VIBRATION
```

风险需要两个连续成功预测时隙才激活。出现 `PDM_FORECAST_RISK` 后，在
<http://127.0.0.1:18081> 登录并使用按钮先预览、再确认 `CREATE_WORK_ORDER`；按钮只发
一次请求，稳定幂等键为 `alert-action:{alert_uuid}:CREATE_WORK_ORDER`。ThingsBoard 原生
ACK 保留，但原生 Clear 被禁用。

只读验收命令同时核对 integration DB、真实 ThingsBoard Alarm 和真实 CMMS external-ref
工单，并拒绝 pending/dead-letter outbox。它不是仅看内部 summary：

```bash
./scripts/closed-loop-pilot.sh acceptance-verify ACTIVE
./scripts/closed-loop-pilot.sh acceptance-verify IN_PROGRESS
./scripts/closed-loop-pilot.sh acceptance-verify COMPLETE
./scripts/closed-loop-pilot.sh acceptance-verify CLEARED
```

用验收输出中的 `external_ref` 推进 CMMS 工单。状态写入同样是 plan/apply 两阶段；apply
冻结公司、完整预测身份、当前状态和 `event_version`，只发一次 PATCH，响应丢失时记录
`OUTCOME_UNKNOWN` 且不自动重试：

```bash
./scripts/closed-loop-pilot.sh work-order-status-plan <external_ref> IN_PROGRESS
./scripts/closed-loop-pilot.sh work-order-status-apply <external_ref> IN_PROGRESS <sha256>
./scripts/closed-loop-pilot.sh work-order-status-plan <external_ref> COMPLETE
./scripts/closed-loop-pilot.sh work-order-status-apply <external_ref> COMPLETE <sha256>
```

`acceptance-verify COMPLETE` 特意要求工单已完成但风险和 Alarm 仍活动，证明“完成工单”
不会提前清警。之后清除模拟故障；只有再出现两个连续健康预测时隙，集成服务才清除
风险和 TB Alarm，最终 `CLEARED` 才通过：

```bash
./scripts/closed-loop-pilot.sh dashboard clear LINE-A-CNC-01
./scripts/closed-loop-pilot.sh acceptance-verify CLEARED
```

`summary` 只用于内部队列诊断；最终验收必须使用 `acceptance-verify`。

### 可恢复 reset

初始化失败不做自动修复。先生成只含 Compose 专属卷的 reset 计划，再原样确认哈希：

```bash
./scripts/closed-loop-pilot.sh reset-plan
./scripts/closed-loop-pilot.sh reset-apply <displayed-sha256>
```

reset 先使用无秘密临时 generated env 启用全部 profiles 停止
`ifactory-closed-loop-pilot`，因此即使派生 env 被中断写坏也能恢复。确认没有残留 one-off
容器后，它会再次核对两份 env 的摘要/inode、回执精确目录项以及四个卷的名称、driver
和 Compose label；任何漂移都会在首个 volume 删除前停止。成功后，
bootstrap/provision/status 回执会移动到 `.runtime/closed-loop/state/recycle/`，generated
env 会持久化原子重建为固定五个空键，从而允许重新 bootstrap；不会触碰 ThingsBoard
数据库、PDM fixture 或其他 Compose project。
