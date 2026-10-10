# 第41批：冻结开发词典、名称候选与实际MinIO验证

2026-10-09，本地完成与验证，沿用 `wsq/i-ml01-analysis-input`。开工 fetch 核验 `origin/main=13a854f`，第36–41批仍未提交/Push/PR。对应 I-ML-01、R-03/R-06、AI-10/CAP-01 的实体候选部分，以及实际 MinIO 固定版本/作用域 IAM 的工程验证；不关闭生产部署或完整现场模型验收。

## 1. 有界开发词典

`DevelopmentDictionary` 从仅空条目升级为最多100条 `DevelopmentDictionaryEntry`：`entity_id`、`canonical_name`（≤200字符）、`aliases`（≤10条、每条≤200字符）、nullable `cas`（严格格式/校验位）、必填 `source`（≤1000字符）。空词典仍合法。拒绝重复 entity_id、空名称/来源、同条目的 NFKC/casefold/空白规范化重复名称、坏 CAS、额外属性和超容量；不同条目的同别名或同 CAS 保留为歧义，不任意选择。整个词典原始 UTF-8 快照≤64KiB。

该开发词典不包含 hazard/storage class，不从标签图片或网络推断安全类别，不替代完整词典导入/批准治理。生成器可用 `--dictionary <冻结JSON>`，默认继续生成空词典；本批真实素材 smoke 使用空词典。合成候选数据只在测试/明确标记的正向 probe 中使用。

## 2. 候选与所有名称字段参与的聚合

新增共享纯 Python `chemical-candidates-v1`。名称 NFKC+casefold+空白压缩，合法 CAS token 只作 CAS 精确检索，未命中为 unknown，不用相似 CAS 猜实体。其他名称优先 canonical/alias 精确匹配，否则按 `1-Levenshtein/maxlen` 生成正分候选。同条目的各名称取最高分，排序按 score 降序/entity_id 升序；一批最多2,000,000个距离矩阵单元，超限整批 MODEL_ERROR，不截断词典/字段。无前缀 OCR 只接受冻结 canonical/alias 整行精确匹配，不对任意背景文字模糊猜名称。

每个 `(image_id,bottle_id)` 的所有 name 字段都参与聚合。每个字段唯一最佳、OCR confidence≥ocr_min、候选score≥entity_min且全部最佳ID相同才 resolved；低置信、并列、缺值或名称冲突保持 candidate/unknown。候选合并按 entity_id 去重，每个字段都参与 min 分数，缺少该候选计0；完整冲突/唯一最佳判断先于 top5，不能把第6个冲突实体删去后宣布 resolved。最终 match_method 记录该实体所有实际命中中的最弱方法（CAS→alias→fuzzy）；不把匹配分数当安全概率。

仅有实际 name 字段才生成 EntityCandidates 分组；没有字段不制造实体。已解析候选也仍 `needs_review`，本批不生成 DateFact、FactRevision 或规则输入的确定 storage_class。

## 3. 正式身份、重算与完整保存

开发 CPU profile 升级为 `dfine-cpu-fp32-ocrv6smallcpu-chemical-v2`，bundle 新增 `entity_min=0.9`；联合本地报告为 `cpu-pipeline-local-v5`/`quality-coco80-ocrv6-cpu-v5`。官方模型、COCO80、PP-OCRv6_small、默认 detection_min=0.4、CPU-only 选择保持。

内部结果新增 `extraction_context`：实际已加载 bundle/dictionary 的原始 JSON 字节文本。新 profile 必带；分别重新计算 SHA 与 Worker 冻结的 model_checksum/dictionary_sha256 比较，再核 bundle ID/词典ID/pipeline/device/profile及词典引用 SHA。这让 Worker 用被固定 bundle SHA 覆盖的阈值和被固定词典 SHA 覆盖的条目重新计算全部 OCRField/EntityCandidates，拒绝改词典、换阈值、伪候选、遗漏或冒充 facts_ready。内部快照只是有界验证数据，不打开其中的模型路径或请求路径。

AI常驻进程、supervisor、HTTP客户端、独立证据进程和事务提交均沿用共享闭包。新增快照/候选完整保存到已有 inference_runs.result JSON，与全部 derivatives 同事务；不扩公共投影、不新增迁移，仍 `0004_ocr_evidence`。旧 profile 的无候选 fixture 可继续校验；非空 entities 无受控快照拒绝。AI/Worker/契约/profile/bundle/lock必须协调升级，历史结果不回填，不改写旧对象。

## 4. 本机测试

| 验证 | 结果 |
|---|---|
| 新候选/快照黄金用例 | 39项通过：CAS/别名/模糊、冲突/并列/缺值、top5前冲突、低阈值/非有限数、距离/字节容量、坏词典、伪候选/快照 |
| 业务/领域/安全/协议 | 最终1795项通过，包含上行；套件重叠不相加为唯一数 |
| CPU/像素/supervisor/协议 | 75项通过，无跳过；非空词典正向接入、仍needs_review和预算错误验证 |
| fixture/spawn/正式ASGI | 26项通过；新profile实际IPC/HTTP及伪候选整批拒绝使用明确synthetic computation |
| 独立Worker真实像素 | 9项通过；候选快照核验、实际两crop重建，伪候选在读存储前拒绝 |
| 隔离MySQL8.0.33 | 50项通过，含21项unit；候选/快照/全部来源围栏保存，旧租约/版本变动拒绝；非全量持久化重跑 |

