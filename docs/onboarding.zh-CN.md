# 面向生产的安装与 onboarding

LIPAS 0.40 是 local-first。推荐先使用不依赖 provider 的路径，验证 storage、sandbox、
审批、恢复和交付，再配置可能产生费用的模型或 connector：

```bash
python -m pip install 'lipas[all]'
mkdir -p ~/.lipas
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

启用 external connector 前必须确认 egress allowlist、稳定幂等 key、provider lookup/
reconciliation、审批中的 scope/preview/diff/budget，以及一次 provider-free 故障演练。
Local operator 提供 `/api/approvals`、`/api/operations` 与
`/api/operations/<key>/reconcile`；mutation 需要 bearer token，reconciliation 必须由
operator 明确决定；每次 operation closeout 都必须记录 observation，found 时还必须
记录 provider reference，Run reopen 则必须记录 evidence 对象。它绝不会由 timeout 或
单独的布尔确认自动推断。

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
