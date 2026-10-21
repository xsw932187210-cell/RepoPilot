# CH-05A：暂存候选与完整可见代次验收

状态：**VERIFIED（本地验收完成，尚未推送或合并）**。本记录仅覆盖 CH-05A。

## 基线、依赖与改动

- 分支：`codex/ch-05a-staged-generations`；起始基线 `a1e5f41`。
- 硬依赖：CH-02A 的 `a2cb1d5`、CH-03A 的 `a5b5dab` 及其修正均已核实为本地祖先。
  CH-02A 有历史合并记录，CH-03A 为已验收的本地提交；本轮没有重新查询远端合并状态。
  分支继承 CH-01A、CH-02B、CH-03A 尚未推送的工作，后续 PR 必须说明堆叠关系。
- 功能提交 `3610e9f`、损坏日志恢复修正 `c4619a8`、CRLF 与依赖缓存复验支持 `a0df3de`。
  本报告对应代码引用为 `a0df3de`；
  最终验收记录单独提交。没有推送、创建 PR 或合并，也没有调用付费模型或开启 GitHub 写入。

原实现通过预检后直接逐文件覆盖工作目录，后一个文件 I/O 失败可能留下部分修改。
本轮改为从父 artifact 复制私有 stage，在完整写入、整树校验和文件/目录刷盘后，通过
SQL CAS 选择完整代次。原 checkout 保留，检索、测试和审批按绑定的 artifact 读取。
同一已提交操作可跨 checkpoint 间隙重放，旧父候选、旧证据和旧最终结果不能覆盖新代次。

新增 `candidate_generations/generation_heads` 和迁移 `20261021_0005`。Worker 启动及任务执行前
核对暂存日志，保留 incomplete/orphan/corrupt；不会自动接纳或删除数据，也不会自动重领任务。
完整写入顺序、锁、异常与操作约定见[候选代次契约](candidate-generations.md)。

## 完整验证

| 验证 | 结果 |
| --- | --- |
| `ruff check .`、`git diff --check`、集成脚本 shell 语法 | 通过 |
| macOS / Python 3.13.5 完整离线套件 | 209 passed / 20 skipped，行覆盖率 82.26% |
| 最终 Linux / Python 3.12.15 完整服务套件 | 229 passed / 0 skipped，行覆盖率 83.45% |
| Mock 工作流评测 | 10/10，完成、测试、范围匹配和 HITL 均通过 |
| API smoke、实际 Worker 重启 | 均通过，恢复原任务且先前测试未重跑 |
| 测试镜像源码、测试及安装包/API 源码摘要 | 73 个 Python 文件与工作区一致；测试/API 的 40 个实际包文件均一致 |
| 专属资源清理 | 容器、卷、网络均核对为零；只清理本次项目资源 |

离线跳过的 20 项全部是服务条件测试：CH-03A 5 项、CH-02B 6 项、CH-09 1 项、CH-10 2 项、
CH-05A 6 项；最终服务套件必须实际执行这 20 项，不能将离线跳过计为通过。
覆盖率仅排除固定旧源码 `/legacy/*` 和 `/legacy-approval/*`，不排除当前应用源码。

## 故障与恢复证据

| 触发点 | 外部观测与结果 |
| --- | --- |
| 第二个文件写入前硬退出 88 | 首文件已变更但 stage 为 incomplete，可见代次仍为 0；原 checkout 和父 artifact 保持原字节 |
| 完整候选刷盘、SQL 提交前硬退出 89 | stage 为 orphan，可见代次仍为 0；新 Worker 核对后保留数据，不自动接纳 |
| SQL 提交后、最终日志更新前硬退出 90 | 可见代次为 1，日志虽仍 sealed，重启核对识别为 committed；精确重提不生成重复代次 |
| 代次提交后、`apply_policy` checkpoint 前硬退出 91 | checkpoint 仍绑定 generation 0，SQL 指向 generation 1；显式重放同一操作，模型调用 2→3，Test Runner 仅运行 1 次 |
| 两个 SQL 提案竞争同一父代次 | 仅 1 个提交成功；绕过文件锁的延迟旧父提案同样被 SQL 接收端拒绝 |
| 私有 stage 写入 I/O 失败、外部字节改动、链接置换 | 原 checkout/已接受候选不被覆盖；整树核对或描述符写入拒绝，保留被外部修改的 stage，无盲目回滚 |
| 旧代次提交测试证据、审批或最终任务结果 | `stale_candidate_generation`，数据库不接纳旧结果；原证据对象保留 |
| 非 JSON、数组、错误字段类型、FIFO 日志 | 标为 corrupt 且保留；不会把异常元数据当成功候选，也不会因 FIFO 阻塞恢复 |
| CH-03A 旧版冻结审批升级 | 固定 `a1e5f41` 生成真实旧 checkpoint/审批，迁移 0004→0005 后对象和完整 Diff 相同；继续原任务完成，模型调用 3→3，Test Runner 仅 1 次 |
| 当前版本等待审批后重启实际 Worker | 完整审批对象、manifest 和测试证据逐字节相同，继续原任务；保留预算 3→3，unknown=0，Test Runner / finalize 各 1 次 |

