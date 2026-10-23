# 能力与证据索引

RepoPilot 当前是可信单用户环境中的学习与作品集项目。下面将实现、程序验证和运行记录
分开列出；Mock 数字描述流程，真实模型数字描述各自固定实验，二者不混算。

## 工程能力

| 能力 | 主要代码 | 可执行验证 | 已有记录与范围 |
| --- | --- | --- | --- |
| 显式编排与单写者 | [builder.py](../src/repopilot/graph/builder.py)、[state.py](../src/repopilot/graph/state.py) | [test_graph.py](../tests/test_graph.py)、`make eval` | [Mock 契约](evaluation.md)；10 个确定性夹具不代表模型修复率 |
| 路径权限与拒绝后纠正 | [security.py](../src/repopilot/security.py)、[repository.py](../src/repopilot/repository.py) | [test_capabilities.py](../tests/test_capabilities.py)、[test_runtime_boundaries.py](../tests/test_runtime_boundaries.py) | [CH-02B 服务记录](ch02b-verification-2026-10-19.md)；全任务一次策略纠正，不能扩大写权限 |
| 暂存候选与完整代次 | [candidates.py](../src/repopilot/candidates.py)、[generations.py](../src/repopilot/generations.py) | [test_generations_postgres.py](../tests/test_generations_postgres.py) | [CH-05A 记录](ch05a-verification-2026-10-21.md)；完整代次 CAS，不是文件系统/数据库事务 |
| 候选、测试与人工审批绑定 | [approvals.py](../src/repopilot/approvals.py)、[api.py](../src/repopilot/api.py) | [test_approvals_postgres.py](../tests/test_approvals_postgres.py)、`make verify` | [CH-03A 记录](ch03a-verification-2026-10-19.md)；固定候选/证据的一次性决定 |
| 发布意图与远端对账 | [publication.py](../src/repopilot/publication.py)、[github.py](../src/repopilot/github.py) | [test_publication_postgres.py](../tests/test_publication_postgres.py) | [CH-03B 记录](ch03b-verification-2026-10-22.md)；GitHub 为测试 transport，未证明外部事务或多 Worker fencing |
| 模型请求预算与未知结果 | [model_calls.py](../src/repopilot/model_calls.py)、[eval_runtime.py](../src/repopilot/eval_runtime.py) | [test_model_calls_postgres.py](../tests/test_model_calls_postgres.py)、[test_eval_runtime.py](../tests/test_eval_runtime.py) | [调用契约](model-call-control.md)、[真实 v2](real-evaluation-results-2026-10-23.md)；失败/未知仍扣次数，金额不完整则为空 |
| 取消、截止与资源停止确认 | [execution.py](../src/repopilot/execution.py)、[sandbox.py](../src/repopilot/sandbox.py) | [test_execution_integration.py](../tests/test_execution_integration.py) | [CH-06A 记录](ch06a-verification-2026-10-22.md)；本地受控资源停止，不保证 provider 停止计费 |
| 公开数据与私有执行状态 | [data_boundary.py](../src/repopilot/data_boundary.py) | [test_data_boundary_postgres.py](../tests/test_data_boundary_postgres.py) | [CH-06C 记录](ch06c-verification-2026-10-22.md)；已知模式和白名单投影，不是任意秘密识别 |
| 锁安装与隔离交付 | [uv.lock](../uv.lock)、[doctor.py](../src/repopilot/doctor.py)、[验证入口](../scripts/ch18_integration.sh) | `make doctor`、`make check`、`make verify` | [CH-18 记录](ch18-verification-2026-10-23.md)；平台、覆盖率、跳过数与资源清理分别记录 |
| 无密钥的完整流程演示 | [demo.py](../src/repopilot/demo.py) | [test_demo.py](../tests/test_demo.py)、`make demo` | [CH-19 记录](ch19-verification-2026-10-24.md)；固定自有夹具，恢复限于同进程内存 checkpoint |

恢复证据中最直接的一条是：任务到达审批后重启实际 Worker，同一审批对象、候选和测试
证据保持一致，提交决定后继续原任务，模型调用次数不增加。这不涵盖任意节点崩溃后的
自动重领；队列 ACK、续租/fencing、恢复调度仍是 CH-04A/B/C。

## 模型质量：两批独立实验

| 实验 | 固定条件 | 结果 | 解释 |
| --- | --- | --- | --- |
| [2026-09-09](real-evaluation-results-2026-09-09.md) | The Fuck 20 个缺陷；Gemini 3.1 Flash Lite；oneshot/workflow | 15/20 与 13/20；调用 22 与 69 | 历史单项目结果；workflow 没有超过基线 |
| [2026-10-23 v2](real-evaluation-results-2026-10-23.md) | 3 个缺陷、2 个项目；三策略各重复 2 轮；Gemini + GPT Reviewer | 每组 0/6；16 completed、2 failed；v2 71 次 | 小型负结果，未证明质量或路由收益；18 单元不是 18 个独立缺陷 |

原 v1 的 30 次与 v2 的 71 次只在总预算里合计为 101/108，不拼接效果统计。
3 次未知响应已经扣预算；剩余 7 次未使用，也没有在本轮追加推理。
[逐单元离线分析](failure-analysis-2026-10-23.md)区分门槛不足、调用停止、测试收集和运行错误；
已有聚合数据不足以建立代码级根因。后续质量收益必须使用新方案和独立验证。

冻结语料总计 23 例/3 项目；原 20 例是开发集，3 例原保留集来自另两个项目。
参见[冻结语料与来源](real-task-corpus.md)、[重复统计契约](repeated-evaluations.md)。
保留集现已观察过结果，不能再次当作未见数据证明调优收益。

## 最新与历史记录怎么读

- 历史记录保留当时源码、镜像、测试数量和覆盖率；它们不表示本次重跑。
- 快测试中的服务 skip 单独报告；完整服务验证应为零 skip。
- 工程 PR 在 GitHub 合并，只证明本项目开发交付；不能替代应用发布器的真实 GitHub 集成验收。
- [演示说明](demo.md)提供新使用者的运行路径；[设计说明](design-decisions.md)提供讲解顺序；
  [版本说明草稿](release-notes.md)列出兼容性与已知限制。
