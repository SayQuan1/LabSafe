# 04.2 模型选型、数据准备与适配实施方案

设计决定：2026-09-27，ML-BASE-02。用户已选择D-FINE-N，生产允许GPU；本次固定CUDA为建议生产profile，保留CPU功能profile。本文是编码规范，不是已训练模型、环境兼容证明或上线批准。业务协议仍为vision-v1；未部署的旧模型契约由本次修订替代，不支持把旧清单改名后继续使用。

## 1. 固定路线与变更边界

| 能力   | 本期决定                                           | 不允许的替代                                  |
| ---- | ---------------------------------------------- | --------------------------------------- |
| 检测   | D-FINE-N，四类微调，640×640，300 queries              | 把COCO权重直接当四类模型；未经确认换S或扩大输入              |
| 适配器  | dfine-n4-rgb-stretch-v1；RGB、直接resize、qmax、无NMS | 旧BGR/0–255/letterbox适配器；混用上游两个推理示例      |
| 生产建议 | ONNX Runtime CUDA、FP32、batch=1、并发1             | 默认TensorRT/FP16、跨主机GPU调度或静默CPU fallback |
| OCR  | 保留PaddleOCR2.10.0、PP-OCRv4及CPU                 | 本次不隐含批准PP-OCRv6、OCR上GPU或大模型             |
| 开发   | fixture-v1联调；真实CPU profile做功能/导出检查             | 把mock、CPU结果当CUDA生产性能证明                  |
| 业务   | AI只出事实；Worker写证据并执行确定性规则；仍需人工复核                | 用模型置信度代替安全判断                            |

上游仓库为Peterande/D-FINE，固定提交956d1709314c2c6a4df6f34de232054578a7449f；模型结构从该提交configs/dfine/dfine_hgnetv2_n_coco.yml及其include展开。source_ref固定为https://github.com/Peterande/D-FINE，source_commit为上述40位提交；不在构建时追踪master。项目包装器和安全加载改动由项目git_commit及补丁摘要追踪，不伪装成上游原样实现。

初始化只选官方dfine_n_coco.pth，获取地址见第10节。只使用COCO版，不擅自替换obj365/obj2coco。代码LICENSE、具体权重/数据条件和现场图授权分别核查；Apache-2.0不等于所有制品无条件使用。真实权重SHA在下载核验阶段记录，不预填。

## 2. 运行时和版本约束

依据[ADR-PY-01](../02-architecture/07-python-runtime.md)，业务API/Worker、AI和离线训练统一Python3.11；各服务依赖环境仍独立，真实训练/推理以Linux x86_64为目标。Windows开发通过相同Linux容器联调。训练框架不进入业务API，也不要求训练与推理安装在同一环境。

| 环境 | 顶层约束 |
|---|---|
| AI CPU | onnxruntime1.20.1、paddleocr2.10.0、paddlepaddle2.6.2、numpy1.26.4 |
| AI CUDA | 以onnxruntime-gpu1.20.1替换CPU包；CUDA12.6系列、cuDNN9系列；OCR仍为CPU Paddle |
| 训练/导出 | PyTorch2.8.0、torchvision0.23.0、官方cu126训练构建、ONNX1.17.0、numpy1.26.4、固定上游提交 |
| 图像 | Pillow11.3.0用于检测resize；OpenCV4.10.0.84用于共享perspective-rgb-v1；Worker与AI共享裁剪实现 |

这些是明确的顶层候选约束，不是已验证安装锁。I-ML-01在空环境解析并冻结全部传递依赖、wheel hash、CUDA/cuDNN具体patch、驱动要求、镜像digest和SBOM；记录构建/离线import/smoke日志。发现冲突或不可接受漏洞，先提交版本修订ADR与回归，不让实现者静默替换。PaddleOCR涉及opencv-python/contrib，固定同版本并验证实际cv2，不再混装不同版本headless。GPU依赖由镜像提供；不使用ORT1.20未提供的preload_dlls接口。

| runtime_profile | device_profiles | detector_device | ocr_device | precision |
|---|---|---|---|---|
| dfine-cpu-fp32-ocrv4cpu-v1 | [cpu] | cpu | cpu | fp32 |
| dfine-cuda-fp32-ocrv4cpu-v1 | [cuda] | cuda | cpu | fp32 |
| fixture-v1 | 测试部署声明的cpu/cuda路由 | mock | mock | mock |

