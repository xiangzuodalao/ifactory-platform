---
name: pdm-train-model
description: Safely prepare, confirm, run, and verify exactly one Informer or Autoformer training job for an already registered PDM equipment/measurement scenario through the local digital-platform MCP server. Use when a user asks to train or retrain one registered PdM model, preview a training plan before execution, check an uncertain submitted training job, or verify the newly trained model with one prediction. Enforce a separate-turn confirmation gate, a new user-supplied ModelInfoID, and no-retry execution. Do not use for scenario onboarding, batch training, or model publication.
---

# PdM 受控单次训练

仅通过本地 `digital-platform` MCP 对已注册场景执行一次受控训练。调用工具前完整读取 [training-contract.md](references/training-contract.md)。

## 执行流程

1. 调用 `pdm_health_check`。服务不健康或不可达时立即停止，不尝试训练。
2. 调用 `pdm_list_models`，按用户给出的设备、测点、模型类型和数据源做精确匹配。只接受唯一的已注册场景及 `informer`/`autoformer`；零个或多个匹配都先让用户消除歧义。
3. 要求用户提供一个全新的 `ModelInfoID`。不要生成、改写、猜测或复用 ID；由预检判定冲突。
4. 调用 `pdm_prepare_training`。除非用户在预检前明确要求平台副作用，否则使用 `local_only`。传入用户要求的参数，不自行扩张训练规模。
5. 若返回 `usable=false`、任一训练/验证/测试窗口为零或存在阻断错误，停止并展示原因，不调用执行工具。
6. 展示预检返回的完整 `plan_hash`、`ModelInfoID`、唯一场景、模型类型、数据源、最终有效参数、三组窗口数、执行模式及副作用。保留 `preview_id` 供后续执行。
7. **在完成预检的当前对话轮结束。** 即使用户在最初请求中说过“已确认”“直接执行”或同义表述，也不得在这一轮调用 `pdm_train_model`；必须等待用户在看到预检结果后的下一条消息中明确确认。
8. 下一条消息只在明确确认当前预览且 ID、参数、场景、模型类型、数据源和执行模式均未变化时有效。任何变化、预览过期或计划漂移都要重新调用 `pdm_prepare_training`、重新展示结果并再次跨轮确认。
9. 确认有效后，仅调用一次 `pdm_train_model(preview_id)`。提交后视为预览已消费；超时、断线、5xx 或结果未知时绝不重试训练。
10. 训练结果未知时调用 `pdm_get_training_status`。状态仍为 `reserved`/`running` 时只报告或继续查询状态；不得再次提交。状态为 `failed`/`interrupted` 时报告失败并停止。
11. 训练成功后使用相同场景和新 `ModelInfoID` 调用一次 `pdm_predict`，只验证 checkpoint 可加载及响应结构合法，不评价预测业务含义。

## 边界与结果

- 每个任务只处理一个已注册场景和一个新模型 ID；不接入场景、不批量训练、不发布或上线模型。
- 不绕过 `prepare`、跨轮确认或一次性预览；不可用数据不得训练。
- 不输出凭证、连接串、绝对产物路径、堆栈或原始时序数据。
- 成功时只报告 `best_val`、`test_loss` 和预测响应结构验证结果。称其为损失指标，不称“准确率”，不据此判断故障、健康状态或维修需求。
- 需要第二次训练时，将其视为新任务：要求另一个全新 ID，并重新完成全部流程。
