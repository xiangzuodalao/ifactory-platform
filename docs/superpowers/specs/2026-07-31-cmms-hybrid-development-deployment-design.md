# CMMS 开发优先混合部署设计

## 文档状态

- 日期：2026-07-31
- 状态：架构方向已批准，等待书面设计复核
- 目标环境：本机隔离开发与 Phase 2 Shadow 验收
- CMMS 源码：总仓 `components/cmms` 当前记录的组件提交

## 目标

在不影响现有 ThingsBoard、PDM 和其他 Docker 工作负载的前提下，部署一套可快速开发、可调试、可复现并能满足预测维护 Phase 2 资格检查的本地 CMMS。

采用以下运行边界：

- CMMS Spring Boot API 和 React 开发服务器运行在宿主机，便于调试、前端 HMR 和快速重启；
- PostgreSQL、MinIO 和统一 Nginx 网关运行在独立 Docker Compose 项目中；
- 宿主机浏览器使用规范公共 origin `http://cmms.localhost:3000`，宿主机健康探针可使用 `http://127.0.0.1:3000`，Compose 中的 API 调用方使用 `http://host.docker.internal:3000`；三者进入同一个 Nginx 网关且都不感知开发进程端口；
- 最终 Phase 2 验收必须使用干净、已提交且与总仓 gitlink 一致的 CMMS 代码；
- CMMS、ThingsBoard、PDM 和 `platform-integration` 只通过稳定 API 与契约协作，不跨服务直连数据库。

## 当前基线

- ThingsBoard 后端已占用宿主机 `127.0.0.1:8080`。
- ThingsBoard PostgreSQL 容器已占用宿主机 `127.0.0.1:5432`。
- PDM API 容器已占用宿主机 `127.0.0.1:10021`。
- CMMS 当前未运行，宿主机 `3000` 没有监听进程，也没有 CMMS 容器或数据卷。
- CMMS 原始 Compose 使用预构建 API/前端镜像，并硬编码全局 `container_name`，不适合作为本项目的隔离开发栈。
- 当前没有 CMMS 运行配置、有效许可证、隔离公司或 API 凭据。
- CMMS API Key 同时要求许可证 entitlement 和公司订阅计划 feature 包含 `API_ACCESS`；最小权限 API Key 还需要 `CUSTOM_ROLES` entitlement 与公司计划的 `ROLE` feature，以便创建专用角色并在 key 生成后移除临时 `SETTINGS`。部署流程不得绕过这些检查或直接修改数据库。
- 当前宿主机默认工具是 Java 25、Maven 3.6.3 和 Node 26.5.0，不等于 CMMS 源码使用的 Java 17、Maven 3.9.3 和 Node 21.6.1 组合；服务不得依赖 ambient `PATH` 猜测工具版本。
- 用户级 systemd manager 可用，但当前因无关的 ThingsBoard 日志轮换 unit 处于 degraded；CMMS 部署只报告该状态，不修改或重启 ThingsBoard unit。

## 非目标

本设计不包含：

- 生产环境高可用、TLS 终止、域名或公网暴露；
- 自动购买、生成或绕过 Atlas CMMS 许可证；
- 直接修改 CMMS 数据库来创建公司、用户、API Key、资产或工单；
- 自动执行 Phase 2 provisioning、Dashboard 发布、遥测注入、模型训练、Alarm 或工单写入；
- 复用 ThingsBoard 的 PostgreSQL 容器、数据库或数据卷；
- 使用上游 `intelloop/atlas-cmms-backend` 或 `intelloop/atlas-cmms-frontend` 预构建镜像证明当前源码版本；
- 在本次部署设计中修改 CMMS 业务代码。

## 方案比较与决策

### 方案 A：宿主机前后端，加容器化状态服务

优点是 Java 调试、前端 HMR 和代码重启最快；缺点是如果没有稳定入口和进程管理，调用方会依赖开发端口，运行状态也不容易审计。

### 方案 B：全部使用源码构建的容器

优点是环境最一致、适合 CI 和发布；缺点是日常修改需要重建镜像，ThingsBoard、CMMS 与 PDM 同时开发时反馈周期较长。

### 方案 C：稳定网关下的混合部署

宿主机运行 CMMS API 和前端，Docker 运行 PostgreSQL、MinIO 与 Nginx。Nginx 使用当前 Linux 开发机支持的 host network，在宿主机 loopback 和 Docker host-gateway 两个精确地址上监听 `3000`，并通过 `127.0.0.1` 访问宿主机开发进程和 MinIO 的 loopback 发布端口。

采用方案 C。它保留方案 A 的开发效率，同时为 Phase 2 提供与全容器部署相同的稳定 HTTP origin。全容器源码构建保留为后续 CI、预发布和生产候选方案，不作为本地日常开发前置条件。

## 总体架构

```text
浏览器 -----------------> cmms.localhost:3000 ------+
                                                    |
platform-integration --> host.docker.internal:3000 -+
                                                    v
                                            Nginx 容器
       |      |       |
       |      |       +-- /storage/ --> MinIO 容器:9000
       |      +---------- /api/ -----> 宿主机 CMMS API:8082
       +----------------- / ---------> 宿主机 React:3001

宿主机 CMMS API:8082
       |      |
       |      +----------> MinIO 容器，经 127.0.0.1:9000
       +-----------------> PostgreSQL 容器，经 127.0.0.1:5433
```

固定端口：

| 服务 | 宿主机绑定 | 说明 |
|---|---:|---|
| CMMS 公共入口 | `cmms.localhost:3000`/`127.0.0.1:3000` 和精确 Docker host-gateway 的 `:3000` | 宿主机与容器调用方进入同一网关 |
| CMMS React 开发服务器 | `127.0.0.1:3001` | 只由 Nginx 转发 |
| CMMS Spring Boot API | `127.0.0.1:8082` | 避免与 ThingsBoard `8080` 冲突 |
| CMMS PostgreSQL | `127.0.0.1:5433` | 避免与 ThingsBoard PostgreSQL `5432` 冲突 |
| CMMS MinIO API | `127.0.0.1:9000` | API 上传和对象访问 |
| CMMS MinIO Console | `127.0.0.1:9001` | 仅本机运维使用 |

实现前的只读 preflight 必须确认这些端口仍可用。若被其他进程占用，流程停止并报告精确冲突，不自动终止或替换进程。

