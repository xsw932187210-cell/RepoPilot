# CH-06C 验收记录 · 2026-10-22

CH-06C 已通过所列环境验收，状态为 **VERIFIED**。硬依赖为无；起始基线为干净的
main@4d55a5b5b83b617881b0b2bf9d2ea7dc94ef3785，本轮分支为
codex/ch-06c-data-boundaries。代码提交 d45cea3、52703d6；最终被验证的源码固定在
52703d6b81bf9451774451f58ca9120a2242a4f4，后续仅更新交付文档。

用户先授权创建、合并 CH-06A，再开启下一卡。已创建并合并
[PR #9](https://github.com/xsw932187210-cell/RepoPilot/pull/9)，head 为 825262c，
CI run 39748939113 success，页面 2/2 checks passed。合并文件树与原验收分支一致。
本轮只实现 CH-06C，没有自动开始其他任务卡。

## 实现与契约

- 统一已知 token、授权字段、凭据 URL、编码 URL、私钥块和多行 header 的处理；公开内容
  先扫描再截断，字段投影有明确 allowlist。事件保存、Redis 通知和 API 历史/SSE 都检查。
- Worker/API 错误只保留稳定 code/type。原始 provider 异常在 LangGraph 写错误 checkpoint
  之前替换；日志不保留异常原文、traceback 或结构化授权 header。
- Issue/审批反馈在保存前处理。异步图输入、模型输入和恢复 checkpoint 检查已知凭据；
  含凭据的源码整文件不进入检索，含凭据的编辑在 checkpoint/写入前拒绝。代码不做字符串替换。
- 私有 workspace/artifact 根目录为 0700；完整 Diff 要求配置并通过 API Key，返回原始字节、
  private/no-store。旧敏感 authority 返回 409，原始 SQL、artifact 和摘要不被静默修改。
- 模型/异步图关闭自动 LangSmith tracing。评测新增 public_data_version 指纹，结构化凭据
  不进入公开记录，配置拒绝凭据容器；旧实验目录拒绝混跑。
- 旧敏感 PR 标题阻止新的 POST；查到既有 PR 时保留已发生效果。发布绑定、取消对账、
  审批身份和历史真实质量结果均未被削弱。

字段、出口、兼容和手动保留规则见[数据边界契约](data-boundaries.md)。

## 最终结果

| 检查 | 结果 |
| --- | --- |
| Ruff、diff、脚本语法 | 退出 0 |
| macOS / Python 3.13.5 | 312 passed，40 skipped，84.94% coverage |
| 独立 Linux / Python 3.12.15 | **352 passed，0 failed，0 skipped，85.42% coverage** |
| 服务入口 | 35 项历史入口与 CH-06C 5 项全部实跑 |
| 新增回归 | 31 项数据边界离线测试、2 项评测测试、5 项服务测试，共 38 项 |
| Mock eval | 10/10，deterministic-mock-v1；数据集摘要不变 |
| API smoke | completed，3 次调用，unknown=0 |
| 实际 Worker 审批重启 | completed，调用 3→3，test_runner/finalize 各 1；完整审批/manifest/证据相同 |
| 来源 | 容器 90 份 Python 源码与工作区及 52703d6 相同；测试/API 各 48 份安装包文件相同 |
| 清理 | 两轮专属项目的容器、卷、网络均确认移除；镜像保留 |

宿主跳过项为未配置专用服务 URL 的条件入口，没有冒充通过。最终专属项目为
repopilot_ch06c_20261022_72448，API 58090；Docker Engine 29.5.3，
PostgreSQL 16.15，Redis 7.4 镜像，LangGraph 1.2.16、saver 3.2.0、
LangSmith 0.14.11、pytest 8.4.2。复用固定本地依赖镜像并重新安装当前源码。

复跑入口：

    ruff check .
    pytest --cov=repopilot --cov-report=term-missing --cov-fail-under=70 -q
    CH06C_DEPENDENCY_IMAGE=repopilot_ch06a_20261022_51033-test:local make ch06c-integration

本地依赖镜像不存在时去掉该环境变量，使用默认生产 Dockerfile。脚本复用 CH-06A
服务 harness，但强制独立 repopilot_ch06c_ 项目；拒绝复用同名资源并检查专用端口。

## 故障与持久化观察

| 场景 | 观察 |
| --- | --- |
| 伪 token、opaque 授权字段、编码 URL、私钥与多行错误 | 公开出口无原值，重复处理幂等；保留错误码与类型 |
| 嵌套 payload、新增未审查字段、历史 API/SSE | 字段投影生效；历史 SQL 原值不被 GET 改写 |
| 错误输入/HTTP 异常 | 不回显 Pydantic input/context，未知 provider 原文不进入响应或日志 |
| 私有 Diff 与旧 evidence | 未配置 key 503、错误 key 401；有 key 字节一致；敏感旧 evidence 409，摘要及私有对象不变 |
| 实际 API/Worker 与审批反馈 | 3 次调用完成；11 个 PostgreSQL checkpoint 无伪凭据 |
| provider 失败 | 3 个持久化 checkpoint 只含安全诊断，SQL 终态和事件无 provider 原文 |
| 含凭据的编辑路径/代码 | 各 7 个持久化 checkpoint 无伪 token，generation=0、retry=0、调用 2 次，未写新候选 |
| 正常审批恢复 | 候选代码字节一致，原始含凭据文件保留在私有存储，不进入模型上下文 |
| 遗留 PR 元数据 | 新 PR 请求被阻止；已存在 PR 仍对账且只有一个，未发送敏感标题 |
| 历史可靠性 | 迁移故障、代次硬退出、审批并发/投递恢复、五阶段发布恢复、真实容器/进程停止及兄弟资源存活均通过 |

测试入口：[离线边界](../tests/test_data_boundary.py)、
[服务探针](../tests/test_data_boundary_postgres.py)、
[评测回归](../tests/test_eval_runtime.py)。来源、镜像和日志摘要见
[机器记录](evidence/2026-10-22-ch06c-verification.json)。

## 中间失败及修复

第一轮专属项目 repopilot_ch06c_20261022_69075 基于 d45cea3，为 346 passed /
1 failed，85.47% coverage。旧 CH-02B 探针使用含 token 的非法路径，假定仍会进入
apply_policy 后硬退出；新边界会更早拒绝，因此夹具对 snapshot.next 的断言失败。
普通非法路径的策略纠正/硬退出恢复保持实跑；含 token 的路径和代码另用两个真实
PostgreSQL 探针检查“落 checkpoint 前拒绝”。没有删除恢复断言或把失败计为通过。
该轮没有继续执行 Mock/smoke/restart，清理已确认。

新增遗留发布测试的初版只更改 intent 标题，没有同步其任务标题，触发既有
publication_intent_conflict。最终夹具模拟一致的旧快照，保留授权一致性检查，并验证
新 POST 被阻止和既有 PR 被保留。宿主中间失败日志也留在忽略的 reports 目录。

## 交付与限制

原始报告在本地忽略目录 reports/repopilot_ch06c_20261022_72448/，最终宿主报告为
reports/ch06c-host-verification.log / .xml；公开记录只保存聚合结果和哈希。
本轮没有调用真实模型、执行应用真实 GitHub 发布或重跑缺陷质量实验。
2026-09-09 报告、失败账本和 Mock 数据集与 b7cf1c9 字节一致。

无需新增 SQL revision，仍为 0007。完整 Diff 客户端需配置 API Key；事件不再包含
Diff 预览，旧敏感 authority/checkpoint 需人工核对并重新验证。正则只覆盖已知模式，
可能误报；私有存储保留期仍由操作者管理。未实现生产加密、多用户隔离、自动清理、
Worker fencing、自动重领或 SSE 游标修复。

本验收记录提交时 CH-06C 未推送、未创建新 PR、未合并；实际推送后回填元数据。
下一张建议为 CH-04A（硬依赖 CH-09 已合并），质量方向可选 CH-07A；
本轮在 CH-06C 交付后停止。
