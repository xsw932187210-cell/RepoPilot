# CH-01C：三策略真实模型对照（2026-10-23）

修正后的 v2 已结束全部 **18 个执行单元：16 completed、2 failed、0 pending**。
这是 **3 个独立缺陷、2 个项目、每策略重复 2 轮**，不是 18 个独立缺陷。
三组均未观察到通过独立验收的修复；本次没有建立 workflow 或 auto 的质量收益。
7 个单元到达人工审批，但均未通过独立验收，不能把审批到达数作为修复成功数。

公开汇总：[机器证据](evidence/2026-10-23-ch01c-v2-results.json)。
[v1 中断记录](ch01c-interrupted-2026-10-23.md) 的原 30 次请求及失败结果独立保留，
不加入下表的效果统计；两次仅在预算记账中相加。

## 固定实验身份

| 项目 | 本次值 |
| --- | --- |
| 源码提交 | `d1025b008cb1b1b0ae8988b5d6a3f7b2a06149fa` |
| 实现 SHA-256 | `f5a3797ec788a19b7889d40847c1f8f56bbe679d6ec648ba75f81e4d0e9c1952` |
| 计划 fingerprint | `c5390b31ee6ac1dc633f767cd9b425c504fe40a3bc5e18a5ebaeae1f4836856d` |
| 协议 / 结果目录 | `ch01c-pilot-v2` / `reports/ch01c-holdout-20261023-v2/` |
| 数据 | `python-pilot-v1` holdout：tqdm-003、cookiecutter-003、cookiecutter-004 |
| 冻结 fingerprint | `c616e16419a9313b46ff733dc201a8f0a8bd86de313301f1b9fae266099c2adf` |
| Planner / Coder | Google Cloud `gemini-3.5-flash`，temperature 1.0，输出上限 32,768 tokens |
| Reviewer | ChatGPT OAuth `gpt-6.1-sol`，本地 90 秒 / 65,536 流式字节限制 |
| SDK / 编排环境 | google-genai 2.32.0、openai 3.35.1；宿主临时环境 Python 3.13.5 |
| 策略 / 顺序 | oneshot、workflow、auto；每组 2 轮，`rotating-v1`，无 provider seed |
| 调用上限 | v2 共 78 次；每缺陷 × 策略 × 轮最多 6 次；与 v1 合计最多 108 次 |

完整配置见 [v2 协议](../evals/real/experiments/ch01c-pilot-v2.json) 和
[事前修正方案](ch01c-corrected-plan-2026-10-23.md)。ChatGPT 没有服务端输出 token
硬上限或 temperature 设置；本地限制不能当作服务端生成量或计费上限。
独立验收在固定项目 Docker 镜像中执行，断网、无模型凭据；模型请求由宿主编排器发起。
镜像 ID、权限与适配器身份均在机器证据中。实验期间仅整理文档，未改程序、提示、
检索预算、路由阈值或验收规则；计划身份再次核对一致。

## 结果和调用

| 策略 | 独立修复 / 选中单元 | completed / failed / pending | 到达审批 | API 预留 / 发起 | 执行耗时中位数 |
| --- | --- | --- | ---: | --- | ---: |
| oneshot | 0/6 | 6 / 0 / 0 | 3 | 14 / 14 | 82.600 秒 |
| workflow | 0/6 | 6 / 0 / 0 | 3 | 27 / 27 | 189.139 秒 |
| auto | 0/6 | 4 / 2 / 0 | 1 | 30 / 30 | 187.115 秒 |

`completed` 表示执行器正常返回，包括可见门槛拒绝、预算内未能产出有效修复等结果，
不表示候选通过测试。16 个 completed 均进行了独立验收，均未解决缺陷；2 个 failed
没有完整的独立验收结果，仍留在每组 6 个选中单元的分母中。
执行耗时累计同一单元的各次执行，包含请求和程序退避；不包含暂停后等待用户、
等待续跑的间隔或续跑前的公共复现准备，不能当作用户等待的完整墙钟时间。

以下 F 表示本单元未修复，E 表示调用层运行错误；斜杠后为已扣预算的请求数。

| 缺陷 | 轮次 | oneshot | workflow | auto |
| --- | ---: | --- | --- | --- |
| tqdm-003 | 1 | F / 1 | F / 5 | E / 3 |
| tqdm-003 | 2 | F / 1 | F / 5 | F / 6 |
| cookiecutter-003 | 1 | F / 1 | F / 5 | F / 6 |
| cookiecutter-003 | 2 | F / 1 | F / 3 | E / 4 |
| cookiecutter-004 | 1 | F / 6 | F / 4 | F / 5 |
| cookiecutter-004 | 2 | F / 4 | F / 5 | F / 6 |

两项运行错误均发生在 auto 的 ChatGPT Reviewer 调用阶段：tqdm-003 第 1 轮记录
`model_provider_non_retryable_failure`，没有足够信息继续细分原因；cookiecutter-003
第 2 轮记录 `model_response_size_exceeded`，触及事前声明的本地流式响应大小上限。
后者不是已证实的远端服务故障。本实验未提高上限或重跑这些终态记录。

## 项目、轮次和成对分析

