# CH-18 交付验证记录

状态：本地验收通过，PR 合并前核对远端 CI。验证从 2026-10-23 持续至 2026-10-24。

基线为 `main@5201a8ee9d682551ef8aa06b178c68a1fc256607`，工作开始时干净。
用户范围为离线失败分析、CH-18、CH-19；本轮没有真实模型请求授权扩展。

## 已完成的准备

- uv 0.9.6 及安装包哈希固定，93 个锁内项目/依赖记录；已有环境已安装包版本保持一致。
- macOS CPython 3.13.5 的全新临时虚拟环境完成锁安装、doctor 和 Mock 10/10。
- 新增诊断/公开汇总边界的 12 项定向用例与全仓 ruff 通过。
- Docker Desktop 29.5.3、Linux aarch64、容器 CPython 3.12.15；PostgreSQL/Redis
  固定镜像清单同时提供 amd64/arm64 对应架构，清单查询不代表两种平台都已实跑。

## 过程中发现的问题

1. 首次干净 editable 安装缺少 `editables`，已将 0.5 与 hatchling 一起纳入锁内 build group，
   不依赖构建时临时解析。修正后干净环境安装、doctor 与 Mock 通过。
2. 首轮 Docker 全量 **550 passed、1 failed、0 skipped，86.92%**；新配置错误测试未清除
   容器的 `MODEL_MAX_CALLS` 覆盖，临时 `.env` 没有生效。测试显式移除该环境变量后重验，
   没有放宽配置校验或验收条件。首轮专属资源已清理，失败记录保留于
   `reports/repopilot_ch18_20261023_66677/`，不作为通过证据。
3. 远端 Ubuntu CI 首轮（[run 39984330712](https://github.com/xsw932187210-cell/RepoPilot/actions/runs/39984330712)）
   **536 passed、15 failed、0 skipped，86.16%**。容器内 Docker SDK 明确返回 socket
   `PermissionError`；集成 Compose 原来固定附加组 0，仅适配了 Docker Desktop 的 VM。
   已改为 Linux 读取宿主 socket 的数字 GID、macOS Desktop 使用 0，并允许显式数字覆盖；
   Worker 与 probe 使用同一映射。没有修改 socket 权限、改用 root 或放宽沙箱隔离。
   新增容器内 Docker 连接预检，避免相同环境问题扩散成多项 Worker 失败。
   失败 CI 及其 `ok=false` 汇总保留，修正后的远端结果另行记录。

## 最终验证

第二轮专属项目 `repopilot_ch18_20261023_67635`：**551 passed、0 failed、0 skipped，
87.00% 行覆盖率，285.89 秒**。完整服务矩阵、doctor、Mock **10/10**、API smoke、
workflow/oneshot 两条实际 Worker 审批重启路径全部通过；验证输入在运行期间保持一致。
工作流重启调用数 3→3，oneshot 1→1，未知调用均为 0，审批对象/候选/测试证据不变。

容器内 `src`、安装包与实际 API 安装包逐文件摘要一致；源码/测试树 SHA-256 为
`70593649d931e9a3016ff73a682b3f7a4404c0e01da1929835729ab26a98eb28`。
这次验证在开发改动尚未提交时执行，记录基线提交与完整输入快照，不能把基线提交独自
当作被测完整源码。交付提交与输入摘要在提交后核对；机器证据保留该时间关系。

专属 Compose 容器、数据卷与网络全部清理。另一个独立合成数据卷在两轮验证之后仍保留
完全相同的 marker，核对后只移除了这个本轮创建的探针卷。没有清理已有项目或全局镜像。
旧实验的 `.env`、授权目录、报告、上游缓存没有挂载进验证环境。

公开[机器汇总](evidence/2026-10-24-ch18-delivery.json)包含镜像 ID、锁摘要、来源摘要、
测试计数、重启和清理状态；原始日志仍位于忽略目录，不作为 CI artifact 整体上传。
验证入口为 `make verify`；安装、故障排查和触发规则见[交付说明](reproducible-delivery.md)。

本轮真实模型请求为 0，原 101/108 的账本未变。上述结论只覆盖已运行的平台与故障矩阵，
不增加修复质量、任意崩溃自动恢复、Worker fencing 或实际 GitHub 发布的主张。
