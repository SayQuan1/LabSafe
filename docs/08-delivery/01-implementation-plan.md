# 08.1 实施计划与当前批次

状态更新于2026-10-10。已fetch核验origin/main=13a854f：PR #10包含截至第26批的持久任务、图像、推理/规则执行与复核；PR #11包含第27–30批整改及CSV报告受理/执行/下载；PR #12包含第31–35批官方80类模型、CPU检测/OCR、质量流水线及可重建裁剪。第36–41批已本地实现与验证，包括第41批冻结开发词典、名称候选/冲突聚合和实际MinIO验证，位于wsq/i-ml01-analysis-input；实现提交726786c已Push，并创建[PR #13](https://github.com/SayQuan1/LabSafe/pull/13)，尚未合并，等待远程CI及独立审查。最新Git交付状态见[第41批交付记录](41-i-ml01-chemical-candidates.md)。

当前主线为I-ML-01真实CPU推理接线：第41批冻结开发词典、CAS/别名/模糊候选及同瓶冲突聚合已本地完成；下一批确定为**第42批，同瓶日期聚合、冲突/低置信/未知保留与完整DateFact证据闭包**。I-02/I-03/I-ML-01仍进行中，局部工程成绩不替代完整业务、现场模型或生产验收。阶段标准见[阶段门禁](05-stage-gates.md)，运行入口见[开发指南](../../DEVELOPMENT.md)。

## 1. 当前任务进度

不要求先有完整应用再准备真实模型；业务和模型按各自依赖推进。采用用户已提供的官方D-FINE COCO 80类ONNX与PP-OCRv6_small det/rec ONNX，当前仅CPU，不安装CUDA/Paddle，不回退四类训练路线。

