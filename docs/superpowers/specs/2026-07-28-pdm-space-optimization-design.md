# PDM 空间优化设计

## 目标

在不丢失 PDM 配置、训练数据和模型产物的前提下，完成三件事：

1. 回收当前未被运行服务使用的 Docker 构建缓存。
2. 停止旧 `integrations/pdm_mcp` 进程，退役旧 PDM Python 环境，并清理不再使用的 uv 缓存。
3. 将 PDM 默认运行镜像改为可复现的 CPU 镜像，消除 CUDA 12/13 重复依赖和开发工具进入生产镜像的问题。

本次不实现 GPU 镜像、训练 Worker、队列或服务拆分。CPU 镜像继续支持现有训练与预测接口，只是训练速度需要通过烟测记录，不承诺 GPU 级吞吐。

## 已确认基线

- 根文件系统 256 GB，已使用约 198 GB，可用约 45 GB。
- `valeo-pdm:latest` 的 Docker unique size 约 15.58 GB；容器可写层仅约 64 KB。
- 镜像内 `/app/.venv` 约 9.1 GB，其中 NVIDIA 库约 5.8 GB、Torch 约 1.8 GB、Triton 约 641 MB。
- Docker 当前报告 13.62 GB 构建缓存可回收，其中约 11.7 GB 是 PDM 构建使用的 uv cache mount。
- 旧 PDM `.venv` 与全局 uv 缓存大量硬链接共享；不能把两个目录的 `du` 数字相加，也不能声称单删 `.venv` 会释放其账面大小。
- `artifacts/` 约 5.4 MB、`data/` 约 48 KB，不是空间问题来源。
- 当前容器使用一个 Uvicorn worker，内存约 944 MiB，未获得 NVIDIA DeviceRequest，`torch.cuda.is_available()` 为 false。
- 旧 `integrations/pdm_mcp` 仍有多个进程；新的 `components/digital-mcp` MCP 也在运行。

## 方案比较

### 方案 A：只清缓存

优点是风险最低且无服务停机；缺点是 15.58 GB 运行镜像仍然保留，下一次构建还会重新生成重复 CUDA 环境。它只能作为第一阶段，不是根治方案。

### 方案 B：保留单体 GPU 镜像，只删除 CUDA 13

可以减少一部分空间并保留未来 GPU 能力，但当前主机没有可用 GPU，常驻服务仍承担数 GB 无效依赖。它也会继续把训练环境和 API 运行环境绑定在一起。

### 方案 C：默认 CPU 镜像，GPU 后续独立建设

这是本次采用的方案。当前服务本来就在 CPU 模式运行，因此它与现状最一致；同时可以把 CUDA、Triton、未实现的 xLSTM 和开发工具从生产镜像移除。未来确需 GPU 时另建单 CUDA 版本镜像，不重新污染默认 API 镜像。

## 设计

### 1. 无停机缓存回收

先记录 `df`、`docker system df`、PDM 容器状态和 `/healthz`。只清理 24 小时前且不被当前镜像使用的 BuildKit 缓存，不执行 `docker system prune -a`，不删除任何镜像、容器、卷、`artifacts/`、`data/` 或 `configs/`。

清理后立即再次检查：

- PDM 容器仍为 `running/healthy`；
- `http://127.0.0.1:10021/healthz` 返回 200；
- 根盘可用空间增加；
- Docker 构建缓存可回收量下降。

### 2. 旧 MCP 与环境退役

只终止命令行明确指向
`/home/vm/code/PDM_Algorithm/integrations/pdm_mcp`
的旧 `uv`/`pdm-mcp` 进程。先发送 `SIGTERM`，等待退出；只有仍未退出的精确 PID 才考虑 `SIGKILL`。不得终止命令行指向 `components/digital-mcp` 的新 MCP。

停止旧进程后验证新的 `digital-platform` MCP 仍能调用 PDM 健康检查。随后删除以下可重建目录：

- 旧 PDM 主 `.venv`；
- 旧 `integrations/pdm_mcp/.venv`。

