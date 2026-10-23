# 可复现安装、分层验证与环境排查

CH-18 固定的是 Python 依赖选择和基础镜像身份。测试是否执行、是否需要服务、是否消耗
模型额度分别记录；安装成功不代表真实修复有效，也不保证远端模型或系统包位级复现。

## 支持范围与依赖身份

- 项目要求 CPython 3.12 或 3.13；Docker 默认 3.12。Linux Docker 使用 amd64/arm64
  对应的固定基础镜像，实际验收的平台以[验证记录](ch18-verification-2026-10-23.md)为准。
- `uv.lock` 是跨平台解析结果，包含精确版本、环境标记和下载哈希；日常安装用
  `uv sync --locked`。`--locked` 在声明与锁不一致时拒绝运行，不会自动更新锁。
- `requirements/uv.txt` 用哈希固定 uv 0.9.6，构建后端 hatchling 1.27.0 和 editable
  安装所需的 editables 0.5 也在锁内。
  先安装锁内 build group，再以 `--no-build-isolation` 构建项目，避免构建过程另外解析依赖。
- app、test 和 sandbox 均消费同一份锁；sandbox 只安装 pytest/ruff 的独立依赖组。
- [镜像清单](../delivery/images.json)记录 Python、PostgreSQL、Redis 的 digest；实际运行
  还记录 app/test/sandbox 镜像 ID。首次构建仍会下载系统包与缺少的 Python 包。
- 本次锁定保留了既有临时环境中已安装包的版本，没有把依赖升级混入评测结论。

