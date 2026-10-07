# 07.1 分层测试与验收用例

## 1. 当前可执行设计检查

在独立虚拟环境安装 tools/design/requirements.txt；从仓库根执行：

~~~powershell
python tools/design/build_specs.py --check
python tools/design/validate_specs.py
~~~

第一项校验生成物未漂移；第二项解析两份OpenAPI、JSON Schema，执行类型/AI/消息正反例、三值规则、FK/链接检查，并运行模拟发布门禁和D-FINE纯语义测试。新增测试覆盖类别往返、qmax并列、横竖图坐标、阈值/裁边、非有限值及100结果上限；不加载图片库或模型。该检查不是应用单测、真实resize/ONNX/CUDA测试、MySQL实跑或专家验收。结果见审计报告。

## 2. 编码阶段必须落地的测试

[第35批裁剪证据](../08-delivery/35-i-ml01-rebuildable-ocr-evidence.md)已补共享图像算法、9项裁剪验证与真实9区域跨进程重建，含源RGB/PNG/识别像素摘要、竖长区域90°识别旋转及篡改拒绝。此证据仅覆盖AI-09/UP-02的本地图像重建部分；未验证对象写入、唯一键事务、完整RPC证据归属、现场照片准确率或瓶子关联，不关闭整个用例。

下表是待实现的应用验收用例，不虚报已经执行。每例保存输入、关键DB断言、HTTP状态和trace；失败不得仅靠截图解释。

| ID | 前置条件与操作 | 明确预期 |
|---|---|---|
| API-01 | 每个operationId最小合法请求、删除required、增加未知字段 | 合法类型可解析；非法422；响应严格符合协议 |
| API-02 | 同key同body并发10次，再同key不同body | 只有一个聚合/任务/审计；后者409，无额外副作用 |
| API-03 | 两人同version同时confirm同finding | 恰一成功，一409，version只增加1 |
| SEC-01 | 不同tenant和同tenant不同lab访问GET/list/export/crop/original | 前者404，无计数/存在性泄露；original额外授权 |
| SEC-02 | 缺csrf、跨Origin、session过期/撤权、重用logout cookie | 拒绝；旧权限不因缓存继续生效 |
| UP-01 | 假MIME、hash不符、16MiB、40M像素以上、EXIF旋转图 | 前四拒绝；最后正确analysis方向/hash，原图不变 |
| UP-02 | 一个image生成3个crop，重复回写同run/crop | 3条不冲突，重复无第四条；crop hash可重建 |
| AI-01 | 相同请求对quality/runs；删pipeline、附rule_version、错模型hash | 正例回显；非法Schema/版本拒绝、不自动最新 |
| AI-02 | AI繁忙、超时、OOM、子进程被kill | 429/稳定错误；health仍可响应；子进程回收，ready恢复需smoke |
| AI-03 | CPU任务超过30秒但低于180秒，CUDA超过45秒 | CPU按预算继续；CUDA终止，不存伪成功 |
| AI-04 | 双图相似瓶、detail来自另一处、OCR日期歧义 | 不跨图合并邻接；越owner拒绝；歧义unknown |
| AI-05 | 官方 COCO 80 类、空标注、稀疏 category_id、80 类训练/评测往返 | 模型标签恰 0..79，评测预测反向到官方 category_id；未知拒绝，不创建背景类；不按前几行压缩类别 |
| AI-06 | 官方 COCO checkpoint，50 图含横竖图/红蓝像素/边缘框；PyTorch、ORT CPU/CUDA FP32 对照 | RGB/PIL直缩放/除255、W/H顺序正确；无二次坐标变换；全部候选数值与过滤结果符合模型方案容差 |
| AI-07 | logits并列、同query多高分、嵌套瓶标签、阈值相等、NaN/Inf、三图100/101结果 | 每query一类并列取小ID；无跨类NMS；等阈值保留；损坏MODEL_ERROR；100可返回、101整次失败不截断 |
| AI-08 | CUDA不可见、CPU fallback、错误lock/provider、OOM、kill/reload，连续容量请求 | 不伪装CUDA ready；不静默CPU重跑；子进程回收后smoke再就绪；容量/显存符合部署门禁，不改旧run设备 |
| AI-09 | 小标签、邻瓶标签、容器边界、重复query、无文字/未知日期 | 原图crop可重建；关联歧义unknown并人工复核；规则不因框变更绕过证据；保留CPU OCR配置 |
| FLOW-01 | 上传→提交→质量失败→补拍→零finding | 新revision/run；item needs_review，人工complete才completed |
| FLOW-02 | needs_review修订事实，旧规则结果晚到 | 新revision+评估；旧指针不回退，候选重新复核 |
| FLOW-03 | confirmed finding后试补拍/事实编辑 | 409；不能抹掉已确认风险 |
| FLOW-04 | 派发→接单→证据→驳回→新证据→独立复查 | 状态严格依序，最新证据必须匹配，最后task/finding同事务closed |
| FLOW-05 | assignee自己复查、wrong evidence、cannot_remediate | 前两拒绝；后者仍属于未解决风险，不计销项 |
| JOB-01 | 在提交前/发布前/发布后mark前/提交后ack前分别kill | 按持久任务规范恢复，至多一份业务结果 |
| JOB-02 | lease过期、新worker领取后旧worker回写 | 旧token拒绝，不能改新任务状态 |
| JOB-03 | 清空隔离测试Redis，连续4次可重试失败，再人工replay | DB任务重新调度；dead_letter；generation+1、attempt清零、历史保留 |
| DB-01 | 空MySQL执行DDL和Alembic；读回columns/index/FK | 与字典完全一致，无语法/索引长度/约束偏差 |
| DB-02 | 插入跨tenant/错item指针/重复scope role/错误owner | FK/unique/check拒绝；需要应用约束者事务拒绝 |
| RULE-01 | 模拟规则true/false/unknown，日期前日/当日/非法 | 与参考实现一致；unknown不出安全结论 |
| RULE-02 | 作用域覆盖、disabled覆盖、重复scoped ID、非法operator | 确定性选择；非法导入422；重放不变 |
| MODEL-01 | 缺artifact、wrong SHA、重复role、词典不匹配、无可信评测 | 不可validated/published/ready |
| MODEL-02 | 新D-FINE清单、旧适配器、改precision/OCR设备/source/runtime-lock却沿用报告；/version与manifest不符 | 合法候选仅在完整门禁后激活；错组合/旧报告拒绝；归档索引和实际lock逐文件校验，非只验证字段非空 |
| EXPORT-01 | 冻结后改finding，导出CSV恶意公式文本；权限撤销再下载 | 内容仍为快照、公式转义；撤权拒绝 |
| AUDIT-01 | 每写命令及敏感读取，故意使审计写失败 | 审计完整；写失败则业务回滚，无日志secret |
| UI-01 | 新run提交后旧轮询响应到达、409、离线重连 | 不回退状态、不重复提交、可恢复 |
| OPS-01 | 隔离环境按备份恢复run/证据/任务 | hash和FK完整，实际RPO≤24h、RTO≤4h |
| OPS-02 | 阻断公网完整闭环、模型首次启动 | 无运行时下载或外联依赖，所有assets本地 |
| EVAL-01 | 冻结test按scene分组，模型/规则真实评测 | 输出规定报告；与预先批准数值目标比较，不调test阈值 |
| JOB-04 | inference首次领取、quality/facts可重试失败、kill后sweeper、人工replay及旧token | task/run/item按IRR原子收敛，重试从quality开始；旧run不复活，rule任务不误用inference守卫 |
| FLOW-06 | 零finding+unknown、confirmed+unknown、全rejected且事实完备；same_location真/假/null往返 | U/C守卫互斥；前两只能cannot_determine，后者no_issue；null不变false；pending finding不能完成 |
| CAP-01 | 2×50/2×51模板位置；200/202 findings；20/21瓶；500/501 OCR；100/101 dates；20000/20001上下文 | 边界允许，超限按规范拒绝，无部分业务/事件；不得截断，原有facts修订和历史保留 |
| SEC-03 | 实际最小权限凭据逐项访问S/O/A/D/R、越tenant/lab、AI写入、cleanup无version或引用证据 | 矩阵允许操作成功，其他拒绝；Copy精确读版本/写目标，应用引用/legal_hold守卫不能跳过 |
| UP-03 | 隔离HTTPS入口浏览器PUT/GET、篡改/过期签名、超15MiB、重复PUT版本、内部端口探测 | 同源签名可达、原样透传；403/413等符合部署规范；固定旧版本不漂移，内部9000不公开 |
| AI-10 | quality黄金像素/resize、OpenCV核等价、原始Paddle行、名称跨行/冲突日期/容器并列 | 精确符合quality-rgb-lap1-v1/ocr-fields-v1及关系规范；不以合成标量测试替代实际图像库对照 |