真实runtime对象同时固定preprocess_id=dfine-rgb-stretch640-v1、postprocess_id=dfine-qmax-v1、onnx_opset=16和runtime_lock_sha256；fixture分别为fixture-v1、fixture-v1、0和测试锁文件真实摘要。一个真实bundle只声明一个设备profile；CPU/CUDA分别打包、评测和批准，不共用报告。CPU生产激活仍需独立通过cpu-functional policy；CUDA可用不自动取消CPU功能基线。

runtime-lock.json以规范JSON记录profile、两阶段设备、精度、预后处理、opset、CPU线程设置、Python完整版本、所有包版本/wheel hash和容器用户态CUDA/cuDNN版本；训练包另有独立lock。镜像digest、GPU型号/显存和宿主驱动放外部评测环境快照，避免镜像包含自身digest的循环。启动时核对锁文件hash及已安装版本/设备，不只信清单字段。

## 3. 数据和类别映射

### 3.1 采集、标注与评测数据

仅使用授权现场/历史照片；记录authorization_ref、来源实验室、scene_id、时间、脱敏、SHA256及撤回规则。无授权时仅用合成fixture开发；真实图、权重不入Git。

| category_id | class_id | 类型 | 标注边界 |
|---|---|---|---|
| 1 | 0 | bottle | 可见瓶体和瓶盖外接框；遮挡只标可见部分 |
| 2 | 1 | label | 可见标签外接框；无法可靠判定的实例单独标ignore供裁决 |
| 3 | 2 | shelf | 单个连续储物层/格内边界，不把整柜当一层 |
| 4 | 3 | cabinet | 可见柜体外边界，可包含多个shelf |

授权标注原件保持COCO category_id=1..4、bbox像素xywh、area=w*h、iscrowd=0。负样本annotation为空；不增加背景类。争议标注隔离在sidecar、裁决后才进入训练/评测；未裁决实例所在图不作为无目标负样本，报告排除数量/理由。sidecar还保存目标关系、OCR字段/可读性、实体和日期真值及审核来源，不作为推理补全输入。

按scene_id分组、seed=20260927划分70%/15%/15% train/validation/test。同次摆放的总览/细节及近重复图同组，增广后不得跨组；冻结split-manifest，训练不读test。起步预算至少1000授权图、100scene、每类300实例，困难/无目标/非目标图至少20%；这不是充分性或达标保证，验收矩阵的独立样本下限仍须满足。先标100图形成手册，第二人复核至少20%及所有争议，第三人或业务专家裁决。

### 3.2 LabSafeCOCODetection与评测桥接

在项目训练包装器注册LabSafeCOCODetection，沿用上游图像/框变换，在向模型交付target前逐项把labels按{1:0,2:1,3:2,4:3}映射成int64。num_classes=4、remap_mscoco_category=false；禁止仅关闭映射后直接把原始1..4传给四分类头。验证标注categories集合、每个annotation类别、正面积及框边界，未知类别立即报错。

COCO GT对象保持1..4不变；项目评测包装器对预测class_id反向映射0..3→1..4后交给COCO evaluator。给业务API的类别始终为bottle/label/shelf/cabinet，不进行反向映射。训练类别映射与COCO评测反向映射均有[纯语义参考](../../tools/design/dfine_reference.py)及合成往返测试。

## 4. 离线训练的首轮配方

项目训练入口包装固定上游实现，不增加在线训练API。创建LabSafeCOCODetection、LabSafePostProcessor和安全checkpoint加载钩子；结构/criterion继承固定N配置，项目覆盖项如下。未列出的网络参数和损失权重从固定提交展开，保存最终resolved-config及SHA，不从远程最新配置补缺值。

