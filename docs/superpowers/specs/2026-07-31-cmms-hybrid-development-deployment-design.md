# CMMS 开发优先混合部署设计

## 文档状态

- 日期：2026-07-31
- 状态：书面设计及 Task 2 确认门补充设计已批准；控制面实施中，live bootstrap 待独立确认
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

Nginx 单独使用 Linux `network_mode: host`。启动脚本从 Docker 默认 bridge 解析网关 IPv4，确认它属于本机 Docker bridge 且不是 wildcard/LAN 地址，再把无秘密模板渲染到 `.runtime/cmms-nginx/cmms-development-nginx.conf`。当前主机的 `cmms.localhost` 同时解析为 `127.0.0.1` 和 `::1`，所以 loopback-only 配置精确监听 `127.0.0.1:3000` 与 `[::1]:3000`；dual 配置只再加入该精确 Docker gateway IPv4 的 `:3000`，不加入 IPv6 wildcard 或 LAN 地址。Nginx 通过 `127.0.0.1:3001`、`127.0.0.1:8082` 和 `127.0.0.1:9000` 访问前端、API 和 MinIO。PostgreSQL 与 MinIO 继续使用 Compose 的隔离桥接网络；只有各自明确声明的 loopback 端口可以被宿主机访问。Nginx 不加入该桥接网络，也不使用容器服务名。获准启动后的隔离容器烟测必须再证明 `host.docker.internal:host-gateway` 实际解析到同一地址；不一致时停止，不放宽监听范围。公共路由保持 CMMS 当前单域语义：

- `/` 转发 React 开发服务器并保留 WebSocket Upgrade，支持 HMR；
- `/api/` 转发 Spring Boot，并移除外部 `/api` 前缀；
- `/storage/` 转发 MinIO，并保留大文件上传所需配置。

任何携带 `x-api-key` 的 `/api` 请求还必须经过绑定当前 `platform-integration` 客户端与契约摘要的 method/path 正向白名单：只允许 `/auth/me`、工单搜索、规范 equipment ID 资产查询，以及带规范 `Idempotency-Key` 的 Phase 2 资产创建；其他 API Key 路由在到达 CMMS 前返回稳定 JSON `403` 和策略头。Bearer bootstrap 流量不走该分支。该规则不检查 JSON body，不能单独证明资产创建含 `equipment_id`；受信的已绑定 Phase 2 客户端、契约和独立 provisioning 计划仍负责请求体与实际写授权。

`cmms.localhost` 是 CMMS 返回给浏览器的规范公共主机名。preflight 必须证明它在宿主机解析为 loopback；任何需要解引用 CMMS 绝对公共 URL 的隔离容器都必须通过精确 `extra_hosts` 把 `cmms.localhost` 映射到同一个 Docker host-gateway。Phase 2 的 CMMS API base URL 仍保持既有固定值 `http://host.docker.internal:3000`，不依赖容器 DNS 自动理解 `.localhost`。

MinIO SDK 先针对内部 endpoint `http://127.0.0.1:9000` 生成签名，再由现有 CMMS 代码把 origin 替换为公共 endpoint。为保持 SigV4 有效，Nginx 的 `/storage/` 必须剥离前缀并把转发 `Host` 精确重写为签名时的 `127.0.0.1:9000`。由于 query 含 access-key identity 与签名，`/storage/` 同时禁用 access log 与可能回显完整 request 的 location error log；控制面只保留稳定结果码。经确认 apply 内的运行烟测必须证明同一条签名 URL 可从宿主机下载，并在带精确 `cmms.localhost:host-gateway` 映射的隔离容器中下载。

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
├── cmms-development-apply.lock
├── cmms-nginx/cmms-development-nginx.conf
├── plans/cmms-development/
├── receipts/
│   └── cmms-bootstrap/{receipt_sha256}.json
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

计划固化每个身份 current/candidate 的非秘密文件 stat 起点；同一已确认 bootstrap 内的合法 promotion 通过保持 candidate FD 跨同目录 rename 来建立 lineage。rename 后 current 必须仍是同一 `dev`/`ino` 且 size/mtime/字节未变，允许 ctime 单调前进并把新 stat receipt-first 记录为后续动作的期望值；candidate 路径必须消失。任何外部文件替换或“路径存在即可信”都会停止，不能因为计划仍有效而放宽。

每个 secret file 都必须是当前用户拥有、单链接、非符号链接、限长的普通文件，权限为 `0600`。PostgreSQL 使用官方 `POSTGRES_PASSWORD_FILE`，MinIO 使用官方 `MINIO_ROOT_USER_FILE`/`MINIO_ROOT_PASSWORD_FILE`，使状态容器的 Docker 配置只出现 secret 路径而不是值。CMMS API 目前只接受普通环境变量，因此受控 API launcher 仅从已验证的数据库、MinIO、JWT 和许可证 key secret file 读取并导出到 API 进程环境，不读取 bootstrap 身份密码；若提供离线许可证文件，launcher 只把其受限绝对路径作为 `LICENSE_FILE_PATH` 传给 API，不把文件内容复制进环境。这意味着同一 Unix 用户或具有 Docker/调试权限的主体属于本地可信边界。设计承诺的是“运行文件是唯一操作者管理的持久秘密来源，日志、状态和 Docker 配置不复制秘密值”，而不是不现实的“秘密只存在于文件”。

`cmms-frontend.env` 只包含同源 API URL、端口和非秘密 UI 开关，也使用 `0600`，以简化统一校验。

`cmms-development-state.json` 不保存秘密，只记录 Compose 项目名、期望 CMMS git SHA、实际启动 SHA、systemd MainPID/进程启动标识、解析后的 Docker host-gateway、启动时间和固定端口。状态文件不能替代 Git、运行进程和 API 验证。验收时必须把 unit MainPID、进程工作目录、启动记录、当前干净 SHA 与总仓 gitlink 重新交叉核对。

