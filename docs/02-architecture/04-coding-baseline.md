# 02.4 编码入口与跨模块约定

状态：设计候选 1.1.0。见 [审计结论](../08-delivery/03-design-audit.md)；本文件不替代主题规范。

## 1. 首期边界

一次巡检项提交 1–3 张静态图片；第一张总览，其余为显式关联的细节图。支持质量、瓶/标签检测、OCR、实体候选、可见关系、规则候选、人工复核和整改闭环。不支持视频、在线 LLM、自动风险定性或跨主机 GPU 调度。

## 2. 技术栈与代码边界

| 目录 | 实现职责 | 禁止事项 |
|---|---|---|
| apps/web | Vue 3、TypeScript、Vite、Element Plus、Vue Router、TanStack Query；Pinia 只管会话/UI | 复制状态机、长期对象凭据 |
| apps/api | Python 3.11、FastAPI、Pydantic v2；路由调用命令/查询 | 加载模型，外部调用持有 DB 事务 |
| apps/worker | Celery、Redis、持久任务、AI client、规则、裁剪、导出 | 覆盖历史结果 |
| apps/ai_inference | Python3.11独立FastAPI supervisor + spawn计算子进程；D-FINE-N四类ONNX/PP-OCRv4 CPU；建议检测CUDA FP32，CPU功能保留；业务API/Worker同为Python3.11，依赖环境仍隔离 | 业务ORM、数据库、Redis、最终风险 |
| packages/domain | 状态、guards、权限动作、不变事实类型 | 通用 status setter |
| packages/application | 命令处理器、查询、unit_of_work；每命令唯一入口 | 路由另写业务逻辑 |
| packages/persistence | SQLAlchemy 2、MySQL 8.0.16+、Alembic、Outbox repository | 省略租户条件 |
| packages/rules | evaluate(facts,bundle,reference_date) 纯函数 | 网络、系统时间、动态 eval |
| contracts / tests | 生成协议；contract/unit/integration/e2e/fault/evaluation 分层 | 模拟成绩冒充真实评测 |

依赖：HTTP/任务入口 → application → domain；persistence 实现仓储接口。AI 只共享协议和图像变换包。应用依赖锁文件和镜像 digest 在实施环境验证后提交；没有完成依赖兼容性验证前不得宣称环境可复现。

统一运行时依据见[ADR-PY-01](07-python-runtime.md)；I-01A开发入口与测试见[开发指南](../../DEVELOPMENT.md)。统一minor不合并AI与业务依赖，也不改变进程/权限边界。

## 3. 事务模板

1. 会话给出租户，服务端授权；不接受客户端 tenant_id 作为权限依据。
2. 校验 Schema、幂等键；锁幂等行。
3. 按 tenant_id+id SELECT FOR UPDATE 聚合根；校验 expected_version、状态、关联对象、证据与版本。
4. 更新聚合、version+1、updated_at；追加审计、Outbox、任务；保存幂等响应。
5. 原子提交后返回。对象/AI/Redis 调用在事务外；失败回滚所有业务写入。

统一锁序：幂等 → inspection → item → run → finding → remediation_task → task_run；同类多行按 ID 排序。数据库死锁最多重跑事务 3 次，重新检查守卫；不能重复执行外部副作用。

## 4. 类型与不变量

- 资源 ID 默认 UUID v4 小写；确定性检测/裁剪 ID 按 AI 文档使用 UUIDv5。时间为 UTC RFC3339 毫秒，DB DATETIME(3)。reference_date 在提交时按 tenant.timezone 生成并冻结。
- 通用字符串 NFC 去首尾空白；密码和 OCR raw_text 不处理；账号 NFKC+casefold 后唯一。
- JSON哈希：UTF-8、键字典序、无空格、禁止NaN、保留数组顺序；共享规范函数为json.dumps(sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)。Python3.11生产者使用同一函数和黄金样本测试；不是任意语言默认JSON字符串。原图/制品哈希直接对字节计算。
- trace_id 为 32 位小写十六进制；request_id/attempt_id 是 UUID。
- 原图、分析图、裁剪图各自保留对象 key、version、SHA，禁止混用。
- 终态结果、事实正文、发布规则/词典正文不可覆盖；元数据只能通过规定命令变化；activation 只影响新 run。
- 所有并发命令使用目标聚合 version；409 VERSION_CONFLICT 时刷新，不自动覆盖。

## 5. 必读规范

1. [状态机](02-domain-model.md)、[AI 进程](03-ai-inference-process.md)。
2. [持久任务](05-durable-jobs.md)、[数据约束](06-data-persistence.md)。
3. [接口语义](../03-interfaces/01-api-contract.md)、[规则算法](../04-ai-rules/01-data-and-rules.md)、[安全](../06-security/01-security-privacy.md)。
4. [前端](../05-frontend/01-interaction-spec.md)、[测试](../07-quality-operations/01-testing-evaluation.md)、[部署](../07-quality-operations/02-deployment-operations.md)。
5. [模型与数据基线](../04-ai-rules/02-model-data-plan.md)、[分层验收政策](../07-quality-operations/04-acceptance-policy.md)、[设计/实施/发布门禁](../08-delivery/05-stage-gates.md)。
6. [确定性事实提取](../04-ai-rules/03-fact-extraction-contract.md)、[IRR关闭清单](../08-delivery/06-implementation-readiness.md)。开工按关闭后的状态/容量/nullable/S3规范，不复制旧审计反例行为。

## 6. 判定原则

结构检查不能替代语义审计。每个命令必须有前置条件、写集、权限、错误、用例；算法适配、故障恢复必须有明确预期。设计可编码不等于人工批准或可上线。
