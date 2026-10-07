# I-ML-01 CPU 本地检测续批验收

2026-10-07：按用户要求，当前使用 CPU，不安装 CUDA。本批在独立 Python 3.11 AI 环境接通官方 COCO 80 类权重的本地检测入口；不将本地检测报告记作完整业务推理服务或生产验收。

## 1. 交付与模型签名

- [CPU 子进程宿主](../../apps/ai_inference/cpu.py)：使用 multiprocessing spawn，一次加载模型及黑图 smoke，顺序处理 1–3 张本地图像；唯一 provider 为 CPUExecutionProvider，默认 intra-op=2、inter-op=1、sequential。
- [检测 CLI](../../apps/ai_inference/detect.py)：只接受 APP_ENV=dev/test、Python 3.11；总期限含启动/加载/推理，最大 180 秒。超时 kill/join 并清理 IPC，崩溃返回错误，多图失败不返回部分结果。输出文件独占创建，已有文件不会覆盖。
- [模型适配器](../../apps/ai_inference/adapters/dfine.py)：加载前核验 model/config/preprocessor SHA；把再次核验的模型字节交给 ORT。实际输入 pixel_values=float32[1,3,640,640]，输出 logits=float32[1,300,80]、pred_boxes=float32[1,300,4]（归一化 cxcywh）。示例中的 images/orig_target_sizes→labels/boxes/scores 是另一种已后处理签名。

预处理使用 RGB、Pillow BILINEAR 直接拉伸 640×640、float32/255、连续 NCHW，不做 padding 或均值/方差标准化。本地原图解码应用一次 EXIF 方向；限制 PNG/JPEG/WebP、12 MiB、最长边 10000、4000 万像素，拒绝动画与损坏图像。

每 query 选最大 logit 对应类别，sigmoid 得分；保留全部 80 类，不做 NMS。全部候选先检查有限性，阈值筛选后再对保留框检查正宽高/正面积，随后裁边、归一化和稳定排序。黑图原有失败候选实际为有限负高度框，分数约 0.0087；原先在阈值过滤之前校验几何导致整图失败，本批修复。高分坏框和任意分数 NaN/Inf 仍失败，全请求检测总数超过 100 仍失败，不截断。

JSON 报告 schema_version=coco80-local-v1，记录实际制品 SHA、runtime/provider/线程快照及摘要、输入字节 SHA、宽高、诊断和耗时；run/image/detection 标识可确定性重放。purpose=development、is_simulated=false 指真实模型执行，输入仍可为合成图。review_required=true；COCO 没有 label/shelf/cabinet，parent_detection_id=null，OCR、化学实体、容器关系能力均为 false。

## 2. 运行入口

已安装 CPU 环境：Python 3.11.4、onnxruntime 1.20.1、numpy 2.2.2、Pillow 11.3.0；依赖见 [CPU requirements](../../requirements/py311-ai-real.txt)，pip check 通过。模型和两个 JSON 默认在同一目录，另可用 --config/--preprocessor 指定。

~~~powershell
$env:APP_ENV = 'dev'
.\.venv-ai\Scripts\python.exe -m apps.ai_inference.detect `
  --model C:\Users\12847\Desktop\model.onnx `
  --input C:\path\image1.jpg C:\path\image2.png `
  --output C:\path\new-detections.json
~~~

图像和输出路径换成实际路径，输出父目录须存在。默认 threshold=0.4、threads=2、timeout=180；省略 --output 则输出到 stdout。0.4 是本地工具开发默认值，未经过现场校准，不替换 manifest 的开发联调初值 0.25。--run-id 固定 UUID 可比较重放；耗时字段允许变化。

## 3. 验证与证据

真实权重合成图证据见 [32-cpu-inference-evidence.json](32-cpu-inference-evidence.json)。黑图 800×400、色块竖图 400×800、固定种子噪声图 800×400 均成功；各 300 候选在 0.4 阈值下全部过滤，检测列表为空。两次独立 CPU 子进程运行的输入摘要、标识、诊断和检测相同。证据记录单次父进程 1500 ms、子进程含加载/smoke/三图 1265 ms，仅工程 smoke，不是现场准确率或生产 P95。

~~~powershell
$env:APP_ENV = 'test'
.\.venv-ai\Scripts\python.exe -m tools.ml.smoke_cpu `
  --model C:\Users\12847\Desktop\model.onnx `
  --output docs/08-delivery/32-cpu-inference-evidence.json
.\.venv-ai\Scripts\python.exe -m unittest tests.ai.test_dfine_adapter tests.ai.test_cpu_runner tests.ai.test_cpu_pixels -v
~~~

CPU 环境 16 项测试全部执行并通过，覆盖真实 NumPy/Pillow 预处理、EXIF/坏图/动画、raw/已后处理签名分派、低分退化/高分坏框/NaN、100/101 容量、超时回收与后续再次成功、崩溃、生产拒绝、模型单次加载及确定性标识。业务环境定向 tests/ai 与 tests/protocol 为 32 passed、3 skipped；跳过的3项真实像素测试已在CPU环境执行，不将跳过算通过。设计 D-FINE 语义 112 项、readiness 148 项、生成契约和设计链接校验通过；ruff check 与 format --check 覆盖 apps/packages/tests/tools/database/tools/ml，git diff --check 通过。CI 新增独立 CPU 适配器 job，安装真实 CPU 依赖并运行上述测试，不下载权重；尚未执行远程CI。本批未改持久化，不重复完整MySQL回归。

## 4. 剩余边界

后续[PP-OCRv6_small CPU续批](33-i-ml01-ocrv6-cpu.md)已接独立文字检测/识别；本批COCO检测报告仍保持原能力标记，不自动合并文字或生成化学事实。

现有 HTTP 服务仍为 AI_MODE=mock。本地报告不符合完整 InferenceResult，不接 Worker RPC 或事实落库。下一步需要受控 analysis 对象读取、真实 quality、/quality 与 /runs 接线、常驻 supervisor 生命周期及版本核验；OCR 需自身文字检测和明确证据来源。现场数据授权、阈值校准、真实照片评测及独立批准继续按 I-ML-02/03/04 与 R-ML-01 关闭。CUDA 暂缓，不安装或用 CPU smoke 声称 GPU 达标。
