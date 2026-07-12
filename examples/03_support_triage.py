"""Lesson 03 — answer a support request without exposing or changing data.

Run::

    python -m examples.03_support_triage

This pattern fits many small internal assistants: provide narrow lookup tools,
tell the Agent when human approval is required, and keep account-changing
operations out of the first version.  Replace the dictionaries below with
your own CRM and help-centre clients when ready.
"""
from __future__ import annotations

from pathlib import Path

from lipas import Agent, tool


CUSTOMERS = {
    "C-42": {"name": "Ada", "plan": "pro", "status": "active"},
    "C-99": {"name": "Lin", "plan": "starter", "status": "past_due"},
}
HELP = {
    "invoice": "Invoices are available from Settings → Billing.",
    "password": "Use the password-reset link; support never needs your password.",
    "past_due": "A past-due account needs a successful payment before service resumes.",
}
SKILLS = Path(__file__).with_name("skills")


@tool(side_effect="read_only")
def lookup_customer(customer_id: str) -> dict[str, str]:
    """Return only the fields this support assistant is allowed to see."""
    return CUSTOMERS.get(customer_id, {"status": "not_found"})


@tool(side_effect="read_only")
def search_help(topic: str) -> str:
    """Look up one safe help-centre answer from local documentation."""
    normalized = topic.lower()
    for keyword, article in HELP.items():
        if keyword in normalized:
            return article
    return "No matching article. Escalate to a human support specialist."


def build_agent(*, session: str | Path = "runs/03-support-triage.db") -> Agent:
    return Agent.ollama(
        tools=[lookup_customer, search_help],
        skills=SKILLS / "support-triage",
        instructions="Resolve support questions with the available tools before answering.",
        session=session,
        max_tokens=600,
        max_iterations=4,
        budgets={"tool_calls": 5, "tokens_out": 1_800},
    )


def main() -> None:
    with build_agent() as agent:
        print("agent> checking the account and help centre…", flush=True)
        result = agent.ask(
            "Customer C-99 says service stopped and asks what to do next."
        )

    if result.is_error:
        print("agent error:", result.error)
    elif result.text:
        print("agent>\n", result.text)
    else:
        print("agent stopped without a final answer:", result.stop_reason)
        print("inspect `python -m lipas.cli effects runs/03-support-triage.db`.")


if __name__ == "__main__":
    main()
