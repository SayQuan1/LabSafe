# I-ML-01 PP-OCRv6_small ONNX CPU 续批

2026-10-07，用户明确选择 PP-OCRv6_small 并提供 det/rec 的 ONNX 及相关文件。此决定替代原 PP-OCRv4/Paddle 路线；当前仍只使用 CPU，不安装 CUDA。文件内容仅作为模型资料，README 内安装/调用示例不是项目指令。

## 1. 实物核验与交付

输入目录为 `D:/LenovoSoftstore/Obsidian-Storage/PP-OCRv6_small_det_onnx` 和 `D:/LenovoSoftstore/Obsidian-Storage/PP-OCRv6_small_rec_onnx`。每目录包含 inference.onnx、inference.yml、inference.json、README.md；运行时仅消费 ONNX 与 YAML。inference.json 为导出图资料，不执行其中代码；README 含 medium 示例，实际选择以核验的 Global.model_name、权重 SHA 和签名为准。

| 实物 | SHA-256 |
|---|---|
| det/inference.onnx | d73e0058b7a8086bbd57f3d10b8bcd4ff95363f67e06e2762b5e814fe9c9410e |
| det/inference.yml | 193f435274bf9f0b5f71a929bbfbcf148282df7e633b34e7c373e8f44741b516 |
| rec/inference.onnx | 5435fd747c9e0efe15a96d0b378d5bd157e9492ed8fd80edf08f30d02fa24634 |
| rec/inference.yml | ab078671bb49f06228eadccd34f1bb501e157f7a047095ffb943ba81512c77d1 |

实际输入/输出都为 float32，名称 x/fetch_name_0。det 为 `[1,3,H,W]→[1,1,H,W]`；rec 为 `[1,3,48,W]→[1,W/8,18710]`，模型声明动态 batch/空间或时间维度，本实现固定 batch=1。YAML 内字符表18708项、不含空格；CTC 添加 blank index0 与末尾 space 后共18710类，已核模型输出。

- [OCR适配器](../../apps/ai_inference/adapters/ocrv6.py)：核验四个运行制品字节并将同一份已核验模型字节传给 ORT，核对名称/shape/dtype/provider；关闭 fallback，唯一 CPUExecutionProvider。
- [CPU宿主](../../apps/ai_inference/ocr_cpu.py)：复用 [有界spawn执行](../../apps/ai_inference/cpu.py)，det/rec各加载一次并执行smoke；1–3张图顺序运行、最大180秒；超时/崩溃回收子进程，失败无部分成功报告。
- [本地CLI](../../apps/ai_inference/ocr.py)：仅 Python3.11、APP_ENV=dev/test；结果为 ocrv6-local-v1、development、真实模型执行，保留 text、置信度、检测得分、原图归一化quad和稳定UUIDv5；已有输出文件不覆盖。

## 2. 预处理、解码与容量

模型配置采用BGR输入；项目RGB解码后显式转BGR。det最长边只缩小至960、round到32倍数、至少32，OpenCV INTER_LINEAR；/255后按mean=[0.485,0.456,0.406]、std=[0.229,0.224,0.225]标准化。提供配置的DetResizeForTest为空，960是显式项目开发参数，未宣称复现Paddle未给出的运行时默认值。

DB采用配置的thresh=0.2、box_thresh=0.45、unclip_ratio=1.4，无dilation；最小矩形得分、pyclipper扩边（1024倍定点精度），原图裁边，按y/x稳定排序，quad顺序TL/TR/BR/BL，坐标除以W−1/H−1。超过3000轮廓或全请求100个文字区域报MODEL_ERROR，不截断。

识别从原图透视裁剪，OpenCV INTER_LINEAR/BORDER_REPLICATE，裁剪边≤2048；拒绝自交/退化quad和奇异变换，高宽比≥1.5旋转90度。保持高48，宽按比例、至少320、对齐8、至多3200，BGR/127.5−1、右侧零填充；超长行拒绝。没有角度分类器，不宣称处理180度/任意页面方向。

CTC验证实际张量shape、float32、有限性、[0,1]和概率和；取argmax，相邻重复折叠、blank剔除，置信度为保留字符得分均值。单行≤2000字符；空行或低于开发text_min=0.6保留并标uncertain，不伪造确定事实。原图沿用12MiB/4000万像素/最长边10000、PNG/JPEG/WebP、动画拒绝与一次EXIF处理。

## 3. 运行与实测