## 组件设计

### 1. Docker 基础设施

总仓新增独立 Compose 文件，项目名固定为 `ifactory-cmms-dev`，只包含：

- `postgres`：固定 PostgreSQL 16 的精确镜像 tag 和 digest，使用独立数据库和命名卷；
- `minio`：固定与当前 CMMS 兼容的精确 MinIO release tag 和 digest，使用独立命名卷；
- `nginx`：固定 Nginx 1.27 的精确镜像 tag 和 digest，挂载只读开发网关配置。

Compose 不使用 `container_name`，不声明外部卷，不连接 ThingsBoard 网络，也不接收不需要的 CMMS API 密钥或许可证。PostgreSQL 和 MinIO 的宿主机端口只绑定 `127.0.0.1`；Nginx 是唯一例外，它同时绑定 loopback 和只供本机容器访问的精确 Docker host-gateway 地址，不绑定 LAN 接口或 `0.0.0.0`。

Nginx 单独使用 Linux `network_mode: host`。启动脚本从 Docker 默认 bridge 解析网关 IPv4，确认它属于本机 Docker bridge 且不是 wildcard/LAN 地址，再把无秘密模板渲染到 `.runtime/`。运行配置显式监听 `127.0.0.1:3000` 和该精确网关的 `:3000`，并通过 `127.0.0.1:3001`、`127.0.0.1:8082` 和 `127.0.0.1:9000` 访问前端、API 和 MinIO。PostgreSQL 与 MinIO 继续使用 Compose 的隔离桥接网络；只有各自明确声明的 loopback 端口可以被宿主机访问。Nginx 不加入该桥接网络，也不使用容器服务名。获准启动后的隔离容器烟测必须再证明 `host.docker.internal:host-gateway` 实际解析到同一地址；不一致时停止，不放宽监听范围。公共路由保持 CMMS 当前单域语义：

- `/` 转发 React 开发服务器并保留 WebSocket Upgrade，支持 HMR；
- `/api/` 转发 Spring Boot，并移除外部 `/api` 前缀；
- `/storage/` 转发 MinIO，并保留大文件上传所需配置。

`cmms.localhost` 是 CMMS 返回给浏览器的规范公共主机名。preflight 必须证明它在宿主机解析为 loopback；任何需要解引用 CMMS 绝对公共 URL 的隔离容器都必须通过精确 `extra_hosts` 把 `cmms.localhost` 映射到同一个 Docker host-gateway。Phase 2 的 CMMS API base URL 仍保持既有固定值 `http://host.docker.internal:3000`，不依赖容器 DNS 自动理解 `.localhost`。

MinIO SDK 先针对内部 endpoint `http://127.0.0.1:9000` 生成签名，再由现有 CMMS 代码把 origin 替换为公共 endpoint。为保持 SigV4 有效，Nginx 的 `/storage/` 必须剥离前缀并把转发 `Host` 精确重写为签名时的 `127.0.0.1:9000`。运行烟测必须证明同一条签名 URL 可从宿主机下载，并在带精确 `cmms.localhost:host-gateway` 映射的隔离容器中下载。

Nginx 可以先于宿主机进程启动；此时 `/api` 或 `/` 返回 `502` 只表示对应开发进程未就绪，不得误报整个基础设施已通过验收。

### 2. 宿主机 CMMS API

API 从 `components/cmms/api` 使用 JDK 17 和 Maven 启动。运行时显式覆盖：

- `SERVER_PORT=8082`；
- `SERVER_ADDRESS=127.0.0.1`；
- PostgreSQL 地址为 `127.0.0.1:5433/atlas`；
- MinIO 地址为 `http://127.0.0.1:9000`；
- `PUBLIC_API_URL=http://cmms.localhost:3000/api`；
- `PUBLIC_FRONT_URL=http://cmms.localhost:3000`；
- `PUBLIC_MINIO_ENDPOINT=http://cmms.localhost:3000/storage`。

受控 launcher 根据受限运行环境文件中的绝对引用读取数据库、MinIO、JWT 和许可证 secret file，再把值仅导出给 API 进程。启动器不得把环境文件、secret file 内容、命令环境或认证头写入日志。

普通 Java 修改采用重新编译并重启 API 进程的方式生效。当前工程没有承诺任意类的真正 HotSwap；“热更新”在本设计中指快速构建、可控重启和稳定网关不变。

### 3. 宿主机 CMMS 前端

前端从 `components/cmms/frontend` 使用锁文件和 `npm ci --legacy-peer-deps` 安装依赖并运行 React 开发服务器：

- 通过 `HOST=127.0.0.1`、`PORT=3001` 监听 `127.0.0.1:3001`；
- API URL 使用同源 `/api`；
- 不直接读取 CMMS API、数据库、MinIO 或许可证秘密；
- `public/runtime-env.js` 和本地 `.env` 保持在组件既有忽略规则内，不提交。

前端代码修改通过 React HMR 生效。依赖或构建配置变化允许重启前端进程，但公共入口始终保持 `3000`。

### 4. 进程管理

使用用户级 systemd 管理两个宿主机进程：

- `ifactory-cmms-api.service`；
- `ifactory-cmms-frontend.service`。

另有不常驻的安全辅助 unit：

- `ifactory-cmms-fail-closed.service`；
- 离线许可证模式使用的 `ifactory-cmms-license-guard.service`/`.timer`。

仓库只保存无秘密的 unit 模板。安装脚本根据当前总仓绝对路径把 unit 渲染到 `.runtime/systemd/`，再通过 `systemctl --user link` 注册；默认不启用开机自启。

API unit 读取只含非秘密配置和 secret file 引用的 CMMS 环境文件，再由受控 launcher 校验并读取秘密。前端 unit 只读取单独的非秘密前端环境文件，避免继承 API 或基础设施凭据。

API unit 必须使用 `Restart=no`，禁止 systemd 在双地址 gateway 仍开放时自动创建新 API 进程。受控编排在关闭并验证 gateway、完成 preflight 和在线预算写前计数后，创建一个权限 `0600`、单链接、非符号链接、最长有效 60 秒的一次性 start permit。permit 绑定计划哈希、随机 nonce、CMMS SHA/dirty fingerprint、预期 unit generation、loopback-only Nginx 配置摘要、许可证模式/预算 debit ID 和本次执行者 UID，不包含秘密。

