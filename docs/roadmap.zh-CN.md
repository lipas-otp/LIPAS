# LIPAS 产品路线图

> 语言：[English](roadmap.md) | [中文](roadmap.zh-CN.md)
>
> 状态：0.40.0 已发布：local operator、恢复演练与 extension conformance 加固
> 日期：2026-08-24

## 产品方向

LIPAS 是面向个人与小团队的本地可信任务 Agent。它负责从选择 workspace、提出任务，
一直到审批、中断恢复、验证和证据化交付的完整路径。实现分为两层：

- 可嵌入的 Python runtime，提供 Agent、工具、effect、replay、budget、
  supervision 和持久协调；
- 第一方本地任务工作台，让真实工作区任务具有审批、恢复和交付证据。

runtime 与范围受限的工作台已经作为 0.40.0 local operator beta 提供。两者共享一个
仓库、一条发布主线、一个 composition root、一个全局 workspace 数据库和一套执行模型；
per-Run evidence 仍是有意保留的隔离边界。

产品主次已经明确：本地任务工作台与后续产品界面是面向用户的独立产品，Python runtime
是内部可靠性基础和可选的高级嵌入能力。LangGraph、MCP server、OpenCrew/OpenClaw
adapter 只是实验性兼容样例，不是路线图承诺或核心产品界面。
更完整的竞争定位和投入顺序见 [LIPAS 战略](strategy.zh-CN.md)。

第一个产品目标不是支持最多的模型或 Agent 角色，而是让用户愿意交出一次真实写操作：
用户能够理解将要发生什么、控制高风险动作、中断后安全继续，并验证最终结果。

## 架构边界

