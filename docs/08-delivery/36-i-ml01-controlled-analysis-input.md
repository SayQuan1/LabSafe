# 第36批：I-ML-01 受控analysis输入与对象版本冻结

2026-10-07，本地实施完成；分支wsq/i-ml01-analysis-input，基于已合并PR #11/#12的origin/main=13a854f。本批未提交/Push/PR，不代表远程CI或独立审查。覆盖R-03/R-06、AI-03/AI-04/DB-02/JOB-02的局部输入与执行基础。

## 1. 实现范围

- 提交事务锁定ready图片的SHA及准确analysis对象版本，冻结到run_images和run input_hash。InferenceImage/ImageRef新增object_version，版本参与request_hash，自动重试仍排除attempt/token/deadline。公共提交DTO不接受客户端对象版本。
- Worker装载冻结版本；历史NULL或asset版本替换拒绝claim，执行后版本变更拒绝旧lease结果提交。未猜历史版本、未回退latest。
- AI纯stdlib只读S3 SigV4 GET：固定labsafe-private/us-east-1、显式endpoint/tenant allowlist、独立secret文件；无SDK/provider chain、业务ORM、Redis、代理或重定向。HTTPS按系统信任链校验，HTTP仅loopback开发测试，无PUT/List/Delete能力。
- 下载前校验生成Schema、请求hash、UUID/准确analysis key、唯一overview、detail父图/位置/顺序。核验响应准确版本、唯一Content-Length/Type/Version头、PNG MIME、无传输/内容压缩、流式SHA及截断；每次读都受剩余deadline约束。
- analysis允许1字节至128MiB、单边≤10000、≤40M像素，必须静态RGB PNG、加载前后均无元数据。AI不再旋转EXIF、转换RGB或resize；本地原图CLI的12MiB上限保留。超过12MiB的合成analysis PNG已实际解码验证。
- run_analysis_pipeline把读取/解码接入现有同批质量→D-FINE COCO80→PP-OCRv6_small CPU流程。保留Worker图片ID、一次解码/模型加载、整批补拍门禁、100检测/100文字容量和失败无部分结果。quality-only≤10秒、联合≤180秒，与请求和配置期限取最小值，含spawn和所有工作；超时kill/join。
- 输出cpu-pipeline-analysis-v1开发报告，含request_hash/准确版本/真实模型和runtime摘要。请求bundle只做结构/hash校验，不宣称匹配已批准模型身份；报告不作为InferenceResult落库，business_capabilities仍不可用。

主要文件：apps/ai_inference/analysis.py、inputs.py、analysis_cpu.py及cpu.py/pipeline_cpu.py；packages/domain/inference_execution.py；packages/persistence/inference_runs.py/inference_execution.py/schema.py；tools/design生成源与contracts；对应AI/业务/MySQL测试；工具smoke_analysis_cpu及CI。

## 2. 协议与迁移

新增head `0003_run_image_version`，在run_images追加nullable analysis_object_version VARCHAR(200)。nullable只支持历史数据，新提交必须非空、1–200可见ASCII字符且非null版本。内部InferenceRequest的object_version必填，缺失、空、控制字符、null版本或超长均拒绝；未部署1.1.0候选允许同步更新Worker/AI，外部既有消费者需另行迁移，禁止兼容读latest。公共DTO和消息schema_version不变。

保留0001/0002和初始JSON/SQL快照，只读schema核对基准与API ready更新为0003。专用dev/test库执行upgrade/verify；未升级不ready。已存在run的新列保持NULL，不把asset当前版本当提交时版本；旧run需要授权用户受控重新提交，保留旧run/任务/审计。不能把旧任务失败等同于新run完成。

回滚优先恢复上一应用版本、保留新增列和记录，重新启用时仍需新head。生产/有数据库不drop列，不删除证据或审计；downgrade仅允许APP_ENV=test且显式确认的可丢弃schema。本批只在随机loopback端口/临时数据目录的独立MySQL运行迁移，不修改既有开发库。

## 3. 实际验证

Python3.11.4；CPU环境ORT1.20.1/NumPy2.2.2/Pillow11.3.0/OpenCV4.11.0.86/pyclipper1.3.0.post6，未新增CUDA/Paddle或业务依赖。签名的独立botocore oracle仅在业务测试环境运行，包含VersionId的 `/+=%` 编码。