bootstrap receipt 不覆盖写一个“当前文件”，而是在 `.runtime/receipts/cmms-bootstrap/` 中按规范内容 SHA-256 保存不可变 `0600` generation。`StateRecord.bootstrap_receipt_sha256` 是唯一 current 指针：先独占创建、fsync 并重开新 generation，再 CAS 更新 StateRecord，旧 generation 始终保留。崩溃发生在 CAS 前时旧指针仍可完整读取，新文件只是无授权 orphan；CAS 后则指向已完整持久化的新文件。恢复流程禁止按目录最新文件、mtime 或最大 generation 猜测 current，也不得让 orphan generation 推进 API Key 或身份状态。

`cmms-license-online-budget.json` 是不含秘密、原子更新的保守预算账本，只在在线 license key 模式使用。它记录 API 进程使用的 `LocalDate`/时区、由受控 launcher 写前计入的新进程尝试数、每日上限和连续性标识；不声称等于 CMMS 内部 `KeygenRequestTracker`。离线许可证模式明确标记为不消耗该在线启动预算。

`start-permits/` 只保存尚未消费且短时有效的 API start permit；控制脚本和 `ExecStartPre` 通过同一目录内原子 rename 实现单次消费。启动完成、失败或 permit 过期后必须清理精确 permit，不能把目录存在本身当作授权。

`cmms-development-nginx.conf` 由跟踪的无秘密模板渲染，只允许出现 loopback 与本轮核验的 Docker host-gateway；不得接受操作者提供的任意监听地址。

Phase 2 的 `.runtime/predictive-maintenance-shadow.env` 独立管理，只保存 CMMS API Key 的严格凭据 envelope；不得把 Atlas 许可证、管理员密码、数据库密码或 MinIO 密码复制进去。该 envelope 是 API Key 的预期持久运行副本，因此不受“基础设施 secret file”措辞混淆。

## 启动、初始化与停止流程

所有会启动、停止、重启或修复 CMMS 的命令都遵守同一个 fail-closed 不变量：除 lease 外仅用于消费 plan hash 的 application audit reservation 外，第一项 service-affecting/planned effect 必须把 Nginx 渲染或 reload 为 loopback-only，并证明 Docker host-gateway 的 `:3000` 不可达；后续任一 preflight、认证或资格检查失败都保持该状态。只有当前 API 完成全部对账后，最后一步才可开放双地址 listener。

每个静态 hash/规范字节/有效期均通过的 `apply`，先在 `.runtime/plans/cmms-development/{plan_sha256}.application.json` 独占创建并 fsync 一个 `ATTEMPTED` reservation；这是 apply-effect 锁之外唯一允许的追加式审计写，也使该 plan hash 从此不可重用。随后才通过已验证父目录安全打开或首次创建 `.runtime/cmms-development-apply.lock`，取得非阻塞进程级独占锁，并把该文件描述符保持到补偿和 application 终态都完成。该锁是所有 `apply` 的 effect single writer，不按 plan hash 分片；两个不同计划也不能并发。竞争失败只把 loser 自己的 reservation 改为 `CONTENDED`，必须在 gateway、HTTP、receipt、State 和服务动作前返回 busy。等待不会让原计划重新可用，操作者只能重新生成计划。

`plan`、`status` 和 `secret` 不持有该 effect lease：`plan` 对 State/receipt/config/secret stat 做前后稳定性复核，`apply` 在每次使用 secret/env 时通过已持有描述符和 stat lineage 检测并发替换；这是 apply effect 的单写锁，不是整个 `.runtime` 的全局文件锁。安全性的 `emergency fail-closed` 是唯一服务动作例外，只能缩小网关暴露。进程崩溃会由操作系统释放锁，但 `ATTEMPTED`/`IN_PROGRESS` reservation 不会变成成功或失败；任何远端提交、receipt/State 锚定、补偿证明或终态写入不确定时保留非终态，并由新哈希 reconciliation plan 处理。

### 计划应用记录与动作验证

每个已消费的计划哈希对应一个固定 schema 的规范 `PlanApplicationRecord`：

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

`application_id` 是 reservation 创建时生成的非秘密 128-bit 小写十六进制值；`plan_sha256` 必须是小写十六进制 SHA-256，并与 application 文件名一致。时间统一编码为固定六位小数的 UTC `YYYY-MM-DDTHH:MM:SS.ffffffZ`，满足 `attempted_at <= claimed_at <= terminal_at`，忽略其中为 `null` 的项。`application_id`、`plan_sha256` 和 `attempted_at` 在所有 generation 中不可变。

状态、generation 和 nullable 字段的合法组合只有：

| State | Generation | `claimed_at` | `terminal_at` | `safe_result_codes` 首项 |
|---|---:|---|---|---|
| `ATTEMPTED` | 1 | `null` | `null` | `null` |
| `CONTENDED` | 2 | `null` | 必填 | `APPLY_CONTENDED` |
| `REJECTED` | 2 | `null` | 必填 | `APPLY_REJECTED` |
| `IN_PROGRESS` | 2 | 必填 | `null` | `null` |
| `SUCCEEDED` | 3 | 必填 | 必填 | `APPLY_SUCCEEDED` |
| `FAILED` | 3 | 必填 | 必填 | `APPLY_FAILED` |

`safe_result_codes` 是有序、去重的稳定代码列表，最多 32 项；每项必须匹配 `[A-Z][A-Z0-9_.-]{0,63}`，并且是控制面定义的枚举值。不允许路径、PID、异常文本、堆栈或上游响应。v1 的 transition 根据目标状态自动写入唯一 primary code，调用者不能传入 primary 或任意字符串；当前没有 secondary code，详细操作/补偿代码只保留在内存结果和脱敏 CLI 输出中。未来增加 secondary 必须先扩展中心枚举和严格转换测试。

