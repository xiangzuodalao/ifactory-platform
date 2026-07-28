# iFactory Platform 协作约定

## 工作目录与项目边界

- 始终从本仓库根目录 `/home/vm/code/ifactory-platform` 启动 Codex。
- 本仓库是集成与发布总仓；`components/*` 是各自独立的 Git 仓库。
- 不创建 `agent/` 应用，也不依赖 `OnCall-Agent`。Codex 是本项目的 Agent，通过项目 Skills 和 `digital-platform` MCP 扩展能力。
- CMMS 负责资产、工单、计划维护和备件；ThingsBoard 负责设备、遥测、告警和规则链；PDM 负责训练、预测及模型产物；`digital-mcp` 只提供受控工具接口。
- 服务之间只通过稳定 API、事件和 `contracts/` 中的契约协作。禁止跨服务直连数据库、共享数据表或复制领域 ORM 模型。

## 修改组件前

1. 先确认目标组件及其工作树状态：`git -C components/<name> status --short`。
2. 完整读取目标组件自己的 `AGENTS.md`（若存在）和 README，再读取与任务直接相关的构建、测试或接口文档。
3. 检查目标组件当前分支和 remote，避免把修改落到错误的上游升级分支。
4. 只在目标组件内修改业务代码；跨组件接口先更新或新增 `contracts/` 契约。

组件目录与 README：

- `components/cmms`：`README.MD`
- `components/thingsboard`：`README.md`
- `components/pdm-algorithm`：`AGENTS.md`、`README.md`
- `components/digital-mcp`：`AGENTS.md`、`README.md`

## Git 规则

- 使用 `feat/*`、`fix/*`、`chore/*` 或 `upgrade/*` 短生命周期分支。
- 组件改动必须先在组件仓库提交并推送；随后在总仓单独提交 submodule 指针、契约或集成配置。
- 不提交带未提交修改的 submodule，不把组件源码复制到总仓，也不对共享分支 force-push。
- 上游升级只在 `upgrade/<component>-<version>` 分支完成，并在合并前运行该组件完整测试。
- 提交前运行 `./scripts/doctor.sh`；涉及多个服务时再运行相关契约测试和端到端测试。

## Codex、Skills 与 MCP

- 项目级 MCP 名称固定为 `digital-platform`；不要再注册或调用旧的 `pdm-algorithm` MCP。
- PdM 单次训练必须使用 `$pdm-train-model`，严格执行“健康检查和精确匹配 → 预检 → 下一轮明确确认 → 单次训练 → 状态/预测验证”。训练请求不得自动重试。
- PdM 场景接入使用 `$pdm-onboard-scenario`；该 Skill 由 PDM 组件维护，总仓只保留相对符号链接。
- 新 Skill 应保持单一职责；需要实时数据或受控操作时调用 MCP，不在 Skill 中复制服务实现。
- 创建工单、训练、发布等写操作必须经过明确确认，并保留幂等键、关联 ID 和审计信息；不得绕过 MCP/API 直接写数据库。

## 数据与安全

- 不提交 `.env`、密钥、令牌、连接串或带认证信息的 remote URL。
- 不提交训练数据、模型 checkpoint、`artifacts/`、虚拟环境、依赖目录、构建产物或运行缓存。
- 错误输出必须脱敏，不向用户展示上游堆栈、绝对产物路径、连接配置或原始时序数据。
- 删除、迁移数据以及外部写操作前，先解析并核对精确目标；优先使用可恢复操作。
