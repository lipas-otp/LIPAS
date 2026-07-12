"""External-operation recovery: prepare, uncertainty, reconciliation.

Run from the repository root: ``python -m examples.11_operation_journal``

This example deliberately does not pretend a failed provider call did not
happen. It shows why the same idempotency key cannot simply be re-sent after a
crash window.
"""
from __future__ import annotations

from pathlib import Path

from lipas import OperationJournal
from lipas.operations import PendingOperation


def flaky_provider(*, idempotency_key: str) -> dict[str, str]:
    raise ConnectionError(f"connection dropped after submit key={idempotency_key}")


def main() -> None:
    path = Path("runs/example-operations.db")
    path.parent.mkdir(exist_ok=True)
    with OperationJournal(str(path)) as journal:
        try:
            journal.execute(
                key="email-001",
                kind="send_email",
                request={"to": "ada@example.test", "subject": "Hello"},
                provider=flaky_provider,
            )
        except ConnectionError as error:
            print("provider outcome is uncertain:", error)

        print("journal state:", journal.get("email-001").state)
        try:
            journal.execute(
                key="email-001", kind="send_email",
                request={"to": "ada@example.test", "subject": "Hello"},
                provider=lambda **_: {"sent": True},
            )
        except PendingOperation as error:
            print("blind retry refused:", error)

        # Replace this lookup with the real provider's idempotency-key lookup.
        operation = journal.reconcile(
            "email-001", lambda _key: (True, {"sent": True}, "provider-msg-42"),
        )
        print("reconciled state:", operation.state, operation.provider_reference)


if __name__ == "__main__":
    main()
