# 02.1 系统边界

## 职责

Web负责交互；API负责认证授权、领域命令和查询；Worker负责持久任务、AI调用、规则、对象派生和落库；独立AI只产出观测；MySQL保存业务/任务真相；MinIO保存不可变版本对象；Redis是可重建传输。

## 流程

创建巡检 → 上传验证 → 质量检查 → 独立AI事实 → 规则候选 → 人工复核 → 派发整改 → 证据 → 独立复查 → 销项。零候选也需人工完成，技术失败/无法判断不等于安全。

## 禁止依赖

AI不访问MySQL、Redis、业务ORM、规则和通知；API不加载模型；浏览器不持有长期对象凭据、不复制业务状态机。原图/裁剪持久写入由Worker完成，AI只返回事实和配方。

## 规范入口

[编码与依赖方向](04-coding-baseline.md)、[领域](02-domain-model.md)、[独立AI](03-ai-inference-process.md)、[持久任务](05-durable-jobs.md)、[数据约束](06-data-persistence.md)。
