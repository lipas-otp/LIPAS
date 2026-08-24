# SQLite 存储与并发

> 语言：[English](sqlite-storage.md) | [中文](sqlite-storage.zh-CN.md)
>
> 存储内核在 0.38 引入，并作为 LIPAS 0.40.0 的受支持后端。

LIPAS 明确选择 SQLite 作为本地和中等并发部署的存储内核。Agent 的主要耗时来自模型、
网络、sandbox 和 Tool，持久控制写入则短而小。对这一常见本地负载，PostgreSQL 会显著
增加部署与运维成本，却不一定改善实际吞吐。

## 物理布局与权威边界

```text
workspace.db
  Task / Run / lease / checkpoint / interrupt
  Workbench / Session / Operation / Handoff / global audit outbox

runs/<run-id>/claims.db
  append-only model、Tool、Effect、usage 与 Run evidence
```

`ExecutionStore` 是 Task/Run 控制权威。Run-local Claim tape 只承载证据和 replay 输入，
绝不决定 lease、approval 或 checkpoint 状态。transactional outbox 把已提交的控制转换
镜像到 evidence，但不会伪装成跨文件分布式事务。

## 统一连接策略

所有常规 Store 都通过同一个内核打开连接：

- `PRAGMA foreign_keys=ON`；
- `PRAGMA trusted_schema=OFF`；
- 5 秒有界 `busy_timeout`；
- 可写文件数据库使用 WAL；
- 默认 `synchronous=FULL`；`NORMAL` 是显式性能选择；
- read-only URI connection 强制 `query_only=ON`；
- WAL 达到 1,000 page 后自动 checkpoint；
- CAS/fencing 控制事务使用 `BEGIN IMMEDIATE`。

一次常规 writer ownership 获取最多等待一个配置的 busy timeout；只有调用方显式要求更多
attempt 时才会再次等待。LIPAS 不会自动重放事务 body，因为调用方代码可能隐藏外部副作用。
任何数据库事务都不得跨越模型、网络、sandbox 或 Tool `await`。

## 并发模型

- 多条 Run 可以同时推理并调用互不相关的 read Tool。
- 每条 durable Run 使用自己的 ExecutionStore connection 与 evidence sink；Workbench
  control store 保持稳定，不再动态 reattach。
- SQLite 串行提交很短的 `workspace.db` 写事务；WAL 让 reader 在提交期间继续读取。
- per-Run evidence tape 把模型与 Tool 事件流量移出全局 writer 热点。
- 同步 Tool 共享 lazy、fork-safe 且同时限制 worker 与 submission 的进程级 executor。
  Python thread 无法强杀，因此超时调用会保持 `uncertain`，直到 reconciliation。
- Dispatcher 和 Tool 并发限制只是 admission control，不是第二个持久队列；待执行工作
  仍以 ExecutionStore 为唯一来源。

单个本地 workspace 的建议运行边界是最多 16 条 active Run，同时可以持久排队更多 Run。
实际合理上限取决于模型 rate limit、Tool 成本、磁盘延迟和 write-heavy 操作比例；应对真实
负载做 benchmark，而不能把这个数字当成数据库保证。

## Evidence 分页与 projection snapshot

Claim identity 与 sequence 在同一 SQLite writer transaction 中接纳。相同 identity 与
payload 的并发重投是 no-op；相同 identity 携带不同 payload 会失败关闭。

```python
from lipas.serialization.store_sqlite import SqliteClaimStore

with SqliteClaimStore("claims.db") as store:
    page = store.read_page(after_seq=-1, limit=100)
    cursor = page.next_cursor
    store.checkpoint_projection()
```

projection snapshot 保存 reducer 的 merged result 与最后 sequence。重新打开时，Store
加载 reducer/context fingerprint 相同的最新 snapshot，只 replay 后续 Claim。Snapshot
只是加速结构：

- append-only tape 始终是权威；
- snapshot 不授予权限，也不能证明 Effect；
- 不兼容 snapshot 会被忽略；
- 损坏、sequence 无锚点或 checksum 不符的 snapshot 会回退到 tape replay；
- 删除 snapshot 只会使下次打开变慢；
- snapshot 创建失败不能让已经提交的 append 看起来失败。

`read_page()` 与带索引的 `filter()` 可以从 SQLite 读取历史 Claim，而不让整个 tape 常驻
内存。为兼容旧 API，`log` 仍然可用，并会明确物化该旧接口请求的全部 Claim。
普通状态转换每次最多 drain 一个有界 outbox batch；显式 `repair_audit()` 会流式处理完整
剩余 backlog。旧审计记录只 seed 并标记一次，不会在每次打开时重新扫描所有 Task、
Operation 或 handoff。

## 不引入服务端数据库时如何扩展

先使用简单分区，而不是增加基础设施：

1. 每个独立 workspace 或用户使用一个 `workspace.db`；
2. 高流量 evidence 继续按 Run 分文件；
3. 分别限制 active model、read Tool 和 write Tool slot；
4. 保持事务短小，把 projection/audit catch-up 移出请求关键路径；
5. retention policy 允许时，以 SQLite 一致性备份和验证归档已完成 workspace。

不要把可写 SQLite workspace 放到锁与持久语义未知的网络文件系统上，也不要让多台机器
共同写同一个数据库文件。如果未来产品确实需要多机调度，可以让远程事务后端实现同一套
领域 Store contract；那是另一种部署档位，不是本地模式中的静默 fallback。

要做有界的本地 contention probe，可以给 benchmark 指定 worker 数量：

```python
from lipas import benchmark_execution_store

result = benchmark_execution_store(
    ".lipas/benchmark.db",
    operations=100,
    workers=4,
)
```

每个 worker 都会打开自己的 SQLite connection 并报告 transition latency。结果只是诊断样本，
不是吞吐承诺，也不会替代 append-only evidence 与 ExecutionStore authority。

DuckDB 很适合分析，却不适合这条 OLTP 控制路径；LMDB 同样只有一个 writer，还需要重建
SQL constraint 与诊断；Redis 不能替代持久权威。libSQL 或托管 SQLite-compatible 服务
未来可以成为远程候选，但它已经引入 server，且必须先通过相同的 lease、CAS、outbox、
crash 与 uncertain Effect contract test，LIPAS 才能如实宣称支持。
