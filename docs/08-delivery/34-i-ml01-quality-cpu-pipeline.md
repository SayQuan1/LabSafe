# I-ML-01 真实质量门禁与联合 CPU 流水线续批

2026-10-07：在已交付的 COCO80 检测、PP-OCRv6_small ONNX 文字识别基础上，实现真实质量算法并串联单次 CPU 流水线。当前仍仅 dev/test、本地原图输入，不安装 CUDA。

## 1. 本批交付

- [质量算法](../../apps/ai_inference/adapters/quality.py)：真实 Pillow/NumPy 像素实现 quality-rgb-lap1-v1，不复用假标量分数。按文档整数取整、长边最多1024、Pillow BILINEAR、不放大小图；uint32灰度 `(77R+150G+29B+128)//256`；float64四邻域Laplacian、REFLECT_101、单像素轴重复，总体方差ddof=0；亮度均值/255、灰度≥250比例，无分数取整。
- [联合CPU宿主](../../apps/ai_inference/pipeline_cpu.py)：在有界spawn子进程内每图只解码一次，先计算全部图片质量。任一不通过时整批停止，不加载模型，不返回前面合格图的检测/OCR。全部通过后D-FINE与OCR det/rec各加载一次及smoke，同一原图顺序运行检测和OCR，质量缩略图不作为检测/OCR输入。
- [联合CLI](../../apps/ai_inference/pipeline.py)：支持1–3张图片、独占输出、全执行最大180秒含启动/解码/质量/模型加载/推理；超时或崩溃回收子进程，任何技术失败无部分成功报告。--quality-only无需模型路径，使用同一质量函数。

默认开发阈值blur_min=80、dark_min=0.12、glare_max=0.30；严格小于/大于失败，恰等通过，多原因按blur/dark/glare排序。默认detection_min=0.4、text_min=0.6，未做现场校准。全请求检测总数≤100且文字区域总数≤100，各自计数，超过任一上限整次MODEL_ERROR，不截断。全部解码的Pillow图像在成功/失败路径关闭，原始字节摘要、一次EXIF转向、RGB和尺寸规则沿用既有本地解码器。

报告schema_version=cpu-pipeline-local-v1。质量失败outcome=needs_retake、quality-only通过为quality_passed、联合完成为needs_review；quality_passed仅为本地枚举，不是HTTP InferenceResult。所有结果review_required=true。报告保留质量参数/分数、检测/文字、输入/模型/config/runtime摘要、实际阶段是否执行及计时；图像/检测/行ID与前两批同公式。未执行模型时artifacts为空、detector/ocr runtime为null，不宣称核验或加载模型。

## 2. 运行入口

已安装requirements/py311-ai-real.txt中的CPU依赖，本批无新增依赖。默认2个intra-op线程、1个inter-op线程、sequential、唯一CPUExecutionProvider，模型及配置SHA仍沿用已核验资产。OCR runtime描述由单独OCR入口和联合入口共用，避免两处配置漂移。

