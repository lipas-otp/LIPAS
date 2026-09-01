"""0.42 Local Web operator HTTP boundary contracts."""
from __future__ import annotations

import http.client
import json
import queue
import socket
import threading
from pathlib import Path

from lipas import AgentEventType, ExecutionStore, LocalWebOperator


def test_authenticated_projection_health_and_bounded_sse(tmp_path: Path):
    ready: queue.Queue[tuple[int, str] | tuple[str, str]] = queue.Queue()
    stop = threading.Event()

    def serve() -> None:
        try:
            with ExecutionStore(tmp_path / "operator.db") as execution:
                task = execution.create_task("operator", tmp_path)
                run = execution.create_run(task.id)
                execution.append_agent_event(
                    run.id, AgentEventType.TOOL_COMPLETED,
                    identity="tool-1", data={"ok": True},
                )
                operator = LocalWebOperator(
                    execution,
                    operator_token="operator-secret",
                    require_authentication=True,
                )
                server = operator.make_server(port=0)
                ready.put((server.server_address[1], run.id))
                while not stop.is_set():
                    server.handle_request()
                operator.close()
        except BaseException as exc:  # pragma: no cover - test harness guard
            ready.put(("error", repr(exc)))

    thread = threading.Thread(target=serve)
    thread.start()
    raw_port, run_id = ready.get(timeout=5)
    assert raw_port != "error", run_id
    port = int(raw_port)

    def request(
        method: str,
        path: str,
        *,
        auth: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object] | str, dict[str, str]]:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        request_headers = dict(headers or {})
        if auth is not None:
            request_headers["Authorization"] = auth
        connection.request(method, path, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        connection.close()
        if "application/json" in response_headers.get("content-type", ""):
            payload: dict[str, object] | str = json.loads(raw.decode("utf-8"))
        else:
            payload = raw.decode("utf-8")
        return response.status, payload, response_headers

    status, _, _ = request("GET", "/api/snapshot")
    assert status == 401
    status, health, _ = request("GET", "/health")
    assert status == 200 and isinstance(health, dict) and health["ok"] is True
    status, snapshot, _ = request(
        "GET", "/api/snapshot", auth="Bearer operator-secret",
    )
    assert status == 200 and isinstance(snapshot, dict)
    status, stream, headers = request(
        "GET", f"/api/runs/{run_id}/stream?after=0&limit=10",
        auth="Bearer operator-secret",
    )
    assert status == 200 and isinstance(stream, str)
    assert "event: tool_completed" in stream and "id: 1" in stream
    assert headers.get("connection") == "close"

    stop.set()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as wake:
            wake.sendall(b"GET /health HTTP/1.0\r\nHost: localhost\r\n\r\n")
    except (ConnectionRefusedError, ConnectionResetError):
        pass
    thread.join(timeout=2)
    assert not thread.is_alive()
