# CH-07A 验收记录 · 2026-10-22

状态：**VERIFIED**。本卡解除真实评测对单仓库的硬编码，交付受信任项目适配、manifest 迁移和第二项目夹具。没有新增真实模型质量实验。

## 基线与范围

- 接手时 `codex/ch-06c-data-boundaries@f6b4c3c` 干净且已推送；CH-06C 未合并，保留其完整历史，不从缺少这些实现的 main 重建。
- `git fetch --prune origin` 后 `origin/main=4d55a5b`，CH-06A 已由 PR #9 合并；硬依赖 CH-02A `a2cb1d5` 为祖先。
- 本卡分支 `codex/ch-07a-project-adapters`，固定验收源码 **`81dae390d218e9d973c629e9375c1fe55ed08300`**。后续提交仅补验收与交接文档。
- 用户已确认 CH-07A → CH-07B → CH-07C → CH-01B → CH-01C 五卡质量路线；本记录只覆盖 CH-07A，不把后续任务或预期收益算作完成。

## 实现

1. manifest schema 2 的 `projects` 显式绑定 source/read-only/test roots、可见/隐藏测试策略、Python 版本与路径、不可变镜像、可信依赖准备、配方路径和 hash。项目/task fingerprint、配方及 overlay 实际字节在加载时核对；非法路径、缺少许可/环境、错误 hash、未保护测试路径和不支持的命令拒绝。
2. `OfflineWorkspace` 由项目适配选择既有源码；只读路径优先，凭据受保护，create/delete/rename 不开启。源码缓存和镜像逐项目映射，镜像别名必须解析到 manifest 指定身份。
3. Agent buggy 导出、buggy/fixed 复现和候选独立验收分开；候选只重放获准源码到全新 buggy 导出。隐藏结果不回流，workflow 停在人工审批，不生成批准或发布。
4. 原 manifest 原字节归档为 `evals/real/manifest-v1.json`，SHA-256 `6b54dc349ba8f493e9cfb2702bf6c227c6b5c3da781e3246620779fdc9984471`；20 个 task 和 fingerprint 不变。v1 仅显式只读加载，新运行采用 evaluator config 9 / acceptance protocol 4 / withheld-junit-v2，旧目录与不同身份拒绝续写。
5. 第二项目 `tinyledger` 为自有合成夹具，使用 `src/`、`checks/` 和显式只读 Python 文件，支持 `/workspace/src`。真实模型入口拒绝把 synthetic 当作真实缺陷运行。

契约见 [project-adapters.md](project-adapters.md)。应用 SQL 版本仍为 `0007`，本卡不引入数据库迁移。

## 最终验证

| 验证 | 结果 |
| --- | --- |
| Ruff、diff、脚本语法 | 退出 0 |
| macOS Python 3.13.5 全量 | **350 passed / 42 skipped，85.66%**，74.28 秒 |
| 独立 Linux Python 3.12.15 全量 | **392 passed / 0 failed / 0 errors / 0 skipped，87.20%**，188.16 秒 |
| 条件入口 | 原有 40 项服务入口与新增两个 Docker 路径全部实跑 |
| 新增回归 | `test_project_adapters.py` 40 项，含两个实际 Docker 端到端路径 |
| Mock 工作流 | **10/10**，`deterministic-mock-v1`，原数据集 SHA 不变 |
| API smoke | completed，3 次调用，unknown=0，test_runner/finalize 各 1 次 |
| 实际 Worker 审批重启 | completed，调用 3→3，unknown=0，test_runner/finalize 各 1 次 |
| 原 20 例兼容 | task metadata/fingerprint、所有导出文件的读写权限、初始 issue-only 检索 **20/20 等价** |
| 原 20 例 Docker 复现 | **20/20 verified**：可见 buggy 通过、独立 buggy 行为失败、fixed 通过，JUnit 身份一致 |
| 来源核对 | 91 份源码/测试、测试/API 各 48 份安装包、37 份 eval 资产与工作区及 `81dae39` 一致 |
| 清理 | 专属 Compose 容器、卷、网络已移除；镜像缓存保留 |

宿主跳过的 42 项均在独立 Linux 验收中执行，不能将宿主结果描述为零跳过。原 20 例复现由 Python 3.13.5 可信编排器调度固定 Python 3.8 Docker 镜像；上游代码/测试没有在宿主执行。

专属 project：`repopilot_ch07a_20261022_82289`，API 端口 `58100`。PostgreSQL 16.15，LangGraph 1.2.16，checkpoint saver 3.2.0，LangSmith 0.14.11，pytest 8.4.2。

