# YOLO 目标检测模块
# 支持: YOLOv5 .pt (torch.hub) / YOLOv8 .onnx (OpenCV DNN)
# 如果没有模型文件，自动回退到模拟模式

import os
# 禁用 ultralytics 自动安装依赖
os.environ.setdefault('YOLO_VERBOSE', 'False')
os.environ.setdefault('YOLO_AUTOUPDATE', 'False')
os.environ.setdefault('MPLCONFIGDIR', os.path.join(os.environ.get('TEMP', '.'), 'matplotlib'))

import cv2
import numpy as np
import os
from typing import List, Dict, Optional
from dataclasses import dataclass


@dataclass
class Detection:
    bbox: List[float]  # [x1, y1, x2, y2]
    class_name: str
    confidence: float


class YOLODetector:
    """YOLO 检测器封装"""

    # COCO 类别中与实验室相关的映射
    LAB_CLASSES = {
        39: "bottle",      # bottle
        41: "cup",         # cup -> 可作为容器
        67: "cell phone",  # 可扩展为标签
    }

    def __init__(self, model_path: Optional[str] = None, conf_threshold: float = 0.25, nms_threshold: float = 0.45):
        self.conf_threshold = conf_threshold
        self.nms_threshold = nms_threshold
        self.net = None
        self.model = None  # torch.hub model
        self.input_size = (640, 640)
        self.use_mock = True
        self.model_type = "none"  # "pt" | "onnx" | "none"

        if model_path and os.path.exists(model_path):
            ext = os.path.splitext(model_path)[1].lower()
            if ext == ".onnx":
                self._load_onnx(model_path)
            elif ext == ".pt":
                self._load_pt(model_path)
            else:
                print(f"[YOLO] 不支持的模型格式: {ext}，使用模拟模式")
        else:
            print(f"[YOLO] 模型文件不存在: {model_path}，使用模拟模式")

    def _load_onnx(self, model_path: str):
        """加载 ONNX 模型 (OpenCV DNN)"""
        try:
            self.net = cv2.dnn.readNetFromONNX(model_path)
            self.use_mock = False
            self.model_type = "onnx"
            print(f"[YOLO] ONNX 模型加载成功: {model_path}")
        except Exception as e:
            print(f"[YOLO] ONNX 加载失败: {e}，使用模拟模式")

    def _load_pt(self, model_path: str):
        """加载 YOLOv5 .pt 模型（直接 torch.load，绕过 hub 依赖检查）"""
        try:
            import torch
            import sys

            # 将本地 yolov5 仓库加入路径
            hub_cache = os.path.join(os.path.expanduser('~'), '.cache', 'torch', 'hub', 'ultralytics_yolov5_master')
            if os.path.exists(hub_cache) and hub_cache not in sys.path:
                sys.path.insert(0, hub_cache)

            # 加载 state_dict
            state_dict = torch.load(model_path, map_location='cpu', weights_only=False)

            # 如果是纯 state_dict，需要构建模型结构
            from models.yolo import Model
            from models.common import DetectMultiBackend

            # 尝试用 yolo v5s 的配置构建模型
            import yaml
            model_yaml = os.path.join(hub_cache, 'models', 'yolov5s.yaml')
            model = Model(model_yaml, nc=80)

            # 去除 'model.' 前缀（因为保存的是 model.model.state_dict()）
            new_state_dict = {}
            for k, v in state_dict.items():
                if k.startswith('model.'):
                    new_key = k[6:]
                else:
                    new_key = k
                new_state_dict[new_key] = v

            model.load_state_dict(new_state_dict, strict=False)
            model = model.float().eval()
            model.conf = self.conf_threshold
            model.iou = self.nms_threshold
            self.model = model
            self.use_mock = False
            self.model_type = "pt"
            print(f"[YOLO] .pt 模型加载成功: {model_path}")
        except Exception as e:
            print(f"[YOLO] .pt 加载失败: {e}，使用模拟模式")

    def detect(self, image_path: str) -> List[Detection]:
        """执行检测，返回 Detection 列表"""
        if self.use_mock:
            return self._mock_detect(image_path)

        try:
            if self.model_type == "onnx":
                return self._real_detect_onnx(image_path)
            elif self.model_type == "pt":
                return self._real_detect_pt(image_path)
            else:
                return self._mock_detect(image_path)
        except Exception as e:
            print(f"[YOLO] 推理失败: {e}，回退到模拟模式")
            return self._mock_detect(image_path)

    def _real_detect_pt(self, image_path: str) -> List[Detection]:
        """YOLOv5 .pt 推理（直接加载的模型，需手动预处理和NMS）"""
        import torch
        from PIL import Image

        img = Image.open(image_path).convert('RGB')
        orig_w, orig_h = img.size

        # 预处理：resize 到 640x640
        img_resized = img.resize((640, 640), Image.BILINEAR)
        img_tensor = torch.from_numpy(np.array(img_resized)).permute(2, 0, 1).float() / 255.0
        img_tensor = img_tensor.unsqueeze(0)

        with torch.no_grad():
            pred = self.model(img_tensor)

        # YOLOv5 输出: tuple, pred[0] 是 [batch, num_boxes, 5+num_classes]
        if isinstance(pred, (tuple, list)):
            pred = pred[0]

        # 尝试使用 yolov5 的 NMS
        try:
            from utils.general import non_max_suppression
            results = non_max_suppression(pred, conf_thres=self.conf_threshold, iou_thres=self.nms_threshold)
            detections = []
            for det in results:
                if det is not None and len(det) > 0:
                    for *box, conf, cls_id in det.cpu().numpy():
                        cls_id = int(cls_id)
                        if cls_id in self.LAB_CLASSES:
                            # 坐标从 640x640 映射回原图
                            scale_x = orig_w / 640
                            scale_y = orig_h / 640
                            x1 = float(box[0]) * scale_x
                            y1 = float(box[1]) * scale_y
                            x2 = float(box[2]) * scale_x
                            y2 = float(box[3]) * scale_y
                            detections.append(Detection(
                                bbox=[x1, y1, x2, y2],
                                class_name=self.LAB_CLASSES[cls_id],
                                confidence=float(conf)
                            ))
            return detections
        except ImportError:
            # 没有 yolov5 utils，使用 torchvision NMS
            pass

        # 手动 NMS 回退
        boxes = []
        scores = []
        class_ids = []

        pred_np = pred[0].cpu().numpy()
        for i in range(pred_np.shape[0]):
            obj_conf = pred_np[i, 4]
            if obj_conf < self.conf_threshold:
                continue
            cls_scores = pred_np[i, 5:]
            cls_id = int(np.argmax(cls_scores))
            cls_conf = cls_scores[cls_id] * obj_conf
            if cls_conf < self.conf_threshold:
                continue
            if cls_id not in self.LAB_CLASSES:
                continue

            x, y, w, h = pred_np[i, :4]
            x1 = (x - w / 2) * orig_w / 640
            y1 = (y - h / 2) * orig_h / 640
            x2 = (x + w / 2) * orig_w / 640
            y2 = (y + h / 2) * orig_h / 640
            boxes.append([x1, y1, x2 - x1, y2 - y1])
            scores.append(float(cls_conf))
            class_ids.append(cls_id)

        if not boxes:
            return []

        # OpenCV NMS
        indices = cv2.dnn.NMSBoxes(boxes, scores, self.conf_threshold, self.nms_threshold)
        detections = []
        if len(indices) > 0:
            for i in indices.flatten():
                x, y, w, h = boxes[i]
                detections.append(Detection(
                    bbox=[x, y, x + w, y + h],
                    class_name=self.LAB_CLASSES[class_ids[i]],
                    confidence=scores[i]
                ))
        return detections

    def _real_detect_onnx(self, image_path: str) -> List[Detection]:
        """YOLOv8 ONNX 推理 (OpenCV DNN)"""
        img = cv2.imdecode(np.fromfile(image_path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError(f"无法读取图片: {image_path}")

        h, w = img.shape[:2]

        blob = cv2.dnn.blobFromImage(img, 1/255.0, self.input_size, swapRB=True, crop=False)
        self.net.setInput(blob)
        outputs = self.net.forward()
        outputs = np.transpose(np.squeeze(outputs))

        boxes = []
        scores = []
        class_ids = []

        for i in range(outputs.shape[0]):
            classes_scores = outputs[i][4:]
            max_score = np.amax(classes_scores)

            if max_score >= self.conf_threshold:
                class_id = np.argmax(classes_scores)

                if class_id in self.LAB_CLASSES:
                    x, y, w_box, h_box = outputs[i][0], outputs[i][1], outputs[i][2], outputs[i][3]

                    left = int((x - w_box/2) * w / self.input_size[0])
                    top = int((y - h_box/2) * h / self.input_size[1])
                    width = int(w_box * w / self.input_size[0])
                    height = int(h_box * h / self.input_size[1])

                    boxes.append([left, top, width, height])
                    scores.append(float(max_score))
                    class_ids.append(class_id)

        indices = cv2.dnn.NMSBoxes(boxes, scores, self.conf_threshold, self.nms_threshold)

        detections = []
        if len(indices) > 0:
            for i in indices.flatten():
                x, y, w_box, h_box = boxes[i]
                detections.append(Detection(
                    bbox=[x, y, x + w_box, y + h_box],
                    class_name=self.LAB_CLASSES[class_ids[i]],
                    confidence=scores[i]
                ))

        return detections

    def _mock_detect(self, image_path: str) -> List[Detection]:
        """模拟检测"""
        filename = os.path.basename(image_path).lower()

        if "blurry" in filename or "glare" in filename or "dark" in filename:
            return []

        if "single" in filename or "alone" in filename:
            return [
                Detection(bbox=[120, 80, 200, 300], class_name="bottle", confidence=0.94),
                Detection(bbox=[125, 85, 195, 160], class_name="label", confidence=0.91),
                Detection(bbox=[100, 50, 400, 320], class_name="shelf", confidence=0.95),
            ]

        return [
            Detection(bbox=[120, 80, 200, 300], class_name="bottle", confidence=0.94),
            Detection(bbox=[125, 85, 195, 160], class_name="label", confidence=0.91),
            Detection(bbox=[250, 75, 330, 290], class_name="bottle", confidence=0.89),
            Detection(bbox=[255, 80, 325, 155], class_name="label", confidence=0.87),
            Detection(bbox=[100, 50, 400, 320], class_name="shelf", confidence=0.95),
        ]


# 全局检测器实例
_detector: Optional[YOLODetector] = None


def get_detector(model_path: Optional[str] = None) -> YOLODetector:
    """获取或创建检测器实例"""
    global _detector
    if _detector is None:
        _detector = YOLODetector(model_path)
    return _detector


def reset_detector():
    """重置检测器"""
    global _detector
    _detector = None