| 项目 | 独立缺陷数 | 每策略执行单元 | oneshot 修复 / 请求 | workflow 修复 / 请求 | auto 修复 / 请求 |
| --- | ---: | ---: | --- | --- | --- |
| tqdm | 1 | 2 | 0/2 / 2 | 0/2 / 10 | 0/2 / 9 |
| Cookiecutter | 2 | 4 | 0/4 / 12 | 0/4 / 17 | 0/4 / 21 |

每轮、每策略均为 0/3；所有案例难度为 `unlabeled`，未从答案或结果补造难度标签。
三组两两比较均纳入 3 个独立缺陷，先在缺陷内平均两轮，再算“第二策略减第一策略”：
workflow−oneshot、auto−oneshot、auto−workflow 的成功率差均为 0。
按 2 个项目整组重采样的描述性 95% 区间均退化为 [0, 0]。全零观测必然产生这个结果，
不证明策略等效，也不证明真实成功率是零；样本是人为筛选的小型 pilot，不能推广。

auto 有 4 个单元返回路由记录，全部选择 workflow，另外 2 个运行错误单元没有路由结果；
未观察到 one-shot 初始路径或升级。已记录的上下文均超过 20,000 字符，Cookiecutter
还超过 2 个可编辑文件。因此本轮没有覆盖 auto 轻路径的节省场景。auto 的请求波动
不能归因为路由收益；提供商错误、不同生成与重试也是本次观测的一部分。

## 预算、未知结果和续跑

- v2 **71 次预留、71 次发起**：Google Cloud 58（Planner 16、Coder 42），ChatGPT Reviewer 13。
- 42 次调用记为成功、26 次记为已知失败、3 次为未知结果。未知结果同样扣预算，
  不是免费重试；它们分别记录在 cookiecutter-003/auto/第 2 轮（1 次）和
  cookiecutter-004/auto/第 2 轮（2 次）。没有预留与单元记录交接遗失的 orphan。
- 71 次中有 **19 次实际预留的 transport retry**。单元计数另有 22 次计划重试
  （9 次限流、13 次临时故障），其中有未获后续预算的重试意图，不能把 22 当作实际新增请求。
- 两次限流暂停时分别已用 31、59 次；对应 7、15 个终态单元逐条保持不变。
  续跑沿用同一身份、结果目录、每单元上限和预留 ID，未重置预算。
- 全部结束后再次执行同一命令，18 份单元文件与预留账本哈希完全一致，**新增模型请求 0**。
- 原 v1 的 30 次加本次 71 次，共 **101/108 次**。剩余 7 次没有用于调参、换模型或追加实验。

观测到的 token 下界为输入 346,970、输出 313,375、合计 660,345；部分失败/响应缺少完整
usage，因此准确 token 总量和金额均为 **unknown / null**。不能把观测下界当作完整用量。
Cloud 控制台的项目收费/抵扣信息没有与每次请求完整对账，本报告不声称代金券实际抵扣
金额。429 可能涉及配额或共享容量，不能仅凭它判断余额耗尽，参考
[Google 官方说明](https://cloud.google.com/vertex-ai/generative-ai/docs/error-code-429)。

## 验证与结论

v14 的全部三题在付费调用前通过导出基线、候选捕获和空初始 Diff 预检；Buggy/fixed
复现仍使用相同固定镜像。修正前后的工程证据分别见
[基线修正](evidence/2026-10-23-ch01c-baseline-correction.json)（29 项边界、3 题 Mock/Docker），
[CH-01B 容器验收](ch01b-verification-2026-10-23.md)（固定源码 4b6f7c9 的 538 项、Mock 10/10、
smoke 和两条实际 Worker 审批重启路径）。这些已有回归没有在本轮重复冒充为新源码全量测试。
最终源码 CI 与 PR 状态以 [#16](https://github.com/xsw932187210-cell/RepoPilot/pull/16) 为准，
完整功能链的 main 集成入口是 [#17](https://github.com/xsw932187210-cell/RepoPilot/pull/17)。

本卡完成了固定条件的三组实验及负结果交付，没有得到可宣传的修复质量提升。
保留显式路由和完整记账能力，默认策略继续为 workflow，不据此把 auto 升为默认。
公开训练污染未知，策展 QA 看过参考修复；v1 后仅修正运行时准备，没有依据保留集结果
调优提示、检索或路由。后续质量迭代应另立方案与预算，不能把已观察结果继续当作未见验证。
2026-09-09 的真实结果、失败分析/账本及原 Mock 案例保持原样，不能与本次不同模型、数据和
执行器的结果直接拼接比较。原始模型内容、补丁、私有会话和授权账本仅保留在忽略目录中。

复核入口：

```sh
# 元数据规划，不调用模型；返回冻结身份
python -m repopilot.eval_protocol

# 本机已完成授权的同目录续跑：复用 18 份终态记录，已实测新增请求为 0
python -m repopilot.eval_protocol --run \
  --source tqdm=reports/real-corpus-cache/tqdm \
  --source cookiecutter=reports/real-corpus-cache/cookiecutter
```

私有授权不会由命令制造。其他机器或新的独立试验必须先取得适用授权，不能复制或清零
本机账本来获取一轮新的 78/108 次请求。
