# 04.1 模型适配、事实与规则

## 1. 三层数据与可信边界

模型输出是观测；FactRevision是可人工修订的解释；Finding是确定规则产生、等待人工确认的候选。模型置信度不是安全严重度。不得从OCR未识别推断安全，也不得从模型结果直接派发整改。

[manifest Schema](../../contracts/model-manifest-v1.json)提供制品、模式、适配器、词典、阈值、校准和评测引用。production必须使用已校准且获得批准的manifest；dev/test允许[模型方案](02-model-data-plan.md)明确标注的联调初值，缺值仍拒绝加载，不临时猜测。bundle有detector/ocr/quality/dictionary各一份、每个SHA逐文件验证。evaluation_passed只是声明，正式validate/publish/activate须按[验收规范](../07-quality-operations/04-acceptance-policy.md)核验真实报告及服务端批准台账。选型和 COCO 80 类模型路线已经固定，实测和业务批准属于I/R阶段，不是当前设计必须先产出的结果。

## 2. vision-v1 适配器接口

| 接口 | 输入 | 输出与算法要求 |
|---|---|---|
| Quality.evaluate | RGB8 H×W×3 | quality-rgb-lap1-v1：最长边仅缩小到1024、PIL BILINEAR；固定整数灰度和四邻域Laplacian总体方差；精确参数见事实提取规范 |
| Detector.detect | RGB8直接resize640/除255，pixel_values=NCHW float32 | dfine-coco80-rgb-stretch-v1：实际logits[1,300,80]/pred_boxes[1,300,4]，qmax/sigmoid及cxcywh转换；官方 COCO 0–79 类、无四类压缩、无NMS；保留框裁边归一化，全请求最多100结果，详见模型方案 |
| OCR.recognize | RGB8原图或明确文字区域；适配层转BGR | PP-OCRv6_small ONNX CPU：DB文字检测、透视裁剪、CTC解码到有序文本行(text,confidence,quad)，按y再x排序；不透传模型原始张量 |
| Dictionary.resolve | OCR字段、冻结词典 | 规范化NFKC+casefold、空白压缩；优先CAS精确，再alias精确，再规范Levenshtein相似度1-distance/maxlen；降序score再entity_id，top5 |
| Relations.evaluate | 同图overview的bottle boxes及容器归属 | 中心点落入的最小面积容器，面积并列为未知；同容器时gap/max(widths)≤adjacent_gap_ratio且竖直交长/min(heights)≥0.5为adjacent；无/歧义容器unknown，不从框重叠猜遮挡 |

这是首期要求模型包装器遵守的接口，不声称任意下载的ONNX都天然符合。实际模型不匹配必须增加显式适配转换并评测，不能猜输出张量。quality artifact保存算法/依赖配置，OCR artifact可为包含模型和字典的受控只读包；解包拒绝绝对路径/..和符号链接。具体模型权重及兼容runtime锁文件尚须实物验收。

当前 [CPU 本地检测续批](../08-delivery/32-i-ml01-cpu-detection.md) 已执行真实官方权重。COCO 无 label/shelf/cabinet，因此COCO入口不产生标签OCR、化学实体或容器关系，parent_detection_id=null；OCR续批可独立识别原图文字，尚未将文字绑定到瓶子/化学字段。下述标签/关系算法是待接线要求，不能通过将任意 COCO 类改名为旧四类来填补证据。

IRR-06的确定性细节、OCR行到字段及黄金输入见[事实提取规范](03-fact-extraction-contract.md)。用户在2026-10-07选择PP-OCRv6_small ONNX，替代原PP-OCRv4 CPU路线；D-FINE-N和确定性事实语义保持一致，不引入LLM。实际CPU文字适配见[OCR续批](../08-delivery/33-i-ml01-ocrv6-cpu.md)，行到业务字段/实体关联仍待接线。参考代码是设计工具，不是已实现的服务。

