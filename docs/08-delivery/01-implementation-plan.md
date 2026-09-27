# 08.1 实施计划与首批任务

状态：设计已给出执行顺序；下列均为未来业务开发任务，不能写成已实现。阶段标准见[阶段门禁](05-stage-gates.md)。

## 1. 从“只有文档”开始

不要求用户先提供代码、训练后权重或运行报告。先创建受控开发环境和最小业务链路；真实模型/数据准备可以与业务代码并行，但任务仍按各自依赖验收。

| 编号 | 任务与输入 | 必须交付 | 完成检查 / 失败处理 |
|---|---|---|---|
| I-01A | 由编码基线创建apps/api、worker、ai_inference、web和共享包；固定APP_ENV=dev | 项目骨架、各进程入口、环境变量样例、顶层依赖约束、CI入口 | 空环境import/build通过；不装真实模型也能启动mock；失败先修环境不改业务协议 |
| I-01B | 设计DDL与字典；专用空MySQL数据库 | 初始Alembic迁移、bootstrap-tenant/roles CLI、DB结构核对测试 | 空库upgrade到head；43表/列/FK/索引与字典一致，实际版本记日志；失败在可丢弃测试库重建，不触碰用户已有库 |
| I-01C | 数据约束和安全规范 | 仓储、session/RBAC、幂等、跨租户拒绝测试 | DB-01/02、API-02/03、SEC-01/02；失败事务回滚且无旁路权限 |
| I-02 | 状态机与公共契约 | 巡检、上传、事实、人工复核、整改等命令/查询 | 每个guard有正/反例；相同payload重发无重复副作用 |
| I-03 | 持久任务设计 | Outbox/inbox/publisher/sweeper、Worker、fixture-v1 AI服务 | JOB-01/02/03故障注入；断Redis仍从DB恢复；不能用同步路由替代持久任务 |
| I-ML-01 | ML-BASE-02固定提交/配方，不依赖完整API或已训练项目权重 | D-FINE四类初始化开发checkpoint、数据映射/安全加载/qmax、固定ONNX导出、训练/CPU/CUDA lock和合成smoke | 分类头未训必须标development；核对RGB/W/H/300query及设备，不冒充实训效果；不兼容形成ADR |
| I-ML-02 | 数据来源授权、标注规范 | 授权台账、100张标注试行、完整数据/split/审核记录 | 类别和scene不泄漏、样本不足标待补充；无授权不抓取替代真实数据 |
| I-ML-03/04 | I-ML-01/02数据和导出原型、验收policy | D-FINE四类训练/校准、训练后50图PyTorch/ORT CPU/CUDA一致性、真实集成和资源报告 | AI-05至AI-09、MODEL-02；检测GPU/OCR CPU固定；不达标不发布，不把初始化模型或mock当生产模型 |
| I-04 | 稳定API和fixture服务 | 前端采集/复核/整改/admin/report，显式开发环境标识 | UI-01、EXPORT-01、FLOW闭环；旧轮询不能覆盖新run |
| I-05 | 已实现各服务 | Compose、secret挂载、探针、日志/指标、备份恢复脚本 | 隔离栈闭环；失败按服务/数据层定位，不改证据记录掩盖失败 |
| R-01 | 机构批准policy/规则、固定真实模型与test | 真实业务评测、恢复/断公网演练、双人发布记录 | EVAL/OPS/Security全部通过；缺批准或指标不足禁止production |

## 2. 数据库验证的精确任务边界

目标MySQL8.0.16+、InnoDB、UTC、utf8mb4_bin。先DDL空库检查，再迁移实现和数据不变量测试；两者使用明确标注可丢弃的独立测试schema，凭据来自测试secret。结果记录数据库版本、迁移head、列/default/index/FK差异、每个非法插入的预期错误、应用事务拒绝路径及日志。

测试至少包括：跨tenant FK、错item current pointer、角色scope重复、owner两列同时空/同时非空、多crop同图合法/同run同crop重复、旧lease回写拒绝。不能只看到CREATE TABLE成功就算通过。首个迁移尚无生产数据，可以在测试环境验证downgrade和重新upgrade；上线后优先备份与前向修复，不复用开发库“重建全部表”策略。

这些验证可在完整应用之前完成，但不要求在当前文档交付阶段先安装/启动数据库。

## 3. 模拟与真实环境

API /me返回environment；InferenceRun返回is_simulated。dev/test的所有页面显示“开发环境，结果不用于安全判断”，模拟run额外标记。fixture-v1是独立进程同协议实现，不是绕过Worker直写成功；保留延迟/超时/错误和质量分支。production启动前检查制品purpose/adapter/台账，任何mock均拒绝。

## 4. 任务完成的证据

IRR六项设计前置按[关闭清单](06-implementation-readiness.md)执行。任务必须认领JOB-04/FLOW-06/CAP-01/SEC-03/UP-03/AI-10，分别附事务、三值、容量、实际IAM、真实签名及图像/OCR黄金对照证据。设计reference不是可直接部署的业务服务，尤其不得把prefix测试当权限校验、把端点校验当签名验证。

任务PR包含：所实现契约/需求/测试ID、实际命令、环境版本、结果日志、未通过项及回滚方式；应用测试和模型评测分开。依赖lock、镜像digest、模型SHA、成绩只能从实施结果生成，不能由文档预填假值。文档阶段完成的是输入、顺序、算法和验收标准。

## 5. 协作和Push

当前保留原分支和用户已有暂存改动；本轮只改设计、契约和设计校验工具，不创建业务工程，不commit/push。设计文档可以先形成独立docs PR，不应等待完整系统上线。推送时再依CONTRIBUTING检查远程基线、变更范围、生成一致性和保密要求；数据库/权限/模型变更须双人评审，不能把本次自审当批准。
