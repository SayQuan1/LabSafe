# 第38批：常驻真实CPU HTTP与受控身份门禁

2026-10-07，本地完成与验证；沿用wsq/i-ml01-analysis-input，第36–38批尚未提交/Push/PR。已fetch核验origin/main=13a854f。本批对应I-ML-01/I-03A2、R-03/R-06/R-07/R-09及AI-03/AI-04/JOB-02/CAP-01的工程部分，不关闭整个I-ML-01、AI-10或生产门禁。

## 1. 交付范围

AI_MODE=cpu新增单Uvicorn supervisor＋一个常驻spawn计算子进程；保留同协议AI_MODE=mock，APP_ENV仍仅dev/test。使用用户提供的官方D-FINE COCO80与PP-OCRv6_small det/rec ONNX，CPUExecutionProvider，无CUDA/Paddle或联网下载。复用第34–37批质量、检测、OCR、固定版本analysis读取与可重建证据算法，模型启动加载一次后跨quality/runs请求复用。

development bundle逐项绑定模型/config/preprocessor/OCR模型及配置、空业务词典、runtime-lock实际SHA、pipeline/device、阈值与线程数。路径为绝对路径，resolve后须在显式artifact roots内；OCR文件必须与适配器实际读取的inference.onnx/inference.yml同目录。模型适配器继续核官方固定SHA并用核验字节建立ORT session；启动前后核所有摘要与实际运行锁。bundle文件SHA作为本地development model_checksum，不冒充经过评测和批准的完整ModelManifest，也未实现公共模型导入/activation或多版本AI_ROUTES_FILE热重载。

runtime-lock是cpu-direct-runtime-v1：准确Python/platform、11项直接依赖版本、裁剪运行指纹及AI/共享协议/像素实现代码摘要。生成工具从实际环境计算，无预填假hash；它不是生产transitive依赖/镜像锁。词典仅绑定空业务entries，OCR字符表仍在已核官方rec配置中；没有化学词典、语义或实体解析能力。修改代码、依赖、阈值或资产必须生成新bundle/lock并重启，不能沿用旧身份或现场评测报告。

## 2. 生命周期、期限与失败

- 子进程120秒内核验、加载、真实smoke并握手后才ready。health在加载时可响应；ready检查实际子进程存活，version在未加载/退出时503，不返回期待值冒充实际身份。繁忙仍ready。
- 同一HTTP闸门覆盖quality/runs；相同活动attempt返回409 RUN_IN_PROGRESS，其他请求429 AI_BUSY。quality 10秒、runs 180秒，与UTC deadline取min，包含IPC、下载、解码和计算；超时、OOM、崩溃、取消、损坏/超容量响应均kill/join并清IPC，不返回部分成功。
- 加载失败或计算故障后至少30秒重建，连续3次加载失败锁定not_ready；重载仍核原bundle字节、摘要和smoke，不接受换文件沿用旧身份。每代独立管道/队列，旧进程回收后才建新进程。
- JSON请求≤64KiB、结果≤2MiB；ASGI在JSON解析前限制流式body，读body最长10秒。receiver线程只读取IPC，计算在可kill的spawn进程内。断连探测在独立协程中，避免其取消作用域吞掉请求取消；退出同时回收进程和receiver。
- SIGTERM先not_ready，Uvicorn与supervisor各保留最多220秒退出grace；当前请求可完成，退出后kill/join。Windows测试通过专用测试host的graceful stop验证，Popen.terminate不是Windows的SIGTERM模拟，不能据此声称已做真实部署信号验收。

## 3. 正式RPC和Worker

quality/runs均输出正式InferenceResult并校验Schema、准确输入SHA、run/tenant/attempt/fence/hash和证据闭包；model/dictionary/pipeline来自实际加载身份。质量函数与固定输入相同，quality通过为facts_ready但所有下游数组为空；runs通过仍needs_review，无chemical/date/container facts。任一图质量失败为全批needs_retake，无检测/文字/裁剪内容。