这是进程硬退出实验，没有模拟宿主断电或证明文件系统/数据库为同一事务。
常规读取者始终使用完整 artifact；当前指针的切换不改变已绑定的旧副本。

## 环境、来源与复跑

最终专用项目：`repopilot_ch05a_20261021_21666`，API 仅绑定 `127.0.0.1:58050`。使用专用 PostgreSQL 数据库、
Redis、工作区和网络，固定 Mock 配置，模型/GitHub 凭据为空。运行时版本与镜像 ID 见
[脱敏证据 JSON](evidence/2026-10-21-ch05a-verification.json)。

专用测试 Compose 的 Python 基础镜像固定为上轮已验收摘要
`sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1`。
最终复验复用 `repopilot_ch05a_20261021_19202-test:local` 的已安装依赖，通过
`Dockerfile.ch05a-cached` 重新构建并安装当前完整源码/测试，记录依赖镜像不可变 ID，逐文件核对
测试/API 实际安装包。没有把早期源码的测试结果代入本轮。基础镜像固定不等于完整依赖锁，
生产环境的依赖/镜像可复现性仍属于 CH-18。

```bash
make ch05a-integration
# 本次最终复验命令；该镜像由前一次正常构建生成，ID 保存在原始证据中
CH05A_DEPENDENCY_IMAGE=repopilot_ch05a_20261021_19202-test:local make ch05a-integration
# 可选：显式选择另一个 Python 基础镜像后重新完整验收
CH05A_PYTHON_IMAGE=python:3.12-slim make ch05a-integration
```

原始日志、JUnit、Mock 报告、镜像 ID 和来源摘要在本地忽略目录
`reports/repopilot_ch05a_20261021_21666/`；宿主完整测试为 `reports/ch05a-host-20261021-release.{log,xml}`。
公开 JSON 只包含计数、版本、摘要和故障结论，不包含原始源码、请求或凭据。
清理核对项目标签下容器、卷和网络为零；镜像与缓存保留，Docker Desktop 保持运行。

## 中间失败与证据适用范围

1. `repopilot_ch05a_20261021_15104`：默认基础镜像构建时 Debian HTTP 源返回 502，尚未运行测试。
2. `repopilot_ch05a_20261021_15936`：HTTPS 重试遇到 TLS 连接中断，尚未运行测试。
3. `repopilot_ch05a_20261021_16105`：223 项全部通过、83.42% 覆盖率，Mock 10/10、smoke/重启
   通过并完成清理。之后补充目录刷盘和日志损坏处理，3 个 Python 文件已变化；这次结果
   只描述中间版本，最终版本另行重建核对。
4. `repopilot_ch05a_20261021_19202`：228 项全部通过、83.45% 覆盖率；收集后续证据时，
   运行中的脚本被本轮编辑，shell 读取位置变化导致收尾失败，已清理专属资源。没有把它记成
   smoke/重启或来源核对通过。修正后固定脚本，以独立实例完整复跑。

所有中间记录和各自 cleanup.json 保留在对应忽略目录，不以重试抹去失败记录。
2026-09-09 的 20 个真实缺陷及修复率/调用量保持原历史口径，本轮没有开展新的真实模型实验。

## 兼容与边界

停止旧 Worker/API，备份 PostgreSQL 和产物卷，再执行迁移。已有明确 CH-03A 冻结审批绑定时
回填原候选指针，不重新测试或变更身份。旧运行中 checkpoint 没有该绑定时，需要取消并新建
任务重新验证。原 checkout 和旧产物保留；不支持混用旧/新 Worker 或破坏性 downgrade。

OS 文件锁支持同一共享卷上的受控 POSIX writer；离线适配器使用进程内指针。
任意同身份/管理员进程可以绕过应用边界。暂存和 SQL 是两个提交，尚无自动垃圾回收。
队列确认、任意节点自动恢复、ownership epoch/fencing、取消及最终发布协议仍由后续卡处理。

## 待提交 PR 说明

**建议标题**：Stage complete candidate generations before switching the current candidate.

多文件修改发生 I/O 失败时，原工作目录可能只写入一部分。本变更使用私有 stage、完整产物验证
及数据库 CAS 选择代次，保留中断现场，并拒绝旧代次的证据、审批和最终结果。精确重放处理
代次提交与 checkpoint 之间的窗口，迁移保留已有冻结审批。

验证范围见上表及脱敏 JSON。PR 应基于 CH-03A 工作说明依赖栈；本轮只完成本地提交。
下一项为 **CH-03B：发布绑定、幂等意图与远端对账**，本轮不自动启动。
