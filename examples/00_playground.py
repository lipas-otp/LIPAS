"""The smallest useful LIPAS project.

From the repository root:

    pip install -e '.[ollama,cli]'
    ollama pull gemma4:12b
    python -m examples.00_playground

Then inspect what happened:

    lipas trace runs/playground.db
    lipas effects runs/playground.db

This is ordinary Python. The only reliability declaration is the tool's
explicit side-effect class; add budgets, Teams, or supervision only when the
project needs those boundaries.
"""
from lipas import Agent, tool


@tool(side_effect="read_only")
def lookup_book(topic: str) -> str:
    """Return one local catalogue entry for a topic."""
    return {
        "replay": "Designing Data-Intensive Applications",
        "agents": "Building LLM-Powered Applications",
    }.get(topic.lower(), "No catalogue entry found.")


def main() -> None:
    # Agent.ollama() defaults to the documented local model. Pass model= only
    # when you intentionally want a different local model.
    with Agent.ollama(
        tools=[lookup_book],
        instructions="Use lookup_book for book recommendations; be concise.",
        session="runs/playground.db",
    ) as agent:
        result = agent.ask("Recommend a book about replay.")
        print(result.text)


if __name__ == "__main__":
    main()