保留旧仓的源码、Git 历史、`configs/`、`data/` 和 `artifacts/`，因为当前容器仍从兼容路径绑定这些运行状态。最后运行 `uv cache prune`，只回收 uv 判定为悬空的缓存；不执行未经范围核对的全局 `uv cache clean`。

### 3. CPU 生产镜像

PDM 的 `pyproject.toml` 改为只声明真实的一阶运行依赖：

- FastAPI/Uvicorn 与 HTTP；
- NumPy/Pandas/Matplotlib；
- PostgreSQL、SQL Server/ODBC；
- YAML、文件锁和训练进度；
- CPU 版 PyTorch。

`pytest`、Ruff 等移入开发依赖。Jupyter、debugpy、Notebook 转换工具、未被源码导入的 `transformers`、`xlstm`、`mlstm-kernels`、`torchvision`，以及所有显式 NVIDIA/CUDA/Triton 传递依赖不进入生产组。

CPU Torch 通过 uv 的显式 PyTorch CPU 索引锁定，`uv.lock` 成为唯一依赖来源。Dockerfile 只执行一次冻结的生产同步，不再在同步后额外运行 `uv pip install torch torchvision`。

保留 Ubuntu 24.04 与现有 ODBC 兼容性，先解决占用最大的 Python/CUDA 层。仅在验证没有运行时依赖后移除 `mssql-tools18`、`unixodbc-dev` 等构建/诊断包；这些百 MB 级优化不得增加数据库驱动风险。

默认 Compose 不再请求 NVIDIA 设备。默认 Uvicorn worker 保持为当前实际运行值 1，避免镜像切换同时改变内存模型。

## 测试策略

基础设施契约测试先失败、后实现，至少断言：

- 生产依赖不包含 `nvidia-*`、CUDA 12/13、Triton、xLSTM、TorchVision、Jupyter、pytest 或 Ruff；
- Torch 绑定显式 CPU 索引；
- Dockerfile 不存在第二条 Torch 安装路径；
- Dockerfile 使用冻结的生产依赖同步；
- 默认 Compose 不请求 GPU。

实现后执行：

1. PDM 默认 pytest 套件和 Ruff。
2. 构建候选 CPU 镜像。
3. 在候选镜像中检查 `torch.cuda.is_available() == false`、核心包导入、`/healthz` 和模型列表。
4. 使用隔离端口和临时产物目录完成候选容器烟测。
5. 记录候选镜像和 `/app/.venv` 大小，与 15.58 GB/9.1 GB 基线比较。
6. 切换当前容器后再次验证健康检查、MCP 健康工具和现有模型读取。

不执行完整生产训练；使用现有测试和最小 CPU 训练/预测烟测证明路径可运行，避免无授权的长时间训练写操作。

## 切换与回滚

候选镜像使用独立 tag 构建和测试。切换前给当前 image ID 增加临时回滚 tag；候选验证通过后才替换 `valeo-pdm:latest` 并重建当前容器。

若健康检查、模型读取或 MCP 调用失败：

1. 停止候选容器；
2. 将回滚 tag 重新标记为 `valeo-pdm:latest`；
3. 使用原绑定路径重建容器；
4. 验证 `/healthz` 后停止本轮清理，不删除旧镜像。

新镜像稳定且所有验收通过后，才删除临时回滚 tag 和未被使用的旧镜像层。

## 验收标准

- PDM API、现有模型列表和 `digital-platform` MCP 健康检查保持可用。
- `configs/`、`data/`、`artifacts/` 的文件数量和校验和不变。
- 旧 `integrations/pdm_mcp` 进程为零，新 MCP 不受影响。
- 生产锁文件中不再解析 CUDA 12/13、NVIDIA 或 Triton 包。
- 候选 CPU 镜像显著小于当前 15.58 GB；目标为 3 GB 以内，若超过则按实测报告而不虚报完成。
- 完整测试、契约测试、Ruff、候选容器烟测和切换后健康检查全部通过。
- 清理前后磁盘与 Docker 占用均有记录，最终释放量使用实测值报告。