API unit 的 `ExecStartPre` 必须调用 fail-closed gate：验证并原子消费 permit，再独立证明实际 Nginx 只监听 loopback、Docker host-gateway 不可达、预期源码/工具链/许可证模式和预算 debit 一致。缺 permit、permit 过期/复用、任一状态不匹配或检查异常时，unit 启动失败并停止 Nginx。这样直接 `systemctl --user start/restart ifactory-cmms-api.service` 不能绕过受控入口。

API unit 的 `ExecStopPost` 在启动失败、clean exit、显式 stop/restart、异常退出等所有结束路径调用无秘密 fail-closed one-shot，把 Nginx 切回 loopback-only；无法安全 reload 时直接停止 Nginx。`OnFailure` 只作为重复保险和脱敏状态记录，绝不重启 API。后续 API 启动只能重新生成 permit 并走受控 `start`/`restart` 编排。前端不执行数据库初始化，可以使用带明确节流的 `Restart=on-failure`。

开发者修改 API 后执行受控重启，修改前端代码则依赖 HMR。状态命令同时报告 Compose 服务、systemd unit、端口和 HTTP 健康状态，但不输出环境值。

### 5. 隔离宿主机工具链

宿主机进程使用项目专用、校验和固定的工具链，不修改系统 Java/Maven、全局 Node 默认版本或现有 ThingsBoard 服务：

- Java 固定为 Eclipse Temurin JDK 17.0.19+10；
- Maven 固定为 CMMS API Dockerfile 使用的 3.9.3；
- Node 固定为 CMMS 前端 Dockerfile 使用的 21.6.1，并使用锁文件对应的 npm 执行 `npm ci --legacy-peer-deps`；不得改用会重写锁文件的 `npm install`。

跟踪的工具链 manifest 记录官方发行 URL、版本和 SHA-256。bootstrap 只把通过校验的归档解压到 `.runtime/toolchains/`，禁止 `curl | sh`、未固定 latest URL、系统包替换或静默回退到 ambient `PATH`。此后任何工具链升级都必须作为独立变更审查。

systemd unit 使用这些工具的绝对路径。若 bootstrap 尚未获准、下载不可用或校验失败，部署停止并报告缺失工具，不尝试用 Java 25、Maven 3.6.3 或 Node 26 继续。

## 配置与秘密

跟踪的示例文件只包含键名、非秘密默认值和空秘密字段。真实运行状态全部位于根仓已忽略的 `.runtime/`：

```text
.runtime/
├── cmms-bootstrap.env
├── cmms-development.env
├── cmms-frontend.env
├── cmms-development-state.json
├── cmms-license-online-budget.json
├── cmms-development-nginx.conf
├── receipts/
├── secrets/
├── start-permits/
├── systemd/
└── toolchains/
```

`cmms-development.env` 必须是当前用户拥有、单链接、非符号链接的普通文件，权限为 `0600`。它只包含 API/基础设施非秘密配置和 `.runtime/secrets/` 下精确 secret file 的绝对引用：

- PostgreSQL 用户、数据库名和密码文件引用；
- MinIO 用户/密码文件引用；
- JWT 密钥文件引用；
- Atlas 许可证密钥文件引用、可选的合法离线许可证文件路径，以及许可证要求的 fingerprint 配置；
- 计划中的唯一隔离管理员标识，用于 `ALLOWED_ORGANIZATION_ADMINS` 限制首次注册；
- 固定启用 `INVITATION_VIA_EMAIL=true`，使带角色的注册必须命中正式邀请记录；固定关闭 `ENABLE_EMAIL_NOTIFICATIONS=false`，本地 bootstrap 只创建邀请记录，不发送邮件；
- API 所需的其他关闭或空置的本地集成功能配置。

`cmms-bootstrap.env` 具有相同的所有者、文件类型和 `0600` 约束，只保存隔离管理员/运行用户 email、角色/API Key 标签等非秘密身份计划，以及以下三组 bootstrap credential slot 的绝对引用：

- 超级管理员密码的 `current` 与可选 `candidate`；
- 隔离公司管理员密码的 `current` 与可选 `candidate`；
- 运行用户密码的 `current` 与可选 `candidate`。

每个待创建或待轮换的新密码都必须在相应远端写操作之前，通过受限交互或单文件描述符原子写入 `.runtime/secrets/<identity>.candidate`；既有 `.current` 在结果确认前保持不变，preflight 必须在不输出值的情况下拒绝 candidate 与 current 相同。首次超级管理员轮换把源码公开的默认密码视为只读的隐式 current，不把它复制到运行文件；尚未创建的公司管理员和运行用户允许 current 缺失。请求完成或响应不确定后，控制进程分别尝试 candidate 与 current：

- 只有 candidate 可认证时，确认远端已采用新密码，才把 candidate 原子提升为 current；
- 只有 current 可认证时，确认远端仍使用旧密码，才删除 candidate 或在新的 repair plan 中重试；
- 两者都可认证、两者都不可认证或无法完成两次认证时，保留两槽、保持 gateway 关闭并停止；
- 对尚不存在的用户，正式 API 确认用户仍不存在时保留 candidate，等待已确认的创建/修复步骤；candidate 可认证后再提升为 current。

上述 current/candidate 协议适用于首次 bootstrap 和以后所有超级管理员、公司管理员、运行用户密码轮换，修复流程不得只依赖响应状态判断成功。所有 slot 都不得出现在聊天、Git、命令行参数、stdout 或 journal。`cmms-bootstrap.env` 只由 bootstrap/reconcile 控制进程读取，绝不作为 systemd `EnvironmentFile`，身份密码也绝不导出给 CMMS API 或前端进程。current 在本地部署存续期内保留，供冷启动认证和部分失败修复；普通 stop/restart 不删除。只有经独立确认的凭据轮换、部署销毁或身份吊销流程才能提升或删除 slot。

这里的“可认证/不可认证”只接受来自当前 loopback API 的确定性认证结果；连接错误、超时、`5xx`、无法证明请求命中当前 MainPID 或其他不确定结果一律保留两槽并停止。

