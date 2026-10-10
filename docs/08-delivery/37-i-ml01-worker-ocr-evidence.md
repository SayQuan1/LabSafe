# 第37批：独立OCR文字证据与Worker原子登记

2026-10-07，本地完成，位于wsq/i-ml01-analysis-input；与第36批一起尚未提交/Push/PR。对应I-ML-01/I-03A2、R-03/R-06及AI-03/AI-04/DB-02/JOB-02/CAP-01的相关部分。仅dev/test、CPU，沿用官方D-FINE COCO80和PP-OCRv6_small det/rec ONNX；未安装CUDA/Paddle。

## 1. 已实施

- 正式InferenceResult新增必填text_regions（最多100）。每行含line_id/image_id/crop_id、原文、置信度、quad及nullable detection_id。CropRecipe新增line_id和重建摘要，detection_id允许NULL；原有OCRField仍表示已关联检测目标的语义字段，独立原文不伪造成字段或化学事实。
- 共享evidence协议校验Schema及结果闭包：质量图片集合完整且唯一；检测/文字/crop ID不可重复；文字与crop一对一且同图/同quad/同检测关联。line/crop和检测ID分别按run/image/行序或检测序UUIDv5生成，跨run移植、悬空/跨图检测、父检测环、非有限数及100以上区域均拒绝；COCO class_id与type必须对应。Worker另核租户/run/attempt/fence/hash/模型和词典回显。
- AI联合CPU开发报告附正式text_regions/crops结构子集，保留原始开发报告和真实权重证据。尚未通过已批准bundle身份门禁，未把开发报告整体当作可上线的InferenceResult服务。
- Worker在推理心跳保护内调用BoundedEvidencePrepare。独立Python证据进程运行固定版本S3 GET、共享严格RGB PNG解码、perspective-rgb-v1重建、源RGB/PNG大小及SHA/识别旋转像素摘要校验、D路径PUT与版本取得。单图顺序读取一次，逐crop处理；20秒包含启动/读取/重建/上传，取消或超时kill并wait回收（最多2秒清理），不向该进程传DB/broker句柄或数据库/AI/API存储凭据变量。
- D路径为tenant/{tenant}/lab/{lab}/derivatives/{run}/{crop}/{sha}.png。仅显式非null、可见ASCII VersionId可登记；重放只在HEAD取得版本后GET该准确版本、实测字节SHA一致时复用。目标内容冲突拒绝覆盖；不以ETag或声明metadata代替实际SHA。
- commit_result要求全量、唯一、同作用域且与配方大小/SHA一致的Worker artifacts；缺任一crop拒绝。锁定并复验当前输入、owner/token/generation/attempt/attempt_id/租约后，在同一事务写derivatives/result/result_hash、item指针、task/attempt成功及适用的fact/rule任务。attempt_id检查提前至任何裁剪登记前；事务结束前再次核租约时间。补拍、失租、被替代run、输入版本改变和后续写入故障不能留下部分数据库成功。
- 公共InferenceRun只读投影新增text_regions，沿用既有租户和读取授权；历史结果缺该字段时返回空数组，不改历史JSON或hash。公共EvidenceRef已有nullable detection_id/crop_id，可引用独立文字crop；人工事实主体仍须真实检测ID。复核额外拒绝检测与证据图片不一致；本批不实现crop下载/UI。

## 2. 迁移、兼容与回滚

新增0004_ocr_evidence，前置0003_run_image_version：image_derivatives.detection_id改nullable，追加nullable line_id CHAR(36)/size_bytes BIGINT UNSIGNED，唯一(tenant,run,crop)及tenant/run/image FK保持原样。历史行新列NULL，原ID/检测/配方/对象版本/SHA均不回填、不重写；新提交必须具有完整line/size及结果闭包。head结构为43表、115FK。0001–0003及历史快照保持不变，ready与结构比较基准同步至0004。

