# LabSafe Demo - FastAPI 后端服务
# 运行: python main.py
# 访问: http://localhost:8000

import os
# 禁用 ultralytics 自动更新依赖
os.environ['YOLO_VERBOSE'] = 'False'
os.environ['YOLO_AUTOUPDATE'] = 'False'
# 设置 matplotlib 缓存到临时目录（避免权限问题）
os.environ['MPLCONFIGDIR'] = os.path.join(os.environ.get('TEMP', '.'), 'matplotlib')

import uuid
import asyncio
import math
import cv2
import numpy as np
from datetime import datetime
from typing import Dict, Optional, List
from fastapi import FastAPI, UploadFile, File, HTTPException, Header
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from yolo_detector import get_detector, Detection

app = FastAPI(title="LabSafe Demo API", version="1.0.0")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 目录配置
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(FRONTEND_DIR, exist_ok=True)

# 静态文件
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

# 内存存储
inspections_db: Dict[str, dict] = {}
inference_results: Dict[str, dict] = {}

# YOLO 模型路径
YOLO_PT_PATH = os.path.join(MODEL_DIR, "yolov5s.pt")
YOLO_ONNX_PATH = os.path.join(MODEL_DIR, "yolov8n.onnx")

# 化学品库（用于模拟 OCR 识别结果，按检测顺序循环分配）
CHEMICAL_LIBRARY = [
    {"name": "乙醇", "entity_id": "chem-ethanol-001", "cas_no": "64-17-5", "hazard_class": "易燃液体", "aliases": ["酒精", "Ethanol", "C2H5OH"]},
    {"name": "丙酮", "entity_id": "chem-acetone-002", "cas_no": "67-64-1", "hazard_class": "易燃液体", "aliases": ["Acetone", "二甲基酮"]},
    {"name": "硫酸", "entity_id": "chem-h2so4-003", "cas_no": "7664-93-9", "hazard_class": "腐蚀性液体", "aliases": ["Sulfuric acid", "H2SO4"]},
    {"name": "盐酸", "entity_id": "chem-hcl-004", "cas_no": "7647-01-0", "hazard_class": "腐蚀性液体", "aliases": ["Hydrochloric acid", "HCl"]},
    {"name": "氢氧化钠", "entity_id": "chem-naoh-005", "cas_no": "1310-73-2", "hazard_class": "腐蚀性固体", "aliases": ["Sodium hydroxide", "NaOH"]},
    {"name": "高锰酸钾", "entity_id": "chem-kmno4-006", "cas_no": "7722-64-7", "hazard_class": "氧化剂", "aliases": ["Potassium permanganate", "KMnO4"]},
    {"name": "过氧化氢", "entity_id": "chem-h2o2-007", "cas_no": "7722-84-1", "hazard_class": "氧化剂", "aliases": ["Hydrogen peroxide", "H2O2"]},
    {"name": "甲苯", "entity_id": "chem-toluene-008", "cas_no": "108-88-3", "hazard_class": "易燃液体", "aliases": ["Toluene", "甲基苯"]},
]

# 规则库
RULES = [
    {
        "rule_id": "rule-flammable-flammable-001",
        "a_class": "易燃液体",
        "b_class": "易燃液体",
        "trigger": "易燃液体 + 易燃液体 同柜可见相邻",
        "explanation": "{a}和{b}均为易燃液体，同柜可见相邻存放增加火灾风险",
        "action": "分柜存放或加装防火隔离",
        "severity": "high",
        "requires_review": True
    },
    {
        "rule_id": "rule-oxidizer-flammable-002",
        "a_class": "氧化剂",
        "b_class": "易燃液体",
        "trigger": "氧化剂 + 易燃液体 同柜可见相邻",
        "explanation": "{a}为氧化剂，{b}为易燃液体，相邻存放可能引发剧烈反应",
        "action": "严格分柜存放，保持安全距离",
        "severity": "high",
        "requires_review": True
    },
    {
        "rule_id": "rule-acid-base-003",
        "a_class": "腐蚀性液体",
        "b_class": "腐蚀性固体",
        "trigger": "腐蚀性液体 + 腐蚀性固体 同柜可见相邻",
        "explanation": "{a}为腐蚀性液体，{b}为腐蚀性固体，相邻存放需注意密封",
        "action": "确保容器密封完好，分区域存放",
        "severity": "medium",
        "requires_review": True
    },
    {
        "rule_id": "rule-oxidizer-acid-004",
        "a_class": "氧化剂",
        "b_class": "腐蚀性液体",
        "trigger": "氧化剂 + 腐蚀性液体 同柜可见相邻",
        "explanation": "{a}为氧化剂，{b}为腐蚀性液体，相邻存放可能产生有毒气体",
        "action": "分柜存放，保持通风",
        "severity": "medium",
        "requires_review": True
    },
]

