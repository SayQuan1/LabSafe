# 02.7 Python 运行时决策（ADR-PY-01）

日期：2026-09-28。依据：用户授权在双版本收益不足时统一 Python 3.11。
本决策随 I-01A 特性分支提交，仍需按协作规范独立评审；不是生产上线批准。

## 1. 决定

业务 API、Worker、AI 推理及离线训练均以 Python 3.11.x 为唯一 minor 基线。
替代原来的“业务 3.12、AI/训练 3.11”版本分离；不改变独立进程、HTTP 契约、服务权限或 GPU 边界。
业务与 AI 继续使用不同虚拟环境、不同依赖集合和后续独立镜像；统一解释器版本不等于合并环境。

## 2. 判断依据

1. 当前骨架没有依赖 Python 3.12 专属语法或 API。
2. 本次业务与 AI 顶层依赖已在本机 Python 3.11 验证安装和运行；CI 按 Python 3.11 分服务验收。
3. 现阶段双 minor 版本没有对应的已验证性能或功能收益，却增加环境说明、测试矩阵与共享序列化核对工作。
4. AI 原有 3.11 基线保持不变，不据此声称 D-FINE、CUDA、Paddle 的真实运行时已经验证。

上述为当前工程的取舍，不是 Python 3.11 比 3.12 更快的结论。完整传递依赖锁、镜像 digest 与真实硬件验证仍归 I-ML/I-05。

## 3. 实现约束

| 边界 | 约束 |
|---|---|
| 业务 | requirements/py311-business.txt；FastAPI/Celery/SQLAlchemy/Alembic |
| AI | requirements/py311-ai.txt；I-01A 无 Celery、Redis、业务 ORM 或真实模型包 |
| 共用部分 | common.txt 固定当前直接依赖；inference_protocol 只包含 wire schema、DTO 校验和规范哈希 |
| Python 版本 | pyproject 要求 >=3.11,<3.12；运行时显式校验 minor；CI 使用 3.11 |
| 模型 | D-FINE-N、检测 CUDA FP32 建议、PP-OCRv6_small ONNX CPU 及真实 CPU profile 均不改变 |
| 环境安全 | I-01A 仅 dev/test，AI_MODE=mock；生产与 real 启动拒绝 |

FastAPI/Celery 是必需运行依赖，缺失时必须失败，不在应用入口构造兼容外观的假框架。

## 4. 后续变更与回滚

后续若需要升级 Python，先在业务/AI 各自环境验证并提交新的运行时决策、依赖锁和测试结果，不能只修改镜像标签。
I-01A 没有数据库迁移和生产部署，回滚通过 revert 对应实现提交恢复文档基线；不删除审计记录、不改写 main 历史。
旧 3.12 文字仅保留在明确归档或本决策的历史说明中，现行操作命令不再引用 py312.txt。
