# LabSafe Git 协作规范

本仓库由两名协作者共同维护。`main` 是可交付分支，禁止直接提交未经审查的功能变更。

## 1. 分支策略

- `main`：稳定、可演示、可部署版本，只接受 Pull Request。
- `feat/<task-id>-<short-name>`：新功能。
- `fix/<task-id>-<short-name>`：缺陷修复。
- `docs/<task-id>-<short-name>`：文档和设计变更。
- `chore/<task-id>-<short-name>`：工具链、依赖和维护工作。

分支从最新 `origin/main` 创建。一个分支只处理一个任务或一个紧密相关的变更，不在同一分支混入无关格式化和重构。

## 2. 日常同步

```powershell
git fetch origin --prune
git switch main
git pull --ff-only origin main
git switch -c feat/T-xxx-short-name
```

开始工作前和提交 PR 前都执行一次 `git fetch origin --prune`。发现 `main` 有新提交时，先在自己的分支 rebase 或合并最新 `origin/main`，再运行测试。

## 3. 提交规范

提交格式：

```text
<type>(<scope>): <imperative summary>
```

允许的 `type`：`feat`、`fix`、`docs`、`test`、`refactor`、`chore`、`build`、`ci`。

示例：

```text
feat(inspection): add inspection item upload contract
fix(rule): reject unresolved chemical entity
docs(gate-2): define finding indexes and constraints
```

每个提交应保持可构建、可测试或明确说明是文档提交。提交正文需要说明原因、影响和验证命令；涉及迁移、权限、数据、模型或规则时必须说明回滚方式。

## 4. Pull Request 规则

- 不直接向 `main` push。
- PR 标题包含任务编号，例如 `T-012: add finding review workflow`。
- 至少一名另一位协作者审查并批准；作者不能批准自己的 PR。
- 数据库、权限、隐私、模型、规则、部署和安全变更需要两名协作者共同确认。
- PR 必须通过自动测试、静态检查和构建检查。
- PR 描述必须列出需求 ID、变更文件、测试结果、风险和回滚方式。
- 使用 Squash merge；合并后删除远程特性分支。
- 禁止 force-push `main`、删除 `main` 或改写已发布提交历史。

## 5. 文件与数据规则

- `.obsidian/`、`.env`、凭据、私钥、真实实验室图片和未脱敏个人信息不得提交。
- 本地配置通过 `.env.example` 说明变量名，不提交真实值。
- 测试图片和样本必须使用脱敏、授权或模拟数据。
- 模型文件和大体积数据使用项目约定的对象存储/制品库，不直接提交 Git；具体方案在 Gate 2 确认。
- 任何规则、模型、数据库迁移和 API 变更都必须同步更新对应设计文档。

## 6. 变更分类与审批

以下变更必须在 PR 中明确标记并附 ADR 或设计更新：

- 租户、权限、审计或数据留存变化；
- Finding/RemediationTask 状态机变化；
- API 兼容性变化；
- 规则 DSL、规则版本或模型推理契约变化；
- 数据库表、索引和迁移变化；
- 部署拓扑、密钥和备份策略变化。

## 7. 发布、标签与回滚

- 可演示版本使用 `v0.x.y` 标签；正式版本使用 `v1.x.y` 及以上。
- 发布前必须记录 Git 提交、数据库迁移版本、模型版本和规则版本。
- 回滚优先使用上一稳定提交、模型版本或规则版本；不得删除审计记录。
- 数据库迁移必须提供向前迁移和可执行的回滚/补偿方案。

## 8. GitHub 仓库设置建议

仓库管理员应在 GitHub 为 `main` 配置保护规则：

1. 禁止直接 push。
2. 要求 Pull Request。
3. 至少 1 个批准审查。
4. 要求分支为最新后才能合并。
5. 要求 CI 检查通过。
6. 禁止 force-push 和删除分支。
7. 对高风险文件启用 CODEOWNERS 审查。

这些设置属于 GitHub 仓库权限，不能由本地 Git 提交自动完成。
