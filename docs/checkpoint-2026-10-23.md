# RepoPilot 项目暂停交接 · 2026-10-23

> 历史暂停快照：用户后来已明确恢复开发，五卡及依赖现已由 [PR #17](https://github.com/xsw932187210-cell/RepoPilot/pull/17) 合入 main（集成提交 `ff7006c`）。本文件保留当时状态；当前入口为 [Openspec.md](../Openspec.md) 和 [真实对照结果](real-evaluation-results-2026-10-23.md)，不要按下文旧 TODO 重复实施或清零预算。

用户因 5 小时额度将满，明确要求先冻结项目、暂停开发，明天再续。
暂停点选在 **CH-07C 源码及隔离验收完成，CH-01B 尚未开始实现**。
没有后台真实模型实验需要继续等待。恢复必须由用户明确提出；不自动续跑、
不定时唤醒、不追加真实 API 请求。

## Git 与交付位置

- 仓库：`/Users/asaki/Documents/秋招/RepoPilot`。
- 分支：`codex/ch-07c-repeated-evaluations`。
- 固定验收源码：`3615d6b3a23b8121612a12fb6cbcf88fe5707826`。
- 实现提交：`7341a2e`；`3615d6b` 补齐专属集成脚本允许列表。
- 本交接文件及验收摘要将一并作本地快照提交；以该分支最新提交为恢复入口。
- CH-07C 尚未推送、创建 PR 或合并。此次冻结只做本地保存。
- PR #12 混合模型已合入 CH-07A 功能分支，merge `d7e9d21`；PR #13
  CH-07B 同样已合入 CH-07A 功能分支，merge `58d581b`。此前 CI 各 2/2。
- 完整依赖链尚未进入远端 main。冻结时 `origin/main` 为 `4d55a5b`，本地
  `main` 为较旧的 `5d7ac36`；不要从本地 main 开始下一卡而丢掉前置实现。
- 原有 push / PR / merge 授权仍存在，暂停期间不执行远端交付。恢复后重新核对
  Git 和 PR 实况，再收尾 CH-07C PR，之后开启 CH-01B。

## 已完成与待做

已批准的五卡顺序：CH-07A → CH-07B → CH-07C → CH-01B → CH-01C。

- CH-07A：适配能力已验收，PR #10 合入功能依赖链。
- CH-07B：23 例 / 3 项目的冻结 pilot 已验收，PR #13 已合并到功能分支。
- CH-07C：重复身份、持久化总预算预留、轮换顺序、分层/成对报告已验收。
  Linux 512 passed / 0 skipped，87.56%；Mock 10/10、smoke、Worker 重启通过。
  104 份源码/测试及测试/API 各 54 份已安装文件均与固定提交逐项一致。
- CH-01B：**TODO，只有源码阅读和设计思考，没有实现、没有既定方案**。
  需要显式 oneshot/workflow/auto、确定性理由/版本、共享预算升级，以及两条路径
  均保留权限、Docker 测试和人工发布审批。旧请求默认 workflow。
- CH-01C：**TODO，未开始真实推理**。需先完成路由器，再冻结三组配置和源码；
  统一端到端验收后才运行真实对照。

用户最新工作偏好是：**先把余下代码写完，再统一做端到端测试**。
不要恢复成每写一小段就反复全量回归。本次只收尾已经启动的 CH-07C 验证，
没有为了冻结新增模型调用或重复测试。

## 模型、预算与数据

- Gemini 使用原生 Google SDK，当前主模型 `gemini-3.5-flash`，负责 Planner/Coder。
- ChatGPT OAuth Reviewer 当前 `gpt-6.1-sol`，使用现有授权；需要时走既有刷新流程。
  先检查授权有效性，不能假定明天的短期 access token 仍有效。
- GPT public Responses 使用 streaming、`store=false`；无服务端输出 Token 硬上限，
  本地 90 秒 / 64 KB 限制。当前 Gemini 输出上限 4096，正式实验前应依据通用
  完整文件输出需求明确上限并冻结，不能按保留集隐藏结果调参。
- DeepSeek Key 保留但本轮未用；不自动改为付费降级路线。
- 旧混合连通额度 **6 次已耗尽**（含失败/重试），不可清零或复用。
- 新 CH-01C 授权：3 个保留案例 × 3 策略 × 2 重复，每单元最多 6 次，
  Gemini/GPT 合计 **最多 108 次，含失败与重试**。冻结时 **0/108**。
- 本地授权记录：`.local/evaluation-authorization/ch01c.json`，已核对
  `authorized_not_started`。真实实验必须使用持久化预算；重启不重置。
- `.env`、`.local/chatgpt/` 和授权记录均为本地私有产物，不提交令牌、Key 或账号信息。
- 冻结入口：`evals/real/freeze-pilot-v1.json`，fingerprint
  `c616e16419a9313b46ff733dc201a8f0a8bd86de313301f1b9fae266099c2adf`。
  原 The Fuck 20 例为开发集，新 tqdm 1 + Cookiecutter 2 为保留集。
  策展者看过参考改动，未用于提示/路由调优；公开训练污染未知。
- 重复配置模板目前只有 oneshot/workflow，示例 72 次，**不是新实验授权**。
  auto 和三组 108 次的最终实验配置要在 CH-01B 后实现/冻结。

## 本地证据与环境

- [CH-07C 验收](ch07c-verification-2026-10-23.md)、
  [机器摘要](evidence/2026-10-23-ch07c-verification.json)、
  [统计契约](repeated-evaluations.md)、
  [人工可算夹具](evidence/2026-10-23-ch07c-hand-computable.json) 随 Git 保存。
- 忽略的原始产物仍在 `reports/repopilot_ch07c_20261023/`、
  `reports/ch07c-container-final.log`、`reports/ch07c-host.*`；未删除历史中间失败。
- 测试项目 `repopilot_ch07c_20261023`、端口 58120：容器、卷、网络已清理；
  `cleanup.json` 为 true。没有该项目的 Worker/实验留在后台运行。
- 镜像保留，依赖缓存 `repopilot-model-deps:20261023`；实际 app/test/sandbox
  镜像 ID 见机器摘要。真实任务的固定镜像与仓库缓存均保留。
- 宿主临时 Python：`/private/tmp/repopilot-ch01a-venv/bin/python`。
  明天先检查是否仍存在；临时目录和 Docker 缓存可能被外部清理，不能当永久依赖。
- `.env`、OAuth、忽略的 reports/上游缓存/镜像不会自动随 Git 同步到另一台电脑。

## 明天恢复顺序

1. 阅读 `AGENTS.md`、`Openspec.md`、本文件及 CH-07C 验收。先核对 Git 状态、
   HEAD 和远端 PR，保留新出现的用户修改，不 reset / clean。
2. CH-07C 已有固定源码和完整验收，不为恢复而重跑 512 项；先收尾其交付。
3. 进入 CH-01B。待解决的设计包括：API/持久化如何保存显式策略；在线 one-shot
   与历史评测一调用基线的定义；auto 只能依赖可见输入，升级不能依赖隐藏验收。
   尚未决定是否增加数据库迁移、是否保留在线 Reviewer，不能当既定实现。
4. 先完成路由与 CH-01C 编排代码，然后统一 Mock / 数据库 / Worker / 端到端验收。
   运行真实对照前固定精确模型、输出/调用上限、路由版本、顺序及报告身份。
5. 在同一持久化账本内执行最多 108 次真实请求；遇 quota 保留 pending 续跑，
   不通过新目录清预算，不为连通性额外发送未记账请求。
6. 按实际观察写报告并完成 PR 交付，再审查整条功能依赖链进入 main。

历史事实保持：2026-09-09 one-shot 15/20、workflow 13/20，workflow 未超过
one-shot。2026-09-09 报告、失败账本及 Mock 数据集与 `b7cf1c9` 相同。
没有新的真实修复成功率；重复次数不增加独立缺陷数，缺 usage 的金额为空。

可直接发给明天的助手：

> 继续 /Users/asaki/Documents/秋招/RepoPilot。先读取 AGENTS.md、Openspec.md
> 和 docs/checkpoint-2026-10-23.md，核对冻结分支与 Git 状态。从 CH-07C
> 已验证检查点接续交付，然后实现 CH-01B、CH-01C。先写完代码再统一端到端测试；
> 已批准真实对照最多 108 次（含失败/重试），冻结时尚未消耗。保留历史评测口径。
