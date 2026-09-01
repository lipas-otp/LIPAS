# 0.63 / 1.0 local-first 发布检查清单

这份清单记录通往 1.0 的 0.63 能力完整整理版，并区分仓库内的自动化契约与部署证据。测试套件通过并不代表已经
拥有 provider SLA、KMS/HSM 密钥托管或外部 design partner 验收。

## 安装前

- 在干净虚拟环境安装 wheel，执行 `lipas install`。
- 执行 `lipas release check`，解决所有失败项。
- 将 workspace 放在可靠的本地文件系统，并实际演练恢复。

## 开启写入前

- 在临时 workspace 执行 `lipas tour --offline` 与 fault matrix。
- 执行有界本地 soak 并保留 JSON 报告，例如
  `lipas soak --home "$LIPAS_HOME" --iterations 10000 --json`。
- 使用 `run_provider_workflow(..., live=True)` 执行一次经过明确批准的真实 provider workflow，
  只归档脱敏后的终态 evidence、usage、provider request identity 与 reconciliation 记录。
- 用 provider-free fixture 演练审批、取消、uncertain operation reconciliation
  和进程重启恢复。
- 只配置 allowlist 的 secret reference；原始凭据不得进入 prompt、tool 参数、URL、报告或日志。
- 非 loopback Operator/Worker 必须启用 TLS 与认证，并记录证书过期、轮换和回滚负责人。
  轮换时加载新的 `TLSConfig` 并调用 `server.reload_tls(...)`；如果是 client trust 轮换，
  同时调用 `RemoteWorkerHTTPClient.reload_tls(...)`。已有连接继续使用旧证书完成，新的连接使用新 context。
- 通过 `ManagedSecretResolver` 注入 KMS/HSM 或 secret-manager；resolver 只返回 opaque reference，
  并为 provider 响应提供 redactor。内置 file resolver 不等于密钥托管证明。

## Backup/restore 演练

```bash
lipas backup --home "$LIPAS_HOME" --destination /safe/lipas-bundle \
  --include-evidence
lipas verify-bundle --source /safe/lipas-bundle
lipas restore --home "$LIPAS_RESTORE" --source /safe/lipas-bundle --yes
lipas release check --home "$LIPAS_RESTORE"
```

evidence bundle 是单 workspace 的归档单元：包含 `workspace.db`、`runs/` 下所有普通文件、
安装元数据，以及带 SHA-256 和 SQLite integrity 校验的 manifest。恢复后的 workspace
通过 audit 前至少保留一个 rollback bundle。

## 外部验收

真实 operator 运行必须与本地 fixture 分开记录。只有在计划的 soak 期、事故复盘和回滚演练
都有负责人及可复现证据后，发布声明才算完成。
