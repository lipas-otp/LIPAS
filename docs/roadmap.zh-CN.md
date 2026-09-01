# LIPAS 产品路线图

> 语言：[English](roadmap.md) | [中文](roadmap.zh-CN.md)
>
> 状态：0.63.0 local-first 能力完整整理版已发布。0.41 至 0.51 与 0.60 生产化基线均已实现并
> 测试各自的 reference 契约。0.63 让能力边界更容易组合和检查，同时保持同一套 authority
> 模型。真实 provider/密钥托管、loopback TLS 和 partner 证据仍是通往 1.0 的部署闸门。
> 日期：2026-09-01

## 产品方向

LIPAS 是带有 local-first control plane 的可信 Agent 执行与交付平台。它负责从对话或选择
workspace、提出任务，一直到审批、中断恢复、验证和证据化交付的完整路径。这里的
local-first 指 workspace 数据、authority、policy 和 evidence 默认留在宿主控制的环境中，
并不要求模型或每个执行 provider 都运行在本地。实现分为三层：

- 可嵌入的 Python runtime，提供 Agent、工具、effect、replay、budget、
  supervision 和持久协调；
- 第一方本地任务工作台，让真实工作区任务具有审批、恢复和交付证据。
- 明确的执行边界，容纳本地 sandbox、远程兼容模型 endpoint，以及未来受限的 worker pool。

runtime 与范围受限的工作台已经作为 0.63.0 local-first 能力完整整理版提供。两者共享一个
仓库、一条发布主线、一个 composition root、一个全局 workspace 数据库和一套执行模型；
per-Run evidence 仍是有意保留的隔离边界。显式配置的远程兼容模型 endpoint 与
provider-neutral HTTPS worker reference transport 已可使用；共享 tenancy 和多机器
control plane 属于未来执行层，不是 0.40 的隐藏承诺。

产品主次已经明确：对话与本地任务工作台是面向用户的产品入口，Python runtime
是它们的可靠性基础和可选的嵌入能力。第一方产品应 conversation-first，但仍以
Task/Run/Approval/Effect 作为 durable control-plane 词汇。LangGraph、MCP server、OpenCrew/OpenClaw
adapter 只是实验性兼容样例，不是路线图承诺或核心产品界面。
更完整的竞争定位和投入顺序见 [LIPAS 战略](strategy.zh-CN.md)。

第一个产品目标不是支持最多的模型或 Agent 角色，而是让用户从自然语言请求开始，明确它
只是回答还是需要行动，控制高风险动作，中断后安全继续，并验证最终结果。

## Conversation-first 运行模型

对话是入口，但不是第二套执行 authority：

```text
chat message
    ├── 只需回答 ───────────── Session / RunHandle
    ├── 需要行动 ───────────── Task / durable Run
    ├── 高风险操作 ─────────── Approval 或 Input Interrupt
    └── 完成工作 ───────────── diff / verification / report / delivery
```

CLI chat、本地 Web chat、Python 嵌入，以及未来桌面或 hosted chat surface，必须共享同一套
event cursor、cancellation、capability policy 和 Effect evidence。聊天宿主可以建议创建
Task，但不能静默授予 Tool、绕过审批，或创建第二套 message authority。

## 架构边界

