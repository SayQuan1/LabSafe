# 02.6 数据库与对象持久化

## 1. 可执行结构

完整列/空值/默认值/索引/唯一键/外键见 [数据字典](../../contracts/data-model.json)，建表顺序与约束见 [设计 DDL](../../contracts/database-design.sql)。生成源 tools/design/data_model.py。目标是 MySQL 8.0.16+、InnoDB、utf8mb4_bin、UTC；不是已经运行过的生产迁移。

DDL 先建表再加 FK 以解决 current pointer 环依赖。迁移使用 Alembic，先在空数据库执行并 introspection 对比字典，再测试同租户/跨租户数据；真实数据库未执行前只能报告静态验证。所有连接设置 session time_zone='+00:00'、严格 SQL mode；应用每次更新显式维护 updated_at/version。

## 2. 核心约束

- 租户表 UNIQUE(tenant_id,id)，关联尽量复合FK；全局只有 tenants、roles。所谓 global 安全规则在每租户有批准副本，不跨租户外键读取。
- user_roles 的 scope_key 由实验室或零UUID生成，防止 nullable laboratory_id 导致角色重复；scope_kind 与空值联动检查。
- image_derivatives 按 (tenant,run,crop) 唯一，同图可有多crop。
- item current_run/fact/evaluation 使用 (tenant,item,id) 复合引用，不能指到另一个item。
- uploads/images 的 inspection_item_id/remediation_task_id 恰一个非空；两者都通过 laboratory_id 复合FK验证。
- run_images 必须同 item，父图必须在同 run 清单；evidence_images 必须同 task。
- finding 对 evaluation/fact/run 的引用必须同时描述同一次链路；单独 FK 只保证同 item，进一步相等性由事务检查。

## 3. 必须由事务补充的约束

MySQL FK 不证明 JSON 内 IDs 或全部跨表条件，因此以下不是可省略的“应用自行处理”：

| 约束 | 执行位置 |
|---|---|
| template_item 属于 inspection.template_id；模板×location唯一且完整 | createInspection 同事务 |
| finding.run/fact 与 evaluation.run/fact 一致；task.lab 与 finding.item.lab一致 | 结果提交、派发事务 |
| run 同一 dictionary/model/activation 快照；entity ID属于字典 | submit、结果校验、facts编辑 |
| upload 和 asset_image 的owner、lab、hash完全相同 | completeUpload、验证提交 |
| 原图被覆盖或版本变化不可悄悄使用 | completeUpload 固定 versionId；验证精确读取版本 |
| role scope、assignee有效、最后一名管理员保护 | 用户/派发命令 |
| 文档 Schema 内引用 ID闭包，数组去重，坐标合法 | Pydantic后语义验证器 |
| image_delete 不能命中 run/evidence/dataset、legal_hold、正在验证对象 | 锁image检查所有引用；有引用409，不物理删历史证据 |

polymorphic resource_id 用于 audit/job/notification，不能建统一FK；根据 task_type/resource_type 白名单在事务中验证。同类资源删除后保留 tombstone，不用级联删除。

## 4. 存储映射

DTO与表不强求一一对应：Inspection.item_ids查item；Template.items查template_items；资源job_id通过task_type+logical_key查task_runs；Finding.task_id查唯一finding关联的task；ReportExport.error_code映射last_error_code；Image.owner_type/id由互斥owner列推导；ModelVersion设备/许可/数据集和InferenceRun.is_simulated从固定manifest读取；RuleVersion.rules返回definition内完整规则正文。Session.environment来自APP_ENV而非客户端。projection必须同租户/白名单，不能直接返回ORM __dict__。model_versions.evaluation_report_key允许未验证/dev时NULL，production的非空和批准匹配在validate/publish事务前置校验，不用假报告路径填数据库。

派生字段 allowed_actions 由与命令完全同源的guard计算，但前端收到允许动作不等于授权，提交仍重检。is_overdue 查询时用服务器时间；统计只计未 superseded 的当前 findings。

## 5. 原图和删除

grant 只允许 PUT staging/{tenant}/{upload_id}，10分钟、15MiB上限；MinIO启用versioning。complete HEAD固定对象version/hash；worker完整解码验证≤10000×10000且≤40M像素，生成独立analysis PNG。原图移动/复制到受控original前缀后记录version，staging由24小时生命周期清理，不授予浏览器读原图能力。

IRR-04：completeUpload用API内部HEAD取得准确versionId/大小/MIME，HEAD元数据声明的SHA不能代替内容校验；validating记录临时original_key/version为该staging版本。general Worker对这个固定版本流式计算SHA256并解码，匹配expected_sha256后复制到O、写A，原子将original/analysis准确key/version/hash及image ready登记。此前任何失败均不得ready；重复PUT生成新staging版本也不得改变已固定输入。对象调用在DB事务外，登记前重验owner和状态；未提交O/A视为孤儿。读取精确版本不存在则OBJECT_NOT_FOUND，不读最新版本顶替。

| 角色 | 规范key（仅服务端生成；ID与SHA已验证） |
|---|---|
| S | staging/{tenant}/{upload_id} |
| O | tenant/{tenant}/lab/{lab}/original/{image_id}/{sha}.{jpg或png或webp} |
| A | tenant/{tenant}/lab/{lab}/analysis/{image_id}/{sha}.png |
| D | tenant/{tenant}/lab/{lab}/derivatives/{run_id}/{crop_id}/{sha}.png |
| R | tenant/{tenant}/lab/{lab}/reports/{export_id}/{sha}.{csv或pdf} |

多实验室导出按排序最小lab_id放R路径；授权仍核完整export快照的所有lab，不能只核路径中的lab。清理只操作DB登记或受控孤儿清单的精确版本，不用前缀递归删。部署生命周期仅清理S；versioning桶须覆盖当前及非当前staging版本，并在UP-03验收，不能删业务证据。对象权限矩阵以安全规范4.1为准。

删除先事务标deleted+审计+cleanup job，下载立刻404；worker逐个 key+version删除，失败重试。历史引用图片返回409，不允许先标删再破坏证据。租户保留期默认业务证据365天、审计730天、导出文件24小时；仅无引用且超过保留期的对象可清理。legal_hold 优先于保留期。实际更长法规保留要求由部署方确定，未批准前采用“不自动删除有引用证据”。

## 6. 初始化和迁移门禁

先 tenants/roles/users，再组织/模板/词典/模型/规则/activation；循环current pointers初始NULL。角色固定六种；bootstrap管理员由一次性CLI输入密码，不将演示账号写进DDL。数据库管理账户只用于migration；API/worker按表权限分配，AI无账户。DDL脚本不能直接在非空生产schema执行；迁移必须可备份、演练且有前向修复方案。

2026-09-29 实现说明：I-01B 已提供 packages/persistence 的 Alembic 0001_initial、只读结构核对和原子租户/角色/管理员初始化。操作与失败恢复见 [开发指南](../../DEVELOPMENT.md)，真实 MySQL 证据见 [I-01B 验收](../08-delivery/08-i01b-acceptance.md)。本阶段迁移/初始化凭据只供离线管理 CLI；不代表 API/Worker 已获得业务数据库访问能力。

真实库核对发现 DDL 生成器遗漏了 user_roles.scope_key 的 NOT NULL；生成源与未发布的初始迁移快照已补齐，以保持原数据字典定义，不改变角色 scope 规则。迁移发布后不得修改历史快照；后续设计变更必须新增 revision，并更新当前 head 的核对基准。
