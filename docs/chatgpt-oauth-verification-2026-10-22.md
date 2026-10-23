# ChatGPT 本地授权预检验收 · 2026-10-22

状态：本机授权预检通过；尚未接入工作流、尚未验证真实生成。

## 来源与范围

- 分支：`codex/chatgpt-oauth-setup`，从 `d4353b9` 创建。
- 实现：`9dfe7a78ae8ec5e71de949e713bd31a3bbba9b3f`。
- 用户临时要求检查 GPT OAuth；原 CH-07B 分支及未跟踪的 pilot Python 3.8 环境配方保留。
- Gemini 原配置保持；只确认 `DEEPSEEK_API_KEY` 存在，未向 DeepSeek 发请求。
- 未改动历史真实评测、manifest、失败账本、工作流模型选择或原五卡状态。

## 实测

1. 用户在默认浏览器亲自完成 RepoPilot 的独立 OAuth 授权。
2. 本机验证 ID Token 签名、issuer、audience、有效期和 nonce 后保存凭据。
3. 实际 token 响应包含套餐使用权限，访问令牌当时有效。
4. 实际 `GET /v1/models` 成功，返回 7 个可见模型：`gpt-6.1-sol`、`gpt-6-astra`、
   `gpt-6-sol`、`gpt-6-luna`、`gpt-5.6-sol`、`gpt-5.6-terra`、`gpt-5.6-luna`。
   这是该账号在验收时的目录，不保证其他账号或未来仍有同样列表。
5. 本轮生成请求为 **0**。目录可读不等于真实生成可用，不报告修复率、Token 或金额改善。
6. 本机凭据目录 `0700`、全部凭据文件 `0600`；`.env` 和 `.local/` 中没有 Git 跟踪文件。

## 测试与来源核对

- 新增专项：**24 passed**，含实际 loopback HTTP、Host/state 拒绝、PKCE、签名/nonce/
  audience/issuer/有效期校验、账号不匹配时保留旧凭据、私有文件和锁、权限不足时拒绝目录查询。
- 主机 Python **3.13.5**：**374 passed / 42 skipped / 0 failed / 0 errors**，57.67 秒。
- 全量覆盖率 **85.45%**；授权模块 **80.51%**。42 项服务条件入口未运行，本轮没有新的容器验收结论。
- PyJWT **2.15.1**、cryptography **50.0.2**、httpx **0.28.1**、pytest **8.4.2**。
- `ruff check .` 和 `git diff --check` 通过。
- 显式 `--provider mock --model deterministic-mock-v1` 回归 **10/10**，非真实模型效果。
- 测试运行时文件与 `9dfe7a7` 的 6 个实现/契约文件字节一致，摘要见机器可读记录。
- 首次沙箱内专项为 23 passed / 1 failed：loopback 监听受沙箱限制，未取得回调准备状态；
  授权使用本机 loopback 权限后，24 项全部实跑通过。依赖首次下载也受网络沙箱限制，授权后安装成功。

原始 JUnit 与 coverage JSON 在忽略的 `reports/` 中，摘要和 SHA-256 保存在
[脱敏证据](evidence/2026-10-22-chatgpt-oauth-verification.json)。记录不包含身份、客户端 ID、主机 ID 或令牌。

## 尚未交付的能力

- 没有生产 Responses 适配或按角色选择不同 provider。
- 没有自动消费刷新令牌；访问令牌过期时需用同一目录重新登录。
- 没有启用套餐外 credits、改变模型预算或自动切换到付费 API。
- 接入真实评测前仍需确认模型、总 API 尝试数和可用预算，并冻结实验身份。

操作见 [授权说明](chatgpt-authorization.md)。

## 交付

代码已推送并创建 [Draft PR #11](https://github.com/xsw932187210-cell/RepoPilot/pull/11)，
base 为 `codex/ch-07a-project-adapters@d4353b9`，未合并。PR 的 `1307cea` 首轮 CI
[run 39808935536](https://github.com/xsw932187210-cell/RepoPilot/actions/runs/39808935536)
已成功；后续纯记录提交不改变上述已测试的实现字节。
