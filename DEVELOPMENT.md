# LabSafe 开发指南（I-01A）

## 1. 本阶段边界

所有 Python 服务统一使用 Python 3.11.x；业务与 AI 使用独立虚拟环境和独立进程。
依据见 [运行时决策](docs/02-architecture/07-python-runtime.md)。前端 CI 使用 Node.js 20。

本阶段提供真实 FastAPI/Celery 入口、同协议开发 fixture、配置保护和 CI。
不包含数据库迁移、用户会话、业务 API、持久任务、真实图像处理、训练权重或生产部署。
API 的 /ready 只检查 I-01A 进程配置，不代表数据库或完整系统已就绪。
所有后端进程和前端构建均拒绝 production；AI_MODE 只接受 mock。

## 2. 安装依赖

在仓库根目录执行。Windows 示例：

~~~powershell
py -3.11 -m venv .venv-business
.\.venv-business\Scripts\python.exe -m pip install -r requirements/dev.txt

py -3.11 -m venv .venv-ai
.\.venv-ai\Scripts\python.exe -m pip install -r requirements/dev-ai.txt
~~~

Linux 使用 python3.11 创建环境，并将 Scripts/python.exe 替换为 bin/python。
不需安装模型、CUDA、数据库驱动到 AI 环境；AI 只安装 common.txt 与测试依赖。
requirements 是顶层依赖约束，不是完整传递依赖锁或 GPU 兼容性证明。

## 3. 配置和启动

.env.example 是变量清单，当前程序不会自动读取 .env。请显式设置环境变量。
以下每个终端均从仓库根目录启动，不能把三个前台进程串在同一个终端等待。

### 3.1 API 终端

~~~powershell
$env:APP_ENV = 'dev'
.\.venv-business\Scripts\python.exe -m apps.api.run
~~~

API 监听 127.0.0.1:8000；GET /health 为存活，GET /ready 返回开发骨架就绪范围。
不提供假的登录、业务成功或数据库就绪结果。

### 3.2 AI 终端

首次创建本地随机令牌；文件在 .local-secrets/ 下，受 Git 忽略：

~~~powershell
.\.venv-ai\Scripts\python.exe -c "import pathlib,secrets; p=pathlib.Path('.local-secrets/ai-token'); p.parent.mkdir(exist_ok=True); p.write_text(secrets.token_urlsafe(32),encoding='ascii')"
$env:APP_ENV = 'dev'
$env:AI_MODE = 'mock'
$env:AI_TOKEN_FILE = (Resolve-Path .local-secrets/ai-token).Path
$env:AI_ALLOWED_TENANTS = '11111111-1111-4111-8111-111111111111'
$env:AI_FIXTURE_SCENARIO = 'no_targets'
.\.venv-ai\Scripts\python.exe -m apps.ai_inference.run
~~~

AI 监听 127.0.0.1:8001。五个接口均使用 /internal/inference/v1 前缀，并要求 Bearer 服务令牌：

| 方法 | 路径 | 开发行为 |
|---|---|---|
| GET | /health | supervisor HTTP 进程存活与当前 attempt |
| GET | /ready | 生命周期启动成功才 200；忙碌不等于未就绪 |
| GET | /version | development、is_simulated、fixture 身份与真实文件摘要 |
| POST | /quality | InferenceRequest → InferenceResult；通过或重拍分支 |
| POST | /runs | 同协议；无目标返回 needs_review，不冒充安全通过 |

不再提供 /fixture、裸 /version 或未鉴权的 AI /healthz。
请求和响应按生成的契约校验，不给 InferenceResult 私加 is_simulated 字段；模拟身份来自 /version，后续由 Worker 固化到业务 run。

AI_ALLOWED_TENANTS 必须是显式 UUID 清单；令牌为至少 32 字节的随机 ASCII 字符串。
请求模型/词典版本、实际 fixture 文件 SHA、设备 cpu、request_hash、图片关联与 analysis key 必须匹配。
fixture 不下载对象或读取图片；质量分数、空事实与阶段耗时均是合成值，不是性能或识别证据。

