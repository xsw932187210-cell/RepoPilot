# CH-03B：发布绑定、幂等意图与远端对账验收

状态：**VERIFIED（本地验收完成，尚未推送、创建 PR 或合并）**。本记录只覆盖 CH-03B。

## 基线与依赖

- 起始状态：干净的 `codex/ch-05a-staged-generations@b7cf1c9`。
- 本轮分支：`codex/ch-03b-publication-reconciliation`；继承已有本地历史，没有重置 main。
- 硬依赖 CH-03A：`a5b5dab`、`c00b23b`、`9e62ce7`；CH-05A：`3610e9f`、`c4619a8`、`a0df3de`。
  已逐项核实为本地祖先，并读取两卡验收及存储契约。它们仍为本地已验收依赖，未重新查询远端合并状态。
- 功能提交 `2e1daa0`；取消/基线并发事实修正 `5c5d892`。最终验收源码对应 **`5c5d892`**；
  最终文档另行提交。分支保留 CH-01A/CH-02B/CH-03A/CH-05A 的本地堆叠历史。

## 交付内容

旧 publisher 接受原始 edits，按发布时 HEAD 重套修改，且分支仅使用任务 ID 前八位。本轮改为
只接受持久化 intent ID，核验 SQL 任务/版本、已消费的同意决定、候选、测试证据、目标及当前代次。
完整 artifact 的普通文件、二进制字节及可执行模式构成完整 Git tree；commit parent 固定获批 base SHA。
固定提交元数据和完整任务/意图身份确定 blob/tree/commit/ref/PR 目标，不 force push、不自动合并。

迁移 `20261022_0006` 新增 `publish_intents`，保存不可变 payload 摘要、唯一 decision 绑定和独立 receipt CAS。
每次请求先保存阶段，再查询远端确认。PR 请求可能发出而查询为空时仍保持 unknown，不再次盲目创建。
基线移动标记需要重测/重批；取消保留已发生或未定效果，同时发生的取消和移动分别保留事实。
`GET /api/v1/tasks/{id}/publication` 受 API Key 保护，只读 SQL，终态后仍能查看 receipt。
`repopilot-publish INTENT_ID` 默认只读远端对账，`--continue-active` 仍要求原任务/审批有效。
详细接口、状态、取消与恢复说明见[发布契约](publication.md)。

## 最终验证

| 验证 | 结果 |
| --- | --- |
| lint、`git diff --check`、脚本 shell 语法、文档本地链接 | 通过 |
| macOS / Python 3.13.5 完整离线套件 | 254 passed / 27 skipped，82.46% coverage |
| Linux / Python 3.12.15 完整服务套件 | **281 passed / 0 skipped，83.55% coverage** |
| Mock 回归 | 10/10；模型 `deterministic-mock-v1`，仅流程回归 |
| API smoke、实际 Worker 等待审批后重启 | 通过；同一审批、manifest、证据不变；调用 3→3，unknown=0；测试与 finalize 各 1 次 |
| 镜像/工作区/提交来源核对 | 80 个 Python 源码与测试文件均与工作区和 `5c5d892` 相同；测试/API 实际安装包各 43 文件一致 |
| 发布 CLI 入口 | 最终镜像内 `--network none` 执行 `repopilot-publish --help` 退出 0 |
| 专属资源清理 | 本轮三个项目的容器、卷、网络均按标签确认清理；镜像缓存保留 |

离线跳过的 27 项在容器内全部实跑：CH-09 1、CH-10 2、CH-02B 6、CH-03A 5、CH-05A 6、
CH-03B 7。覆盖率只排除固定 `/legacy/*`、`/legacy-approval/*`，没有排除当前发布代码。
已有 ORJSONResponse 弃用提示没有改变通过门槛。计数、版本、镜像和证据摘要见
[脱敏 JSON](evidence/2026-10-22-ch03b-verification.json)。

## 故障与恢复证据

| 场景 | 观察结果 |
| --- | --- |
| 每种远端阶段已成功、响应丢失 | blob/tree/commit/ref/PR 均先查原目标；能立即查到时同次调用继续，暂时不可查时保存 unknown；重开数据库后收敛到同一 Draft PR |
| 成功效果后进程硬退出 92 | 真实 PostgreSQL 已保存 sending；测试 transport 的独立持久化远端状态保留效果；新进程/连接重放，意图 1 个、PR 1 个、对象及 parent 相同 |
| PR 发出后空查询 | 连续重放不再创建；后来可见时接纳原 PR；查询 503 或 tree 截断也不会授权继续写入 |
| 分支/PR 内容身份冲突 | 分支不覆盖；PR 的 head/base/draft/state/body 或重复记录冲突均停止，不创建替代 PR |
| 取消发生在请求前、ref/PR 在途、丢响应后 | 请求前无 POST；在途请求先对账，保存 branch/PR 或 unknown；后续写入停止；取消在空查询后仍保持有效 |
| base 在发布前、ref/PR 在途、丢响应后移动 | 不套新 HEAD；标记需要新测试/审批；已有远端 ID 保留；base 回到原 SHA 也不清除重验标记 |
| 取消和 base 移动同时发生 | `cancel_requested`、`requires_revalidation` 同时为 true；保留 ref/PR 与观测到的 SHA |
| 原 checkout 在审批后被改写 | 发布仍使用获批 artifact 字节；本机 Git 独立核对完整二进制/可执行/CRLF/中文路径树及 commit ID |
| 产物损坏、审批失效、旧代次、旧发布决定、开关关闭 | 拒绝发布；不发出未经授权 HTTP；原始 edits 或无持久化意图的旧接口调用拒绝 |
| 意图初始化/receipt 并发 | 同一 decision 得到同一意图；真实 PostgreSQL receipt CAS 仅一个写入成功 |
| 0005→0006、重复升级与历史迁移故障恢复 | 保留原 task/event；不补造意图；CH-09/CH-10 的 DDL 故障后前向重跑继续通过 |

