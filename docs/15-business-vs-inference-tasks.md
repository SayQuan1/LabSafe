# 15 业务端与推理端任务清单

> 基于 docs/01~14 文档整理，按"业务端"与"推理端"两大责任域划分。

---

## 一、总体责任边界

```
AI/推理端   = 事实与候选输出，不负责最终安全结论
规则端     = 专家批准规则的可解释计算，不负责派发任务
业务端     = 权限、人工确认、状态机、审计和报表
安全人员   = 最终风险确认与复查销项
```

---

## 二、推理端要做的事

### 2.1 模型流水线建设

#### 2.1.1 流水线总览

```text
┌─────────────┐   ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
│  质量检测    │ → │  目标检测    │ → │ 标签预处理   │ → │    OCR      │
│ image_quality│   │  detection  │   │ label_crop  │   │    ocr      │
└─────────────┘   └─────────────┘   └─────────────┘   └─────────────┘
                                                          ↓
┌─────────────┐   ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
│  严重度建议   │ ← │  规则引擎    │ ← │ 相邻关系构建  │ ← │ 化学品标准化 │
│ severity    │   │compatibility│   │  proximity  │   │entity_norm  │
└─────────────┘   └─────────────┘   └─────────────┘   └─────────────┘
```

**执行约束：**
- 质量检测失败 → 直接返回 `needs_retake`，不进入后续模块
- OCR 失败 → 允许人工录入，确认前不执行相容性判断
- 无法标准化 → 不得执行相容性结论
- 每个模块输出必须带置信度，低置信度结果进入待确认队列

---

#### 2.1.2 模块详细设计

##### 模块 1：质量检测（image_quality）

| 项 | 内容 |
|---|---|
| **输入** | 原图（MinIO 对象键）、图片元数据（尺寸、方向、压缩信息） |
| **输出** | `quality_score` (0-1)、`issues[]`（模糊/反光/倾斜/遮挡）、`passed` (bool)、`retake_hint` |
| **阈值** | 模糊：Laplacian 方差 < 100；反光：高光区域占比 > 15%；倾斜：角度偏差 > 15° |
| **性能目标** | P95 ≤ 5 秒 |
| **失败处理** | 返回 `needs_retake` + 具体补拍提示（如"请减少反光""请正对标签"） |
| **版本追踪** | `quality_model_version`、阈值参数哈希 |

##### 模块 2：目标检测（detection）

| 项 | 内容 |
|---|---|
| **输入** | 原图（质量检测通过后）、检测类别清单 |
| **输出** | `detections[]`：每个含 `bbox` (x1,y1,x2,y2)、`class`（试剂瓶/标签/柜/货架/相邻容器）、`confidence` |
| **模型** | YOLOv8 / RT-DETR 基线，输入尺寸 640×640 |
| **性能目标** | GPU P95 ≤ 3 秒；CPU P95 ≤ 15 秒 |
| **MVP 类别** | `bottle`（试剂瓶）、`label`（标签区域）、`shelf`（货架/柜）、`container`（相邻容器） |
| **后处理** | NMS IoU=0.45；置信度阈值 0.25；同类别重叠框合并 |
| **版本追踪** | `detection_model_version`、`confidence_threshold`、`nms_iou` |

##### 模块 3：标签预处理（label_crop）

| 项 | 内容 |
|---|---|
| **输入** | 原图、`detections` 中所有 `label` 类目标框 |
| **输出** | `cropped_labels[]`：每个含 `crop_image_key`（MinIO）、`source_bbox`、`perspective_corrected` (bool) |
| **处理** | 按标签框裁剪 → 透视校正（四点变换）→ 分辨率归一化（高度 64px，宽度等比） |
| **失败处理** | 透视校正失败时退化为直接裁剪，标记 `perspective_corrected=false` |
| **版本追踪** | `preprocess_version` |

##### 模块 4：OCR（ocr）

| 项 | 内容 |
|---|---|
| **输入** | `cropped_labels[]` 中的裁剪图 |
| **输出** | `ocr_fields[]`：每个含 `text`（原文）、`bbox`、`confidence`、`field_type`（名称/日期/批号/其他） |
| **模型** | PaddleOCR 中文基线（ch_PP-OCRv4） |
| **字段抽取** | 正则 + 规则匹配：名称（化学品词典）、日期（YYYY-MM-DD / YYYY/MM/DD / 有效期至）、批号（Lot/Batch 编号） |
| **置信度策略** | 字段级置信度 = OCR 字符置信度 × 字段匹配置信度 |
| **失败处理** | OCR 整体失败 → 标记 `ocr_status=failed`，允许人工录入 |
| **版本追踪** | `ocr_model_version`、字段抽取规则版本 |

