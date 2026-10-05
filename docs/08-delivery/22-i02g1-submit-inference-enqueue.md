# I-02G1 `submitInspectionItem` 与推理任务入队验收

状态（2026-10-03）：本批在既有领域 `submit_item` guard 上接通真实 HTTP 写命令。请求只接受 `expected_version` 与有序 `images`；租户、实验室、父巡检、图片 owner/ready 状态、activation 和历史 findings 均由服务端在同一事务加载。

提交事务按 tenant/session → item → 当前 findings/images → activation 的顺序读取并锁定。成功时旧 run/evaluation/findings 被标记 superseded，item 的 `submission_revision` 与 version 增加，新 `inference_runs` 固定 published model/dictionary/rule、pipeline、device、UTC reference date 和 input hash，`run_images` 保存图像 SHA 与顺序；随后原子创建 `inference_pipeline` task、task-message-v1 `TaskDispatch` outbox、审计和幂等响应。客户端不得提交模型、规则、设备、日期、对象 key 或租户字段。

激活读取同时要求模型的 `dictionary_version_id` 与激活记录一致，并且模型、词典均为 `published`；不接受跨词典拼接的配置入队。

本批没有 AI 推理结果，也没有把任务标记 succeeded。统一 TaskDispatch 校验新增已有契约定义的 `inference_pipeline` 分支；发布器可从 outbox 投递该消息，但 Worker 的 inference lease、独立进程、超时、fencing、结果提交和重放仍属于后续 I-03/AI 批次。

验证入口：

- `tests/persistence/test_inference_submit_mysql.py`：真实 MySQL activation、ready image、run/task/outbox 和 item pointer 验收；
- `tests/business`：路由、严格请求和既有调度协议回归；
- `tools/design/build_specs.py --check` 与 `tools/design/validate_specs.py`：契约/链接/就绪检查。

关闭条件：本批专门 MySQL 测试已通过；合并前仍需增加并发同 key/不同 key、stale version、跨租户/实验室、旧 run fencing、任一写步骤失败回滚用例。I-02G1 不关闭 JOB-04、独立 AI 进程或生产发布门禁。
