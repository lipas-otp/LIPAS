---
name: workspace-files
description: Inspect, organize, and edit workspace files conservatively with verification.
category: files
authority: instructions-only
---
# Workspace file work

Treat the selected workspace as user-owned state.

1. Inspect relevant paths and nearby conventions before proposing a change.
2. Read the smallest useful set of files. Do not infer file contents or claim access that a Tool did not provide.
3. Preserve encoding, line endings, naming, formatting, and unrelated content unless the task explicitly requires a change.
4. Prefer a small, reviewable edit. Explain destructive operations, broad rewrites, renames, and deletions before requesting them.
5. Keep temporary output, caches, credentials, and generated artifacts out of the deliverable unless explicitly requested.
6. Verify the resulting paths and contents. When verification is unavailable, say exactly what remains unchecked.

A Skill does not authorize reading or writing. Use only the file Tools supplied by the host and respect every approval or staging boundary.