每次转换都必须原子替换、fsync、重开并核对前一 generation；终态不可再次转换。`REJECTED` 只表示取得 effect lease 后、第一次 service effect 前已经确定的 snapshot、binding 或 confirmation 拒绝。只有在补偿结果和终态持久化均可证明时才能写 `FAILED`；不确定的 fail-close、远端提交、补偿或终态写入继续保留 `ATTEMPTED` 或 `IN_PROGRESS`。

动作验证采用单一 action tuple、分层验证，不在计划中增加另一个 `PlanBranch` 真相源：

- `ActionRegistry` 只校验已持久化字段：完整 `ActionCode` 注册、handler/mutation class、target 类型和格式、allowed operations、互斥关系、依赖及规范顺序。它接收 operation、profile、license mode、bootstrap bindings 和 actions，但不读取 State、文件、进程或 HTTP。
- `DeploymentPlan.create` 校验 snapshot、随机 nonce、有效期、operation/profile/license 组合和 action-dependent bindings 的 presence/nullability。
- 生命周期规划器根据一个稳定的 `PlanningAssessment` 唯一生成规范 action tuple；调用者不能直接指定分支或任意 actions。
- apply-time preflight 在 fail-close 和 claim 后，以零 HTTP 方式重新生成同一 assessment 和 tuple，并逐项精确比较 actions、targets、profile/license 与 bootstrap bindings；它不能补充、删除或重排动作。

注册表可在内部把 tuple 分类为 active/stopped start、discovery repair、bootstrap repair、budget recovery repair 或 capture-cleanup repair，但分类值不导出、不序列化。所谓“唯一 action tuple”是指同一个 `PlanningAssessment` 只能生成一个 tuple，而不是每个 operation 在所有运行状态下只有一组固定动作。结构性错误由注册表和 `DeploymentPlan.create` 拒绝；active/stopped、卷状态、当前 receipt 和其他运行上下文的选择由规划器决定，并由 preflight 精确重放。

规范顺序由实施计划中一张冻结的全局 action rank 和逐分支 cardinality 表定义，不通过任意拓扑排序推断；同 code 的多 target 行按 `(target_kind,target_id)` ASCII 次序排列。规划器只把 assessment 中精确标为 `SATISFIABLE_BY_ACTION` 且属于该分支 allowlist 的 finding 合并进必选集合，再按该 rank 排序，因此没有可由调用者选择的“可选顺序”。

bootstrap repair 内部再分成四个互斥子语法：按显式 receipt/probe/slot 映射选择并以 Phase 2 publish 结束的连续 bootstrap completion 后缀；发布与 capture cleanup 已完整锚定后只恢复 composite readiness/dual/acceptance 的 readiness-only；精确 ID 的 revoke-only；精确 identity target 的 candidate-discard-only。Readiness-only 不含任何 bootstrap/repair mutation；revoke/discard 不能和 create/publish 混在同一计划。Discovery、revoke-only 和 discard-only 在写入并 State-select 自己的 receipt generation 后，必须执行计划中显式的 `repair.stop-loopback-runtime`，证明 API/frontend/guard/timer 停止并清除 State 进程标识；后续 mutation 必须重新生成 stopped-state 计划，不能复用仍活动的 PID 或 permit。

invitation probe 和 Phase 2 publish 都不推进 `BootstrapState`，所以 completion 后缀不能只按“下一状态”推导。Task 8 从 State-selected receipt 的 action-attempt 行派生 `NOT_ATTEMPTED`、`PENDING_OR_UNCERTAIN`、`ENFORCED`、`UNKNOWN_NO_USER` 或 `UNEXPECTED_USER_OR_INVALID`，Task 9 在 plan/preflight 入口按冻结映射选择且不在同一 apply 内重分类：`ROLE_CREATED + NOT_ATTEMPTED + unused slot` 或单条 terminal `UNKNOWN_NO_USER + distinct new slot` 从 probe 开始；`ROLE_CREATED + ENFORCED + no slot` 从 invitation 开始；pending/uncertain 必须绑定旧 slot 进入 discovery-only；后续状态只允许 terminal `ENFORCED` 且不再绑定 slot。追加历史的唯一例外是“第一条 terminal UNKNOWN_NO_USER 后，由后续已确认计划绑定不同 slot 的第二条 attempt”；第二条存在后只取它作为 effective disposition，第一条只证明 distinctness；第二次 UNKNOWN 耗尽重试且不允许第三条。异常用户、两条 pending、交错/重用 target、第三条 attempt 或 state/result 不一致全部阻断，不能回退到通用 next-state 规则。

readiness-only 只允许 proven-stopped 的 `REPAIR`，且 State 指针必须精确选中 `FINAL_PERMISSIONS_VERIFIED` 或 `GATEWAY_ENABLED` receipt，并独立绑定当前 State generation/SHA 与 receipt 的 `last_plan_sha256`；普通 stop 可以更新 State 的 last-plan transition，不能伪造或抹除 receipt 所指的历史 bootstrap application。该 receipt 必须证明 invitation probe `ENFORCED`、Phase 2 `PUBLISHED`、anchored capture 已清理且路径持续不存在、当前 Phase 2 env stat 精确匹配、没有 pending/uncertain attempt 或 cleanup/discovery/revoke/discard 目标，并且仍存在“未到 `GATEWAY_ENABLED`、receipt 的 `last_plan_sha256` 所指 application 非终态、或 eligible acceptance receipt 缺失”之一的 completion gap。Capture cleanup pending 时必须先执行固定 cleanup-only plan，再生成新 readiness-only plan；已经健康且终态一致时拒绝 repair，使用普通 status/start。

bootstrap/repair 写入前的 `readiness.require-api-loopback` 与写入后的 composite `readiness.require-loopback` 是两个不同动作。前者只证明当前 API PID/启动标识、socket、固定 loopback health、网关 generation 和 license barrier，不认证、不读取 company/user/role/API Key；后者在计划中的 bootstrap mutations 完成后执行完整 pre-open 对账。二者不可互相替代。