每个 secret file 都必须是当前用户拥有、单链接、非符号链接、限长的普通文件，权限为 `0600`。PostgreSQL 使用官方 `POSTGRES_PASSWORD_FILE`，MinIO 使用官方 `MINIO_ROOT_USER_FILE`/`MINIO_ROOT_PASSWORD_FILE`，使状态容器的 Docker 配置只出现 secret 路径而不是值。CMMS API 目前只接受普通环境变量，因此受控 API launcher 仅从已验证的数据库、MinIO、JWT 和许可证 key secret file 读取并导出到 API 进程环境，不读取 bootstrap 身份密码；若提供离线许可证文件，launcher 只把其受限绝对路径作为 `LICENSE_FILE_PATH` 传给 API，不把文件内容复制进环境。这意味着同一 Unix 用户或具有 Docker/调试权限的主体属于本地可信边界。设计承诺的是“运行文件是唯一操作者管理的持久秘密来源，日志、状态和 Docker 配置不复制秘密值”，而不是不现实的“秘密只存在于文件”。

`cmms-frontend.env` 只包含同源 API URL、端口和非秘密 UI 开关，也使用 `0600`，以简化统一校验。

`cmms-development-state.json` 不保存秘密，只记录 Compose 项目名、期望 CMMS git SHA、实际启动 SHA、systemd MainPID/进程启动标识、解析后的 Docker host-gateway、启动时间和固定端口。状态文件不能替代 Git、运行进程和 API 验证。验收时必须把 unit MainPID、进程工作目录、启动记录、当前干净 SHA 与总仓 gitlink 重新交叉核对。

`cmms-license-online-budget.json` 是不含秘密、原子更新的保守预算账本，只在在线 license key 模式使用。它记录 API 进程使用的 `LocalDate`/时区、由受控 launcher 写前计入的新进程尝试数、每日上限和连续性标识；不声称等于 CMMS 内部 `KeygenRequestTracker`。离线许可证模式明确标记为不消耗该在线启动预算。

`start-permits/` 只保存尚未消费且短时有效的 API start permit；控制脚本和 `ExecStartPre` 通过同一目录内原子 rename 实现单次消费。启动完成、失败或 permit 过期后必须清理精确 permit，不能把目录存在本身当作授权。

`cmms-development-nginx.conf` 由跟踪的无秘密模板渲染，只允许出现 loopback 与本轮核验的 Docker host-gateway；不得接受操作者提供的任意监听地址。

Phase 2 的 `.runtime/predictive-maintenance-shadow.env` 独立管理，只保存 CMMS API Key 的严格凭据 envelope；不得把 Atlas 许可证、管理员密码、数据库密码或 MinIO 密码复制进去。该 envelope 是 API Key 的预期持久运行副本，因此不受“基础设施 secret file”措辞混淆。

## 启动、初始化与停止流程

所有会启动、停止、重启或修复 CMMS 的命令都遵守同一个 fail-closed 不变量：第一项有状态动作必须把 Nginx 渲染或 reload 为 loopback-only，并证明 Docker host-gateway 的 `:3000` 不可达；后续任一 preflight、认证或资格检查失败都保持该状态。只有当前 API 完成全部对账后，最后一步才可开放双地址 listener。

### 一次性 bootstrap

1. 只读检查 Docker、Compose、项目专用 JDK 17/Maven 3.9.3/Node 21.6.1、端口和工作树；解析并核验 Docker 默认 bridge gateway。缺少工具链时只输出待执行的校验和固定 bootstrap 计划，并在授权门停止。
2. 核对总仓记录的 CMMS gitlink 与组件实际 HEAD；开发模式报告 dirty 状态，验收模式必须拒绝 dirty 或不匹配的组件。
3. 校验三个运行环境文件、全部 secret file 和三组 credential slot 的所有者、类型、链接数、权限、大小、必需键与合法状态，不输出值；在任何远端身份创建或密码轮换前，必须已经安全持久化对应 candidate，并保留既有 current。
4. 生成规范的 CMMS deployment/bootstrap plan。计划精确列出源码 SHA、工具链与镜像摘要、Compose 项目/卷、端口、仅 loopback 的 bootstrap Nginx 配置、隔离公司管理员标识、集成角色权限、邀请对象、API Key 标签、离线/在线许可证模式、在线模式的日期/时区与保守启动预算、预期外部请求/本地计数副作用和预期零工单结果；秘密值只以“已安全提供/缺失”表示。输出计划哈希后停止，等待后续一轮明确确认。
5. 确认后执行 `docker compose config -q`，确认没有预构建 CMMS API/前端镜像、全局容器名、LAN/wildcard 监听或计划外对象。
6. 首先启动 PostgreSQL、MinIO 和只监听 `127.0.0.1:3000` 的 bootstrap Nginx。Docker host-gateway 此时没有 listener。
7. 按选定许可证模式准备 API 和前端；在线模式先原子写前计入一次保守预算，离线模式先证明 IP 过滤与 license guard 可强制执行。创建一次性 start permit 后才通过 systemd 启动 API。API 首次启动执行 Liquibase，并创建 CMMS 默认订阅计划和超级管理员；等待 loopback `/api/health-check` 和许可证状态成功。
8. 在同一次已确认 apply 中立即通过 CMMS 正式 API 完成超级管理员密码轮换、隔离公司管理员注册、专用集成角色、精确邀请、运行用户、API Key 创建和临时权限移除。任何步骤失败都停止 API/Nginx，并保持 gateway listener 关闭。
9. 使用新建的隔离身份验证许可证 entitlement、公司订阅 feature、邀请门、运行身份权限、API Key 能力和零工单基线。
10. 只有第 8–9 步全部成功后，才把 Nginx 配置渲染为同时监听 loopback 与已核验 Docker host-gateway，受控 reload 后执行宿主机和隔离容器双路径烟测，并把非秘密状态写为 `GATEWAY_ENABLED`。

设计批准不等于 deployment/bootstrap apply 授权；实际执行必须使用后续返回的精确计划哈希确认。bootstrap 只允许在尚未完成初始化或经对账确认的部分状态上执行，不能作为普通启动命令调用。该 apply 不创建资产、遥测、Alarm 或工单，也不执行 Phase 2 provisioning。

首次初始化会产生源码中公开已知的默认超级管理员凭据。它存在期间，Nginx 只能监听 loopback，Docker gateway 上不得开放登录或任何 API 路由。密码通过请求体或本地受限文件传入正式密码更新端点，不得写入聊天、Git、命令行参数或日志。

### 日常启动与重启