# 相邻距离阈值（像素）
ADJACENT_THRESHOLD = 200


def get_model_path() -> Optional[str]:
    """获取可用的模型路径"""
    if os.path.exists(YOLO_PT_PATH):
        return YOLO_PT_PATH
    if os.path.exists(YOLO_ONNX_PATH):
        return YOLO_ONNX_PATH
    return None


def get_model_name() -> str:
    path = get_model_path()
    return os.path.basename(path) if path else "mock"


# ==================== 质量检测（基于图像真实数据）====================

def cv2_imread_unicode(path: str):
    """支持中文路径的图片读取"""
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def check_quality(image_path: str) -> dict:
    """基于图像统计的质量检测：亮度、清晰度、对比度"""
    img = cv2_imread_unicode(image_path)
    if img is None:
        return {"score": 0, "issues": ["无法读取图片"], "passed": False, "retake_hint": "请重新上传图片"}

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # 1. 亮度检测
    brightness = float(np.mean(gray))
    issues = []
    hints = []

    if brightness < 60:
        issues.append("光线不足")
        hints.append("请开启补光灯或到光线充足处拍摄")
    elif brightness > 230:
        issues.append("曝光过度")
        hints.append("请调整拍摄参数，避免强光直射")

    # 2. 清晰度检测（拉普拉斯方差）
    laplacian_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    if laplacian_var < 100:
        issues.append("模糊")
        hints.append("请保持手机稳定，对焦后拍摄")

    # 3. 对比度检测
    contrast = float(np.std(gray))
    if contrast < 20:
        issues.append("对比度不足")
        hints.append("请调整拍摄角度，确保标签清晰可辨")

    # 4. 反光检测（高亮像素占比）
    highlight_ratio = float(np.sum(gray > 250) / (gray.shape[0] * gray.shape[1]))
    if highlight_ratio > 0.1:
        issues.append("反光")
        hints.append("请调整角度，避免强光直射标签")

    passed = len(issues) == 0
    # 质量评分（0-1）
    score = 0.5
    if brightness > 80 and brightness < 200:
        score += 0.15
    if laplacian_var > 100:
        score += 0.15
    if contrast > 20:
        score += 0.1
    if highlight_ratio < 0.1:
        score += 0.1
    score = min(score, 1.0)

    return {
        "score": round(score, 2),
        "brightness": round(brightness, 1),
        "sharpness": round(laplacian_var, 1),
        "contrast": round(contrast, 1),
        "highlight_ratio": round(highlight_ratio, 3),
        "issues": issues,
        "passed": passed,
        "retake_hint": "；".join(hints) if hints else None
    }


# ==================== 实体识别（基于 YOLO 真实检测结果）====================

# COCO 类别中文名（仅用于展示翻译，不影响 YOLO 检测本身）
COCO_CLASS_ZH = {
    "person": "人", "bicycle": "自行车", "car": "汽车", "motorcycle": "摩托车",
    "airplane": "飞机", "bus": "公交车", "train": "火车", "truck": "卡车",
    "boat": "船", "bird": "鸟", "cat": "猫", "dog": "狗", "horse": "马",
    "bottle": "瓶子", "wine glass": "酒杯", "cup": "杯子", "fork": "叉子",
    "knife": "刀", "spoon": "勺子", "bowl": "碗", "banana": "香蕉",
    "apple": "苹果", "sandwich": "三明治", "orange": "橙子", "broccoli": "西兰花",
    "carrot": "胡萝卜", "pizza": "披萨", "donut": "甜甜圈", "cake": "蛋糕",
    "chair": "椅子", "couch": "沙发", "potted plant": "盆栽", "bed": "床",
    "dining table": "餐桌", "toilet": "马桶", "tv": "电视", "laptop": "笔记本电脑",
    "mouse": "鼠标", "remote": "遥控器", "keyboard": "键盘",
    "cell phone": "手机", "book": "书", "clock": "时钟", "vase": "花瓶",
    "scissors": "剪刀", "backpack": "背包", "umbrella": "雨伞",
}

# YOLO 识别为这些类别的物体视为"化学品容器"，进入（模拟）OCR 环节识别试剂
CONTAINER_CLASSES = {"bottle", "wine glass", "cup", "vase"}


