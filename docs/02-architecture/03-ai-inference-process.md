# 02.3 独立 AI 推理进程

状态：设计候选 1.1.0。字段见 [AI OpenAPI](../../contracts/inference-v1.yaml)。

## 1. 边界与进程

公共提交永远异步，API 原子创建 InferenceRun、TaskRun 后 202。Worker 调用有界同步 AI RPC；AI 不持久化任务，不提供 GET run 状态；公共查询读取 MySQL。

AI 是单 Uvicorn worker supervisor + 一个 multiprocessing spawn 计算子进程。supervisor 管鉴权、Schema、并发闸门、deadline、IPC、健康；子进程管下载、解码、质量、检测、内存裁剪、OCR、词典、关系。禁止用多 Web worker 复制显存。禁止使用“超时线程仍在后台计算”的伪取消。

AI 只能读授权 analysis/ 对象和只读模型目录，无 MySQL/Redis/业务 ORM/规则依赖。Worker 根据裁剪配方写证据对象。AI 不返回可写 URL、不持有写权限。

模型和数据路线以[固定模型方案](../04-ai-rules/02-model-data-plan.md)及本批验收为准：AI运行时 Python3.11、官方 D-FINE COCO 80 类 ONNX，业务同为 Python3.11 但保留独立依赖环境；RGB直接resize640并除255，官方输出为 300 queries、qmax/无NMS，协议保留 0–79 `class_id` 和对应 `type`。建议生产检测 CUDA FP32；OCR保持 PP-OCRv6_small ONNX CPU。APP_ENV/AI_MODE 在启动校验；production 禁止 fixture 和未批准 bundle，dev/test 可用同协议 mock 进程。/version 除 purpose/is_simulated 外回显 adapter_id、runtime_profile、runtime_lock_sha256、detector_device 和 ocr_device；这些值取实际通过启动校验的加载状态，Worker 与受控 manifest 逐项核对。

I-01A交付同协议开发fixture和HTTP进程入口；I-ML-01已接真实CPU检测、PP-OCRv6_small、全批质量门禁与可重建文字证据。[第36批受控analysis输入](../08-delivery/36-i-ml01-controlled-analysis-input.md)把准确对象版本读取接入同一有界CPU流水线。当前仅CPU，不安装CUDA；第38批AI_MODE=cpu已接常驻supervisor、正式HTTP/Worker pin门禁和实际ready/version，保留mock。业务事实、完整公共模型导入/activation、多版本路由与现场批准仍待接通。运行和验收入口见[开发指南](../../DEVELOPMENT.md)。

## 2. 生命周期

[联合CPU续批](../08-delivery/34-i-ml01-quality-cpu-pipeline.md)已实现真实质量门禁和同一子进程的检测/OCR顺序执行。全部输入先检查质量，任一失败返回整批needs_retake、跳过模型；quality-only与联合入口使用同一函数。第36批受控analysis入口内部预算取请求deadline、配置预算及quality 10秒/runs 180秒的最小值，含spawn、下载、解码、模型加载和推理；超时kill/join，无部分结果。第38批常驻HTTP/ready已本地实现，模型加载移到120秒启动阶段；请求10/180秒包含IPC/固定版本下载/解码/计算，不重复加载模型。

2026-10-07 [PP-OCRv6_small CPU续批](../08-delivery/33-i-ml01-ocrv6-cpu.md)交付独立本地文字检测/识别，后续第35–38批已补独立裁剪证据与正式HTTP。当前独立OCR CLI为ocrv6-local-v2，无D-FINE输入或瓶子归属；联合CPU第39批为v3，以真实bottle关联独立文字，不借用其他COCO类别填充label。业务字段/语义事实仍待后续，原文不等于安全事实。

### 静态路由与多版本

Worker加载受控AI_ROUTES_FILE，其结构见[路由Schema](../../contracts/inference-routes-v1.json)。键(tenant_id,model_bundle_id,dictionary_version_id,device_profile)必须唯一，对应一个管理员预先部署的AI endpoint及token_file。endpoint只能来自配置中的内网allowlist，不从业务请求接受URL。每实例只加载一个固定bundle；不做请求级切换或跨主机调度。初始部署可仅一条route；不同租户/版本需要独立实例和经过容量审核的资源，不能在同一GPU上盲目复制模型。

activation命令通过该路由访问/ready和/version，实际tuple一致才允许激活；无route为MODEL_VERSION_UNAVAILABLE，已配置但未ready为MODEL_NOT_READY。新activation不影响旧run：旧route保留到所有固定版本任务结束；恢复历史run前先恢复其route和制品。单实例升级必须先排空旧版本任务，不能让旧任务流向新模型。route文件原子更换并重载，非法配置拒绝且保留旧配置，记录审计。

### 进程步骤

