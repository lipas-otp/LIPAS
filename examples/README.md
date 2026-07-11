# Examples

Examples are numbered in a recommended learning order. Examples 01–09 cover
the lower-level harnesses; 10–12 use the current high-level Python API and run
without a model provider unless noted.

| Example | Scenario | Provider required |
|---|---|---|
| `01_single_call.py` | One audited LLM call | Ollama |
| `02_budget.py` | Budget rejection before a model call | Ollama setup only; call is rejected |
| `03_guard.py` | Guard rejection before a model call | Ollama setup only; call is rejected |
| `04_replay.py` | LLM transcript replay | Ollama for recording |
| `05_react_calculator.py` | ReAct plus pure tools | Ollama |
| `06_react_replay.py` | ReAct replay and request mismatch | Ollama for recording |
| `07_tool_replay.py` | Strict tool tape substitution | No provider |
| `08_supervisor.py` | Supervisor recommendation claims | No provider |
| `09_loop_with_supervisor.py` | High-level Agent supervision | Ollama |
| `10_team_mailbox.py` | Named Team-member handoff and durable mailbox | No provider |
| `11_operation_journal.py` | Uncertain external operation and reconciliation | No provider |
| `12_supervised_agent.py` | High-level `Agent(supervisor_policy=...)` | No provider |

All examples write any durable demo state under `runs/`; that directory can be
deleted between runs. Run them from the repository root, for example
`python -m examples.07_tool_replay`; invoking a numbered file directly does
not place the project package on Python's import path.