内部候选协议新增必填字段，旧生产者缺text_regions/line/evidence将被拒绝；Worker、fixture/未来真实AI需协调升级。公共投影增加字段但路径、命令输入与EvidenceRef未变。历史run仍按已有只读流程保留；不重新解释旧crop为文字区域，也不利用当前asset版本补写旧run。

回滚优先停止新任务、保留证据对象/004列及审计记录，使用兼容head=004的回滚构建或前向修复；旧003应用严格ready检查不会自动放行004，不能只降应用而宣称恢复可用。downgrade仅允许明确确认的可丢弃test schema，且存在任何detection_id=NULL证据时拒绝004→003，禁止伪造检测、删除历史证据或为满足NOT NULL填随机ID。真实部署仍需按协作规范双人确认。

## 3. 本机验证

| 范围 | 命令/环境 | 成绩 |
|---|---|---|
| 协议与Worker失败路径 | .venv-business pytest test_inference_evidence及test_inference_evidence_storage | 60通过；重复/悬空/跨图/跨run/跨租户、100/101、可选真实关联、环、NaN、artifact缺失/重复/错版本、心跳失租、SDK错误/截断/SHA/目标冲突 |
| 真实图像与SDK | .venv-evidence unittest tests.worker.test_evidence_pixels | 5通过、无跳过；两个crop共享一次源GET、90°识别、坏摘要/配方、第二次上传失败、SDK固定版本/PUT/准确复用、真实进程超时/取消kill及回收 |
| CPU与协议回归 | .venv-ai unittest：dfine_adapter/cpu_runner/cpu_pixels/ocrv6_adapter/quality_pixels/pipeline_cpu/crop_evidence/analysis及protocol.test_contract | 63 CPU＋4协议通过，无跳过 |
| 业务全量回归 | .venv-business pytest tests/business tests/domain tests/security tests/protocol | 1613通过；含上述定向用例，已有anyio弃用提示 |
| 真实MySQL定向 | PYTEST_ADDOPTS指定inference_execution_mysql/constraints/inference_submit_mysql/review_mysql/test_unit；tools/database/run_mysql_tests.py --mysqld 本机路径 | MySQL8.0.33，40通过，含21项持久化单元；非全部持久化套件 |
| 真实模型→Worker工程smoke | .venv-ai tools.ml.smoke_worker_evidence，Worker使用.venv-evidence | 两张合成中英标签图、6文字区/6裁剪；真实ONNX、正式文字结构、独立Worker、准确版本和SHA重放、第二crop失败无部分成功通过 |

MySQL覆盖有历史数据003→004保持旧行、nullable新文字行、downgrade保留门禁及可丢弃库完整迁移循环；needs_review与facts_ready两种成功路径；owner/token/generation/attempt/attempt_id、过期/superseded/input版本改变拒绝；裁剪已插入后注入result写入故障，全部裁剪、事实及task成功回滚；公共文字只读投影一致。首次测试fixture复用已关闭连接已修正；attempt_id围栏的过晚检查已修复并重跑，未改历史迁移。

最新smoke可核对[结构化记录](37-i-ml01-worker-evidence-smoke.json)：模型真实，S3为版本化loopback模拟器，测试身份外壳为合成请求，不代表已批准真实bundle；数据库原子提交在独立MySQL用例验证，没有把分开的测试冒充真实生产端到端。失败上传可留下未引用孤儿；本批不会删除对象或将其暴露为已登记证据。

静态/设计验证：ruff check及format --check、build_specs --check、148项readiness设计检查、validate_specs --report、三个独立环境pip check及git diff --check。新增CI独立worker-evidence任务；本机成绩不代表远程CI已执行。

## 4. 剩余边界与下一批

I-ML-01和AI-10仍进行中。第38批接常驻真实CPU supervisor、quality/runs、受控development bundle/runtime-lock/词典身份、health/ready/version、串行并发门禁及超时故障重载。之后再接文字/瓶子关联、词法/日期/词典事实及关系。crop签名下载/UI、孤儿清理、实际MinIO/IAM/TLS、现场授权数据/准确率、资源验收及生产批准仍待完成。