远端始终是 `httpx.MockTransport`；这里的硬退出是真实进程故障和 PostgreSQL 持久化验证，
并非真实 GitHub 服务实验，也没有模拟宿主断电。实际 Worker 重启 smoke 单独验证审批恢复，
不把它当作自动队列找回或发布调度器证据。

## 环境与复跑

最终 project：`repopilot_ch03b_20261022_35230`，API `127.0.0.1:58070`；Docker 29.5.3、
PostgreSQL 16.15、Redis 7.4 镜像、LangGraph 1.2.16、PostgreSQL saver 3.2.0。
服务配置仅 Mock、模型凭据为空、真实 GitHub 写入关闭。发布测试使用假 token 和测试 transport。
主实例没有重启；继承的服务探针在隔离容器内使用 `repopilot_ch05a_test`，新发布探针另用
`repopilot_ch03b_test`，两者都在本次专属 PostgreSQL 卷内。

```bash
make ch03b-integration
# 本次复用已验收依赖缓存，重新 COPY/安装当前完整源码和测试：
CH03B_DEPENDENCY_IMAGE=repopilot_ch05a_20261021_21666-test:local make ch03b-integration
```

依赖镜像不可变 ID 与最终 app/test/sandbox ID 已记录；依赖缓存不等于完整依赖锁。
原始 JUnit、日志、Mock、smoke、来源摘要和清理记录位于 Git 忽略目录 `reports/<project>/`。
最终宿主记录为 `reports/ch03b-host-20261022-release.{log,xml}`。公开 JSON 只保存计数、版本、
摘要和故障结论，不发布原始代码、模型输出、请求、凭据或隐藏验收标识。

## 中间失败与证据适用范围

1. `repopilot_ch03b_20261022_32924`：275 passed / 3 failed，83.51% coverage。
   历史 CH-09/CH-10 专用库重建夹具漏清理新增表，二次迁移报 `publish_intents` 已存在。
   已在两处固定清理列表补齐新表，没有放宽迁移、跳过失败测试或降低覆盖率。
2. `repopilot_ch03b_20261022_33203`：修正后的 `2e1daa0` 镜像，278 passed / 0 skipped，83.51%；
   Mock、smoke、Worker 重启和清理通过。其后补上取消/基线并发事实及 3 个回归；
   该轮只作为中间版本证据，不能代替最终 `5c5d892` 的 281 项验收。
3. 最终项目独立重建、全量复跑并逐文件核对来源，全部通过。三个项目的原始日志与 cleanup.json 均保留。

2026-09-09 的真实模型报告、失败账本与原 Mock 数据集均与起始基线逐字节相同。
本轮没有真实模型调用、真实 GitHub 写入或新的修复率/多 Agent 收益主张。

## 升级、恢复与限制

停止旧 API/Worker，备份 SQL 和 artifact 卷，再单实例迁移到 `20261022_0006`，只启动新版。
等待中的绑定审批由新校验记录 publication protocol marker 后继续；固定 CH-03A 源码 `a1e5f41`
产生的旧审批在完整服务套件中仍可继续。已经越过审批、可能发出旧 HTTP 的 checkpoint 不会自动
获得新意图，明确拒绝并要求人工核对远端。没有无损破坏性 downgrade；恢复采用备份或前向修复。

图遇到未定/冲突/重验发布时为 failed，取消信号使用现有 cancelled；独立 receipt 保留事实。
CLI 默认仅 GET 对账且不修改终态；显式继续只允许原任务仍运行、审批/代次/版本有效时补做同一意图。
不能通过重置任务、清除 unknown 或新建意图来跳过未定 PR 的对账。
完整任务停止确认仍属 CH-06A；没有后台自动投递/自动重试，没有多 Worker ownership/fencing，
SQL 与 GitHub 不是跨系统事务，base 检查也不与远端分支移动原子同步。

## 待提交 PR 说明

建议标题：Bind GitHub publication to approved artifacts and reconcile uncertain remote effects.

发布使用完整获批候选和固定 parent，避免审批后 HEAD 移动导致提交未测试代码；持久化确定性意图和
阶段 receipt，响应丢失后查原对象，分支冲突停止，PR 未定时禁止重复创建，并保留取消/基线变化后的效果。

验证见上表。PR 应声明基于 `b7cf1c9` 的本地依赖栈；本轮仅本地提交，没有推送、创建 PR 或合并。
下一张可选 **CH-06A**，其 CH-09/CH-10/CH-03B 硬依赖已有实现与证据；本轮不执行。
