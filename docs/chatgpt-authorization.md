# ChatGPT 本地授权预检

这是独立的操作工具：为 RepoPilot 注册本地 OAuth 客户端、校验用户授权，并读取账号可用模型目录。
它不调用生成接口，不改变 Planner/Coder/Reviewer 的模型，也不读取已有 Codex 登录凭据。
不属于 CH-07B 的数据集交付，不改变原五卡的验收状态。

## 使用

在仓库根目录、Python 3.12+ 虚拟环境中安装可选依赖：

```bash
python -m pip install -e '.[oauth]'
python -m repopilot.chatgpt_auth login
```

命令先启动 `127.0.0.1` 回调监听，再显示本机链接。用浏览器打开链接，点击
**Continue with ChatGPT**，选择自己的账号和工作区，并在官方页面授权套餐使用。
回到终端后，命令校验身份签名和实际授予的权限；只登录身份、未授予套餐使用权限时不会查询模型。

```bash
python -m repopilot.chatgpt_auth status
python -m repopilot.chatgpt_auth models
```

`status` 只返回连接、套餐授权和本地令牌有效期的布尔状态。
`models` 只发送 `GET https://api.openai.com/v1/models`，返回可见模型的名称和 ID。
它们均不输出账号身份、访问令牌、刷新令牌或授权回调内容。
模型目录可读不等于已完成生成验证。

每次登录默认等待 600 秒；可用 `--port 58455 --timeout 900` 指定本机端口和等待时限。
一次登录流程只能消费一次有效回调。超时或未完成时重新运行命令，不复用旧授权码。

## 本地存储与边界

- 默认存储为 `.local/chatgpt/`，目录权限 `0700`，文件 `0600`，原子替换并 fsync；`.local/` 已忽略。
- 一个存储目录对应一份账号/工作区注册。需要另一份连接时，使用不同的 `.local/` 子目录及 `--storage-dir`。
- 主机 ID 跨重启复用；已签发的客户端 ID 在重新登录和授权码交换失败后保留。
- PKCE、state、nonce 每次重新生成；校验回调 Host、路径、state 和注册身份。
- ID Token 使用 PyJWT/cryptography 验证 RS256 签名、issuer、audience、有效期、subject 和 nonce。
- 用文件锁串行化同一存储目录的操作；失败的身份验证不能替换已有凭据。
- 权限以 token 响应实际返回的 scope 为准；模型目录请求前校验套餐授权和剩余有效期。
- HTTP 回调禁用请求日志；异常只输出固定错误码或 HTTP 状态，不回显 provider 内容。

当前工具只实现连接预检，不自动刷新令牌、不调用 Responses、不提供生产工作流模型适配。
访问令牌过期后，使用同一存储目录重新登录。刷新令牌保存在同一私有文件内，尚不被工具消费。
可在 [ChatGPT 设置 → Usage](https://chatgpt.com/settings/usage) 管理额度和撤销该应用的授权。

## 后续接入评测的条件

工作流需增加单独的 OAuth Responses 适配，保持调用预留、失败分类、总预算和实验身份记录。
官方当前要求 `store=false`、`stream=true`；此路径不接受现有适配中的 `temperature`、
`max_output_tokens` 等参数。应独立记录模型、接入方式与预算语义，不能直接把 OAuth 令牌填入
现有 `OPENAI_API_KEY` 后视为兼容，也不能未经确认在配额不足时切换到付费模型。

## 官方依据

- [注册与登录](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [模型与请求](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [预览限制](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
- [账号与凭据](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)

验证记录见 [2026-10-22 授权预检验收](chatgpt-oauth-verification-2026-10-22.md)。
