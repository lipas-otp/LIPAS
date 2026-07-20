# LIPAS 产品路线图

> 语言：[English](roadmap.md) | [中文](roadmap.zh-CN.md)
>
> 状态：Draft 0.3
> 日期：2026-07-20

## 产品方向

LIPAS 是面向个人与小团队的本地可信任务 Agent。它负责从选择 workspace、提出任务，
一直到审批、中断恢复、验证和证据化交付的完整路径。实现分为两层：

- 可嵌入的 Python runtime，提供 Agent、工具、effect、replay、budget、
  supervision 和持久协调；
- 第一方本地任务工作台，让真实工作区任务具有审批、恢复和交付证据。

runtime 当前已经可用。任务工作台已作为范围受限的 0.20.0 产品 alpha 提供，并继续
积极开发。两者共享一个仓库、一条路线、一个发布主线和一套执行模型。

产品主次已经明确：本地任务工作台与后续产品界面是面向用户的独立产品，Python runtime
是内部可靠性基础和可选的高级嵌入能力。LangGraph、MCP server、OpenCrew/OpenClaw
adapter 只是实验性兼容样例，不是路线图承诺或核心产品界面。

第一个产品目标不是支持最多的模型或 Agent 角色，而是让用户愿意交出一次真实写操作：
用户能够理解将要发生什么、控制高风险动作、中断后安全继续，并验证最终结果。

## 架构边界

```text
CLI / Local Web
       │
       ▼
任务工作台
  Task / Workspace / Approval / Artifact / Report / 产品 policy
       │
       ▼
Python runtime
  Agent / Tool / Effect / Checkpoint / Guard / Budget / Replay / Operation
       │
       ▼
Filesystem / Shell / Git / HTTP / MCP / Model provider
```

这是一条内部架构边界，不是项目边界。工作台可以依赖 runtime；runtime 不得依赖工作台
概念。工具与模型的执行证据仍由 runtime Effect 记录承载；workbench 只增加 task 创建、
审批、artifact、verification 与报告交付等产品生命周期事件，不复制 Effect tape。

只有同时满足以下条件，能力才下沉到 runtime：真实工作台流程已经需要它；实现不包含
Task、Workspace、UI 或产品 policy 概念；可以独立测试。这样既保持 runtime 可复用，
也避免它脱离用户需要扩张成抽象工程。

## 首批用户与纵切场景

先服务专业个人以及小型技术、运维和数据团队。第一个纵切场景是本地工作区任务：

```text
选择工作区并提出任务
          ↓
检查环境并提出计划
          ↓
识别 read / local write / external write 风险
          ↓
需要时请求审批
          ↓
持久执行；支持取消、暂停和恢复
          ↓
交付变更、验证、成本与未解决风险
```

代表性任务包括修复明确缺陷、更新配置或文档、检查发布风险、处理本地数据，以及调用
经过审批的 HTTP 或 MCP 操作。

## 当前基础

0.10.0 public beta 提供 Python Agent 与工具 API、持久 SQLite session、Effect、
guard、budget、安全 replay、supervision、外部操作 reconciliation、
at-least-once Team handoff，以及第一套持久执行基础。

源码中已经加入第一条完整的持久 ReAct 纵切：`ExecutionStore` 持久化 Task、Run、
带版本的 Checkpoint 和 Interrupt；run lease 可以 fencing 过期 worker；保存
checkpoint 并进入等待是一个原子操作；审批结果可持久化且只能一致地消费一次。
`Agent.run_durable()` 已把 ReAct 的 reason/act/observe 阶段接入这个 store，逐步保存
模型 reply、每个已完成的工具结果、对话状态和 terminal result。恢复时使用 run 范围内
的稳定 effect identity：已完成的模型或工具调用从 Effect tape 恢复，不会再次提交；
只有 intent 而没有 terminal outcome 时则明确暴露 orphan，不会盲目重试。
协作式 cancellation 也会写入 checkpoint；带取消请求且已经过期的 lease 可以被重新
claim 并完成结算，不会再发起 external call。
supervisor tick 使用 run/iteration 范围内的稳定 claim identity，能够幂等修复
recommendation 已写入、checkpoint 尚未保存时的崩溃窗口。
ExecutionStore、OperationJournal 与 Team mailbox 都有显式 schema compatibility
gate。它们会把权威 transition 与 Claim-shaped 本地 outbox 原子提交，并能在崩溃后
镜像至已连接的 Claim tape。真实
subprocess `SIGKILL` 测试验证：完成 write Effect 后、下一个 checkpoint 前中止时，
恢复会复用该 Effect，而不是再次执行 write。

0.10.0 发布版尚未包含面向调用者的 token streaming、自动 lease heartbeat 或完整任务
工作台。执行状态 store 与 claim/Effect session 有意作为两份独立持久记录；当前
durable Agent 要求二者都使用 SQLite。