| 编号 | 已交付及当前状态 | 证据 | 剩余边界 |
|---|---|---|---|
| I-01A | 工程/进程入口、独立Python3.11环境、CI；已合并PR #5 | [骨架验收](07-i01a-acceptance.md) | 开发骨架不代表完整服务或生产部署 |
| I-01B | 初始迁移、结构核对、bootstrap；已合并PR #7。0002已随#11合并，0003/0004为第36/37批本地增量 | [数据库验收](08-i01b-acceptance.md)、[第36批](36-i-ml01-controlled-analysis-input.md) | 恢复、部署与生产迁移另行验收；历史快照不改 |
| I-01C | Session/RBAC、租户事务、幂等、安全原语；已合并PR #8 | [安全原语](09-i01c-acceptance.md) | 实际部署权限及完整安全验收仍待完成 |
| I-02 | 身份、基础、上传、提交、规则管理、复核、整改、CSV报告API已分批合并；整体进行中 | 第9–30批交付记录 | 完整item complete/retry等其余命令、PDF/通知/过期清理、真实存储与完整UI仍待完成 |
| I-02A | 13个领域命令守卫；已合并PR #9 | [领域守卫](10-i02a-domain-guards.md) | 纯守卫不等于每个公共命令已接事务 |
| I-02B | 登录/会话/用户7个API、限流与并发安全；已合并PR #9 | [身份API](11-i02b-identity-api.md) | 仅dev/test；生产授权单独验收 |
| I-02C | 角色列表/授予/撤销、作用域与会话撤销；已合并PR #9 | [角色API](12-i02c-role-api.md) | 前端管理与部署验收待完成 |
| I-02D | 组织/模板/巡检草稿16个API；已合并PR #9 | [基础API](13-i02d-foundation-api.md) | 不等于巡检全部状态命令已实现 |
| I-02E | 巡检项列表/详情、动作投影基础；已合并PR #9 | [巡检项查询](14-i02e-item-query-api.md) | item allowed_actions仍需可信默认图片选择上下文 |
| I-02F1/F2 | 上传授权、完成受理、固定staging版本及图像任务入队；已合并PR #9 | [授权](15-i02f1-upload-grant-api.md)、[完成受理](16-i02f2-upload-completion-api.md) | 实际S3/IAM/nginx/TLS与浏览器联调待完成 |
| I-02F3 | 真实图像解码/一次EXIF/RGB PNG、O/A版本登记及ready/rejected事务；已合并PR #10 | [图像处理](19-i02f3-image-validation.md) | 真实存储部署验收、ImageValidated领域事件发布待完成 |
| I-02F4 | ready分析图READ/原图Admin审计、准确版本60秒下载签发；已合并PR #10 | [图片下载](21-i02f4-image-download.md) | 真实IAM/TLS验收待完成 |
| I-02G1 | submit锁定输入/配置，run/run_images/task/outbox原子入队；已合并PR #10。第36批补SHA及准确对象版本冻结 | [提交入队](22-i02g1-submit-inference-enqueue.md)、[版本冻结](36-i-ml01-controlled-analysis-input.md) | 常驻真实CPU HTTP已本地接通第38批；公共模型activation与语义事实闭环尚未完成 |
| I-02（规则管理） | 规则导入/审批/发布/退休、不可变bundle与治理API；已合并PR #10 | [规则管理](25-i02-rule-management.md) | 工程治理不代表真实化学安全规则获批 |
| I-02（人工复核） | 事实读取/人工修订、finding查询和三种决策；已合并PR #10 | [复核](26-i02-review.md) | 完整item complete及真实AI证据/事实接线待完成 |
| I-02H | 整改派发/认领/证据/独立复查/驳回/转派，事务审计与事件登记；已合并PR #11 | [整改](27-i02-remediation.md) | 通用领域事件发布/通知投递及完整UI待完成 |
| I-02I1 | 报告快照/异步受理/查询/failed重放；已合并PR #11 | [报告受理](28-i02-report-export.md) | 不再把CSV执行/下载列为待实现；PDF等见下两行 |
| I-02I2 | CSV公式防注入、q.reports发布、专属租约/心跳/重试/回收、版本/SHA/大小与ready登记；已合并PR #11 | [CSV执行](29-i02-report-csv-execution.md) | PDF、完成通知、expired转换/精确版本清理、实际存储与UI；EXPORT-01/JOB-04未整体关闭 |
| I-02I3 | 创建人/同范围Admin、完整授权/expiry重检、60秒准确VersionId CSV下载；已合并PR #11 | [报告下载](30-i02-report-download.md) | PDF与真实签名入口/IAM/TLS验收待完成 |
| I-03 | 图像/推理/规则/CSV的持久调度执行基础已交付；整体进行中 | [调度](17-i03a1-durable-dispatch.md)、第18/23/24/29批 | 通用领域事件publisher/inbox、完整非图像任务重放和真实AI闭环待完成 |
| I-03A1 | Outbox发布租约、有界发布、补发/回收及q.general调度；已合并PR #10，CSV q.reports已随#11合并 | [调度](17-i03a1-durable-dispatch.md)、[CSV执行](29-i02-report-csv-execution.md) | TaskDispatch已支持的类型不代表所有领域事件均已投递 |
| I-03A2 | validate_image/inference_pipeline/rule_evaluation及CSV report_export的执行租约、围栏和恢复已接；#10/#11已合并 | [图像执行](18-i03a2-image-execution.md)、[推理执行](23-i03a2-inference-execution.md)、[规则执行](24-i03a2-rule-evaluation.md)、[CSV执行](29-i02-report-csv-execution.md) | 独立OCR证据闭包已本地接通第37批；常驻真实CPU HTTP已本地接通第38批；完整JOB-04仍待关闭 |
| I-03A3 | 图像任务查询与管理员固定输入重放；已合并PR #10。报告failed重放随#11合并 | [图像重放](20-i03a3-image-job-replay.md)、[报告受理](28-i02-report-export.md) | 不宣称已实现所有任务类型的人工重放 |
| I-ML-01 | 官方80类适配、CPU检测/OCR/质量/可重建证据已合并#12；受控analysis/文字证据/常驻HTTP/关联/字段/开发词典候选已本地完成第36–41批；整体进行中 | 下方批次表 | 日期聚合/事实快照、可信化学安全词典、公共activation/多版本路由和现场评测仍待完成 |
| I-ML-02 | 未完成 | [模型/数据路线](../04-ai-rules/02-model-data-plan.md) | 授权台账、100张标注试行、完整数据/split/审核，未获授权不抓取替代 |
| I-ML-03/04 | 真实现场评测/校准未开始验收；确定性事实规范已有设计，业务算法仍待接线 | [事实规范](../04-ai-rules/03-fact-extraction-contract.md) | 数据、字段/词典/关联、真实照片和一致性/资源报告；当前CPU工程smoke不替代AI-05至AI-10/MODEL-02 |
| I-04 | 只有开发Web骨架，完整业务UI未完成 | [交互规范](../05-frontend/01-interaction-spec.md) | 登录、采集/复核/整改/admin/report与真实API联调 |
| I-05 | 开发入口/局部探针及第41批独立真实MinIO版本/限定IAM验证已交付，完整部署未完成 | [部署规范](../07-quality-operations/02-deployment-operations.md)、[第41批](41-i-ml01-chemical-candidates.md) | Compose/持久secret/公开签名入口/IAM/TLS、监控、备份恢复、离线演练；临时loopback不等于生产部署 |
| R-01 | 未进入，production仍禁止 | [发布门禁](05-stage-gates.md) | 数据/模型/专家规则、现场指标、安全恢复与双人批准 |