质量未通过比较严格使用blur_score<blur_min、brightness<dark_min、glare_ratio>glare_max；恰等阈值为通过。首期自动quality只输出blur/dark/glare原因；协议occluded/unreadable保留扩展，本期不以没有定义的遮挡模型自动判失败；识别不足通过needs_review保留不确定性。

label→bottle关联：label中心在bottle内且包含面积≥80%时选面积最小的bottle；歧义则parent_detection_id=null并needs_review。没有label时不对整张背景OCR猜化学品。日期标签按明确关键词“有效期/失效/EXP”“生产/MFG”“开封”分类；日期只接受YYYY-MM-DD、YYYY/MM/DD、YYYY年MM月DD日，缺年/仅年月/含糊格式保留date_unknown并unknown；不得将生产日当到期日。到期当天不算expired，expiry<reference_date才true。

实体仅当唯一最佳候选且score≥entity_min、OCR相应字段confidence≥ocr_min才resolved；并列最佳或低阈值为candidate；无结果unknown。模糊候选不能直接给规则提供确定storage_class。人工选择必须属于固定dictionary，保留reason和证据。

## 3. rules-dnf-v1 语法

[规则Schema](../../contracts/rule-dsl-v1.json)，[可执行语义参考](../../tools/design/rule_reference.py)。clauses为OR，内部atoms为AND；不支持NOT、脚本、网络、任意字段、隐式类型转换。最大200规则，每规则10子句，每子句10原子。

| 字段类型 | 允许运算 | value |
|---|---|---|
| left/right.storage_class 字符串 | eq / in | 字符串 / 非空去重字符串数组 |
| same_location、adjacent 布尔 | eq | 严格boolean，不接受0/1或字符串 |
| expiry_date 日期 | before_reference_date | 必须省略value，引用run冻结日期 |

incompatible_storage 每个子句必须包含same_location==true、adjacent==true；因此绝不把跨图、不同柜或未知关系误作不相容存放。日期规则按单目标评估；成对规则枚举同overview的无序瓶对，按detection_id排序为left/right，双向规则要显式用两个OR子句，不隐式交换。

## 4. 三值逻辑、冲突和输出

- 缺失/null/未resolved实体/无证据/日期非法为unknown；不能当false。AND任一false为false，否则任一unknown为unknown，否则true；OR任一true为true，否则任一unknown为unknown，否则false。
- 在run提交时间按[effective_from,effective_to)选择有效规则，非执行时当前时间。相同rule_id按laboratory>tenant>global覆盖，scope_id必须对应；同scope同rule_id重复在导入时报错。上层disabled可屏蔽下层，但须专家审批且审计；不同rule_id的冲突全部保留，不能凭severity自动删除。
- priority只控制显示/稳定排序，不改变真值；结果按severity critical/high/medium/low、priority降序、rule_id和subject IDs升序排序。
- true创建needs_review finding，severity/action/explanation来自已发布规则；false无finding；unknown记录evaluation.result的原因和subjects，要求人工复核，不能显示“全部通过”。每次评估输出逐(rule_id,subject_ids)的truth/reasons/evidence；finding fingerprint=hash(rule_id+有序subject_ids)，同evaluation唯一。
- explanation只允许固定占位符left_name/right_name/expiry_date/reference_date；未知或其他模板表达式导入422，必须HTML转义显示，不支持Python格式表达式执行。

## 5. 事实到规则上下文

Worker将resolved entity映射到固定词典storage_class；relation是adjacent/not_adjacent/unknown映射true/false/null；same_location仅明确同容器或人工证据确认才true，不能只因同laboratory置true。日期只采用kind=expiry且可解析的值；多个冲突日期unknown。