验收 profile 的 fresh bootstrap、bootstrap-mutation completion 或满足上述 completed-publication 谓词的 readiness-only repair，只有在 Phase 2 seed receipt 缺失、State-selected/动作投影后的 bootstrap 状态为 `FINAL_PERMISSIONS_VERIFIED` 或 `GATEWAY_ENABLED`，并且 tuple 包含完整 composite pre-open、dual enable、post-open 和恰好一个 MinIO probe 时，才可生成验收回执。不满足谓词的 acceptance bootstrap 直接拒绝，而不是降级为 operational。Development bootstrap、discovery、revoke-only、discard-only、budget recovery 和 capture-cleanup repair 永不生成验收回执；readiness adapter 只按已确认 plan 中的 operation/profile/probe presence 分类并执行已有 probe，不能自行读取 planning-only 字段或添加动作。

Capture-cleanup repair 的唯一 plan-visible tuple 是：

```text
gateway.fail-closed
repair.finalize-api-key-capture-cleanup
    target_kind = receipt
    target_id = receipt:cmms-bootstrap
```

Claim 和 local preflight 是生命周期屏障，不是伪造的 `ActionCode`。该 tuple 的固定执行顺序为：

```text
静态验证
-> 创建 ATTEMPTED reservation
-> 取得 effect lease
-> 执行 gateway.fail-closed 并证明外部 listener 不存在
-> ATTEMPTED 转为 IN_PROGRESS
-> local preflight 精确重放
-> cleanup handler
-> 创建 immutable receipt generation
-> CAS 更新 StateRecord current pointer
-> application terminal transition
```

Cleanup handler 只能在 observed stat 与历史完整 stat 相等时通过 descriptor-bound unlink、目录 fsync 并证明文件消失，或在 observed 为 `null` 时证明持续不存在；published outcome 还必须精确复核 Phase 2 env stat。随后它只能写入并由 State 选中新 cleanup generation。该 tuple 禁止 Compose/systemd 启动、许可证、permit、认证、HTTP、raw-key 解包、publish/revoke、readiness 和 dual gateway 动作。

### 一次性 bootstrap

1. 只读检查 Docker、Compose、项目专用 JDK 17/Maven 3.9.3/Node 21.6.1、端口和工作树；解析并核验 Docker 默认 bridge gateway。缺少工具链时只输出待执行的校验和固定 bootstrap 计划，并在授权门停止。
2. 核对总仓记录的 CMMS gitlink 与组件实际 HEAD；开发模式报告 dirty 状态，验收模式必须拒绝 dirty 或不匹配的组件。
3. 校验三个运行环境文件、全部 secret file 和三组 credential slot 的所有者、类型、链接数、权限、大小、必需键与合法状态，不输出值；在任何远端身份创建或密码轮换前，必须已经安全持久化对应 candidate，并保留既有 current。
4. 通过“本地/receipt-only 状态发现 → 精确目标动作规划 → 规范计划固化”的无环流程生成 CMMS deployment/bootstrap plan，public `plan` 不发出 HTTP、不登录 CMMS，因而不会在确认前触发 `lastLogin`/`lastUsed` 写入；apply 再重放同一零 HTTP 发现规则并要求动作、顺序和目标完全一致。计划精确列出一个每次生成都更新的非秘密 128-bit nonce、源码 SHA、工具链与镜像摘要、Compose 项目/卷、端口、仅 loopback 的 bootstrap Nginx 配置、隔离公司管理员标识、集成角色权限、邀请对象、API Key 标签、离线/在线许可证模式、在线模式的日期/时区与保守启动预算、预期外部请求/本地计数副作用和预期零工单结果；秘密值只以“已安全提供/缺失”表示。fresh bootstrap 只绑定规范身份、角色 external ID、API Key 标签等稳定语义目标，不伪造尚未由 CMMS 创建的 company/user/settings/role live ID。nonce 确保任何已消费/竞争失败的 plan 重新生成后得到新 hash；输出计划哈希后停止，等待后续一轮明确确认。
5. 确认后执行 `docker compose config -q`，确认没有预构建 CMMS API/前端镜像、全局容器名、LAN/wildcard 监听或计划外对象。
6. 首先启动 PostgreSQL、MinIO 和只监听 `127.0.0.1:3000`、`[::1]:3000` 的 bootstrap Nginx。Docker host-gateway 此时没有 listener。
7. 按选定许可证模式准备 API 和前端；在线模式先原子写前计入一次保守预算，离线模式先证明 IP 过滤与 license guard 可强制执行。创建一次性 start permit 后才通过 systemd 启动 API。API 首次启动执行 Liquibase，并创建 CMMS 默认订阅计划和超级管理员；等待 loopback `/api/health-check` 和许可证状态成功。
8. 在同一次已确认 apply 中立即通过 CMMS 正式 API 完成超级管理员密码轮换、隔离公司管理员注册、专用集成角色、精确邀请、运行用户、API Key 创建和临时权限移除。每个非幂等请求前先持久化当前计划哈希、动作及稳定目标；公司注册后的 `company_id`/`company_settings_id` 等 live ID 只有在同身份正式读回后才 receipt-first 落盘，并供后续角色动作重新核验使用。任何步骤失败都停止 API/Nginx，并保持 gateway listener 关闭。
9. 使用新建的隔离身份验证许可证 entitlement、公司订阅 feature、邀请门、运行身份权限、API Key 能力和零工单基线。
10. 只有第 8–9 步全部成功后，才把 Nginx 配置渲染为同时监听 loopback 与已核验 Docker host-gateway，受控 reload 后执行宿主机和隔离容器双路径烟测，并把非秘密状态写为 `GATEWAY_ENABLED`。