```text
CLI / Conversation UI / Local Web / Python host
       │
       ▼
Local-first control plane
  Conversation / Task / Run / Approval / Event / Evidence
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
执行层
  Local sandbox / 显式模型 endpoint / 未来受限 worker
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

0.40 引入的目录包含 17 个 Skill 和 18 个 Scenario，覆盖文件、文档处理、Coding/review/release、
办公与个人写作、Email、Calendar、云盘和工单分流。Workspace Scenario 复用受限
Workbench Tool；Draft Scenario 不需要执行 authority。Connector Scenario 会发布结构化
Tool 与 host-policy 要求，但不会伪装成已内置 provider access。真实 external write 仍需
scope、preview approval、idempotency、provider evidence、数据出境 policy 和
uncertain-result reconciliation。

## 当前基础

历史 0.10–0.39 切片提供 Python Agent 与工具 API、持久 SQLite session、Effect、guard、
budget、安全 replay、supervision、外部操作 reconciliation、协调和第一套持久执行基础。
当前版本以下面的实时契约描述，不再用历史里程碑冒充当前状态。
0.60 生产化新增显式 installation manifest、幂等 upgrade、权限加固、
release-readiness 检查、本地 secret 文件轮换、TLS-gated operator/worker server，
以及把 bounded workflow 作为一个持久 Task/Run 执行的 Runtime API。这些能力使
local-first 单工作区可以安装和审计，但不会伪造外部 partner 证据或 hosted tenancy。

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
scaffold/conformance SDK。历史 0.40 正式版本增加 token 保护的 `LocalWebOperator` projection、
确定性的故障演练 helper 和有界的本地 ExecutionStore transition benchmark；本轮加固再
加入有界 Task detail、取消/审批别名、不可变可复用 fault plan、隔离的 fault-matrix runner、
无依赖浏览器 projection 与多连接 contention probe。浏览器优先使用 SSE catch-up，必要时回退
到 polling；它仍是 event page 的薄视图，不是第二套 scheduler 或 metrics authority。

当前 integrity pass 还把 approval/replay identity 绑定到请求 payload 与 causation，明确区分
provider request identity 和 operation idempotency identity，把 external write 的 redirect 视为
uncertain，拒绝空的 adapter identity，并且不会把空 SLO 窗口报告为 healthy。这些是加固保证；
TLS/key custody、provider account 与外部 partner 仍需部署证据。0.49 与 0.50 的 reference
闸门已经实现；通往 1.0 的剩余工作统一记录在 0.63 部署证据闸门中。

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

- 0.49 的初始 3–5 位伙伴证据稳定后，再扩展到至少 10 位真实设计伙伴；
- 分析失败任务和人工接管原因；
- 修复反复出现的恢复、审批、工具和 onboarding 问题；
- 选择重复率最高的狭窄场景作为下一版本重点。

## 0.41 Conversation kernel

目标：在现有 SQLite authority 上持久化 Conversation、Message 和 chat-to-Task link。
Message identity 对调用方稳定且幂等，每条消息只有一个 event cursor 和一个 Task/Run。

已实现并独立检查：

- [x] additive schema migration 与 future-version fail-closed；
- [x] 幂等 append、跨 conversation ownership 检查、确定性 message → Task → Run 提升；
- [x] 消息、AgentEvent、工具活动、approval/input card 的统一 cursor projection；
- [x] Python、CLI 与 HTTP 共享同一 message/event 契约。

测试闸门：`tests/test_v041_conversation.py` 及 execution/storage 套件通过。退出标准是
重试消息仍检查或恢复同一个 Run，不产生重复执行。更完整的 hosted identity layer 不属于本版。

## 0.42 Local Web conversation operator

目标：让新用户在本地浏览器完成 inspect → plan → approve → verify → deliver，且不改变
0.41 的 authority。Operator 只是有界 projection，不创建新 queue 或数据库。

已实现并独立检查：

- [x] conversation list/timeline/composer、task promotion 与有界 detail；
- [x] 带 cursor 重连和 polling fallback 的认证 SSE catch-up；
- [x] approval/input card、tool activity、diff、report 与安全 content-addressed attachment；
- [x] 浏览器与 Python client 使用相同 event/mutation 契约。

测试闸门：`tests/test_v041_conversation.py`、`tests/test_v040_beta.py` 与 operator route 套件通过。
退出标准是用户无需读原始日志或写 Python 就能判断安全性和完成度。Hosted tenancy 留待后续。

## 0.43 Hybrid execution

目标：通过受 fencing 保护的 remote Worker 明确执行位置，同时保持 control、policy 和
evidence 由宿主掌握。

已实现并独立检查：

- [x] capability declaration、HMAC attestation、HTTPS-gated transport、lease/heartbeat、
  cancellation 与 attempt fencing；
- [x] `RemoteExecutionResult` 在 canonical Run 终态前持久化 worker event、checkpoint 和 Effect；
- [x] worker 丢失与 redelivery 收敛到同一条有证据的 Run。

测试闸门：`tests/test_release_contracts.py` 中的 0.43 用例覆盖 attestation、lease mismatch/
expiry、有界 response 与 replay。生产证书轮换和跨区域 routing 仍由宿主负责。

## 0.44 Shared team workspace

目标：为多个 operator 提供显式 identity、共享 Task/Conversation、委托审批、policy scope
与可审计撤销。

已实现并独立检查：

- [x] `WorkspacePolicyStore` 在同一 SQLite authority 保存 immutable identity、bounded delegation、
  revocation 和 policy audit；
- [x] Coordinator 与 Runtime 共享一个 ownership boundary，mailbox 兼容层不能授予权限；
- [x] delegation 限定 action、resource 与 expiry。

测试闸门：contracts 测试中的 0.44 用例覆盖 delegation、revocation 与 audit。外部 identity
provider 和 tenant isolation 留待后续 tier。

## 0.45 Production connector vertical

目标：让 HTTP、MCP、Email 在 timeout、retry、redelivery 下保持诚实，并验证一个可重复的
external workflow。

已实现并独立检查：

- [x] operation/provider request identity、幂等、rate limit、secret reference 与 provider reference；
- [x] HTTP/MCP/Email 的 timeout → uncertain → reconcile、orphan 收敛和 deterministic local fixture；
- [x] connector descriptor/conformance 不静默切换 provider，也不虚报 capability。

测试闸门：contracts 与 connector 套件覆盖重复写防护和 reconciliation。真实 SaaS account 与
provider SLA 仍需部署证据。

## 0.46 Plan/Handoff interoperability

目标：把 LangGraph、AutoGen 或其他 workflow 作为一个受限 LIPAS Run 托管，不引入第二套
Task/Run authority。

已实现并独立检查：

- [x] `ExternalRunEnvelope` 与 `execute_external()` 携带 plan、handoff、identity、deadline、
  cancellation 和终态 evidence；
- [x] 外部 graph/team state 留在 LIPAS 之外，每个 handoff 都映射到 LIPAS Task/Run identity；
- [x] 空白或不稳定 framework identity fail closed。

测试闸门：contracts 中的 0.46 用例覆盖 identity、cancellation、lease renewal 与 terminal replay。
state/checkpoint adapter 和跨框架 fixture 仍开放。

## 0.47 Observability and evaluation

目标：让 operator 从 projection 观测 cost、latency、SLO、replay 和 incident，而不是读原始数据库。

已实现并独立检查：

- [x] Execution metrics、cost ledger、incident projection、evaluation fixture 和 bounded SLO window
  均从 `ExecutionStore` 派生；
- [x] 空或不完整 window 不能被报告为 healthy；
- [x] replay/failure evidence 保留因果 Run/Effect identity。

测试闸门：contracts 的 0.47 用例与 performance/execution 套件通过。export dashboard、durable
billing、benchmark dataset 和 incident workflow 仍开放。

## 0.48 Extension ecosystem

目标：让第三方 Scenario、Skill、Connector 可发现、可验证，但不获得隐式 authority。

已实现并独立检查：

- [x] canonical HMAC-SHA256 artifact/manifest signature 与 provenance 在 certification 前验证；
- [x] 认证 `ExtensionRegistryService` 提供 metadata、revoke、rollback，且不 import/execute package；
- [x] conformance 结果显式且可复现。

测试闸门：contracts 的 0.48 用例覆盖 tampering、trust policy、revocation 与 service authentication。
key custody/rotation、resolver/install sandbox 和第三方 certification 仍需部署证据。

## 0.49 Release candidate 与 design-partner 验证

目标：把独立测试的边界变成可安装、可升级产品，并由 3–5 位外部 design partner 验证。
Local fixture 不计为 partner evidence。

当前状态：backup/restore 已做 integrity check 且受 workspace lease 保护；仓库已提供
显式 `install`/`upgrade`、0600 installation manifest、`release check`，以及包含
`runs/**`（包括每个 Run 的 `claims.db`）并带 manifest、哈希和 SQLite 校验的 evidence bundle。
兼容策略、rollback 演练、证书/密钥轮换和外部验收仍属于部署工作。退出闸门是连续两周重复运行纵切，
且没有无法解释的不安全交付。

## 0.50 Agentic Execution System 基线

目标：统一 conversation-first agency、deterministic/agentic orchestration 与一个 Runtime Semantics
层；`EffectProposal → Harness → EffectObservation` 必须是 Run-owned durable evidence。

当前状态：`execute_effect_for_run()` 已闭合 proposal-to-observation 路径，
`LIPASRuntime.execute_workflow()` 可把编译计划作为一个带 lease heartbeat、逐步 checkpoint 的
Task/Run 执行，并由
`tests/test_v050_runtime.py` 覆盖。完整闸门仍未通过：用户需要从 chat 开始，在真实 workspace
自主工作，在固定 workflow 与自适应 action 间切换，以有 owner 的 Task/Effect 协作，失败后恢复并接受验证交付。

## 0.51 有界 autonomous workflow compiler

目标：把宿主提供的 Goal 与 constraints 编译为混合 Plan；固定部分可审查，自适应部分有明确上限。

已实现并独立检查：

- [x] `WorkflowGoal`、`WorkflowConstraint` 使用严格 JSON 规划输入，不把约束当作 authority；
- [x] `AutonomousWorkflowCompiler` 生成由现有 `AgentPlan`/`PlanStep` handoff 边界承载的确定性 `CompiledWorkflow`；
- [x] fixed/adaptive 模式可检查，自适应数量有界，未知依赖或循环依赖 fail closed；
- [x] `LIPASRuntime.compile_workflow()` 保持无副作用，不创建 Task、Run、Effect 或 Tool claim。
- [x] `LIPASRuntime.execute_workflow()` 通过宿主 callback 执行一个带 lease heartbeat、逐步 checkpoint
  的 Task/Run 并记录步骤事件；
  改变世界的 callback 仍必须使用标准 Effect bridge。

回归闸门：`tests/test_v051_workflow.py` 覆盖确定性输出、约束快照、自适应上限、依赖校验与 Runtime workspace 默认值。
模型辅助计划生成、provider-backed 执行与生产验收仍属于后续证据工作。

## 历史 0.60 local-first 单工作区生产化基线

目标：让 local-first control plane 可安装、可恢复、可观测，并在不把仓库测试冒充为外部生产证据的前提下，
形成通往 1.0 的可度量路径。

当前 tree 已实现并有回归覆盖：

- [x] 幂等 `install`/`upgrade`、受限 installation metadata 与机器可读的 `release check`；
- [x] 带 integrity 校验的 SQLite/evidence backup bundle、离线验证、lease-fenced restore 与保守 crash recovery；
- [x] 带 terminal-state invariant 的有界 `lipas soak` durability 演练；
- [x] Operator 与 remote Worker endpoint 的 TLS 1.2+ 配置，以及证书/trust context 热轮换；
- [x] 带有界 redaction 的 `ManagedSecretResolver` 集成边界，以及 provider key 轮换后的重新解析；
- [x] 显式真实 provider workflow evidence，以及不能把 local fixture 晋升为外部验收的摘要校验
  `DesignPartnerSignoff` artifact。

验证闸门：825 个测试通过，Ruff、mypy、compileall 与 whitespace 检查均通过。1.0 仍需真实 provider 运行、
外部 KMS/HSM 托管、可创建 loopback socket 的 TLS 轮换演练、计划中的长期 soak，以及由 partner 独立签署的
真实 workflow 证据。

## 0.63 local-first 能力完整整理版

目标：让不断增长的 capability 更容易理解和复用，同时不创建第二套 authority、permission
system 或执行路径。

当前 tree 已实现并有回归覆盖：

- [x] 简短架构导览，说明请求路径、模块职责、权威存储以及只读 chat/Workbench 边界；
- [x] Workbench 统一的路径校验、文件要求、摘要计算、evidence 记录和原子写入辅助逻辑；
- [x] 文档、代码、归档、Web 和本地知识等有界、可选依赖 capability 统一经过 Workbench policy；
- [x] 归档校验拒绝 extraction-root 成员，并在验证异常时关闭 archive handle；
- [x] provider-free capability 冒烟示例，以及中英文文档/索引覆盖。

验证闸门：852 个测试通过，Ruff 与目标模块 mypy 检查通过；package version、changelog、README
和 `PKG-INFO` 均一致为 0.63.0。真实 provider、密钥托管、TLS 与 partner 证据仍属于部署责任，
不是本地测试的隐含结论。

## 发布闸门：每个版本独立

以上每个版本都有独立目标、回归测试、退出标准和开放项；一个版本完成不会自动替另一个版本
标记完成。“已实现”仅表示本 tree 中可导入并通过测试，不等于生产证据。

LIPAS 的统一原语因此是：

```text
Conversation → Task → Run → Effect → Artifact/Report → Delivery
```

任何里程碑都不应增加第二个 message store、scheduler、permission system 或隐藏 retry policy。
新的执行位置只是同一 control plane 的 adapter，不是新的 Agent 语义。

## 0.50 架构北极星：Agentic Execution System

0.50 不是“把 LangGraph 放进 Codex”，也不是让 AutoGen 与 LIPAS 并排运行，而是一个带有
三层边界的 Agent Operating Runtime：

```text
Agent / Harness / Tool / Worker
  感知 → 推理 → 提出 Effect → 行动 → 观察
                         │
                         ▼
Plan / Handoff / Graph adapter / Team
  已知处 deterministic；不确定处 agentic
                         │
                         ▼
Task / State / Effect / Resource / Policy
  准入 → 预留 → 执行 → 恢复 → replay → 审计 → 交付
```

Agent 提出请求；Runtime 决定是否允许；execution adapter 执行；系统观察 World；最后由
Artifact/Report 关闭 Task。这就是 **autonomous workflow**：固定 workflow 与自主 action 可以
共存，但必须共享同一套 Effect、budget、approval、recovery 与 evidence 语义。Conversation 是
面向人的入口，不是真相来源；message、graph state 和 memory 都不能证明 permission 或完成。

## 安全默认值

- 工作区之外的文件访问默认拒绝；
- secret 值不得进入 prompt、trace 或报告；
- 记录实际命令、退出状态和结构化 Shell 风险类别；
- 删除、发布、推送、发送消息和外部写默认需要审批；
- external write 使用稳定 idempotency key；
- 不确定的外部结果进入 `uncertain`，不得盲目重试；
- “完成”必须附带验证证据，或者明确说明尚未验证；
- replay 默认不得重新执行实时 write。

## 当前 0.63 明确不做

- 通用生活助理；
- 多聊天渠道 gateway；
- Agent graph 编辑器或角色社会；
- 自动生成和自我改进 Skill；
- 长期用户画像和通用 memory；
- SaaS control plane、SSO、SCIM、复杂 RBAC 和计费（这些属于未来组织级能力，不是 0.63
  的隐藏能力）；
- 自动发布、自动推送或不受限的系统访问。

## 真正重要的指标

产品信号是真实任务的重复执行、同类任务的重复使用，以及用户是否愿意为某个具体高频
流程付费。可靠性信号是所有工具和模型调用都有 terminal result 或可见 orphan、审批
能够持久恢复、强制中断恢复稳定、不出现无记录的写重试，并且每次“完成”都有证据。
模型数量、Agent 数量和 GitHub stars 不是当前阶段的主要里程碑。
