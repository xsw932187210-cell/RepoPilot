# CH-19 交付验证记录

状态：MERGED，由 [PR #21](https://github.com/xsw932187210-cell/RepoPilot/pull/21)
合入 main，提交 `5659dda1793580daf17d699fb779f74e13e82b0d`，合并前 3/3 检查通过。

本轮完成可运行演示、配置后真实任务路径、能力证据索引、设计取舍、第三方来源、贡献
流程及版本说明草稿。前置 CH-01A、CH-18 已合并；当前状态见[集成记录](delivery-status-2026-10-24.md)。
真实模型质量结果与剩余预算保持原样。

## 本地与容器验证

- macOS arm64、Python 3.13.5：全仓 ruff、三项演示测试通过。用例实际运行图与 pytest，
  覆盖模拟同意、模拟拒绝和 CLI 默认只停在审批点；环境即使选择真实 provider、
  Reviewer 和开启 GitHub 写入，演示仍固定 Mock、禁用发布。
- Linux aarch64、Python 3.12.15：`make demo` 在断网、只读根文件系统、移除 capabilities、
  禁止提权、512 MiB/128 PID 限制的容器中通过。只挂载本轮 `reports/demo` 输出目录，
  没有凭据或 Docker socket 挂载。
- 首次实际测试退出码 1，第二轮退出码 0；同进程内存 checkpoint 重建后，模拟同意得到
  `completed`，角色调用 5→5、测试仅执行两轮，候选/证据绑定不变，原夹具未改变。
- 同一镜像直接运行未指定 `--decision` 的 CLI，返回 `awaiting_approval`，没有执行
  恢复或 finalize；输出完整自有夹具 Diff。拒绝路径由上述实际图测试覆盖。
- 镜像中安装的 demo 源码摘要与交付源码一致；锁一致性离线检查通过。
  新文档中引用的 115 个本地路径首轮全部存在，最终新增记录再做链接核对。
- `evals/real` 对比本轮开始的 `main@5201a8e` 没有变化；没有重写旧模型实验，
  新增真实模型请求 0。已知本机凭据与私有授权路径在推送前另行核对。

机器证据：[2026-10-24-ch19-demo.json](evidence/2026-10-24-ch19-demo.json)。
演示的原始日志和完整输出保留在忽略目录 `reports/`；公开证据只导出受控字段。

## 验证边界

短演示是自有夹具和脚本化 Mock，不证明模型修复质量，也不是 PostgreSQL/Worker 的
跨进程恢复。后者由 `make verify` 的实际 Worker 重启路径及
[CH-18 记录](ch18-verification-2026-10-23.md)单独证明。

[真实任务路径](demo.md)已按当前 Compose、API Key、候选 Diff 和绑定审批接口核对，
本轮没有用真实 provider 执行。用户若另行运行，需要先确认新任务调用预算并配置私有
凭据；旧实验余 7 次不能自动移作新预算。没有创建应用演示 PR、正式 Tag 或 Release。

## 远端 CI

PR #21 的源码提交 `af4b8ebd9521da36641e48d67448e568381e604a`：

- [快 CI 39993208569](https://github.com/xsw932187210-cell/RepoPilot/actions/runs/39993208569)
  通过：510 passed、45 skipped、85.97%，Mock 10/10；跳过项不是服务验收。
- [完整服务 CI 39993208626](https://github.com/xsw932187210-cell/RepoPilot/actions/runs/39993208626)
  通过：**555 passed、0 skipped、87.07%**，pytest 340.71 秒；doctor、Mock 10/10、API
  smoke、workflow/oneshot 实际 Worker 审批重启、安装包/API 源码一致、输入未变及专属
  容器/卷/网络清理全部通过。
- CI 检出的临时 PR merge commit 为 `f0273ad5214585a793ab1f7492782d838d4f5fe6`。
  源码/测试树摘要为 `d38653ccaf18ab23f0a7663094dfd98e1dd9983fec703559cc8fd623798e9039`，
  与上述 feature 提交的 120 份 Python 源码/测试逐文件计算结果一致。
- 上一轮同分支因 Docker SDK context manager 使用错误而失败（553 passed、2 failed，
  [run 39991490914](https://github.com/xsw932187210-cell/RepoPilot/actions/runs/39991490914)）；
  修正与定向实际 Docker 验证见 CH-18 记录，失败结果保留。

公开[远端汇总](evidence/2026-10-24-remote-delivery.json)保留 checkout、feature 源码身份
与各类测试分母；main 合并身份见本页开头，不将 PR 临时 merge commit 当作已部署版本。
