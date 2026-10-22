# Gemini SDK 与 ChatGPT Reviewer

本文描述接入配置，不代表新的真实修复率结论。原 2026-09-09 真实实验配置和报告保持不变。

## 本地配置

使用独立 Python 环境安装 `.[dev,oauth]`。SDK 是调用客户端，模型在服务端运行。

```dotenv
MODEL_PROVIDER=google_cloud
MODEL_NAME=gemini-3.5-flash
GOOGLE_CLOUD_API_KEY=<只在本地填写>
MODEL_TEMPERATURE=1.0
MODEL_MAX_OUTPUT_TOKENS=4096
REVIEW_MODEL_PROVIDER=chatgpt
REVIEW_MODEL_NAME=gpt-6.1-sol
CHATGPT_STORAGE_DIR=.local/chatgpt
CHATGPT_MAX_RESPONSE_BYTES=65536
```

Cloud Express 模式使用显式 Cloud API Key 与 `aiplatform.googleapis.com`；不需要额外的项目 ID、location 或 ADC。`MODEL_NAME` 就是模型名，不另设 `GOOGLE_CLOUD_MODEL`。原 AI Studio Key、DeepSeek Key 不会被自动选用或覆盖。Google Key 所属账单与优惠券抵扣仍以 Cloud 账单为准。

Planner、Coder 使用主模型；设置 Reviewer override 后，只有只读 Reviewer 调用 ChatGPT。未设置 override 时沿用主模型。`MODEL_PROVIDER=mock` 总是完全离线并忽略真实 Reviewer override。确定性测试门槛、修改权限、人工审批和 GitHub 发布协议继续生效。

ChatGPT 模型名必须来自当前账号模型目录。这里的模型名是配置示例，不保证对每个账号可用，也不代表不可变的版本快照。

## 授权与续期

```bash
python -m repopilot.chatgpt_auth login
python -m repopilot.chatgpt_auth status
python -m repopilot.chatgpt_auth refresh
python -m repopilot.chatgpt_auth models
```

首次授权仍通过用户浏览器完成。刷新使用同一 client/host，序列化会话刷新，原子替换最新 access/refresh token；普通网络/临时故障保留原凭据，确认 refresh token 不可用才清除令牌并要求重新登录。刷新返回 ID token 时验证签名、issuer、audience、expiry 和 subject；不能切换到别人的账号。自动刷新在已预留的角色调用内进行，不绕开任务预算。刷新可能已在服务端轮换但本地未保存的崩溃窗口无法事务化，此时可能需要重新登录。

令牌仅留在私有 `.local/chatgpt/`（目录 0700、文件 0600）；`.env` 和 `.local/` 同时排除于 Git 和 Docker build context。公开代码不包含用户凭据。`status` 不探测 Worker，`workflow_adapter_connected=null` 表示该项未检测；`inference_requests=0` 表示该授权 CLI 命令不发送推理，不是整个账号的历史调用总数。

## 实际预算与协议

- Planner、Coder、Reviewer、策略纠正及 transport retry 共用同一个任务调用预算；在线默认总计 12 次，失败和重试也计入；两个 provider 不各拿一份预算。
- Google SDK 显式 `attempts=1`，OpenAI SDK `max_retries=0`；外层控制器负责全部模型重试。调用账本保存每次的实际 provider、model、role、adapter/schema version。没有自动切换到其他计费路径。
- Google 发送 temperature 和服务端 max output tokens。ChatGPT 套餐 OAuth 当前只使用 `POST /v1/responses`、`store=false`、`stream=true`，把 system 提示转换为 instructions，省略该路径不接受的 temperature/max_output_tokens 等参数。
- **GPT 没有服务端输出 Token 硬上限保证**。本地限制接收的事件字节数与整次请求时长；这不能限制服务端已经生成或计量的 Token。响应超限或流未完成消耗预算且记 unknown；没有 `response.completed` 不算成功。流中的套餐/应用额度错误停止调用，不默默重试到另一家。
- 只有完整可信 usage 才进入精确 Token 统计。OpenAI reasoning 已包含在 output_tokens，不重复相加；Google thoughts 计入输出并核对 total。不完整或失败 usage 保持 unknown，不推算金额。
- `model_role_profile` 明确列出各角色模型、temperature 是否适用、服务端输出上限和 GPT 本地字节限制；真实 evaluator v11 将它纳入实验身份，禁止与单模型或另一种角色配置的记录混跑。

## 验证入口

```bash
python scripts/check_model_connections.py
# 仅在确认本次真实调用总上限后运行；失败/重试计入，续跑不会清零。
python scripts/check_model_connections.py --run --max-calls <已确认上限>
```

检查先用 GPT 审查明确未运行测试的合成 Diff，再依次调用 Gemini Planner/Coder 和 GPT Reviewer。没有执行模型生成的代码，也不构成真实缺陷修复或完整图验收。私有 `.local/model-connection-checks/checks.json` 保存身份、阶段结果和预算；配置不匹配拒绝复用。终端仅输出受控状态、角色身份和计数。

## Docker

镜像安装 `.[oauth]`，不把 `.local/`、`.env` 复制进镜像。需要显式使用：

```bash
docker compose -f docker-compose.yml -f docker-compose.chatgpt.yml up --build -d
```

先在宿主完成授权；override 只给 Worker 绑定私有凭据目录，允许其原子保存轮换后的令牌。部署者必须确认容器 UID 对该目录有正确读写权限，不能为方便改成公开可读。未绑定凭据或权限不足时停止 GPT 调用，不自动退回 Gemini。先前取消的构建未执行；用户随后允许构建后，隔离 Mock 容器回归已完成，见[验收](mixed-model-verification-2026-10-23.md)。没有使用真实凭据做 Docker 内推理验收。

## 官方资料

- [Google Cloud Express SDK](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/start/express-mode/express-mode-api-quickstart)
- [OpenAI 会话与刷新](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)
- [OpenAI 模型与流式推理](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [OAuth 套餐接口限制](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
