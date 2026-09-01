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
import base64
import binascii
import hashlib
import json
import math
import secrets
import ipaddress
import socket
import ssl
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, cast
from urllib.parse import parse_qs, unquote, urlsplit

from ._version import __version__
from .coordination import AgentCoordinator, CoordinationEventPage
from .conversation_store import (
    Conversation,
    ConversationEventPage,
    Message,
    SessionConflictError,
    SQLiteSessionStore,
)
from .execution import (
    ExecutionStateError,
    ExecutionStore,
    Interrupt,
    Run,
    Task,
)
from .security import TLSConfig
from .performance import measure_execution, project_cost_ledger, project_incidents
from .deployment import release_check

if TYPE_CHECKING:
    from .workbench import Workbench

__all__ = ["LocalWebOperator", "OperatorServer", "OperatorAuthenticator"]


class OperatorAuthenticator:
    """Stateless HMAC bearer tokens with explicit subject and expiry."""

    def __init__(self, secret: bytes | str, *, issuer: str = "lipas", ttl_s: float = 3600.0) -> None:
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        if not isinstance(secret, bytes) or len(secret) < 16:
            raise ValueError("operator auth secret must contain at least 16 bytes")
        if not isinstance(issuer, str) or not issuer.strip():
            raise ValueError("issuer must be non-empty")
        try:
            valid_ttl = (
                not isinstance(ttl_s, bool)
                and isinstance(ttl_s, (int, float))
                and math.isfinite(float(ttl_s))
                and ttl_s >= 1
            )
        except (OverflowError, TypeError, ValueError):
            valid_ttl = False
        if not valid_ttl:
            raise ValueError("ttl_s must be finite and at least one second")
        self._secret = secret
        self.issuer = issuer.strip()
        self.ttl_s = float(ttl_s)

    def issue(self, subject: str, *, ttl_s: float | None = None, now: int | None = None) -> str:
        if not isinstance(subject, str) or not subject.strip():
            raise ValueError("subject must be non-empty")
        if now is not None and (isinstance(now, bool) or not isinstance(now, int)):
            raise TypeError("now must be an int or None")
        issued = int(time.time() if now is None else now)
        if ttl_s is not None and isinstance(ttl_s, bool):
            raise TypeError("ttl_s must be a finite number")
        try:
            ttl = self.ttl_s if ttl_s is None else float(ttl_s)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("ttl_s must be finite and at least one second") from exc
        if not math.isfinite(ttl) or ttl < 1:
            raise ValueError("ttl_s must be finite and at least one second")
        payload = {"iss": self.issuer, "sub": subject.strip(), "iat": issued, "exp": issued + int(ttl), "jti": secrets.token_urlsafe(12)}
        encoded = _b64json(payload)
        signature = _b64(hmac.new(self._secret, encoded.encode(), hashlib.sha256).digest())
        return f"{encoded}.{signature}"

    def verify(self, token: str, *, now: int | None = None) -> str | None:
        if not isinstance(token, str) or token.count(".") != 1:
            return None
        encoded, signature = token.split(".", 1)
        expected = _b64(hmac.new(self._secret, encoded.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        try:
            payload = json.loads(
                _unb64(encoded).decode("utf-8"),
                parse_constant=lambda raw: (_ for _ in ()).throw(
                    ValueError(f"non-JSON numeric constant {raw!r}")
                ),
            )
        except (ValueError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError):
            return None
        if now is not None and (isinstance(now, bool) or not isinstance(now, int)):
            return None
        current = int(time.time() if now is None else now)
        if not isinstance(payload, Mapping) or payload.get("iss") != self.issuer:
            return None
        if not isinstance(payload.get("sub"), str) or not payload["sub"]:
            return None
        issued = payload.get("iat")
        expires = payload.get("exp")
        if (
            not isinstance(issued, int)
            or isinstance(issued, bool)
            or not isinstance(expires, int)
            or isinstance(expires, bool)
            or expires <= issued
            or current < issued - 30
            or current >= expires
        ):
            return None
        return payload["sub"]


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _b64json(value: Mapping[str, Any]) -> str:
    return _b64(
        json.dumps(
            dict(value),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    )


def _operator_host_is_non_loopback(host: str) -> bool:
    """Return whether a bind host can expose the operator beyond this host."""
    normalized = host.strip().lower().strip("[]")
    if normalized in {"localhost", "ip6-localhost"}:
        return False
    try:
        return not ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        # DNS names cannot be proven loopback without a network lookup.  A
        # production bind therefore treats them as externally reachable.
        return True


def _tls_context(
    value: TLSConfig | ssl.SSLContext | None,
    *,
    server: bool,
) -> ssl.SSLContext | None:
    if value is None:
        return None
    if isinstance(value, TLSConfig):
        return value.server_context() if server else value.client_context()
    if not isinstance(value, ssl.SSLContext):
        raise TypeError("tls must be TLSConfig, ssl.SSLContext, or None")
    return value


class OperatorServer(HTTPServer):
    """Typed HTTP server carrying its owning :class:`LocalWebOperator`."""

    operator: "LocalWebOperator"
    tls_enabled: bool = False
    tls_context: ssl.SSLContext | None = None
    _tls_lock: Any = None

    def get_request(self) -> tuple[socket.socket, Any]:  # noqa: D401 - stdlib hook
        """Accept one connection using the context current at accept time."""
        request, client_address = super().get_request()
        lock = getattr(self, "_tls_lock", None)
        if lock is None:
            context = self.tls_context
        else:
            with lock:
                context = self.tls_context
        if context is None:
            return request, client_address
        try:
            return context.wrap_socket(request, server_side=True), client_address
        except BaseException:
            request.close()
            raise

    def reload_tls(self, tls: TLSConfig | ssl.SSLContext) -> None:
        """Atomically switch certificates for future connections.

        Existing connections keep their negotiated session; newly accepted
        sockets use the new context.  The listening socket is never replaced,
        so the bound port and in-flight requests remain stable.
        """
        context = _tls_context(tls, server=True)
        if context is None:  # defensive; the public type excludes None
            raise ValueError("tls material is required for reload")
        lock = getattr(self, "_tls_lock", None)
        if lock is None:
            self.tls_context = context
            self.tls_enabled = True
        else:
            with lock:
                self.tls_context = context
                self.tls_enabled = True


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
                if path and path[-1] == "stream":
                    access_token = _query_one(query, "access_token")
                    authorized = operator._authorized(self.headers.get("Authorization"))
                    if access_token is not None:
                        authorized = authorized or operator._authorized(
                            "Bearer " + access_token,
                        )
                    if operator.require_authentication and not authorized:
                        self._send(401, {"error": "operator authorization required"})
                        return
                    if "after" not in query:
                        last_event = self.headers.get("Last-Event-ID")
                        if last_event:
                            query = {**query, "after": [last_event]}
                    self._send_sse(operator._sse(path, query))
                    return
                # Health probes remain public when authentication is enabled;
                # all data projections still require an operator credential.
                public_health = path in {
                    ("health",), ("api", "health"), ("ready",), ("api", "ready"),
                }
                if (
                    operator.require_authentication
                    and not public_health
                    and not operator._authorized(self.headers.get("Authorization"))
                ):
                    self._send(401, {"error": "operator authorization required"})
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
        except SessionConflictError as exc:
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
            value = (
                json.loads(
                    raw.decode("utf-8"),
                    parse_constant=lambda raw: (_ for _ in ()).throw(
                        ValueError(f"non-JSON numeric constant {raw!r}")
                    ),
                )
                if raw else {}
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
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
        if self.server.tls_enabled:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        self.end_headers()
        self.wfile.write(encoded)

    def _send_html(self, status: int, payload: str) -> None:
        encoded = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        if self.server.tls_enabled:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        self.end_headers()
        self.wfile.write(encoded)

    def _send_sse(self, events: tuple[tuple[str, str, str], ...]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        if self.server.tls_enabled:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        # This endpoint emits one bounded catch-up batch.  Closing the
        # response lets EventSource reconnect and resume from Last-Event-ID
        # without retaining a server-side socket indefinitely.
        self.send_header("Connection", "close")
        self.end_headers()
        if not events:
            self.wfile.write(b": heartbeat\n\n")
            self.wfile.flush()
            return
        for event_id, event_type, data in events:
            self.wfile.write(f"id: {event_id}\nevent: {event_type}\ndata: {data}\n\n".encode("utf-8"))
        self.wfile.flush()


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
        sessions: SQLiteSessionStore | None = None,
        conversation_workspace: str | Path | None = None,
        conversation_event_reader: Any | None = None,
        promote_message: Any | None = None,
        coordinator: AgentCoordinator | None = None,
        operator_token: str | None = None,
        authenticator: OperatorAuthenticator | None = None,
        require_authentication: bool = False,
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
            not isinstance(operator_token, str) or not operator_token.strip()
        ):
            raise ValueError("operator_token must be a non-empty string or None")
        if authenticator is not None and not isinstance(authenticator, OperatorAuthenticator):
            raise TypeError("authenticator must be OperatorAuthenticator or None")
        if not isinstance(require_authentication, bool):
            raise TypeError("require_authentication must be bool")
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
        self.sessions = sessions
        self.conversation_workspace = conversation_workspace
        self.conversation_event_reader = conversation_event_reader
        self.promote_message = promote_message
        self.coordinator = coordinator
        self.operator_token = operator_token.strip() if operator_token is not None else None
        self.authenticator = authenticator
        self.require_authentication = require_authentication
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

        The page uses cursor-based SSE when available and bounded polling as a
        fallback; it never stores a second execution state. Mutations remain
        explicit API calls protected by the bearer-token boundary; the token
        field is only kept in the browser tab's session storage.
        """
        return """<!doctype html>
<html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LIPAS Local Operator</title>
<style>body{font:15px system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;background:#f7f7f8;color:#202124}pre{white-space:pre-wrap;background:#fff;border:1px solid #ddd;padding:1rem;border-radius:8px}button{padding:.4rem .7rem;margin:.2rem}small{color:#666}</style>
<h1>LIPAS Local Operator</h1>
<p><small>Conversation is the front door; Task/Run/Effect remain the execution authority.</small></p>
<label>Operator token <input id="token" type="password" autocomplete="off"></label>
<button onclick="refresh()">Refresh</button>
<section><h2>Conversation</h2><select id="conversation"></select><input id="title" placeholder="New conversation title"><button onclick="newConversation()">New</button><br><textarea id="composer" rows="3" cols="70" placeholder="Ask or describe work…"></textarea><br><button onclick="sendMessage()">Send message</button><button onclick="promoteMessage()">Promote last message to Task</button><br><input id="attachment" type="file"><button onclick="uploadAttachment()">Upload attachment</button><div id="messages"></div></section>
<div id="controls"></div>
<pre id="view">Loading…</pre>
<script>
const view=document.getElementById('view'), controls=document.getElementById('controls'), token=document.getElementById('token'), conversation=document.getElementById('conversation'), messages=document.getElementById('messages');
token.value=sessionStorage.getItem('lipas.operator.token')||'';
token.onchange=()=>sessionStorage.setItem('lipas.operator.token',token.value);
async function mutate(path,body={}){const r=await fetch(path,{method:'POST',headers:{'Authorization':'Bearer '+token.value,'Content-Type':'application/json'},body:JSON.stringify(body)});let d={};try{d=await r.json()}catch(_){}if(!r.ok){alert(d.detail||d.error||r.status);return false}await refresh();return true}
function currentConversation(){return conversation.value}
async function newConversation(){const title=document.getElementById('title').value||'New conversation';await mutate('/api/conversations',{title:title})}
async function sendMessage(){const text=document.getElementById('composer').value.trim();if(!text||!currentConversation())return;await mutate('/api/conversations/'+encodeURIComponent(currentConversation())+'/messages',{role:'user',content:text});document.getElementById('composer').value=''}
async function promoteMessage(){if(!currentConversation())return;const r=await fetch('/api/conversations/'+encodeURIComponent(currentConversation()),{cache:'no-store'}),d=await r.json(),m=(d.messages||[]).filter(x=>x.role==='user').pop();if(m)await mutate('/api/conversations/'+encodeURIComponent(currentConversation())+'/messages/'+encodeURIComponent(m.id)+'/promote',{})}
async function uploadAttachment(){const input=document.getElementById('attachment'),file=input.files[0];if(!file||!currentConversation())return;if(file.size>44*1024){alert('Attachment exceeds the local operator body limit');return}const bytes=new Uint8Array(await file.arrayBuffer());let binary='';for(const byte of bytes)binary+=String.fromCharCode(byte);await mutate('/api/conversations/'+encodeURIComponent(currentConversation())+'/attachments',{filename:file.name,mime_type:file.type||'application/octet-stream',content_base64:btoa(binary)});input.value=''}
function button(label,path,body={}){const b=document.createElement('button');b.textContent=label;b.onclick=()=>mutate(path,body);return b}
function operationButton(label,op,found){const b=document.createElement('button');b.textContent=label;b.onclick=()=>{const observation=prompt('How was the provider outcome checked?');if(!observation)return;const body={found:found,observation:observation};if(found){const reference=prompt('Provider reference');if(!reference)return;body.provider_reference=reference;body.result={operator:'browser'}}mutate('/api/operations/'+encodeURIComponent(op.key)+'/reconcile',body)};return b}
function controlsFor(data){controls.replaceChildren();for(const t of (data.tasks||[])){if(t.state==='open')controls.append(button('Cancel task '+t.id.slice(0,8),'/api/tasks/'+encodeURIComponent(t.id)+'/cancel'))}for(const r of (data.runs||[])){if(['pending','running','waiting'].includes(r.state))controls.append(button('Cancel run '+r.id.slice(0,8),'/api/runs/'+encodeURIComponent(r.id)+'/cancel'));if(r.recovery_required)controls.append(button('Reopen uncertain '+r.id.slice(0,8),'/api/runs/'+encodeURIComponent(r.id)+'/reopen',{acknowledge_uncertain:true,reconciled:true,evidence:{source:'operator_ui',observation:'Operator confirmed the external Effect/provider outcome and completed the required reconciliation.'}}))}for(const i of (data.pending_interrupts||[])){if(i.state!=='pending')continue;if(i.kind==='approval'){controls.append(button('Approve '+i.id.slice(0,8),'/api/interrupts/'+encodeURIComponent(i.id)+'/approve'));controls.append(button('Deny '+i.id.slice(0,8),'/api/interrupts/'+encodeURIComponent(i.id)+'/deny'))}else{const b=button('Answer input '+i.id.slice(0,8),'#');b.onclick=()=>{const response=prompt('Provide the missing input');if(response!==null&&response.trim())mutate('/api/interrupts/'+encodeURIComponent(i.id)+'/resolve',{allow:true,response:response})};controls.append(b)}}for(const op of (data.operations||[])){if(op.state==='uncertain'){controls.append(operationButton('Reconcile delivered '+op.key.slice(0,12),op,true));controls.append(operationButton('Reconcile absent '+op.key.slice(0,12),op,false))}}}
function renderConversations(data){const selected=currentConversation();conversation.replaceChildren();for(const c of (data.conversations||[])){const o=document.createElement('option');o.value=c.id;o.textContent=c.title+' ('+c.id.slice(0,8)+')';conversation.append(o)}if(selected&&[...conversation.options].some(o=>o.value===selected))conversation.value=selected;if(!conversation.value&&conversation.options.length)conversation.selectedIndex=0;renderMessages()}
async function renderMessages(){if(!currentConversation()){messages.textContent='No conversation yet';return}try{const base='/api/conversations/'+encodeURIComponent(currentConversation()),r=await fetch(base,{cache:'no-store'}),d=await r.json(),er=await fetch(base+'/events?limit=100',{cache:'no-store'}),ed=await er.json();messages.replaceChildren();for(const m of (d.messages||[])){const p=document.createElement('p');p.textContent=String(m.role)+': '+String(m.content??'');messages.append(p)}for(const a of (d.attachments||[])){const p=document.createElement('p');p.textContent='attachment: '+String(a.filename)+' ('+String(a.size)+' bytes, '+String(a.sha256).slice(0,12)+'…)';messages.append(p)}for(const e of (ed.events||[])){if(e.kind==='message_created')continue;const card=document.createElement('pre');card.textContent='['+e.kind+'] '+JSON.stringify(e.payload);messages.append(card)}}catch(e){messages.textContent=String(e)}}
conversation.onchange=renderMessages;
let stream;
function connectStream(){
  if(!currentConversation()||typeof EventSource==='undefined')return;
  if(stream)stream.close();
  const suffix=token.value?'&access_token='+encodeURIComponent(token.value):'';
  stream=new EventSource('/api/conversations/'+encodeURIComponent(currentConversation())+'/stream?limit=100'+suffix);
  const streamEvent=()=>{renderMessages();refreshSnapshotOnly()};
  ['message_created','task_promoted','agent_event','model_delta','event_appended'].forEach(k=>stream.addEventListener(k,streamEvent));
  stream.onerror=()=>{stream.close();stream=null};
}
async function refreshSnapshotOnly(){try{const r=await fetch('/api/snapshot',{cache:'no-store'}),data=await r.json();renderConversations(data);controlsFor(data);view.textContent=JSON.stringify(data,null,2)}catch(e){view.textContent=String(e)}}
async function refresh(){await refreshSnapshotOnly();connectStream()}
refresh(); setInterval(()=>{if(!stream)refreshSnapshotOnly()},5000);
</script>
"""

    def make_server(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        tls: TLSConfig | ssl.SSLContext | None = None,
    ) -> OperatorServer:
        if not isinstance(host, str) or not host.strip():
            raise ValueError("host must be a non-empty string")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("port must be an integer between 0 and 65535")
        if self._server is not None:
            raise RuntimeError("operator server is already active")
        non_loopback = _operator_host_is_non_loopback(host)
        if non_loopback and not self.require_authentication:
            raise ValueError("non-loopback operator binds require authentication")
        if non_loopback and tls is None:
            raise ValueError("non-loopback operator binds require TLS")
        context = _tls_context(tls, server=True)
        server = OperatorServer((host, port), _OperatorHandler)
        server._tls_lock = threading.RLock()
        if context is not None:
            server.tls_context = context
            server.tls_enabled = True
        server.operator = self
        self._server = server
        return server

    def serve_forever(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        poll_interval: float = 0.5,
        tls: TLSConfig | ssl.SSLContext | None = None,
    ) -> None:
        server = self.make_server(host, port, tls=tls)
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
        if not isinstance(header, str):
            return False
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer" or not value:
            return False
        if self.authenticator is not None and self.authenticator.verify(value) is not None:
            return True
        return self.operator_token is not None and hmac.compare_digest(value, self.operator_token)

    def _sse(self, path: tuple[str, ...], query: Mapping[str, list[str]]) -> tuple[tuple[str, str, str], ...]:
        """Return one reconnectable SSE batch; clients use Last-Event-ID/after."""
        after = _query_int(query, "after", default=0)
        limit = _query_int(query, "limit", default=100)
        if limit < 1 or limit > 1_000:
            raise ValueError("limit must be between 1 and 1000")
        if len(path) == 4 and path[:2] == ("api", "conversations"):
            page = self._conversation_events(path[2], after=after, limit=limit)
            return tuple(
                (
                    str(item.sequence),
                    item.kind,
                    json.dumps(
                        _conversation_event_json(item),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                )
                for item in page.events
            )
        if len(path) == 4 and path[:2] == ("api", "runs"):
            events = self.execution.agent_events(path[2], after=after, limit=limit)
            return tuple(
                (
                    str(item.sequence),
                    item.type,
                    json.dumps(
                        _event_json(item),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                )
                for item in events
            )
        raise KeyError("/" + "/".join(path))

    def _get(self, path: tuple[str, ...], query: Mapping[str, list[str]]) -> dict[str, Any]:
        if path == ("api", "conversations"):
            sessions = self._conversation_store()
            conversations = sessions.list_conversations(limit=self.max_items + 1)
            return {
                "conversations": [
                    _conversation_json(value)
                    for value in conversations[:self.max_items]
                ],
                "truncated": len(conversations) > self.max_items,
            }
        if len(path) == 3 and path[:2] == ("api", "conversations"):
            sessions = self._conversation_store()
            conversation = sessions.get_conversation(path[2])
            if conversation is None:
                raise KeyError(path[2])
            messages = sessions.list_messages(path[2], limit=self.max_items + 1)
            return {
                "conversation": _conversation_json(conversation),
                "messages": [
                    _message_json(value) for value in messages[:self.max_items]
                ],
                "truncated": len(messages) > self.max_items,
                "attachments": [
                    _attachment_json(item)
                    for item in sessions.list_attachments(path[2], limit=self.max_items)
                ],
            }
        if (
            len(path) == 4
            and path[:2] == ("api", "conversations")
            and path[3] == "messages"
        ):
            sessions = self._conversation_store()
            messages = sessions.list_messages(path[2], limit=self.max_items + 1)
            return {
                "conversation_id": path[2],
                "messages": [
                    _message_json(value) for value in messages[:self.max_items]
                ],
                "truncated": len(messages) > self.max_items,
            }
        if (
            len(path) == 5
            and path[:2] == ("api", "conversations")
            and path[3] == "attachments"
        ):
            sessions = self._conversation_store()
            attachment, content = sessions.read_attachment(path[4])
            if attachment.conversation_id != path[2]:
                raise KeyError(path[4])
            if len(content) > self.max_body_bytes:
                raise ValueError("attachment exceeds operator response limit")
            return {
                "attachment": _attachment_json(attachment),
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
        if (
            len(path) == 4
            and path[:2] == ("api", "conversations")
            and path[3] == "attachments"
        ):
            sessions = self._conversation_store()
            return {
                "conversation_id": path[2],
                "attachments": [
                    _attachment_json(item)
                    for item in sessions.list_attachments(path[2], limit=self.max_items)
                ],
            }
        if (
            len(path) == 4
            and path[:2] == ("api", "conversations")
            and path[3] == "events"
        ):
            after = _query_int(query, "after", default=0)
            limit = _query_int(query, "limit", default=100)
            if limit < 1 or limit > 1_000:
                raise ValueError("limit must be between 1 and 1000")
            page = self._conversation_events(path[2], after=after, limit=limit)
            return _conversation_event_page_json(page)
        if path == ("health",) or path == ("api", "health"):
            return {
                "ok": True,
                "version": __version__,
                "schema_version": self.execution.schema_version,
            }
        if path == ("ready",) or path == ("api", "ready"):
            if self.workbench is None:
                return {
                    "ready": self.execution.schema_version >= 1,
                    "version": __version__,
                    "schema_version": self.execution.schema_version,
                }
            return release_check(self.workbench.home).as_dict()
        if path == ("api", "metrics"):
            metrics, slo = measure_execution(self.execution)
            return {"metrics": metrics.as_dict(), "slo": slo.as_dict()}
        if path == ("api", "incidents"):
            return {
                "incidents": [
                    item.as_dict() for item in project_incidents(self.execution)
                ],
            }
        if path == ("api", "cost"):
            return {"cost": project_cost_ledger(self.execution).as_dict()}
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
                "conversations": self._conversations_json(),
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
        if path == ("api", "conversations"):
            sessions = self._conversation_store()
            conversation = sessions.create_conversation(
                conversation_id=_optional_text(body.get("conversation_id")),
                title=body.get("title", "New conversation"),
                workspace=body.get("workspace", self.conversation_workspace or "."),
                metadata=body.get("metadata"),
            )
            return {"conversation": _conversation_json(conversation)}
        if (
            len(path) == 4
            and path[:2] == ("api", "conversations")
            and path[3] == "attachments"
        ):
            encoded = body.get("content_base64")
            if not isinstance(encoded, str) or not encoded.strip():
                raise ValueError("content_base64 is required")
            try:
                attachment_content = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError) as exc:
                raise ValueError("content_base64 must be valid base64") from exc
            attachment = self._conversation_store().save_attachment(
                path[2], attachment_content,
                attachment_id=_optional_text(body.get("attachment_id")),
                filename=body.get("filename", "attachment.bin"),
                mime_type=body.get("mime_type", "application/octet-stream"),
                max_bytes=max(1, self.max_body_bytes),
            )
            return {"attachment": _attachment_json(attachment)}
        if (
            len(path) == 4
            and path[:2] == ("api", "conversations")
            and path[3] == "messages"
        ):
            sessions = self._conversation_store()
            role = body.get("role", "user")
            content = body.get("content")
            if content is None:
                raise ValueError("content is required")
            task_id = _optional_text(body.get("task_id"))
            run_id = _optional_text(body.get("run_id"))
            if (task_id is None) != (run_id is None):
                raise ValueError("task_id and run_id must be provided together")
            if task_id is not None:
                assert run_id is not None
                task = self.execution.get_task(task_id)
                run = self.execution.get_run(run_id)
                if task is None or run is None or run.task_id != task.id:
                    raise KeyError("linked task/run does not exist")
            message = sessions.append_message(
                path[2], role=role, content=content,
                message_id=_optional_text(body.get("message_id")),
                kind=body.get("kind", "message"),
                task_id=task_id,
                run_id=run_id,
                metadata=body.get("metadata"),
            )
            return {"message": _message_json(message)}
        if (
            len(path) == 4
            and path[:2] == ("api", "conversations")
            and path[3] == "events"
        ):
            sessions = self._conversation_store()
            kind = body.get("kind")
            payload = body.get("payload", {})
            if not isinstance(kind, str) or not kind.strip():
                raise ValueError("kind is required")
            if not isinstance(payload, Mapping):
                raise ValueError("payload must be an object")
            event = sessions.append_event(
                path[2], kind=kind,
                event_id=_optional_text(body.get("event_id")),
                message_id=_optional_text(body.get("message_id")),
                task_id=_optional_text(body.get("task_id")),
                run_id=_optional_text(body.get("run_id")),
                payload=payload,
            )
            return _conversation_event_page_json(
                ConversationEventPage((event,), event.sequence, False),
            )
        if (
            len(path) == 6
            and path[:2] == ("api", "conversations")
            and path[3] == "messages"
            and path[5] == "promote"
        ):
            if self.promote_message is None:
                raise KeyError("conversation task promotion is not configured")
            task, run, message = self.promote_message(
                path[2], path[4], goal=body.get("goal"),
                workspace=body.get("workspace"),
            )
            return {
                "message": _message_json(message),
                "task": _task_json(task),
                "run": _run_json(run),
            }
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

    def _conversation_store(self) -> SQLiteSessionStore:
        if self.sessions is None:
            raise KeyError("conversation kernel is not configured")
        return self.sessions

    def _conversation_events(
        self, conversation_id: str, *, after: int, limit: int,
    ) -> ConversationEventPage:
        if self.conversation_event_reader is not None:
            return self.conversation_event_reader(
                conversation_id, after=after, limit=limit,
            )
        return self._conversation_store().events(
            conversation_id, after=after, limit=limit,
        )

    def _operations_json(self) -> list[dict[str, Any]]:
        if self.operations is None:
            return []
        return [
            _operation_json(value)
            for value in self.operations.pending()[:self.max_items]
        ]

    def _conversations_json(self) -> list[dict[str, Any]]:
        if self.sessions is None:
            return []
        return [
            _conversation_json(value)
            for value in self.sessions.list_conversations(limit=self.max_items)
        ]


def _query_one(query: Mapping[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    return values[0] if values else None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("value must be a non-empty string or null")
    return value.strip()


def _conversation_json(conversation: Conversation) -> dict[str, Any]:
    return {
        "id": conversation.id,
        "title": conversation.title,
        "workspace": conversation.workspace,
        "state": conversation.state,
        "metadata": _wire(conversation.metadata),
        "created_at": conversation.created_at,
        "updated_at": conversation.updated_at,
    }


def _message_json(message: Message) -> dict[str, Any]:
    return {
        "id": message.id,
        "conversation_id": message.conversation_id,
        "role": message.role,
        "kind": message.kind,
        "content": _wire(message.content),
        "task_id": message.task_id,
        "run_id": message.run_id,
        "metadata": _wire(message.metadata),
        "created_at": message.created_at,
    }


def _conversation_event_page_json(page: ConversationEventPage) -> dict[str, Any]:
    return {
        "events": [
            {
                "event_id": event.event_id,
                "conversation_id": event.conversation_id,
                "sequence": event.sequence,
                "kind": event.kind,
                "message_id": event.message_id,
                "task_id": event.task_id,
                "run_id": event.run_id,
                "payload": _wire(event.payload),
                "created_at": event.created_at,
            }
            for event in page.events
        ],
        "next_cursor": page.next_cursor,
        "has_more": page.has_more,
    }


def _conversation_event_json(event: Any) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "conversation_id": event.conversation_id,
        "sequence": event.sequence,
        "kind": event.kind,
        "message_id": event.message_id,
        "task_id": event.task_id,
        "run_id": event.run_id,
        "payload": _wire(event.payload),
        "created_at": event.created_at,
    }


def _attachment_json(attachment: Any) -> dict[str, Any]:
    return {
        "id": attachment.id,
        "conversation_id": attachment.conversation_id,
        "filename": attachment.filename,
        "mime_type": attachment.mime_type,
        "size": attachment.size,
        "sha256": attachment.sha256,
        "created_at": attachment.created_at,
    }


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
    if value > 2**63 - 1:
        raise ValueError(f"{name} exceeds SQLite integer range")
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


def _wire(value: Any, *, _active: set[int] | None = None) -> Any:
    """Keep the HTTP boundary JSON-only without leaking object repr secrets."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        marker = "nan" if math.isnan(value) else "-inf" if value < 0 else "inf"
        return {"__lipas_nonfinite__": marker}
    if _active is None:
        _active = set()
    identity = id(value)
    if identity in _active:
        return {"type": "opaque", "python_type": type(value).__name__}
    if isinstance(value, Mapping):
        _active.add(identity)
        try:
            if any(not isinstance(key, str) for key in value):
                return {
                    "__lipas_mapping__": [
                        {
                            "key": _wire(key, _active=_active),
                            "value": _wire(item, _active=_active),
                        }
                        for key, item in value.items()
                    ],
                }
            return {
                key: _wire(item, _active=_active)
                for key, item in value.items()
            }
        finally:
            _active.remove(identity)
    if isinstance(value, (tuple, list)):
        _active.add(identity)
        try:
            return [_wire(item, _active=_active) for item in value]
        finally:
            _active.remove(identity)
    if isinstance(value, (set, frozenset)):
        _active.add(identity)
        try:
            projected = [_wire(item, _active=_active) for item in value]
            return sorted(
                projected,
                key=lambda item: json.dumps(
                    item, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False,
                ),
            )
        finally:
            _active.remove(identity)
    return {"type": type(value).__name__}
