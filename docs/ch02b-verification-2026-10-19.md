# CH-02B：隔离服务验收

验证日期：2026-10-19。起始提交：`f27e847`，包含 CH-02B 原实现 `9032103` 与 CH-01A 交付。

本轮为已有的结构化策略纠正补充真实 PostgreSQL、Redis、Docker sandbox 和进程边界证据。结果支持将 CH-02B 标为 **VERIFIED**；不代表已创建或合并 PR，也不改变 2026-09-09 的真实模型修复率。

## 环境与运行入口

| 项目 | 本轮记录 |
| --- | --- |
| Docker Engine | 29.5.3，Docker Desktop / Linux arm64 容器 |
| PostgreSQL | 16.15，专用数据库 `repopilot_ch02b_test` |
| Redis | 本地 `redis:7.4-alpine` 镜像，专属 Compose 网络 |
| 容器 Python / pytest | 3.12.15 / 8.4.2 |
| LangGraph / PostgreSQL saver | 1.2.16 / 3.2.0 |
| 模型与发布 | `mock`；模型凭据为空；GitHub 写入关闭 |
| 最终验收实例 | `repopilot_ch02b_20261019_68210`；仅本机端口 58020 |
| 原始产物 | 本地忽略目录 `reports/repopilot_ch02b_20261019_68210/` |

运行 `make ch02b-integration` 或 `./scripts/ch02b_integration.sh`。脚本使用独立的 [Compose 配置](../docker-compose.ch02b-test.yml)，拒绝复用同名容器、网络或数据卷，检查端口，并使用 `--env-file /dev/null` 和明确的 Mock 配置；不会从个人 `.env` 取得模型或 GitHub 凭据。需在本地 Git 中保留旧版提交 `12b9e3d`，兼容性夹具由该提交的源码只读导出。

[公开计数与来源摘要](evidence/2026-10-19-ch02b-verification.json) 保留镜像 ID、版本、JUnit 结果、各场景断言、脚本/测试/关键源码摘要，以及原始产物的 SHA-256。它不包含源码正文、模型输出、凭据、隐藏验收或原始任务 Diff。

## 六个专项场景

测试位于 [test_policy_recovery_postgres.py](../tests/test_policy_recovery_postgres.py)，故障注入进程位于 [ch02b_process.py](../tests/probes/ch02b_process.py)。这些入口只在专用验收中加载，生产 Worker 没有新增故障开关。

前五个场景先使用生产 Worker 的依赖构造、模型调用账本和工作流完成一次非法提案。`apply_policy` 将拒绝与计数同步提交到真实 PostgreSQL 后，测试执行进程通过 `os._exit(86)` 硬退出；新的进程显式从同一个 `graph_thread_id` 继续，使用原任务账本。退出时 finally 无法替进程保存内存状态。

| 场景 | 重启后的可观察结果 | 总模型调用 |
| --- | --- | ---: |
| 首次越权后合法纠正 | 纠正次数仍为 1；只有源码合法修改落盘，测试字节不变；真实 Docker 测试通过，API 审批后原任务完成 | 4 |
| 持续越权 | 第二次拒绝以 `policy_retry_exhausted` 结束；次数仍为 1，合法源码项也未被部分写入 | 3 |
| 预算耗尽 | Planner 和首次 Coder 已耗尽 2 次预算；恢复后以 `model_call_budget_exhausted` 停止，账本没有额外请求 | 2 |
| 刷新后的旧摘要 | 在两个进程之间模拟外部源码变动；旧摘要以 `stale_content` 拒绝，外部标记保留，错误修复没有写入 | 3 |
| 非受控路径与脱敏 | 非法宿主路径被拒；规范化反馈和任务结果不含注入的私有标记；一次合法纠正后完成 | 4 |
| 旧版审批 checkpoint | 固定 `12b9e3d` 的 Worker/图源码生成真实审批 checkpoint；当前 API 和 Worker 接受决定并恢复同一任务/图线程，未重复测试或模型调用 | 3，恢复前后不变 |

结果：**6 passed，0 failed，0 skipped**。JUnit 总耗时 14.186 秒。每个场景同时检查任务终态、持久化计数/调用记录和实际工作区内容，避免仅以预设的 Mock 返回值判断通过。模型输出采用确定性夹具，服务、持久化和测试容器实际运行。

## 流程与真实 Worker 重启

同一专用实例中还完成了：

- 容器内确定性 Mock 评测 **10/10**，测试、修改范围和检索目标均通过；数据集 SHA-256 为 `6e5c1012e81c11d9a4cda4d2d5f761313398f5383bb0a3a71960e6569b25bdf7`。
- [API smoke](../scripts/smoke.sh)：创建任务、等待审批、批准、完成；调用预留/成功均为 3，unknown 为 0，测试及 finalize 各执行一次。
- [审批恢复 smoke](../scripts/recovery_smoke.sh)：实际重启本轮 Compose 的 Worker 服务，再通过 API 批准原任务；任务完成、调用总数仍为 3、测试和 finalize 各一次。
- 本地 Python 3.13.5 的完整离线套件：**154 passed、9 skipped、80.78% 行覆盖率**；lint 通过。其中 6 项为本轮专项服务测试，在上面的独立实例中另行实跑；另外 3 项专用 PostgreSQL 条件测试没有在本轮重复执行，不计为通过。
- 默认 Docker 构建通过；最终 `production` 阶段沿用运行时镜像，开发依赖和测试仅加入显式选择的 `integration-test` 阶段。

## 恢复范围与兼容策略

策略拒绝后的硬退出测试证明：显式恢复同一 PostgreSQL checkpoint 时，全任务纠正计数和数据库调用账本不清零。它使用测试入口接续执行，并未增加生产队列自动找回中途任务的能力。Redis ACK/reclaim、所有权 fencing 和任意节点的自动恢复仍归 CH-04。

审批等待期间的正式 Worker 重启，以及旧版图源码产生的审批 checkpoint，分别由正常 API/Worker 路径验证。兼容测试针对固定前置源码和本轮相同的依赖环境，不承诺任意旧框架版本、任意中途 checkpoint 或混跑新旧 Worker 都能恢复。

部署仍需先停止旧 Worker、备份并选择兼容的代码/checkpoint 组合。`apply_policy` 中途状态不能直接交给不认识该节点的旧图。文件预检、逐文件写入和 checkpoint 并非一个事务；冻结候选与文件代次一致性继续由 CH-03A/CH-05A 完成。

## 资源与记录

最终运行的清理报告为 `resources_removed=true`：脚本执行后分别查询专属标签，确认容器、网络和数据卷均为空；只删除本轮临时导出的旧源码目录。测试镜像和构建缓存保留，未执行全局 prune。

最初一次基础镜像拉取因 Docker Hub 临时 EOF 失败，没有启动服务；重试构建成功后完成验收。首次完整矩阵和最终带版本/清理记录的复跑均通过，公开证据以最终实例为准。

本轮没有付费模型调用或 GitHub 发布。下一步的 P0 工作是 CH-03A：将被测试、被审批和被发布的候选身份绑定起来。
