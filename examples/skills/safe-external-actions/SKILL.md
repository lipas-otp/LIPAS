---
name: safe-external-actions
description: Handle external writes with confirmation, idempotency, and recovery awareness.
---
Before an external write such as sending an email, changing an account, or
charging money:

1. State the intended effect and the target.
2. Require explicit confirmation when the action is consequential.
3. Use a stable idempotency key when the provider supports one.
4. Treat an interrupted submission as uncertain until reconciled.
5. Never claim an external action succeeded without a recorded provider result.
