# 00 文档控制

状态：设计版本1.1.0；2026-09-28进入I-01A实施。文档提交、实现验证和独立人工批准分别记录，不将代码自审等同于批准。

## 1. 规范边界

- 本目录除 90-archive 外为现行设计；归档只记录历史，不提供默认值或补充实现规则。
- [编码入口](02-architecture/04-coding-baseline.md)规定模块和阅读顺序；主题文档分别维护各自定义，不重复维护同一状态表。
- contracts 定义字段结构；主题文档定义前置条件、事务、权限和失败语义。两者必须同时满足，冲突是缺陷，不能用“优先级”掩盖。
- 契约由 tools/design/build_specs.py 及其模块生成；修改源后重新生成并验证，禁止只改生成物。
- 1.1.0 是未部署设计的破坏性清理，消息 schema_version=1.1。若其他环境已有消费者，先盘点和制定迁移方案，不可直接覆盖生产。
- ML-BASE-02将检测器固定为D-FINE-N；更新ModelManifest来源/运行配置和16字段content hash，内部/version增加适配器与实际运行信息；vision-v1事实语义和消息schema_version保持不变。旧未部署模型清单不兼容本次门禁，禁止改名复用；外部既有消费者另行迁移。
- IRR-01至06补齐跨模块实现边界：RelationFact.same_location改为必填boolean|null；重试、容量、对象身份/入口及确定性事实算法同步。仍属未部署1.1.0候选，消息schema_version=1.1不变；客户端重新生成nullable类型，禁止旧客户端把null当false。没有已部署消费者兼容性承诺，若外部已使用须先另行迁移。关闭证据见实现就绪清单，不代表人工批准或实际服务测试已完成。

- ADR-PY-01统一业务/AI/训练的Python3.11基线；独立进程、依赖环境、GPU和协议边界不变。I-01A仅为开发骨架，真实模型与生产能力尚未验收。

## 2. 状态与审批

2026-10-07 用户选择PP-OCRv6_small ONNX并提供det/rec资产，替代原PP-OCRv4/Paddle CPU选型；当前CPU、不安装CUDA。ocr_backend及runtime_profile生成契约同步变更，旧v4/paddleocr组合不兼容；不能给历史制品或报告改名复用。文字本地实现与完整业务接线分别验收，见[OCR续批](08-delivery/33-i-ml01-ocrv6-cpu.md)。

“结构校验通过”“设计可编码”“协作者批准”“系统可上线”分别判断。无签署记录不得称已批准。实际模型、依赖兼容性、数据许可、专家规则和性能成绩不可虚构。

2026-10-07 [可重建OCR证据续批](08-delivery/35-i-ml01-rebuildable-ocr-evidence.md)统一真实OCR裁剪到perspective-rgb-v1黑色边界，替代本地旧复制边缘算法；适配器和本地报告升级v2，历史33/34证据保留。RPC/业务引用闭包未改变，不把局部证据基础记作已落库或已批准。

## 3. 排版和维护

2026-10-09 [第41批名称候选](08-delivery/41-i-ml01-chemical-candidates.md)扩展有界DevelopmentDictionary，新增实体ID/名称/别名/CAS/来源与语义拒绝；新增extraction_context原始bundle/dictionary快照，由既定Worker固定SHA核身份及阈值后重算。development CPU profile升级chemical-v2、报告v5，不扩完整ModelManifest或公共activation的批准范围。内部消费者/代码锁协调升级，无迁移/历史回填。实际MinIO版本/IAM和新素材工程结果另记，不代表生产部署或现场准确率。

2026-10-07 [受控analysis输入续批](08-delivery/36-i-ml01-controlled-analysis-input.md)新增ImageRef.object_version必填及run_images冻结版本迁移0003。内部契约仍属未部署1.1.0候选，缺版本旧请求不兼容；Worker/AI同步升级，禁止旧run补猜当前版本。公共提交DTO、事实语义、消息schema_version不变，真实HTTP和生产批准仍未完成。

主题目录两位数字排序；README 只做导航；正文一级标题为文档、二级为职责、三级为规则。业务变更同步更新主题文档、契约、测试用例、追踪矩阵与审计。所有现行链接必须指向存在的文件。

2026-10-07 [第37批OCR文字证据](08-delivery/37-i-ml01-worker-ocr-evidence.md)新增正式text_regions及crop的line_id/重建摘要/nullable detection_id，公共InferenceRun新增只读text_regions，数据库新增0004_ocr_evidence。旧无新字段的内部结果拒绝，需协调升级Worker/AI；历史结果/derivative不重写。候选协议状态及production限制保持，真实CPU常驻HTTP待第38批。

2026-10-07 [第38批常驻CPU HTTP](08-delivery/38-i-ml01-resident-cpu-http.md)本地接通实际ready/version与development身份；Version新增必填model_checksum/dictionary_sha256，InferenceResult可选execution_identity，真实CPU必带且Worker核受控pin。新增development bundle/直接运行锁/空业务词典三个Schema，区别于完整ModelManifest及生产批准；内部消费者协调升级，历史结果保留，迁移仍0004。公共is_simulated取已验证执行身份，旧结果默认true，语义事实/公共activation/现场与生产验收未完成。

2026-10-09 [第39批文字/bottle关联](08-delivery/39-i-ml01-text-bottle-association.md)明确独立文字/裁剪指向真实同图COCO bottle，使用text-bottle-quad80-v1实际四边形面积/唯一最小框，未知保持NULL；AI/Worker重算语义门禁。字段形状及vision-v1不变，联合本地报告v3/代码锁重新绑定，无迁移、无历史回填；旧AI/Worker需协调升级。字段词法/化学词典/日期事实和现场批准仍待后续，几何关联不等于安全事实。

2026-10-09 [第40批OCR字段](08-delivery/40-i-ml01-ocr-fields.md)新增OCRField必填source_lines（1–2行全部line/crop/原文/逐行置信度），首行crop_id保留。AI/Worker共享ocr-fields-v1并核全量有序输出，联合报告v4/实际代码锁重新绑定；内部消费者协调升级，旧字段不猜补来源，历史结果只读。无新迁移，无实体/日期事实。用户素材经过默认质量门禁/官方CPU模型，实际零瓶检测，保持未知，不视作现场评测通过。