IRR-02：RelationFact.same_location为必填boolean|null。相同已知容器=true，不同已知容器=false，任一容器缺失/歧义=null；缺整条relation也补为规则上下文的null，不补false。AI Relation仍不增加same_location字段，由Worker按本结果父容器闭包计算；跨图不生成成对上下文。same_location非true时relation必须unknown；adjacent/not_adjacent只可与same_location=true组合。人工编辑遵守同一语义及证据要求，未知用JSON null，不接受0/1/字符串。持久化JSON和DTO原样保留null；规则适配层不得bool(value)。

人工修订source=human；原模型observations不变。Worker拼接引用闭包后evaluate，记录evaluator_version、bundle checksum、reference_date、fact_revision。单次评估最多20000上下文命中，超过上限422 RULESET_INVALID，不截断假装没有风险。

### 5.1 IRR-03容量闭包

| 层 | 固定上限和超限行为 | 检查位置 |
|---|---|---|
| 巡检创建 | template_items×distinct location_ids≤100；超出422 VALIDATION_ERROR，details.field=location_ids | 建Inspection/items前检查，零业务/Outbox写入 |
| 规则snapshot | 合并成员的原始规则总数≤200（含scope覆盖项）；超出422 RULESET_INVALID | createRuleBundle拒绝；不得拼成100×200条规则 |
| 检测 | 全1–3图阈值/裁边后≤100 | 超出AI HTTP500 MODEL_ERROR、retryable=false |
| AI/事实关系 | overview全部bottle无序对，n×(n−1)/2≤200；含不同/未知容器的unknown关系；detail不生成关系 | 检测后、OCR前预检，超出MODEL_ERROR；不挑选前200对 |
| OCR字段 | 全请求≤500，raw_text≤2000字符、normalized_text≤500字符 | 超出MODEL_ERROR，不截断字符串/数组 |
| DateFact | 先按(bottle,kind)折叠相同日期，冲突归null；总行数≤100 | Worker落facts前检查，超出MODEL_ERROR |
| 规则上下文 | 选定有效规则后构造的(rule_id,subject_ids)总数≤20000 | 评估前预检，超出RULESET_INVALID |
| findings / 完成事件 | true结果≤200，按fingerprint去重，不同rule_id全部保留 | 评估后、成功结果和Outbox提交前检查；超出RULESET_INVALID |

这些容量错误均不可自动重试。同步命令返回422；异步Worker只能在合法lease下将job/run或evaluation记failed及稳定error_code，不将后台422变成后续GET 500。初次inference失败不提交任何新facts/findings；人工事实修订的重评失败保留已提交revision，但不写部分findings。对象半成品按孤儿机制清理。details给出计数/上限及缩小采集范围或调整规则bundle提示，不把partial结果显示为成功。

2模板项×51位置必须在创建前拒绝；101条日期规则×2目标得到202个true时必须在结果事务前拒绝。200个true恰好允许且事件完整携带200个ID。FactsEdit校验关系/日期上限、无序pair唯一及当前run闭包，缺关系按unknown而不是安全。既有100检测/200关系/500OCR/200事件Schema上限不扩大；21个overview瓶即210对，需缩小采集范围。

## 6. 测试与真实发布证据

[synthetic-rule-cases.json](../../contracts/examples/synthetic-rule-cases.json)仅工程模拟：synthetic_a/b不是任何真实化学品类别。包含true/false/unknown、日期前一日/当天/非法日期。验证程序检查全部三值真值表；此结果不证明真实安全规则正确。

真实发布需逐规则positive/negative/boundary专家案例及来源；评审者不得等于提交者。模型数据按scene_id分组划分train/validation/test，同场景不得泄漏；冻结test之后只能一次最终评估，调阈值用validation。报告记录模型、字典、数据、代码hash、硬件、逐类precision/recall、OCR字段准确率、标准化top1/top5、unknown率、质量误拒/漏拒、端到端P50/P95和失败率。

真实阈值、目标召回/准确率和评测样本量目前没有专家/业务签署证据，必须在试点门禁确定并提交报告；工程可按字段和算法开发，不能因此宣称AI业务验收已经通过。