| 项目 | 固定初值和行为 |
|---|---|
| 网络 | num_classes=4、num_queries=300、eval_spatial_size=[640,640]、remap_mscoco_category=false；N型HGNetv2/encoder/decoder结构不变 |
| 初始化 | HGNetv2.pretrained=false；显式加载本地已核验dfine_n_coco.pth，不在构造模型时另行联网下载backbone |
| 优化器 | AdamW；非backbone LR=0.00025，backbone LR=0.000025；betas=[0.9,0.999]；weight_decay=0.000125；所有bias、归一化参数weight_decay=0 |
| 参数组 | 每个requires_grad参数恰好进入一组；backbone前缀判别学习率、模块类型/参数名判别bias/norm；组间重复或漏参数即失败 |
| 轮次 | seed=20260927，100 epochs；epoch采用0..99；每轮验证；不根据test选超参 |
| 学习率 | 前500个optimizer更新线性warmup：第s次使用base_lr×min(1,(s+1)/500)；完成80、95轮后各乘0.1；不沿用旧SGD/每图LR公式 |
| 批量 | 单GPU起始物理batch=8、有效batch=8；OOM从原始初始化重新开始，物理batch按8→4→2→1递减，通过梯度累积维持有效batch，不降低640输入 |
| 尾批/梯度 | 训练drop_last=true；轮末不足完整累积组时按实际微批数k平均loss后更新；clip_grad_norm=0.1；scheduler warmup/EMA只在optimizer.step后更新 |
| 精度/并行 | use_amp=false、sync_bn=false、单训练进程；固定随机种子并记录确定性设置；不承诺不同GPU训练位级一致 |
| EMA | use_ema=true；沿用固定提交ModelEMA，decay=0.9999、warmups=1000、start=0；从完成部分加载后的模型初始化EMA |
| 增广 | RandomPhotometricDistort p=0.5、RandomHorizontalFlip p=0.5、Resize[640,640]、SanitizeBoundingBoxes min_size=1、ConvertPILImage float32 scale=true、ConvertBoxes cxcywh normalize=true |
| 验证/拼批 | 验证仅Resize与ConvertPILImage；项目collate直接stack固定尺寸图、保留targets列表；不使用上游多尺度collate、IoU随机裁切、ZoomOut、MixUp或Mosaic |
| 其他 | 初始num_workers=4；无wandb/外部遥测；记录每轮loss、学习率、各类mAP/P/R和失败信息 |

上述是项目首轮工程配方，不声称最优或保证收敛。物理batch=1仍OOM则停止并更换已批准训练资源；修改配方需新训练配置摘要与验证，不改测试集。训练用验证mAP50-95选最佳EMA checkpoint；平分保留较早epoch。mAP诊断使用qmax后的全部300候选、类别反向映射；业务检测阈值/100结果上限在后续完整流水线校准及验收执行，不能用诊断mAP冒充业务通过。

### 安全加载和分类头初始化

只能加载白名单来源和实际hash核验后的本地checkpoint，torch.load明确weights_only=true、map_location=cpu；优先取ema.module，否则取model。无这两种结构或包含非Tensor模型参数即失败。不得调用上游Objects365↔COCO类别搬运逻辑来初始化项目四类头。

按同名且shape一致复制参数；decoder.denoising_class_embed、decoder.enc_score_head、decoder.dec_score_head.*等类别相关参数在四类网络中重新初始化，不按前4行截取COCO类别。导出matched/missing/mismatched/unexpected清单；除这些明确的分类参数外存在未加载网络参数即停止，调查后以ADR增加精确允许项，不能用strict=false掩盖任意缺失。安全加载失败时走隔离转换审查，不静默设置weights_only=false。断点续训只接受同source/config/dataset/split哈希、同四类结构的项目checkpoint，恢复优化器/EMA/epoch；微调初始化不恢复旧COCO优化器。

## 5. dfine-n4-rgb-stretch-v1适配器

### 5.1 图像输入

1. 输入为经过现有流程保存的RGB8分析图；W/H取解码图实际宽高，均为正整数。
2. Pillow Image.fromarray(rgb).resize((640,640), resample=Image.Resampling.BILINEAR)，直接拉伸，无padding，无EXIF二次旋转。
3. 结果按RGB顺序转换numpy float32后除float32(255)，转CHW、增加batch轴、保证连续内存。无BGR转换、无均值/方差标准化。
4. ONNX输入images=float32[1,3,640,640]；orig_target_sizes=int64[1,2]，值为[[W,H]]，不是[[H,W]]、不是[[640,640]]。

训练/验证的PIL Resize与线上Pillow版本、插值必须相同。选择直接resize是为了统一固定提交的验证路线；不复制上游onnx_inf.py中的letterbox。OCR裁剪始终从原始分析图恢复，不能从拉伸后的640图生成证据。

### 5.2 项目导出包装器

加载四类项目checkpoint并model.eval；使用cfg.model.deploy()得到部署模型。包装器读取pred_logits[1,300,4]和pred_boxes[1,300,4]（归一化cxcywh）。不使用上游flatten(class×query) TopK后处理。