## 3. 实施完成定义

本地[质量/联合CPU续批](../08-delivery/34-i-ml01-quality-cpu-pipeline.md)新增17项真实像素/门禁/失败回收测试，CPU45项全部执行；包含实际Pillow resize、NumPy与OpenCV核对照和真实横竖图联合模型smoke。仍未覆盖文字到业务字段/关系、受控对象存储和HTTP集成，AI-09/10不因此整体关闭。

2026-10-07本地CPU OCR实际覆盖见[PP-OCRv6_small续批](../08-delivery/33-i-ml01-ocrv6-cpu.md)：12项新增数值/几何/容量/安全加载/子进程测试、真实合成中英标签与重放已执行。与既有检测CPU回归合计28项，不含现场准确率、Paddle完整黄金对照、化学实体/日期事实或HTTP集成验收。AI-09/10仍须业务证据闭包与完整真实图像对照，不能用三行合成字替代。

每模块PR必须附对应上述测试。集成测试真实MySQL/Redis/MinIO；AI流程可先使用协议mock，但MODEL/EVAL/OPS不接受mock替代。故障注入只在隔离测试环境，不对用户现有数据库执行破坏操作。

## 4. 发布门禁

协议检查→应用单元/集成→端到端/故障→真实模型数据/专家规则→安全评审→恢复/断公网→试点。任何mock通过只解锁下一阶段，不宣布真实模型业务验收完成。precision/recall、样本量等业务目标仍须审批冻结后才运行最终EVAL-01。