设计批准不等于 deployment/bootstrap apply 授权；实际执行必须使用后续返回的精确计划哈希确认。bootstrap 只允许在尚未完成初始化或经对账确认的部分状态上执行，不能作为普通启动命令调用。该 apply 不创建资产、遥测、Alarm 或工单，也不执行 Phase 2 provisioning。

首次初始化会产生源码中公开已知的默认超级管理员凭据。它存在期间，Nginx 只能监听 loopback，Docker gateway 上不得开放登录或任何 API 路由。密码通过请求体或本地受限文件传入正式密码更新端点，不得写入聊天、Git、命令行参数或日志。

### 日常启动与重启

receipt 中的 `GATEWAY_ENABLED` 只能证明上一轮完成，不能授权本轮直接开放 gateway。日常 `start`/`restart` 必须先恢复 loopback-only 状态，再由当前正式 API 重新证明资格：

1. 无论 Nginx 是否正在运行，都先把待启动配置原子渲染为只监听 `127.0.0.1:3000` 与 `[::1]:3000`；若正在运行则立即 reload。从隔离测试容器证明 Docker host-gateway 的 `:3000` 已不可达后，才继续。若无法证明 listener 已关闭，则停止。
2. 重做端口、工具链、镜像摘要、环境文件、secret file、CMMS SHA、许可证模式/预算和 Docker host-gateway preflight；receipt 缺失、未记录 `GATEWAY_ENABLED` 或在线预算不是已知可用时，保持 gateway 关闭并生成 repair plan。
3. 启动或重启 PostgreSQL、MinIO、loopback-only Nginx 和前端。若现有 API MainPID、进程启动标识、源码/产物/controller/unit/许可证与历史预算绑定全部精确匹配，则幂等 `start` 保留同一 MainPID，不创建 permit、不调用 API systemd start 且不消耗在线预算；任一绑定不确定就停止。只有 API 已停止或显式 `restart-api` 才走新进程分支：在线模式在创建新 MainPID 前原子写前计入一次预算，离线模式先验证强制网络 profile 与 guard，随后创建并由 `ExecStartPre` 消费一次性 permit。此时 receipt 无论记录什么都不得开放 gateway。
4. 只通过 loopback 对当前 API/数据库做不改变计划身份配置的认证与读取对账：验证计划中的已轮换超级管理员、隔离公司管理员和运行用户凭据可认证，源码公开的默认超级管理员凭据已失效，并核验许可证、公司、邀请门、运行用户、角色最终权限和 API Key 元数据。只有尚未执行 Phase 2 provisioning 的 bootstrap readiness 才要求零工单；日常启动不得把合法的后续工单误判为部署故障。
5. 只有第 4 步全部通过，才渲染双地址配置并 reload Nginx；随后核验宿主机与隔离容器公共路径命中同一实例。

当前 CMMS API 启动并非数据库只读。与已审查 CMMS 基线及启动副作用敏感文件 manifest 绑定的允许副作用白名单包括：

- Liquibase 执行该提交声明的 schema migration；
- `ApplicationInitializer` 在缺失时补建内置超级管理员/订阅计划，更新内置默认角色，并修正临时时区值；
- 许可证验证访问已配置的 Keygen 服务，并更新本地 `KeygenRequestTracker` 计数；
- signin 更新 `lastLogin`，API Key 认证按实现节流更新 `lastUsed`。

这些是 CMMS 自己拥有的初始化、维护和审计写入，不授权编排脚本直连数据库。日常编排本身禁止 signup、邀请、角色创建/修改、密码轮换、API Key 创建/吊销或领域对象写入。当前 `ApplicationInitializer` 在超级管理员公司用户为空时可能在普通启动中重建默认密码用户；在不直连数据库且不修改 CMMS 业务代码的边界下，控制面无法在 Java 启动前阻止这一内部写入。它必须作为源码残余风险处理：启动后若 receipt 绑定的用户 ID/凭据失配或默认凭据重新有效，立即关闭 gateway 并停止宿主机服务，保留证据并要求独立安全修复，绝不开放双地址 listener 或把它算作成功日常启动。

Fresh bootstrap 的 `cmms.initialize-fresh-database` action 是对该启动副作用的显式授权与事后验证，不是第二个初始化器：`process.start-api` 在启动前要求该行已存在，API 返回后才按 rank dispatch 该行核对源码定义的初始化结果，且不得再次写入。验证必须在 frontend 启动和 API-loopback readiness 之前完成。

gateway 开放前必须通过正式 API 证明计划中的公司、定制集成角色、用户和 API Key 仍保持精确身份与权限；在受控重启测试中还要证明资产和工单集合未被启动流程改变。实现必须维护一份跟踪的“启动副作用敏感文件”清单，至少覆盖 migration、`ApplicationInitializer`、许可证与认证过滤器及其直接写入依赖；这些文件有未审查变化时，API 最多以 loopback-only 运行供诊断，gateway 保持关闭。其他业务代码的 dirty 修改仍可在开发模式热迭代，但继续标记 `UNCOMMITTED`，不能生成验收回执。receipt 缺失、状态不一致或认证/读取对账失败时，不得把日常启动隐式升级为 bootstrap；流程停止并生成修复计划。

### 部分失败修复

部分 bootstrap 必须使用独立的 `repair plan`/`repair apply`，重新输出新的计划哈希并等待下一轮明确确认。repair 只可按日常流程先建立 loopback-only 运行态，所有“计划凭据”都从已验证的 bootstrap secret file 经受限文件描述符读取。修复计划逐步通过正式 API 读后再写：

