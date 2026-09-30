# 08.7 I-01A 工程骨架验收记录

日期：2026-09-28。范围：本次特性分支中的开发骨架，不是完整业务系统或生产发布验收。
运行时决策见 [ADR-PY-01](../02-architecture/07-python-runtime.md)，操作入口见 [开发指南](../../DEVELOPMENT.md)。

## 1. 六项修正关闭证据

| 问题 | 修正 | 可重验依据 |
|---|---|---|
| CI 静态检查失败 | 修正超长行，统一 Ruff 格式 | ruff check / ruff format --check；没有关闭原 lint 规则 |
| fixture 协议漂移 | 既有五个 AI 路由、请求/响应/错误 Schema；契约由同一生成器打包 | test_authenticated_health_ready_version_follow_contract、test_quality_and_runs_echo_identity_without_false_safe_result；build_specs --check |
| production/mock 与鉴权缺失 | 后端仅 dev/test、AI_MODE=mock；随机 token 文件、租户白名单；Web 拒绝生产配置 | test_invalid_configuration_is_rejected、test_all_contract_routes_require_token、test_production_cli_fails；前端生产构建反例 |
| 双 Python 环境不落实 | 统一 Python 3.11；保留业务与 AI 的依赖隔离；CI 分服务安装 | 干净 AI 环境不含 Celery/Redis/SQLAlchemy/真实模型包；独立 AI 套件通过 |
| 假框架掩盖运行错误 | 移除框架 fallback；使用真实 FastAPI/Celery；实际 API/AI HTTP 子进程 smoke | test_missing_frameworks_do_not_fall_back、test_api_starts_as_an_independent_http_process、test_standalone_ai_http_process |
| 虚拟环境未忽略 | 忽略 .venv*、工具缓存、.local-secrets、egg-info | git check-ignore 对虚拟环境、Ruff 缓存、令牌文件均匹配 |

另有并发、相同 attempt、超时释放、版本/哈希/对象引用、JSON规范化及运行依赖漂移检查。
日期校验显式安装 rfc3339-validator；缺少该能力立即失败，不能让 jsonschema 跳过 date-time 格式校验。

## 2. 本机实际执行结果

环境：Windows、Python 3.11.4、Node 24.21.0、npm 9.6.7；依赖安装在临时虚拟环境。
远程 CI 使用 Linux/Python 3.11、Node 20；其成绩以本 PR 的对应提交检查为准，不用本机成绩代替。

| 检查 | 本次结果 |
|---|---|
| python -m unittest discover -s tests -t . -v | 25 项通过；覆盖真实框架、HTTP 子进程、协议、启动保护和异常分支 |
| ruff check apps packages tests | 通过 |
| ruff format --check apps packages tests | 通过，34个Python文件 |
| 独立 AI 环境安装 requirements/dev-ai.txt，pip check | 通过；未安装 Celery、Redis、SQLAlchemy、Torch、Paddle、ORT |
| AI 单独套件 + 共用协议套件 | 干净环境15项AI测试和4项协议测试通过 |
| Python wheel构建与已安装包资源检查 | 通过；隔离导入可找到运行时契约和fixture资源，无需源码目录 |
| npm ci、npm run build（dev） | 通过；npm 安装时审计报告 0 vulnerabilities，不代表持续安全保证 |
| production Web 构建 | 按预期失败，没有生成可用生产模式 |
| build_specs.py --check | 通过，16 个生成物；其中运行时包契约来自同一源 |
| test_readiness_design.py / validate_specs.py | 通过；148 IRR + 41 发布 + 39 D-FINE 合成设计检查 |

设计检查输出只描述该工具调用的证据范围；其中“No application runtime tests”不能被读成仓库没有应用测试。
本页的应用验证与设计合成验证分别记录，不能相互替代。

## 3. 明确未完成的后续阶段

以下保留 2026-09-28 的历史边界；2026-09-29 后续实现状态见 [I-01B 验收](08-i01b-acceptance.md)，不回写当时的验收结论。

- I-01B/C：专用 MySQL、43 表 Alembic 迁移、仓储、会话、RBAC、幂等与租户事务。
- I-02/I-03：实际巡检命令、Outbox/inbox、Worker→AI→DB、租约/fencing/恢复。
- I-03：真实 spawn 计算子进程、硬超时 kill/join 与故障重建；I-01A仅有可取消的合成延迟。
- I-ML：D-FINE-N、PP-OCRv4 CPU、真实图像/数据/权重、CUDA 推理和完整运行时锁。
- I-04/I-05/R：业务前端、真实对象存储权限/签名、Compose、备份恢复和人工批准。

当前 fixture 不下载或验证图片字节，不执行真实质量算法；其分数、空事实和耗时都是合成值。
API /ready 只表明骨架进程配置有效；worker --check 不验证 broker 连通，也不能视为完整异步链路。
不得据此激活生产模型、宣称准确率/GPU性能达标，或把本阶段自审等同于协作者批准。

## 4. 提交与回滚

本次提交特性分支并创建 PR，base=main；不直接 push main、不自动合并。
ADR、启动保护和开发 token 边界变更由协作者按 CONTRIBUTING 评审，未代替任何人签署。
没有生产数据迁移；如需回滚，revert 本 PR 的实现提交即可，不改写已发布历史。