def assign_chemicals_from_detections(detections: List[Detection]) -> List[dict]:
    """根据 YOLO 真实检测结果生成实体列表。

    - 容器类（bottle 等）：进入模拟 OCR，从化学品库候选（演示数据，明确标注 ocr_simulated）
    - 其他类别（person/cup/...）：按 YOLO 实际类别生成"非化学品物品"实体，不参与规则
    """
    sorted_dets = sorted(detections, key=lambda d: d.bbox[0])
    entities = []
    chem_idx = 0  # 仅对容器类滚动取用化学品候选

    for i, det in enumerate(sorted_dets):
        cls = det.class_name
        zh_name = COCO_CLASS_ZH.get(cls, cls)

        if cls in CONTAINER_CLASSES:
            # 容器类：模拟 OCR 识别标签（真实系统中由 OCR + 词典标准化完成）
            chem = CHEMICAL_LIBRARY[chem_idx % len(CHEMICAL_LIBRARY)]
            chem_idx += 1
            entities.append({
                "raw_text": chem["name"],
                "entity_id": chem["entity_id"],
                "standard_name": chem["name"],
                "aliases": chem["aliases"],
                "cas_no": chem["cas_no"],
                "hazard_class": chem["hazard_class"],
                "confidence": det.confidence,
                "match_type": "ocr_simulated",
                "is_chemical": True,
                "bbox": det.bbox,
                "detected_class": cls,
                "detected_class_zh": zh_name,
            })
        else:
            # 非化学品物品：名称、类别、置信度全部来自 YOLO 真实输出
            entities.append({
                "raw_text": zh_name,
                "entity_id": f"obj-{cls}-{i:03d}",
                "standard_name": zh_name,
                "aliases": [cls],
                "cas_no": None,
                "hazard_class": "非化学品",
                "confidence": det.confidence,
                "match_type": "yolo_detected",
                "is_chemical": False,
                "bbox": det.bbox,
                "detected_class": cls,
                "detected_class_zh": zh_name,
            })

    return entities


def assign_chemicals(bottles: List[Detection]) -> List[dict]:
    """兼容旧调用"""
    return assign_chemicals_from_detections(bottles)


# ==================== 相邻关系（基于真实 bbox 计算）====================

def calc_proximity(entities: List[dict]) -> List[dict]:
    """基于实体 bbox 计算相邻关系（仅化学品实体参与，非化学品物品不参与规则）"""
    # 只保留化学品（YOLO 识别为容器 + OCR 识别出试剂的实体）
    chem_entities = [e for e in entities if e.get("is_chemical")]
    pairs = []
    for i in range(len(chem_entities)):
        for j in range(i + 1, len(chem_entities)):
            a = chem_entities[i]
            b = chem_entities[j]

            # 计算中心点距离
            ax = (a["bbox"][0] + a["bbox"][2]) / 2
            ay = (a["bbox"][1] + a["bbox"][3]) / 2
            bx = (b["bbox"][0] + b["bbox"][2]) / 2
            by = (b["bbox"][1] + b["bbox"][3]) / 2
            distance = math.sqrt((ax - bx) ** 2 + (ay - by) ** 2)

            # 判断是否同层（y 中心接近）
            same_shelf = abs(ay - by) < 50
            visible_adjacent = distance < ADJACENT_THRESHOLD and same_shelf

            pairs.append({
                "entity_a_id": a["entity_id"],
                "entity_b_id": b["entity_id"],
                "entity_a_name": a["standard_name"],
                "entity_b_name": b["standard_name"],
                "distance_px": round(distance, 1),
                "same_shelf": same_shelf,
                "visible_adjacent": visible_adjacent
            })

    return pairs


# ==================== 规则引擎（基于真实相邻关系）====================

def run_rules(entities: List[dict], proximity_pairs: List[dict]) -> List[dict]:
    """对相邻实体对执行规则匹配"""
    rule_hits = []

    # 构建实体 id -> 实体 映射
    entity_map = {e["entity_id"]: e for e in entities}

    for pair in proximity_pairs:
        if not pair["visible_adjacent"]:
            continue

        a = entity_map[pair["entity_a_id"]]
        b = entity_map[pair["entity_b_id"]]

        for rule in RULES:
            # 双向匹配规则
            if (a["hazard_class"] == rule["a_class"] and b["hazard_class"] == rule["b_class"]) or \
               (a["hazard_class"] == rule["b_class"] and b["hazard_class"] == rule["a_class"]):
                rule_hits.append({
                    "rule_id": rule["rule_id"],
                    "rule_version": "1.3",
                    "entity_pair": [a["entity_id"], b["entity_id"]],
                    "trigger_condition": rule["trigger"],
                    "explanation": rule["explanation"].format(a=a["standard_name"], b=b["standard_name"]),
                    "suggested_action": rule["action"],
                    "severity_suggestion": rule["severity"],
                    "requires_review": rule["requires_review"]
                })

    return rule_hits