| 步骤 | 认证/读取对账 | 允许的单次配置写入 |
|---|---|---|
| 首次超级管理员密码 | 依次核对 candidate、current（如有）与源码默认凭据；candidate 成功且默认失败才提升 | 仅在 candidate 已持久化、candidate 失败且默认凭据有效时调用正式密码更新端点一次；响应不确定时保留两槽并重新认证 |
| 任一既有身份密码轮换 | 确认 current 可认证、candidate 已持久化且二者不同 | 调用正式密码更新端点一次；随后按 current/candidate 双槽协议认证并提升、丢弃或停止 |
| 隔离公司 | 先用 current 或 candidate 登录并读取 company | 确认身份不存在时使用 candidate signup 一次；响应不确定时重新登录，candidate 成功后才提升 |
| 集成角色 | 按公司与精确 `externalId=ifactory-pdm-runtime` 读取，并把 name、类型与完整权限作为必须匹配的属性 | 仅在该 externalId 不存在时创建；externalId 重复、name/类型/权限冲突或只有同名不同 externalId 时停止并另做变更计划 |
| 用户邀请 | 读取最近待处理邀请并匹配精确 email 与 role ID | 仅在不存在时调用正式 invite API 一次；冲突邀请时停止 |
| 运行用户 | 先用 current 或 candidate 登录并核对 company/role | 确认不存在时使用匹配邀请和 candidate signup 一次；响应不确定时重新登录，candidate 成功后才提升 |
| API Key | 按计划标签读取元数据并核对 owner/company | 仅在不存在时创建一次；原始 key 未捕获时按下述响应丢失规则停止 |
| 最终角色权限 | 读取角色并确认临时 `SETTINGS` 是否仍存在 | 仅在仍存在时移除一次；响应不确定时重新读取 |
| 网关开放 | 验证全部资格、未邀请注册负测、双路径前置条件 | 仅在全部通过时渲染并 reload 双地址 listener |

修复流程不得把“请求超时”解释为“写入失败”，也不得自动重试任何非幂等写操作。

若补偿已经停止 API，且 receipt 中还没有后续删除/吊销所需的精确 live target ID，修复计划不得按标签猜测目标。此时只能先生成并独立确认一个 discovery-only repair：恢复既有 API 到 loopback-only、使用显式 current/candidate slot 做正式只读对账、把安全 live ID 与证据绑定到该计划回执，然后在任何 CMMS 写入和双地址 gateway 之前停止。操作者必须再次生成并确认含精确目标的新 repair plan，才能执行删除、吊销或重建。

未受邀 signup probe 的响应丢失也遵循两阶段修复。原 apply 保留旧 slot 且不重发；discovery-only repair 必须把 receipt 指定的旧 descriptor/password 精确绑定进计划，只做认证与 email 查询。再次证明零用户时只把旧 attempt 终结为“未知但未创建”、清理该 owned slot，不能据此宣称邀请门已验证；下一份新哈希计划才可绑定不同 email/slot 再做一次负测。发现用户或结果不确定时继续保留旧 slot，转入独立清理设计。

若 Phase 2 publish 与 capture cleanup 已经 receipt-first/State-anchor 完成，但进程在 composite readiness、dual enable、`GATEWAY_ENABLED`、验收回执或 application terminal 之前中断，则不得再次 publish。若 capture cleanup 尚未完成，先独立确认并执行固定两行动作的 cleanup-only plan；随后从 proven-stopped 状态生成 readiness-only repair。该计划只包含许可证/permit/API/frontend 启动、API-loopback、正式只读对账、composite pre-open、dual/post-open，以及满足验收谓词时的既定 MinIO probe；它不包含密码、signup、邀请、角色或 API Key 写入。规划与 apply-time preflight 必须逐项重放 completed-publication 谓词。

### 停止

1. 先把 gateway 切回 loopback-only，停止 Nginx，并证明 Docker host-gateway listener 已关闭；
2. 停止前端和 API 用户服务；
3. 显式停止离线 license guard service/timer 并证明 inactive；在线模式也要证明没有残留 guard；
4. 执行 Compose `stop` 停止 MinIO 和 PostgreSQL；
5. 保留数据卷、运行配置和状态记录。

普通停止或重启流程禁止执行 `down -v`、`docker volume rm`、数据库清空或 MinIO 删除。需要重置隔离数据时必须另行设计可恢复目标核验和独立确认门。

## 许可证、公司与 API Access

部署流程不生成许可证，也不提供开发旁路。启动前由操作员把有效许可证材料安全放入受限 secret file，并只在非秘密环境文件中保存该文件的绝对引用；不通过聊天、Git、命令行参数或日志传输。

许可证支持两个不改变 CMMS 业务逻辑的正式模式：

- **离线文件模式（开发环境优先，严格可预测模式必选）**：操作员同时提供合法的加密许可证文件和解密所需 license key。文件存在且可读时，API 通过 `LICENSE_FILE_PATH` 优先执行源码自带的本地验证，不调用 Keygen 在线校验，也不消耗在线请求计数。
- **在线 key 模式（受限 fallback）**：每个新 API 进程首次需要许可证状态时通常会调用 Keygen；进程内缓存只有 12 小时，缓存到期后的下一次资格查询还可能再次调用。源码内部硬限制为每个 API `LocalDate` 20 次成功校验。在线模式适合操作者接受运行期重校验限制的本地验证，不提供“运行期间绝不重试外部校验”的保证。

当前 CMMS 源码在离线文件缺失或不可读时会隐式回退到在线 key。为使离线模式不可回退，离线 API unit 必须启用 systemd IP 过滤：`IPAddressDeny=any`，只允许 `127.0.0.0/8` 与 `::1/128`，从 unit 层阻止任何 Keygen 或其他非 loopback 出站，同时保留对本机 PostgreSQL、MinIO 和 Nginx 的访问。preflight 必须用同一 user manager/profile 实测“loopback 可达、非 loopback 不可达”；若当前内核、cgroup 或用户级 systemd 不能强制该策略，离线严格模式不可用且 gateway 保持关闭，不能静默降级为在线模式。

