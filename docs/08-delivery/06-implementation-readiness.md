# 08.6 实现就绪评审：六项关闭记录

日期：2026-09-27；对象：当前工作树的1.1.0候选设计。没有替协作者签署，没有真实模型、GPU或存储部署验收。本轮不重新选型或改变服务拓扑，只关闭IRR-01至IRR-06的编码歧义。

## 1. 结论与证据级别

六项均已在设计层落实唯一行为、契约/规范及合成回归。开发可以按修订基线进入实现；正式基线提交SHA和协作者批准仍属于治理待办，不虚构为本轮已取得。最新机器状态以[验证报告](design-validation-results.json)为准；报告须在本轮所有源/文档更新后重新生成。

本轮新增148项纯设计检查，完整验证还保留41项发布门禁、39项D-FINE语义检查。测试使用synthetic数据，不连接应用、MySQL、MinIO、GPU，不证明实际SigV4、nginx、Pillow/OpenCV/Paddle已运行。反例从“未定义行为”变为有明确错误、时机和原子边界的受测行为，不用扩大协议容量掩盖问题。

## 2. 可执行关闭清单

| 编号 / 设计状态 | 已确定行为 | 规范与当前证据 | 实施负责人角色 / 必须交付 |
|---|---|---|---|
| IRR-01 已关闭 | inference失败/租约回收原子回queued；run retrying；自动重试从quality开始；旧token/替代run不能回写；rule任务单独入口 | [持久任务](../02-architecture/05-durable-jobs.md)；参考测试首次/失败/回收/耗尽/过期/重放入口 | 后端；I-03实现JOB-04故障注入和事务断言 |
| IRR-02 已关闭 | same_location必填true/false/null；完成守卫按U/C互斥；confirmed+unknown仍cannot_determine，保留风险 | [领域状态机](../02-architecture/02-domain-model.md)、[事实映射](../04-ai-rules/01-data-and-rules.md)；Schema/null往返及完成真值表 | 领域/规则；FLOW-06，持久化/DTO/人工编辑全链路不强转null |
| IRR-03 已关闭 | 巡检乘积≤100；规则≤200；关系/OCR/日期/上下文/findings有预检和永久错误；无半份结果事件 | [容量表](../04-ai-rules/01-data-and-rules.md)；102巡检项、202findings反例及全上限边界 | 后端/AI；CAP-01，证明事务前拒绝和不截断 |
| IRR-04 已关闭 | S/O/A/D/R按身份最小权限；精确版本复制/读/删；cleanup独立secret且须DB引用守卫；原图不向浏览器泛授权 | [安全矩阵](../06-security/01-security-privacy.md)、[对象生命周期](../02-architecture/06-data-persistence.md)；允许/拒绝/越前缀合成矩阵 | 后端/运维；SEC-03，实际MinIO policy正反例，不授予全bucket管理 |
| IRR-05 已关闭 | 同源HTTPS443 path-style签名入口；内部client与签名client分离；Host/path/query不改；Content-Type签名、15MiB入口限制 | [部署3.1](../07-quality-operations/02-deployment-operations.md)；端点配置拒绝检查、代理/请求样例 | 运维/前端；UP-03真实浏览器成功/过期/篡改/超限/版本测试 |
| IRR-06 已关闭 | 固定质量像素算式/边界/取整、关系比例分母；OCR前缀/配对/跨行/日期/置信度/冲突聚合均有唯一规则 | [确定性事实规范](../04-ai-rules/03-fact-extraction-contract.md)；标量、几何、词法黄金样例 | AI/规则；AI-10真实图像库/Paddle行对照，输出变更重做校准 |

角色须在实现任务认领时登记真实姓名；不把文档作者自动当业务专家或批准人。表中的应用案例尚未实现，已执行证据仅是设计工具。

## 3. 首批实现与禁止事项

1. I-01A/B先建立工程/CI和隔离数据库迁移；提交公共客户端nullable类型生成结果。
2. I-01C/I-02/I-03按关闭后的guard/容量/三值实现；每PR附对应新增用例，不复用旧“item保持处理中再领取”逻辑。
3. I-ML-01保持D-FINE-N固定源、初始化导出原型；I-ML-04落地本次确定性事实算法和真实图像对照；不是另选模型。
4. I-05按权限矩阵生成IAM及nginx配置，实际运行SEC-03/UP-03；不能把reference.object_allowed当生产权限引擎。
5. R阶段继续真实数据/训练/评测、专家规则、恢复/断公网与独立批准，不因本轮关闭而自动放行production。

禁止将“没有训练模型/GPU成绩”重新登记为这六项的设计未决；也禁止将合成测试报告当作这些真实实现已经完成。

## 4. 重验命令和完成口径

~~~powershell
python -B tools/design/build_specs.py --check
python -B tools/design/test_readiness_design.py
python -B tools/design/validate_specs.py --report
git diff --check
git diff --cached --check
~~~

CI先--check，不先重新生成来掩盖漂移。修改生成源时本地先build_specs.py再执行上述检查。所有生成契约、主题文档、应用案例和追踪矩阵须一起提交；不只提交Schema。当前仍未执行git add/commit/push，也未变更既有暂存区。
