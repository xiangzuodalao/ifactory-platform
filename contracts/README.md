# 平台契约

此目录用于集中维护 CMMS、ThingsBoard、PDM Algorithm、`platform-integration` 与 `digital-mcp` 之间稳定、可评审的公开契约。Phase 1 已定义 PDM 预测、CMMS 资产集成和设备映射边界，并由 `tests/contract/phase1` 中的离线契约测试验证；这不代表生产部署或在线写操作已启用。

契约按类型放置：

```text
contracts/
├── openapi/       # 同步 HTTP API
├── asyncapi/      # 事件与消息主题
└── json-schema/   # 跨服务共享的消息体 schema
```

Phase 1 的契约责任与兼容策略：

- `pdm-prediction-v2`：provider=PDM（`pdm-algorithm`），consumer=`platform-integration`，additive v2；旧版 `/measPredict/predict` 保持不变。
- `cmms-integration-v1`：provider=CMMS，consumer=`platform-integration`，additive fields/endpoints；普通资产创建保持兼容，集成调用新增可选 `equipment_id`、幂等语义和按设备标识查询端点。
- `equipment-mapping-v1`：定义租户范围内 `equipment_id`、`cmms_asset_id` 与 `tb_device_id` 的启用映射形状；跨记录唯一性由 `platform-integration` 存储层执行。

约束如下：

- 契约描述跨进程接口，不复制任一组件的内部 ORM、数据库表或私有领域对象。
- 每个契约必须声明负责组件、版本、兼容策略以及错误与幂等语义。
- 跨系统写操作应携带 `tenant_id`、`correlation_id` 和 `idempotency_key`；设备/资产关联应显式区分 `equipment_id`、`cmms_asset_id` 与 `tb_device_id`。
- 破坏兼容性的变更必须使用新版本，并先完成消费者契约测试，再升级组件指针。
- 凭据、真实业务数据、数据库连接配置和生产响应样本不得进入此目录。

新增契约由提供方和消费者在同一变更中共同提交契约及测试；不为尚未批准的链路创建推测性 schema。
