# 第40批：OCR字段词法、严格格式与完整多行证据

2026-10-09，本地完成与验证；沿用 `wsq/i-ml01-analysis-input`，第36–40批尚未提交/Push/PR。本批对应 I-ML-01、R-03/R-06、CAP-01 及 AI-10 的字段部分，不关闭实体候选、日期事实、现场评测或生产门禁。

## 1. 实现与契约

新增共享纯 Python `packages/inference_protocol/fields.py`，算法版本 `ocr-fields-v1`。输入只接受已经通过 `text-bottle-quad80-v1` 关联的真实同图唯一 COCO bottle；detection_id 为 NULL 的文字保留人工证据，不产生 OCRField。

`OCRField` 新增必填 `source_lines`，每项保存完整的 `line_id`、`crop_id`、原始 `raw_text` 和该行 `confidence`，数量为 1–2。字段的 `crop_id` 是第一行引用，所有来源裁剪均通过 `source_lines` 追溯；跨行 `raw_text` 使用单个 LF，字段 confidence 是实际来源行的最小值。原文、quad、line/crop ID、像素摘要和 bottle 关联不被标准化覆盖。

词法执行 NFKC、连续空白压缩、固定最长前缀和英文边界检查。支持名称/浓度/危险标识前缀、有效期/生产/开封日期前缀；日期只接受完整的两位月日公历 `YYYY-MM-DD`、`YYYY/MM/DD` 或 `YYYY年MM月DD日`。CAS 只接受 2–7 位、2 位、1 位的格式并核验校验位。非法/不完整日期和 CAS 保留字段但 normalized_text 为 NULL；同行多个日期关键词输出 `date_unknown`。未冻结业务词典时，不把任意无前缀文本猜成名称。

跨行只消费全局 OCR 顺序中的**紧邻下一行**，且必须同图、同唯一 bottle、非空、无任何定义前缀或多日期关键词。不会先过滤未关联/其他瓶/空白行再跨越配对；前缀已有值时不消费下一行；所有同 kind 字段和冲突保持原顺序。

协议生成源和输出已同步：`contracts/inference-v1.yaml`、`contracts/public-api-v1.yaml`、`packages/inference_protocol/contract.json` 等加入 `OCRFieldSource`/`source_lines`。没有数据库迁移，仍为 `0004_ocr_evidence`；历史结果不回填。

## 2. Worker 与流水线门禁

CPU 联合流水线升级 `quality-coco80-ocrv6-cpu-v4`/`cpu-pipeline-local-v4`，在关联后从正式 `text_regions` 生成字段并记录 `ocr-fields-v1` 与字段耗时。`runtime_cpu.wire_result` 只转发报告字段，仍固定 `needs_review`，不产生 chemical entity、DateFact 或事实快照。

`validate_closure` 在存储读取前从完整文字区域重新运行词法，逐字段比较字段顺序、字段类型、规范文本、原文、最小置信度、首行 crop 以及每一条 source line。缺行、伪造值、反转顺序、跨瓶/跨图来源、单 crop 冒充多行、超容量或任何坏引用都使整批失败；不会部分提交裁剪或结果。

## 3. 本机验证

| 验证 | 结果 |
|---|---|
| OCR 字段黄金样例与闭包 | 83 项通过：NFKC/空白、最长前缀、CAS 校验、严格日期、date_unknown、低置信、容量边界和全局紧邻配对 |
| Worker/字段闭包 | 多行两 crop、遗漏/重复/错误 crop/line/原文/置信度/顺序/规范文本/瓶子引用均拒绝 |
| 业务/领域/安全/协议回归 | 最终 1755 项通过；套件有重叠，不相加为唯一测试数 |
| CPU/HTTP/进程/协议 | 75 项 CPU 回归、25 项 fixture/ASGI 回归通过；末轮词法调整后定向 CPU/像素/协议 17 项再通过；无跳过 |
| 独立 Worker 像素环境 | 8 项通过；两行字段的两个实际裁剪均重建并校验后才登记 |
| 隔离 MySQL 8.0.33 | 50 项通过；`0004_ocr_evidence`、43 表、115 外键，无错误；needs_review 结果完整保存 source_lines |
| 设计/契约/静态检查 | `build_specs --check`、148 readiness、11 JSON Schema/423 文档链接、ruff/check-format、三环境 pip check/隔离、diff check 通过 |