“已合并”“本地实现”“本机验证”“远程CI”“独立批准”分别记录。各交付文件中未提交或未实现的描述为实施当时的历史状态；本表给出现行进度，后续批次覆盖的旧边界不继续当作待办。默认开关仍关闭，仅dev/test，不因合并改为生产可用。

### 1.1 CPU主线截至第41批

| 批次 | 已交付 | 当前交付状态 | 证据 |
|---|---|---|---|
| 31 | 官方model/config/preprocessor核验、raw ONNX签名与COCO 0–79类别协议 | 已合并PR #12 | [官方适配](31-i-ml01-official-onnx-80class.md) |
| 32 | 有界spawn CPU检测、1–3图CLI、失败/超时回收和重放 | 已合并PR #12 | [CPU检测](32-i-ml01-cpu-detection.md) |
| 33 | PP-OCRv6_small det/rec、DB/CTC、独立文字行与CLI | 已合并PR #12 | [OCR](33-i-ml01-ocrv6-cpu.md) |
| 34 | quality-rgb-lap1-v1、同一子进程联合模型、整批质量门禁 | 已合并PR #12 | [联合流水线](34-i-ml01-quality-cpu-pipeline.md) |
| 35 | perspective-rgb-v1共享重建、方向/PNG/识别像素摘要、9区域跨进程核对 | 已合并PR #12 | [可重建证据](35-i-ml01-rebuildable-ocr-evidence.md) |
| 36 | run_images准确版本冻结/0003、只读SigV4 GET、SHA/128MiB/像素校验、保留Worker图片ID、10/180秒预算 | 已提交/Push，PR #13待审查 | [受控输入](36-i-ml01-controlled-analysis-input.md)、[真实模型smoke](36-i-ml01-analysis-cpu-smoke.json) |
| 37 | 独立text_regions/闭包、0004、独立Worker重建/版本上传、围栏原子登记、只读文字投影 | 已提交/Push，PR #13待审查 | [OCR证据](37-i-ml01-worker-ocr-evidence.md)、[真实模型→Worker smoke](37-i-ml01-worker-evidence-smoke.json) |
| 38 | 常驻CPU加载/smoke、受控development bundle/lock、实际ready/version、正式RPC、Worker pin/有界socket和真实标记 | 已提交/Push，PR #13待审查 | [常驻CPU HTTP](38-i-ml01-resident-cpu-http.md)、[真实HTTP→Worker smoke](38-i-ml01-cpu-http-smoke.json) |
| 39 | text-bottle-quad80-v1实际quad/同图COCO bottle/唯一最小关联、Worker重算及NULL未知、证据不变 | 已提交/Push，PR #13待审查 | [文字/bottle关联](39-i-ml01-text-bottle-association.md)、[正式HTTP smoke](39-i-ml01-text-association-smoke.json) |
| 40 | ocr-fields-v1词法/严格日期CAS/全局连续配对，source_lines全部证据、Worker全量重算、正式CPU/HTTP/像素/MySQL；用户素材工程探针 | 已提交/Push，PR #13待审查 | [OCR字段](40-i-ml01-ocr-fields.md)、[真实HTTP/素材smoke](40-i-ml01-ocr-fields-smoke.json) |
| 41 | 冻结开发词典/身份快照、chemical-candidates-v1及全名称冲突聚合；Worker完整重算/保存；4张新素材及实际MinIO版本/IAM | 已提交/Push，PR #13待审查 | [名称候选](41-i-ml01-chemical-candidates.md)、[真实MinIO/素材](41-i-ml01-minio-smoke.json)、[明确synthetic候选probe](41-i-ml01-candidates-smoke.json) |

