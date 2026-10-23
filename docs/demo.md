# 从离线演示到真实任务

## 无密钥的离线演示

在仓库根目录执行，需要 Docker/Compose、Make 和 Git。首次构建会下载固定依赖；镜像
准备好后，演示运行在断网、只读、限制内存/进程的容器里，不需要本机安装项目 SDK。

```sh
make demo
```

这条命令只运行自带的计算器，不接受自选仓库或 provider。模型是明确编排的 Mock：
第一份提案故意保留 bug，真实 pytest 失败；第二份提案修复加法，pytest 通过。它随后
停在人工审批节点，冻结候选和测试证据，再按命令参数指定的**模拟同意决定**恢复。
图重建后没有重新生成或重复测试，GitHub 写入关闭，没有创建或合并真实 PR。

预期能看到：

```text
创建内置计算器任务；全部角色为脚本化 Mock，真实 API 请求为 0。
第 1 轮测试：失败。
第 2 轮测试：通过。
已冻结候选与测试证据，图停在审批点；可查看保存的 candidate.diff。
从同进程内存 checkpoint 重建图，提交命令参数指定的模拟审批决定。
演示结束：completed；恢复没有新增角色调用或重复测试。
```

每次输出到独立 `reports/demo/<task-id>/`：`summary.json` 保存状态、实际测试退出码、
节点次数、审批绑定和调用计数；`candidate.diff` 是自有夹具的完整修改。临时工作区会回收，
原始夹具不变。首次失败是演示脚本刻意安排的，不是一次真实模型修复试验。

已有本机锁定环境时也可以运行：

```sh
.venv/bin/repopilot-demo --decision inspect  # 只展示审批点与 Diff
.venv/bin/repopilot-demo --decision reject   # 模拟拒绝，终态 cancelled
.venv/bin/repopilot-demo --decision approve  # 模拟同意，终态 completed
```

它们仅适用于受信任的内置夹具。标准展示优先使用 `make demo` 的外层 Docker。
这里的恢复是**同一个进程里重建图，复用内存 checkpoint**；没有演示 PostgreSQL 或
Worker 进程重启。要验证后者，运行：

```sh
make verify
```

verify 在独立服务中创建真实任务，等待审批，重启实际 Worker，再提交绑定决定，检查
同一任务完成、审批对象/候选/测试证据不变，模型调用数不增加。它还运行完整故障矩阵，
会比短演示耗时更久。记录见 [CH-18 验收](ch18-verification-2026-10-23.md)。

## 配置后运行一条真实模型任务

以下流程会消耗真实模型额度，**本轮未执行**。先为这次任务确认总请求上限，包含失败、
重试与三个角色；在本机设置 `REPOPILOT_DEMO_CALL_CAP`，并按[模型配置](model-providers.md)
填好私有 `.env`。旧实验剩余 7 次不会自动作为新任务预算。下面先使用主 provider 完成
三个角色；需要 GPT Reviewer 时按模型文档显式授权和绑定私有目录。

在新克隆目录准备配置时可用 `cp -n .env.example .env`，它不会覆盖已有配置。
保持 `GITHUB_WRITE_ENABLED=false`。本流程需要查看完整 Diff，因此先在本机设置
`REPOPILOT_DEMO_API_KEY`，并让它成为该演示实例的 API Key；不在聊天或公开报告中填写值。
Linux 主机还需按 README 设置 `DOCKER_SOCKET_GID` 为宿主 socket 的数字组；Docker
Desktop 默认使用 0。

```sh
: "${REPOPILOT_DEMO_CALL_CAP:?请先确定本次任务调用上限}"
API_KEY="${REPOPILOT_DEMO_API_KEY:?请先在本地设置演示 API Key}" \
  REPOPILOT_API_PORT=58090 MODEL_MAX_CALLS="$REPOPILOT_DEMO_CALL_CAP" \
  REVIEW_MODEL_PROVIDER= GITHUB_WRITE_ENABLED=false \
  docker compose -p repopilot_real_demo up --build -d
```

API 只绑定本机 `127.0.0.1:58090`。本例使用内置仓库让操作者先熟悉流程；真实仓库任务
可以替换为允许的公开 GitHub HTTPS 地址。创建任务的请求就是推理开始点：

```sh
curl -fsS http://127.0.0.1:58090/api/v1/tasks \
  -H "X-API-Key: ${REPOPILOT_DEMO_API_KEY:-}" \
  -H 'Content-Type: application/json' \
  -d '{"repository_url":"demo://buggy-calculator","issue_title":"Fix add returning the wrong result","issue_body":"The add function subtracts instead of adding. Preserve subtract and make the smallest safe correction.","test_command":"python -m pytest -q","max_iterations":2,"execution_strategy":"workflow"}'
```

复制响应中的任务 ID 后，可查以下路径。只有到达 `awaiting_approval` 才执行后续审批：

| 路径 | 查看内容 |
| --- | --- |
| `/api/v1/tasks/TASK_ID` | queued/running/awaiting_approval 或终态、错误、版本 |
| `/api/v1/tasks/TASK_ID/events` | 持久化节点进度；SSE 仍存在已知切换空窗 |
| `/api/v1/tasks/TASK_ID/metrics` | 角色调用、重试、未知响应及节点次数 |
| `/api/v1/tasks/TASK_ID/approval` | 固定候选、测试证据、Reviewer、审批版本 |
| `/api/v1/tasks/TASK_ID/candidates/CANDIDATE_ID/diff` | 完整 Diff；必须配置并提供 API Key，否则 503/401 |

检查完整 Diff 和测试证据后，在本机设置 `TASK_ID` 并主动提交决定：

```sh
curl -fsS "http://127.0.0.1:58090/api/v1/tasks/$TASK_ID/approval" \
  -H "X-API-Key: ${REPOPILOT_DEMO_API_KEY:-}" \
  | python3 scripts/approval_payload.py 'Reviewed full diff and test evidence' \
  | curl -fsS -X POST "http://127.0.0.1:58090/api/v1/tasks/$TASK_ID/approval" \
      -H "X-API-Key: ${REPOPILOT_DEMO_API_KEY:-}" \
      -H 'Content-Type: application/json' --data-binary @-
```

这份决定固定绑定原候选和证据；旧绑定冲突返回 409，不能用查询最新结果补造批准。
Redis 投递失败时决定仍保留为 pending，按响应中的 decision ID 显式重投；目前没有后台
自动队列恢复。由于 GitHub 写入关闭，任务 completed 只表示候选流程完成，不代表 PR 或合并。

`docker compose -p repopilot_real_demo stop` 可停止这个手动演示项目，数据卷保留。
不要用验证环境的清理命令处理真实任务数据。真实路径的源码/API 绑定由既有 Mock 服务
验收覆盖，本轮不把配置核对写成真实 provider 在线端到端验收。

## 看完后如何核验与讲解

按[证据索引](evidence-index.md)核对每个声明的代码、测试、日期和版本，再阅读
[设计取舍](design-decisions.md)和[负结果分析](failure-analysis-2026-10-23.md)。
当前没有证明多角色提高修复率；演示通过仅证明已知夹具的流程与边界按预期工作。
