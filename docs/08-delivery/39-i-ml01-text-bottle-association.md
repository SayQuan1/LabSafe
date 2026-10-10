# 第39批：独立文字行与真实COCO bottle关联

2026-10-09，本地完成与验证；沿用wsq/i-ml01-analysis-input，第36–39批尚未提交/Push/PR。本次fetch核验origin/main=13a854f，无新main提交。对应I-ML-01、R-03/R-06及AI-10/CAP-01的文字关联部分，不关闭整个真实模型接线任务或现场/生产门禁。

## 1. 实现与语义

新增共享纯Python实现packages/inference_protocol/association.py，算法版本text-bottle-quad80-v1。官方D-FINE COCO80/PP-OCRv6_small与CPU选择保持；只将独立TextRegion/CropRecipe的nullable detection_id关联到真实同图bottle（class_id=39）。不创建label/shelf/cabinet，不把OCR line_id当作检测ID。几何关联只说明文字区域与可见瓶框重合，不能证明化学品身份、安全类别或规则事实。

联合CPU流水线在既有稳定文字/检测ID生成后运行关联，再序列化文字与crop。只更新关联值；不修改原文、置信度、quad、line/crop ID、裁剪配方、源/PNG/识别摘要或图片归属。不用OCR置信度过滤关联，低置信原文仍保留uncertain；后续字段/实体有独立阈值门禁。质量失败整批仍无模型/证据；质量成功runs仍needs_review，ocr_fields/entities/relations为空。

## 2. 确定性几何

输入是原分析图归一化坐标。quad必须四点、有限、[0,1]、按图像坐标顺时针、严格凸且面积为正；坏/自交/退化几何不能降级为未知成功。瓶框须严格x1<x2、y1<y2；所有检测框在共享闭包中再次核正面积。

文字中心为四顶点坐标的算术平均；中心在瓶框闭区间内，且**文字四边形与瓶矩形实际交面积≥0.8×文字四边形面积**才为候选。使用四边矩形裁剪与三角扇面积，禁止用文字外接矩形代替倾斜区域。原始double计算，不取整、不加epsilon扩阈值；0.8恰等接受，紧邻低于阈值拒绝。面积和中心只用于几何关联，不是模型概率。

候选只来自同图type=bottle/class_id=39；在所有候选中取框面积严格最小者。最小面积按实际double相等且多于一项即NULL，无ID/置信度/顺序破平局；重复query保持原检测，不合并。不跨overview/detail推断同瓶或从parent_image_id继承关联。无候选、并列、COCO无瓶目标均NULL，原文及裁剪完整保留。

全请求≤100检测、≤100文字/裁剪；算法不截断。关联先全量计算再修改行，坏后续quad不会留下局部关联。正式RPC与Worker共享同一实现重算全部关联，包含NULL；正确类别但错误瓶框、错误关联、应唯一却返回NULL、应并列却硬选均拒绝。

## 3. 契约、持久化与兼容

内部TextRegion/CropRecipe的字段形状保持，第39批补充detection_id语义为真实瓶子关联，文字与crop必须一对一同值。公共InferenceRun的既有text_regions只读投影保留该关联；Worker独立证据准备、事务提交和公共投影继续使用既有路径。共享Schema之后的闭包核真实类别、同图、UUIDv5当前run及几何算法，不靠Schema单独证明语义。

联合本地报告升级cpu-pipeline-local-v3/pipeline quality-coco80-ocrv6-cpu-v3，config记录text_association版本，正式normalization_ms记录本批关联耗时（字段标准化尚未实现）；非quality-only成功时bottle_association能力为true，其他语义能力仍false。独立OCR CLI仍无D-FINE关联，原本地v2不变；第35–38批历史报告不改写。正式pipeline_version仍vision-v1，development bundle/runtime-lock通过共享协议/代码实际摘要绑定新实现，旧代码/lock/bundle不可直接沿用，生成新身份并协调AI/Worker升级。

本批无迁移，head仍0004_ocr_evidence。唯一关联登记真实detection_id，未知登记NULL，derivative recipe与完整结果同事务一致。历史独立文字、JSON、对象和审计不重写；读取历史投影不做新算法回填。旧服务输出在当前Worker门禁中可能不再满足几何闭包，必须成套升级/排空，不兼容猜补关联。

## 4. 本机证据

| 验证 | 本批结果 |
|---|---|
| 几何与Worker闭包定向 | 31项通过：实际quad/80%边界/紧邻低值、倾斜/并列/跨图/坏几何/容量/伪关联、完整证据 |
| 业务/领域/安全/协议总回归 | 1672项通过；包含上行31项，套件有重叠，不相加成唯一测试数 |
| 独立CPU/像素/supervisor/协议 | 74项通过，无跳过；含实际OpenCV独立交面积对照和真实裁剪字节不变 |
| fixture/spawn/正式ASGI | 24项通过；关联正向与错误框拒绝使用显式synthetic computation、实际spawn/IPC/HTTP |
| 独立Worker证据环境 | 7项通过，无跳过；关联/并列时源/裁剪摘要、准确对象版本及取消回收；错关联不读存储 |
| 隔离MySQL8.0.33定向 | 50项通过（含21项unit），唯一/无目标/并列关联、真实/模拟标记、全量登记/recipe/公共投影及旧围栏/版本变动拒绝；非全量持久化重跑 |

真实权重正式HTTP→Worker证据见[机器记录](39-i-ml01-text-association-smoke.json)：两张合成中英标签图、6文字/6crop；实际模型没有检测到bottle，6条关联全部NULL，验证缺目标保留、重放/质量一致、准确版本复用、崩溃重载和退出清理。**没有把合成瓶框当作真实模型正向关联成绩**：正向/并列的几何、HTTP、像素和MySQL用显式合成框另测。本机对象存储是loopback版本模拟器，MySQL围栏另验；尚未覆盖真实瓶照片、现场关联准确率、实际S3/IAM/TLS或生产批准。

ruff/check-format、build_specs --check、148项readiness、validate_specs --report、三个环境pip check/隔离与git diff --check通过。独立环境仍无相互模型/ORM/broker混装，无CUDA/Paddle安装；远程CI未运行，已将独立几何像素测试接入CPU CI。

## 5. 运行、回滚与下一批

第38批CPU启动/Worker pin流程继续使用，代码/契约变化后在空目录重新生成development bundle/lock，不能手改旧lock SHA。运行指南见[DEVELOPMENT.md](../../DEVELOPMENT.md)3.23。仅dev/test，默认开关仍关闭；原文及关联需要人工复核。

回滚先停止新流量并排空/回收CPU进程，再恢复成套旧代码、契约、运行环境、bundle与Worker pin；不删数据库/对象或改写历史关联。已执行结果只读保留；无法加载旧固定身份的任务不能自动转最新模型，由既有技术恢复或授权重新提交生成新任务。无新迁移无需downgrade，保持0004历史。

下一批第40批：OCR字段词法、严格日期/CAS格式与同瓶连续行配对，并保留全部line/crop证据。先解决单/跨行字段的真实多证据契约与闭包，再接正式CPU/Worker；不拿单一crop冒充两行来源。实体解析、业务化学词典、确定DateFact/事实快照、容器关系、crop下载/UI、孤儿清理和现场/生产验收分别后续实现。
