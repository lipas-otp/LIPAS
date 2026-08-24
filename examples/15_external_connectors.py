"""Provider-neutral HTTP/MCP/email connector boundaries (no network needed)."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from lipas import EmailConnector, EmailDelivery, EmailMessage, OperationJournal
from lipas.integrations import MCPClient


class DemoEmailProvider:
    def __init__(self) -> None:
        self.sent: dict[str, EmailDelivery] = {}

    def send(self, message: EmailMessage, *, idempotency_key: str) -> EmailDelivery:
        delivery = EmailDelivery(
            provider_reference=f"demo-{len(self.sent) + 1}",
            provider_request_id=idempotency_key,
        )
        self.sent[idempotency_key] = delivery
        return delivery

    def lookup(self, *, idempotency_key: str):
        delivery = self.sent.get(idempotency_key)
        return (
            delivery is not None,
            None if delivery is None else {"accepted": True},
            None if delivery is None else delivery.provider_reference,
        )


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="lipas-connectors-") as root:
        journal = OperationJournal(Path(root) / "operations.db")
        provider = DemoEmailProvider()
        connector = EmailConnector(provider, journal)
        message = EmailMessage(
            sender="agent@example.test",
            recipients=("owner@example.test",),
            subject="Review requested",
            text="The staged change is ready for review.",
        )
        first = connector.send(message, idempotency_key="demo-mail-1", approved=True)
        replay = connector.send(message, idempotency_key="demo-mail-1", approved=True)
        print("email:", first.provider_reference, replay.provider_reference)

        async def transport(message):
            if message["method"] == "initialize":
                result = {"protocolVersion": "2025-06-18"}
            else:
                result = {"isError": False, "content": []}
            return {"jsonrpc": "2.0", "id": message["id"], "result": result}

        mcp = MCPClient(transport)
        print("mcp:", await mcp.initialize())
        journal.close()


if __name__ == "__main__":
    asyncio.run(main())
