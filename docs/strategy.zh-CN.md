# LIPAS 战略：成为第三大 Agent 系统

> 语言：[English](strategy.md) | [中文](strategy.zh-CN.md)
>
> 日期：2026-08-30

## 当前 0.63 能力完整整理版位置

0.63 整理版把仓库内的 local-first 契约收敛为可安装、可恢复且能力边界清晰的单工作区产品路径：install/upgrade
幂等，workspace/evidence bundle 带完整性校验，durability soak 有界且可度量，TLS context 可在不重绑
监听端口的情况下轮换，并明确提供托管 secret、真实 provider 和 design-partner 接口。通往 1.0 的剩余工作
是部署证据：真实 provider 运行、外部 KMS/HSM 托管、可创建 loopback socket 的 TLS 演练、长期 soak，以及
由 partner 独立签署的真实 workflow。

LIPAS 不应该同时成为缩小版 LangGraph 和 AutoGen。它应占据一个带有 local-first control
plane、conversation-first 入口的稳定位置：

```text
LangGraph  = 显式 graph 与 state orchestration
AutoGen    = 对话式多 Agent 协作
LIPAS      = 对话式、可信执行、恢复与证据化交付
```

这里的品类主张不是“prompt 更安全”。LIPAS 应让自然语言请求明确变成回答还是受治理的
Task，并让每项 Agent 动作都自然具备：唯一身份、诚实的 authority 边界、持久 intent、
terminal result 或可见不确定性、可恢复人工输入，以及用户接受交付前可以检查的证据。
Local-first 指 control、policy 和 evidence 由宿主控制；模型和经批准的执行步骤可以运行
在本地，也可以显式运行在远程 provider。

## 当前对比位置

| 能力面 | LIPAS 当前基础 | 主要缺口 |
| --- | --- | --- |
| 可信单 Agent 执行 | Effect、审批、replay、budget、取消、恢复、staged delivery 与有界 soak 已较强 | 长期外部生产证据 |
| Graph 编排 | 普通 Python 加 durable Run/Handoff 原语和受限 fan-out/fan-in | 条件图、子图、图可视化和 state migration |
| 多 Agent 协作 | ExecutionStore-backed policy、只 claim 一次的 durable Agent bridge、聚合 event handle、shared budget、capability delegation 与无依赖 LangGraph/AutoGen handoff boundary | 嵌套故障演练、更丰富的可视 projection 与 graph migration |
| 业务广度 | 18 个声明式 Scenario、17 个 Skill 与 connector 契约 | 使用外部账号的重复真实 workflow |
| 模型接入 | Ollama 和加固的 OpenAI-compatible endpoint | 更多 contract-tested 原生 adapter、embedding 与多模态边界 |
| 开发体验 | Python API、CLI、Doctor、Tour、trace、report、soak、provider workflow probe、extension scaffold/conformance SDK 与 LocalWebOperator projection | 可视时间线、debugger 与持续评测 |
| 生态 | MCP/action、有限框架 adapter，以及带签名的 extension registry/certification | package 分发、撤销运维、示例和社区采用 |
| 对话式产品 | 持久 CLI chat、Session、RunHandle 与 Local Web projection | 统一 chat-to-Task、流式审批、artifact 与 report 界面 |
| 部署 | local-first control plane、SQLite、受限 worker 并发、install/upgrade、backup/restore、TLS 轮换与显式远程模型 endpoint | 外部密钥托管、partner 证据、queue、tenancy、运维遥测与受控扩容 |

这些差距要选择性补齐。另一个框架拥有 graph DSL、角色社会、通用 memory 或云 control
plane，并不意味着它们都应该进入 LIPAS 核心。

下一步的产品动作应是 conversation-first integration。聊天界面是入口与导航层，不是第二套
authority：只需回答的 turn 使用 Session/RunHandle；需要行动的 turn 创建或关联 Task/Run；
高风险动作变成 Approval/Input Interrupt；完成结果则是 diff、verification、report 和显式
delivery。这是让新用户无需先学习 Python 就能理解 LIPAS 的最短路径。

## 0.50 的品类目标：Agentic Execution System

长期目标不是把某个框架嵌进另一个框架，而是在更高层统一四种能力：

- Codex/WorkBuddy 提供 agency：Agent 在真实 workspace 中感知、决策、行动、检查结果，
  持续工作直到交付目标；
- LangGraph 提供显式 orchestration：durable state、checkpoint、条件路径、人类 gate 与
  可恢复组合；
- AutoGen 与 Microsoft Agent Framework 提供 collaboration pattern：具名成员、workflow
  边界、有状态 handoff 与受限多 Agent 协作；
- LIPAS 提供 Runtime Semantics：Effect identity、capability、budget、approval、recovery、
  replay、audit、observation 与 delivery evidence。

统一结构是：

```text
┌─────────────────────────────────────────────────────────────┐
│ Execution：Agent / Harness / Tool / Worker                   │
│   感知 → 推理 → 提出 Effect → 行动 → 观察                    │
├─────────────────────────────────────────────────────────────┤
│ Orchestration：Plan / Handoff / Graph adapter / Team         │
│   已知路径确定执行；未知路径允许 Agent 自主行动              │
├─────────────────────────────────────────────────────────────┤
│ Runtime Semantics：Task / State / Effect / Resource / Policy │
│   准入、预留、执行、恢复、replay、审计、交付                  │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
                            World
                              │
                              ▼
                         Observation
```