- 对每query先在原始logits上求最大值和class_id；严格相等取较小class_id。confidence=sigmoid(max_logit)，不是objectness乘概率，也不是softmax。
- 每个query只输出一个类别；不做TopK重排、不做NMS；保留300个query原始顺序，避免同query多个类别以及TopK并列不稳定。
- 将cxcywh转换成xyxy，逐坐标乘[W,H,W,H]。不提前裁边/阈值过滤。
- 包装器对每query检查原始logits/box有限及w/h>0；无效query把返回score置NaN，线上将其判为MODEL_ERROR。不能因无效值低于阈值而藏起来。
- 输出labels=int64[1,300]、boxes=float32[1,300,4]（原图像素xyxy）、scores=float32[1,300]。

这是项目LabSafePostProcessor的明确语义，不声称与上游默认TopK完全相同。训练验证、PyTorch参考和ONNX导出共用该定义；语义参考见dfine_reference.py，实际向量化版本在I-ML-01实现。

使用公开torch.onnx.export(dynamo=false)、opset_version=16、batch固定1，无dynamic_axes，不启用simplifier/FP16/量化；示例输入两项batch均为1，不沿用上游32张图/1组尺寸的示例。无checkpoint立即失败，禁止导出未加载预训练参数的随机网络。I-ML-01允许先保存COCO匹配参数+未训四类头的初始化checkpoint，必须标development，仅用于导出/接口原型，不冒充完成微调。运行onnx.checker并校验输入/输出名称、dtype、shape与opset；未知张量签名SCHEMA_MISMATCH，不靠猜下标读取。

### 5.3 线上过滤、几何和数量限制

在过滤前校验全部输出类别0..3、分数有限且0..1、框有限且x2>x1/y2>y1；数值损坏MODEL_ERROR，不返回空成功。score<detection_min剔除，恰等保留。框裁到[0,W]×[0,H]，裁后空框丢弃，再除[W,H,W,H]得到API归一化xyxy。不得再次执行letterbox逆变换或乘原图尺寸。

每图按(y1,x1,y2,x2,class_id,confidence,query_index)升序稳定排序，生成现有UUIDv5 run/image/type/ordinal标识；query_index只是内部并列决胜字段，不新增API字段。不同query即使重叠也不默认合并；不同类型嵌套框不能互相NMS抑制。重复检测由四类训练和完整关联/规则验收约束，不能临时加入未版本化去重。

按image ordinal处理1–3图；所有图经过阈值/裁边后的检测总数>100时HTTP500 MODEL_ERROR、不可自动重试，details说明缩小采集范围；不截断为前100，也不只保留label。用户创建范围更小的新巡检项，不能绕过现有失败项状态机。300是网络候选数，不是业务允许的最终结果数。

label crop quad仍为矩形框TL/TR/BR/BL；宽高按原图框差向上取整，限制1..2048；继续使用perspective-rgb-v1，不宣称矩形检测提供真实透视四角。下游label→bottle、bottle→容器关联和人工复核规则保持不变并回归。

### 5.4 导出一致性门禁

固定50张授权/脱敏图，覆盖横图、竖图、边缘框、小标签、密集/空目标；同一PNG由同一预处理实现生成输入。分别运行PyTorch FP32、ORT CPU FP32和目标ORT CUDA FP32的完整300候选。要求各候选归一化box和score最大绝对差≤1e-3；实际阈值过滤后query/class集合、数量和业务排序必须一致，超差或阈值翻转即停止发布并调查。不得ROUND输出、提高容差或调测试阈值掩盖差异；变更容差须ADR和误差影响验证。

纯参考测试仅证明映射/qmax/几何/边界语义，不证明Pillow像素一致、真实权重或CUDA算子已验证。参考只接Python原生数值；真实numpy输出先验证dtype再tolist对照，不能先int强转来掩盖错误的类别张量。I-ML-01先做合成原型smoke；本节正式50图门禁在I-ML-02/03取得数据和训练后权重之后、I-ML-04执行，避免“先有训练结果才能开始导出原型”的循环。模型每次训练、后处理或运行环境变化均重新执行此门禁。

## 6. OCR保持原选型

