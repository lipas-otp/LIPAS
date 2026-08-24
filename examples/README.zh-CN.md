# LIPAS 示例：可靠 Agent 的小课程

> 语言：[English](README.md) | [中文](README.zh-CN.md)

从仓库根目录运行每一课：

```bash
python -m examples.01_first_agent
```

持久 state 会写入 `runs/`。目录会自动创建；想重新实验时可以安全删除。

不要把这些文件当作 framework API 目录来读。请按顺序阅读，并把最接近你场景的
文件复制到项目中。每一课都是一份完整、普通的 Python 脚本，并沿用同一稳定结构：

```text
本地数据或真实 client
        ↓
@tool(side_effect=...)      # assistant 实际可以执行什么
        ↓
build_agent(...)            # 模型、instructions、Skill、session、budget
        ↓
main()                      # 一项清晰的业务请求与可见结果
```

## 第一部分：构建有用的单 Agent

这四课需要本地 Ollama 模型。安装一次
`pip install -e '.[ollama]'`，然后运行 `ollama pull gemma4:12b`。

| 课次 | 运行 | 它回答的实际问题 | 适合复制的时机 |
| --- | --- | --- | --- |
| 01 | `python -m examples.01_first_agent` | “如何给一个 assistant 一项安全 capability？” | 刚开始写一个小 assistant。 |
| 02 | `python -m examples.02_research_brief` | “如何搜索来源并写一份谨慎的 brief？” | 筛选论文、政策或内部知识。 |
| 03 | `python -m examples.03_support_triage` | “如何处理客户问题而不改变数据？” | 同时使用账户查询和帮助中心信息。 |
| 04 | `python -m examples.04_daily_brief` | “如何将多个来源整理成每日决策 brief？” | 汇总指标、事故、报告或 dashboard。 |

这些脚本有意使用本地数据。先理解边界，再用自己的数据库、API 或搜索 client 替换
一个工具体；工具的副作用声明必须保持真实。

## 第二部分：每次增加一个可靠性边界

这些课程均可独立运行。第 05 课需要 `ollama` extra，因为它会构建 Ollama-backed
Agent；但它会在联系 Ollama daemon 或模型之前拒绝请求。第 06–15 课只使用 core
package，完全可以离线运行。

| 课次 | 运行 | 所讲边界 | 在何时加入 |
| --- | --- | --- | --- |
| 05 | `python -m examples.05_budget_limit` | 在模型调用前按 budget 拒绝 | 请求不得超过硬输出/支出限制。 |
| 06 | `python -m examples.06_strict_replay` | 已记录工具输出替代实时执行 | 必须安全地检查或复现之前一次运行。 |
| 07 | `python -m examples.07_supervision` | policy 记录终止/人工复核建议 | 需要审计明确风险或升级规则。 |
| 08 | `python -m examples.08_team_handoff` | 到单一 owner 的可持久、at-least-once handoff | 任务需要自己的重启或归属边界。 |
| 09 | `python -m examples.09_external_operation` | idempotency、不确定性与 reconciliation | provider write 可能在崩溃前已经发生。 |
| 10 | `python -m examples.10_research_review_team` | 双 owner 的证据与 review workflow | 一个 assistant 已不再适合承担全部归属。 |
| 11 | `python -m examples.11_durable_execution` | 带 checkpoint 的审批暂停与恢复 | 同一个 Agent run 必须在等待或中断后安全继续。 |
| 12 | `python -m examples.12_local_task_product` | 暂存变更、审批、重启恢复、验证与显式交付 | 需要完整理解第一方本地任务产品边界。 |
| 13 | `python -m examples.13_multi_agent_coordination` | 基于 ExecutionStore 的顺序与 map/reduce 归属 | 多个 owner 需要受限并发、replay 与唯一控制权威。 |
| 14 | `python -m examples.14_operator_beta` | 本地 operator/browser projection、隔离 fault matrix 与双连接 SQLite contention 测量 | 验证已发布 0.40 operator 与恢复边界。 |
| 15 | `python -m examples.15_external_connectors` | 幂等 Email delivery 与 transport-neutral MCP client 边界 | 增加真实 connector、但不创造第二套 authority。 |

## 可复用 Skill

`skills/` 目录有小型、可移植的 `SKILL.md` 模板。Skill 是可复用的指导，不是访问
控制机制：它会改变 Agent 的处理方式，但只有 `@tool` 才授予可执行 capability。

| Skill | 相关课程 | 它教授什么 |
| --- | --- | --- |
| `research-brief` | 第 02 课 | 分开来源事实、综合与不确定性。 |
| `support-triage` | 第 03 课 | 保护客户信息，并说明清晰的下一步。 |
| `daily-brief` | 第 04 课 | 排定运营风险与建议的优先级。 |
| `safe-external-actions` | 第 09 课的配套模板 | Agent 请求该操作时要求确认、idempotency 与 reconciliation。 |

复制一个目录到项目中，直接传入路径：

```python
agent = Agent.ollama(
    tools=[search_papers],
    skills="skills/research-brief",
)
```

## 检查一次运行

会持久化 state 的课程都会在文件中声明数据库路径。运行后，请检查精确的 claim 和
effect，不要猜测：

```bash
python -m lipas.cli trace runs/02-research-brief.db
python -m lipas.cli effects runs/02-research-brief.db
```

`orphan` effect 表示进程在记录 intent 后、LIPAS 观察到终止结果之前结束。应将它
视为中断的操作，而不是成功的答案。需要引导式多功能项目时，请使用
[循序上手 LIPAS](../docs/tutorial.zh-CN.md#11-引导式项目)中唯一维护的项目清单。