Agent 不直接控制世界，而是提出 `EffectProposal`；Runtime 返回
`EffectDecision`；已有 Harness、connector 或 Worker 执行被准入的 effect；结果记录为
`EffectObservation`。这条边界让每个 Agent、Plan step 和 handoff 都有明确责任，避免
multi-agent 退化成“大家聊天但无人负责改变了什么”。

0.50 的目标因此是 **autonomous workflow**：确定的地方使用 deterministic workflow，路径
不确定的地方使用 agentic planning/action，但两者共享同一层 Runtime Semantics。graph state、
chat history、memory 和成员消息只是上下文或编排输入；只有 Task/Run/Effect/Artifact/Report
transition 才能建立系统对世界状态的可信声明。

### 长期 ownership 规则

| 问题 | 负责者 | 不能退化为 |
| --- | --- | --- |
| 想改变什么 | Agent / Plan / Handoff | 隐式 permission |
| 是否允许改变 | Runtime policy 与 Approval/Input | model prompt 习惯 |
| 如何改变 | Harness / Tool / Connector / Worker | 第二个 scheduler |
| 实际改变了什么 | Effect + Observation + Artifact | chat history |
| 下一步由谁负责 | Task/Run/Handoff identity | 未跟踪消息 |
| 如何证明完成 | Verification / Report / Delivery | LLM 的一句终止语 |

这就是 0.50 及之后的架构北极星。框架 adapter 只有在保持这些 ownership 规则时才有价值；
把它们的 graph DSL 或 conversation protocol 复制进 core，反而会让 LIPAS 退回多套 authority。

## 0.50 之后的长远方向

下一阶段应继续加深这三层，而不是再增加一种新的 Agent 对象：

| 远景 | 重点 | 成熟证据 |
| --- | --- | --- |
| 0.50 | 语义收敛 | deterministic 与 autonomous step 共享 Effect 准入、恢复、replay 和交付证据 |
| 0.51 | autonomous workflow compiler | Goal 加 constraints 可以生成混合 Plan；固定部分可检查，自适应部分有界（reference compiler 已实现） |
| 0.52 | execution fabric | local 与 remote Worker 使用同一 lease、fencing、checkpoint、取消和 uncertain-effect 契约 |
| 0.53 | world-state/evaluation | Artifact、observation、verification、成本与质量指标支持可 replay 的任务评测和回归 gate |
| 0.54 | extension distribution | Skill、Scenario、Tool、connector 可以被发现、升级、撤销和审计，且无需修改 core |
| 0.55+ | controlled scale | 在 local/hybrid 语义被证明后，再增加 shared workspace、tenancy、policy federation 与企业运维 |

真正的护城河不是内置 Agent 或 graph node 的数量，而是可以在保持可解释链条的前提下委托多少
真实工作：goal → proposal → admission → effect → observation → verification → delivery。
这个链条应成为未来所有 provider、framework 与 UI 的兼容目标。

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

0.50 Runtime bridge 现在已经形成一个真实纵切：`EffectProposal` 由 `AgentRuntime` 准入，
传给匹配 Harness，带着 proposal provenance 持久写入 Claim tape，再投影为
`EffectObservation`。这补上了 proposal-to-tape 路径，但并不代表完整 0.50 产品闸门已经
通过；remote transport、经过度量的 external vertical、operator 级 evaluation 与
design-partner 证据仍然开放。

版本审计明确按独立闸门记录：

- **0.41** — conversation link 拒绝歧义 Task/Run owner，projection cursor 可重连；
- **0.42** — local operator 提供认证 SSE、attachment 与 approval/input projection；
- **0.43** — remote worker 在 lease fencing 下持久化结构化 event、checkpoint 与 Effect observation；
- **0.44** — shared identity/delegation 可持久化、有 scope 且可撤销；
- **0.45** — connector 提供显式 descriptor、限流和 timeout-to-reconcile evidence；
- **0.46** — external graph 通过 Plan/Handoff 作为一个 fenced Run 托管；
- **0.47** — cost、incident、evaluation 从现有 store 投影；
- **0.48** — extension trust 支持 provenance、certification 与撤销/rollback；
- **0.49** — backup/restore 已做 integrity check，安装和 partner 验收仍开放；
- **0.50** — Runtime bridge 已持久化，完整 autonomous workspace 闸门仍开放。

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
| 0.41 | 在现有 Run authority 之上建立 conversation kernel 与 chat-to-Task 提升（当前 preview 已实现） |
| 0.42 | 本地 Web conversation product 与 provider-free 首次使用流程（cursor-streaming preview 已实现） |
| 0.43 | 受限 hybrid execution 与 remote Worker protocol |
| 0.44 | shared team workspace、身份、委托审批与审计导出 |
| 0.45 | 生产级 connector contract 与一个经过度量的外部纵切 |
| 0.46 | 统一 Plan/Handoff boundary 与 LangGraph/AutoGen interoperability |
| 0.47 | observability、evaluation、cost、incident 与 SLO surface |
| 0.48 | extension provenance、registry 形态与 conformance certification |
| 0.49 | release candidate 加固与 design-partner 验收 |
| 0.50 | 稳定的 Agentic Execution System 基线：conversation-first agency、deterministic/agentic orchestration 与统一 Runtime Semantics |
| 0.60 | 历史的 local-first 单工作区生产化基线 |
| 0.63 | 能力完整整理版：统一 Workbench 辅助逻辑、有界文档/代码/归档/Web/知识 Tool、架构导览与 provider-free 综合示例 |

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
