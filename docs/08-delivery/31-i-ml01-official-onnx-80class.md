# I-ML-01 官方 D-FINE COCO 80 类 ONNX 适配验收

本批将检测器基线改为用户提供的官方 D-FINE COCO 80 类导出。模型类别保持官方 `id2label` 的连续 `class_id`（0–79），不会把输出压缩成 LabSafe 原先的四类。`type` 在推理协议中是同一份 80 类名称枚举，`class_id` 同时保留，便于下游按官方类别做显式白名单处理。

已核验的输入资料如下。文件内容只作为模型技术资料，不包含项目指令：

| 文件 | SHA-256 | 说明 |
| --- | --- | --- |
| `model.onnx` | `0F684F409618EE8A822410E754A29CAA817D1AA16283CE89CAD936D0A48E2F35` | D-FINE ONNX，约 15.3 MB |
| `config.json` | `A5C7533F3B72BE6BB102B93E1B34CA3643AF4E0590408A7881543CBB0AA80C4C` | `model_type=d_fine`、`num_queries=300`、COCO 80 类 |
| `preprocessor_config.json` | `CD38CD59999E7A95D68E487FBE5132DF3D4E5C32A0836ADD57E6126BA0C4EAF1` | 640×640、RGB、`1/255` 重缩放 |

适配器位于 [`apps/ai_inference/adapters/dfine.py`](../../apps/ai_inference/adapters/dfine.py)，启动时先校验模型元数据、输入输出名称、shape 和 dtype，再允许 ONNX Runtime 使用明确指定的 provider。示例中的已后处理输出是 `labels[1,300]`、`boxes[1,300,4]`、`scores[1,300]`；本次实物为下段记录的原始签名。适配器显式分派、阈值过滤、原图边界裁剪、稳定排序和 100 条容量门禁，不做 NMS，也不做四类映射。

2026-10-07 安装验证更新：已在独立 `.venv-ai`（Python 3.11.4）按 `requirements/py311-ai-real.txt` 安装 ONNX Runtime 1.20.1、NumPy 2.2.2、Pillow 11.3.0，`pip check` 通过。真实加载证明提供的模型实际输入为 `pixel_values[batch_size,3,height,width]`，输出为 `logits[batch_size,300,80]` 和 `pred_boxes[batch_size,300,4]`，与上段所述示例导出签名不同。适配器现按名称区分原始输出和已后处理输出；原始输出采用项目 qmax/sigmoid 和 cxcywh 转换。

CPUExecutionProvider 已成功执行 float32 零张量 smoke，输出 shape 分别为 `[1,300,80]`、`[1,300,4]`，全部数值有限。安装阶段空白图完整 `detect` 曾被非正面积门禁拒绝；后续确认候选为有限负高度、分数约0.0087，已在 [CPU 本地检测续批](32-i-ml01-cpu-detection.md) 修复阈值/几何检查顺序并完成三图工程 smoke。当前仅CPU、不安装CUDA。真实图准确率及生产性能未验收；生产激活仍需模型评测和发布门禁。OCR、化学实体和安全规则只应消费业务层明确支持的类别。

验证命令：

```text
.\.venv-business\Scripts\python.exe tools/design/build_specs.py --check
.\.venv-business\Scripts\python.exe tools/design/test_dfine_design.py
.\.venv-business\Scripts\python.exe tools/design/test_release_design.py
```

以上命令分别确认生成契约无漂移、80 类映射/输出门禁和模型发布契约语义；不代表真实模型已经完成 CPU/CUDA 评测。