固定资产为ch_PP-OCRv4_det_infer、ch_PP-OCRv4_rec_infer、ch_ppocr_mobile_v2.0_cls_infer、ppocr_keys_v1.txt。显式构造ocr_version=PP-OCRv4、lang=ch、use_gpu=false、use_angle_cls=true，并传det/rec/cls/字符表本地路径。启动核验每个文件；库即使关闭角度功能也可能检查cls，因此不省略资产。缺失返回MODEL_VERSION_UNAVAILABLE/not_ready，不允许运行时下载。

每个label原图crop由RGB转BGR后调用ocr(det=true,rec=true,cls=true)；None/空列表产生未知事实，不伪造识别值。逐项检查返回shape，按quad最小y、最小x排序，保留原文和confidence；未知shape为SCHEMA_MISMATCH。日期解析、实体匹配和证据关联仍遵守[事实与规则](01-data-and-rules.md)。ORT与Paddle CPU线程各固定2，MKL/OMP线程各1；单子进程内顺序运行检测与OCR，禁止每个crop创建OCR实例。CPU预算不足只允许经容量评测调整并发布新runtime lock。

OCR升级或GPU化是独立变更：先用分阶段耗时证明需要，再选择具体模型/运行时、重做依赖和联合显存验证；不能在本次D-FINE清单中暗换模型。

## 7. 制品、hash与发布门禁

ModelManifest保留detector/ocr/quality/dictionary四个角色，每角色恰一份。各角色object_key指向版本化不可变制品，其sha256对下载原始字节计算。detector使用受控tar包，包含detector.onnx、adapter.json、source.json、runtime-lock.json、files.sha256.json及LICENSE/NOTICE；不打包训练checkpoint。adapter.json存输入输出签名、类别双向映射、resize、qmax和容量规则；source.json存上游提交、项目git_commit、训练配置/数据split摘要及初始化权重实际hash。files.sha256.json列出除自身外所有文件的实际hash，外层tar哈希覆盖索引本身。

OCR角色为模型/cls/keys及其文件索引包；quality角色为算法和图像依赖配置；dictionary角色为冻结词典快照。解包拒绝绝对路径、..、符号/硬链接、重复路径；每包最多4096文件、累计解压最多1GiB，越界MODEL_VERSION_UNAVAILABLE。校验完整文件清单后原子放入只读模型目录，不从归档执行代码。runtime_lock_sha256必须等于detector包中runtime-lock.json实际字节摘要，内容还须与runtime对象和当前环境匹配；检查字段非空不等于校验制品。

content_sha256对下列16项按共享规范JSON计算：artifacts、pipeline_version、dictionary_version_id、dictionary_sha256、thresholds、input_max_side、adapter_id、model_family、source_ref、source_commit、git_commit、detector_backend、ocr_backend、device_profiles、runtime_profile、runtime。评测绑定content_sha256；批准台账绑定完整manifest/report/policy哈希。runtime lock或代码/预后处理变动必须产生新评测内容，不能沿用旧CUDA报告。校准报告为独立sidecar，真实发布从受控制品登记取出、核其report/split摘要，不增加第五个artifact角色。

开发联调初值保留detection_min=0.25、ocr_min=0.60、entity_min=0.90、blur_min=80、dark_min=0.12、glare_max=0.30、adjacent_gap_ratio=0.25。0.25只是新模型dev起点，并非原检测器分数等价；仅validation依[校准策略](../07-quality-operations/04-acceptance-policy.md)选择正式阈值，失败则calibration_status=failed。

APP_ENV=dev/test允许purpose=development的fixture-v1和真实开发bundle；production仍拒绝mock、未校准、未评测或无独立批准。fixture用真实测试文件hash，不伪造权重；独立开发数据库/对象前缀，页面标is_simulated。当前设计只接受dfine_n/新adapter或fixture，旧模型字段明确拒绝；本仓库无既有应用部署，不替任何历史生产run改模型。若外部已有旧消费者，先另行盘点迁移，不能直接覆盖。

## 8. CUDA启动与恢复约束

CUDA profile显式构造ORT会话，要求CUDAExecutionProvider可用且排首位，device_id=0为容器中暴露的单GPU；启动必须实际执行模型smoke。session.disable_fallback()禁止执行失败后整次改用CPU重跑；CPU EP只能承担经过配置审核、记录在runtime lock中的辅助算子。通过ORT profiling记录节点实际provider，模型主要计算落在CPU、未知CPU节点或GPU无效均not_ready。不把get_available_providers含CUDA当作已证明GPU推理。CPU profile只配置CPU EP。

