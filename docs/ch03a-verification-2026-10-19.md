# CH-03A 验收记录：冻结候选与一次性审批

本卡把送测代码、测试结果和人工审批绑定为可持久化的同一对象。候选从完整文件及权限
生成摘要，测试运行临时副本；审批不再以布尔值代表“批准当前工作区”。本次只验证工程
行为，不调用付费模型，不作修复率提升或真实 GitHub 发布主张。

## 基线与交付

- 基线：干净的 `codex/ch-02b-integration-validation@b23ac4f`，保留此前未合并的
  CH-01A/CH-02B 提交。分支为 `codex/ch-03a-candidate-approval`。
- 硬依赖：CH-02A `a2cb1d5` 已随 `792cb84` 进入历史主线；CH-09 `59d996f` 已由
  `bc49d5b` 合并；CH-10 `adbca64` 已由 `12b9e3d` 合并。按本地提交图核对，未改写历史。
- 代码：`a5b5dab` 实现；`c00b23b` 补齐无 Git 历史的评测导出目录兼容；`9e62ce7` 统一拒绝无法解析的产物。最终源码身份由[机器证据](evidence/2026-10-19-ch03a-verification.json)记录。
- 契约：[candidate-approvals.md](candidate-approvals.md)。新增 Alembic
  `20261019_0004`，不补造旧任务的候选、测试或授权记录。
- 本轮仅本地开发与提交，未推送、创建 PR 或合并。后续 PR 应以 `b23ac4f` 所在依赖分支
  为明确基线，或先整理/合入前置提交，避免把继承内容误算成本卡新增功能。

## 最终验证

| 环境/入口 | 结果 |
| --- | --- |
| macOS arm64 / Python 3.13.5 | `ruff check .` 通过；190 passed / 14 skipped，82.02% coverage |
| Linux arm64 / Python 3.12.15 / 全量套件 | 204 passed / 0 skipped / 0 failed，82.91% coverage；14 个服务条件入口全部实际运行 |
| Mock workflow eval | 10/10；模型 `deterministic-mock-v1`，不是 LLM 修复质量实验 |
| API smoke / 实际 Worker restart | 均通过；调用 3 → 3，测试与 finalize 各 1 次，unknown=0；审批/manifest/证据完全一致 |
| PostgreSQL / LangGraph | PostgreSQL 16.15，LangGraph 1.2.16，checkpoint-postgres 3.2.0 |
| 资源与源码身份 | project `repopilot_ch03a_20261019_75308`；清理确认 true；镜像内 67 份 Python 文件摘要与交付源码一致 |

命令为 `./scripts/ch03a_integration.sh`（即 `make ch03a-integration`），完整测试耗时 81.232 秒。
最终代码提交 `9e62ce7`；完整镜像、数据集及日志摘要见机器证据。3 条 FastAPI deprecation
warning 不影响测试结果，未隐藏测试失败或降低覆盖率门槛。

测试镜像与 API/Worker 镜像都从本轮源码构建。最终运行记录源文件摘要，并与工作区逐一
比对；没有用旧镜像结果替代当前代码。Linux 覆盖率配置只排除 `/legacy/*` 中固定旧版本
兼容探针的源码，当前版本全部纳入，70% 门槛未降低。

## 故障和拒绝场景

