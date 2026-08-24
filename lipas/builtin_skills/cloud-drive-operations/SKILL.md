---
name: cloud-drive-operations
description: Plan and review scoped cloud-drive organization without hiding provider writes or sharing changes.
category: connectors
authority: instructions-only
---
# Cloud-drive operations

Treat provider files as shared user-owned state.

1. Confirm the provider account, allowed root or folder ids, item types, ownership, sharing boundaries, and retention rules.
2. Inventory before proposing moves, renames, uploads, deduplication, archival, or deletion. Use provider item ids rather than ambiguous display names.
3. Show a deterministic old-to-new mapping and collisions before requesting approval.
4. Never broaden sharing, change ownership, delete versions, or cross the allowed root as a side effect of organization.
5. Use stable operation keys and record provider item references. Reconcile timeouts or lost responses before repeating a write.
6. Report skipped, conflicted, and uncertain items separately from completed moves.

This Skill grants no cloud account, file, sharing, or network authority.
