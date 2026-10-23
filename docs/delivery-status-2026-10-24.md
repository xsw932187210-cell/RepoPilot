# 2026-10-24 交付与接续位置

本轮范围是既有失败分析、CH-18、CH-19，三部分均已验收并合入 main。没有启动下一张卡，
本轮未创建正式 Tag/Release；`0.1.0` 的版本说明仍是草稿。

| 交付 | PR | main 合并提交 | 合并前检查 |
| --- | --- | --- | --- |
| 18 单元离线失败观察 | [#19](https://github.com/xsw932187210-cell/RepoPilot/pull/19) | `e745fd6ff76506d1f256b03387c2e76f3f98d93d` | 2/2 通过 |
| CH-18 锁安装与隔离 CI | [#20](https://github.com/xsw932187210-cell/RepoPilot/pull/20) | `2a23317742f5b7be640bbffe56f558ff7e425082` | 3/3 通过 |
| CH-19 演示与交付材料 | [#21](https://github.com/xsw932187210-cell/RepoPilot/pull/21) | `5659dda1793580daf17d699fb779f74e13e82b0d` | 3/3 通过 |

`5659dda` 包含以上全部历史。随后状态记录 PR 只同步文档与白名单证据，运行源码以这份
集成提交为参照；PR 的临时 CI merge commit、feature 提交与 main 合并提交分别保留。

## 可执行入口

```sh
make demo    # 自有夹具；断网只读容器；实际测试失败→返工→通过→模拟审批恢复
make doctor  # 安装与配置预检，不连接模型
make check   # 独立容器快测试和 Mock 回归
make verify  # 专属服务、完整故障矩阵、实际 Worker 重启与自动清理
```

从[演示说明](demo.md)开始，看[证据索引](evidence-index.md)核对声明，再按
[设计取舍](design-decisions.md)讲解。短演示是同进程内存 checkpoint；实际 Worker
跨进程重启由完整服务入口证明。真实 provider 配置路径本轮没有在线执行。

## 最终验证身份

- CH-18：Ubuntu 完整 **552 passed / 0 skipped、86.98%**；原本机快照的
  551 passed / 87.00% 单独保留，不能改成同一次运行。
- CH-19：Ubuntu 完整 **555 passed / 0 skipped、87.07%**，Mock 10/10、smoke、
  workflow/oneshot 实际 Worker 审批重启、安装包/API 源码一致、输入未变和专属清理通过。
- CH-19 快 CI 为 510 passed / 45 skipped、85.97%；这些 skip 由上述完整服务验证实跑。
- Docker Desktop 中离线演示完成：实际测试退出码 1→0，模拟审批后角色调用 5→5，
  默认 CLI 停在审批点，原夹具未变。三项演示回归通过。
- [CH-18 原记录](ch18-verification-2026-10-23.md)、[CH-19 记录](ch19-verification-2026-10-24.md)
  保留环境问题与修正过程；[远端机器汇总](evidence/2026-10-24-remote-delivery.json)
  含 checkout、feature、源码摘要及各项检查结果。

## 真实模型结果与边界

本轮新增真实模型请求 **0**。既有 3 缺陷 × 3 策略 × 2 次重复仍为每策略 **0/6**；
v2 71 次与初试 30 次合计 **101/108**，余 7 次未用。原报告 SHA-256
`8808dcdf24fbb993da262e374017245ddea5f4f17445c6cd893b3b906c1c9ed4` 保持一致，
冻结语料与上游夹具字节未变。

[离线观察](failure-analysis-2026-10-23.md)识别到可见门槛与独立验收不一致、调用控制停止、
收集失败和运行错误；现有聚合数据不足以还原具体代码根因。没有增加质量提升或泛化主张。
剩余历史预算不能自动移给新实验。

队列 ACK/自动重领、Worker 续租/fencing、恢复调度、多人权限和产物自动保留仍按
Openspec 的原计划处理；下一轮由用户指定目标，不自动扩展本轮范围。
