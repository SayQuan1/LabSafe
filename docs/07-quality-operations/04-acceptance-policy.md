# 07.4 分层验收、指标计算与校准策略

决定编号ACC-BASE-01，日期2026-09-27。这里冻结“如何开发和判定”，不伪造测量结果或专家批准。工程标准立即生效；业务数值是明确的开发目标提案，正式试点仍要求业务负责人和安全专家批准。

## 1. 三层标准与代码行为

| 层 | 判定依据 | 失败后行为 |
|---|---|---|
| E 工程可运行 | 协议/权限/状态/幂等/租约/数据约束及故障用例全部通过 | 阻止相应模块合并，修代码，不靠调整模型阈值放行 |
| M 模型开发达标 | 本文数值目标和样本下限、固定模型/词典/数据/阈值 | 继续数据/训练/适配；可以保持dev联调，不允许宣称安全可用 |
| P 正式试点 | 使用机构批准的policy快照；真实数据评测、专家规则和安全/恢复记录齐全 | 未批准/未通过即禁止production发布，不影响dev/test编码 |

APP_ENV=production只接受purpose=production且calibrated的模型清单；必须有matching evaluation policy/report hash以及服务端批准台账。客户端evaluation_passed=true不是发布授权。dev/test允许模拟与未校准真实bundle，但必须显著标记，不连接生产数据库。

## 2. 工程验收现在冻结

测试目录的全部应用案例（含IRR新增案例）是待实现任务；每例必须通过，不用覆盖率代替行为。要求：跨租户数据泄露0例、重复消息额外业务结果0例、过期fencing回写成功0例、绕人工复核销项0例、引用证据缺失0例、密码/token明文日志0例。并发幂等固定10个同请求；租约和崩溃用例采用可控时钟，验证所有规定故障点。失败不得以业务指标较好抵销。

设计验证器检查Schema/参考语义/数据结构和文档；它的PASS不能替代上述应用实现测试。

## 3. 业务开发目标提案

policy_id=LABSAFE-PILOT-DRAFT-01、status=proposed。下列是本项目选择的开发目标，尚无已达标或已批准证据；不可当作通用实验室安全标准。机构可在最终test解封前形成新policy版本调整目标，必须写明原因与风险接受，不可看完test后临时降低门槛。

| 指标ID | 公式/单位与分组 | 开发目标 | 最少有效样本 |
|---|---|---|---|
| DET-P / DET-R | 每类IoU≥0.5的一对一匹配；P=TP/(TP+FP)，R=TP/(TP+FN) | 每类P≥0.85、R≥0.90 | 每类GT≥200且预测≥200，≥30个scene |
| OCR-NAME | 专家标为可读名称字段的规范化字符串完全相等比例；漏字段计错 | ≥0.90 | 200个名称字段、≥30scene |
| OCR-DATE | 可读且类型明确的expiry字段，字段类型和ISO日期都相等 | ≥0.95 | 100个日期字段、≥30scene |
| ENT-P | 标为resolved的实体中entity_id正确比例 | ≥0.98 | 至少200个resolved预测、≥30scene |
| ENT-COVER | 字典内可读目标中系统正确resolved的比例；unknown/candidate计未覆盖 | ≥0.80 | 200个字典内可读目标 |
| ENT-OOV | 字典外目标未被错误resolved的比例 | ≥0.99 | 100个字典外目标 |
| Q-FALSE-REJECT | 专家可用图被needs_retake拒绝的比例 | ≤0.10 | 100张可用图 |
| Q-FALSE-ACCEPT | 专家不可用图通过quality的比例 | ≤0.05 | 100张不可用图，覆盖模糊/暗/过曝 |
| RISK-P / RISK-R | 各已批准规则类型的候选与专家风险匹配，P/R；unknown不能算识别成功 | 每类P≥0.85、R≥0.95 | 每类100正例、100反例且≥30scene |
| UNCERTAIN-ROUTING | 专家标为证据不足的item被送needs_review/cannot_determine的比例 | ≥0.99 | 100个证据不足item |
| LATENCY-CUDA | submit事务提交至needs_review/needs_retake的耗时P95，含队列/RPC/规则 | ≤30秒 | 固定200case；预热10case不计入 |

样本不足状态inconclusive，不是pass；分母为0不得填0或100%。检测同时报告mAP50和mAP50-95作为诊断，但不以总体平均掩盖某一类失败。所有指标报告分子/分母、按scene分组的明细；比例额外报告Wilson95%区间，仅作不确定性呈现，不能当作安全保证。实现时z=1.96，公式中心=(p+z²/(2n))/(1+z²/n)，半宽=z*sqrt(p(1-p)/n+z²/(4n²))/(1+z²/n)；场景内相关性限制必须在报告说明，生产风险方可要求更大独立样本。

名称规范化仅NFKC、去首尾空白、连续空白合一；不删除化学名称中的数字/符号来提高成绩。匹配顺序预测confidence降序，再稳定ID；每个GT最多匹配一次。risk匹配键为rule_id+对应专家目标（通过检测匹配映射），不是只要同图有一个候选就算全部风险检出。

时延测试固定并发1，每case为1张2048×1536总览及2张1280×960细节，正式case包含成功/质量失败/证据不足；GPU、CPU、内存、驱动、依赖、模型hash写入测试开始前的环境快照。CPU只验完整闭环和已有超时预算，不承诺30秒。GPU达标只是该profile性能，不推定更高并发或所有设备均达标；失败/超时另报率，200case中任何技术失败均不得判该轮性能通过。

