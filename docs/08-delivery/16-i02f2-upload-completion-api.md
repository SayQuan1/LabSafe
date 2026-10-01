# I-02F2 上传完成受理与图像查询验收

日期：2026-10-01。状态：本地实现与验证完成；本次按用户授权提交特性分支 PR，远程 CI 和独立评审另行记录。

## 1. 本批边界

本批新增两个公共接口，累计 **31 个**：

| 接口 | 行为 | 本批不做 |
|---|---|---|
| `POST /api/v1/uploads/complete` / `completeUpload` | 固定 staging 对象版本，原子登记 `asset_images(validating)`、`validate_image` 任务、TaskDispatch Outbox、审计和幂等响应，返回 202 | 不计算真实 SHA、不解码、不生成 O/A、不标记 ready |
| `GET /api/v1/images/{id}` / `getImage` | 返回当前授权范围内的 Image 白名单元数据 | 不返回对象 URL/key/version，不提供下载 |

本批复用 I-02F1 的 `API_UPLOADS_ENABLED` 开关和 S3 配置。任务只写入 MySQL 真相源；没有 publisher、worker、sweeper 或 Redis 队列消费，`task_runs=ready`、Outbox=pending、Image=validating 是预期状态。

## 2. 完成请求和响应

请求严格为 `{upload_id,sha256}`，拒绝 owner、tenant、object_key、version_id、query 和未知字段。沿用 Origin、JSON、Secure session、CSRF、Idempotency-Key、no-store 和统一错误封装。

首次成功返回 202，响应只含契约 Image 字段；内部 upload_id、tenant_id、对象 key/version、legal_hold、任务 payload 均不公开。日期统一为 UTC RFC3339 毫秒，`analysis_sha256`、`width`、`height` 在 validating 阶段为空。

## 3. 授权、固定版本和事务

首次完成在 API 事务外 HEAD：先锁并重验当前 owner、grant 和声明 hash；若尚无 image，事务回滚后释放全部 DB 锁，再执行内部 HEAD。HEAD 只接受服务端 staging key、非 `null` VersionId、1–15 MiB、精确 MIME/大小；Metadata SHA/ETag 不可信，固定 VersionId 不回退 latest。

第二次事务重新认证并比对快照；会话撤销、owner 状态变化、grant 过期、大小/MIME/对象版本不一致均拒绝。最终事务同时写 image、upload 状态、task_runs、outbox、audit 和幂等响应。事务中不执行 HEAD/GET/PUT、凭据刷新或 Redis；任意失败全回滚。

## 4. 幂等和并发

同 key/正文重放返回原 202、原 request_id 和原响应，不重 HEAD、不续签、不再建图/任务。新 key 命中已登记 image 时返回同一图像当前投影，不新增任务；当前权限仍重验。四会话竞争只产生一个 image、task 和 Outbox；跨 tenant/lab 在对象访问前返回 404。

## 5. 图像查询

`getImage` 只要求 owner.read，不要求 capture/assignee；查询只读 RR 快照，不调用对象存储，不改变状态、版本、审计或任务（既有 session idle touch 除外）。沿用既有普通读限流：Redis 不可用时使用每用户每分钟最多 30 次的进程内保守 fallback，仍重新认证；完成写请求则返回 503。`deleted` 可返回数据库墓碑元数据，但不提供下载或恢复。

## 6. 本地验证与限制

主套件 1283 项通过；完整隔离持久化回归 213 项通过（192 项真实 MySQL + 21 项重叠单元，含本批新增 35 项数据库用例）；独立 AI fixture 15 项、协议测试 4+4 项和 AI 依赖隔离通过。覆盖完成请求严格字段、UUID/hash、Origin/JSON/CSRF/幂等/query、图像投影日期和字段白名单；真实 MySQL/Redis 覆盖首次登记、并发、HEAD 失败、撤权/状态变化、事务回滚、范围隔离、读权限和任务协议。Stubber 只验证 SDK 参数和错误封装，不是实时 MinIO/IAM/TLS 验收。

真实 SHA、解码/像素/EXIF、O/A 复制、ImageValidated、publisher/sweeper、lease/fencing、远程 CI 和人工审查仍未完成。下一批先完成 I-03A 任务基础，再接 general 图像验证消费；不得把 validating 或 ready task 当作 image ready。
