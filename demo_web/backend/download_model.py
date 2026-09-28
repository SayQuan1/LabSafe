# 下载 YOLO 模型
# 方案1: 使用 torch.hub 下载 YOLOv5s (内置自动下载)
# 方案2: 手动下载 ONNX 模型

import os
import sys

MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")


def download_yolov5s_pt():
    """使用 torch.hub 下载 YOLOv5s .pt 模型"""
    try:
        import torch
        print("正在通过 torch.hub 下载 YOLOv5s...")

        # 这会下载模型到 torch hub 缓存目录
        model = torch.hub.load('ultralytics/yolov5', 'yolov5s', pretrained=True, trust_repo=True)

        # 保存到我们的 models 目录
        os.makedirs(MODEL_DIR, exist_ok=True)
        model_path = os.path.join(MODEL_DIR, "yolov5s.pt")

        # 获取模型状态字典
        torch.save(model.model.state_dict(), model_path)
        print(f"模型已保存: {model_path}")
        return model_path

    except Exception as e:
        print(f"torch.hub 下载失败: {e}")
        return None


def download_yolov8n_onnx():
    """下载 YOLOv8n ONNX 模型"""
    import urllib.request

    os.makedirs(MODEL_DIR, exist_ok=True)
    model_path = os.path.join(MODEL_DIR, "yolov8n.onnx")

    if os.path.exists(model_path):
        print(f"模型已存在: {model_path}")
        return model_path

    # 尝试多个镜像源
    mirrors = [
        "https://github.com/ultralytics/assets/releases/download/v8.2.0/yolov8n.onnx",
        "https://ghproxy.com/https://github.com/ultralytics/assets/releases/download/v8.2.0/yolov8n.onnx",
        "https://mirror.ghproxy.com/https://github.com/ultralytics/assets/releases/download/v8.2.0/yolov8n.onnx",
    ]

    for url in mirrors:
        try:
            print(f"尝试下载: {url}")
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=30) as response:
                with open(model_path, 'wb') as f:
                    f.write(response.read())
            size = os.path.getsize(model_path) / (1024 * 1024)
            print(f"下载成功! 大小: {size:.2f} MB")
            return model_path
        except Exception as e:
            print(f"  失败: {e}")
            continue

    print("所有镜像源均失败")
    return None


def export_yolov5_to_onnx(pt_path):
    """将 YOLOv5 .pt 导出为 ONNX"""
    try:
        import torch

        # 加载模型
        model = torch.hub.load('ultralytics/yolov5', 'custom', path=pt_path, trust_repo=True)
        model.eval()

        # 导出 ONNX
        onnx_path = pt_path.replace('.pt', '.onnx')
        dummy_input = torch.randn(1, 3, 640, 640)

        torch.onnx.export(
            model.model,
            dummy_input,
            onnx_path,
            input_names=['images'],
            output_names=['output'],
            dynamic_axes={'images': {0: 'batch'}, 'output': {0: 'batch'}},
            opset_version=12
        )
        print(f"ONNX 导出成功: {onnx_path}")
        return onnx_path

    except Exception as e:
        print(f"ONNX 导出失败: {e}")
        return None


if __name__ == "__main__":
    print("=" * 50)
    print("YOLO 模型下载工具")
    print("=" * 50)

    # 方案1: 尝试 torch.hub
    print("\n[方案1] 尝试 torch.hub 下载 YOLOv5s...")
    pt_path = download_yolov5s_pt()

    if pt_path:
        print(f"\nYOLOv5s .pt 模型已下载: {pt_path}")
        print("可以直接使用，无需 ONNX 转换")

        # 可选: 导出 ONNX
        print("\n[可选] 导出为 ONNX...")
        export_yolov5_to_onnx(pt_path)
    else:
        # 方案2: 尝试 ONNX 镜像
        print("\n[方案2] 尝试下载 YOLOv8n ONNX...")
        onnx_path = download_yolov8n_onnx()

        if not onnx_path:
            print("\n所有下载方式均失败，请手动下载模型文件:")
            print(f"  保存目录: {MODEL_DIR}")
            print("  支持格式: yolov5s.pt / yolov8n.onnx / yolov8n.pt")
