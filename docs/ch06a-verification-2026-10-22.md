# CH-06A 验收记录 · 2026-10-22

CH-06A 已通过所列环境的验收，状态为 **VERIFIED**。实现基线为干净的
`codex/ch-03b-publication-reconciliation@18ed369`，本轮分支为
`codex/ch-06a-confirmed-cancellation`，代码提交为 `89f45a2`、`83e0c73`。
最终被验证的 Python 源码固定在 `83e0c735814ebca376e81f432d084f64e5397730`。

硬依赖均在提交历史中：CH-09 `59d996f` / PR #6、CH-10 `adbca64` / PR #7、
CH-03B `2e1daa0` / `5c5d892` 及其 281 项容器验收。此前未合并的依赖历史已由
[PR #8](https://github.com/xsw932187210-cell/RepoPilot/pull/8) 合并为
`main@554012eb1f4dc895cec9a42452d52eb9e5389344`，原 head 为
`codex/ch-03b-publication-reconciliation@18ed369`，对应 CI run `39642188749` 已成功。
收尾时经 GitHub 和 fetch 核实，合并后的文件树与本轮起始基线完全一致；没有重写已验收源码。
CH-06A 没有写入 PR #8 的分支。

## 实现范围

- SQL 持久化停止请求。运行中取消返回 202 / `cancelling`，本地执行资源清理完成后才用 CAS
  写终态和 `stopped_at`；排队/审批中的无资源任务可以通过 CAS 直接停止。旧 Worker 的迟到结果被拒绝。
- 新任务保存默认 900 秒执行期限，分别记录执行、排队和审批时间；后两者是否计入期限可配置，
  默认不计入。等待任务的期限扫描独立于正在运行的任务。节点、模型、重试退避和进程等待使用剩余预算。
- 统一执行 scope 中断模型等待、POSIX 进程组和 Docker 容器。候选冻结线程的二进制 Git I/O
  也复用同一进程控制。线程必须被等待；Docker 的创建 ID 在启动前保存，清理失败保留诊断。
- 发布使用 CH-03B 的停止谓词和 receipt，对已发出的请求有界只读对账，保留已存在 PR/未知结果。
  `abort_attempt()` 只中止一次执行，不把整个任务改为取消。
- Alembic `20261022_0007` 添加时间、停止和资源字段；旧任务不补造期限、历史耗时或停止确认。
  API、配置、事件及恢复边界见[执行控制契约](execution-control.md)。没有实现其他任务卡。

## 最终验证

| 检查 | 结果 |
| --- | --- |
| `ruff check .`、`git diff --check`、脚本语法 | 退出 0 |
| macOS / Python 3.13.5 完整测试 | 279 passed，35 skipped，83.68% coverage；35 项为未配置专用服务 URL 的条件入口 |
| 独立 Linux / Python 3.12.15 完整测试 | **314 passed，0 failed，0 skipped，84.19% coverage** |
| 35 项服务入口 | 历史 PostgreSQL 入口 27 项、CH-06A 专项 8 项全部实跑 |
| Mock eval | 10/10，`deterministic-mock-v1`，数据集摘要保持不变 |
| API smoke | completed，3 次模型调用，unknown=0 |
| 实际 Worker 审批后重启 | completed，调用 3→3，unknown=0；test_runner/finalize 各一次，完整审批/manifest/证据不变 |
| 来源核对 | 容器 86 份 Python 源文件与工作区及 `83e0c73` 相同；测试/API 安装包各 46 份文件相同 |
| 资源清理 | 三轮专属 Compose 项目均确认容器、卷、网络已移除；镜像保留供复跑 |

最终独立项目为 `repopilot_ch06a_20261022_51033`，API 端口 58080，Docker Engine 29.5.3，
PostgreSQL 16.15，Redis 使用 `redis:7.4-alpine` 镜像，LangGraph 1.2.16，PostgreSQL saver 3.2.0。
依赖镜像复用固定本地镜像，当前源码重新安装；生产 Dockerfile 和默认构建路径未被替换。

复跑入口：

```bash
ruff check .
pytest --cov=repopilot --cov-report=term-missing --cov-fail-under=70 -q
CH06A_DEPENDENCY_IMAGE=repopilot_ch05a_20261021_21666-test:local make ch06a-integration
```

本地依赖镜像不存在时，去掉 `CH06A_DEPENDENCY_IMAGE` 使用默认生产 Dockerfile 构建。
脚本拒绝复用已有同名资源，默认建立专用项目；不要使用主实例代替独立验收。

## 故障证据与观察

| 触发点 | 实际观察 |
| --- | --- |
| 排队/审批取消 | CAS 阻止执行，审批/投递意图失效；重复请求不创建新停止请求 |
| 模型等待/退避取消 | 本地等待结束；已发起尝试保留 UNKNOWN，不发下一次 retry，不宣称 provider 停止计费 |
| 四种假时钟时间策略 | 队列、执行、审批分别计时；排队/审批是否计入的四种组合均正确，数据库重开不重置预算 |
| 期限到期 | 不再预留/发送新模型请求；真实 Docker 中到期终止测试，Reviewer 未运行，模型调用保持 2 次 |
| 真实 Docker 中请求取消 | 先观察 `cancelling`，等待该 ID 的容器移除后才有停止确认；另一个容器仍运行 |
| 真实 POSIX 进程组取消 | 父进程和子进程已停止，另一个 session 中的进程仍运行 |
| 候选冻结 Git 等待 | 二进制输入原样传递；用户取消或仅中止执行均停止受控进程组，并等待线程结束 |
| 清理失败/停止中重启 | 保留 `cancelling`、资源 ID 和 `cleanup_pending`；不伪造 `stopped_at`，不杀历史 PID |
| PostgreSQL 并发取消 | 两个请求只有一份持久化请求，旧完成 CAS 被拒绝 |
| blob / PR 已生效但响应未到时取消 | 先 GET 核对，保留真实远端 ID，不重复 POST；实际 GitHub 由测试 transport 代替 |
| PostgreSQL 迁移中第三列失败 | DDL 回滚到 0006，无半列残留；重跑到 0007、重复迁移成功，旧 cancelled 仍无伪造确认 |

测试入口：[单元/假时钟](../tests/test_execution_control.py)、
[真实服务探针](../tests/test_execution_integration.py)。来源、镜像和原始证据摘要见
[机器记录](evidence/2026-10-22-ch06a-verification.json)。

## 中间结果与修复

第一轮 `repopilot_ch06a_20261022_44110` 为 303 passed、1 failed、83.98% coverage。
失败是 CH-05A 的旧库夹具使用最新版 ORM 写入 revision 0004，引用了尚未迁移的字段。
旧库夹具现用旧字段建任务，并由固定 CH-03A Worker 输出升级前审批快照；升级后仍比较完整对象，
没有删除兼容性断言。其他历史迁移夹具采用相同旧字段插入方式。

第二轮 `repopilot_ch06a_20261022_49200` 基于 `89f45a2`，312 passed / 0 skipped、
84.12% coverage，Mock/smoke/Worker 重启全部通过。复核发现候选冻结的同步 Git 也需要纳入执行控制，
因此补交 `83e0c73` 和两个回归场景，再完整重跑最终 314 项验收。前两轮日志和清理记录保留。

原始报告仅在本地忽略目录 `reports/repopilot_ch06a_20261022_51033/`，宿主结果为
`reports/ch06a-host-release.log` / `.xml`；公开记录保存摘要和哈希，没有发布私有源码或凭据。

## 兼容、限制与交付

部署前停止旧 API/Worker，确认执行已排空，备份 SQL 和 artifacts，升级到 0007 后只启动新版。
旧任务的期限及停止确认保持 null，阶段计时从升级后开始；没有无损破坏性 downgrade，
恢复使用备份或前向修复。运行中任务的取消响应从立即 `cancelled` 改为 202 / `cancelling`，
客户端应等待 `stopped_at`；旧 cancelled 不能补造确认。

本轮不声称 Worker fencing、自动崩溃重领、外部事务或远端回滚；provider 账单是否停止没有实测。
清理不可确认或 Worker 停止时保留人工检查入口。真实模型、真实缺陷质量和应用发布到 GitHub
均未重跑。2026-09-09 的真实报告、失败账本和 Mock 数据集与 `b7cf1c9` 字节相同。

CH-06A 已推送到独立分支 [codex/ch-06a-confirmed-cancellation](https://github.com/xsw932187210-cell/RepoPilot/tree/codex/ch-06a-confirmed-cancellation)，首次推送提交为 `6daad10`，
包含实现与验收记录；后续仅回填实际推送元数据。CH-06A 未创建新 PR、未合并；
PR #8 仅包含前置依赖栈，已经合并。
下一张建议为无硬依赖的 P0 **CH-06C**；本轮完成 CH-06A 后停止。

2026-10-22 交付更新：[PR #9](https://github.com/xsw932187210-cell/RepoPilot/pull/9) 已通过 CI
并合并为 `main@4d55a5b5b83b617881b0b2bf9d2ea7dc94ef3785`，当前状态 **MERGED**。
上述未创建 PR/未合并为首次推送时的历史快照，验收源码和历史质量口径没有变化。