离线模式同时启用 `OnUnitActiveSec=30s`、`AccuracySec=1s` 的 license guard。guard 校验离线文件仍是预期 owner/type/link count/mode/size/SHA-256，并从 loopback 读取 `/api/license/state`；文件或状态异常、guard 自身失败时立即调用 fail-closed one-shot，且不重启 API。guard 也只允许 loopback 网络。离线 API unit 通过 `BindsTo=`/`After=` 绑定 guard timer：timer 被直接停止或失活会停止 API，并由 `ExecStopPost` 关闭 gateway。timer 反向声明 `PartOf=ifactory-cmms-api.service` 和 `StopWhenUnneeded=yes`，确保 API stop/restart/退出后 timer 也停止，不会成为下次 online 模式的残留进程；gateway 开放前必须证明 timer 正在使用预期配置。该 profile 因此明确关闭 SMTP、SSO 或其他需要 API 主动访问外网的可选功能；未来需要这些能力时必须另行设计精确 egress allowlist，而不能放开 wildcard 出站。

offline/online 模式切换属于受控部署配置变更：先按 fail-closed 流程关闭 gateway 和 API，显式停止 guard service/timer 并证明 inactive，渲染目标 API unit profile 后执行 `daemon-reload`，再生成绑定新模式的一次性 start permit。切到 offline 时必须先启动并验证 guard/IP 过滤；切到 online 时必须证明不存在 active guard unit 和 offline IP profile。模式切换不能复用旧 permit、旧 mode/start receipt 或旧模式的预算判断；已有身份/bootstrap receipt 仍需通过正式 API 重新对账，但不重复身份写入。

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
7. 使用运行身份创建一次性 API Key；POST 前先为该计划/动作 attempt 独占创建私有 capture reservation，成功响应后把 raw key 与返回的 Java Long ID、标签、运行用户和公司 ID 原子写入 `0600` secret envelope。重新打开后取得完整文件 stat，把同一 attempt/ID/stat 写入新的不可变 bootstrap receipt generation，再让 StateRecord CAS 选择该 SHA；只有三者全部交叉重开成功，才形成 raw key 到该 ID 的持久证据。fresh plan 的 capture 起点为 null，同一次 claimed apply 只能由这个 State-selected generation 推进私有 `ApiKeyFileLineage`，向后续权限验证/发布动作提供不可伪造的 anchored-capture 能力；不能把未知 stat 回填进 immutable plan。后续公司范围 API 搜索只能确认 ID/标签/owner/company，因为 CMMS mapper 会把 `code` 掩码，不能重建或独立证明原始 key；
8. 只有 plan-bound continuation capture，或同一 apply 中由 State-selected generation 推进的 capture lineage，才能通过单文件描述符、限长和原子替换写入权限 `0600` 的 Phase 2 凭据 envelope；目标存在时必须拒绝符号链接、重复键和计划外字段，并保留其他已验证配置，不在 stdout、journal 或状态文件中出现。发布读回后，先创建包含 Phase 2 新 stat 的不可变 receipt generation 并由 StateRecord 选择，才可删除精确 lineage-owned capture；删除结果再由第二个不可变 generation/State 指针锚定；
9. API Key 创建响应丢失，或 capture 写成后 receipt/State 锚定尚未完整即崩溃时，现有 capture 一律视为不可发布，不能通过事后 API 搜索或 orphan receipt 升级为可信；独立确认的 discovery repair 只记录公司范围读回的精确 ID，下一份计划只能吊销该 ID，再另行确认重建。只有 capture generation 已被 StateRecord 选择、但原 application 终态不确定时，新哈希 reconciliation plan 才可重新核验原 stat/metadata/owner 并继续。Phase 2 发布或吊销结果必须先锚定再清理 capture；若清理 pending，纯本地 repair 同时支持“历史 exact stat 仍存在”和“精确缺失”两态，前者按描述符 unlink/fsync/prove absent，后者证明 continued absence，再写 cleanup generation，全程不认证、不调用 HTTP、不解包 raw。不得按标签发布或自动重试生成第二把 key。

bootstrap 使用显式状态机记录非秘密进度：`UNINITIALIZED → ADMIN_ROTATED → COMPANY_CREATED → ROLE_CREATED → INVITATION_CREATED → RUNTIME_IDENTITY_CREATED → API_KEY_CAPTURED → FINAL_PERMISSIONS_VERIFIED → GATEWAY_ENABLED`。重入时先通过正式 API 与 StateRecord 指向的本地 receipt generation 对账，再生成只包含缺失动作的新 repair plan；不得假定前一轮全部失败、扫描 orphan generation 或重新执行已经成功的写操作。receipt 只记录公司/用户/角色/邀请/API Key 元数据 ID、计划哈希、捕获/Phase 2 文件的非秘密 stat、发布/清理状态和时间，不保存密码、token、原始 API Key 或其内容摘要。

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
   - 只有 Nginx 使用 host network，并且渲染配置只监听两个精确 loopback `127.0.0.1:3000`、`[::1]:3000` 和核验后的 Docker host-gateway；
   - PostgreSQL 与 MinIO 保持桥接网络并只发布 loopback 端口。