uv 的行为参考[锁与同步](https://docs.astral.sh/uv/concepts/projects/sync/)和
[Docker 安装](https://docs.astral.sh/uv/guides/integration/docker/)官方文档。

## Docker 路径：不需要本机安装项目 SDK

需要 Docker Desktop/Engine、Compose v2、Make、Git 和 Python 3（只用于宿主辅助脚本）。
命令在仓库根目录执行；构建时需要网络，运行 Mock 不需要模型凭据。

```sh
make check    # 构建固定测试镜像，lint、快测试、Mock 10 场景；运行容器断网
make doctor   # 在同一镜像里检查安装和配置，不连接 provider
make verify   # 独立 PostgreSQL/Redis/API/Worker/Docker 全量验证，结束自动清理
```

`make check` 的服务条件用例会明确 skip；这些 skip 不能当作服务验收。
`make verify` 复用既有故障矩阵，并开启“出现 skip 则整体失败”的规则；完整结果应为零跳过。
它还运行 API smoke 和 workflow/oneshot 的真实 Worker 审批重启检查。
自有 tinyledger 的合成适配回归在临时 corpus 中绑定本轮实际 sandbox 镜像、Python、
recipe 和 lock 的新身份；其可见/独立测试及写权限保持不变，不依赖另一台电脑的历史
Python 3.8 镜像缓存。原冻结元数据和真实模型评测仍使用各自原身份，不由此入口改写。

verify 使用随机专属项目名 `repopilot_ch18_*`、自动选择的本机端口、项目私有数据卷与网络，
拒绝复用已有项目资源。Compose 显式 `--env-file /dev/null`，固定 Mock、关闭 GitHub 写入，
不挂载 `.env`、OAuth 目录或已有任务工作区。默认 Dockerfile 会重新安装当前源码，不接受
旧缓存依赖镜像替代锁文件安装。构建缓存可以复用，但不替代源码与安装包摘要核对。

原生 Linux 使用 `/var/run/docker.sock` 的实际数字组，macOS Docker Desktop 使用 VM
内的组 0；集成脚本将其传给 Worker/probe 的附加组，保持容器非 root 用户。特殊 Docker
主机可显式设置 `CH06A_DOCKER_SOCKET_GID`（只接受数字）；不能用修改 socket 为全员可写
的方式绕过连接失败。完整验证先在 probe 容器中检查 Docker 连接，再开始故障矩阵。

输出位于忽略目录 `reports/repopilot_ch18_*/`。其中 `delivery-summary.json` 只含计数、
版本、源码/锁摘要和验证状态；完整服务 CI 只上传这份白名单汇总，不上传整个 reports。
`cleanup.json` 确认专属容器、网络和卷已清理。镜像作为构建缓存保留；不运行全局 prune。
如果进程被强制终止，须先核对项目标签再处理残留，不能推断清理已经成功。

## 本机开发路径：SDK 安装在虚拟环境

已有 CPython 3.12；3.13 使用者将下面的 `python3.12` 替换为 `python3.13`，并在 sync
时加 `--python python3.13`。工具和项目依赖分别保存在忽略的 `.tools/`、`.venv/`。

```sh
python3.12 -m venv .tools
.tools/bin/python -m pip install --require-hashes -r requirements/uv.txt
.tools/bin/uv sync --locked --extra dev --group build --no-install-project
.tools/bin/uv sync --locked --extra dev --group build --no-build-isolation
PATH="$PWD/.tools/bin:$PWD/.venv/bin:$PATH" repopilot-doctor
.venv/bin/ruff check .
.venv/bin/python -m repopilot.evaluation --provider mock --fail-on-regression
```

Python 库只进入项目虚拟环境，不修改系统 Python。Docker 路径不需要上述本机安装。
普通快测试可运行 `.venv/bin/pytest --cov=repopilot --cov-fail-under=70 -q`，但宿主测试
不能替代容器、数据库和 Worker 恢复的验证。

## doctor 的含义

```sh
repopilot-doctor                   # Python、锁新鲜度、安装版本、配置、内置夹具
repopilot-doctor --profile docker  # 再检查 Docker/Compose 与基础镜像缓存
repopilot-doctor --profile corpus  # 再检查冻结元数据与三个上游缓存是否存在
```

这三个命令只读取状态，不 pull/build/start，不登录，也不调用模型。输出只显示凭据字段
是否存在，校验错误不打印原始配置值。缺少必要资源返回非零；“缓存存在”不代表缺陷复现
通过，“凭据存在”不代表凭据有效。默认 offline profile 不证明 Docker/provider 可用。
在 Docker 镜像里用默认 profile；宿主 Docker profile 需要本机有 Docker CLI。

| 失败项 | 处理步骤 |
| --- | --- |
| python | 使用 3.12/3.13，或选择 Docker 路径；3.14 暂不在支持范围 |
| lock_current | 检查 pyproject/uv.lock 是否来自同一提交；不要在验收时自动重解依赖 |
| dependencies | 按锁同步新的虚拟环境，避免混用旧环境或全局安装 |
| configuration | 本地检查 `.env` 的字段类型；不要把整个文件或异常原文发到公开渠道 |
| docker_daemon | 启动 Docker，核对 `docker context show`；不要切换上下文后复用旧验收身份 |
| image_* | 首次构建/拉取固定清单所需镜像，再检查；命令不会自动下载 |
| cache_* | 按真实语料协议准备对应上游缓存；Mock 演示无需这些缓存 |

## CI 与更新流程

每个 PR 的 `ci` 跑锁安装、doctor、lint、快测试、70% 行覆盖率和 Mock 10/10。
`isolated-service-verification` 在依赖、镜像、迁移、隔离入口或工作流文件变化时自动执行；
其他时候可通过 GitHub Actions 的 workflow_dispatch 手动触发。单次上限 30 分钟，使用
runner 上的独立 Compose 资源和现有 sandbox 资源限制，不连接真实 provider。

更新依赖时使用固定工具执行 `uv lock --upgrade-package NAME`，审查 pyproject/uv.lock 的
差异，并重跑相应快测试与完整服务验证。更新镜像时同步镜像清单、Dockerfile、Compose
中对应 digest，并保留旧/新实际 ID。冻结评测夹具的原始字节、hash 和旧报告不随依赖更新
重写；涉及真实实验实现身份时必须创建新的实验目录并事先确认预算。

应用表本次没有迁移；部署升级仍须遵循[数据库升级说明](database-migrations.md)，
先备份并停止旧 Worker，不能把验证用的私有卷清理命令用于真实任务数据。