receipt 中的 `GATEWAY_ENABLED` 只能证明上一轮完成，不能授权本轮直接开放 gateway。日常 `start`/`restart` 必须先恢复 loopback-only 状态，再由当前正式 API 重新证明资格：

1. 无论 Nginx 是否正在运行，都先把待启动配置原子渲染为只监听 `127.0.0.1:3000`；若正在运行则立即 reload。从隔离测试容器证明 Docker host-gateway 的 `:3000` 已不可达后，才继续。若无法证明 listener 已关闭，则停止。
2. 重做端口、工具链、镜像摘要、环境文件、secret file、CMMS SHA、许可证模式/预算和 Docker host-gateway preflight；receipt 缺失、未记录 `GATEWAY_ENABLED` 或在线预算不是已知可用时，保持 gateway 关闭并生成 repair plan。
3. 启动或重启 PostgreSQL、MinIO、loopback-only Nginx 和前端；在线模式在创建 API 新 MainPID 前原子写前计入一次预算，离线模式先验证强制网络 profile 与 guard。随后创建并由 `ExecStartPre` 消费一次性 start permit，再启动 API。此时 receipt 无论记录什么都不得开放 gateway。
4. 只通过 loopback 对当前 API/数据库做不改变计划身份配置的认证与读取对账：验证计划中的已轮换超级管理员、隔离公司管理员和运行用户凭据可认证，源码公开的默认超级管理员凭据已失效，并核验许可证、公司、邀请门、运行用户、角色最终权限和 API Key 元数据。只有尚未执行 Phase 2 provisioning 的 bootstrap readiness 才要求零工单；日常启动不得把合法的后续工单误判为部署故障。
5. 只有第 4 步全部通过，才渲染双地址配置并 reload Nginx；随后核验宿主机与隔离容器公共路径命中同一实例。

当前 CMMS API 启动并非数据库只读。与已审查 CMMS 基线及启动副作用敏感文件 manifest 绑定的允许副作用白名单包括：

- Liquibase 执行该提交声明的 schema migration；
- `ApplicationInitializer` 在缺失时补建内置超级管理员/订阅计划，更新内置默认角色，并修正临时时区值；
- 许可证验证访问已配置的 Keygen 服务，并更新本地 `KeygenRequestTracker` 计数；
- signin 更新 `lastLogin`，API Key 认证按实现节流更新 `lastUsed`。

这些是 CMMS 自己拥有的初始化、维护和审计写入，不授权编排脚本直连数据库。日常编排本身禁止 signup、邀请、角色创建/修改、密码轮换、API Key 创建/吊销或领域对象写入。gateway 开放前必须通过正式 API 证明计划中的公司、定制集成角色、用户和 API Key 仍保持精确身份与权限；在受控重启测试中还要证明资产和工单集合未被启动流程改变。实现必须维护一份跟踪的“启动副作用敏感文件”清单，至少覆盖 migration、`ApplicationInitializer`、许可证与认证过滤器及其直接写入依赖；这些文件有未审查变化时，API 最多以 loopback-only 运行供诊断，gateway 保持关闭。其他业务代码的 dirty 修改仍可在开发模式热迭代，但继续标记 `UNCOMMITTED`，不能生成验收回执。receipt 缺失、状态不一致或认证/读取对账失败时，不得把日常启动隐式升级为 bootstrap；流程停止并生成修复计划。

### 部分失败修复

部分 bootstrap 必须使用独立的 `repair plan`/`repair apply`，重新输出新的计划哈希并等待下一轮明确确认。repair 只可按日常流程先建立 loopback-only 运行态，所有“计划凭据”都从已验证的 bootstrap secret file 经受限文件描述符读取。修复计划逐步通过正式 API 读后再写：

| 步骤 | 认证/读取对账 | 允许的单次配置写入 |
|---|---|---|
| 首次超级管理员密码 | 依次核对 candidate、current（如有）与源码默认凭据；candidate 成功且默认失败才提升 | 仅在 candidate 已持久化、candidate 失败且默认凭据有效时调用正式密码更新端点一次；响应不确定时保留两槽并重新认证 |
| 任一既有身份密码轮换 | 确认 current 可认证、candidate 已持久化且二者不同 | 调用正式密码更新端点一次；随后按 current/candidate 双槽协议认证并提升、丢弃或停止 |
| 隔离公司 | 先用 current 或 candidate 登录并读取 company | 确认身份不存在时使用 candidate signup 一次；响应不确定时重新登录，candidate 成功后才提升 |
| 集成角色 | 按公司与精确角色名读取并比较权限 | 仅在不存在时创建；同名但权限不匹配时停止并另做变更计划 |
| 用户邀请 | 读取最近待处理邀请并匹配精确 email 与 role ID | 仅在不存在时调用正式 invite API 一次；冲突邀请时停止 |
| 运行用户 | 先用 current 或 candidate 登录并核对 company/role | 确认不存在时使用匹配邀请和 candidate signup 一次；响应不确定时重新登录，candidate 成功后才提升 |
| API Key | 按计划标签读取元数据并核对 owner/company | 仅在不存在时创建一次；原始 key 未捕获时按下述响应丢失规则停止 |
| 最终角色权限 | 读取角色并确认临时 `SETTINGS` 是否仍存在 | 仅在仍存在时移除一次；响应不确定时重新读取 |
| 网关开放 | 验证全部资格、未邀请注册负测、双路径前置条件 | 仅在全部通过时渲染并 reload 双地址 listener |

修复流程不得把“请求超时”解释为“写入失败”，也不得自动重试任何非幂等写操作。

### 停止

1. 先把 gateway 切回 loopback-only，停止 Nginx，并证明 Docker host-gateway listener 已关闭；
2. 停止前端和 API 用户服务；
3. 执行 Compose `stop` 停止 MinIO 和 PostgreSQL；
4. 保留数据卷、运行配置和状态记录。

普通停止或重启流程禁止执行 `down -v`、`docker volume rm`、数据库清空或 MinIO 删除。需要重置隔离数据时必须另行设计可恢复目标核验和独立确认门。

## 许可证、公司与 API Access

部署流程不生成许可证，也不提供开发旁路。启动前由操作员把有效许可证材料安全放入受限 secret file，并只在非秘密环境文件中保存该文件的绝对引用；不通过聊天、Git、命令行参数或日志传输。

许可证支持两个不改变 CMMS 业务逻辑的正式模式：