1. 校验配置、服务 token、租户白名单、只读对象凭据、本地 manifest。
2. 核对所有制品/词典SHA、pipeline、设备、适配器、锁文件实际hash及安装版本；缺失模型不得联网下载。CUDA profile不能与CPU runtime对象混配。
3. spawn子进程、加载模型，跑随bundle提供的不含敏感信息smoke；120秒内完成才ready。supervisor不初始化CUDA；CUDA子进程确认CUDA EP、执行真实smoke并核对节点provider记录，不以GPU枚举成功代替实际推理。禁用整会话错误fallback；经审核的CPU辅助节点列在runtime lock，未知回落拒绝ready。
4. 加载失败每 30 秒至多重建一次，连续 3 次失败锁定 not_ready，由运维更换 bundle/restart。
5. /health 表示 supervisor 存活；/ready 200 才可接流量，否则 503；繁忙不等于未就绪。两者都需服务 token。/version 回显实际加载版本。
6. SIGTERM 先 not_ready、拒绝新请求；等当前请求在 grace 内完成，否则 kill+join 子进程、关闭 IPC 后退出。Worker 靠持久租约恢复。

计算 deadline/OOM 必须终止并回收子进程，重新加载与 smoke 后才恢复 ready。不能继续复用错误 CUDA 上下文。

## 3. 协议与幂等

- /quality、/runs 均接收 InferenceRequest：run/attempt/fencing、tenant/lab/item、submission_revision、model/dictionary 版本和 hash、pipeline、device、deadline、request_hash、image_refs。
- 没有 rule_version；AI 不做规则评估。请求版本必须等于实际加载版本，禁止自动最新、请求级下载、静默 CPU fallback。
- request_hash 对 tenant/lab/item/run/submission_revision、版本/device 和有序输入清单计算，不含 attempt/token/deadline。自动重试保持业务内容不变。
- MAX_INFLIGHT=1（含质量请求）。其他请求 429 AI_BUSY；相同活动 attempt 409 RUN_IN_PROGRESS。重启后可重复计算；不承诺 exactly-once 推理。
- key 固定 tenant/{tenant}/lab/{lab}/analysis/{image}/{sha}.png，由 Worker 从 DB 构造；禁止 URL、..、反斜线或越前缀。ImageRef必填object_version（1–200可见ASCII字符，拒绝null版本），来自run_images提交时冻结版本；版本进入request_hash。GET固定VersionId，回包版本、Content-Length、PNG MIME、实算SHA全部核验，失败不能转为空事实成功或读最新版本。
- 受控analysis读取只接受显式AI_S3_ENDPOINT/AI_S3_ALLOWED_ENDPOINTS和租户allowlist；固定labsafe-private/us-east-1，独立只读secret文件，不读取AWS provider chain、代理环境或请求URL，不跟重定向。HTTPS使用系统信任链；HTTP仅loopback开发测试。仅读analysis准确版本，无PUT/List/Delete。
- analysis允许≤128MiB、≤40M像素、单边≤10000；必须静态RGB PNG且无元数据（加载前后均核验）。Worker已做EXIF、RGB和去元数据，AI不再旋转、转换或resize。原图CLI的12MiB上限独立保留。请求图片ID/overview-detail父子关系/位置/顺序在下载前校验，报告保留Worker图片ID。

## 4. 预算

工程保护预算并非模型成绩。每阶段总时间含下载/IPC，和请求绝对 deadline 取较小值。

| 项目 | CUDA | CPU |
|---|---:|---:|
| quality 内部 / HTTP 总超时 | 5 / 8 秒 | 10 / 15 秒 |
| runs 内部 / HTTP 总超时 | 45 / 55 秒 | 180 / 200 秒 |
| Worker 其他工作预算 | 20 秒 | 20 秒 |
| Celery soft / hard | 90 / 110 秒 | 240 / 260 秒 |
| AI stop_grace_period | 70 秒 | 220 秒 |

HTTP connect=2秒、池等待=1秒、读写取剩余总预算；不能仅靠 socket read timeout。两次 RPC 分别生成 deadline。soft limit 尝试记错并清理；hard kill 由 sweeper 回收。GPU P95 30秒是目标，CPU 只承诺有界执行。

## 5. 流水线

