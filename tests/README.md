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