- **离线文件模式（开发环境优先，严格可预测模式必选）**：操作员同时提供合法的加密许可证文件和解密所需 license key。文件存在且可读时，API 通过 `LICENSE_FILE_PATH` 优先执行源码自带的本地验证，不调用 Keygen 在线校验，也不消耗在线请求计数。
- **在线 key 模式（受限 fallback）**：每个新 API 进程首次需要许可证状态时通常会调用 Keygen；进程内缓存只有 12 小时，缓存到期后的下一次资格查询还可能再次调用。源码内部硬限制为每个 API `LocalDate` 20 次成功校验。在线模式适合操作者接受运行期重校验限制的本地验证，不提供“运行期间绝不重试外部校验”的保证。

当前 CMMS 源码在离线文件缺失或不可读时会隐式回退到在线 key。为使离线模式不可回退，离线 API unit 必须启用 systemd IP 过滤：`IPAddressDeny=any`，只允许 `127.0.0.0/8` 与 `::1/128`，从 unit 层阻止任何 Keygen 或其他非 loopback 出站，同时保留对本机 PostgreSQL、MinIO 和 Nginx 的访问。preflight 必须用同一 user manager/profile 实测“loopback 可达、非 loopback 不可达”；若当前内核、cgroup 或用户级 systemd 不能强制该策略，离线严格模式不可用且 gateway 保持关闭，不能静默降级为在线模式。

离线模式同时启用 `OnUnitActiveSec=30s`、`AccuracySec=1s` 的 license guard。guard 校验离线文件仍是预期 owner/type/link count/mode/size/SHA-256，并从 loopback 读取 `/api/license/state`；文件或状态异常、guard 自身失败时立即调用 fail-closed one-shot，且不重启 API。guard 也只允许 loopback 网络。离线 API unit 通过 `BindsTo=`/`After=` 绑定 guard timer：timer 被直接停止或失活会停止 API，并由 `ExecStopPost` 关闭 gateway；gateway 开放前还必须证明 timer 正在使用预期配置。该 profile 因此明确关闭 SMTP、SSO 或其他需要 API 主动访问外网的可选功能；未来需要这些能力时必须另行设计精确 egress allowlist，而不能放开 wildcard 出站。

在线模式的受控 launcher 每次创建新 API 进程前，先在预算账本中写前计入一次尝试，再允许 systemd start；请求未发生或失败也不返还，以保持保守。受控新进程默认最多 10 次/日，至少保留另外 10 次给 12 小时缓存刷新、故障诊断和不可见的源码内部消耗。普通 `status`、已健康 MainPID 的幂等 `start` 和前端重启不计入；API `restart` 必须计入。

bootstrap/start plan 和脱敏状态输出必须显示 `offline`/`online` 模式、API 使用的日期/时区、本日受控尝试数、拟新增次数、10 次上限、保留余量，以及在线运行期重校验限制。账本缺失、损坏、日期连续性或启动来源无法证明时，预算状态为 `UNKNOWN`，不得自动创建新 API 进程；对于既有数据库，只能生成一次性、明确确认的恢复计划来消费一次未知预算，成功后当天仍按零剩余受控预算处理。全新且已证明为空的隔离卷可以从零初始化账本。由编排发起的启动校验失败必须保持 gateway 关闭，launcher 不自动重启或重试 API。编排不通过直连 CMMS 数据库读取或修改 `KeygenRequestTracker`。

gateway 已开放后，在线模式的 12 小时缓存重校验由当前 CMMS 业务请求触发；网络异常时，源码可能在后续请求再次访问 Keygen，预算账本无法观测或阻止。此时 CMMS 自身仍会把许可证状态判为无效并通过 entitlement 检查拒绝受限能力，Phase 2 readiness 必须失败，但编排不承诺立即关闭 gateway 或阻止源码内部重试。需要无在线依赖、严格可预测重启与运行期行为时必须选择离线文件模式；改变在线负缓存/健康信号属于未来 CMMS 业务代码设计，不在本次部署范围内。

deployment/bootstrap apply 使用以下闭环，不假设“既有受限身份”：

1. 先持久化超级管理员 candidate，使用默认超级管理员登录后，通过正式 `/auth/updatepwd` 端点在 loopback 上轮换密码；按 current/candidate 双槽认证确认后才提升 candidate；
2. 将无角色的公司注册限制到 `ALLOWED_ORGANIZATION_ADMINS` 中计划的单个隔离管理员标识，并保持 `INVITATION_VIA_EMAIL=true`；使用预先持久化的 candidate 通过正式 signup API 创建隔离公司，candidate 可认证后才提升为 current。响应不确定时先登录对账，不盲目重复 signup；
3. 验证该公司自动绑定的订阅计划 features 包含 `API_ACCESS` 和 `ROLE`；
4. 由隔离公司管理员通过正式角色 API 创建专用集成角色。创建 API Key 期间可临时包含 `SETTINGS`，最终只保留 `auth/me`、资产查询/创建和工单只读搜索所需权限；
5. 由隔离公司管理员调用正式 invite API，为计划中的运行用户 email 和精确 role ID 创建邀请记录。`ENABLE_EMAIL_NOTIFICATIONS=false` 只跳过邮件发送，不绕过邀请记录；
6. 在创建运行用户前，使用一个未被邀请的测试 email 和同一 role ID 调用 signup，必须得到 HTTP `406` 的“未受邀”响应，且查询确认没有创建该用户；随后只允许计划中的 email 使用匹配邀请和预先持久化的 candidate 完成 signup，candidate 可认证后才提升为 current，并核对其 company 和 role；
7. 使用运行身份创建一次性 API Key；取得 key 后立即由管理员通过正式角色 API 移除临时 `SETTINGS`，并验证该 key 无法访问设置管理接口；
8. 把一次性 API Key 通过单文件描述符、限长和原子替换写入权限 `0600` 的 Phase 2 凭据 envelope；目标存在时必须拒绝符号链接、重复键和计划外字段，并保留其他已验证配置，不在 stdout、journal 或状态文件中出现；
9. API Key 创建响应丢失时，先按标签读取元数据。若已存在但原始 key 未安全落盘，停止并要求新的修复计划精确删除/吊销该 key 后再创建；不得自动重试生成第二把 key。