AI_FIXTURE_SCENARIO 可设 no_targets、needs_retake、error、timeout；AI_FIXTURE_DELAY_MS 范围 0–10000。
这些只能由开发者在服务启动环境配置，不能通过请求选择。
同一实例只接收一个在途请求；重复活动 attempt 返回 409，其他请求返回 429；超时返回 504 并释放容量。
当前模拟延迟可协作取消；真实 spawn 计算子进程及 kill/join 恢复在 I-03 实现，不能把本次 fixture 当成已完成的生产 supervisor。

开发 fixture 位于 apps/ai_inference/fixtures/：model.json、dictionary.json 均为小型合成文件；runtime-lock.json 校验 Python minor 及直接依赖版本。
该锁的 scope 明确为 fixture-direct-constraints-only，不声称传递依赖可复现，也不能用于真实模型激活。

### 3.3 Worker 终端

无需 Redis 的入口检查：

~~~powershell
$env:APP_ENV = 'dev'
.\.venv-business\Scripts\python.exe -m apps.worker.run --check
~~~

实际 Celery 队列运行按设计使用 Linux/WSL 的独立业务环境及 Redis：

~~~bash
APP_ENV=dev REDIS_URL=redis://localhost:6379/0 .venv-business/bin/python -m apps.worker.run
~~~

--check 只验证真实 Celery 配置与任务注册，不证明 Redis 连通或任务消费。
labsafe.fixture.echo 只是本地 smoke 任务，不是推理流程，不能用它绕过持久任务或写入成功状态。
完整 Worker→AI→数据库链路及 Outbox/inbox/fencing 在 I-03 落地。

### 3.4 Web 终端

~~~powershell
$env:APP_ENV = 'dev'
$env:VITE_APP_ENV = 'dev'
Set-Location apps/web
npm ci
npm run dev
~~~

两个环境变量必须相等且为 dev/test。开发页面始终显示“开发环境，结果不用于安全判断”。
构建命令是 npm run build；production 或不一致环境必须失败。

## 4. 验证

根目录、业务环境执行：

~~~powershell
.\.venv-business\Scripts\python.exe -m unittest discover -s tests/business -t . -v
.\.venv-business\Scripts\python.exe -m unittest discover -s tests/protocol -t . -v
.\.venv-business\Scripts\python.exe -m ruff check apps packages tests
.\.venv-business\Scripts\python.exe -m ruff format --check apps packages tests
~~~

AI 环境执行：

~~~powershell
.\.venv-ai\Scripts\python.exe -m unittest discover -s tests/ai -t . -v
.\.venv-ai\Scripts\python.exe -m unittest discover -s tests/protocol -t . -v
~~~

测试自行在临时目录生成令牌和合成请求，不要求预设 APP_ENV，也不连接 Redis/MySQL/对象存储。
包含实际 API/AI 子进程 HTTP smoke，不只是 import 非空检查。

设计校验使用独立工具环境，安装 tools/design/requirements.txt 后执行：

~~~powershell
python -B tools/design/build_specs.py --check
python -B tools/design/test_readiness_design.py
python -B tools/design/validate_specs.py
~~~

修改契约时只改 tools/design 的生成源，再运行 build_specs.py；同一生成器输出 contracts 和运行时 packages/inference_protocol/contract.json。
CI 必须先 --check，不能先生成来掩盖漂移。设计校验不是应用、模型或部署验收。

## 5. 提交和下一阶段

遵循 [Git 协作规范](CONTRIBUTING.md)。提交特性分支、创建 PR；不直接推送 main，不自行合并或代替独立审查。
不提交虚拟环境、.local-secrets、真实图片、权重或 .env。
下一阶段从 I-01B 专用空库迁移开始；真实模型主线可按 I-ML-01 并行，但不扩大本 PR 验收范围。