FP32基线禁用TF32优化（ORT CUDA provider use_tf32=0），不自动启用FP16、TensorRT或量化。ORT会话、OCR实例各加载一次；supervisor不初始化CUDA，只由spawn计算子进程初始化。GPU OOM/计算超时终止并join子进程、清理IPC，再重载全部模型和smoke；坏CUDA上下文不复用。不因图小/负载高而修改device_profile；切换只通过新activation影响新run。

保留ready前120秒启动期限、MAX_INFLIGHT=1、CUDA runs内部45秒/HTTP55秒和Worker/Celery预算；具体时间是保护边界，不是D-FINE成绩。加载超时/连续失败按既有not_ready锁定规则处理。部署、GPU容器预留、显存观测及排空升级见[部署规范](../07-quality-operations/02-deployment-operations.md)。P95目标仍包含队列、全部OCR和规则，不能用检测器单张GPU毫秒数替代。

## 9. 编码任务及完成标准

以下是未来实现交付，不表示apps或模型文件已存在。纯参考代码和合成测试属于设计工具。

| 任务 | 必须实现/交付 | 失败处理 |
|---|---|---|
| I-ML-01 | 固定提交和项目补丁；训练/CPU/CUDA lock；LabSafeCOCODetection/评测反向映射、安全加载、LabSafePostProcessor、初始化开发checkpoint、固定ONNX导出和合成CPU/CUDA smoke | 不兼容形成有证据ADR；初始化权重仅development，不冒充训练后制品 |
| I-ML-02 | 数据授权/标注手册/scene划分/争议裁决/冻结split | 不足继续采集，无授权只用fixture联调 |
| I-ML-03 | 四类微调、最佳EMA、resolved-config、validation阈值、受控制品包 | 目标不达标继续分析/训练，不读取test挑结果 |
| I-ML-04 | 训练后50图PyTorch/ORT CPU/CUDA对照、既有RPC、多图与100上限、原图裁剪/OCR/关系/规则；GPU缺失/OOM/超时/重载/断网验证 | 失败不写部分事实、不静默降级；报告分阶段延迟和资源 |
| R-ML-01 | 冻结test实际报告、许可/SBOM扫描、目标设备环境快照、独立批准台账 | 缺证据禁止生产，不改变proposed状态冒充批准 |

预期模块职责：训练目录实现数据/加载/配置/导出；apps/ai_inference/adapters实现预处理/ORT/输出校验；进程宿主负责期限与设备；共享包实现裁剪和规范hash；Worker业务逻辑不引入模型库。新增AI-05至AI-09案例在[测试目录](../07-quality-operations/01-testing-evaluation.md)定义。检测/分类不足先检查标注、原图质量和细节图，不自动扩大模型、切块或加入大模型。

## 10. 官方来源与核验边界

2026-09-27只读核对上游HEAD为上述提交；实际检查N配置、include的模型/优化器/数据增强、postprocessor.py、export_onnx.py和安全加载实现。上游默认TopK、动态batch示例及COCO/Objects365头映射不直接作为本项目实现。本设计的qmax和训练配方是明确的项目决定，真实兼容性由I阶段验证。

~~~text
源仓库与提交：https://github.com/Peterande/D-FINE/tree/956d1709314c2c6a4df6f34de232054578a7449f
初始化权重：https://github.com/Peterande/storage/releases/download/dfinev1.0/dfine_n_coco.pth
模型配置：configs/dfine/dfine_hgnetv2_n_coco.yml
验证预处理：configs/dfine/include/dataloader.yml
类别加载：src/data/dataset/coco_dataset.py
默认后处理：src/zoo/dfine/postprocessor.py
导出入口：tools/deployment/export_onnx.py
checkpoint实现：src/solver/_solver.py
ORT CUDA兼容性：https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html
ORT执行与fallback：https://onnxruntime.ai/docs/api/python/api_summary.html
PaddleOCR2.10：https://www.paddleocr.ai/v2.10.0/en/ppocr/quick_start.html
Pillow11.3.0：https://pypi.org/project/pillow/11.3.0/
PyTorch版本配对：https://pytorch.org/get-started/previous-versions/
~~~

本轮没有下载实际权重、训练、安装GPU环境或生成真实性能成绩。源码存在、合成测试通过、设计可编码与环境可运行/模型达标/获准上线分别判断。
