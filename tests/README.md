# 平台级测试

此目录用于跨组件契约测试与端到端测试。组件自身的单元测试、集成测试和构建验证继续保留在各自仓库中。

Phase 1 的根仓验收命令：

```bash
uv run --directory tests --frozen pytest \
  test_workspace_governance.py contract/phase1 -v
./scripts/doctor.sh
```

CI 中可独立运行契约套件：

```bash
uv run --directory tests --frozen pytest contract/phase1 -v
```

当前目录按用途组织：

```text
tests/
├── contract/phase1/   # Phase 1 OpenAPI、JSON Schema、样例和摘要一致性
└── e2e/               # 后续隔离的跨组件链路
```

Phase 1 契约测试覆盖：

- PDM v2 与 CMMS v1 OpenAPI 文档有效性、认证边界、稳定错误码和兼容行为。
- 请求与响应样例、规范 UUID、JSON 整数 token、固定预测长度及安全错误样例。
- PDM 提供方与消费者共享的请求/输入摘要投影和冻结摘要值。
- 设备映射 schema，以及租户范围唯一性约束的契约声明。

测试规则：

- 契约测试必须覆盖提供方与消费者，并验证错误、超时、幂等和兼容行为。
- 端到端测试使用隔离租户与合成数据，不连接生产数据库，不触发真实工单或生产训练。
- 模型训练、工单创建等写操作默认关闭；启用时必须显式确认、可审计且不得自动重试。
- 平台 CI 不重复构建 CMMS、ThingsBoard 和 PDM 的全部依赖；重型验证由组件仓库完成，平台仓只验证组合版本和跨组件行为。

`contract/phase1` 只读取仓库内的合成 fixture 和契约文件，不连接真实服务或数据库，不执行模型训练、工单创建或其他外部写操作。

闭环契约与部署边界的默认离线测试：

```bash
uv run --directory tests --frozen pytest \
  contract/closed_loop \
  e2e/test_closed_loop_deployment_contract.py \
  e2e/test_closed_loop_bootstrap.py \
  e2e/test_closed_loop_runtime_security.py \
  e2e/test_closed_loop_state_io.py \
  e2e/test_pilot_cmms_status.py -v
```

这些测试验证审批 API、CMMS 工单契约、Alarm details、Compose 网络/凭据隔离、网关
allowlist、bootstrap/reset 的精确哈希确认、运行目录阶段绑定，以及状态写入的单次
确认门；还覆盖双 env 物理隔离、ambient override 清除、短写/目录 fsync 与 reset
删除前漂移拒绝。它们只执行 `docker compose config` 和 fake transport，不连接 Docker daemon、
不启动容器、不访问真实服务，也不创建公司、Alarm 或工单。

真实闭环不是默认 pytest：必须先按 `deploy/README.md` 完成隔离环境的各个独立
plan/apply。`closed-loop-pilot.sh acceptance-verify` 会只读核对 integration DB、真实
ThingsBoard Alarm 和真实 CMMS 工单；`ACTIVE`、`IN_PROGRESS`、`COMPLETE`、`CLEARED`
四个里程碑共同证明 ACK 保留、工单状态回传、完成不提前清警，以及两轮健康后清警。

## Phase 2 离线与 opt-in 验收

Phase 2 的默认测试只验证 Compose 渲染、fixture 脚本边界、遥测 seed 的
plan/apply 协议，以及 shadow summary/回执解析。它使用临时文件和 mock transport，
不构建或启动容器、不访问真实 API，也不执行遥测、告警或工单写入：

```bash
uv run --directory tests --frozen pytest \
  e2e/test_fixture_scripts.py \
  e2e/test_shadow_deployment_contract.py \
  e2e/test_shadow_seed.py \
  e2e/test_shadow_prediction.py \
  -m "not pilot_e2e" -v
```

`pilot_e2e` 是显式启用的隔离环境门禁。未提供
`--confirmed-seed-receipt` 时，pytest 在 fixture 执行前跳过该 marker，因此常规
测试和 CI 不会意外触发 Compose 或真实服务。只有完成独立部署授权、三次分别确认的
plan/apply，以及 seed 精确读回后，才运行：

```bash
uv run --directory tests --frozen pytest \
  -m pilot_e2e e2e/test_shadow_prediction.py \
  --confirmed-seed-receipt ../.runtime/receipts/phase2-shadow-seed.json -v
```

默认环境文件为
`../.runtime/predictive-maintenance-shadow.env`；如需覆盖可显式传
`--pilot-env-file`。覆盖路径会解析为同一个绝对路径并同时绑定 host 校验和
`PilotCompose`，不会退回默认文件。环境文件通过单个
`O_NOFOLLOW|O_NONBLOCK` 文件描述符限长读取，必须为当前用户所有、单链接普通文件且
权限 `0600`；解析所得 inode/元数据/内容指纹会在每个 Compose 命令前重新核对。
每次核对成功后的原始字节会复制到只读、禁止写入/伸缩且不可解除 seal 的匿名
`memfd`。Python 验收进程在命令完成前保持描述符存活，Compose plugin 通过
`/proc/<Python PID>/fd/<fd>` 打开同一对象，不依赖第一层 Docker CLI 把额外描述符
继续转交给 plugin，也不会在已验证与实际执行之间重新打开操作者路径；描述符在命令
返回后关闭。该边界要求当前 Linux procfs 允许同用户子进程读取父进程的 fd；当前
验收环境已用不转发 fd 的真实两跳进程模型验证，若主机的 `hidepid`/dumpable 策略
禁止访问则命令 fail closed。
文件中的 internal tenant、isolated mode、PDM/TB/CMMS URL、凭据引用和 PDM tenant
allowlist 必须与隔离试点固定值完全一致，且不得包含 `COMPOSE_*` 控制变量。进程继承的
环境变量（包括运行参数、凭据、`COMPOSE_*`、Docker context/host、`HOME` 与
`XDG_CONFIG_HOME`）都不会传给验收命令。子进程环境只包含固定 `PATH` 和该次命令专用、
权限 `0700` 的空 `DOCKER_CONFIG`；argv 还固定
`docker --host unix:///var/run/docker.sock compose` 和隔离项目名。

seed 回执遵循相同的所有者、单链接普通文件和 `0600` 边界。读取使用单个
`O_NOFOLLOW|O_NONBLOCK` 描述符、64 KiB 上限和短读循环，并比较读取前后元数据及实际
字节数；FIFO、读取期间替换/截断和超限输入都会 fail closed。回执还必须是严格、规范
JSON：重复键、`NaN`/`Infinity`、过深输入、非规范字节或凭据都会被稳定拒绝。回执
精确包含 20 组可逆 ThingsBoard/equipment/CMMS/measurement 映射。

验收固定运行
`scheduler --once --now 2026-07-29T01:15:00Z` 和
`prediction-worker --once --now 2026-07-29T01:15:00Z`，两条临时命令共享同一个
加速时钟且不启用 `continuous` profile。summary 必须只有 20 个 `SUCCEEDED`、20 组
与回执完全一致的映射，以及 PDM fixture manifest 中的 6 个精确 artifact
SHA-256。最终 API 门只执行 ThingsBoard 告警 GET 和 CMMS 工单搜索；后者虽然是
POST，但属于无副作用的只读查询，任何非零结果都会使验收失败。两种查询都使用禁用
环境代理和 HTTP 重定向的固定-origin transport，provider 响应与 shadow summary
同样采用拒绝重复键和非有限常量的严格 JSON 解析。