2. 配置与脚本测试
   - 缺文件、符号链接、错误所有者、错误权限、空秘密和端口冲突全部 fail closed；
   - 输出不包含测试秘密；
   - bootstrap 身份密码只被控制进程通过受限文件描述符读取，不进入 API/前端 systemd 环境或 Compose 配置；
   - 密码轮换测试覆盖请求未发送、确定失败、成功、响应丢失和进程崩溃；current 在 candidate 认证成功前始终保留，两槽歧义时 gateway 保持关闭；
   - API unit 固定 `Restart=no`；`ExecStartPre` 必须原子消费有效 permit 并复核 loopback-only 状态，`ExecStopPost` 覆盖 clean exit、失败、直接 stop/restart 和启动前失败，`OnFailure` 只作重复保险；前端 unit 才允许节流的 `Restart=on-failure`；
   - 无 permit、过期/复用 permit 和直接 `systemctl --user start/restart` 都不能创建 API MainPID，并保持或恢复 gateway 关闭；
   - 离线许可证只把受限 `LICENSE_FILE_PATH` 传给 API，强制 systemd profile 只允许 loopback；删除、替换、改权或破坏许可证文件时不能到达 Keygen，guard 按 `30s`/`1s` timer 配置触发 gateway 关闭；
   - 离线 API 与 guard timer 的 `BindsTo=`/`After=` 生效；timer 的 `PartOf=`/`StopWhenUnneeded=yes` 反向生效；停任一侧都不能留下双地址 gateway、失去 guard 的 API 或失去 API 的残留 timer；
   - offline stop → online start 模式切换必须证明 guard inactive、offline IP profile 已移除并消费新模式 permit；残留 guard、旧 permit、旧 mode/start receipt 或旧模式预算状态全部 fail closed；
   - 在线许可证预算在进程创建前写前计入、每日最多 10 次；未知/耗尽预算和启动校验失败时 launcher 不创建、重启或重试 API；
   - 在线预算测试覆盖日期切换、账本损坏、进程启动失败、崩溃和长进程 12 小时缓存刷新预留，不把保守账本冒充 CMMS 内部实际计数；
   - 在线运行期测试记录 12 小时缓存过期/异常后的源码重校验限制，确认 entitlement 失败会阻断 Phase 2 readiness；不得把该模式宣称为无重试或严格 fail-closed；
   - 停止命令不包含 `down -v` 或卷删除；
   - 日常 `start`/`restart` 不包含 signup、邀请、密码、角色或 API Key 写操作，状态不一致时只能生成 repair plan；
   - 冷启动和重启必须先证明 Docker gateway listener 已关闭，再启动当前 API 并通过 loopback 对账；模拟旧 receipt 配合替换/空数据库时 gateway 始终保持关闭；
   - 已健康且全绑定匹配的幂等 `start` 保留同一 MainPID、零 permit/零在线 debit；stopped start 或 restart 才创建一个 permit 与一个新 MainPID；
   - 模拟初始化器重建默认超级管理员时，gateway 不开放、宿主机服务停止且不会自动执行身份修复；
   - 启动副作用白名单和敏感文件 manifest 绑定已审查 CMMS 基线；相关源码或 migration 变化时拒绝开放 gateway；
   - 验收模式拒绝 dirty 或 gitlink 不匹配；
   - 工具链下载必须使用固定 URL 和 SHA-256，禁止 ambient 版本回退。
3. Nginx 路由测试
   - `/`、`/api/`、`/storage/` 指向精确 upstream；
   - HMR/WebSocket Upgrade 和上传边界保留；
   - `/storage/` 剥离前缀并把 upstream Host 固定为 `127.0.0.1:9000`。
   - API Key 只通过绑定契约的 method/path/idempotency-header 正向白名单；其他路由返回固定 JSON `403` 且不命中上游；
   - `/storage/` 的成功、上游拒绝与超时日志都不包含 SigV4 query、credential 或 signature。
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
- [CMMS API Key 返回值掩码](../../../components/cmms/api/src/main/java/com/grash/mapper/ApiKeyMapper.java)
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
- 所有 `apply` 共享 apply-effect 独占锁；静态有效的 plan 先写唯一的 `ATTEMPTED` 审计 reservation，竞争 loser 只把它改为 `CONTENDED`，随后在任何 gateway/HTTP/receipt/StateRecord/service effect 前失败；未知提交或补偿保留 `ATTEMPTED`/`IN_PROGRESS` 并要求新 reconciliation plan。
- 每个 plan application 都使用固定 schema、单调 generation 和状态相关的 nullable/result-code 组合；终态不可修改，不确定结果不得伪装为 `FAILED` 或 `REJECTED`。
- ActionRegistry、DeploymentPlan、规划器和 apply-time preflight 按分层职责共享同一个规范 action tuple；不序列化额外 `PlanBranch`，cleanup-only repair 只能使用固定的两行动作和隐式 claim/preflight 屏障。
- bootstrap 写前 API-loopback 检查与写后 composite pre-open 使用不同 action；fresh initializer 只由 API 启动执行一次，计划行在启动后仅验证结果。
- bootstrap completion 只能使用冻结的 receipt/probe/slot 映射所选连续后缀；已发布并完成 capture cleanup 的中断态只能走零 mutation 的 readiness-only；discovery、revoke-only 和 discard-only 必须以显式 runtime-stop action 收尾，不能与 create/publish 或 acceptance 混合。
- 未跟踪运行文件是唯一操作者管理的持久秘密来源；必要的 API 进程环境复制受本机可信边界约束，状态容器配置、日志和状态输出不泄露秘密值。
- Phase 2 API Key 只能来自 capture stat、bootstrap receipt 与 StateRecord 完整锚定的同一 raw-key/ID 证据链；发布结果先锚定、capture 后删除，未锚定捕获只能精确吊销。
- bootstrap receipt 使用 SHA-addressed immutable generations，StateRecord 是唯一 current 指针；fresh apply 的动态 capture/发布 stat 只能通过同一 claimed context 的 State-selected lineage 传给后续已计划动作，orphan generation 无权推进。
- 许可证模式明确可审计：离线模式引用受限许可证文件、强制只允许 loopback 出站并运行双向绑定的 guard；在线模式不存在残留 guard/IP profile，显示保守日预算和运行期重校验限制，并在启动预算未知/耗尽或启动校验失败时保持 gateway 关闭。
- 验收模式能证明 CMMS SHA、有效的 `API_ACCESS`/`CUSTOM_ROLES` entitlements、公司计划的 `API_ACCESS`/`ROLE` features、强制邀请门、公司身份、最小权限 API Key 和零工单基线。
- 停止、重启和失败恢复不会删除 CMMS 数据卷，也不会影响 ThingsBoard、PDM 或其他容器。
- 在后续独立确认门之前，不执行 Phase 2 provisioning、Dashboard 发布、遥测注入、训练、Alarm 或工单写入。