```text
CLI / Local Web
       │
       ▼
业务层
  Scenario / Skill / Connector contract
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

## 场景扩展模型

业务广度在执行内核之外增长：

- Skill 增加只含 instruction 的知识，并且必须显式选择，因此目录增长不会膨胀每次
  prompt；
- Capability 增加受限的真实动作，并继续作为唯一 execution authority；
- `BusinessScenario` 组合最小 Skill bundle、生命周期与 capability requirement，
  但不创建第二套状态机；
- 场景需要审批、等待、恢复、reconciliation 或交付证据时，由 durable Run 组合这些契约。

0.40 目录包含 17 个 Skill 和 18 个 Scenario，覆盖文件、文档处理、Coding/review/release、
办公与个人写作、Email、Calendar、云盘和工单分流。Workspace Scenario 复用受限
Workbench Tool；Draft Scenario 不需要执行 authority。Connector Scenario 会发布结构化
Tool 与 host-policy 要求，但不会伪装成已内置 provider access。真实 external write 仍需
scope、preview approval、idempotency、provider evidence、数据出境 policy 和
uncertain-result reconciliation。

## 当前基础

历史 0.10–0.39 切片提供 Python Agent 与工具 API、持久 SQLite session、Effect、guard、
budget、安全 replay、supervision、外部操作 reconciliation、协调和第一套持久执行基础。
当前版本以下面的实时契约描述，不再用历史里程碑冒充当前状态。

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

执行状态 store 与 claim/Effect session 仍有意作为两份独立持久记录，当前 durable Agent
要求二者都使用 SQLite。两者的 crash window 由稳定 identity、transactional outbox、repair
以及显式 uncertain/orphan 状态覆盖；不声称它们构成分布式单事务。

0.20.0 产品 alpha 开启产品发布线，并在这个基础上加入自动 lease heartbeat、模型/工具阶段 timeout、独立读
工具的安全并行执行、第一套 Workspace/Approval/Artifact/Verification/Report 产品模型、
受限文件/Shell/Git capability、持久且有界的多 Task dispatcher，以及带漂移检查
apply/discard 交付的 staged ChangeSet，
以及 `lipas task` CLI。workbench command 默认使用失败关闭的 Bubblewrap 文件系统/网络
隔离；原始 secret 在持久化前被拒绝，allowlist 中的环境变量引用只在工具执行时解析。
产品生命周期事件已经持久化并可输出为 JSONL。端到端测试已经覆盖“创建任务 → 写入审批
→ 恢复 → 验证审批 → 恢复 → 报告”。面向 UI 的实时 streaming、timeout 后的更多自动
恢复策略和真实设计伙伴验证在 0.20 milestone 当时尚未完成；0.40 已提供 local operator、
显式 timeout reconciliation 与 validation protocol，外部伙伴验证仍是开放实验。

LIPA 架构复盘后的两条契约纵切现已落地：`LIPASRuntime` 成为 composition root；
普通、Session 与 durable 调用共享 `RunContext` 和 `AgentEvent`；durable event cursor
支持重连 catch-up；`InputPolicy` 与 approval 完成权限隔离；模型能力要求显式失败；
behaviour-neutral `RunObserver` 的 recommendation 默认只有建议性。schema v2 现在把兼容
全局状态统一进 `workspace.db`，提供显式 backup/verify/rollback 与持久审计诊断，并继续
隔离 per-Run evidence。新的多 Agent 工作现在使用 `AgentCoordinator`，把确定性 handoff
Task/Run 映射到 `ExecutionStore`；legacy Team mailbox ownership 仍是兼容迁移，不是新
工作需要采用的第二套权威。

0.32 的模型接入纵切增加一条第一方 OpenAI-compatible Chat Completions 边界，而不是为
每个 provider 建立独立子系统。显式 URL、model、API-key source、streaming mode 与
token-limit field 可以覆盖 OpenAI、火山引擎方舟、阿里百炼、腾讯混元、DeepSeek 和
private compatible gateway。adapter 会验证和 redact transport boundary，规范化
tool/usage/SSE/error，并让未经证明的模型 capability 保持 unknown。它不会改变
Workbench authority model，也不会把 provider availability 误当成 durability guarantee。

已完成的 0.35 场景纵切增加不可变 `BusinessScenario`、`CapabilityRequirement`、
`ScenarioAssessment` 与 `ScenarioRegistry`。CLI 与 Python 调用方可以查看、组合并校验
18 个配方，同时只加载已选择 Skill。无 Tool chat 与默认 Workbench 会在模型执行前拒绝
缺失 capability 或 effect class 不诚实的 Scenario。Connector assessment 会继续把账号
scope、secret、egress、approval、idempotency、provider evidence 与 reconciliation
作为显式 host obligation。

已完成的 0.38 存储纵切保持 SQLite-first 部署，不要求 PostgreSQL。统一内核让所有核心
Store 使用同一套 WAL、有界 busy timeout、事务和错误策略；durable convenience call
使用 Run-scoped evidence attachment，不再依赖 Runtime 全局锁。Claim tape 可以协调并发
connection，提供带索引的 cursor page，并从兼容的确定性 projection snapshot 恢复后只
replay delta。Snapshot 始终是可丢弃的派生状态；append-only evidence tape 与
ExecutionStore 权威边界不变。它面向本地和中等并发，不伪装成多机数据库。

历史 coordination 切片增加稳定 `HandoffEnvelope` identity、heartbeat/cancellation/
terminal replay、失败关闭的过期 lease policy，以及 sequential、RoundRobin、受限
parallel、map/reduce、durable Selector 与受限 Swarm。它复用 `ExecutionStore`；成员
registry 是 host 配置，不是另一套持久 scheduler。普通 Agent 成员获得 causal metadata
与 branch `RunContext`。SQLite-backed Agent 成员现在把已经 claim 的 coordination Run
直接传给 `Agent.run_durable()`，让 checkpoint、Approval/Input Interrupt、Effect recovery
和同一 envelope 的 resume/replay 共用一个 Run 与 lease，不会 double claim。

0.39 extension 切片现在还提供可重连的聚合 event handle、原子共享 budget reservation、
显式 capability delegation、无依赖的 LangGraph/AutoGen handoff boundary，以及
scaffold/conformance SDK。0.40 正式版本增加 token 保护的 `LocalWebOperator` projection、
确定性的故障演练 helper 和有界的本地 ExecutionStore transition benchmark；本轮加固再
加入有界 Task detail、取消/审批别名、不可变可复用 fault plan、隔离的 fault-matrix runner、
无依赖浏览器 projection 与多连接 contention probe。浏览器仍然是轮询可重连 event page 的薄视图，
不是第二套 scheduler 或 metrics authority。

## 0.40 加固与产品完整性

本发布线已经补齐之前的重要空白：

- [x] 第一方 `HttpClient`：HTTPS/egress policy、request identity、幂等 external write
  和通过 `OperationJournal` 的 uncertain reconciliation；
- [x] 第一方 `MCPClient`/`MCPHttpClient`，并保留已有 audited MCP server；
- [x] 幂等 `EmailConnector`，具备 provider reference、稳定 request identity 与 provider lookup；
- [x] 统一 operation reconciliation sweep，以及 Local Web 的 pending/uncertain operation、
  approval risk、preview/diff、budget、scope 和 verification projection；
- [x] canonical model request 与每次 LLM retry attempt 的 provider request identity，Effect
  result 保留 aggregate billed usage；
- [x] 异步 timeout orphan 的后台收敛，以及无法强杀的 sync tool 的显式
  `ToolHarness.reconcile_orphan()` closeout；
- [x] checkpoint payload migration hook 与显式 schema compatibility gate，未知未来版本仍失败关闭；
- [x] provider-free `doctor`/`tour --offline` onboarding、安装说明与 design-partner validation playbook（见[onboarding](onboarding.zh-CN.md)）。

这不表示任意 Python tool 都自动进入 sandbox，也不表示没有幂等/reconciliation 能力的 provider
可以提供 exactly-once delivery。

## 交付阶段

### 阶段一：可靠执行纵切

- [x] 在已经交付的 ReAct checkpoint 上增加 lease heartbeat 和阶段 timeout；
- [x] 并发执行彼此独立的 read，同时让 write 和涉及 policy/accounting 的调用保持串行且可恢复；
- [x] 让核心 SQLite Store 共享 WAL/timeout/transaction 策略，并显式分类 contention、
  read-only、disk-full 与 corruption；
- [x] 以稳定 Workbench ownership 和每 durable call 一条 Run-scoped evidence attachment
  移除 composition-root 全局锁；
- [x] 增加并发 Claim admission、有界 cursor page、索引化 catch-up 和不删除 evidence 的
  可重建 projection snapshot；
- [x] 持久化提交的 Task，以原子 lease、heartbeat、过期重领和审批释放槽位的方式并发调度多个 Run；
- [x] 把任务写入限制在每 Run staging workspace，并要求显式、带漂移检查的 ChangeSet apply 或 discard；
- [x] 增加高层模型与工具事件 streaming，并支持 durable catch-up；
- [x] 增加 Session、RunHandle 与跨阶段 cancellation/绝对 deadline 上下文；
- [x] 把缺失用户输入与 capability approval 分开；
- [x] 增加显式模型能力要求和诊断；
- [x] 为 Python、CLI 与 Task worker 增加加固的 OpenAI-compatible Chat Completions
  route，不做 provider/model fallback；
- [x] 引入 behaviour-neutral、只读的 RunObserver 边界；
- [x] 增加 ExecutionStore-backed handoff Run 与受限多 Agent coordination 标准库，
  不另建 scheduler database；
- [ ] 在有兼容迁移说明后淘汰 legacy Team mailbox authority；迁移前不对一个逻辑
  handoff 做双写；
- [x] 在 `LIPASRuntime` 背后物理整合兼容的控制、事件、产品和证据表，同时保持
  per-Run budget 隔离；
- [x] 在已经交付的持久 cancellation、审批 interrupt/resume 与 orphan 检测之上增加
  timeout recovery、uncertain reconciliation 与 sync-tool orphan closeout；
- [x] 增加 Task、Workspace、Run、Approval、Artifact、Verification 和 Report 应用模型；
- [x] 增加受限文件、Shell 和 Git capability；
- [x] 为第一方 command execution 增加失败关闭的 OS 隔离；
- [x] 在持久化前拒绝原始 secret，仅在工具执行时解析 allowlist 引用；
- [x] 持久化 task 生命周期事件，供产品以流式友好的形式消费；
- [x] 从 CLI 跑通“检查 → 修改 → 验证 → 报告”；

退出标准：同一个 CLI 工作区任务在长调用和进程终止后都能恢复，不会静默丢失状态或
重复已完成的 write；路径逃逸被拒绝；所有写入和命令均有审批与证据；报告明确说明
变更、验证和不确定性。

### 阶段二：CLI 与 Extension Alpha

- [x] 把显式目录扩展到 17 个 Skill 与 18 个文件、工程、办公、个人和 connector
  Scenario contract；
- [x] 定义可分发 Scenario/Connector package manifest、scaffold command 与离线 conformance，
  包含 provenance、connector scope、approval/reconciliation 声明和版本兼容 fixture；
- [x] 加强双向 LangGraph/AutoGen action 与 handoff adapter，但不把它们的 graph/team state model
  导入 LIPAS 核心；
- [x] 在 ExecutionStore 之上增加可选协调标准库，提供受限 Selector、RoundRobin、
  parallel map/reduce 与 Swarm policy；
- [x] 把已经 claim 的 handoff Run 接入 durable Agent checkpoint、Approval/Input Interrupt
  与 Effect recovery，避免 double claim；
- [x] 增加聚合 coordination event cursor、fan-in cause 导航，以及显式 shared budget/
  capability policy；
- [x] 增加确定性的故障演练和本地 transition benchmark helper，并支持多连接 contention；
  通过隔离的 named fault-matrix runner 覆盖 process-kill、SQLite busy/corruption、
  cancellation、redelivery 与 uncertain-member fixture；
- [x] 增加真实 LIPAS 任务所需的第一方 HTTP 与 MCP client capability；
- [x] 在数据出境 policy 与 uncertain operation reconciliation 对产品可见后，增加第一条
  经过审批且幂等的 email delivery connector；
- [x] 把已经交付的 CLI 审批 inbox 与单次消费状态做成聚焦的 diff/risk operator 体验；
- [x] 展示风险、budget、diff、命令、验证结果和 uncertain operation；
- [x] 通过 `doctor`、`tour --offline`、migration plan/verify 让用户无需维护者协助即可完成
  安装和 provider-free 第一个任务；
- [ ] 与 3–5 位外部设计伙伴持续完成重复出现的工作区任务；仓库已经提供 protocol 与
  measurement fixture，但不会伪造外部验证结果。

退出标准：设计伙伴能够完成真实任务，并根据报告说明改了什么、验证了什么、还有什么
不确定。

### 阶段三：0.40 Local Web beta

- [x] 增加无依赖的 `LocalWebOperator` HTTP projection，默认 loopback、脱敏 lease 状态和
  bearer-token mutation guard；
- [x] 增加基于该 projection 的有界任务详情，包括 Run event、Interrupt、artifact、
  ChangeSet diff、report 与 product event；
- [x] 通过有界、可重连的 event page 与 polling browser projection 展示执行状态、工具活动和等待审批；
- [x] 支持显式允许/拒绝别名与 Task/Run 取消。暂停和继续仍由 worker 在安全 checkpoint
  边界协作完成，不由 operator 直接篡改 lease；
- [x] 展示 diff、artifact、budget、验证结果、orphan 与 uncertain，不要求用户阅读原始日志。

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
