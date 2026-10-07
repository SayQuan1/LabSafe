# I-02I3 报告下载签发验收

日期：2026-10-07。范围：接通既有 `downloadExport` 契约，为已完成的 CSV 报告签发受控 GET URL。PDF、完成通知、过期状态转换、对象清理和真实对象存储联调仍属于后续边界。

## 契约与授权

- `GET /api/v1/reports/exports/{id}/download` 不接受 query 参数，返回既有 `DownloadGrantResponse`，响应使用 `no-store` 和 `no-referrer`。
- 只有 `ready`、CSV、`expires_at` 仍在窗口内且具备完整固定对象证据的报告可下载；queued/running/failed/expired、过期或缺少 key/checksum/VersionId/size 均拒绝且不签名。
- 请求人在报告创建人或对完整快照范围具有 admin 权限时才可下载，并且每个快照实验室都必须重新具备 `export` 权限。跨租户或不可见范围隐藏为 404，部分范围授权不能下载。
- 下载只使用冻结报告登记的 checksum、规范对象 key 和准确 VersionId；不重新查询业务数据、不重新生成文件、不向响应或审计暴露内部对象字段。

## 实现边界

`packages/domain/report_download.py` 校验 CSV key、SHA、对象版本、大小和范围；`packages/persistence/reports.py` 在租户事务中锁定并读取私有证据；`packages/application/reports.py` 在事务外执行共享 download grant 限流，在租户/报告锁内重新认证授权后调用本地 S3 SigV4；`packages/storage/s3.py` 的 `presign_report_get` 固定 GET、公共 origin、准确 VersionId 和 60 秒 TTL，并复核完整签名查询。

数据库事务不执行 HEAD/GET/PUT。签名失败、寿命异常或证据不一致不会返回 URL，也不改变报告状态。

## 验证

业务测试见 [test_report_download.py](../../tests/business/test_report_download.py)，MySQL/HTTP 专项见 [test_report_download_mysql.py](../../tests/persistence/test_report_download_mysql.py)。覆盖规范引用、非 ready/过期/缺失或错误证据、创建人和同范围 admin、撤权与部分实验室、跨租户 404、固定 60 秒 TTL、精确 VersionId 传入 signer、query 拒绝及不触发重新生成。业务回归 1550 项、此前完整持久化回归 446 项通过；新增 MySQL 专项随完整 runner 执行，真实 S3/IAM/TLS 联调仍需部署验收。

2026-10-07 提交前重验：业务/领域/安全/协议1550项通过。隔离MySQL8.0.33 runner以 `PYTEST_ADDOPTS=-k "report or remediation or structure"` 筛选当前447项中的73项，全部通过、374项未选中；包含新增报告下载及整改/报告受理/执行用例，迁移head=0002_report_object_version、43表/115外键。此为定向回归，不宣称本次重新执行完整447项；历史446项完整成绩仍按第29批记录。
