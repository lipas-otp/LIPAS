"""Dependency-free local Web operator over the existing execution authority.

The operator is intentionally a thin HTTP projection.  It reads Tasks, Runs,
Interrupts, and AgentEvents from :class:`ExecutionStore`; mutating endpoints
only call the store's existing cancellation and interrupt-resolution
transitions.  It does not queue work, keep an in-memory status copy, or create
another event sequence.

The default bind address is loopback.  Mutating requests require an explicit
operator token, even on loopback, to make accidental browser/CSRF writes fail
closed.  ``HTTPServer`` is used instead of ``ThreadingHTTPServer`` because an
ExecutionStore connection is deliberately thread-bound; applications that
need a background server should open a dedicated store in that thread.
"""
from __future__ import annotations

import hmac
import json
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, Any, Mapping, cast
from urllib.parse import parse_qs, unquote, urlsplit

from ._version import __version__
from .coordination import AgentCoordinator, CoordinationEventPage
from .execution import (
    ExecutionStateError,
    ExecutionStore,
    Interrupt,
    Run,
    Task,
)

if TYPE_CHECKING:
    from .workbench import Workbench

__all__ = ["LocalWebOperator", "OperatorServer"]


class OperatorServer(HTTPServer):
    """Typed HTTP server carrying its owning :class:`LocalWebOperator`."""

    operator: "LocalWebOperator"


