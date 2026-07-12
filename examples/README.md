# LIPAS examples: a small course in reliable agents

Run every lesson from the repository root:

```bash
python -m examples.01_first_agent
```

Durable state is written under `runs/`. The directory is created
automatically and is safe to delete when you want a fresh experiment.

Do not read these files as a framework API catalogue. Read them in order, and
copy the closest scenario into your own project. Each lesson is a complete
ordinary Python script with the same stable shape:

```text
local data or a real client
        ↓
@tool(side_effect=...)      # what the assistant may actually do
        ↓
build_agent(...)            # model, instructions, Skill, session, budget
        ↓
main()                      # one clear business request and visible result
```

## Part 1: build useful single assistants

These four require a local Ollama model. Install once with
`pip install -e '.[ollama]'`, then run `ollama pull gemma4:12b`.

| Lesson | Run | Practical question it answers | Copy it when… |
| --- | --- | --- | --- |
| 01 | `python -m examples.01_first_agent` | “How do I give one assistant one safe capability?” | You are starting a small assistant. |
| 02 | `python -m examples.02_research_brief` | “How do I search sources and write a careful brief?” | You triage papers, policies, or internal knowledge. |
| 03 | `python -m examples.03_support_triage` | “How do I handle a customer question without changing data?” | You combine account lookup and help-centre information. |
| 04 | `python -m examples.04_daily_brief` | “How do I turn several sources into a daily decision brief?” | You summarize metrics, incidents, reports, or dashboards. |

The data in these scripts is local on purpose. First understand the boundary;
then replace one tool body with your database, API, or search client. Keep the
tool's side-effect declaration truthful.

## Part 2: add one reliability boundary at a time

These lessons are self-contained. Lesson 05 needs the `ollama` extra because
it constructs an Ollama-backed Agent, but it rejects the request before it
contacts an Ollama daemon or model. Lessons 06–10 use only the core package
and run fully offline.

| Lesson | Run | Boundary it teaches | Add it when… |
| --- | --- | --- | --- |
| 05 | `python -m examples.05_budget_limit` | Budget rejection before a model call | A request must not exceed a hard output/spend limit. |
| 06 | `python -m examples.06_strict_replay` | Recorded tool output replaces live execution | You must inspect or reproduce a prior run safely. |
| 07 | `python -m examples.07_supervision` | A policy records termination/review advice | A concrete risk or escalation rule needs an audit trail. |
| 08 | `python -m examples.08_team_handoff` | Durable, at-least-once handoff to one owner | A task needs its own restart or ownership boundary. |
| 09 | `python -m examples.09_external_operation` | Idempotency, uncertainty, and reconciliation | A provider write might have happened before a crash. |
| 10 | `python -m examples.10_research_review_team` | Two-owner evidence and review workflow | One assistant is no longer the right ownership boundary. |

## Reusable Skills

The `skills/` directory contains small portable `SKILL.md` templates. A Skill
is reusable guidance, not an access-control mechanism: it changes how an
Agent approaches a task, but only an `@tool` gives it an executable
capability.

| Skill | Used by | What it teaches |
| --- | --- | --- |
| `research-brief` | Lesson 02 | Separate source facts, synthesis, and uncertainty. |
| `support-triage` | Lesson 03 | Protect customer information and state clear next steps. |
| `daily-brief` | Lesson 04 | Prioritize operational risks and recommendations. |
| `safe-external-actions` | Lesson 09 | Require confirmation, idempotency, and reconciliation. |

Copy a directory into your project and pass its path directly:

```python
agent = Agent.ollama(
    tools=[search_papers],
    skills="skills/research-brief",
)
```

## Inspect a run

Every Agent/Team/OperationJournal lesson names its session path in the file.
After a run, inspect the exact claims and effects rather than guessing:

```bash
python -m lipas.cli trace runs/02-research-brief.db
python -m lipas.cli effects runs/02-research-brief.db
```

An `orphan` effect means the process ended after intent was recorded but
before LIPAS observed a terminal result. Treat it as an interrupted operation,
not a successful answer.
