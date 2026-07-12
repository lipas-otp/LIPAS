"""Lesson 09 — recover safely after an ambiguous external write.

Run::

    python -m examples.09_external_operation

This is the pattern for email, payment, or remote account changes.  Persist a
caller-owned idempotency key before submission.  If the provider connection
drops, do not resend blindly: reconcile provider state first.  The example is
fully local and intentionally does not claim provider-independent exactly-once
delivery.
"""
from __future__ import annotations

from uuid import uuid4

from lipas import OperationJournal
from lipas.operations import PendingOperation


def flaky_email_provider(*, idempotency_key: str) -> dict[str, str]:
    """Simulate the crash window: the provider outcome is not knowable here."""
    raise ConnectionError(f"connection dropped after submit key={idempotency_key}")


def main() -> None:
    with OperationJournal("runs/09-external-operation.db") as journal:
        # Retain this key for every retry and reconciliation of this one email.
        operation_key = f"email-{uuid4().hex}"
        request = {"to": "ada@example.test", "subject": "Hello"}

        try:
            journal.execute(
                key=operation_key,
                kind="send_email",
                request=request,
                provider=flaky_email_provider,
            )
        except ConnectionError as error:
            print("provider outcome is uncertain:", error)

        print("journal state:", journal.get(operation_key).state)
        try:
            journal.execute(
                key=operation_key,
                kind="send_email",
                request=request,
                provider=lambda **_: {"sent": True},
            )
        except PendingOperation as error:
            print("blind retry refused:", error)

        # Replace this with the provider's lookup by the same idempotency key.
        operation = journal.reconcile(
            operation_key,
            lambda _key: (True, {"sent": True}, "provider-msg-42"),
        )
    print("reconciled state:", operation.state, operation.provider_reference)


if __name__ == "__main__":
    main()