| 验证 | 命令/范围 | 本机结果 |
|---|---|---|
| CPU与协议 | .venv-ai python -m unittest：dfine_adapter/cpu_runner/cpu_pixels/ocrv6_adapter/quality_pixels/pipeline_cpu/crop_evidence/analysis及protocol.test_contract | 63 CPU + 4协议，无跳过 |
| 主业务回归 | .venv-business python -m pytest tests/business tests/domain tests/security tests/protocol -q | 1552通过，含签名oracle；已有anyio弃用提示 |
| 持久化单元 | .venv-business python -m pytest tests/persistence/test_unit.py -q | 21通过 |
| 定向真实MySQL | PYTEST_ADDOPTS=-k "inference or migration or schema or structure"；tools/database/run_mysql_tests.py --mysqld 本机MySQL路径 | MySQL8.0.33，18通过/429未选；含2项重叠单元，未把未选算通过 |
| 真实CPU工程smoke | .venv-ai python -m tools.ml.smoke_analysis_cpu，完整参数见DEVELOPMENT 3.20 | 合成横/竖中英标签，真实官方权重；固定版本GET、重复执行、质量一致、6个裁剪跨进程重建通过 |

MySQL覆盖：有旧数据的0002→0003升级保持所有旧列/主键/SHA、版本NULL；结构43表115FK无差异；无确认downgrade拒绝、可丢弃库完整downgrade/upgrade；提交版本/input_hash冻结；历史NULL和asset版本替换claim拒绝；已执行lease的版本替换提交拒绝，回滚替换后原lease正常提交。首轮结构核对漏新增列导致失败，修复head比较基准后重跑通过，初始快照未改。

AI覆盖：跨租户/非法key/非canonical UUID/无版本/控制字符等请求不出网；不跟302/404；重复或缺失头、错误MIME/版本/SHA、超限、压缩、截断均拒绝；慢header/HTTP1.0滴流和子进程时限/回收已测。PNG覆盖非RGB、APNG、EXIF、加载后文本metadata、尺寸拒绝和超过原图12MiB上限；远程整批补拍不加载模型、图片ID和版本保留已测。

固定版本404返回OBJECT_NOT_FOUND，不读取latest；其他非200响应拒绝。共享校验保留既有fixture的PNG MIME错误VALIDATION_ERROR；相关15项fixture回归通过。全AI回归在业务测试环境运行78项，43通过/35缺数值依赖跳过；这些跳过由独立真实CPU的63项验证覆盖，不能记作fixture环境中的通过。

机器证据：[真实analysis CPU smoke](36-i-ml01-analysis-cpu-smoke.json)。6次成功GET对应两图×两次联合及一次quality；错误版本另一次GET后整次拒绝，整批黑图停止模型加载。local/analysis/quality-only质量分数完全一致，ETHANOL/EXP 2027-12-31/乙醇实际OCR一致，重放图像ID/检测/文字/crop摘要一致。对象存储为loopback模拟器，模型推理是真实官方ONNX；不计现场准确率或生产性能批准。

静态/设计命令：ruff check与format --check（apps/packages/tests/tools/database/tools/ml），build_specs --check、test_readiness_design（148 synthetic）、validate_specs --report、两环境pip check、git diff --check。设计成绩取design-validation-results.json，不替代真实应用或部署验收。

## 4. 剩余边界

下一批继续常驻真实CPU HTTP/ready与受控bundle身份，以及OCR文字区域在RPC/Worker中的证据闭包。当前HTTP保持AI_MODE=mock，没有业务瓶子归属、化学实体/日期事实、Worker crop对象写入或完整事实事务；COCO仍80类，不能伪造label检测。

2026-10-07进度审阅补充：上述为剩余工作总范围；按[现行实施计划1.2](01-implementation-plan.md#12-下一批第37批独立ocr文字证据协议与worker接线)的依赖顺序，第37批先完成独立OCR文字区域协议、Worker重建上传和围栏登记，再接常驻真实HTTP/ready与bundle身份。理由是当前CropRecipe/OCRField及image_derivatives强制detection_id，真实OCR区域不能借用COCO检测填充。此补充仅确定计划，第37批未实施，不改变第36批验收成绩。

真实对象存储IAM、TLS/nginx/可达性、只读身份授权、现场图片指标、性能/内存批准、完整发布/试点仍需独立验收。SEC-03/UP-03/AI-10、整个I-ML-01/04均未关闭。数据库/权限/模型变更后续PR需两位协作者确认，本地PASS不构成人工批准。

2026-10-07后续更新：[第37批](37-i-ml01-worker-ocr-evidence.md)已本地接独立text_regions/crop协议、Worker重建/准确版本上传/围栏事务及004；本记录中的“下一批37”及未接OCR证据为第36批交付时状态。当前下一批38接常驻真实CPU HTTP与受控身份，以实施计划为准。