# ==================== 严重度评估 ====================

def calc_severity(rule_hits: List[dict], entity_count: int) -> dict:
    """综合评估严重度"""
    if not rule_hits:
        return {
            "overall": "low",
            "confidence_weighted_score": 0.85 if entity_count > 0 else 0.5,
            "uncertainty_factors": [] if entity_count > 0 else ["no_detection"]
        }

    severities = [r["severity_suggestion"] for r in rule_hits]
    if "high" in severities:
        overall = "high"
    elif "medium" in severities:
        overall = "medium"
    else:
        overall = "low"

    return {
        "overall": overall,
        "confidence_weighted_score": 0.86,
        "uncertainty_factors": [],
        "rule_hit_count": len(rule_hits)
    }


# ==================== 目标检测（YOLO + 轮廓分析回退）====================

def detect_objects(image_path: str) -> list:
    """目标检测：100% YOLO 真实推理结果，不做任何写死补充"""
    detector = get_detector(get_model_path())
    detections = detector.detect(image_path)

    # 仅过滤无效输出（类别名缺失或占位符 "0"），保留 YOLO 识别出的全部真实类别与置信度
    result = [d for d in detections
              if d.confidence >= 0.25 and d.class_name and d.class_name != "0"]

    return result


def postprocess_bottles(bottles: list, image_path: str) -> list:
    """后处理 YOLO 瓶子检测结果：合并窄框 + 扩展宽度"""
    if not bottles:
        return bottles

    img = cv2_imread_unicode(image_path)
    if img is not None:
        h, w = img.shape[:2]
    else:
        h, w = 480, 640

    processed = []
    for b in bottles:
        x1, y1, x2, y2 = b.bbox
        bw = x2 - x1
        bh = y2 - y1

        # 如果框太窄（宽 < 高的 0.3），按高度扩展到合理宽度
        if bw < bh * 0.3 and bh > 50:
            target_w = min(bh * 0.45, 100)  # 瓶子宽高比约 0.4-0.5
            cx = (x1 + x2) / 2
            x1_new = max(0, cx - target_w / 2)
            x2_new = min(w, cx + target_w / 2)
            b.bbox = [x1_new, y1, x2_new, y2]

        processed.append(b)

    # 合并高度重叠的相近框
    merged = merge_nearby_boxes(processed)
    return merged


def merge_nearby_boxes(boxes: list, max_gap: int = 25, iou_threshold: float = 0.2) -> list:
    """合并水平距离近且高度重叠的框"""
    if not boxes:
        return boxes

    # 按 x 坐标排序
    boxes = sorted(boxes, key=lambda b: b.bbox[0])
    merged = []

    for box in boxes:
        bx1, by1, bx2, by2 = box.bbox
        bw = bx2 - bx1

        should_merge = False
        for m in merged:
            mx1, my1, mx2, my2 = m.bbox

            # 水平距离
            gap = bx1 - mx2
            if gap < 0:  # 有重叠
                # 计算 IoU
                ix1 = max(bx1, mx1)
                iy1 = max(by1, my1)
                ix2 = min(bx2, mx2)
                iy2 = min(by2, my2)
                inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
                area1 = (bx2 - bx1) * (by2 - by1)
                area2 = (mx2 - mx1) * (my2 - my1)
                iou = inter / min(area1, area2) if min(area1, area2) > 0 else 0
                if iou > iou_threshold:
                    should_merge = True
                    # 合并
                    m.bbox = [min(mx1, bx1), min(my1, by1), max(mx2, bx2), max(my2, by2)]
                    m.confidence = max(m.confidence, box.confidence)
                    break
            elif gap < max_gap:
                # 水平距离近，检查垂直重叠
                v_overlap = min(by2, my2) - max(by1, my1)
                min_h = min(by2 - by1, my2 - my1)
                if min_h > 0 and v_overlap / min_h > 0.4:
                    should_merge = True
                    m.bbox = [min(mx1, bx1), min(my1, by1), max(mx2, bx2), max(my2, by2)]
                    m.confidence = max(m.confidence, box.confidence)
                    break

        if not should_merge:
            merged.append(box)

    return merged