截至第36批最新本机成绩：63项CPU＋4项协议，无跳过；主业务/领域/安全/协议1552项；持久化单元21项；隔离MySQL8.0.33定向18项通过/429未选，含2项重叠单元。第36批验证时工作树head=0003_run_image_version，43表/115FK；远程main已合并的head仍为0002。最新真实权重工程smoke为两图固定版本读取、两次联合重放、quality一致与6区域裁剪重建。此前第29批完整持久化446项及第30批定向73项为历史成绩，不当作0003的完整重跑。各套件有重叠，不累加成唯一测试数。

第37批最新本机成绩：主业务/领域/安全/协议1613通过，CPU63＋协议4无跳过；独立证据环境5项真实像素/SDK/实际子进程用例无跳过；新协议/Worker/S3定向60项；隔离MySQL8.0.33定向40项通过，含21项持久化单元，不代表全量持久化重跑。head=0004_ocr_evidence，43表/115FK无结构差异。真实CPU→Worker工程smoke为两张合成中英标签图、6文字区域/裁剪，核源RGB/PNG/识别摘要、准确版本上传和复用、第二crop失败无部分成功。对象存储为loopback模拟器，数据库围栏另以MySQL验证；详见第37批交付记录，不替代现场准确率或生产批准。

### 1.2 第41批交付与下一批范围

第38批常驻CPU HTTP/身份/Worker pin与故障恢复继续复用；其1641项业务、72项CPU、23项HTTP/进程、42项MySQL定向为历史成绩，不作为本批复测。第39批**本地完成与验证，未提交/Push/PR**：独立文字/crop指同图真实COCO bottle（class 39），text-bottle-quad80-v1中心/实际quad交面积≥80%/唯一最小框，无候选或并列NULL；Worker重算全部选择，原文/ID/像素/准确对象版本不变。字段形状保持，本地联合报告v3，development代码锁重新绑定，无迁移、历史不回填。

最新本机：业务/领域/安全/协议1672通过，含31项关联/闭包；独立CPU/像素/supervisor/协议74项无跳过；fixture/spawn/ASGI24项；独立Worker证据7项无跳过；隔离MySQL8.0.33定向50项通过（含21项unit），head0004/43表115FK无差异，非全量持久化重跑。官方权重HTTP→Worker合成标签两图/6区域/6crop，模型没有检测到bottle，6条均NULL；正向/并列几何、HTTP、真实像素、MySQL关联用显式synthetic框另测，不冒充现场模型识别成绩。详见[第39批](39-i-ml01-text-bottle-association.md)。

