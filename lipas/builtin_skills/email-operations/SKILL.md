---
name: email-operations
description: Prepare a scoped email delivery request with preview, approval, idempotency, and reconciliation checks.
category: connectors
authority: instructions-only
---
# Email operations

Treat composing and sending as separate phases.

1. Confirm the sending account, To/Cc/Bcc recipients, subject, final body, attachments, reply/thread identity, and confidentiality classification.
2. Show a complete preview before requesting approval. Never infer recipients from prose or expand a distribution list silently.
3. Use one stable idempotency key for the approved logical message. Do not reuse it after recipients, body, subject, or attachments change.
4. Require the Tool result to include a provider message id or a structured failure. A timeout or lost response is uncertain, not safe to resend.
5. Reconcile uncertain attempts through provider state before retrying. Record what was checked and the resulting provider reference.
6. Never claim delivery from an intent, approval, queued state, or missing Tool result.

This Skill grants no account or network authority. A separately supplied `send_email` Tool and trusted host policy are required.