| 阶段 | 行为 | 输出 |
|---|---|---|
| 解码 | Worker 已应用 EXIF 方向、RGB、剥离元数据、PNG 保存；AI 再核验 hash/尺寸 | 不信任对象元数据 |
| quality | 每图计算质量；任一图不通过即停止 | needs_retake，整个图片清单重提 |
| runs 质量复验 | 同不可变输入重验，应和 quality 相同 | 差异 SCHEMA_MISMATCH |
| 检测 | RGB直接resize640/除255；D-FINE qmax输出原图像素框，裁边后按W/H归一化；全请求最多100结果 | 0≤x1<x2≤1、0≤y1<y2≤1；不截断候选冒充完整事实 |
| OCR/文字关联/字段 | 独立文字quad内存透视变换；text-bottle-quad80-v1真实bottle或NULL；ocr-fields-v1词法/连续配对 | 原文、全部source_lines及crop recipe；NULL不提字段，仍待复核 |
| 开发标准化 | 冻结开发词典；CAS精确、名称/别名精确及有界模糊候选；全部名称先共识再top5 | resolved/candidate/unknown，仍needs_review，无安全分类 |
| 关系（待接） | 只计算同 overview 内可信容器关系；COCO缺失容器时保持未知 | 禁止跨图推断相邻 |
| 返回 | 回显 hash/版本，所有证据关联 image/detection/crop | facts_ready 或 needs_review，均需人工完成 |

/quality 复用 InferenceResult；detections/crops/ocr_fields/entities/relations 为空。通过 outcome=facts_ready，失败 needs_retake；不建立事实修订。/runs needs_retake 也无下游事实；无目标、文字不可解析、实体/日期歧义均 needs_review。

## 6. 多图与裁剪

- 恰一张 overview 且 ordinal=0；其余 detail 的 parent_image_id 指向它；图片都属于同 item/location。不自动合并跨图目标；parent 只表示上下文，不证明同瓶或邻接。
- 当前COCO80没有label/shelf/cabinet，bottle.parent_detection_id=NULL，不拿其他家具类冒充容器。独立文字按text-bottle-quad80-v1的80%实际quad交面积/中心/唯一最小瓶框规则关联，歧义NULL；证据指独立line/crop，不能伪造label。后续具备可信容器时才计算same_location（同一已知parent=true、已知不同=false、缺失/歧义=null）、overview无序瓶对≤200及关系；共享item/location不够，当前不生成关系事实。
- detection_id/crop_id：UUIDv5(run_id, image_id+类型+稳定 ordinal)，检测排序 (y1,x1,y2,x2,class,confidence)。不合并两个 attempt 的结果。
- perspective-rgb-v1：点乘 (width-1,height-1)，OpenCV float32 getPerspectiveTransform → warpPerspective INTER_LINEAR + BORDER_CONSTANT 黑色；RGB8 PNG 无元数据；输出边 1–2048。自交/奇异/面积<1像素即错误。
- AI/Worker 必须锁定同一图像变换实现及依赖版本；Worker 重建最多 100 个 crops，key 含 run/crop/sha。唯一键 (tenant,run,crop)，同图允许多个 crop。
- 裁剪写入失败整个 attempt 失败。未提交对象保留 24h 后作为孤儿清理；只有已提交 derivative 才允许签名访问。

## 7. 结果落库与恢复

[第35批可重建OCR证据](../08-delivery/35-i-ml01-rebuildable-ocr-evidence.md)已实现packages/image_evidence/perspective.py并接真实OCR本地入口。AI/Worker共用配方重建函数，PNG固定RGB/Pillow compress9、无元数据；记录源RGB/PNG/识别像素摘要。识别的90°旋转单独记录，证据PNG保留原图方向。运行记录含图像依赖版本、OpenCV build摘要、zlib版本，不把同版本号当跨平台字节一致性保证，重建时实际核SHA。独立/联合本地报告升级v2。第37批已将独立文字区域纳入正式text_regions与nullable detection_id的CropRecipe，并接Worker重建/上传/围栏登记，第38批已本地接常驻真实HTTP及实际development bundle身份；语义事实和现场批准仍待后续。

先校验 Schema，再核 tenant/run/attempt/token、输入 hash、model/dictionary/pipeline；所有证据 ID 必须在本结果闭包内；父子图、坐标和 relation 归属再做语义检查。不合法为 SCHEMA_MISMATCH，禁止自动补零或改字段。

锁 item/task_run；lease_owner、token、未过期 lease、当前 run/submission_revision 全部相等才接纳。旧结果 LEASE_LOST，不更新任何当前指针。规则评估后 item=needs_review；零候选也不自动完成。见 [持久任务](05-durable-jobs.md)。

错误可重试性见 [目录](../../contracts/error-codes.json)：MODEL_NOT_READY、AI_BUSY、OOM、超时、依赖故障可重试；版本/hash/schema错误不可自动重试。设备切换只通过新 activation 影响新 run；旧 run 保持原配置。

## 8. 第38批development CPU实施边界

