# LabSafe 开发指南（I-01A / I-01B）

## 1. 本阶段边界

所有 Python 服务统一使用 Python 3.11.x；业务与 AI 使用独立虚拟环境和独立进程。
依据见 [运行时决策](docs/02-architecture/07-python-runtime.md)。前端 CI 使用 Node.js 20。

已提供真实 FastAPI/Celery 入口、同协议开发 fixture、配置保护，以及 I-01B 初始数据库迁移、租户初始化和真实 MySQL 测试。
不包含用户会话、RBAC/仓储、业务 API、持久任务、真实图像处理、训练权重或生产部署。
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

### 3.5 数据库迁移与初始化（I-01B）

先在独立的开发 MySQL 8.0.16+ 实例创建专用空库 labsafe_dev_local。
由数据库管理员提供仅对该库具有 SELECT/INSERT/UPDATE/DELETE/CREATE/ALTER/DROP/INDEX/REFERENCES 权限的迁移/初始化账户；不要给 API/Worker 或 AI 使用该管理账户。
本阶段只允许本机回环连接；远端 TLS、应用最小权限账户和生产发布不在 I-01B 范围内。

将管理员提供的 mysql+pymysql 连接 URL 保存到 .local-secrets/database-url，包含用户名、URL 编码后的密码、回环地址、端口和准确的库名。
URL 不允许 query 参数；程序只读取 DATABASE_URL_FILE，不读取明文 DATABASE_URL，也不自动加载 .env。
该文件必须仅当前用户可读；不要把真实连接串放在命令行、终端历史、日志、PR 或文档中。

~~~powershell
$env:APP_ENV = 'dev'
$env:DATABASE_SCHEMA = 'labsafe_dev_local'
$env:DATABASE_URL_FILE = (Resolve-Path .local-secrets/database-url).Path
.\.venv-business\Scripts\python.exe -m packages.persistence.cli upgrade
.\.venv-business\Scripts\python.exe -m packages.persistence.cli verify
.\.venv-business\Scripts\python.exe -m packages.persistence.cli bootstrap-tenant --code my_org --name '机构名称' --timezone Asia/Shanghai --username admin --display-name '初始管理员' --reason '首次初始化'
~~~

最后一步交互式输入并确认 12–128 字符密码，不提供项目默认密码。
非交互环境使用 --password-file 指向一次性 UTF-8 秘密文件：内容就是密码，不要额外添加 BOM 或结尾换行，程序不会 trim/规范化密码；成功后由提供该文件的操作者安全移除。
用户名执行 NFKC+casefold；租户、六角色、租户级 safety_admin 授权与脱敏审计在一个事务中提交。
重复初始化同一租户返回错误，绝不重置已有密码；本命令不是登录接口，也不是管理员重置工具。
upgrade 成功不等于结构正确，必须继续 verify；JSON 中 errors 应为空，退出码为 0。
CLI 退出码：0=成功，2=输入/保护检查失败，1=数据库/迁移/结构检查失败；错误不输出连接串或密码。

初始迁移仅允许空库（可以已有 Alembic 版本表）；不使用 stamp 绕过保护。
MySQL DDL 非整体事务：中途失败可能遗留部分表，不能承诺自动回滚。
此时保留失败证据，并由操作者确认后丢弃整个专用测试库重建；不要对已有业务库执行重建。
降级会删除全部 43 张业务表，只能在 APP_ENV=test 且 DATABASE_SCHEMA 为 labsafe_test_ 前缀的可丢弃库执行：

~~~powershell
# 仅在已确认的专用测试库配置中执行，绝不能指向业务/开发数据库。
$env:LABSAFE_ALLOW_DESTRUCTIVE_DOWNGRADE = $env:DATABASE_SCHEMA
.\.venv-business\Scripts\python.exe -m packages.persistence.cli downgrade
Remove-Item Env:LABSAFE_ALLOW_DESTRUCTIVE_DOWNGRADE
~~~

本机自动验证不需要配置或连接已有库：

~~~powershell
.\.venv-business\Scripts\python.exe tools/database/run_mysql_tests.py --mysqld 'D:/SQL/MySQL/MySQL Server 8.0/bin/mysqld.exe'
~~~

把路径替换为实际 mysqld 可执行文件。脚本创建临时 data directory、随机回环端口和随机凭据，核对实例身份后才建库；结束后只关闭本次子进程并清理临时数据。
不注册或修改 Windows 服务。Linux 本机模式需已安装 mysqld，并以允许运行 mysqld 的非 root 用户执行；CI 使用隔离 MySQL 容器模式，不依赖本机服务。

## 4. 验证

根目录、业务环境执行：

~~~powershell
.\.venv-business\Scripts\python.exe -m unittest discover -s tests/business -t . -v
.\.venv-business\Scripts\python.exe -m unittest discover -s tests/protocol -t . -v
.\.venv-business\Scripts\python.exe -m pytest tests/persistence/test_unit.py --tb=short
.\.venv-business\Scripts\python.exe -m ruff check apps packages tests tools/database
.\.venv-business\Scripts\python.exe -m ruff format --check apps packages tests tools/database
~~~

AI 环境执行：

~~~powershell
.\.venv-ai\Scripts\python.exe -m unittest discover -s tests/ai -t . -v
.\.venv-ai\Scripts\python.exe -m unittest discover -s tests/protocol -t . -v
~~~

I-01A 的业务/AI/协议测试自行在临时目录生成令牌和合成请求，不要求预设 APP_ENV，也不连接 Redis/MySQL/对象存储。
包含实际 API/AI 子进程 HTTP smoke，不只是 import 非空检查。
I-01B 的真实数据库测试必须运行 3.5 的隔离启动器；直接 pytest tests/persistence 会跳过未配置的 MySQL 用例，不能把跳过算作数据库通过。

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
I-01B 的实际结果与后续边界见 [验收记录](docs/08-delivery/08-i01b-acceptance.md)。I-01C 会话、RBAC、幂等与租户事务实现及验收范围见 [I-01C 验收记录](docs/08-delivery/09-i01c-acceptance.md)。
I-01A PR #5 已合并；I-01B 在 wsq/i-01b-database-foundation 上通过 PR #7 提交，目标为最新 main。数据库/安全变更仍需按 CONTRIBUTING 独立评审；不直接推送 main。
PR #7 的远程 CI 必须运行其自身的 persistence-mysql、业务、AI、设计和 Web 检查，不能用 PR #5 的成绩代替。
下一阶段 I-01C 是仓储、会话/RBAC、幂等与租户事务；真实模型主线可按 I-ML-01 推进，但不以数据库通过替代模型或生产验收。
