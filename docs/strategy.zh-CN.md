# LIPAS 战略：成为第三大 Agent 系统

> 语言：[English](strategy.md) | [中文](strategy.zh-CN.md)
>
> 日期：2026-08-23

LIPAS 不应该同时成为缩小版 LangGraph 和 AutoGen。它应占据一个稳定的位置：

```text
LangGraph  = 显式 graph 与 state orchestration
AutoGen    = 对话式多 Agent 协作
LIPAS      = 可信执行、恢复与证据化交付
```

这里的品类主张不是“prompt 更安全”。LIPAS 应让每项 Agent 动作都自然具备：唯一身份、
诚实的 authority 边界、持久 intent、terminal result 或可见不确定性、可恢复人工输入，
以及用户接受交付前可以检查的证据。

## 当前对比位置

| 能力面 | LIPAS 当前基础 | 主要缺口 |
| --- | --- | --- |
| 可信单 Agent 执行 | Effect、审批、replay、budget、取消、恢复、staged delivery 已较强 | 更广泛的故障注入和长期生产证据 |
| Graph 编排 | 普通 Python 加 durable Run/Handoff 原语和受限 fan-out/fan-in | 条件图、子图、图可视化和 state migration |
| 多 Agent 协作 | ExecutionStore-backed policy、只 claim 一次的 durable Agent bridge、聚合 event handle、shared budget、capability delegation 与无依赖 LangGraph/AutoGen handoff boundary | 嵌套故障演练、更丰富的可视 projection 与 graph migration |
| 业务广度 | 18 个声明式 Scenario 与 17 个 Skill | 生产级 provider connector 与重复真实 workflow |
| 模型接入 | Ollama 和加固的 OpenAI-compatible endpoint | 更多 contract-tested 原生 adapter、embedding 与多模态边界 |
| 开发体验 | Python API、CLI、Doctor、Tour、trace、report、extension scaffold/conformance SDK 与 LocalWebOperator projection | 可视时间线、debugger 与持续评测 |
| 生态 | MCP/action 与有限框架 adapter | 稳定 package SDK、registry、认证、示例和社区分发 |
| 部署 | 本地进程、SQLite、受限 worker 并发 | remote worker、queue、tenancy、运维遥测与受控扩容 |

这些差距要选择性补齐。另一个框架拥有 graph DSL、角色社会、通用 memory 或云 control
plane，并不意味着它们都应该进入 LIPAS 核心。

## 五条投入主线

### 1. 把可信执行做成参考实现

- 完成唯一权威的 Task/Run/Handoff 生命周期，淘汰独立 mailbox ownership。
- 在反复进程故障下验证 timeout recovery、lease fencing、取消、orphan 与 external-write reconciliation。
- 发布 Tool side effect、adapter、checkpoint、connector 与 Scenario capability conformance tests。
- 为所有 durable schema 提供稳定 export/import 和显式 migration。
- 衡量恢复时间、重复写、不确定 operation、审批延迟和 verified completion，而不只统计测试数。

### 2. 把 Scenario 发展成扩展生态

一个可分发业务 package 应只包含实际需要的层：

```text
package manifest
├── Skills                 instruction 知识
├── Scenarios              生命周期与 capability contract
├── Tools / connectors     可选执行 authority
├── host policy            scope、secret、approval、egress
└── conformance tests      success、denial、crash、redelivery、reconciliation
```

下一步 SDK 应包含 manifest/version compatibility、无 prompt 注入的 discovery、scaffold
命令、离线校验、package provenance/signing 和 registry 格式。安装 package 绝不能自动启用
connector 或授予权限。

### 3. 增加可选 Orchestration 标准库

在 `ExecutionStore` 之上实现小型、behaviour-neutral 协调协议，再提供少数经过验证的
policy：顺序 handoff、RoundRobin、Selector、受限 parallel map/reduce 与 Swarm transfer。
每个 coordination branch 都是共享 context、event 与 cancellation 的 Run。SQLite-backed
Agent member 现在可以复用该 Run 承载 checkpoint、Approval/Input Interrupt 与 Effect
recovery；共享 budget 与 capability delegation 仍必须显式。协调层不能创建另一个 mailbox
数据库或隐藏 state calculus。