MySQL结构核对为0004/43表115FK，无错误；业务/MySQL有既有 Starlette/anyio 弃用警告，无失败。文档同步后ruff check/format（245文件）、build_specs --check、validate_specs --report（含148 readiness）、git diff --check均通过；三环境pip check通过，importlib检查禁止依赖均未出现，未安装CUDA/Paddle/Torch。最新设计机器报告只代表设计检查，不代替上述应用/存储测试。远程CI未执行。实际回归命令沿用[第40批](40-i-ml01-ocr-fields.md)和[开发指南](../../DEVELOPMENT.md)，新增候选定向入口为 `pytest tests/business/test_chemical_candidates.py tests/business/test_cpu_bundle.py -q`。

最终收尾检查于2026-10-10（北京时间）完成，CPU pipeline/supervisor定向16项、独立Worker像素9项再次通过，无跳过；与上述套件重叠，不累加为唯一测试数。

## 5. 新素材和真实MinIO

用户已安装 `D:/Tool/minio/bin/minio.exe` 和 `mc.exe`，版本 `RELEASE.2025-07-23T15-54-02Z`。本批使用新建独立临时数据目录、随机 loopback API/console端口及随机测试凭据启动真实 MinIO，未修改 `D:/Tool/minio/data` 或安装配置。仅临时 bucket `labsafe-private` 开启 versioning；测试结束回收 CPU/MinIO进程并清理自己创建的目录，Windows 长路径清理使用经核验的完整路径。

为AI建立当前 tenant/lab analysis 的 GetObject/GetObjectVersion 权限；Worker只允许同范围 analysis/derivatives读取和 derivatives写入。Worker对derivatives的限定ListBucket权限仅用于可靠区分HEAD缺失，不允许全bucket或父目录列举。共11项实际AccessDenied：跨租户读取、父目录列表、analysis写入、删除、AI写/读derivatives及Worker删除已存在的准确版本均被拒绝。没有给AI写/删除权限或Worker全bucket权限。

真实签名GET中先写分析图V1，再向同key写坏的latest V2；官方模型及独立Worker仍从准确V1读取，核所有裁剪版本/SHA并复用。机器记录见[四图/空词典smoke](41-i-ml01-minio-smoke.json)。四图逐图运行，没有把无关素材冒充同一次巡检的overview/detail。

| 用户素材顺序 | 默认质量 | 检测/瓶子 | OCR文字/crop | 唯一关联文字 | 字段/候选 |
|---|---|---|---|---|---|
| 1：试剂瓶货架 | pass | 3/3 | 6/6 | 1 | 0/0 |
| 2：分类柜 | pass | 1/1 | 8/8 | 0 | 0/0 |
| 3：单瓶近照 | pass | 1/0（COCO cup） | 19/19 | 0 | 0/0 |
| 4：实验台 | pass | 0/0 | 1/1 | 0 | 0/0 |

共34条真实文字/crop，均完整上传/准确版本复用。第三图没有把cup改名bottle，分类柜文字不作为容器或化学安全事实。质量通过只说明既定质量算法通过，不能证明标签足够清晰；四图无完整字段，全部保持needs_review。

另有真实OCR合成标签和明确synthetic瓶框的独立正向probe：空词典产生2个字段/1个unknown分组；[测试专用非空词典smoke](41-i-ml01-candidates-smoke.json)用真实冻结SHA/官方OCR/实际MinIO核验2个名称字段共识，结果resolved但仍needs_review。synthetic名称/CAS映射不属于真实化学数据，瓶框标记is_simulated=true，不计现场模型检测或实体准确率。无真实图片/原文/凭据/临时制品提交Git。

## 6. 回滚与下一批

只允许dev/test，production开关仍禁止。升级先排空旧CPU/固定身份任务，在新空目录重新生成bundle/lock/pin；回滚恢复成套旧代码、契约、环境和身份，不重写历史结果或删除对象，无迁移downgrade。新词典未获数据/安全审核不得成为规则事实来源。

下一批第42批：同瓶日期字段聚合、冲突/低置信/未知保留及全部行/crop证据，先冻结DateFact契约与Worker重算；仍needs_review，完整事实快照/规则接线、化学安全数据、现场标注/校准、公共模型activation和部署审批分别后续。实际MinIO loopback固定版本/IAM已验，不等于nginx、公开下载签名、TLS、持久部署/备份与生产权限验收。

## 7. 2026-10-10 Git交付与CI收尾

第36–41批实现汇总为提交726786c，已Push到wsq/i-ml01-analysis-input并按.github/PULL_REQUEST_TEMPLATE.md创建[PR #13](https://github.com/SayQuan1/LabSafe/pull/13)，基线main=13a854f，尚未合并。前文未提交/未运行远程CI为实施时的历史状态。

远程Push检查暴露请求取消恰逢IPC发送完成的竞态：Python3.11 asyncio.wait_for可能消费外层取消，导致调用等待到推理超时。发送期限改用asyncio.timeout以保留外层CancelledError和既有进程回收；新增确定性用例调度发送完成与取消的同一时序，原实现稳定出现AI_TIMEOUT，新实现正确取消并回收，无放宽超时预算。第41批两份真实MinIO/官方CPU smoke按修复后的代码锁重新运行，结果及现场局限保持。

提交前主回归1795、CPU/协议75、fixture/spawn/HTTP27、独立Worker9通过，静态/契约/三环境pip与隔离检查通过。取消修复后CPU/协议76、fixture/spawn/HTTP27再次通过，两份真实MinIO smoke通过，确定性新增用例已验证旧实现失败/新实现通过；套件重叠不累加。本次完整隔离MySQL8.0.33持久化套件458项全部通过、无跳过，0004/43表115FK无差异，测试实例已回收；1条既有Starlette/anyio弃用警告。实现与修复提交为726786c/ae53d0a，远程CI最终结果以PR描述和检查页为准，独立协作者批准另行确认，不自动合并。