业务与MySQL套件各有一条既有 Starlette/anyio 弃用警告，无测试失败；远程 CI 未执行。CI 已沿用业务 pytest、新HTTP unittest 和实际 CPU/Worker 像素入口覆盖新增测试。实际命令为：

~~~powershell
.venv-business/Scripts/python.exe -B -m pytest tests/business tests/domain tests/security tests/protocol -q
.venv-business/Scripts/python.exe -B -m unittest tests.ai.test_fixture tests.ai.test_supervisor tests.ai.test_cpu_http -q
.venv-ai/Scripts/python.exe -B -m unittest tests.ai.test_dfine_adapter tests.ai.test_cpu_runner tests.ai.test_cpu_pixels tests.ai.test_ocrv6_adapter tests.ai.test_quality_pixels tests.ai.test_pipeline_cpu tests.ai.test_crop_evidence tests.ai.test_analysis tests.ai.test_supervisor tests.ai.test_text_association_pixels tests.protocol.test_contract -q
.venv-evidence/Scripts/python.exe -B -m unittest tests.worker.test_evidence_pixels -q
$env:PYTEST_ADDOPTS='-q tests/persistence/test_inference_execution_mysql.py tests/persistence/test_constraints.py tests/persistence/test_inference_submit_mysql.py tests/persistence/test_review_mysql.py tests/persistence/test_unit.py'
.venv-business/Scripts/python.exe -B tools/database/run_mysql_tests.py --mysqld 'D:/SQL/MySQL/MySQL Server 8.0/bin/mysqld.exe'
~~~

## 4. 官方权重与素材图片 smoke

机器记录见 [40-i-ml01-ocr-fields-smoke.json](40-i-ml01-ocr-fields-smoke.json)。使用官方 D-FINE COCO80 ONNX、PP-OCRv6_small det/rec ONNX、CPUExecutionProvider、loopback 版本对象存储和独立 Worker：两张合成标签图加用户提供现场图片共 3 张，12 条文字区域、12 个裁剪，正式模型检测到 0 个 bottle，全部关联 NULL，字段数量为 0。

用户现场图片通过默认质量门禁（blur 168.254479、brightness 0.4679936、glare 0.0012881），但官方模型在既定 640 直接拉伸预处理下最高 bottle 分数约 0.328，低于 detection_min=0.4，因此没有降低阈值或伪造检测。该结果说明当前工程接线可安全保留未知，不是现场准确率结论。

另以明确标记的全图 synthetic bottle 框绑定同一批真实 ONNX OCR/像素裁剪，独立验证 4 个字段、其中 2 个跨行字段及全部 source crop 的 Worker 重建。这个正向探针用于证据闭包，不代表 D-FINE 在素材图片上的检测成绩，也不构成现场标注评测。

## 5. 兼容、回滚与下一批

字段形状和代码摘要变化后必须在新空目录重新生成 development bundle/lock，协调升级 AI、Worker 和内部消费者；旧固定身份不能继续接受新字段。回滚停止新流量、排空/回收常驻 CPU、恢复成套旧代码/契约/bundle/Worker pin；不删除数据库、对象或改写历史结果。无新迁移，无 downgrade。

下一批为第41批：先冻结开发化学词典的 entity_id/canonical_name/alias/CAS、版本和内容 SHA，再实现同瓶 CAS/别名候选与名称冲突聚合。没有可信词典条目时继续 unknown；确定 DateFact、事实快照、容器关系、现场评测和生产批准另行验收。
