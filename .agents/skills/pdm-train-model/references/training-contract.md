# PdM 单次训练契约

## 工具顺序

只使用本地 MCP Server `digital-platform` 暴露的以下工具，并遵循运行时工具 schema：

| 工具 | 用途 | 关键结果 |
|---|---|---|
| `pdm_health_check` | 在任何训练工作流开始前检查 API。 | `healthy`/服务状态。 |
| `pdm_list_models` | 用设备、测点、模型类型、数据源筛选已注册场景。 | 可精确匹配的场景列表。 |
| `pdm_prepare_training` | 解析最终训练计划并检查数据；不执行训练。 | `preview_id`、`plan_hash`、最终参数、窗口数、`usable`、副作用和有效期。 |
| `pdm_train_model` | 消费一个已确认的 `preview_id`，同步提交一次训练。 | 训练状态及成功时的 `best_val`、`test_loss`。 |
| `pdm_get_training_status` | 查询已提交训练，尤其处理结果未知。 | `reserved`、`running`、`succeeded`、`failed` 或 `interrupted`。 |
| `pdm_predict` | 成功后用新模型执行一次结构冒烟验证。 | 预测时间点和值等结构化响应。 |

`pdm_check_training_data` 是独立只读诊断工具；标准训练流程以 `pdm_prepare_training` 返回的数据检查为准，不要用它替代预检或确认门禁。

## 预检输入约束

- 从 `pdm_list_models` 选择设备与测点字符串完全相等的唯一场景，不使用模糊匹配结果执行训练。
- 仅允许 `informer` 或 `autoformer`。不尝试训练 `testmodel`、`xlstm` 或 `timellm`。
- `ModelInfoID` 必须由用户原样提供且为新 ID。冲突后请求另一个用户提供的 ID，不追加时间戳或随机后缀。
- 默认 `execution_mode=local_only`。只有用户在生成预览前明确要求平台回写/上传时才使用 `platform`。
- 将用户给出的参数覆盖传给预检；未指定字段保留服务解析出的默认值。不要自行调高 epochs、窗口或模型规模。
- CSV 路径只能使用服务允许的数据目录内相对路径；不要传递主机绝对路径。

## 可执行预览

只有同时满足以下条件才能请求确认：

- 场景精确且唯一；
- `usable=true`；
- train、validation、test 三组窗口数都大于零；
- `preview_id` 和 `plan_hash` 均存在；
- 预检未报告 ID 冲突、参数非法、路径非法或不支持的模型/数据源。

向用户展示：

1. 设备、测点、模型类型和数据源；
2. 用户提供的 `ModelInfoID`；
3. 完整 `plan_hash`；
4. 最终有效参数；
5. train/validation/test 窗口数；
6. `local_only` 或 `platform` 及服务返回的全部副作用说明；
7. 预览有效期（若返回）。

展示后结束当前回复，只请求用户在下一条消息中确认该预览。首次请求中提前给出的确认永远不能满足这一门禁。

## 确认与提交

下一条用户消息必须明确表达执行刚展示的计划。含糊回复、提出问题或仅确认部分参数不构成确认。

确认前比较用户消息与缓存预览：设备、测点、模型类型、数据源、ID、参数或执行模式有任何变化，都废弃旧预览并重新预检。`preview_id` 过期、计划 hash 变化或服务拒绝预览时也执行相同步骤。

确认有效后只调用一次：

```text
pdm_train_model(preview_id=<已确认预览的 ID>)
```

不要同时传入参数修改，不要并行提交，不要因为没有立即收到成功响应而重试。

## 状态与错误处理

| 情况 | 动作 |
|---|---|
| `PDM_TRAIN_OUTCOME_UNKNOWN`、超时、断线或提交阶段 5xx | 使用同一 `ModelInfoID` 调用 `pdm_get_training_status`；绝不再次调用训练。 |
| `reserved` / `running` | 只查询或报告状态，等待终态；不得重发。 |
| `succeeded` | 记录损失指标，继续一次预测结构验证。 |
| `failed` / `interrupted` | 报告脱敏错误并停止；再次训练必须使用新 ID、新预览。 |
| 预览过期或 `plan_hash` 漂移 | 重新预检、重新展示并等待新一轮确认。 |
| 数据不可用或任一窗口为零 | 停止；不要降低门槛或绕过检查。 |
| ID 冲突 | 要求用户提供另一个全新 ID；不要自动构造。 |

不要向用户转发上游堆栈、连接配置、绝对路径或原始数据。只解释稳定错误码和安全的摘要。

## 成功验证与报告

训练成功后只调用一次 `pdm_predict`。使用已确认场景和刚训练的 `ModelInfoID`，验证：

- checkpoint 能被服务加载；
- 响应包含预测时间点和值的结构；
- 对应数组长度一致且响应非空（以运行时 schema 为准）。

最终报告只包含：

- `best_val`（最佳验证损失）；
- `test_loss`（测试损失）；
- 预测结构验证通过或失败及安全的失败摘要。

不要把损失换算或描述成准确率，不分析数值代表的设备故障、健康等级或维修建议。