## 4. 阈值校准可直接实现的流程

除时延项外，各项至少覆盖30个scene；同一scene可支持多个字段但不能冒充多个独立scene。报告measurement同时记录positive_count/negative_count：DET的positive_count为该类GT实例数，RISK为规则类型的专家正/反例数，其他指标无这两个数量门槛则填实际可用计数或0。精确率分母是预测数，不能用正例数量代替；程序同时检查所有门槛。

只用validation，固定split hash与seed。顺序依赖：质量→检测→OCR→实体→邻接；先前步骤冻结后再调后一步，不在test调参。首轮候选网格：

| 参数 | 候选值 | 目标/约束 |
|---|---|---|
| blur_min | 20/40/80/120/160/240 | 与dark/glare按全笛卡尔积枚举；满足Q约束后最小总拒绝错误 |
| dark_min | 0.05/0.08/0.12/0.16/0.20 | 同上 |
| glare_max | 0.10/0.20/0.30/0.40/0.50 | 同上，共6×5×5=150组 |
| detection_min | 0.05至0.75，步长0.05 | 各类P≥0.85下最大宏平均R；再比较P，再较低阈值 |
| ocr_min | 0.40至0.95，步长0.05 | 名称/日期精确率达到目标下最大保留率 |
| entity_min | 0.60至0.99，步长0.01，加1.00 | ENT-P/OOV满足下最大ENT-COVER |
| adjacent_gap_ratio | 0.00至0.50，步长0.05 | 各规则P满足下最大R；再P，再较小ratio |

质量网格并列按(blur_min,dark_min,-glare_max)字典序选择；其他未说明并列按参数升序。浮点网格用整数缩放构建，避免累计误差。任一步无可行解，calibration_status=failed，报告约束违例；不把最接近的结果标calibrated，也不自动扩大test/改标签。

模型/阈值/规则/词典/代码任一影响输出的改动均创建新版本和新validation报告。固定test解封后仅作发布评估；据test改动模型需要新的独立保留集并说明旧test已变为研究数据，不能重复挑最好成绩。

## 5. 规则来源与专家工作包

首期只实现expired_label和incompatible_storage两种规则语法；不在文档中编造真实化学兼容矩阵。初始化dev规则使用明确synthetic类，生产拒绝synthetic来源。

每条真实规则需提供：rule_id/版本、适用实验室、规范或SDS/机构规程的文件版本及章节、对应entity/storage_class映射、severity/action、有效期、至少正例/反例/unknown边界例、提交人/独立专家复核人和记录引用。不能把“同柜且相邻”本身当全部化学风险知识；事实不足输出unknown。

管理员导入→Schema/语义/案例自动验证→独立专家审查来源和范围→批准发布→bundle冻结。无专家时工程开发继续，正式风险规则发布保持关闭；不得给开发者分配“自行推断安全标准”的任务。

## 6. 审批记录与防伪通过

配置由部署方维护，APP_ENV=production挂载只读approved-releases.json，结构在contracts/release-approvals-v1.json。每条精确绑定tenant、bundle、model manifest hash、approved policy id/hash、evaluation report hash、专家与业务批准人标识、批准时间和可审计记录引用。该文件由仓库外受控发布流程产生，API上传不能修改；仅知道用户ID或提交approved=true不能注册批准。

API validate/publish/activate和AI readiness检查匹配条目、两位批准人不同、审批记录非空、所指hash真实匹配；缺失即MODEL_VERSION_UNAVAILABLE或RULESET_INVALID（按资源），不发布。部署方的文件权限/审批存储是真实信任边界，不能由Schema证明人已经签字。未经批准的开发目标policy不得登记production条目。

## 7. 设计完成与后续任务

机器定义见contracts/acceptance-policy-v1.json、model-evaluation-v1.json及development-acceptance-policy.json。ML-BASE-02的content_sha256对artifacts、pipeline_version、dictionary_version_id、dictionary_sha256、thresholds、input_max_side、adapter_id、model_family、source_ref、source_commit、git_commit、detector_backend、ocr_backend、device_profiles、runtime_profile、runtime这16项的规范JSON计算；源码常量以tools/design/release_reference.py为准。evaluation绑定content_sha256，批准台账再绑定完整manifest hash，避免循环。runtime内含两阶段设备、精度、预后处理、opset、实际runtime-lock文件hash；锁文件变化必须新评测，不能复用同为cuda但内容不同的报告。校准报告是独立sidecar，不打包进4个模型artifact；正式validate从受控制品记录取出并校验report/split hash，不能只看字段非空。CPU/CUDA各有独立runtime_profile和bundle，评测policy的profile必须匹配。

本次检测器变更不降低第3节业务目标；D-FINE的sigmoid分数重新做validation校准，不继承旧检测器阈值的业务含义。正式报告必须包含四类映射/导出一致性、标签裁剪/OCR、瓶与容器关联、重复检测对规则结果的回归。50图PyTorch/ORT对照以及GPU容量/恢复工程门禁见模型方案和部署规范；不得以纯合成39项语义测试替代真实模型验收。

现在完成：目标、公式、样本下限、校准算法、版本/审批结构和缺失处理均已明确。I阶段编写评测器、采集标注、跑validation和模型；R阶段由实际责任人批准policy/规则并完成test。当前并不要求用户先交付这些尚未开发的产物，也不把它们的待执行状态改写成“设计未完成”。