~~~powershell
$env:APP_ENV = 'dev'
.\.venv-ai\Scripts\python.exe -m apps.ai_inference.pipeline `
  --model C:/Users/12847/Desktop/model.onnx `
  --det-dir D:/LenovoSoftstore/Obsidian-Storage/PP-OCRv6_small_det_onnx `
  --rec-dir D:/LenovoSoftstore/Obsidian-Storage/PP-OCRv6_small_rec_onnx `
  --input C:/path/overview.jpg C:/path/detail.png `
  --output C:/path/new-pipeline.json

.\.venv-ai\Scripts\python.exe -m apps.ai_inference.pipeline `
  --quality-only --input C:/path/overview.jpg `
  --output C:/path/new-quality.json
~~~

替换图片/输出路径，输出父目录须存在、文件须不存在；省略output写UTF-8 stdout。可选--config/--preprocessor、--threshold/--text-min、--blur-min/--dark-min/--glare-max、--threads/--timeout/--run-id。质量评估正常完成但需补拍时仍退出0，检查outcome；技术错误非零退出并输出稳定错误，不写成功报告。质量检查不加载模型，因此不能据quality-only或补拍报告判断模型目录可用或服务ready。

## 3. 验证与证据

[34-pipeline-cpu-evidence.json](34-pipeline-cpu-evidence.json)记录真实权重联合执行。微软雅黑42像素在灰度180背景生成横图800×400、竖图400×800：两图默认质量阈值通过（blur约475.60、brightness约0.69325、glare=0），检测各300候选全部低于0.4，OCR各完整识别 `ETHANOL`、`EXP 2027-12-31`、`乙醇`。两次独立CPU子进程的输入摘要、质量/检测/文字/坐标/标识一致。单次子进程总耗时2329ms，仅这组工程smoke，不是生产P95。

加入一张黑图的混合批次返回needs_retake，全部图片inference_executed=false、检测/文字为空、制品记录为空。独立quality-only与联合流水线的质量分数逐图完全一致。证据含字体、输入、模型/配置/runtime摘要、生成参数及分阶段耗时；没有降低默认质量阈值来绕过门禁。

~~~powershell
$env:APP_ENV = 'test'
.\.venv-ai\Scripts\python.exe -m tools.ml.smoke_pipeline_cpu `
  --model C:/Users/12847/Desktop/model.onnx `
  --det-dir D:/LenovoSoftstore/Obsidian-Storage/PP-OCRv6_small_det_onnx `
  --rec-dir D:/LenovoSoftstore/Obsidian-Storage/PP-OCRv6_small_rec_onnx `
  --font C:/Windows/Fonts/msyh.ttc `
  --output docs/08-delivery/34-pipeline-cpu-evidence.json
.\.venv-ai\Scripts\python.exe -m unittest tests.ai.test_dfine_adapter tests.ai.test_cpu_runner tests.ai.test_cpu_pixels tests.ai.test_ocrv6_adapter tests.ai.test_quality_pixels tests.ai.test_pipeline_cpu -v
~~~

本轮CPU45项全部通过、无跳过，新增17项：质量黑/白/RGB黄金值、单行/列REFLECT_101与方差260100、阈值恰等及nextafter、不取整、真实Pillow resize/原图保留、NumPy核与当前OpenCV4.11独立对照；联合批次门禁、质量单独检查、一次解码/加载/共享原图、ID重放、100/101双容量、后图失败/坏解码清理、超时/崩溃回收、生产拒绝、CLI新文件与不覆盖。CI独立CPU job加入两个模块，不下载权重。业务环境缺数值依赖的跳过不计为真实像素通过。

## 4. 后续边界

补充验证记录：业务环境tests/ai与tests/protocol为39 passed/25 skipped，跳过的25项真实数值用例已在CPU环境执行，不计作业务环境通过。真实联合CLI另行执行成功；pip check、ruff check/format、生成契约--check、设计发布43项/D-FINE112项/readiness148项和345个文档链接校验通过，git diff --check通过。本批未改业务持久化实现，未重复完整MySQL回归；远程CI尚未执行。

HTTP仍AI_MODE=mock，本地联合报告尚不符合完整InferenceResult，也未接Worker/业务事实。quality-only的180秒总期限是本地入口保护；HTTP quality的CPU10秒内部/15秒总预算、常驻supervisor及版本ready仍须接线，不把一次性spawn当常驻服务。

没有文字到瓶子/字段的业务证据闭包，parent_detection_id=null；chemical_entities、date_facts、bottle_association、container_relations能力均false。质量通过和零检测均不代表安全或自动完成。后续接受控analysis对象读取/版本核验、共享可重建裁剪、文字/瓶子关联与字段词典、/quality和/runs、真实评测及批准台账。整个I-ML-01/04、AI-09/10和生产门禁保持未关闭。