##### 模块 5：化学品实体标准化（entity_normalization）

| 项 | 内容 |
|---|---|
| **输入** | `ocr_fields` 中的 `field_type=名称` 文本 |
| **输出** | `entities[]`：每个含 `raw_text`、`entity_id`、`standard_name`、`aliases`、`cas_no`、`hazard_class`、`confidence`、`match_type`（精确/别名/模糊/人工确认） |
| **匹配层级** | 1. 精确匹配（标准名/CAS）→ 2. 别名匹配 → 3. 编辑距离模糊匹配（阈值 0.8）→ 4. 多候选/低置信度 → 待确认 |
| **待确认策略** | 低置信度（< 0.7）或多个候选时，不输出相容性结论，进入人工确认队列 |
| **数据来源** | 化学品实体表（内部维护），含标准名、别名、CAS、危险类别、存储要求 |
| **版本追踪** | `entity_table_version`、`match_rule_version` |

##### 模块 6：相邻关系构建（proximity）

| 项 | 内容 |
|---|---|
| **输入** | `detections`（所有目标框）、`entities`（已标准化实体） |
| **输出** | `proximity_pairs[]`：每对含 `entity_a_id`、`entity_b_id`、`distance_px`、`same_shelf` (bool)、`visible_adjacent` (bool) |
| **判定规则** | 同一货架/柜内 + 可见相邻（中心距 < 阈值，无遮挡物） |
| **阈值** | 同货架：IoU > 0 或中心距 < 200px（640 图）；可见相邻：中间无 `container` 类遮挡 |
| **版本追踪** | `proximity_rule_version` |

##### 模块 7：规则引擎（compatibility_check）

| 项 | 内容 |
|---|---|
| **输入** | `proximity_pairs`、`entities`、`rule_set_version` |
| **输出** | `rule_hits[]`：每个含 `rule_id`、`rule_version`、`entity_pair`、`trigger_condition`、`explanation`、`suggested_action`、`severity_suggestion`、`requires_review` |
| **规则格式** | JSON Schema：`{id, version, name, condition: {hazard_class_a, hazard_class_b, relationship}, action, severity, explanation_template}` |
| **执行策略** | 对每对 `visible_adjacent` 的实体执行规则匹配；无命中 → 记录"规则缺失/待确认" |
| **人工复核** | 所有 `severity=high` 的命中必须 `requires_review=true` |
| **版本追踪** | `rule_set_version`、规则命中日志 |

##### 模块 8：严重度建议（severity）

| 项 | 内容 |
|---|---|
| **输入** | `rule_hits`、`entities`、`confidence_scores` |
| **输出** | `severity`（high/medium/low）、`confidence_weighted_score`、`uncertainty_factors[]` |
| **计算逻辑** | 基础严重度（规则定义）× 实体匹配置信度 × OCR 置信度 × 检测置信度 |
| **不确定性因子** | 低 OCR 置信度、模糊匹配、多候选实体、遮挡场景 |
| **输出约束** | 严重度建议仅供人工参考，不得作为最终结论 |

---

#### 2.1.3 统一输出契约（InferenceRun）