第40批**本地完成与验证，未提交/Push/PR**：OCRField新增必填source_lines（1–2条line_id/crop_id/原文/逐行confidence），原crop_id仅作首行引用；纯Python ocr-fields-v1执行NFKC、最长前缀、严格公历日期/CAS校验和同图同唯一瓶全局紧邻配对。Worker从已核text_regions重算全部有序字段，遗漏/伪值/不全证据/冲突丢弃整批拒绝。联合本地报告v4，正式HTTP保留低置信与date_unknown，仍needs_review；无实体/日期事实或新迁移，旧内部消费者协调升级，历史不回填。

最新本机：83项新增词法/字段闭包、业务/领域/安全/协议1755通过；75项CPU、25项fixture/HTTP、8项独立Worker像素通过，末轮CPU/像素/协议17项定向再通过；MySQL8.0.33定向50项通过（含21项unit），非全量持久化重跑，0004/43表115FK。静态/生成/设计/三环境检查通过，远程CI未执行。详见[第40批交付](40-i-ml01-ocr-fields.md)。真实官方权重HTTP→独立Worker运行两张合成标签及用户提供现场图：共12文字/12crop，现场图通过默认质量门禁，但最高瓶分数约0.328低于0.4，实际0检测/4文字/0字段。没有擅自降低阈值。独立正向探针用明确synthetic瓶框及真实OCR/像素验证4字段，其中2个跨行字段；不冒充模型现场检测成绩，也不视为标注评测。

第41批**本地完成与验证，未提交/Push/PR**：有界开发词典≤100条/64KiB，chemical-candidates-v1执行CAS/别名/模糊候选和所有name字段min共识，先判冲突/并列/低置信再top5。新development profile/报告v5及extraction_context原始bundle/dictionary快照绑定Worker冻结SHA，重算全部字段/候选及阈值，整批拒绝伪候选或词典/阈值替换；仍needs_review，无DateFact/事实快照。无迁移，历史不回填。

第41批最新本机：39项新候选/快照用例；业务/领域/安全/协议1795通过，CPU75、fixture/HTTP26、独立Worker像素9通过；MySQL8.0.33定向50通过（含21 unit），0004/43表115FK，非全量持久化重跑。实际MinIO RELEASE.2025-07-23启动独立临时版本存储，11项限定IAM拒绝、准确旧版本读取/裁剪SHA/版本复用/退出清理通过。4张新素材默认质量均pass、瓶子分别3/1/0/0，共34文字/裁剪、仅1条唯一关联、0字段；第三图只检出cup，不改名。测试词典/合成瓶框正向probe单独记录，不冒充现场化学实体识别。见[第41批](41-i-ml01-chemical-candidates.md)。

下一批确定为**第42批：同瓶日期聚合、冲突/低置信/未知保留与完整DateFact证据闭包**，对应AI-10的日期部分。先冻结DateFact与全部来源证据契约，再从第40批日期字段聚合；不能把生产日期当有效期，缺失/非法/低置信/冲突继续unknown。

| 工作包 | 必须交付 | 验收条件 |
|---|---|---|
| 日期/全部证据契约 | 明确同瓶kind/value及全部行/crop证据，≤10条证据上限、kind唯一 | 多行/多字段来源完整、去重不丢冲突，容量溢出整批拒绝 |
| 日期确定性聚合 | 同瓶同kind所有字段均严格可解析、达到固定ocr_min且日期相同才保留value | 冲突/非法/低置信值null，date_unknown转unknown；不补缺失kind，不用生产日替代有效期 |
| 正式CPU/Worker | 共享日期算法、受控阈值/快照身份与全部来源重算，接HTTP/像素/围栏保存 | 伪日期或缺来源整批拒绝；仍needs_review，完整FactRevision/规则接线另批 |