依赖缓存为已验收 CH-06C 测试镜像 `sha256:b76b7e77bc68cf47dc90da397c01036bfd1db454ef413ca6cd8a075cb4e5dd22`；重新安装当前源码。原 20 例和合成项目固定使用 `sha256:4488c6ee019a76b875dcbd99737dbeb5163c5bc5221c4ab491494a7890664e58`，各自的 Python path 分别绑定。没有在候选运行中安装依赖或联网。

## 第二项目的实际证明

- 确定性模型 one-shot 和完整 workflow 均执行实际 Docker 测试、独立候选重放与隐藏验收。
- 隐藏集有 2 个 fail-to-pass 和 1 个 pass-to-pass，修改仅为 `src/tinyledger/totals.py`。
- workflow 依次调用 Planner/Coder/Reviewer 并停在 awaiting_approval；没有合成批准或 GitHub 发布。
- 捕获全部模型角色输入，未出现 fixed commit、overlay hash、隐藏断言 sentinel、隐藏测试函数、`expected_fix_files`、`hidden_overlay` 或 `acceptance_argv`。
- 修改 `checks/`、源码根中的显式只读文件或新文件均拒绝；单独验收导出写入隐藏 overlay，Agent 导出保持原字节。
- 两项目不同镜像的受控单元测试核对 runtime/source 逐项目分派、精确续跑、环境变化拒绝混用，以及错误镜像覆盖在写记录前拒绝。

## 命令与原始证据

```bash
ruff check .
git diff --check
bash -n scripts/eval_real.sh scripts/ch06a_integration.sh scripts/ch07a_integration.sh
python -m pytest --cov=repopilot --cov-report=term-missing --cov-fail-under=70 -q
CH07A_DEPENDENCY_IMAGE=repopilot_ch06c_20261022_72448-test:local make ch07a-integration
python scripts/check_ch07a_compatibility.py \
  --source reports/real-corpus-cache/thefuck --output reports/ch07a-compatibility.json
MODEL_PROVIDER=mock python -m repopilot.real_evaluation \
  --source reports/real-corpus-cache/thefuck \
  --output reports/ch07a-real-reproduction --reproduce-only
```

宿主实际 Python/Ruff 来自本地已有 `/private/tmp/repopilot-ch01a-venv/bin/`。原始证据仅在忽略的 `reports/`：

- `ch07a-host-final.log/.xml`、`ch07a-final-integration.log`；
- `repopilot_ch07a_20261022_82289/` 的 JUnit、日志、Mock、smoke/recovery、镜像/版本/源码/资产 hash 和 cleanup；
- `ch07a-compatibility.json`、`ch07a-real-reproduction/`、`ch07a-source-audit.json`。

最初默认 `python3` 没有 pytest、仓库无 `.venv`，改用已有宿主验证环境；最初组合镜像名称查询返回不存在，按完整 ID 和单名称重查成功。宿主中间版为 346 passed / 42 skipped、84.73%；补充运行时绑定与资产 hash 回归后，完整重跑最终版本。两个早期 Docker 夹具通过不替代最终 Linux 验收。以上均无真实模型调用或未知费用。

脱敏机器摘要见 [2026-10-22-ch07a-verification.json](evidence/2026-10-22-ch07a-verification.json)。

## 历史口径、交付和限制

2026-09-09 真实模型报告、离线失败分析/账本及 Mock 数据集与 `b7cf1c9` 逐字节一致。本轮 **0 次真实模型 API 调用**。原结果仍为 one-shot 15/20、workflow 13/20，不能将本次固定版本复现或合成夹具通过写成修复率提升。

本卡不采集新的真实缺陷，不划分 development/holdout，不实现重复统计或自适应路由。下一张 CH-07B 依赖本卡已有实现和验收，需单独完成真实多项目 pilot 与冻结记录；CH-07C、CH-01B/C 仍为 TODO。

CH-06C 未合并的依赖历史保留；创建 PR 时应使用该分支为基线，或先确认其合并后再选择 main。当前验收不等于远端合并或发布。镜像配方 hash 不证明所有 OS/传递依赖可重建；新镜像/配置须更新 manifest 和实验目录。Docker 仍共享内核。

2026-10-22 已推送 `codex/ch-07a-project-adapters@4d76853`，并创建草稿 [PR #10](https://github.com/xsw932187210-cell/RepoPilot/pull/10)：base `codex/ch-06c-data-boundaries@f6b4c3c`，head `4d76853`，2 个提交、39 个文件，仅含 CH-07A。尚未合并，依赖合入后再核对/调整 main 基线。GitHub 连接器写入返回 403，改用已登录浏览器提交；API 已核实 Draft/Open 和两个分支身份。后续仅回填交付元数据，固定验收源码仍为 `81dae39`。