0.20.0 产品 alpha 开启产品发布线，并在这个基础上加入自动 lease heartbeat、模型/工具阶段 timeout、独立读
工具的安全并行执行、第一套 Workspace/Approval/Artifact/Verification/Report 产品模型、
受限文件/Shell/Git capability、持久且有界的多 Task dispatcher，以及带漂移检查
apply/discard 交付的 staged ChangeSet，
以及 `lipas task` CLI。workbench command 默认使用失败关闭的 Bubblewrap 文件系统/网络
隔离；原始 secret 在持久化前被拒绝，allowlist 中的环境变量引用只在工具执行时解析。
产品生命周期事件已经持久化并可输出为 JSONL。端到端测试已经覆盖“创建任务 → 写入审批
→ 恢复 → 验证审批 → 恢复 → 报告”。面向 UI 的实时 streaming、timeout 后的更多自动
恢复策略和真实设计伙伴验证仍未完成。

## 交付阶段

### 阶段一：可靠执行纵切

- [x] 在已经交付的 ReAct checkpoint 上增加 lease heartbeat 和阶段 timeout；
- [x] 并发执行彼此独立的 read，同时让 write 和涉及 policy/accounting 的调用保持串行且可恢复；
- [x] 持久化提交的 Task，以原子 lease、heartbeat、过期重领和审批释放槽位的方式并发调度多个 Run；
- [x] 把任务写入限制在每 Run staging workspace，并要求显式、带漂移检查的 ChangeSet apply 或 discard；
- [ ] 增加高层模型与工具 streaming；
- [ ] 在已经交付的持久 cancellation、审批 interrupt/resume 与 orphan 检测之上增加
  timeout recovery；
- [x] 增加 Task、Workspace、Run、Approval、Artifact、Verification 和 Report 应用模型；
- [x] 增加受限文件、Shell 和 Git capability；
- [x] 为第一方 command execution 增加失败关闭的 OS 隔离；
- [x] 在持久化前拒绝原始 secret，仅在工具执行时解析 allowlist 引用；
- [x] 持久化 task 生命周期事件，供产品以流式友好的形式消费；
- [x] 从 CLI 跑通“检查 → 修改 → 验证 → 报告”；

退出标准：同一个 CLI 工作区任务在长调用和进程终止后都能恢复，不会静默丢失状态或
重复已完成的 write；路径逃逸被拒绝；所有写入和命令均有审批与证据；报告明确说明
变更、验证和不确定性。

### 阶段二：CLI Private Alpha

- 增加真实 LIPAS 任务所需的第一方 HTTP 与 MCP client capability；
- 把已经交付的 CLI 审批 inbox 与单次消费状态做成聚焦的 diff/risk operator 体验；
- 展示风险、budget、diff、命令、验证结果和 uncertain operation；
- 让用户无需维护者协助即可完成安装和第一个真实任务；
- 与 3–5 位设计伙伴持续完成重复出现的工作区任务。

退出标准：设计伙伴能够完成真实任务，并根据报告说明改了什么、验证了什么、还有什么
不确定。

### 阶段三：Local Web

- 增加任务列表和任务详情；
- 实时展示执行状态、工具活动和等待审批；
- 支持允许、拒绝、取消、暂停和继续；
- 展示 diff、artifact、budget、验证结果和 orphan，不要求用户阅读原始日志。

退出标准：用户可以直接从产品界面判断任务是否安全、是否真正完成。

### 阶段四：先验证，再扩功能

- 支持至少 10 位真实设计伙伴；
- 分析失败任务和人工接管原因；
- 修复反复出现的恢复、审批、工具和 onboarding 问题；
- 选择重复率最高的狭窄场景作为下一版本重点。

## 安全默认值

- 工作区之外的文件访问默认拒绝；
- secret 值不得进入 prompt、trace 或报告；
- 记录实际命令、退出状态和结构化 Shell 风险类别；
- 删除、发布、推送、发送消息和外部写默认需要审批；
- external write 使用稳定 idempotency key；
- 不确定的外部结果进入 `uncertain`，不得盲目重试；
- “完成”必须附带验证证据，或者明确说明尚未验证；
- replay 默认不得重新执行实时 write。

## 首版明确不做

- 通用生活助理；
- 多聊天渠道 gateway；
- Agent graph 编辑器或角色社会；
- 自动生成和自我改进 Skill；
- 长期用户画像和通用 memory；
- SaaS control plane、SSO、SCIM、复杂 RBAC 和计费；
- 自动发布、自动推送或不受限的系统访问。

## 真正重要的指标

产品信号是真实任务的重复执行、同类任务的重复使用，以及用户是否愿意为某个具体高频
流程付费。可靠性信号是所有工具和模型调用都有 terminal result 或可见 orphan、审批
能够持久恢复、强制中断恢复稳定、不出现无记录的写重试，并且每次“完成”都有证据。
模型数量、Agent 数量和 GitHub stars 不是当前阶段的主要里程碑。