bootstrap 使用显式状态机记录非秘密进度：`UNINITIALIZED → ADMIN_ROTATED → COMPANY_CREATED → ROLE_CREATED → INVITATION_CREATED → RUNTIME_IDENTITY_CREATED → API_KEY_CAPTURED → FINAL_PERMISSIONS_VERIFIED → GATEWAY_ENABLED`。重入时先通过正式 API 与本地 receipt 对账，再生成只包含缺失动作的新 repair plan；不得假定前一轮全部失败或重新执行已经成功的写操作。receipt 只记录公司/用户/角色/邀请/API Key 元数据 ID、计划哈希和时间，不保存密码、token 或原始 API Key。

完成后必须分别证明：

1. `/api/license/state` 返回有效许可证，entitlements 包含 `API_ACCESS` 和 `CUSTOM_ROLES`；
2. 当前隔离公司的订阅计划 features 包含 `API_ACCESS` 和 `ROLE`；
3. 未受邀 email 即使知道 role ID 也无法注册，计划中的运行用户通过精确邀请加入；
4. 运行身份属于该公司并具有最小必要资产权限；
5. API Key 是公司范围、最小权限的独立集成凭据；
6. 工单搜索基线为零。

创建公司、管理员、角色、用户或 API Key 都是外部写操作，只能在已确认的 deployment/bootstrap plan 中使用 CMMS 正式 API 完成并保留关联信息；不得通过 SQL seed 或 ORM 脚本绕过服务边界。

## 开发模式与验收模式

同一拓扑提供两个严格程度不同的入口：

- 开发模式允许组件工作树有本地修改，但状态输出必须清晰标记 `UNCOMMITTED`，不得生成 Phase 2 通过回执；
- 验收模式要求根仓干净、CMMS 组件干净、组件 HEAD 等于总仓 gitlink、许可证和身份验证通过；浏览器公共 origin 固定为 `http://cmms.localhost:3000`，宿主机健康 origin 固定为 `http://127.0.0.1:3000`，Compose API origin 固定为 `http://host.docker.internal:3000`，且三者必须命中同一个 Nginx 实例。

服务脚本绝不自动切换 Git 分支、重置文件或提交代码。开发修改必须遵守组件短生命周期分支和组件先提交推送、总仓后更新 gitlink 的规则。

## 故障处理

- 端口冲突：停止启动并报告冲突；不终止现有进程。
- PostgreSQL 或 MinIO 不健康：不启动 API；保留容器和脱敏日志供诊断。
- API 迁移失败：停止 API 重启循环，不修改数据库，不自动重试迁移。
- API 任意退出或 start permit 校验失败：`ExecStopPost` 关闭 gateway，不自动创建新 MainPID。
- Nginx `502`：分别检查 API 和前端 unit，不能把网关进程存活当作应用健康。
- 离线许可证文件/guard/IP 过滤异常：关闭 gateway；在线许可证无效或缺少 `API_ACCESS`：允许基础健康诊断，但 Phase 2 readiness 必须失败。
- CMMS SHA 或工作树不符合验收条件：继续允许开发模式，拒绝生成或消费验收凭据和回执。
- 配置或日志检测到秘密：停止流程，轮换受影响凭据，并在继续前完成泄露范围核对。

所有诊断输出必须脱敏，不显示许可证、JWT、数据库/MinIO 密码、API Key、管理员令牌、完整环境或上游堆栈。

## 测试策略

实现按测试驱动方式覆盖：

1. Compose 契约测试
   - 固定项目名、状态服务的 loopback 端口与网关端口；
   - 没有 `container_name`、外部数据卷或 ThingsBoard 网络；
   - 不引用预构建 CMMS API/前端镜像；
   - 只有 Nginx 使用 host network，并且渲染配置只监听 `127.0.0.1:3000` 和核验后的 Docker host-gateway；
   - PostgreSQL 与 MinIO 保持桥接网络并只发布 loopback 端口。
2. 配置与脚本测试
   - 缺文件、符号链接、错误所有者、错误权限、空秘密和端口冲突全部 fail closed；
   - 输出不包含测试秘密；
   - bootstrap 身份密码只被控制进程通过受限文件描述符读取，不进入 API/前端 systemd 环境或 Compose 配置；
   - 密码轮换测试覆盖请求未发送、确定失败、成功、响应丢失和进程崩溃；current 在 candidate 认证成功前始终保留，两槽歧义时 gateway 保持关闭；
   - API unit 固定 `Restart=no`；`ExecStartPre` 必须原子消费有效 permit 并复核 loopback-only 状态，`ExecStopPost` 覆盖 clean exit、失败、直接 stop/restart 和启动前失败，`OnFailure` 只作重复保险；前端 unit 才允许节流的 `Restart=on-failure`；
   - 无 permit、过期/复用 permit 和直接 `systemctl --user start/restart` 都不能创建 API MainPID，并保持或恢复 gateway 关闭；
   - 离线许可证只把受限 `LICENSE_FILE_PATH` 传给 API，强制 systemd profile 只允许 loopback；删除、替换、改权或破坏许可证文件时不能到达 Keygen，guard 按 `30s`/`1s` timer 配置触发 gateway 关闭；
   - 离线 API 与 guard timer 的 `BindsTo=`/`After=` 生效；直接停止 timer 会停止 API 并触发 `ExecStopPost`，不能留下双地址 gateway 与失去 guard 的 API；
   - 在线许可证预算在进程创建前写前计入、每日最多 10 次；未知/耗尽预算和启动校验失败时 launcher 不创建、重启或重试 API；
   - 在线预算测试覆盖日期切换、账本损坏、进程启动失败、崩溃和长进程 12 小时缓存刷新预留，不把保守账本冒充 CMMS 内部实际计数；
   - 在线运行期测试记录 12 小时缓存过期/异常后的源码重校验限制，确认 entitlement 失败会阻断 Phase 2 readiness；不得把该模式宣称为无重试或严格 fail-closed；
   - 停止命令不包含 `down -v` 或卷删除；
   - 日常 `start`/`restart` 不包含 signup、邀请、密码、角色或 API Key 写操作，状态不一致时只能生成 repair plan；
   - 冷启动和重启必须先证明 Docker gateway listener 已关闭，再启动当前 API 并通过 loopback 对账；模拟旧 receipt 配合替换/空数据库时 gateway 始终保持关闭；
   - 启动副作用白名单和敏感文件 manifest 绑定已审查 CMMS 基线；相关源码或 migration 变化时拒绝开放 gateway；
   - 验收模式拒绝 dirty 或 gitlink 不匹配；
   - 工具链下载必须使用固定 URL 和 SHA-256，禁止 ambient 版本回退。
