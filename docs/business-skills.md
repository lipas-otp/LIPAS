# Business Skills, Scenarios, and capabilities

> Language: [English](business-skills.md) | [中文](business-skills.zh-CN.md)

LIPAS 0.35 grows business breadth outside the execution core:

| Layer | Owns | Never owns |
| --- | --- | --- |
| Skill | instruction-only domain method | file, network, account, or delivery authority |
| BusinessScenario | selected Skills, lifecycle, and required Tool contracts | another Run state machine or implicit permission |
| Tool / Capability | one real action with an honest side-effect class | durable business orchestration |
| Runtime / Workflow | Run, Effect, approval, recovery, evidence, and delivery | hidden domain inference |

This gives applications a useful recipe without confusing knowledge with
authority. A Scenario can say that email delivery needs a `send_email`
external-write Tool, approval, idempotency, and reconciliation; selecting it
does not create that Tool or make an account available.

## Catalog

The 17 packaged Skills cover the complete first business surface:

| Area | Skills |
| --- | --- |
| Files | `workspace-files`, `document-processing` (bounded PDF extraction and text/office conversion Tools) |
| Engineering | `coding-task`, `code-review`, `release-readiness` |
| Office | `email-drafting`, `business-report`, `meeting-notes`, `business-notice`, `proposal-writing`, `calendar-planning` |
| Personal writing | `personal-letter`, `speech-writing`, `celebration-message` |
| Connector method | `email-operations`, `cloud-drive-operations`, `ticket-triage` |

The 18 packaged Scenarios turn those units into explicit recipes:

| Mode | Scenarios | Execution boundary |
| --- | --- | --- |
| Draft | `email-draft`, `office-report`, `meeting-notes`, `business-notice`, `proposal-draft`, `calendar-planning`, `personal-letter`, `speech-draft`, `celebration-message` | no Tool required; returns reviewable text |
| Workspace | `file-management`, `document-processing`, `coding-change`, `code-review`, `release-readiness` | bounded Workbench Tools and staged delivery |
| Connector | `email-delivery`, `calendar-update`, `cloud-drive-organization`, `ticket-triage` | application-supplied scoped Tools; external writes remain approval-gated |

Nothing is auto-selected. Catalog growth therefore does not increase prompt
size, token cost, or model attention for unrelated jobs. Built-ins are loaded
lazily and cached per process.

## Inspect, select, and validate

No model or account is needed to inspect the catalog:

```bash
lipas skill list
lipas skill show code-review
lipas scenario list
lipas scenario show email-delivery
lipas scenario check email-draft
```

`scenario show` exposes the lifecycle, Skill bundle, exact Tool names,
required input fields, side-effect classes, approval point, and idempotency/reconciliation
requirements. Check an application's Tool factory before execution:

```bash
lipas scenario check email-delivery \
  --factory connectors:email_tools \
  --json
```

The check proves only that required Tool names, input fields, and side-effect
declarations match. It cannot prove provider account scope, recipient policy, secret
handling, human approval, or reconciliation implementation; connector
Scenarios report those host obligations separately.

Select one complete recipe or compose several:

```bash
lipas chat --scenario office-report --once "Draft the supplied weekly status"

lipas task start . "repair the parser and assess release readiness" \
  --scenario coding-change \
  --scenario release-readiness
```

A tool-less built-in chat accepts draft Scenarios and rejects workspace or
connector Scenarios that would be missing their declared capabilities. The
Task Workbench validates default Tools before starting. Custom chat/task
factories may accept `skills=` and optionally `scenarios=`; they remain
responsible for composing any additional Tools.

For engineering Tasks, the default Workbench Tool set includes a pure
`calculate` evaluator, bounded `analyze_csv` profiling, and approval-gated
`python_exec`. Python runs in a temporary worker with time, memory, source,
and output limits and receives no implicit project files. Choose the
Bubblewrap sandbox for an OS isolation boundary; the explicit `local` sandbox
is a trusted compatibility mode and its non-isolated result is recorded in
evidence.

Document workflows also expose bounded ZIP/TAR `inspect_archive` and
approval-gated `extract_archive` Tools. Member traversal, links/devices,
member count, and expanded size are checked before extraction.
Text extraction reports `needs_ocr` for image-only PDFs; OCR is never invoked
implicitly and must be supplied as a separately sandboxed capability.

Applications that need local RAG can use `KnowledgeStore` to ingest
already-authorized text into a durable, scope-filtered lexical index. Results
include source, chunk, and document digest citations. It is intentionally
ordinary application context (not conversation memory and not Claim
authority); an embedding/vector provider can be layered behind the same
boundary later.

For provider integrations, `fetch_url_tool(HttpClient(...))` supplies a
read-only `fetch_url` Tool. It reuses `HttpClient`'s HTTPS/host allowlist,
redirect, timeout, and response policy, then extracts bounded visible HTML or
UTF-8 text with a SHA-256 digest. Search providers such as Tavily, Exa, or
ArXiv should be separate adapters that return source URLs and citation
evidence rather than widening this generic fetch boundary.

## Python API

```python
from lipas import Agent, ScenarioRegistry

scenarios = ScenarioRegistry.from_names([
    "coding-change",
    "release-readiness",
])
skills = scenarios.skill_registry(
    paths=["skills/repository-conventions"],
)

agent = Agent.ollama(model="gemma4:12b", skills=skills)
```

Before constructing an application Agent, validate its Tool set:

```python
assessment = scenarios.require_compatible(application_tools)
```

`BusinessScenario`, `CapabilityRequirement`, `ScenarioAssessment`, and
`ScenarioRegistry` are immutable, provider-neutral public values. Scenarios
do not depend on ReAct and can be used by a custom behaviour or an external
LangGraph/AutoGen host through the normal LIPAS action boundary.

## External connector boundary

The packaged connector Scenarios are contracts, not provider integrations.
A production external write needs all of the following:

- explicit provider account, tenant, recipient, folder, queue, and object scope;
- secrets resolved outside prompts and durable evidence;
- preview plus human approval before delivery;
- stable logical-operation idempotency keys;
- provider ids stored as delivery evidence;
- data-egress and attachment policy;
- an `uncertain` state and provider reconciliation instead of blind retry.

`ActionGateway`, `OperationJournal`, durable Runs, and Effects provide the
runtime pieces. A provider package must still implement and test its own API,
scope, idempotency, lookup, and reconciliation semantics honestly.

## Adding a business package

1. Add a Skill when the job only needs method, structure, tone, or checks.
2. Add a read-only Tool when current facts must be retrieved.
3. Add a write Tool only with an explicit approval point and honest effect class.
4. Add an operation journal and reconciliation route when a provider result can be uncertain.
5. Add a Scenario to publish the minimal Skill bundle, lifecycle, and capability contract.
6. Add crash, redelivery, scope, secret, and conformance tests before calling a connector production-ready.

This model lets LIPAS grow from files and coding to office, personal, and
provider work without turning every business area into another Agent class or
a competing authority store.
