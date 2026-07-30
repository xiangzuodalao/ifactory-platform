# iFactory Platform

iFactory Platform 是 CMMS、ThingsBoard、PDM Algorithm、数字化平台 MCP 与平台集成服务的集成总仓。业务代码保留在独立组件仓库中；本仓库统一锁定组件版本，并维护契约、部署、端到端测试和 Codex 项目能力。

本项目不维护独立 Agent 应用。Codex 直接作为开发与运维协作 Agent，通过仓库级 `AGENTS.md`、Skills 和 `digital-platform` MCP 获取项目规则、受控训练流程及后续系统工具。

## 目录

```text
ifactory-platform/
├── AGENTS.md
├── .codex/config.toml
├── .agents/skills/
├── components/
│   ├── cmms/
│   ├── thingsboard/
│   ├── pdm-algorithm/
│   ├── digital-mcp/
│   └── platform-integration/
├── contracts/
├── deploy/
├── docs/
├── scripts/
└── tests/
```

- `components/*`：独立 Git submodule，不在总仓直接混合提交业务源码。
- `contracts/`：跨系统 OpenAPI、AsyncAPI 和 JSON Schema 契约。
- `deploy/`：本地 Compose、网关及后续生产部署配置。
- `.agents/skills/`：Codex 可复用工作流；PDM 训练 Skill 位于总仓，场景接入 Skill 由 PDM 组件维护。
- `.codex/config.toml`：仅在信任本仓库后加载的项目级 MCP 配置。

## 初始化

```bash
git clone --recurse-submodules <repository-url> ifactory-platform
cd ifactory-platform
./scripts/bootstrap.sh
./scripts/doctor.sh
```

已有 clone 可执行：

```bash
git submodule sync --recursive
git submodule update --init --recursive
```

始终从总仓根目录启动 Codex：

```bash
cd /home/vm/code/ifactory-platform
codex
```

Codex 需要信任本仓库才能加载 `.codex/config.toml`、项目 Skills 和 MCP 配置。修改配置后请新开会话。

## digital-platform MCP

项目配置通过 STDIO 启动：

```bash
uv run --project components/digital-mcp --frozen --no-dev pdm-mcp
```

配置只转发下列本地环境变量，不在仓库保存凭据或环境值：

```bash
export PDM_API_BASE_URL=http://127.0.0.1:10021
export PDM_TRAIN_TIMEOUT_SECONDS=7200
```

`PDM_ENABLE_TRAIN` 未设置时由 MCP 服务保持安全默认值 `false`。只有准备执行已预检、已跨轮确认的单次训练时，才在启动 Codex 前显式设置：

```bash
export PDM_ENABLE_TRAIN=true
```

`digital-platform` 对写工具默认请求审批，`pdm_train_model` 始终单独请求确认。Skill 的业务确认门禁与 Codex 工具审批是两层独立保护，二者都不能跳过。

## Git 工作流

组件业务改动先在对应 submodule 中完成：

```bash
git -C components/<component> switch -c feat/<topic>
git -C components/<component> status
git -C components/<component> add <files>
git -C components/<component> commit
git -C components/<component> push -u origin feat/<topic>
```

组件提交合并或确定引用后，再在总仓更新指针与集成文件：

```bash
git status --short
git add components/<component> contracts/ deploy/
git commit
```

不要提交脏 submodule、训练数据、模型产物、构建目录或任何密钥。更完整的协作约定见 [AGENTS.md](AGENTS.md)。
