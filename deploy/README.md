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