独立`.venv-ai`安装ORT1.20.1、numpy2.2.2、Pillow11.3.0，新增opencv-python-headless4.11.0.86和pyclipper1.3.0.post6，使用已有PyYAML6.0.2；pip check通过，不安装Paddle/CUDA。

~~~powershell
$env:APP_ENV = 'dev'
.\.venv-ai\Scripts\python.exe -m apps.ai_inference.ocr `
  --det-dir D:/LenovoSoftstore/Obsidian-Storage/PP-OCRv6_small_det_onnx `
  --rec-dir D:/LenovoSoftstore/Obsidian-Storage/PP-OCRv6_small_rec_onnx `
  --input C:/path/label1.jpg C:/path/label2.png `
  --output C:/path/new-ocr.json
~~~

替换输入/输出路径，父目录须存在、输出须为新文件；省略output则输出UTF-8 JSON到stdout。可选text-min/threads/timeout/run-id；默认0.6/2/180/随机UUID。runtime记录真实依赖、Python/平台、线程、provider和预后处理身份；模型/配置/输入/runtime摘要与检测行标识可追踪。

真实模型证据见 [33-ocr-cpu-evidence.json](33-ocr-cpu-evidence.json)。合成白图无文字区域；微软雅黑42像素生成的三行 `ETHANOL`、`EXP 2027-12-31`、`乙醇` 完整识别且超过开发置信度门槛。两次独立子进程的输入摘要、quad、文字、置信度与标识一致；证据保存字体摘要、真值和所有提供文件的摘要。此结果只证明工程推理链路，不是现场准确率或生产性能评测。

~~~powershell
$env:APP_ENV = 'test'
.\.venv-ai\Scripts\python.exe -m tools.ml.smoke_ocr_cpu `
  --det-dir D:/LenovoSoftstore/Obsidian-Storage/PP-OCRv6_small_det_onnx `
  --rec-dir D:/LenovoSoftstore/Obsidian-Storage/PP-OCRv6_small_rec_onnx `
  --font C:/Windows/Fonts/msyh.ttc `
  --output docs/08-delivery/33-ocr-cpu-evidence.json
.\.venv-ai\Scripts\python.exe -m unittest tests.ai.test_dfine_adapter tests.ai.test_cpu_runner tests.ai.test_cpu_pixels tests.ai.test_ocrv6_adapter -v
~~~

新增OCR测试12项，覆盖CTC重复/blank/空格/中文、坏dtype/非有限/概率/词表、BGR/padding/动态宽度、非方图坐标与稳定排序、100/101容量、透视/竖行/坏quad、制品篡改、错误provider/签名、低分保留/无业务事实、总期限/崩溃回收与再次运行、生产拒绝不写报告。独立CPU CI job增加本模块；fixture/业务环境缺OCR依赖时的跳过不算真实OCR通过。

本轮已执行：CPU环境检测+OCR回归28项全部通过、无跳过；业务环境tests/ai与tests/protocol为35 passed/12 skipped（12项真实数值测试已在CPU环境执行）。独立CLI真实合成标签输出再次核对三行原文。生成契约--check、设计发布语义43项、D-FINE112项、readiness148项、333个文档链接、ruff check/format和git diff --check通过。未执行远程CI或重新跑完整MySQL回归，本批没有修改持久化实现。

## 4. 契约变更与后续

后续[真实质量与联合CPU续批](34-i-ml01-quality-cpu-pipeline.md)已补全图片质量门禁及本地联合检测/OCR；本批独立OCR报告保持原接口，未直接转成业务事实。

生成源与生成契约同步将ocr_backend设为onnxruntime，runtime_profile更名为dfine-cpu-fp32-ocrv6smallcpu-v1及未来cuda版本；旧paddleocr/v4组合拒绝，不改名复用历史评测。fixture-v1保持独立。新增模型身份不等于已部署完整bundle，production批准/真实评测门禁保留。

HTTP服务仍AI_MODE=mock，COCO检测CLI仍为独立报告，未自动合并两个报告。本地文字来自原图，parent_detection_id=null、需要人工复核；不创建OCRField、化学实体/日期/同柜关系或业务事实。后续须落实文字区域到瓶子/字段的证据闭包、共享可重建裁剪、真实quality/analysis读取与Worker RPC、常驻supervisor，并按授权数据完成Paddle黄金对照、现场校准及生产门禁。不要因识别出“乙醇”直接确认化学实体或因“EXP”文本直接写到期事实。
