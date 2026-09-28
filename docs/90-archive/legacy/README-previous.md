# LabSafe 设计文档入口

## 规范性阅读顺序

开发、测试和部署只需要按以下顺序阅读：

1. [19-coding-baseline.md](19-coding-baseline.md)：唯一编码基线，冻结技术选型、状态机、数据约束、API 语义、任务、AI 边界、部署和验收。
2. [18-independent-ai-inference-process.md](18-independent-ai-inference-process.md)：独立 AI 进程的生命周期、模型加载、HTTP 调用、资源隔离和降级。
3. contracts/：机器可读的公共 API、AI API、Celery 消息和领域事件契约。
4. [20-design-audit.md](20-design-audit.md)：详细设计审计问题、裁决和编码前制品清单。

## 背景文档

01～13 和 14～17 保留产品需求、范围、架构决策、领域背景、Gate 评审和测试部署历史。它们不再作为实现时的独立规范；若与 19 冲突，以 19 和 contracts 为准。

## 开始编码前的检查

- 先校验四份契约文件并生成 API 客户端类型。
- 再执行 Alembic 初始迁移和 seed。
- 使用 AI mock server 跑通 Worker、Outbox、重试、死信和幂等。
- 接入真实模型前，登记 model manifest、冻结数据集和 smoke 图片。
- 进入 pilot 前，完成权限、备份恢复、断公网和性能门禁。