Version新增必填model_checksum/dictionary_sha256；InferenceResult增加可选execution_identity引用完整Version。真实CPU必须携带，Worker核与ready/version及本地受控expected-version逐字段一致；fixture兼容旧结果但Worker补入已核模拟身份。共享闭包再次核execution_identity与结果版本摘要一致。旧内部version缺新字段拒绝，协调升级AI/Worker；公共InferenceRun字段不新增，通过已登记结果身份投影is_simulated，历史/尚无结果默认true，不改写历史JSON。

Worker endpoint来自配置，禁proxy/redirect；明文仅loopback，HTTPS使用系统信任链。CPU需要AI_EXPECTED_VERSION_FILE，仅含Version，不传权重路径给Worker或请求。未配置该pin时只接受仓库已核fixture身份，不能降级到未知真实服务。每次RPC先ready/version，再POST，端到端15/200秒与请求deadline取min；connect≤2秒，无连接池等待，有限读和整体socket watchdog处理慢头/慢body；失租关闭活动socket，超大/错误回显/错身份/坏闭包均拒绝。

第37批独立Worker证据进程与围栏事务继续复用：冻结analysis准确版本、三个摘要、D上传/复用、全量登记；租约、attempt、当前输入版本变化仍拒绝旧结果。CPU子进程移除业务DB/broker/API/可写S3凭据环境，只接显式只读AnalysisSettings。隔离环境和最小IAM部署仍须分别验证。

## 4. 本机验证

环境Python3.11.4，官方ONNX Runtime1.20.1 CPU；对象存储为loopback版本模拟器，图像为合成中英标签，没有真实实验室图或生产凭据。

| 验证 | 本批实际结果 |
|---|---|
| pytest tests/business tests/domain tests/security tests/protocol | 1641通过；含13项bundle/15项socket RPC新增用例 |
| fixture＋spawn supervisor＋CPU ASGI | 23项通过；synthetic computation，实际spawn/IPC/HTTP gate/取消回收 |
| 独立真实CPU依赖套件＋supervisor＋协议 | 72项通过，无跳过；官方权重另做下行smoke |
| 隔离MySQL8.0.33定向 | 42项通过，含21项unit；head0004、43表/115FK无差异，非全量持久化重跑 |
| 独立Worker证据环境 | 5项实际像素/SDK/子进程用例无跳过 |
| 官方权重正式HTTP→Worker | 两图、6文字/6crop；质量/重放一致、resident PID复用、未知pin无analysis GET、准确D版本复用、实际child崩溃后not_ready/重载、graceful退出清理 |

真实HTTP证据见[机器记录](38-i-ml01-cpu-http-smoke.json)。该smoke不在数据库中提交；MySQL围栏/投影用synthetic wire metadata单独测试，不能把两者说成已做真实存储＋真实模型＋数据库的完整现场端到端。

ruff/check-format、生成器--check、148项readiness、设计报告（11份JSON Schema）、三个环境pip check/隔离检查及git diff --check通过；末轮Worker/bundle/执行定向35项与supervisor/ASGI定向8项重测通过。各套件有重叠，不相加成唯一测试数；本机通过不等于远程CI或协作者批准。

## 5. 运行与回滚

配置和生成命令见[开发指南](../../DEVELOPMENT.md)3.22及[.env.example](../../.env.example)。模型/本地bundle/lock/词典/令牌/venv/测试图片不提交。生成bundle目录必须为空，已有身份不可覆盖；保留旧代码、运行环境与资产才可恢复旧pin。

本批无新迁移，head仍0004。回滚先停流量、排空/回收计算进程，恢复成套代码/契约和原fixture配置，或恢复原CPU代码/lock/bundle/Worker pin；不能仅改AI_MODE伪称旧真实任务已成功。旧CPU任务保持原绑定，无法匹配时按既有技术失败/重试或授权新提交处理；不改历史结果、对象或审计。production仍禁止。

下一批第39批先落实独立文字行到真实COCO bottle的确定性几何关联与未知保留，明确没有label/shelf/cabinet类别时的证据语义；不伪造缺失检测，不强行归属并列候选，不跨图猜同瓶。随后再接字段词法、业务词典、日期/实体与事实快照，crop下载/UI、孤儿清理、现场评测和生产批准另行交付。