```json
{
  "inference_run_id": "uuid",
  "inspection_item_id": "uuid",
  "pipeline_version": "1.0.0",
  "model_versions": {
    "quality": "q-v1.0",
    "detection": "yolov8n-v1.2",
    "ocr": "ppocrv4-v1.0",
    "entity_table": "chem-2024-001",
    "rule_set": "rules-v1.3"
  },
  "status": "succeeded | needs_retake | failed",
  "error_code": null,
  "quality": {
    "score": 0.92,
    "issues": [],
    "passed": true
  },
  "detections": [
    {
      "bbox": [120, 80, 200, 300],
      "class": "bottle",
      "confidence": 0.94
    },
    {
      "bbox": [125, 85, 195, 160],
      "class": "label",
      "confidence": 0.91
    }
  ],
  "ocr_fields": [
    {
      "text": "乙醇",
      "field_type": "name",
      "confidence": 0.96,
      "bbox": [130, 90, 190, 110]
    },
    {
      "text": "2025-01-15",
      "field_type": "date",
      "confidence": 0.88,
      "bbox": [130, 115, 190, 135]
    }
  ],
  "entities": [
    {
      "raw_text": "乙醇",
      "entity_id": "chem-ethanol-001",
      "standard_name": "乙醇",
      "cas_no": "64-17-5",
      "hazard_class": "易燃液体",
      "confidence": 0.98,
      "match_type": "exact"
    }
  ],
  "proximity_pairs": [
    {
      "entity_a_id": "chem-ethanol-001",
      "entity_b_id": "chem-acetone-002",
      "distance_px": 45,
      "same_shelf": true,
      "visible_adjacent": true
    }
  ],
  "rule_hits": [
    {
      "rule_id": "rule-flammable-oxidizer-001",
      "rule_version": "1.3",
      "entity_pair": ["chem-ethanol-001", "chem-acetone-002"],
      "trigger_condition": "易燃液体 + 易燃液体 同柜可见相邻",
      "explanation": "乙醇和丙酮均为易燃液体，同柜存放增加火灾风险",
      "suggested_action": "分柜存放或加装防火隔离",
      "severity_suggestion": "high",
      "requires_review": true
    }
  ],
  "severity_summary": {
    "overall": "high",
    "confidence_weighted_score": 0.87,
    "uncertainty_factors": ["ocr_date_confidence_0.88"]
  },
  "timing_ms": {
    "quality": 1200,
    "detection": 2800,
    "ocr": 3500,
    "normalization": 150,
    "rules": 80,
    "total": 7730
  }
}
```

---

#### 2.1.4 性能与降级策略

| 场景 | 策略 |
|---|---|
| GPU 可用 | 完整流水线，目标 P95 ≤ 30 秒 |
| CPU 降级 | 降低输入分辨率至 320×320，批大小=1，Worker 并发 1~2；优先保留质量检查、OCR、规则匹配；非核心类别暂停 |
| 质量检测失败 | 立即返回 `needs_retake`，不占用后续计算资源 |
| OCR 失败 | 标记 `ocr_status=failed`，允许人工录入，跳过标准化和规则检查 |
| 标准化低置信度 | 进入待确认队列，不执行相容性结论 |
| 规则无命中 | 记录"规则缺失/待确认"，不报错 |

---

#### 2.1.5 演示 Demo

