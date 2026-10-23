# Gemini SDK 与 ChatGPT Reviewer 验收 · 2026-10-23

本次是用户确认的模型接入支撑变更，不代表 CH-07B/07C/01B/01C 已完成，也不改变 2026-09-09 历史质量结果。

## 固定源码

- 起始基线 `dbef10172204b0cb7aa83cd36e0f96c2823d17f9`，分支 `codex/google-cloud-express-model`。
- 实现 `5926c25`；真实流适配修复 `285e762`；容器工具打包 `28cc88d`。
- 固定验收源码 **`28cc88d80e79234306cd0f811cadec387fb5829d`**。
- 原有未提交的 `evals/real/environments/pilot-python38.Dockerfile` 保留，未纳入接入提交。

## 行为与边界

Google SDK 显式使用 Cloud Key、Cloud Express 地址和单次 SDK 尝试，Planner/Coder 沿用既有提示和权限。Reviewer 可路由到 ChatGPT OAuth；所有角色、返工及 transport retry 共用原持久化预算。Mock 保持完全离线，没有自动计费 fallback。

GPT 使用 Responses 公开端点、`store=false`、`stream=true`，SDK 重试为零。原私有 client/host 的令牌可串行刷新并原子保存，返回新 ID token 时验证签名与账号绑定。临时错误保留凭据，确认 refresh token 无效才要求重登。只有完成终帧及结构校验通过才算成功；终帧不重复正文时读取已完成的输出项，拒绝半截或冲突数据。

GPT 套餐路径不接受服务端 max_output_tokens/temperature。本地事件字节和整次请求时长限制不能控制服务端已经生成或计费的 Token。角色 profile 明确这一差异，纳入 evaluator v11 身份；旧实验记录不改写。

## 离线验证

| 环境 | 结果 | 范围 |
| --- | --- | --- |
| macOS / Python 3.13.5 | 421 passed / 42 skipped，85.93%，59.51s | 服务条件未开启的 42 项未算通过 |
| Linux / Python 3.12 | 463 passed / 0 skipped，87.41%，178.14s | 原服务入口及两个实际 Docker 适配路径全跑 |
| Mock | 10/10 | 显式 mock 在宿主混合配置下仍不调用外部模型 |
| API smoke / Worker 审批重启 | 均通过 | 完整审批/manifest/证据保持一致；调用 3→3、unknown=0，test_runner/finalize 各一次 |

测试使用真实 Google/OpenAI SDK 的模拟 HTTP transport，覆盖 schema、usage、Key 选择、429/503/401、无隐式重试、半截/冲突流、超限、令牌轮换/并发刷新/取消释放锁，以及 SQLite 持久化的跨 provider 总预算。连接工具另验证预留先落盘、续跑不清零、配置不同拒绝复用。

初版完整宿主结果为 415 passed / 42 skipped、85.97%；真实流缺陷修复和新增回归后完整重跑，以最终表格为准。早期导入路径、错误码断言及 async pytest 上下文错误均已修正，中间失败没有计入最终通过。

## 真实连接检查

用户确认的新预算为 **Gemini + GPT 合计最多 6 次推理请求，含失败与重试**，在私有 `.local/model-connection-checks/checks.json` 中随预留/发送/结果落盘。最终使用 6/6，没有增加第七次。

1. 前两次 GPT 生成结束，但适配器只读终帧 `output=[]`，未通过结构校验。诊断确认正文在已完成输出项中；修复并补回归后续跑，前两次预算没有清零。
2. 后四次通过：GPT 独立 Reviewer、Gemini Planner、Gemini Coder、GPT Reviewer。两次 GPT 审查都拒绝明确没有测试通过证据的候选。
3. 新预算内 GPT 4 次（2 成功/2 适配失败）、Gemini 2 次（均成功）。此前独立 Google 预检是另一份已结束的 3 次预算（2 成功/1 结构校验失败）；两份合计 9 次，不混称本次 6 次。
4. 新预算仅成功请求观察到 3,782 tokens；失败 usage 未完整进入账本，因此整体 `token_usage_complete=false`。这个数不是全部调用用量，不据此推算精确费用。

这些是合成连接检查：没有执行模型生成的代码，没有运行真实缺陷或批量修复评测，也没有验证完整真实模型图。不能由此声称混合模型提高修复率。GPT 另已用原 client/host 成功续期并读取当前模型目录，授权操作没有发送推理。

## 容器、隐私和来源证明

先前取消的构建未执行；用户随后明确允许构建和隔离回归，本次才执行。复用已有 CH-07A 依赖镜像安装当前 SDK，再用隔离 harness 重装当前源码。项目 `repopilot_ch07a_models_20261023`、端口 58110，`--env-file /dev/null`，不挂载真实令牌或传入模型 Key。

98 份源码/测试和测试/API 镜像各 51 份安装包源码，与工作区及固定提交逐字节相同。SDK/依赖：Google Gen AI 2.32.0、OpenAI 3.35.1、PyJWT 2.15.1、cryptography 50.0.2、tenacity 9.1.4。专属容器、卷、网络均已清理，镜像保留。

实际 OAuth 生成在宿主验证；Docker 中真实凭据绑定、文件所有权和续期未做生成验收，部署者仍需确认私有目录的 UID/读写权限。凭据仅保存在本地 `.env`/`.local/`，二者均排除于 Git 和 Docker context。交付前对可达 Git 历史扫描当前本地凭据的精确匹配为零；此检查不承诺发现任意未知秘密。账号身份、令牌、Key、券详情和模型正文不进入公开记录。

机器摘要：[2026-10-23-mixed-model-verification.json](evidence/2026-10-23-mixed-model-verification.json)。原始宿主结果：忽略目录 `reports/mixed-model-final*`；容器日志、来源 hash、镜像、清理：`reports/repopilot_ch07a_models_20261023/`。

## 状态与复跑

按上述范围为 VERIFIED，PR/合并状态以远端实际结果为准；继续 CH-07B 筛选、复现、冻结。配置与命令见[接入契约](model-providers.md)。`python scripts/check_model_connections.py` 仅检查本地状态；真实检查须有已确认预算，不能靠重跑命令清零。独立 `CH07A_COMPOSE_PROJECT/CH07A_API_PORT/CH07A_DEPENDENCY_IMAGE` 可调用 `scripts/ch07a_integration.sh`，不得复用活跃服务数据。
