# YOLO 目标检测模块
# 直接通过 torch 加载 YOLOv5 .pt（不依赖 ultralytics 的下载逻辑）
# 无模型时回退到颜色+轮廓分析

import os
import sys
import cv2
import numpy as np
import torch
from typing import List, Optional
from dataclasses import dataclass


@dataclass
class Detection:
    bbox: List[float]  # [x1, y1, x2, y2] 原图像素坐标
    class_name: str
    confidence: float


class YOLODetector:
    """YOLOv5 检测器（直接 torch 加载）"""

    COCO_NAMES = [
        "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
        "truck", "boat", "traffic light", "fire hydrant", "stop sign",
        "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep",
        "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
        "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard",
        "sports ball", "kite", "baseball bat", "baseball glove", "skateboard",
        "surfboard", "tennis racket", "bottle", "wine glass", "cup", "fork",
        "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
        "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
        "couch", "potted plant", "bed", "dining table", "toilet", "tv",
        "laptop", "mouse", "remote", "keyboard", "cell phone", "microwave",
        "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase",
        "scissors", "teddy bear", "hair drier", "toothbrush"
    ]

    def __init__(self, model_path: Optional[str] = None, conf_threshold: float = 0.25, nms_threshold: float = 0.45):
        self.conf_threshold = conf_threshold
        self.nms_threshold = nms_threshold
        self.model = None
        self.use_mock = True
        self.model_type = "none"
        self._yolov5_path = None

        if model_path and os.path.exists(model_path):
            try:
                self._load_yolov5_direct(model_path)
            except Exception as e:
                print(f"[YOLO] 直接加载失败: {e}")
        else:
            print(f"[YOLO] 模型文件不存在: {model_path}")

    def _load_yolov5_direct(self, model_path: str):
        """通过本地 yolov5 源码加载权重（优先官方 attempt_load，不走网络）"""
        hub = os.path.join(os.path.expanduser('~'), '.cache', 'torch', 'hub', 'ultralytics_yolov5_master')
        if not os.path.exists(hub):
            print("[YOLO] 本地无 yolov5 源码缓存，无法加载")
            return

        sys.path.insert(0, hub)
        self._yolov5_path = hub

        # 方式1：官方加载器（标准 checkpoint，含完整 BN/结构校验）
        try:
            from models.experimental import attempt_load
            model = attempt_load(model_path, device=torch.device('cpu'))
            model = model.float().eval()
            self.model = model
            self.use_mock = False
            self.model_type = "attempt_load"
            print(f"[YOLO] 官方加载成功: {os.path.basename(model_path)}")
            return
        except Exception as e:
            print(f"[YOLO] attempt_load 失败（{e}），尝试裸 state_dict 加载")

        # 方式2：裸 state_dict 兜底（key 可能带 model. 前缀）
        from models.yolo import Model
        yaml_path = os.path.join(hub, 'models', 'yolov5s.yaml')
        model = Model(yaml_path, nc=80)
        ckpt = torch.load(model_path, map_location='cpu', weights_only=False)
        result = model.load_state_dict(ckpt, strict=False)
        if len(result.missing_keys) > 50:
            # 尝试剥离一层前缀
            ckpt = {k[6:] if k.startswith('model.') else k: v for k, v in ckpt.items()}
            model.load_state_dict(ckpt, strict=False)
        model = model.float().eval()
        if isinstance(model.names, list) and (model.names[0] == 0 or model.names[0] == '0'):
            model.names = self.COCO_NAMES
        self.model = model
        self.use_mock = False
        self.model_type = "state_dict"
        print(f"[YOLO] state_dict 加载成功: {os.path.basename(model_path)}")

    def detect(self, image_path: str) -> List[Detection]:
        """执行检测：仅返回 YOLO 真实推理结果，模型不可用时返回空列表（不伪造检测）"""
        if self.use_mock or self.model is None:
            print("[YOLO] 模型未加载，跳过检测（无真实推理结果）")
            return []

        try:
            return self._real_detect(image_path)
        except Exception as e:
            print(f"[YOLO] 推理失败: {e}")
            return []

    def _real_detect(self, image_path: str) -> List[Detection]:
        """YOLOv5 推理：标准 letterbox 预处理 + scale_boxes 坐标映射"""
        from utils.general import non_max_suppression, scale_boxes
        try:
            from utils.augmentations import letterbox
        except ImportError:
            from utils.general import letterbox

        img = cv2.imdecode(np.fromfile(image_path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return []

        orig_h, orig_w = img.shape[:2]

        # letterbox：等比缩放 + 填充到 640x640
        im = letterbox(img, 640, auto=False)[0]
        im = im.transpose((2, 0, 1))[::-1]  # HWC BGR -> CHW RGB
        im = np.ascontiguousarray(im)
        t = torch.from_numpy(im).float() / 255.0
        t = t.unsqueeze(0)

        with torch.no_grad():
            pred = self.model(t)

        pred = non_max_suppression(pred, self.conf_threshold, self.nms_threshold, max_det=50)

        detections = []
        names = self.model.names if hasattr(self.model, 'names') else {i: n for i, n in enumerate(self.COCO_NAMES)}

        if pred and len(pred) > 0 and pred[0] is not None and len(pred[0]) > 0:
            det = pred[0]
            # 坐标从 640x640 letterbox 空间映射回原图（官方函数，自动扣除填充）
            det[:, :4] = scale_boxes(im.shape[1:], det[:, :4], (orig_h, orig_w)).round()
            for d in det:
                x1, y1, x2, y2, conf, cls_id = d.cpu().numpy()
                cls_id = int(cls_id)
                class_name = names.get(cls_id, f"class_{cls_id}") if isinstance(names, dict) else (names[cls_id] if cls_id < len(names) else f"class_{cls_id}")

                detections.append(Detection(
                    bbox=[float(max(0, x1)), float(max(0, y1)),
                          float(min(orig_w, x2)), float(min(orig_h, y2))],
                    class_name=class_name,
                    confidence=float(conf)
                ))

        detections.sort(key=lambda d: d.confidence, reverse=True)
        print(f"[YOLO] 检测到 {len(detections)} 个目标: {[(d.class_name, round(d.confidence, 2)) for d in detections[:10]]}")
        return detections

    def _color_contour_detect(self, image_path: str) -> List[Detection]:
        """颜色 + 轮廓分析（无模型时或合成图片回退）"""
        img = cv2.imdecode(np.fromfile(image_path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return []

        h, w = img.shape[:2]
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        detections = []

        # 策略1：检测红色瓶盖（HSV 红色范围）
        red_mask1 = cv2.inRange(hsv, np.array([0, 80, 80]), np.array([10, 255, 255]))
        red_mask2 = cv2.inRange(hsv, np.array([160, 80, 80]), np.array([180, 255, 255]))
        red_mask = cv2.bitwise_or(red_mask1, red_mask2)

        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        red_mask = cv2.morphologyEx(red_mask, cv2.MORPH_CLOSE, kernel, iterations=2)
        red_mask = cv2.morphologyEx(red_mask, cv2.MORPH_OPEN, kernel, iterations=1)

        red_contours, _ = cv2.findContours(red_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cap_centers = []
        for c in red_contours:
            area = cv2.contourArea(c)
            if area < 100:
                continue
            x, y, cw, ch = cv2.boundingRect(c)
            if 0.5 < ch / cw if cw > 0 else 0 < 3.0:
                cap_centers.append((x + cw // 2, y + ch // 2, cw, ch, area))

        # 策略2：基于瓶盖位置向下扩展瓶身
        for cx, cy, cap_w, cap_h, cap_area in cap_centers:
            # 瓶身宽度约为瓶盖的 2-3 倍
            body_w = int(cap_w * 2.5)
            body_h = int(cap_h * 12)  # 瓶身高约瓶盖的 12 倍
            bx1 = max(0, cx - body_w // 2)
            by1 = max(0, cy - cap_h // 2)
            bx2 = min(w, cx + body_w // 2)
            by2 = min(h, by1 + body_h)

            # 验证：瓶身区域应该有浅色像素（标签/瓶身）
            roi = img[by1:by2, bx1:bx2]
            if roi.size > 0:
                mean_brightness = np.mean(cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY))
                if mean_brightness > 100:  # 瓶身区域足够亮
                    detections.append(Detection(
                        bbox=[float(bx1), float(by1), float(bx2), float(by2)],
                        class_name="bottle",
                        confidence=0.75
                    ))

        # 策略3：如果没有检测到瓶盖，用灰度轮廓
        if not detections:
            blurred = cv2.GaussianBlur(gray, (5, 5), 0)
            edges = cv2.Canny(blurred, 30, 100)
            contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            candidates = []
            for c in contours:
                area = cv2.contourArea(c)
                if area < 500:
                    continue
                x, y, cw, ch = cv2.boundingRect(c)
                aspect = ch / cw if cw > 0 else 0
                if 1.2 < aspect < 8 and cw > 25 and ch > 50:
                    candidates.append((x, y, cw, ch, area))

            candidates.sort(key=lambda c: c[4], reverse=True)
            for x, y, cw, ch, area in candidates[:10]:
                detections.append(Detection(
                    bbox=[float(x), float(y), float(x + cw), float(y + ch)],
                    class_name="bottle",
                    confidence=0.65
                ))

        # 去重
        detections = self._merge_nearby(detections)
        print(f"[YOLO-颜色轮廓] 检测到 {len(detections)} 个目标")
        return detections

    def _merge_nearby(self, boxes: List[Detection], max_gap: int = 30) -> List[Detection]:
        """合并水平距离近的框"""
        if not boxes:
            return boxes
        boxes = sorted(boxes, key=lambda b: b.bbox[0])
        merged = []
        for box in boxes:
            bx1, by1, bx2, by2 = box.bbox
            should_merge = False
            for m in merged:
                mx1, my1, mx2, my2 = m.bbox
                gap = bx1 - mx2
                if gap < 0:
                    ix1 = max(bx1, mx1)
                    iy1 = max(by1, my1)
                    ix2 = min(bx2, mx2)
                    iy2 = min(by2, my2)
                    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
                    a1 = (bx2 - bx1) * (by2 - by1)
                    a2 = (mx2 - mx1) * (my2 - my1)
                    iou = inter / min(a1, a2) if min(a1, a2) > 0 else 0
                    if iou > 0.2:
                        m.bbox = [min(mx1, bx1), min(my1, by1), max(mx2, bx2), max(my2, by2)]
                        m.confidence = max(m.confidence, box.confidence)
                        should_merge = True
                        break
                elif gap < max_gap:
                    v_overlap = min(by2, my2) - max(by1, my1)
                    min_h = min(by2 - by1, my2 - my1)
                    if min_h > 0 and v_overlap / min_h > 0.4:
                        m.bbox = [min(mx1, bx1), min(my1, by1), max(mx2, bx2), max(my2, by2)]
                        m.confidence = max(m.confidence, box.confidence)
                        should_merge = True
                        break
            if not should_merge:
                merged.append(box)
        return merged


# 全局检测器
_detector: Optional[YOLODetector] = None

def get_detector(model_path: Optional[str] = None) -> YOLODetector:
    global _detector
    if _detector is None:
        _detector = YOLODetector(model_path)
    return _detector

def reset_detector():
    global _detector
    _detector = None
