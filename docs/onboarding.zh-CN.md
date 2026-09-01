# 面向生产的安装与 onboarding

LIPAS 0.63 是 local-first。推荐先使用不依赖 provider 的路径，验证 storage、sandbox、
审批、恢复和交付，再配置可能产生费用的模型或 connector：

## 五分钟首次试用

如果从源码 checkout 开始，先使用隔离的虚拟环境和专门的试用 home：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[all]'
mkdir -p ~/lipas-demo
cd ~/lipas-demo
lipas install --home ~/.lipas-demo --sandbox auto
lipas doctor --home ~/.lipas-demo
lipas tour --offline
```

离线 tour 不需要模型、provider 账号或网络访问，是最推荐的第一个成功标准。
如果 `doctor` 报告 Bubblewrap 不可用，仍可先完成离线 tour；只有在临时 workspace
中处理可信代码、并明确接受不隔离 fallback 时，才使用 `--sandbox local`。

之后若要试用本地模型，启动 Ollama、准备模型，再执行一次有界 prompt：

```bash
ollama pull gemma4:12b
lipas model list
lipas chat --model gemma4:12b \
  --session ~/.lipas-demo/runs/chat.db \
  --once "用三句话解释 local-first control plane。"
```

模型名也可以直接写在 `chat` 后面，适合快速试用：

```bash
lipas chat phi4-mini --once "用一句话介绍你自己"
```

本地 Ollama 默认直连，不会读取 shell 中的 `HTTP(S)_PROXY`/`ALL_PROXY`，
因此不会被不兼容的代理配置挡住。连接远程 Ollama 且确实需要代理时，显式加上
`--trust-env`。

内置 Chat 默认会把上下文记在 `~/.lipas/runs/chat.db`；一次性试用可加
`--no-memory`。这里保存的是当前会话的完整消息历史（用户消息、助手回复和工具结果），
不是模型的隐式个人记忆；目前还没有自动摘要或向量检索，因此超长对话仍受模型上下文窗口
限制。如果希望它读取项目文件或提取 PDF 文本，可显式加上 `--workspace .`，这只提供
受边界保护的只读文件工具；写入、格式转换和命令执行仍应使用 Task Workbench。
交互式 REPL 中的 `:memory` 可查看会话记忆，`:runtime` 可查看当前目录与能力边界。

Chat 还会提供只读的 `get_runtime_info` 工具，返回真实的当前目录、选定 workspace、会话
身份、记忆模式和能力边界。询问目录或能力时，模型必须以这些运行时事实为准，不能笼统声称
“没有文件系统”。Chat 本身没有写入和 shell 权限；需要修改文件时使用
`lipas task start <目标> <workspace>`，再明确审核并应用生成的 ChangeSet。

例如：`lipas chat phi4-mini --workspace . --once "列出 Python 文件并概括入口"`。

## 标准安装与 readiness

```bash
python -m pip install 'lipas[all]'
lipas install --home ~/.lipas --sandbox auto
lipas release check --home ~/.lipas
lipas doctor --home ~/.lipas --json
lipas tour --offline
```

`doctor` 会区分未初始化 workspace、需要显式 migration、SQLite 健康度和默认 sandbox
是否可用。Linux 默认使用 Bubblewrap；只有可信代码才可以显式选择 `--sandbox local`。
离线 tour 会验证用户输入与写审批的权限分离、durable resume、audit 和 report，不调用模型，
也不执行外部写入。

已有旧 workspace 时：

```bash
lipas migrate plan --home ~/.lipas
lipas migrate apply --home ~/.lipas --yes
lipas migrate verify --home ~/.lipas
lipas audit --home ~/.lipas --repair
```

生产环境也可以直接使用幂等的 `lipas upgrade --home ~/.lipas`；它会写入
`.installation.json`，不会删除保留的 legacy 或 pre-restore backup。`lipas backup`
与 `lipas restore` 使用 SQLite online-backup 和 integrity check；restore 会与活动
Runtime fencing，并要求显式确认参数。

需要完整迁移（包括每个 Run 的证据）时，显式创建 bundle：

```bash
lipas backup --home ~/.lipas --destination /safe/lipas-bundle --include-evidence
lipas restore --home ~/.lipas-restored --source /safe/lipas-bundle --yes
```

bundle 包含 `workspace.db`、`runs/**`（包括每个 Run 的 `claims.db`）和安装元数据；
manifest 记录文件大小、SHA-256 与 SQLite integrity。恢复到新 home 时会重写安装路径，
并默认保留 rollback bundle。

启用 external connector 前必须确认 egress allowlist、稳定幂等 key、provider lookup/
reconciliation、审批中的 scope/preview/diff/budget，以及一次 provider-free 故障演练。
Local operator 提供 `/api/approvals`、`/api/operations` 与
`/api/operations/<key>/reconcile`；mutation 需要 bearer token，reconciliation 必须由
operator 明确决定；每次 operation closeout 都必须记录 observation，found 时还必须
记录 provider reference，Run reopen 则必须记录 evidence 对象。它绝不会由 timeout 或
单独的布尔确认自动推断。

本地密钥可以由 `FileSecretResolver` 管理：业务数据只保存
`secret://file/NAME` 引用，文件以 0600 权限原子轮换。任何非 loopback 的 Operator
或 remote Worker bind 都必须配置 `TLSConfig`（TLS 1.2+ 证书/私钥）并保持认证开启；
loopback HTTP 只用于开发 fixture。

邀请 partner 前可以先运行可重复的 fixture harness：

```python
from lipas import DesignPartnerCase, run_design_partner_validation

report = run_design_partner_validation(
    "local-fixture",
    [
        DesignPartnerCase("repo", "Repository maintenance", "inspect, patch, verify"),
        DesignPartnerCase("mail", "Controlled email", "draft, approve, send, reconcile"),
    ],
    lambda case: {
        "run_id": "fixture-" + case.case_id,
        "success": True,
        "unsafe_delivery": False,
        "operator_accepted": True,
    },
)
assert report.evidence_scope == "local_fixture"
```

这只验证报告格式和 operator workflow，不是 partner 证据；外部 partner 必须使用自己的
账号执行同样的 case，并对脱敏证据包签字确认。

## Design partner 验证

首批外部 partner 每家选择一个重复的 workspace workflow：代码/发布、文档/数据处理，或
受控的 email/ticket 准备。试点不得启用不受限的 autonomous publishing；provider 账号、workspace、
保留策略和审批策略由 partner 自己负责。

每位 partner 执行同一组有界 fixture：

1. inspect-only task；
2. staged local write、verification 与 diff review；
3. Effect 已提交但 checkpoint 尚未写入时 process kill；
4. approval 与缺失输入的 suspend/resume；
5. provider timeout 进入 `uncertain` 后 reconciliation；
6. 使用相同 request identity 的 redelivery；
7. path escape 或 egress request 拒绝。

记录 verified completion、重复写入次数（目标为零）、恢复时间、uncertain operation 数量与
reconciliation 时间、approval latency、人工接管原因、估算与实际 usage，以及 operator
能否解释变更、验证结果和仍然不确定的部分。不要把测试数量当作信任的代理指标；连续两周没有
无法解释的重复写入、未审批 external write 或不可恢复 workspace 状态，才扩大试点。

证据包只包含脱敏 task report、event cursor、verification 结果、operation reconciliation
记录和访谈摘要；未经明确的保留同意，不得导出 raw secret、完整 provider payload 或个人内容。
