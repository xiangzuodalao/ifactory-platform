# 平台契约

此目录用于集中维护 CMMS、ThingsBoard、PDM Algorithm 与 `digital-mcp` 之间稳定、可评审的公开契约。当前阶段只完成工作区和 Git 结构整理，尚未定义或发布跨系统业务接口。

后续新增集成时，按契约类型放置：

```text
contracts/
├── openapi/       # 同步 HTTP API
├── asyncapi/      # 事件与消息主题
└── json-schema/   # 跨服务共享的消息体 schema
```

约束如下：

- 契约描述跨进程接口，不复制任一组件的内部 ORM、数据库表或私有领域对象。
- 每个契约必须声明负责组件、版本、兼容策略以及错误与幂等语义。
- 跨系统写操作应携带 `tenant_id`、`correlation_id` 和 `idempotency_key`；设备/资产关联应显式区分 `equipment_id`、`cmms_asset_id` 与 `tb_device_id`。
- 破坏兼容性的变更必须使用新版本，并先完成消费者契约测试，再升级组件指针。
- 凭据、真实业务数据、数据库连接配置和生产响应样本不得进入此目录。

在首个真实集成接口落地前，不要创建推测性的 schema；由提供方和消费者在同一变更中共同提交契约及测试。