[第38批交付](../08-delivery/38-i-ml01-resident-cpu-http.md)新增受控本地development bundle：固定模型/config/preprocessor/OCR/空业务词典/runtime-lock路径与实际SHA、阈值/线程及pipeline/device。解析前限制64KiB，resolve后须在显式artifact roots内；启动核字节摘要、实际依赖/代码/裁剪指纹，官方适配器继续核固定模型SHA和CPU provider。runtime-lock明确只锁11项直接依赖及运行指纹，非生产transitive锁；不以该小型bundle冒充完整ModelManifest/评测审批。

每实例120秒加载/smoke后握手ready；health加载时可响应，实际/version未ready为503。串行CPU阶段预算10/180秒；JSON IPC请求64KiB/结果2MiB，ASGI body在解析前限容量/10秒，receiver线程只读IPC。崩溃/超时/OOM/断连/取消清理子进程及管道，至少30秒后重载，连续3次加载失败锁定not_ready。SIGTERM先停止接入，再220秒grace/kill/join；Windows本机通过测试host的graceful stop验证，真实部署信号/离线恢复另验。

Worker本批用一个受控AI_INFERENCE_URL及AI_EXPECTED_VERSION_FILE pin；每次RPC先核ready/version，CPU无pin拒绝。Version必填模型/词典SHA；真实结果携带完整execution_identity并再次核闭包/回显，历史/无结果公共模拟标记默认true。禁代理/重定向，HTTPS系统TLS、HTTP仅loopback；connect2秒、15/200秒整体watchdog及响应2MiB上限，失租关闭socket。多版本AI_ROUTES_FILE、公共activation探针/热重载尚未实现，不将现有单实例pin当作整套治理交付。

质量通过后runs仍needs_review，原文不直接构成化学/日期事实；第39批已接文字到真实bottle关联和未知保留。COCO缺少label/shelf/cabinet，不伪造这些类别或容器关系。

## 9. 第39批文字到bottle实施边界

独立TextRegion/CropRecipe的detection_id为真实同图COCO bottle（class 39）或NULL，替代原label目标依赖；ID/原文/置信度/quad/三个像素字节摘要不变。text-bottle-quad80-v1使用四顶点平均中心、实际quad交面积≥80%及唯一最小瓶框，并列/无候选返回NULL，坏几何错误不部分成功；AI/Worker共享重算，包括未知值。低置信文字仍保留uncertain，几何关联不等于化学/日期事实。能力/配置版本与运行代码锁更新，旧bundle需重新生成身份；历史结果只读不回填，无迁移。容器/字段/词典/日期与事实聚合仍待后续，见[第39批](../08-delivery/39-i-ml01-text-bottle-association.md)。

## 10. 第40批OCR字段实施边界

第40批在文字/bottle关联后加入ocr-fields-v1：NFKC、最长前缀、严格日期/CAS、同图同瓶全局紧邻行配对，保留低置信/date_unknown及全部冲突。OCRField.source_lines记录实际1–2行的line/crop/原文/置信度；Worker在读存储前从text_regions重算完整字段，之后对所有源crop独立重建/准确版本上传/围栏登记。联合报告v4，正式normalization_ms累计关联与字段耗时；仍needs_review，无实体/日期事实。新增内部必填字段要求协调升级和新bundle/lock，不回填历史，迁移仍0004。见[第40批](../08-delivery/40-i-ml01-ocr-fields.md)。

## 11. 第41批冻结开发词典与候选实施边界

chemical-candidates-v1在全部同瓶name字段上重算候选：合法CAS只精确检索，其他名称/别名精确优先，否则有界Levenshtein；全批距离矩阵预算2,000,000单元。各字段唯一最佳、OCR/实体阈值通过且最佳ID全部一致才resolved。冲突/并列/低置信/缺值保留candidate或unknown，聚合min、缺候选计0，全部字段判定先于top5。无前缀只提取冻结名称/别名整行精确匹配，不猜背景文字。

DevelopmentDictionary≤100条/64KiB，具有来源、无hazard/storage_class；默认空词典。新development CPU profile dfine-cpu-fp32-ocrv6smallcpu-chemical-v2/报告v5冻结entity_min及词典原始摘要，结果必带extraction_context。Worker核原始bundle/dictionary SHA与已固定身份，并使用其阈值/条目完整重算字段和候选；只验证快照，不读取其中资产路径。非空entities缺快照拒绝；新profile不能冒充facts_ready。全部候选/context与已有result JSON及derivatives围栏保存，无迁移/历史回填，成套代码/契约/bundle/lock/pin协调升级。

真实MinIO loopback已验证固定分析V1读取、准确D版本/SHA/复用和11项IAM拒绝；用户四图结果仍needs_review。测试词典/合成瓶框正向probe单独标识，不充当真实化学或现场模型评测。公共activation、多版本路由、日期/事实快照、nginx/公开签名/TLS及生产批准仍待后续。见[第41批](../08-delivery/41-i-ml01-chemical-candidates.md)，下一批第42批日期聚合。