| 触发点 | 可观察结果与证据入口 |
| --- | --- |
| 同一快照重复捕获、mtime 变化 | 摘要稳定；单字节、权限、添加、删除和二进制文件改变身份；完整 Diff 超过预览长度仍可读取。`test_candidates.py` |
| 基线有 export-ignore 属性 | 读取实际提交树/blob，原始基线无变化时 Diff 仍为空；未使用受导出属性影响的 archive 充当完整基线。`test_candidates.py` |
| 评测导出目录没有 `.git` | 从受信任 source checkout 读取固定 buggy commit，图正常到达审批，模型/测试目录仍不带 Git 历史。`test_real_evaluation.py` |
| manifest、Diff、tar 内容或编码损坏 | 拒绝不一致 artifact；API 不保存审批决定。有效 tar 内容篡改由真实 API 验证，无法解析的 JSON/tar 有负向回归。`test_candidates.py / test_approvals.py / test_approvals_postgres.py` |
| 两个相反决定并发 | PostgreSQL 仅保存一个决定、任务版本仅前进一次；另一方为冲突，精确重试返回原 decision ID。`test_approvals_postgres.py` |
| 过期、已取消、非等待、旧证据或旧代次请求 | 409；取消还阻止 pending/enqueued 意图重投。旧无版本布尔请求 422。`test_approvals.py` |
| 决定事务提交后 `os._exit(87)` | 新进程仍读取原候选、证据、决定和 pending 意图，原任务为 queued。`probes/approval_decide.py / test_approvals_postgres.py` |
| Redis SDK 连接到隔离服务的拒绝端口 | 202，`saved=true / pending / queue_delivery_failed`；重新打开 DB 后显式重投，真实 Worker 完成原任务。消息布尔值与 SQL 决定相反时仍只使用 SQL 决定；模型调用保持 3 次，测试与 finalize 各 1 次。`test_approvals_postgres.py` |
| 实际 Worker 在审批等待时重启 | 前后审批对象、完整 manifest、测试证据逐字一致；原任务完成，测试不重跑、预算不重置。`recovery_smoke.sh` |
| 固定 `12b9e3d` 旧图 checkpoint | GET 明确要求重新验证，旧布尔请求 422；原任务/测试/调用数保留，不由当前工作区推定历史授权。`test_policy_recovery_postgres.py` |
| 人工拒绝、恢复时 state edits 与 artifact 不一致 | 拒绝走 cancelled，finalize 不执行；不同字节不能进入 publisher。`test_approvals_postgres.py / test_reliability.py` |
| 0003 → 0004 与历史迁移回归 | 升级两次后任务/事件保留；CH-09/CH-10 独立测试库验证旧结构升级、故障回滚、预算并发与重试。对应 PostgreSQL 测试文件 |

## 运行记录与清理

首次窄范围运行 `repopilot_ch03a_20261019_71157` 完成 11 项服务检查、Mock 10/10 与
smoke/restart。完整套件首次运行 `repopilot_ch03a_20261019_72798` 为 200 passed、1 failed：
测试镜像缺少已提交的历史失败账本夹具；同时覆盖率混入旧源码导致 64.74%，整条命令正确
失败。补齐夹具并排除旧版本分母后，`repopilot_ch03a_20261019_73085` 为 201 passed、
0 skipped、82.64%，其 smoke/restart 通过。无 Git 导出目录回归补齐后，
`repopilot_ch03a_20261019_73897` 为 202 passed、0 skipped、82.82%，smoke/restart 通过。
它们是开发中间版本，最终版本以本页结果及机器证据为准；失败记录未删除。

所有运行仅使用新的 `repopilot_ch03a_*` Compose project、专用 PostgreSQL/Redis 与 volume，
API 绑定本机 58030；额外迁移测试库位于该次专属 PostgreSQL 容器。cleanup 只按本轮标签
移除容器、网络和数据卷，不修改主实例、不做全局 prune。镜像与构建缓存保留。
忽略目录 `reports/<project>/` 保存 JUnit、完整测试日志、构建日志、镜像/源码摘要、Mock
结果、smoke/restart 和 cleanup 记录；公开 JSON 只导出摘要、计数和校验身份。

## 升级与剩余边界

停止旧 API/Worker，备份 SQL 与 artifact volume，执行单实例迁移并核对 head
`20261019_0004` 后再启动新版本。旧布尔审批返回 422；无候选绑定的旧审批任务须取消并
新建任务重新测试，不能给历史 checkpoint 自动补授权。不支持破坏性 downgrade。

候选存储是应用层不可变接口及篡改检测；它与数据库没有跨系统事务，可能留下未引用
bundle。Redis 接受不等于消费确认；尚无自动 Outbox、崩溃重领、watchdog 或 Worker fencing。
`finalize` 校验 edits 字节，但 publisher 的获批 base 构建、意图幂等和远端对账仍是
CH-03B；本轮不能声称最终 GitHub commit 已完整绑定。API 仍为单用户可选 API Key。

## 待提交 PR 说明

建议标题：`Bind human approvals to frozen candidates and durable decisions`。

原审批只有布尔值，无法确认对应哪份代码；决定提交后投递 Redis 失败也缺少可靠恢复
对象。本变更冻结完整候选与测试证据，要求版本化审批绑定，以数据库事务保存一次决定、
状态 CAS、审计事件与恢复意图，并提供明确的显式投递重试入口。

验证见本页及机器证据。兼容变更为 approval-v2 必填字段与旧无绑定 checkpoint 重新验证；
部署需前向迁移。没有自动队列恢复或最终 GitHub 发布身份保证。

下一张 P0 建议为 **CH-05A**：在 staging 中完成整批候选后切换可见代次，处理半写入与
orphan；其硬依赖 CH-02A/CH-03A 已有实现。CH-03B 仍需等待 CH-05A，本轮不继续实施。
