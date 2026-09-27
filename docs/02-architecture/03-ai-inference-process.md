# 02.3 独立 AI 推理进程

状态：设计候选 1.1.0。字段见 [AI OpenAPI](../../contracts/inference-v1.yaml)。

## 1. 边界与进程

公共提交永远异步，API 原子创建 InferenceRun、TaskRun 后 202。Worker 调用有界同步 AI RPC；AI 不持久化任务，不提供 GET run 状态；公共查询读取 MySQL。

AI 是单 Uvicorn worker supervisor + 一个 multiprocessing spawn 计算子进程。supervisor 管鉴权、Schema、并发闸门、deadline、IPC、健康；子进程管下载、解码、质量、检测、内存裁剪、OCR、词典、关系。禁止用多 Web worker 复制显存。禁止使用“超时线程仍在后台计算”的伪取消。

AI 只能读授权 analysis/ 对象和只读模型目录，无 MySQL/Redis/业务 ORM/规则依赖。Worker 根据裁剪配方写证据对象。AI 不返回可写 URL、不持有写权限。

模型和数据路线以[固定模型方案](../04-ai-rules/02-model-data-plan.md)为准：AI运行时Python3.11、D-FINE-N四类ONNX，业务仍Python3.12；RGB直接resize640并除255，qmax/无NMS，不套用其他模型的letterbox。建议生产检测CUDA FP32；OCR保持PP-OCRv4 CPU。APP_ENV/AI_MODE在启动校验；production禁止fixture和未批准bundle，dev/test可用同协议mock进程。/version除purpose/is_simulated外回显adapter_id、runtime_profile、runtime_lock_sha256、detector_device和ocr_device；这些值取实际通过启动校验的加载状态，Worker与受控manifest逐项核对。

## 2. 生命周期

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
- key 固定 tenant/{tenant}/lab/{lab}/analysis/{image}/{sha}.png，由 Worker 从 DB 构造；禁止 URL、..、反斜线或越前缀。下载校验 hash；失败不能转为空事实成功。

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
| OCR | label quad 顺时针 TL,TR,BR,BL；内存透视变换 | OCR 原文/归一化值、crop recipe |
| 标准化 | 冻结词典，输出 top5 CAS/别名/模糊匹配候选 | resolved/candidate/unknown |
| 关系 | 只计算同 overview 内可见容器关系 | 禁止跨图推断相邻 |
| 返回 | 回显 hash/版本，所有证据关联 image/detection/crop | facts_ready 或 needs_review，均需人工完成 |

/quality 复用 InferenceResult；detections/crops/ocr_fields/entities/relations 为空。通过 outcome=facts_ready，失败 needs_retake；不建立事实修订。/runs needs_retake 也无下游事实；无目标、文字不可解析、实体/日期歧义均 needs_review。

## 6. 多图与裁剪

- 恰一张 overview 且 ordinal=0；其余 detail 的 parent_image_id 指向它；图片都属于同 item/location。不自动合并跨图目标；parent 只表示上下文，不证明同瓶或邻接。
- bottle.parent_detection_id指向同图包含瓶中心的最小面积shelf/cabinet；同最小面积并列为null。label关联按80%包含面积及最小瓶规则，歧义null。Worker生成same_location：同一已知parent为true、不同已知parent为false、缺失/歧义为null；共享item/location不够。实体候选关联bottle，证据仍指到label/crop；无bottle的标签留作人工参考。overview全无序瓶对在OCR前检查≤200，再依确定性事实规范产生关系；不静默删减。
- detection_id/crop_id：UUIDv5(run_id, image_id+类型+稳定 ordinal)，检测排序 (y1,x1,y2,x2,class,confidence)。不合并两个 attempt 的结果。
- perspective-rgb-v1：点乘 (width-1,height-1)，OpenCV float32 getPerspectiveTransform → warpPerspective INTER_LINEAR + BORDER_CONSTANT 黑色；RGB8 PNG 无元数据；输出边 1–2048。自交/奇异/面积<1像素即错误。
- AI/Worker 必须锁定同一图像变换实现及依赖版本；Worker 重建最多 100 个 crops，key 含 run/crop/sha。唯一键 (tenant,run,crop)，同图允许多个 crop。
- 裁剪写入失败整个 attempt 失败。未提交对象保留 24h 后作为孤儿清理；只有已提交 derivative 才允许签名访问。

## 7. 结果落库与恢复

先校验 Schema，再核 tenant/run/attempt/token、输入 hash、model/dictionary/pipeline；所有证据 ID 必须在本结果闭包内；父子图、坐标和 relation 归属再做语义检查。不合法为 SCHEMA_MISMATCH，禁止自动补零或改字段。

锁 item/task_run；lease_owner、token、未过期 lease、当前 run/submission_revision 全部相等才接纳。旧结果 LEASE_LOST，不更新任何当前指针。规则评估后 item=needs_review；零候选也不自动完成。见 [持久任务](05-durable-jobs.md)。

错误可重试性见 [目录](../../contracts/error-codes.json)：MODEL_NOT_READY、AI_BUSY、OOM、超时、依赖故障可重试；版本/hash/schema错误不可自动重试。设备切换只通过新 activation 影响新 run；旧 run 保持原配置。