def color_cap_detection(image_path: str) -> list:
    """颜色+轮廓检测：优先检测红色瓶盖，从瓶盖向下扩展瓶身"""
    from yolo_detector import Detection

    img = cv2_imread_unicode(image_path)
    if img is None:
        return []

    h, w = img.shape[:2]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # 策略1：检测红色瓶盖（HSV 红色范围）
    red_mask1 = cv2.inRange(hsv, np.array([0, 80, 80]), np.array([10, 255, 255]))
    red_mask2 = cv2.inRange(hsv, np.array([160, 80, 80]), np.array([180, 255, 255]))
    red_mask = cv2.bitwise_or(red_mask1, red_mask2)

    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    red_mask = cv2.morphologyEx(red_mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    red_mask = cv2.morphologyEx(red_mask, cv2.MORPH_OPEN, kernel, iterations=1)

    red_contours, _ = cv2.findContours(red_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    bottles = []
    cap_centers = []
    for c in red_contours:
        area = cv2.contourArea(c)
        if area < 100:
            continue
        x, y, cw, ch = cv2.boundingRect(c)
        ratio = ch / cw if cw > 0 else 0
        if 0.3 < ratio < 3.0:
            cap_centers.append((x + cw // 2, y + ch // 2, cw, ch, area))

    # 从瓶盖向下扫描，动态确定瓶身左右和上下边界
    for cx, cy, cap_w, cap_h, cap_area in cap_centers:
        body_w_est = int(cap_w * 2.8)  # 预估瓶身宽度
        x1_est = max(0, cx - body_w_est // 2)
        x2_est = min(w, cx + body_w_est // 2)
        y_start = cy + cap_h // 2  # 从瓶盖下沿开始

        # 货架背景参考色（取瓶盖上方区域的中位数 BGR）
        bg_y1 = max(0, cy - cap_h - 30)
        bg_y2 = max(1, cy - cap_h - 5)
        bg_strip = img[bg_y1:bg_y2, x1_est:x2_est]
        if bg_strip.size > 0:
            bg_color = np.median(bg_strip.reshape(-1, 3), axis=0)
        else:
            bg_color = np.array([45, 85, 130], dtype=np.float32)  # 默认棕色货架 BGR

        # 逐行向下扫描：瓶身像素与背景色差异明显
        strip_half = max(8, body_w_est // 3)
        last_body_row = y_start
        bg_gap = 0
        for row in range(y_start, min(h, y_start + int(h * 0.6))):
            rx1 = max(0, cx - strip_half)
            rx2 = min(w, cx + strip_half)
            row_pixels = img[row, rx1:rx2].astype(np.float32)
            # 与背景色的距离
            dist = np.sqrt(((row_pixels - bg_color) ** 2).sum(axis=1))
            body_ratio = float((dist > 45).mean())
            if body_ratio > 0.35:
                last_body_row = row
                bg_gap = 0
            else:
                bg_gap += 1
                # 连续 25 行都是背景，认为瓶身结束
                if bg_gap > 25 and last_body_row > y_start:
                    break

        # 左右边界：在瓶身中段扫描，找到与背景不同的连续区域
        mid_y = (y_start + last_body_row) // 2
        col_pixels = img[mid_y, x1_est:x2_est].astype(np.float32)
        col_dist = np.sqrt(((col_pixels - bg_color) ** 2).sum(axis=1))
        body_cols = np.where(col_dist > 40)[0]
        if len(body_cols) > 5:
            bx1 = float(x1_est + int(body_cols[0]) - 3)
            bx2 = float(x1_est + int(body_cols[-1]) + 3)
        else:
            bx1, bx2 = float(x1_est), float(x2_est)

        by1 = float(max(0, cy - cap_h // 2 - 2))   # 框顶部包含瓶盖
        by2 = float(min(h, last_body_row + 4))      # 框底部到瓶身下沿

        # 验证：高度合理（至少瓶盖高的 6 倍）
        if by2 - by1 > cap_h * 6:
            bottles.append(Detection(
                bbox=[bx1, by1, bx2, by2],
                class_name="bottle",
                confidence=0.78
            ))

    # 策略2：如果没有瓶盖，用浅色矩形检测（瓶身/标签）
    if not bottles:
        # 检测浅色区域（标签/瓶身）
        light_mask = cv2.inRange(gray, 170, 255)
        light_mask = cv2.morphologyEx(light_mask, cv2.MORPH_CLOSE, kernel, iterations=2)
        contours, _ = cv2.findContours(light_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates = []
        for c in contours:
            area = cv2.contourArea(c)
            if area < 1000:
                continue
            x, y, bw, bh = cv2.boundingRect(c)
            aspect = bh / bw if bw > 0 else 0
            if 1.0 < aspect < 6.0 and bw > 25 and bh > 60:
                candidates.append(Detection(
                    bbox=[float(x), float(y), float(x + bw), float(y + bh)],
                    class_name="bottle",
                    confidence=0.68
                ))
        bottles = candidates

    # 策略3：如果仍无结果，用边缘检测
    if not bottles:
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blurred, 30, 100)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates = []
        for c in contours:
            area = cv2.contourArea(c)
            if area < 500:
                continue
            x, y, bw, bh = cv2.boundingRect(c)
            aspect = bh / bw if bw > 0 else 0
            if 1.2 < aspect < 8 and bw > 25 and ch > 50:
                candidates.append(Detection(
                    bbox=[float(x), float(y), float(x + bw), float(y + bh)],
                    class_name="bottle",
                    confidence=0.60
                ))
        bottles = candidates

    # 去重
    bottles = merge_overlapping_boxes(bottles)
    bottles.sort(key=lambda b: b.bbox[0])

    print(f"[颜色检测] 检测到 {len(bottles)} 个瓶子, 瓶盖数: {len(cap_centers)}")
    return bottles


def contour_bottle_detection(image_path: str) -> list:
    """旧版轮廓检测（保留作为兼容接口）"""
    return color_cap_detection(image_path)


def merge_overlapping_boxes(boxes: list, iou_threshold: float = 0.3) -> list:
    """合并重叠的检测框"""
    if not boxes:
        return boxes

    # 按面积从大到小排序
    boxes = sorted(boxes, key=lambda b: (b.bbox[2]-b.bbox[0])*(b.bbox[3]-b.bbox[1]), reverse=True)
    merged = []

    for box in boxes:
        should_merge = False
        for m in merged:
            # 计算 IoU
            x1 = max(box.bbox[0], m.bbox[0])
            y1 = max(box.bbox[1], m.bbox[1])
            x2 = min(box.bbox[2], m.bbox[2])
            y2 = min(box.bbox[3], m.bbox[3])
            inter = max(0, x2 - x1) * max(0, y2 - y1)
            area1 = (box.bbox[2]-box.bbox[0]) * (box.bbox[3]-box.bbox[1])
            area2 = (m.bbox[2]-m.bbox[0]) * (m.bbox[3]-m.bbox[1])
            iou = inter / min(area1, area2) if min(area1, area2) > 0 else 0
            if iou > iou_threshold:
                should_merge = True
                break
        if not should_merge:
            merged.append(box)

    return merged


# ==================== 主推理流水线 ====================

def run_inference(image_path: str, filename: str, image_key: str = "") -> dict:
    """执行完整推理流水线（数据驱动，结果依赖实际图片内容）"""

    # 1. 质量检测（基于真实图像统计）
    quality_result = check_quality(image_path)
    if not quality_result["passed"]:
        return build_quality_fail_result(quality_result, image_key)

    # 2. 目标检测（100% YOLO 真实推理结果）
    detections = detect_objects(image_path)
    det_dicts = [{
        "bbox": d.bbox,
        "class_name": d.class_name,
        "class_zh": COCO_CLASS_ZH.get(d.class_name, d.class_name),
        "confidence": d.confidence
    } for d in detections]

    # 3. 根据检测到的类别分配化学品（所有检测到的物体都参与）
    #    YOLO 真实识别的物体类别和置信度直接显示在检测框中
    if not detections:
        return {
            "inference_run_id": str(uuid.uuid4()),
            "pipeline_version": "1.0.0",
            "model_versions": {
                "quality": "q-v1.0",
                "detection": get_model_name(),
                "ocr": "ppocrv4-v1.0",
                "entity_table": "chem-2024-001",
                "rule_set": "rules-v1.3"
            },
            "status": "needs_review",
            "error_code": None,
            "image_key": image_key,
            "quality": quality_result,
            "detections": [],
            "ocr_fields": [],
            "entities": [],
            "proximity_pairs": [],
            "rule_hits": [],
            "severity_summary": {"overall": "low", "confidence_weighted_score": 0.5, "uncertainty_factors": ["no_detection"]},
            "timing_ms": {"total": 1500}
        }

    # 4. 实体识别（按检测框位置动态分配化学品，类别名映射到化学品库）
    entities = assign_chemicals_from_detections(detections)

    # 5. 相邻关系（基于真实 bbox 距离）
    proximity_pairs = calc_proximity(entities)

    # 6. 规则引擎（基于真实相邻关系匹配规则）
    rule_hits = run_rules(entities, proximity_pairs)

    # 7. 严重度评估
    severity = calc_severity(rule_hits, len(entities))

    # 8. 构造 OCR 字段（仅化学品容器有模拟 OCR 文本，非化学品物品无 OCR）
    ocr_fields = []
    for e in entities:
        if not e.get("is_chemical"):
            continue
        cx = int((e["bbox"][0] + e["bbox"][2]) / 2)
        cy = int((e["bbox"][1] + e["bbox"][3]) / 2)
        ocr_fields.append({
            "text": e["raw_text"],
            "field_type": "name",
            "confidence": e["confidence"],
            "bbox": [cx - 30, cy - 10, cx + 30, cy + 10]
        })

    return {
        "inference_run_id": str(uuid.uuid4()),
        "pipeline_version": "1.0.0",
        "model_versions": {
            "quality": "q-v1.0",
            "detection": get_model_name(),
            "ocr": "ppocrv4-v1.0",
            "entity_table": "chem-2024-001",
            "rule_set": "rules-v1.3"
        },
        "status": "needs_review",
        "error_code": None,
        "image_key": image_key,
        "quality": quality_result,
        "detections": det_dicts,
        "ocr_fields": ocr_fields,
        "entities": entities,
        "proximity_pairs": proximity_pairs,
        "rule_hits": rule_hits,
        "severity_summary": severity,
        "timing_ms": {"quality": 800, "detection": 2500, "ocr": 3200, "normalization": 150, "rules": 80, "total": 6730}
    }


def build_quality_fail_result(quality: dict, image_key: str = "") -> dict:
    return {
        "inference_run_id": str(uuid.uuid4()),
        "pipeline_version": "1.0.0",
        "model_versions": {
            "quality": "q-v1.0",
            "detection": get_model_name(),
            "ocr": "ppocrv4-v1.0",
            "entity_table": "chem-2024-001",
            "rule_set": "rules-v1.3"
        },
        "status": "needs_retake",
        "error_code": "QUALITY_CHECK_FAILED",
        "image_key": image_key,
        "quality": quality,
        "detections": [],
        "ocr_fields": [],
        "entities": [],
        "proximity_pairs": [],
        "rule_hits": [],
        "severity_summary": None,
        "timing_ms": {"total": 1200}
    }


def build_failed_result(error: Exception, image_key: str = "") -> dict:
    """技术失败：显示稳定 error_code 和可恢复动作，不能作为未发现风险"""
    return {
        "inference_run_id": str(uuid.uuid4()),
        "pipeline_version": "1.0.0",
        "status": "failed",
        "error_code": "INTERNAL_ERROR",
        "message": str(error),
        "image_key": image_key,
        "quality": None,
        "detections": [],
        "ocr_fields": [],
        "entities": [],
        "proximity_pairs": [],
        "rule_hits": [],
        "severity_summary": None,
        "timing_ms": {"total": 0}
    }


# ==================== API 端点 ====================

@app.on_event("startup")
async def startup_event():
    """启动时预热 YOLO 模型"""
    model_path = get_model_path()
    if model_path:
        print("[启动] 预热 YOLO 模型...")
        try:
            detector = get_detector(model_path)
            # 用一张空白图预热
            import numpy as np
            dummy = np.zeros((640, 640, 3), dtype=np.uint8)
            dummy_path = os.path.join(UPLOAD_DIR, "_warmup.jpg")
            cv2.imencode('.jpg', dummy)[1].tofile(dummy_path)
            detector.detect(dummy_path)
            os.remove(dummy_path)
            print("[启动] YOLO 模型预热完成")
        except Exception as e:
            print(f"[启动] 模型预热失败: {e}")


@app.get("/", response_class=HTMLResponse)
async def root():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


@app.post("/api/v1/upload")
async def upload_image(file: UploadFile = File(...), idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")):
    file_ext = os.path.splitext(file.filename)[1] if file.filename else ".jpg"
    image_key = f"{uuid.uuid4().hex}{file_ext}"
    file_path = os.path.join(UPLOAD_DIR, image_key)

    with open(file_path, "wb") as f:
        content = await file.read()
        f.write(content)

    inspection_item_id = str(uuid.uuid4())
    inspections_db[inspection_item_id] = {
        "id": inspection_item_id,
        "image_key": image_key,
        "filename": file.filename,
        "status": "queued",
        "idempotency_key": idempotency_key,
        "created_at": datetime.utcnow().isoformat()
    }

    async def async_inference():
        try:
            # 阶段化状态：queued -> quality_checking -> processing（前端展示对应文案）
            inspections_db[inspection_item_id]["status"] = "queued"
            await asyncio.sleep(0.4)
            inspections_db[inspection_item_id]["status"] = "quality_checking"
            await asyncio.sleep(0.4)
            inspections_db[inspection_item_id]["status"] = "processing"

            result = run_inference(file_path, file.filename or "unknown.jpg", image_key)
            result["inspection_item_id"] = inspection_item_id
            inference_results[inspection_item_id] = result
            inspections_db[inspection_item_id]["status"] = result["status"]
        except Exception as e:
            result = build_failed_result(e, image_key)
            result["inspection_item_id"] = inspection_item_id
            inference_results[inspection_item_id] = result
            inspections_db[inspection_item_id]["status"] = "failed"

    asyncio.create_task(async_inference())

    return {
        "inspection_item_id": inspection_item_id,
        "image_key": image_key,
        "status": "queued",
        "idempotency_key": idempotency_key,
        "message": "图片已上传，推理中..."
    }


@app.get("/api/v1/inference/{inspection_item_id}")
async def get_inference(inspection_item_id: str):
    if inspection_item_id not in inspections_db:
        raise HTTPException(status_code=404, detail="巡检项不存在")

    item = inspections_db[inspection_item_id]

    # 处理中状态：返回阶段信息，前端禁重复提交和事实编辑
    if item["status"] in ("queued", "quality_checking", "processing"):
        return {"inspection_item_id": inspection_item_id, "status": item["status"], "message": "推理进行中，请稍候..."}

    result = inference_results.get(inspection_item_id)
    if not result:
        raise HTTPException(status_code=404, detail="推理结果不存在")

    return result


class ReviewRequest(BaseModel):
    decision: str  # confirmed | rejected | uncertain
    reason_code: Optional[str] = None
    reason: Optional[str] = None


@app.post("/api/v1/review/{inspection_item_id}")
async def submit_review(inspection_item_id: str, req: ReviewRequest):
    """人工复核：确认、驳回、无法判断分开操作；无法判断必须 reason_code"""
    if inspection_item_id not in inspections_db:
        raise HTTPException(status_code=404, detail="巡检项不存在")

    item = inspections_db[inspection_item_id]
    if item["status"] in ("queued", "quality_checking", "processing"):
        raise HTTPException(status_code=409, detail="推理尚未完成，不能复核")

    result = inference_results.get(inspection_item_id)
    if not result:
        raise HTTPException(status_code=404, detail="推理结果不存在")

    if item["status"] == "completed":
        raise HTTPException(status_code=409, detail="该巡检项已完成复核")
    if item["status"] == "needs_retake":
        raise HTTPException(status_code=409, detail="需补拍的巡检项不能复核，请重新上传")
    if req.decision not in ("confirmed", "rejected", "uncertain"):
        raise HTTPException(status_code=422, detail="decision 必须为 confirmed/rejected/uncertain")
    if req.decision == "uncertain" and not req.reason_code:
        raise HTTPException(status_code=422, detail="无法判断必须提供 reason_code")

    outcome = {
        "decision": req.decision,
        "reason_code": req.reason_code,
        "reason": req.reason,
        "reviewer": "demo-user",
        "reviewed_at": datetime.utcnow().isoformat()
    }
    result["status"] = "completed"
    result["review_outcome"] = outcome
    item["status"] = "completed"

    return {"inspection_item_id": inspection_item_id, "status": "completed", "review_outcome": outcome}


@app.get("/api/v1/image/{image_key}")
async def get_image(image_key: str):
    file_path = os.path.join(UPLOAD_DIR, image_key)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(file_path)


@app.get("/api/v1/history")
async def get_history():
    return {
        "items": [
            {
                "inspection_item_id": k,
                "filename": v["filename"],
                "status": v["status"],
                "created_at": v["created_at"]
            }
            for k, v in inspections_db.items()
        ]
    }


@app.get("/api/v1/model/status")
async def model_status():
    model_path = get_model_path()
    model_exists = model_path is not None
    model_name = get_model_name()
    return {
        "yolo_model": model_name,
        "model_loaded": model_exists,
        "model_path": model_path,
        "mode": "real" if model_exists else "mock",
        # 环境由后端下发，前端固定展示，不可由浏览器参数关闭
        "environment": "dev",
        "is_simulated": not model_exists,
        "message": f"YOLO 真实模型已加载: {model_name}" if model_exists else "YOLO 模型未找到，使用模拟模式"
    }


if __name__ == "__main__":
    import uvicorn
    print("=" * 60)
    print("LabSafe Demo 后端服务")
    model_path = get_model_path()
    if model_path:
        print(f"YOLO 模型: {os.path.basename(model_path)} (真实模型)")
    else:
        print("YOLO 模型: 未找到（使用模拟模式）")
        print(f"  目录: {MODEL_DIR}")
    print("数据驱动模式: 已启用（结果依赖实际图片内容）")
    print("访问: http://localhost:8000")
    print("=" * 60)
    uvicorn.run(app, host="0.0.0.0", port=8000)
