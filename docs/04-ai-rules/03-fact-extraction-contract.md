# 04.3 确定性质量、OCR字段与关系提取

决定：IRR-06，2026-09-27。补齐ML-BASE-02现有流程，不改变模型或业务能力。所有算法共享版本和黄金样例；参考为[readiness_reference.py](../../tools/design/readiness_reference.py)，测试为[test_readiness_design.py](../../tools/design/test_readiness_design.py)。这两个文件不是应用实现，纯数值测试不证明真实Pillow/OpenCV/Paddle输出已验证。

2026-10-09实施更新：用户固定官方COCO80及PP-OCRv6_small ONNX CPU；第34–40批接通质量/检测/OCR、独立文字证据、常驻HTTP/Worker、真实bottle关联和字段。[第41批](../08-delivery/41-i-ml01-chemical-candidates.md)实现冻结开发词典、名称候选及全部字段共识；日期聚合/事实快照仍待接线。字段和候选仅供复核，不等于已确定安全事实。

## 1. quality-rgb-lap1-v1

1. 输入已按既定流程解码、应用EXIF且去元数据的RGB8分析图；不再旋转或转BGR。
2. M=max(W,H)。M≤1024不resize；否则w=max(1,(W×1024+floor(M/2))//M)，h同理。Pillow11.3.0、Image.Resampling.BILINEAR、无padding，不放大小图。
3. 灰度逐像素计算Y=(77R+150G+29B+128)//256。先转uint32防溢出，结果uint8；不使用另一套库默认灰度系数。
4. Laplacian核为[[0,1,0],[1,-4,1],[0,1,0]]。输入转float64，边界REFLECT_101；宽/高为1时该轴重复唯一像素。等价OpenCV的Laplacian(ddepth=CV_64F,ksize=1,scale=1,delta=0,borderType=BORDER_REFLECT_101)，不得用CV_8U截负值。本批NumPy实现已与实际OpenCV4.11独立核对照，包括边界和单像素轴；quality执行本身不依赖OpenCV。
5. blur_score=所有Laplacian像素的总体方差（ddof=0）；brightness=mean(Y)/255；glare_ratio=count(Y≥250)/(w×h)。float64累计，禁止先ROUND。
6. blur_score<blur_min、brightness<dark_min、glare_ratio>glare_max分别加blur/dark/glare；恰等通过。多原因按上述顺序，任一原因即needs_retake。

quality artifact记录algorithm_id、上述resize/灰度/核/边界/dtype以及依赖。配置必须与该版本完全一致；修改需新quality artifact和评测内容hash。quality/runs共用函数及输入字节。黄金样例包括黑/白常量、红色单像素(灰度77)、单行[0,255]（Laplacian为[510,-510]、方差260100）、长边1025取整及三个阈值恰等。

## 2. 容器、标签和邻接

当前COCO80没有label/shelf/cabinet。第39批以独立文字quad替代原label候选输入，只关联真实同图COCO bottle（class 39）；不伪造缺失检测。算法text-bottle-quad80-v1：quad四点在[0,1]、严格凸、图像坐标顺时针且正面积；中心为四顶点算术平均，位于瓶框闭区间内，实际quad/瓶矩形交面积≥0.8×quad面积。按四边矩形裁剪计算实际面积，不用文字外接矩形代替倾斜quad；double无取整/epsilon，0.8恰等接受。候选取瓶框面积唯一最小者，同面积并列返回NULL，不按ID/置信度破平局。无目标或跨图均NULL；所有原文/crop保留，line与crop关联一致。AI与Worker共享同函数重算，包括NULL；容量≤100文字/100检测，坏几何失败不部分成功。

以下容器/邻接为后续能力规范，当前pipeline未实现。缺少真实shelf/cabinet时bottle.parent_detection_id=NULL，不能用item/location或其他COCO家具类别冒充容器。将来具备可信容器检测才按同图、包含瓶中心、面积最小/并列未知规则关联；本批不产生关系事实。

overview全部bottle按detection_id排序生成i<j无序对，先检查200对上限；detail不参与。双方有相同已知parent时same_location=true；已知不同parent为false；任何parent未知为null。非true时relation=unknown、confidence=0；不存在的pair在规则适配时为unknown，不当not_adjacent。

同容器时：gap=max(0,max(x1a,x1b)-min(x2a,x2b))；horizontal_ratio=gap/max(wa,wb)；vertical_ratio=max(0,min(y2a,y2b)-max(y1a,y1b))/min(ha,hb)。horizontal_ratio≤阈值且vertical_ratio≥0.5为adjacent，否则not_adjacent；恰等保留。confidence=min(两个bottle confidence)，只是几何关联分数，不是安全概率。检测器没有遮挡分类头，不从框重叠猜遮挡。重复query不自动合并。

## 3. OCR行的输入与排序

独立文字区域通过PP-OCRv6_small ONNX CPU得到(text,confidence,quad)，按(min_y,min_x,原返回索引)稳定排序，shape/有限数值/容量先核验。第35–37批可重建crop/line ID与三个摘要保留，第39批在稳定ID生成后计算瓶子关联；不合并、删行或修改原文/置信度/quad/像素。低置信关联仍保留uncertain，不代表可信名称/日期。独立OCR CLI没有D-FINE输入，仍保留NULL关联；第40批联合CPU报告为v4，记录text_association及ocr_fields算法版本。正式TextRegion/CropRecipe可指真实bottle或NULL，不能把文字字框当作label检测。

匹配文本为NFKC+连续空白压缩+trim；英文关键词不区分大小写。raw_text保留原字符串，跨行用单个LF连接，不改OCR原文。normalized_text另按规则生成。所有字符长度按Unicode字符数，不按UTF-8字节。超过现有字段容量立即MODEL_ERROR，不截断后尝试匹配。

## 4. ocr-fields-v1词法和配对

仅在一行开头识别以下前缀，可跟中文/英文冒号、空白或紧接日期/中文值；英文前缀后不能紧接英文字母。按前缀最长优先。固定关键词表随quality artifact的fact_extraction配置发布，不从在线服务补词。

| 字段 | 关键词 | normalized_text |
|---|---|---|
| name | 化学品名称、名称、品名、NAME、CAS | 名称：NFKC/空白压缩；CAS：完整数字-数字-数字格式且校验位通过 |
| expiry | 有效期、失效、EXP | 严格完整日期→YYYY-MM-DD，否则null |
| production | 生产日期、生产、MFG | 同上；不改成expiry |
| opened | 开封日期、开封 | 同上 |
| concentration | 浓度、含量、CONCENTRATION | 规范文本，不换算单位，不作安全规则输入 |
| hazard_mark | 危险标识、危险性、HAZARD | 规范文本，不从图标猜危险类别 |

配对从上到下消费行：

1. 前缀后有值：只使用本行；不得自动拼接后一行名称或日期。
2. 前缀单独成行：最多消费紧邻一行作为值，但下一行不得有任何已定义前缀或多个日期关键词。两行confidence取min，raw_text为两行LF连接；不跨过空白/其他行向下寻找。多日期歧义行独立输出date_unknown，不能被上一行名称前缀消费。
3. 同行含两个或以上日期关键词，不猜绑定，整行输出date_unknown、normalized_text=null；未配对的日期关键词输出其已知kind且值null。
4. 无前缀整行恰为冻结词典canonical_name/alias时可输出name；或整行恰为校验位有效CAS时输出name。其他无前缀文本不作名称模糊匹配，避免地址/浓度被猜成化学品。名称只能对明确name字段继续运行既定词典resolve。
5. 无前缀的整行日期/含日期但无类型线索输出date_unknown/null；非法CAS、日期、无法识别的标签均不制造resolved事实。未生成name或expiry通过事实完整性检查进入unknown/needs_review。

日期值只接受整个值匹配YYYY-MM-DD、YYYY/MM/DD或YYYY年MM月DD日，月日各两位且公历合法。例如仅2027-03、2027-02-30、10/11/2027均不解析。前缀值为空时输出null；日期字段低于ocr_min也不得进入确定DateFact。CAS校验位：去连字符后的最后一位等于前面数字从右到左乘1..n求和mod10；格式2–7位数字、2位数字、1位数字。

## 5. 字段聚合与事实

第40批OCRField以真实关联bottle为detection_id，crop/line证据来自其独立文字区域，不要求不存在的label类别。跨行只能同图、同唯一bottle且全局紧邻，不先删未关联/其他瓶行再拼接。source_lines必填1–2项，每项line_id/crop_id/raw_text/confidence；首行crop_id不代表全部来源。Worker重算全量有序字段，遗漏/伪值/缺证据整批拒绝。无唯一bottle的文字/crop保留人工参考，不参与规则。同瓶多行同kind字段保持顺序和冲突，低置信日期可保留规范文本供复核，但不产生确定DateFact；仍needs_review。

对于同bottle的全部name字段，逐字段按CAS→alias→fuzzy产生候选。仅当每个字段都唯一最佳、字段OCR confidence≥ocr_min、最佳score≥entity_min，且全部最佳entity_id相同，才resolved。任一冲突/缺失保持candidate或unknown；候选合并按entity_id去重，每个字段都参与计分（该字段没有此候选记0），score取所有字段分数的min，再score降序/entity_id升序取top5。这样resolved实体仍在合并排序首位。已知冲突不得因top5截去另一名称而变resolved。没有任何候选为unknown。名称OCR精确匹配评测使用字段规范化文本，不把CAS伪装成名称成绩。

第41批chemical-candidates-v1将上述候选/共识接入正式CPU/Worker，字节≤64KiB、≤100条开发词典，无安全分类属性；重复ID/同条目规范化名称、坏CAS、空来源/名称拒绝，跨条目别名/CAS歧义保留。有效CAS token只作CAS精确检索，未命中unknown，避免相似CAS猜测；普通名称优先canonical/alias精确，再正分规范Levenshtein候选。全批距离矩阵单元≤2,000,000，超限MODEL_ERROR，不能丢条目。每个候选match_method为全部实际命中的最弱方法，所有名称来源仍保留在ocr_fields；无name字段不造EntityCandidates。extraction_context绑定原始bundle/dictionary SHA和阈值，Worker重算完整有序结果，非空entities无快照拒绝；仍needs_review，不建立FactRevision。日期聚合以下为第42批待实现规范。

DateFact按(bottle,kind)聚合：所有同kind字段均可解析、达到ocr_min且日期相同才取该日期；否则该kind.value=null。未知类型为kind=unknown/value=null；同键只保留一条，evidence去重并全部保留，最多10条，超出MODEL_ERROR。缺少某kind不补伪日期，当前适用规则需要但缺失时evaluation记录insufficient_facts。name低置信与date冲突可由人工事实修订解决；重评后重新计算U。

每字段confidence取实际参与行的min，不平均、不默认1。原文标点保留在raw_text，生成OCRField时normalized_text不作化学推断。所有ID来自既定run/image/bottle/line/crop闭包；字段数、长度、DateFact数量及证据数量在Worker提交前再次校验。图像质量/规则上下文的所有不确定性最终受领域完成守卫约束。

## 6. 必须落地的黄金测试

| 输入 | 预期 |
|---|---|
| NAME（0.9）下一行Ethanol（0.6） | name=Ethanol，confidence=0.6，raw两行原文 |
| 有效期 下一行 2027/03/01 | expiry=2027-03-01，不拿生产日替代 |
| 有效期 下一行 生产日期:2026-03-01 | expiry=null；下一行独立production |
| 生产日期:2026-03-01 有效期:2027-03-01 | date_unknown/null，不猜两日期配对 |
| 两个expiry为2027-03-01及2027-04-01 | 两条OCR保留；一条expiry DateFact.value=null |
| 名称:甲与名称:乙指向不同entity | candidate，不能用较高分强行resolved |
| 2027-03 / 2027-02-30 / 10/11/2027 | 不产生确定expiry |
| 文字quad有两个同面积最小候选瓶框 | detection_id=null，原文/crop保留人工复核 |
| 高度0.4与0.2的瓶竖直交长0.1 | vertical_ratio=0.5；其他条件满足则adjacent |

纯参考测试覆盖标量/词法/几何与Schema边界。真实Pillow resize、OpenCV核等价、Paddle行结构、CPU/CUDA联合效果仍由AI-10及I-ML任务执行，不以合成通过宣称真实识别达标。
