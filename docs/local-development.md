# 本地开发

## 环境要求

平台工作区的轻量管理命令需要 Bash、Git 和 `uv`。使用 Codex 进行项目开发时，还需安装 Codex CLI。Java、Node.js、数据库和 GPU/PyTorch 等重型依赖仅在对应组件任务需要时按组件文档安装。

不要复用旧目录中的 `.venv`、`node_modules`、`target` 或其他构建缓存。

## 初始化

```bash
git clone --recurse-submodules https://github.com/xiangzuodalao/ifactory-platform.git
cd ifactory-platform
./scripts/bootstrap.sh
./scripts/doctor.sh
```

`bootstrap.sh` 只初始化缺失的 submodule、同步 submodule URL，并为 CMMS/ThingsBoard 补齐 `upstream`。它不会切换已初始化组件的分支、安装依赖或启动服务。重复执行是安全的；若已存在的 `upstream` 指向其他地址，脚本会停止并要求人工核对。

日常应从仓库根目录启动 Codex，使根 `AGENTS.md`、项目 `.codex/config.toml` 和 `.agents/skills` 同时生效。只处理单个组件时仍可进入组件目录运行其构建命令，但 Git 提交必须留在该组件仓库。

## 运行 PDM MCP

先按 `components/pdm-algorithm` 自身文档启动 PDM FastAPI，再从总仓运行 STDIO MCP：

```bash
PDM_API_BASE_URL=http://127.0.0.1:10021 \
  ./scripts/run-digital-mcp.sh
```

脚本使用 `components/digital-mcp/uv.lock` 的生产依赖启动兼容入口 `pdm-mcp`。STDIO 标准输出属于 MCP 协议，不要把它作为普通终端日志解析。

项目 Codex 配置默认应保持：

```text
PDM_ENABLE_TRAIN=false
```

只有明确需要受控训练时才在本地环境启用，并严格执行“预检 → 下一轮确认 → 单次训练 → 状态/预测验证”。训练超时、断线或 5xx 后不得再次提交同一任务。

## 常见检查

```bash
./scripts/doctor.sh
git status --short
git submodule status --recursive
```

`doctor.sh` 是只读检查：验证四个 submodule、remote、Skill 链接和元数据、项目 Codex/MCP 配置以及轻量工具。它不会下载依赖、访问上游或修改 Git 状态。

本工作区没有独立 Agent 服务，也不需要启动 OnCall-Agent。后续能力通过单用途 Skill 和 `digital-mcp` 中边界清晰的工具扩展。