已提供可运行的模拟流水线脚本：[demo_pipeline.py](file:///d:/研究生/ai算法/LabSafe/docs/demo_pipeline.py)

**运行方式：**

```bash
python docs/demo_pipeline.py
```

**演示场景：**

| 场景 | 输入 | 预期结果 |
|---|---|---|
| 正常流程 | 乙醇与丙酮同柜相邻 | 命中规则 `rule-flammable-flammable-001`，严重度 `high`，要求人工复核 |
| 质量拦截 | 模糊图片 | 状态 `needs_retake`，返回补拍提示"请保持手机稳定，对焦后拍摄" |
| 无规则命中 | 单独存放的硫酸 | 无规则命中，严重度 `low` |

**Demo 输出示例（场景 1）：**

```json
{
  "status": "succeeded",
  "entities": [
    {"standard_name": "乙醇", "hazard_class": "易燃液体", "match_type": "exact"},
    {"standard_name": "丙酮", "hazard_class": "易燃液体", "match_type": "exact"}
  ],
  "rule_hits": [{
    "rule_id": "rule-flammable-flammable-001",
    "explanation": "乙醇和丙酮均为易燃液体，同柜可见相邻存放增加火灾风险",
    "severity_suggestion": "high",
    "requires_review": true
  }],
  "severity_summary": {"overall": "high", "confidence_weighted_score": 0.86}
}
```

### 2.2 数据采集与标注

| 任务 | 说明 | 参考文档 |
|---|---|---|
| 采集规范制定 | 按模板逐项拍摄，记录实验室、区域/柜号、时间、采集人 | 06 |
| 标注规范制定 | 目标框、OCR 文字框、风险三态（risk / safe_observed / cannot_determine） | 06 |
| 困难样本池 | 建立 300~500 个反光、遮挡、小目标、模糊样本池 | 06 |
| 数据切分 | 按实验室、采集批次和场景分组切分 70/15/15，测试集冻结后不回流训练 | 06 |
| 人脸脱敏 | 训练前对非必要人脸自动打码或裁剪 | 06 |

### 2.3 评测与质量保障

| 任务 | 说明 | 参考文档 |
|---|---|---|
| 分阶段评测集 | 第 1-3 天冻结最小基线集，第 4-7 天临时门槛，第 8-10 天冻结候选验收集 | 02、14 |
| 模型指标 | 检测：mAP、召回率；OCR：字符/字段准确率；风险：高风险召回率、误报率、无法判断率 | 06 |
| 版本追踪 | 模型版本、阈值、数据集版本、代码提交号写入 `InferenceRun` | 06 |
| 困难样本导出 | 支持批量困难样本导出用于迭代 | 14 |

### 2.4 推理服务工程化

| 任务 | 说明 | 参考文档 |
|---|---|---|
| 独立进程/服务 | AI 推理作为独立进程运行，通过版本化 HTTP/gRPC 契约与业务层通信 | 03、14 |
| 异步任务接入 | 接入 Celery + Redis 队列，支持 `image_quality`、`detection`、`ocr`、`entity_normalization`、`compatibility_check` 任务类型 | 03 |
| 幂等与重试 | 每个任务具备幂等键、最大重试次数、退避策略、死信记录和人工重放入口 | 14 |
| 超时控制 | 质量检查 P95 ≤5 秒；GPU 完整推理 P95 ≤30 秒；CPU 降级允许异步超时 | 14 |
| 资源隔离 | GPU 资源隔离、模型热更新、独立扩缩容和模型版本追踪 | 14 |
| 不可覆盖记录 | 推理结果写入不可覆盖的版本化记录 | 03 |

### 2.5 推理端交付物

- 可运行的推理服务进程（FastAPI/gRPC）
- 质量检测、检测、OCR、标准化、规则匹配各模块基线模型
- 化学品实体表与规则集（≥20 条）
- 评测脚本与冻结测试集
- 困难样本池与迭代报告
- 模型/规则版本登记与回滚方案

---

## 三、业务端要做的事

### 3.1 核心领域服务

| 模块 | 任务 | 参考文档 |
|---|---|---|
| 认证与权限 | 登录、角色、资源范围、会话管理；支持租户（学校）-学院-实验室层级 | 14 |
| 实验室管理 | 实验室建档、归档、授权；区域、货架/柜号管理 | 04、14 |
| 模板管理 | 巡检模板发布、停用、复制；检查项定义 | 04 |
| 巡检管理 | 创建巡检、检查项、图片元数据、采集状态流转 | 04、14 |
| 复核服务 | Finding 确认、修改、驳回、无法判断；保存复核记录和原因 | 04、05 |
| 整改服务 | 派发、接收、提交证据、复查、销项；逾期派生状态 | 04、14 |
| 报表服务 | 聚合统计、CSV/PDF 导出、证据索引 | 04 |
| 审计服务 | 全量状态变更记录、操作者、时间、前后值 | 04、14 |

### 3.2 异步任务编排

| 任务 | 说明 | 参考文档 |
|---|---|---|
| 队列管理 | Celery + Redis 队列搭建，任务状态追踪（queued/running/succeeded/failed/retrying/cancelled） | 05 |
| 任务编排 | 上传后按序触发质量检测 → 检测 → OCR → 标准化 → 规则检查 | 03 |
| 状态机同步 | 推理完成后将结果写回业务层，驱动 InspectionItem 和 Finding 状态流转 | 14 |
| 失败处理 | 质量失败 → `needs_retake`；推理失败 → 稳定错误码 + 重试入口 | 05 |

### 3.3 API 层

| 任务 | 说明 | 参考文档 |
|---|---|---|
| REST API | 实现 `/api/v1` 下全部端点（认证、实验室、模板、巡检、上传、推理结果、复核、整改、规则、报表、审计） | 05 |
| 幂等设计 | 写操作要求 `Idempotency-Key` | 05 |
| 权限过滤 | 所有资源按权限过滤，禁止依赖前端隐藏按钮 | 05 |
| 错误格式 | 统一 `{code, message, details, request_id}` | 05 |
| 领域命令 | 状态变化只能通过领域命令接口，禁止通用 `PATCH status` | 05 |

### 3.4 前端（Web + 移动端）

| 端 | 任务 | 参考文档 |
|---|---|---|
| 移动巡检端 | 登录、选择实验室/模板、检查项列表、拍摄/上传、质量提示、AI 结果复核、整改任务、复查拍摄 | 07 |
| Web 管理端 | 总览看板、实验室/人员/权限管理、巡检记录、风险问题、整改任务、规则与模型版本、报表导出、审计日志 | 07 |
| 交互规范 | 检查项状态显示（未开始/处理中/待复核/已完成/需补拍）；确认/驳回/无法判断/补拍为明确动作 | 07 |
| 错误处理 | 上传失败保留本地草稿；推理失败显示稳定错误码；无规则匹配显示"规则缺失/待确认" | 07 |
| 响应式 | 手机单手可达；处理中状态可离开页面异步等待 | 07 |

### 3.5 基础设施

| 任务 | 说明 | 参考文档 |
|---|---|---|
| 数据库 | MySQL 部署，核心业务表设计（含 `tenant_id`），JSON 字段证据存储 | 04、14 |
| 对象存储 | MinIO 部署，图片和报告存储，上传凭证签发 | 03、14 |
| 缓存/队列 | Redis 部署，Celery 队列和短期状态 | 03 |
| 认证 | 本地账号体系（首期），预留 OIDC/SAML 接口 | 14 |
| 可观测性 | 结构化日志 + OpenTelemetry + Prometheus；记录请求 ID、任务 ID、模型/规则版本、耗时 | 03 |
| 备份恢复 | RPO ≤24 小时，RTO ≤4 小时 | 14 |

### 3.6 业务端交付物

- 可运行的业务 API 服务（FastAPI）
- 响应式 Web 应用（移动巡检端 + Web 管理端）
- MySQL 数据库 Schema 与迁移脚本
- MinIO + Redis + Celery 基础设施
- 权限、审计、状态机核心逻辑
- 异步任务编排与监控
- 断网 Demo 环境（预置数据、模型、规则、账号）

---

## 四、业务端与推理端协作接口

| 接口 | 方向 | 内容 | 参考文档 |
|---|---|---|---|
| 上传触发 | 业务 → 推理 | 图片写入 MinIO 后，业务层创建异步推理任务 | 03 |
| 推理结果回传 | 推理 → 业务 | 版本化事实（质量、检测框、OCR 字段、标准化实体、规则命中）写入 `InferenceRun` | 03、04 |
| 人工复核 | 业务 → 推理 | 复核修改（字段修正）作为新输入重新触发推理（可选） | 05 |
| 规则发布 | 推理 → 业务 | 新规则版本发布事件 `RuleVersionPublished`，业务层用于新任务 | 14 |
| 质量拦截 | 推理 → 业务 | 质量检查失败直接返回 `needs_retake`，不进入后续模型 | 03 |

---

## 五、里程碑对照

| 里程碑 | 推理端关键交付 | 业务端关键交付 |
|---|---|---|
| M1 | 采集/标注规范确认；基线模型选型 | 合作实验室确认；验收口径冻结 |
| M2 | 首轮采集、标注；困难样本池初版 | MySQL/MinIO/Redis/API 骨架；模型登记 |
| M3 | 实体标准化；20+ 规则；测试集冻结 | 推理结果回传契约；复核界面初版 |
| M4 | 模型优化（反光/倾斜/小目标/遮挡） | 巡检-复核-整改-复查-销项闭环；异步推理接入 |
| M5 | 困难样本迭代；指标达标验证 | 端到端联调；性能/异常/权限测试；试运行 |
| M6 | 算法测试报告；模型/规则版本归档 | 业务对照报告；部署文档；Demo 脚本 |

---

## 六、不可妥协的边界

1. **AI 不输出最终安全结论** — 只输出事实和候选，必须人工确认
2. **规则不由大模型生成** — 首期不接入 LLM，规则由专家批准
3. **历史记录不可改写** — 规则/模型版本不可变，历史记录绑定原版本
4. **状态变更必须审计** — 所有状态转移走事务 + 审计事件
5. **权限不依赖前端** — 后端全量过滤，前端只做展示