3. Nginx 路由测试
   - `/`、`/api/`、`/storage/` 指向精确 upstream；
   - HMR/WebSocket Upgrade 和上传边界保留；
   - `/storage/` 剥离前缀并把 upstream Host 固定为 `127.0.0.1:9000`。
4. 宿主机构建验证
   - CMMS API 目标测试与 Maven 编译通过；
   - 前端必须使用 `npm ci --legacy-peer-deps` 完成锁定安装，并通过构建；测试拒绝会改写锁文件的 `npm install`。
5. 经独立执行授权后的运行烟测
   - PostgreSQL、MinIO、Nginx、API 和前端状态正确；
   - `http://127.0.0.1:3000/api/health-check` 成功；
   - 从隔离测试容器访问 `http://host.docker.internal:3000/api/health-check` 成功并命中同一实例；
   - 同一条 `cmms.localhost` MinIO 签名 URL 可从宿主机和带精确 host-gateway 映射的隔离容器下载；
   - 页面经 `3000` 加载；
   - API unit MainPID、工作目录、启动 SHA、当前干净 SHA 与总仓 gitlink 一致；
   - `INVITATION_VIA_EMAIL=true` 且邮件通知关闭；未受邀测试 email 使用已知 role ID 的 signup 被拒绝且不会留下用户；
   - 受控重启前后通过正式 API 比较计划身份、资产和工单，确认除已列明的初始化/维护/审计副作用外没有变化；
   - 许可证、公司计划、邀请身份、API Key 资格和零工单基线通过。
6. Phase 2 资格门
   - 只读身份发现返回精确 CMMS company ID；
   - `platform-integration` 通过公司范围 API Key 读取；
   - 在后续独立 plan/apply 授权前不创建资产、遥测、Alarm 或工单。

## 回滚与数据保护

代码回滚由操作者选择已经提交和推送的 CMMS 提交；部署脚本只停止/启动服务，不执行 Git checkout、reset 或清理。切换提交前先停止 API，并确认目标提交的 Liquibase 兼容性。

运行故障时的默认回滚是：

1. 把 gateway 切回 loopback-only，停止 Nginx，并证明 Docker host-gateway listener 已关闭；
2. 停止 API 和前端 unit；
3. `docker compose stop` 保留 PostgreSQL 与 MinIO 数据卷；
4. 恢复上一份已验证的非秘密部署配置和已提交代码；
5. 按日常启动流程重新建立 loopback-only 运行态、完成认证/读取对账，再决定是否开放 gateway。

任何数据库恢复、schema 降级、数据卷删除或 MinIO 覆盖都不属于默认回滚，必须使用单独的备份/恢复计划和明确授权。

## 关键实现依据

- [PostgreSQL 官方镜像的 `_FILE`/Docker secrets 说明](https://hub.docker.com/_/postgres)
- [MinIO 官方 Docker secret file 说明](https://github.com/minio/minio/blob/master/docs/docker/README.md)
- [Eclipse Temurin JDK 17.0.19+10 官方发布](https://github.com/adoptium/temurin17-binaries/releases/tag/jdk-17.0.19%2B10)
- [CMMS MinIO 签名 URL 实现](../../../components/cmms/api/src/main/java/com/grash/service/MinioService.java)
- [CMMS API Key 权限检查](../../../components/cmms/api/src/main/java/com/grash/service/ApiKeyService.java)
- [CMMS API Key 认证与 `lastUsed` 审计更新](../../../components/cmms/api/src/main/java/com/grash/security/ApiKeyAuthFilter.java)
- [CMMS 当前密码验证与密码轮换端点](../../../components/cmms/api/src/main/java/com/grash/controller/AuthController.java)
- [CMMS 邀请注册检查与邀请记录创建](../../../components/cmms/api/src/main/java/com/grash/service/UserService.java)
- [CMMS 启动初始化维护行为](../../../components/cmms/api/src/main/java/com/grash/ApplicationInitializer.java)
- [CMMS 许可证验证与本地 Keygen 请求计数](../../../components/cmms/api/src/main/java/com/grash/service/LicenseService.java)
- [CMMS 前端源码镜像的 Node 与 legacy peer 依赖安装方式](../../../components/cmms/frontend/Dockerfile)
- [CMMS 默认角色定义](../../../components/cmms/api/src/main/java/com/grash/utils/Helper.java)

## 验收标准

- CMMS 浏览器入口固定为 `http://cmms.localhost:3000`，宿主机健康入口固定为 `http://127.0.0.1:3000`，容器 API 入口固定为 `http://host.docker.internal:3000`；三者命中同一 Nginx 且不与现有 ThingsBoard/PDM 端口冲突。
- PostgreSQL、MinIO 和 Nginx 只存在于独立 `ifactory-cmms-dev` Compose 项目中。
- API 和前端从当前 CMMS 组件源码运行，不使用预构建 CMMS 应用镜像。
- 前端修改可通过 HMR 生效；API 修改可快速构建和受控重启，公共入口不变。
- API MainPID 只能由短时单次 permit 启动；任何退出都会执行 unit 级 fail-closed，直接 systemctl 操作不能绕过 gateway 对账门。
- 未跟踪运行文件是唯一操作者管理的持久秘密来源；必要的 API 进程环境复制受本机可信边界约束，状态容器配置、日志和状态输出不泄露秘密值。
- 许可证模式明确可审计：离线模式引用受限许可证文件、强制只允许 loopback 出站并运行 guard；在线模式显示保守日预算和运行期重校验限制，并在启动预算未知/耗尽或启动校验失败时保持 gateway 关闭。
- 验收模式能证明 CMMS SHA、有效的 `API_ACCESS`/`CUSTOM_ROLES` entitlements、公司计划的 `API_ACCESS`/`ROLE` features、强制邀请门、公司身份、最小权限 API Key 和零工单基线。
- 停止、重启和失败恢复不会删除 CMMS 数据卷，也不会影响 ThingsBoard、PDM 或其他容器。
- 在后续独立确认门之前，不执行 Phase 2 provisioning、Dashboard 发布、遥测注入、训练、Alarm 或工单写入。
