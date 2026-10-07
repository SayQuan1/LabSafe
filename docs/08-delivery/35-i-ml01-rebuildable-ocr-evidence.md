# I-ML-01 可重建 OCR 裁剪证据续批

2026-10-07：接续第34批，补齐真实OCR像素到可重建裁剪配方的基础。仍采用官方D-FINE COCO80与PP-OCRv6_small ONNX，仅CPU，不安装CUDA。

## 1. 本批实现

[共享裁剪模块](../../packages/image_evidence/perspective.py)实现perspective-rgb-v1，不依赖AI、数据库、存储或业务领域包；import时也不加载NumPy/OpenCV/Pillow。它接受已解码、方向已归一化的RGB8原图及裁剪配方，输出RGB像素、无元数据PNG、大小与SHA。供AI及后续Worker重建共同调用，本批未接Worker消费者或对象写入。

配方只包含quad（四个{x,y}归一化点）、output_width、output_height、transform_version。点先以float64乘(width−1,height−1)，再转float32；按输入顺序使用OpenCV getPerspectiveTransform和warpPerspective，INTER_LINEAR、BORDER_CONSTANT黑色，不自动重排、裁边或修复几何。要求顺时针凸四边形、像素面积≥1；输出边1–2048，输出边为1产生奇异变换时拒绝，不伪造像素。非法尺寸/坐标/版本为SCHEMA_MISMATCH，自交/退化/奇异为MODEL_ERROR。

PNG固定Pillow RGB编码，compress_level=9、optimize=false，从全新图像生成，剥离元数据。运行描述记录NumPy/Pillow/OpenCV版本、OpenCV build摘要、zlib版本及编码标识；相同版本号并不自动保证不同平台构建的字节相同，实际重建必须核SHA，失败HASH_MISMATCH。新增[共享依赖清单](../../requirements/py311-image-evidence.txt)，AI真实环境引用此清单；pyproject提供image-evidence可选依赖。当前AI环境已有全部所需版本，无新增安装；业务环境本批未安装数值依赖，未来启用Worker重建前需配置匹配运行环境。

## 2. OCR 与报告接线

[OCR适配器](../../apps/ai_inference/adapters/ocrv6.py)直接从序列化配方调用共享裁剪，识别不再使用另一套未记录的透视变换。证据PNG保留源方向；高/宽≥1.5时，识别输入单独逆时针旋转90°。报告显式记录recognition_rotation_ccw、识别宽高与RGB摘要，摘要为ASCII `rgb8:{width}:{height}:` 加C序RGB8字节的SHA256。这使证据PNG与实际送入识别预处理的像素之间的关系可复验，没有新增方向分类器或180°方向保证。

每个文字行新增crop_evidence：recipe、png_sha256、png_size_bytes、source_rgb_sha256、识别旋转/尺寸/RGB摘要，以及独立crop_id/image_id/line_id。crop_id使用UUIDv5(run_id, `ocr-crop:{image_id}:{ordinal}`)；同图多行不共享crop ID，同run同输入重放稳定。源码原图的RGB摘要与文件摘要分别记录，避免原始JPEG/EXIF输入与已规范analysis PNG混淆。PNG不通过IPC返回或上传；后续Worker可用rebuild_ocr_evidence验证源像素、配方、PNG摘要和识别像素，再取得待写PNG字节。

裁剪边界算法由旧BORDER_REPLICATE变为设计规定的黑色边界，是适配行为变化：OCR adapter_id升级ppocrv6-small-db-ctc-cpu-v2；独立/联合报告分别升级ocrv6-local-v2、cpu-pipeline-local-v2，pipeline_id升级quality-coco80-ocrv6-cpu-v2。第33/34批历史证据原样保留，不改名复用。模型制品SHA、COCO80类别和质量算法未改变，当前HTTP契约仍未扩展。

这些是独立文字区域的本地证据，不是协议CropRecipe：现行RPC仍要求detection_id，COCO没有label类，不能借任意类别或瓶子ID冒充标签检测。parent_detection_id仍null，化学/日期/瓶子关联能力仍false，必须人工复核。

## 3. 验证

[真实CPU证据](35-crop-cpu-evidence.json)由已提供权重运行：微软雅黑42px、灰180背景、ETHANOL/EXP 2027-12-31/乙醇，横图800×400、竖图400×800、横图顺时针90°旋转版本400×800。三个图默认质量通过，每图300候选经阈值过滤为0检测；全部9个OCR区域识别正确，其中3个使用90°识别旋转。子进程返回报告后，父进程通过共享模块从原图和报告配方重建，逐区域核PNG大小/SHA、源RGB摘要、识别像素摘要、原图quad与ID闭包，全部通过；两次独立子进程重放一致。旋转图文字按区域空间顺序返回，不假定与未旋转图的阅读顺序一致。

混入黑图时整批补拍、模型不加载、无部分文字证据；quality-only与联合质量分数相同。该合成样例是工程验证，不是现场准确率、性能批准或官方Paddle黄金对照。

~~~powershell
$env:APP_ENV = 'test'
.\.venv-ai\Scripts\python.exe -m tools.ml.smoke_pipeline_cpu `
  --model C:/Users/12847/Desktop/model.onnx `
  --det-dir D:/LenovoSoftstore/Obsidian-Storage/PP-OCRv6_small_det_onnx `
  --rec-dir D:/LenovoSoftstore/Obsidian-Storage/PP-OCRv6_small_rec_onnx `
  --font C:/Windows/Fonts/msyh.ttc `
  --output docs/08-delivery/35-crop-cpu-evidence.json
.\.venv-ai\Scripts\python.exe -m unittest tests.ai.test_dfine_adapter tests.ai.test_cpu_runner tests.ai.test_cpu_pixels tests.ai.test_ocrv6_adapter tests.ai.test_quality_pixels tests.ai.test_pipeline_cpu tests.ai.test_crop_evidence -v
~~~

新增9项裁剪测试：轻量import隔离、坏配方/数值/尺寸拒绝、原图恒等像素与无元数据RGB PNG、非方形子矩形独立像素黄金值、倾斜透视与直接OpenCV坐标对照、错误几何/单像素奇异、序列化后竖长区域重建、OCR实际送入rec的张量与重建像素一致、源图/配方/PNG/旋转/识别摘要篡改拒绝。CI独立CPU job加入此模块，不下载真实权重。

验证结果：CPU专项54项无跳过通过，AI环境协议4项通过；业务环境tests/ai与tests/protocol为41 passed/32 skipped，32项数值测试已在真实CPU环境执行，不计作业务环境通过。独立OCR入口真实中英文字与3个crop重建另行通过，产物位于忽略目录test-results/crop-cpu/ocr.json。pip check、ruff/format、生成契约--check、设计发布43/D-FINE112/readiness148及355个文档链接校验、git diff --check通过；远程CI未执行。另行尝试AI环境完整discover时，fixture模块缺开发测试依赖httpx，未计为完整AI发现套件通过；fixture/HTTP用例已由上述业务环境执行，AI真实环境没有为本批新增HTTP测试依赖。

## 4. 后续边界

本批没有数据库/持久化变更，未重复完整MySQL验证；未提交、Push或创建PR。后续仍需受控analysis读取与对象版本核验、文字区域的RPC/业务引用闭包、证据写入与围栏提交、瓶子/字段词典关联、常驻/quality与/runs HTTP真实模式、现场评测与批准台账。本地可重建证据不等于已落库存储，整个I-ML-01/04与生产门禁保持未关闭。