第42批不实现完整事实快照/规则接线、容器/邻接、crop下载/UI、孤儿清理或现场评测；可信化学数据和安全分类仍须来源/审核，不导入模拟条目供真实判断。真实MinIO loopback版本/IAM本批已验证，公开签名/nginx/TLS/持久部署及公共activation/多版本路由仍未完成。素材检测和文字能力局限需后续标注/阈值与预处理对照评测，当前默认阈值和CPU选择保持。

### 1.3 后续依赖与待办顺序

1. 第42批先冻结DateFact和全部来源契约，再接同瓶日期/冲突/低置信聚合及Worker重算。
2. 再接完整事实快照、固定可信化学安全数据及人工复核/规则；容器不确定保留unknown。分别关闭AI-10部分，几何、词法或候选成功不等于完整事实能力。
3. 业务侧继续item complete/retry等未接命令、通用领域事件投递/inbox、完整任务重放与UI；报告侧继续PDF证据快照/渲染、通知、expired转换和精确版本清理。已交付CSV执行/下载不重复开发。
4. 公共模型导入/activation、多版本静态路由、数据授权/标注、现场校准评测、实际MinIO/IAM/nginx/TLS、依赖/资源/恢复/离线演练、试点双人批准分别验收。当前CPU选择不变，CUDA须后续明确授权才安装。

## 2. 数据库验证的精确任务边界

目标MySQL8.0.16+、InnoDB、UTC、utf8mb4_bin。先DDL空库检查，再迁移实现和数据不变量测试；两者使用明确标注可丢弃的独立测试schema，凭据来自测试secret。结果记录数据库版本、迁移head、列/default/index/FK差异、每个非法插入的预期错误、应用事务拒绝路径及日志。

测试至少包括：跨tenant FK、错item current pointer、角色scope重复、owner两列同时空/同时非空、多crop同图合法/同run同crop重复、旧lease回写拒绝。不能只看到CREATE TABLE成功就算通过。仅明确可丢弃test库验证downgrade/upgrade；有数据时优先备份与前向修复，不复用“重建全部表”策略。新批次按变化重跑相关验证，不用历史成绩替代。

## 3. 模拟与真实环境

API /me返回environment；InferenceRun返回is_simulated。dev/test的所有页面显示“开发环境，结果不用于安全判断”，模拟run额外标记。fixture-v1是独立进程同协议实现，不是绕过Worker直写成功；保留延迟/超时/错误和质量分支。真实开发报告的is_simulated=false只说明执行真实模型，不代表生产批准。production启动前检查制品purpose/adapter/台账，任何mock或未批准bundle均拒绝。

## 4. 任务完成的证据

IRR六项设计前置按[关闭清单](06-implementation-readiness.md)执行。任务必须认领JOB-04/FLOW-06/CAP-01/SEC-03/UP-03/AI-10，分别附事务、三值、容量、实际IAM、真实签名及图像/OCR黄金对照证据。设计reference不是可直接部署的业务服务，尤其不得把prefix测试当权限校验、把端点校验当签名验证。

任务PR包含所实现契约/需求/测试ID、实际命令、环境版本、结果日志、未通过项及回滚；应用测试、模型评测和人工批准分别记录。依赖lock、镜像digest、模型SHA、成绩只能从实施结果生成，不预填假值。最新记录是[第41批](41-i-ml01-chemical-candidates.md)，本地验证与真实部署验收分开记录。

## 5. 协作和Push

2026-10-09本次fetch核验：截至26批随PR #10合并（4e4f8bf），27–30批随PR #11合并（27c847c），31–35批随PR #12合并；origin/main=13a854f。此前报告/模型堆叠分支安排已完成，不再作为当前开工步骤。

当前分支wsq/i-ml01-analysis-input包含第36–41批未提交实现与进度文档更新；本次未自动提交/Push/PR。按[协作规范](../../CONTRIBUTING.md)，提交PR前fetch并同步最新main后重测，不直接push main；独立协作者审查，数据库/权限/模型变更须两位协作者确认，不自动合并。