第一版标准库切片现已实现：稳定 envelope 映射到确定性 Task/Run，具备 lease、heartbeat、
持久 cancellation、terminal replay、受限 policy 与显式 redelivery safety。durable
Agent-member bridge 现在让已经 claim 的 handoff Run 承载 checkpoint、Approval/Input
Interrupt 与 Effect recovery，不产生第二次 claim。聚合 event handle、shared reservation、
capability delegation、无依赖框架 handoff adapter 和 scaffold/conformance SDK 已纳入 0.39。
`LocalWebOperator`、`FaultCampaign`、`run_fault_matrix()` 与本地 transition benchmark 构成已发布的
0.40 边界；本轮加固增加有界 Task detail（含产品证据）、过期 operator mutation 的显式 state conflict、
可复用且冻结的 fault plan 与共享 SQLite writer contention 测量。非 durable Agent member
仍受显式 redelivery gate 约束。

LangGraph/AutoGen adapter 应成为双向边界：外部宿主可以编排 LIPAS Action，LIPAS 也可以
把一个外部 graph/team 当成单项受限 capability。无需复制它们的完整 DSL。

### 4. 建设 Operator 与开发者产品

- Local Web：Task、事件时间线、Tool 活动、审批、Input、diff、artifact、budget、verification、orphan 与 connector reconciliation。
- Scenario wizard：选择配方、模型端点、workspace、connector scope 与 policy；启动前展示缺失能力。
- Debugger：可重连事件流、确定性 replay、checkpoint 检查、cause/effect 导航与脱敏导出。
- Fast path：lazy import、受限 prompt 组合、索引化 event catch-up、启动 benchmark、并行 read Tool，避免无意义 store 打开。
- Onboarding：每个核心纵切都有 provider-free Tour；Doctor 能区分配置、加载、生成、sandbox 与 connector 故障。

### 5. 用真实纵切证明价值

优先完成三个可重复纵切，而不是几十个浅层 demo：

1. repository maintenance 与 release readiness；
2. 本地 workspace 内的 document/report/meeting workflow；
3. 一个范围明确的外部流程，优先 email draft → approval → delivery → reconciliation。

只有 connector contract 和 UI 能展示 scope、数据出境、审批、provider evidence 与 uncertain
result 后，再依次接 Calendar、云盘和工单 provider。每个纵切都需要设计伙伴、任务 fixture、
质量标准、失败案例与可测量的人工接受率。

## 建议发布顺序

| 版本 | 主要结果 |
| --- | --- |
| 0.35 | 公共 Scenario contract、广泛 instruction 目录、capability check |
| 0.38 | SQLite 并发内核、并发 durable Run、分页与 snapshot evidence |
| 0.39 | package/scaffold/conformance SDK、durable Agent-member bridge、更强框架 adapter |
| 0.40 | Local Web operator、浏览器 projection、named fault matrix、性能与 extension conformance 加固（已发布） |

版本号只是顺序建议，不能成为削弱契约的理由。生产 connector 宁可延期，也不能静默
retry uncertain write 或隐藏缺失 scope。0.36–0.38 的可靠性工作被有意合并到 0.38，
先完成存储与并发内核，再增加新的 operator surface。

## 架构护栏

- 只有一个 Task/Run authority；view 和 adapter 不建立平行真相。
- Durability 改变保存语义，不改变 Agent 语义。
- Skill 与 Memory 绝不授予 authority，也不能证明 Effect。
- Input 提供事实；Approval 授权一项具体动作。
- Recommendation 默认只读，直到 behaviour 或 host 接受。
- 未知模型或 connector capability 保持 unknown。
- External write 需要稳定 identity、preview、approval、provider evidence 与 reconciliation。
- Scenario 组合核心契约；核心绝不导入业务 policy。

## 成功信号

最重要的指标是真实任务的重复使用、证据化结果被用户接受、中断恢复成功、零无记录重复写、
用户能够理解审批、connector reconciliation 时间，以及第三方 package 无需修改核心即可
通过 conformance。Agent 数、graph node 数、model 数和 GitHub star 只是辅助信号，不是
成熟度本身。
