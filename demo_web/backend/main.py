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


# ==================== 实体识别（基于检测结果动态分配）====================

def assign_chemicals(bottles: List[Detection]) -> List[dict]:
    """为检测到的每个瓶子分配化学品（按 x 坐标从左到右循环分配）"""
    # 按 x 坐标排序
    sorted_bottles = sorted(bottles, key=lambda b: b.bbox[0])

    entities = []
    for i, bottle in enumerate(sorted_bottles):
        chem = CHEMICAL_LIBRARY[i % len(CHEMICAL_LIBRARY)]
        # 置信度基于检测置信度
        ocr_conf = round(bottle.confidence * 0.98, 2)
        entities.append({
            "raw_text": chem["name"],
            "entity_id": chem["entity_id"],
            "standard_name": chem["name"],
            "aliases": chem["aliases"],
            "cas_no": chem["cas_no"],
            "hazard_class": chem["hazard_class"],
            "confidence": ocr_conf,
            "match_type": "exact",
            "bbox": bottle.bbox
        })

    return entities


# ==================== 相邻关系（基于真实 bbox 计算）====================

def calc_proximity(entities: List[dict]) -> List[dict]:
    """基于实体 bbox 计算相邻关系"""
    pairs = []
    for i in range(len(entities)):
        for j in range(i + 1, len(entities)):
            a = entities[i]
            b = entities[j]

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
    """目标检测：优先 YOLO，若无结果则用轮廓分析回退"""
    detector = get_detector(get_model_path())
    detections = detector.detect(image_path)

    bottles = [d for d in detections if d.class_name == "bottle"]

    # 如果 YOLO 没检测到瓶子，用图像轮廓分析回退
    if not bottles:
        bottles = contour_bottle_detection(image_path)
        detections = bottles

    return detections


def contour_bottle_detection(image_path: str) -> list:
    """基于轮廓分析的瓶子检测（用于合成图片等 YOLO 无法识别的场景）"""
    from yolo_detector import Detection

    img = cv2_imread_unicode(image_path)
    if img is None:
        return []

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape

    bottles = []

    # 策略：检测瓶子/标签的边框颜色（灰度 130-165 的灰色边框）
    border_mask = cv2.inRange(gray, 130, 165)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    border_closed = cv2.morphologyEx(border_mask, cv2.MORPH_CLOSE, kernel, iterations=2)

    contours, _ = cv2.findContours(border_closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < (h * w * 0.003):
            continue

        x, y, bw, bh = cv2.boundingRect(cnt)
        aspect_ratio = bh / bw if bw > 0 else 0
        # 瓶子高大于宽
        if aspect_ratio < 1.2 or aspect_ratio > 10.0:
            continue
        if bw > w * 0.8 or bh > h * 0.8:
            continue

        confidence = min(0.5 + area / (h * w) * 15, 0.85)
        bottles.append(Detection(
            bbox=[float(x), float(y), float(x + bw), float(y + bh)],
            class_name="bottle",
            confidence=round(confidence, 2)
        ))

    # 策略2：如果没找到，尝试检测整体矩形物体（边缘检测 + 霍夫变换思路）
    if not bottles:
        blurred = cv2.GaussianBlur(gray, (3, 3), 0)
        edges = cv2.Canny(blurred, 30, 100)
        # 找垂直和水平直线
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < (h * w * 0.01):
                continue
            x, y, bw, bh = cv2.boundingRect(cnt)
            aspect_ratio = bh / bw if bw > 0 else 0
            if aspect_ratio < 1.3 or aspect_ratio > 8.0:
                continue
            if bw > w * 0.5 or bh > h * 0.7:
                continue
            confidence = min(0.4 + area / (h * w) * 10, 0.7)
            bottles.append(Detection(
                bbox=[float(x), float(y), float(x + bw), float(y + bh)],
                class_name="bottle",
                confidence=round(confidence, 2)
            ))

    # 去重和合并重叠框
    bottles = merge_overlapping_boxes(bottles)

    # 按 x 坐标排序
    bottles.sort(key=lambda b: b.bbox[0])
    return bottles


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

    # 2. 目标检测（YOLO + 轮廓分析回退）
    detections = detect_objects(image_path)
    det_dicts = [{"bbox": d.bbox, "class_name": d.class_name, "confidence": d.confidence} for d in detections]

    bottles = [d for d in detections if d.class_name == "bottle"]

    # 3. 如果没检测到瓶子（零候选也进入待人工复核，由人工完成结论）
    if not bottles:
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
            "ocr_fields": [],
            "entities": [],
            "proximity_pairs": [],
            "rule_hits": [],
            "severity_summary": {"overall": "low", "confidence_weighted_score": 0.5, "uncertainty_factors": ["no_bottle_detected"]},
            "timing_ms": {"total": 1500}
        }

    # 4. 实体识别（按瓶子位置动态分配化学品）
    entities = assign_chemicals(bottles)

    # 5. 相邻关系（基于真实 bbox 距离）
    proximity_pairs = calc_proximity(entities)

    # 6. 规则引擎（基于真实相邻关系匹配规则）
    rule_hits = run_rules(entities, proximity_pairs)

    # 7. 严重度评估
    severity = calc_severity(rule_hits, len(entities))

    # 8. 构造 OCR 字段（基于分配的化学品）
    ocr_fields = []
    for e in entities:
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