class _OperatorHandler(BaseHTTPRequestHandler):
    server: OperatorServer

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        self._dispatch("POST")

    def log_message(self, _format: str, *_args: Any) -> None:
        # Local operator requests are already represented by execution events;
        # avoid writing an unbounded second access log to stderr.
        return

    def _dispatch(self, method: str) -> None:
        operator = self.server.operator
        parsed = urlsplit(self.path)
        path = tuple(unquote(part) for part in parsed.path.split("/") if part)
        query = parse_qs(parsed.query, keep_blank_values=False)
        try:
            if method == "GET":
                if path in {(), ("ui",), ("index.html",)}:
                    self._send_html(200, operator.render_ui())
                    return
                payload = operator._get(path, query)
                self._send(200, payload)
                return
            if not operator._authorized(self.headers.get("Authorization")):
                self._send(401, {"error": "operator authorization required"})
                return
            body = self._read_json()
            payload = operator._post(path, body)
            self._send(200, payload)
        except (BrokenPipeError, ConnectionResetError):
            # A browser/tab may disconnect while a bounded projection is
            # being written.  There is no second response to send and this is
            # not an execution failure.
            return
        except KeyError as exc:
            self._send(404, {"error": "not found", "detail": str(exc)})
        except ValueError as exc:
            self._send(400, {"error": "invalid request", "detail": str(exc)})
        except PermissionError as exc:
            self._send(403, {"error": "forbidden", "detail": str(exc)})
        except ExecutionStateError as exc:
            # A stale approval/cancel request is a client conflict, not an
            # operator-server failure.  Keeping this distinction makes a UI
            # refresh and retry safe without treating the database as broken.
            self._send(409, {"error": "state conflict", "detail": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive HTTP boundary
            self._send(500, {"error": "operator failure", "detail": type(exc).__name__})

    def _read_json(self) -> Mapping[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("POST requires Content-Length")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Content-Length must be an integer") from exc
        operator = self.server.operator
        if length < 0 or length > operator.max_body_bytes:
            raise ValueError("request body exceeds operator limit")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body must be UTF-8 JSON") from exc
        if not isinstance(value, Mapping):
            raise ValueError("request body must be a JSON object")
        return cast(Mapping[str, Any], value)

    def _send(self, status: int, payload: Mapping[str, Any]) -> None:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _send_html(self, status: int, payload: str) -> None:
        encoded = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)


class LocalWebOperator:
    """A small local HTTP operator for Tasks, Runs, events, and interrupts.

    ``execution`` may be the Runtime-owned store or a standalone
    ``ExecutionStore``.  ``coordinator`` is optional and only enables the
    aggregate coordination-event route; it must borrow the same store.
    """

    def __init__(
        self,
        execution: ExecutionStore,
        *,
        workbench: "Workbench | None" = None,
        operations: Any | None = None,
        coordinator: AgentCoordinator | None = None,
        operator_token: str | None = None,
        max_body_bytes: int = 64 * 1024,
        max_items: int = 1_000,
    ) -> None:
        if not isinstance(execution, ExecutionStore):
            raise TypeError("execution must be an ExecutionStore")
        if workbench is not None and getattr(workbench, "execution", None) is not execution:
            raise ValueError("workbench must borrow the supplied execution store")
        if coordinator is not None and coordinator.execution is not execution:
            raise ValueError("coordinator must borrow the supplied execution store")
        if operator_token is not None and (
            not isinstance(operator_token, str) or not operator_token
        ):
            raise ValueError("operator_token must be a non-empty string or None")
        if (
            isinstance(max_body_bytes, bool)
            or not isinstance(max_body_bytes, int)
            or max_body_bytes < 1
        ):
            raise ValueError("max_body_bytes must be a positive int")
        if (
            isinstance(max_items, bool)
            or not isinstance(max_items, int)
            or max_items < 1
            or max_items > 10_000
        ):
            raise ValueError("max_items must be between 1 and 10000")
        self.execution = execution
        self.workbench = workbench
        self.operations = operations
        self.coordinator = coordinator
        self.operator_token = operator_token
        self.max_body_bytes = max_body_bytes
        self.max_items = max_items
        self._server: OperatorServer | None = None

    @property
    def server(self) -> OperatorServer | None:
        """The active server, if :meth:`serve_forever` was started."""
        return self._server

    def snapshot(self) -> dict[str, Any]:
        """Return a bounded JSON projection suitable for a dashboard."""
        return self._get(("api", "snapshot"), {})

    def render_ui(self) -> str:
        """Return a dependency-free browser projection for local operation.

        The page polls the reconnectable JSON routes and never stores a
        second execution state. Mutations remain explicit API calls protected
        by the bearer-token boundary; the token field is only kept in the
        browser tab's session storage.
        """
        return """<!doctype html>
<html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LIPAS Local Operator</title>
<style>body{font:15px system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;background:#f7f7f8;color:#202124}pre{white-space:pre-wrap;background:#fff;border:1px solid #ddd;padding:1rem;border-radius:8px}button{padding:.4rem .7rem;margin:.2rem}small{color:#666}</style>
<h1>LIPAS Local Operator</h1>
<p><small>Projection only. State and recovery remain owned by ExecutionStore.</small></p>
<label>Operator token <input id="token" type="password" autocomplete="off"></label>
<button onclick="refresh()">Refresh</button>
<div id="controls"></div>
<pre id="view">Loading…</pre>
<script>
const view=document.getElementById('view'), controls=document.getElementById('controls'), token=document.getElementById('token');
token.value=sessionStorage.getItem('lipas.operator.token')||'';
token.onchange=()=>sessionStorage.setItem('lipas.operator.token',token.value);
async function mutate(path,body={}){const r=await fetch(path,{method:'POST',headers:{'Authorization':'Bearer '+token.value,'Content-Type':'application/json'},body:JSON.stringify(body)});if(!r.ok) alert((await r.json()).detail||r.status);await refresh()}
function button(label,path,body={}){const b=document.createElement('button');b.textContent=label;b.onclick=()=>mutate(path,body);return b}
function operationButton(label,op,found){const b=document.createElement('button');b.textContent=label;b.onclick=()=>{const observation=prompt('How was the provider outcome checked?');if(!observation)return;const body={found:found,observation:observation};if(found){const reference=prompt('Provider reference');if(!reference)return;body.provider_reference=reference;body.result={operator:'browser'}}mutate('/api/operations/'+encodeURIComponent(op.key)+'/reconcile',body)};return b}
function controlsFor(data){controls.replaceChildren();for(const t of (data.tasks||[])){if(t.state==='open')controls.append(button('Cancel task '+t.id.slice(0,8),'/api/tasks/'+encodeURIComponent(t.id)+'/cancel'))}for(const r of (data.runs||[])){if(['pending','running','waiting'].includes(r.state))controls.append(button('Cancel run '+r.id.slice(0,8),'/api/runs/'+encodeURIComponent(r.id)+'/cancel'));if(r.recovery_required)controls.append(button('Reopen uncertain '+r.id.slice(0,8),'/api/runs/'+encodeURIComponent(r.id)+'/reopen',{acknowledge_uncertain:true,reconciled:true,evidence:{source:'operator_ui',observation:'Operator confirmed the external Effect/provider outcome and completed the required reconciliation.'}}))}for(const i of (data.pending_interrupts||[])){if(i.state==='pending'){controls.append(button('Approve '+i.id.slice(0,8),'/api/interrupts/'+encodeURIComponent(i.id)+'/approve'));controls.append(button('Deny '+i.id.slice(0,8),'/api/interrupts/'+encodeURIComponent(i.id)+'/deny'))}}for(const op of (data.operations||[])){if(op.state==='uncertain'){controls.append(operationButton('Reconcile delivered '+op.key.slice(0,12),op,true));controls.append(operationButton('Reconcile absent '+op.key.slice(0,12),op,false))}}}
async function refresh(){try{const r=await fetch('/api/snapshot',{cache:'no-store'}),data=await r.json();controlsFor(data);view.textContent=JSON.stringify(data,null,2)}catch(e){view.textContent=String(e)}}
refresh(); setInterval(refresh,2000);
</script>
"""

    def make_server(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> OperatorServer:
        if not isinstance(host, str) or not host.strip():
            raise ValueError("host must be a non-empty string")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("port must be an integer between 0 and 65535")
        if self._server is not None:
            raise RuntimeError("operator server is already active")
        server = OperatorServer((host, port), _OperatorHandler)
        server.operator = self
        self._server = server
        return server

    def serve_forever(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        poll_interval: float = 0.5,
    ) -> None:
        server = self.make_server(host, port)
        try:
            server.serve_forever(poll_interval=poll_interval)
        finally:
            self.close()

    def close(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.server_close()

    def shutdown(self) -> None:
        """Ask an active ``serve_forever`` loop to stop from another thread.

        ``HTTPServer.shutdown`` must not be called by the serving thread
        itself; callers running the operator in a background thread should
        use this method and let ``serve_forever`` perform final cleanup.
        """
        server = self._server
        if server is not None:
            server.shutdown()

    def __enter__(self) -> "LocalWebOperator":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _authorized(self, header: str | None) -> bool:
        if self.operator_token is None or not isinstance(header, str):
            return False
        scheme, _, value = header.partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(
            value, self.operator_token,
        )

    def _get(self, path: tuple[str, ...], query: Mapping[str, list[str]]) -> dict[str, Any]:
        if path == ("health",) or path == ("api", "health"):
            return {
                "ok": True,
                "version": __version__,
                "schema_version": self.execution.schema_version,
            }
        if path in {("api", "snapshot"), ("api", "tasks")}:
            tasks = tuple(self.execution.list_tasks())
            if path == ("api", "tasks"):
                return {
                    "tasks": [_task_json(task) for task in tasks[:self.max_items]],
                    "truncated": len(tasks) > self.max_items,
                }
            runs = tuple(self.execution.list_runs())
            pending = tuple(
                interrupt
                for interrupt in self.execution.list_interrupts()
                if interrupt.state.value == "pending"
            )
            return {
                "version": __version__,
                "tasks": [_task_json(task) for task in tasks[:self.max_items]],
                "runs": [_run_json(run) for run in runs[:self.max_items]],
                "pending_interrupts": [
                    _interrupt_json(interrupt)
                    for interrupt in pending[:self.max_items]
                ],
                "approvals": [
                    _approval_json(interrupt)
                    for interrupt in pending
                    if interrupt.kind == "approval"
                ][:self.max_items],
                "operations": self._operations_json(),
                "truncated": (
                    len(tasks) > self.max_items
                    or len(runs) > self.max_items
                    or len(pending) > self.max_items
                ),
                "evidence": _execution_evidence(
                    self.execution, runs, pending_interrupts=pending,
                    operations=self.operations,
                ),
            }
        if len(path) == 3 and path[:2] == ("api", "tasks"):
            task_id = path[2]
            task = self.execution.get_task(task_id)
            if task is None:
                raise KeyError(task_id)
            runs = tuple(self.execution.list_runs(task_id=task_id))
            run_ids = {run.id for run in runs}
            interrupts = tuple(
                item for item in self.execution.list_interrupts()
                if item.run_id in run_ids
            )
            details: dict[str, Any] = {
                "task": _task_json(task),
                "runs": [_run_json(run) for run in runs[:self.max_items]],
                "interrupts": [
                    _interrupt_json(item) for item in interrupts[:self.max_items]
                ],
                "events": [
                    _event_json(event)
                    for run in runs[:self.max_items]
                    for event in self.execution.agent_events(
                        run.id, limit=self.max_items + 1,
                    )
                ][:self.max_items],
                "truncated": (
                    len(runs) > self.max_items
                    or len(interrupts) > self.max_items
                ),
            }
            details["evidence"] = _execution_evidence(
                self.execution, runs,
                pending_interrupts=tuple(
                    item for item in interrupts
                    if item.state.value == "pending"
                ),
                workbench=self.workbench,
                task_id=task_id,
                coordinator=self.coordinator,
                operations=self.operations,
            )
            if self.workbench is not None:
                details["workbench"] = _workbench_task_json(
                    self.workbench, task_id, max_items=self.max_items,
                )
            return details
        if path == ("api", "runs"):
            runs = tuple(self.execution.list_runs())
            return {
                "runs": [_run_json(run) for run in runs[:self.max_items]],
                "truncated": len(runs) > self.max_items,
            }
        if path == ("api", "interrupts"):
            state = _query_one(query, "state")
            interrupts = self.execution.list_interrupts()
            if state is not None:
                interrupts = tuple(
                    item for item in interrupts if item.state.value == state
                )
            return {
                "interrupts": [
                    _interrupt_json(item) for item in interrupts[:self.max_items]
                ],
                "truncated": len(interrupts) > self.max_items,
            }
        if path == ("api", "approvals"):
            interrupts = tuple(
                item for item in self.execution.list_interrupts()
                if item.kind == "approval"
                and item.state.value == (_query_one(query, "state") or "pending")
            )
            return {
                "approvals": [
                    _approval_json(item) for item in interrupts[:self.max_items]
                ],
                "truncated": len(interrupts) > self.max_items,
            }
        if path == ("api", "operations"):
            return {"operations": self._operations_json()}
        if len(path) == 3 and path[:2] == ("api", "runs"):
            run_id = path[2]
            run = self.execution.get_run(run_id)
            if run is None:
                raise KeyError(run_id)
            after = _query_int(query, "after", default=0)
            limit = _query_int(query, "limit", default=100)
            if limit < 1 or limit > 1_000:
                raise ValueError("limit must be between 1 and 1000")
            events = self.execution.agent_events(
                run_id, after=after, limit=limit + 1,
            )
            has_more = len(events) > limit
            events = events[:limit]
            return {
                "run": _run_json(run),
                "interrupts": [
                    _interrupt_json(item)
                    for item in self.execution.list_interrupts(run_id=run_id)
                ],
                "events": [_event_json(event) for event in events],
                "next_cursor": events[-1].sequence if events else after,
                "has_more": has_more,
                "evidence": _execution_evidence(
                    self.execution, (run,),
                    pending_interrupts=tuple(
                        self.execution.list_interrupts(
                            run_id=run_id,
                        )
                    ),
                    workbench=self.workbench,
                    task_id=run.task_id,
                    coordinator=self.coordinator,
                    operations=self.operations,
                ),
            }
        if (
            len(path) == 4
            and path[:2] == ("api", "runs")
            and path[3] == "events"
        ):
            run_id = path[2]
            run = self.execution.get_run(run_id)
            if run is None:
                raise KeyError(run_id)
            after = _query_int(query, "after", default=0)
            limit = _query_int(query, "limit", default=100)
            if limit < 1 or limit > 1_000:
                raise ValueError("limit must be between 1 and 1000")
            events = self.execution.agent_events(
                run_id, after=after, limit=limit + 1,
            )
            has_more = len(events) > limit
            events = events[:limit]
            return {
                "run_id": run_id,
                "events": [_event_json(event) for event in events],
                "next_cursor": events[-1].sequence if events else after,
                "has_more": has_more,
            }
        if (
            len(path) == 4
            and path[:2] == ("api", "coordination")
            and path[3] == "events"
        ):
            if self.coordinator is None:
                raise KeyError("coordination event projection is not configured")
            cursor = _query_one(query, "after")
            limit = _query_int(query, "limit", default=100)
            if limit < 1 or limit > 1_000:
                raise ValueError("limit must be between 1 and 1000")
            return _coordination_page_json(
                self.coordinator.event_handle(path[2]).read(
                    after=cursor,
                    limit=limit,
                ),
            )
        raise KeyError("/" + "/".join(path))

    def _post(self, path: tuple[str, ...], body: Mapping[str, Any]) -> dict[str, Any]:
        if len(path) == 4 and path[:2] == ("api", "tasks") and path[3] == "cancel":
            task = self.execution.get_task(path[2])
            if task is None:
                raise KeyError(path[2])
            return {"task": _task_json(self.execution.cancel_task(path[2]))}
        if len(path) == 4 and path[:2] == ("api", "runs") and path[3] == "cancel":
            run = self.execution.get_run(path[2])
            if run is None:
                raise KeyError(path[2])
            return {"run": _run_json(self.execution.request_cancel(path[2]))}
        if len(path) == 4 and path[:2] == ("api", "runs") and path[3] == "reopen":
            if body.get("acknowledge_uncertain") is not True:
                raise ValueError("acknowledge_uncertain=true is required")
            if body.get("reconciled") is not True:
                raise ValueError(
                    "reconciled=true is required after the Effect/provider outcome "
                    "has been checked",
                )
            evidence = body.get("evidence")
            if not isinstance(evidence, Mapping):
                raise ValueError(
                    "evidence must be an object with an observation before reopening",
                )
            return {
                "run": _run_json(
                    self.execution.reopen_uncertain(path[2], evidence=evidence),
                ),
            }
        if (
            len(path) == 4
            and path[:2] == ("api", "interrupts")
            and path[3] in {"resolve", "approve", "deny"}
        ):
            allow = body.get("allow")
            if path[3] == "approve":
                allow = True
            elif path[3] == "deny":
                allow = False
            if not isinstance(allow, bool):
                raise ValueError("allow must be a boolean (or use /approve or /deny)")
            interrupt = self.execution.resolve_interrupt(
                path[2],
                allow=allow,
                response=body.get("response"),
            )
            return {"interrupt": _interrupt_json(interrupt)}
        if (
            len(path) == 4
            and path[:2] == ("api", "operations")
            and path[3] == "reconcile"
        ):
            if self.operations is None:
                raise KeyError("operation journal is not configured")
            found = body.get("found")
            if not isinstance(found, bool):
                raise ValueError("found must be a boolean")
            observation = body.get("observation")
            if not isinstance(observation, str) or not observation.strip():
                raise ValueError(
                    "observation must explain how the provider outcome was checked",
                )
            if found:
                provider_reference = body.get("provider_reference")
                if (
                    not isinstance(provider_reference, str)
                    or not provider_reference.strip()
                ):
                    raise ValueError(
                        "provider_reference is required when found=true",
                    )
            operation = self.operations.reconcile(
                path[2],
                lambda _key: (
                    found,
                    body.get("result"),
                    body.get("provider_reference"),
                ),
                observation=observation,
            )
            return {"operation": _operation_json(operation)}
        raise KeyError("/" + "/".join(path))

    def _operations_json(self) -> list[dict[str, Any]]:
        if self.operations is None:
            return []
        return [
            _operation_json(value)
            for value in self.operations.pending()[:self.max_items]
        ]


def _query_one(query: Mapping[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    return values[0] if values else None


def _query_int(query: Mapping[str, list[str]], name: str, *, default: int) -> int:
    raw = _query_one(query, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _task_json(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "goal": task.goal,
        "workspace": task.workspace,
        "state": task.state.value,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def _run_json(run: Run) -> dict[str, Any]:
    # Lease tokens are ownership secrets and must never be sent to a browser.
    return {
        "id": run.id,
        "task_id": run.task_id,
        "state": run.state.value,
        "attempt": run.attempt,
        "checkpoint_version": run.checkpoint_version,
        "lease_expires": run.lease_expires,
        "cancel_requested": run.cancel_requested,
        "result": _wire(run.result),
        "error": _wire(run.error),
        "recovery_required": bool(
            isinstance(run.error, Mapping)
            and run.error.get("recovery_required") is True
        ),
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


def _interrupt_json(interrupt: Interrupt) -> dict[str, Any]:
    return {
        "id": interrupt.id,
        "run_id": interrupt.run_id,
        "kind": interrupt.kind,
        "request": _wire(interrupt.request),
        "state": interrupt.state.value,
        "response": _wire(interrupt.response),
        "created_at": interrupt.created_at,
        "resolved_at": interrupt.resolved_at,
    }


def _approval_json(interrupt: Interrupt) -> dict[str, Any]:
    payload = _interrupt_json(interrupt)
    request = interrupt.request
    payload["risk"] = {
        "side_effect": request.get("side_effect", request.get("declared_side_effect")),
        "tool": request.get("tool_name", request.get("tool")),
        "external": request.get("side_effect") == "external_write",
        "preview": _wire(request.get("preview", request.get("diff"))),
        "budget": _wire(request.get("budget", request.get("estimate"))),
        "scope": _wire(request.get("scope")),
    }
    return payload


def _operation_json(operation: Any) -> dict[str, Any]:
    return {
        "key": operation.key,
        "kind": operation.kind,
        "state": operation.state,
        "request": _wire(operation.request),
        "result": _wire(operation.result),
        "provider_reference": operation.provider_reference,
        "provider_request_id": operation.provider_request_id,
        "error": _wire(operation.error),
        "effect_id": operation.effect_id,
    }


def _event_json(event: Any) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "run_id": event.run_id,
        "sequence": event.sequence,
        "type": event.type,
        "iteration": event.iteration,
        "data": _wire(event.data),
        "created_at": event.created_at,
    }


def _workbench_task_json(
    workbench: "Workbench",
    task_id: str,
    *,
    max_items: int,
) -> dict[str, Any]:
    """Project product evidence without making it a second authority."""
    change_set = workbench.change_set(task_id)
    events = workbench.events(task_id)
    artifacts = workbench.artifacts(task_id)
    return {
        "events": [
            {
                "id": event.id,
                "run_id": event.run_id,
                "kind": event.kind,
                "data": _wire(event.data),
                "created_at": event.created_at,
            }
            for event in events[:max_items]
        ],
        "artifacts": [
            {
                "id": artifact.id,
                "run_id": artifact.run_id,
                "kind": artifact.kind,
                "path": artifact.path,
                "sha256": artifact.sha256,
                "metadata": _wire(artifact.metadata),
                "created_at": artifact.created_at,
            }
            for artifact in artifacts[:max_items]
        ],
        "change_set": None if change_set is None else {
            "task_id": change_set.task_id,
            "run_id": change_set.run_id,
            "state": change_set.state,
            "changed_paths": list(workbench.change_set_paths(task_id)),
            "diff": _wire(workbench.change_set_diff(task_id))
            if change_set.state != "discarded" else None,
            "created_at": change_set.created_at,
            "updated_at": change_set.updated_at,
        },
        "report": _wire(workbench.get_report(task_id)),
        "truncated": len(events) > max_items or len(artifacts) > max_items,
    }


def _coordination_page_json(page: CoordinationEventPage) -> dict[str, Any]:
    return {
        "events": [
            {
                "coordination_id": event.coordination_id,
                "cursor": event.cursor,
                "event": _event_json(event.event),
            }
            for event in page.events
        ],
        "next_cursor": page.next_cursor,
        "has_more": page.has_more,
    }


def _execution_evidence(
    execution: ExecutionStore,
    runs: tuple[Run, ...],
    *,
    pending_interrupts: tuple[Interrupt, ...],
    workbench: "Workbench | None" = None,
    task_id: str | None = None,
    coordinator: AgentCoordinator | None = None,
    operations: Any | None = None,
) -> dict[str, Any]:
    """Return a compact safety/completion projection for an operator UI.

    This is intentionally derived on demand from the execution/workbench
    authorities.  It is not a metrics store and never infers success from a
    missing record: an expired lease is explicitly surfaced as an orphan and
    a missing verification report remains unknown.
    """
    event_counts: Counter[str] = Counter()
    event_count = 0
    orphan_runs = 0
    uncertain_runs = 0
    now = time.time()
    for run in runs:
        if (
            run.state.value == "running"
            and run.lease_expires is not None
            and run.lease_expires <= now
        ):
            orphan_runs += 1
        if run.state.value == "failed" and isinstance(run.error, Mapping):
            error_type = str(run.error.get("type", "")).lower()
            if (
                "uncertain" in error_type
                or "orphan" in error_type
                or run.error.get("recovery_required") is True
            ):
                uncertain_runs += 1
        try:
            events = () if run.id == "" else execution.agent_events(
                run.id, limit=10_000,
            )
        except (KeyError, ValueError):
            events = ()
        event_count += len(events)
        event_counts.update(event.type for event in events)

    report_data: Mapping[str, Any] | None = None
    if workbench is not None and task_id is not None:
        report = workbench.get_report(task_id)
        if report is not None:
            report_data = report
            risks = report_data.get("unresolved_risks") or ()
            if any("uncertain" in str(value).lower() for value in risks):
                uncertain_runs += 1

    budget = None
    if coordinator is not None:
        budget = coordinator.budget_snapshot()
    pending_operations = 0
    uncertain_operations = 0
    if operations is not None:
        for operation in operations.pending():
            if operation.state == "uncertain":
                uncertain_operations += 1
            elif operation.state == "pending":
                pending_operations += 1
    return {
        "pending_approvals": sum(
            item.kind == "approval"
            for item in pending_interrupts
            if item.state.value == "pending"
        ),
        "pending_inputs": sum(
            item.kind != "approval"
            for item in pending_interrupts
            if item.state.value == "pending"
        ),
        "orphan_runs": orphan_runs,
        "uncertain_runs": uncertain_runs,
        "pending_operations": pending_operations,
        "uncertain_operations": uncertain_operations,
        "event_count": event_count,
        "event_types": dict(sorted(event_counts.items())),
        "budget": _wire(budget),
        "verification": None if report_data is None else {
            "verified": bool(report_data.get("verified", False)),
            "count": len(report_data.get("verifications") or ()),
        },
        "risks": [] if report_data is None else list(
            report_data.get("unresolved_risks") or (),
        ),
    }


def _wire(value: Any) -> Any:
    """Keep the HTTP boundary JSON-only without leaking object repr secrets."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _wire(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_wire(item) for item in value]
    return {"type": type(value).__name__}
