"""Bounded computation helpers used by the local Workbench.

The functions in this module deliberately do not know anything about a
workspace or a Task.  Path policy, staging, approval, and evidence remain the
responsibility of :mod:`lipas.workbench`.  ``execute_python`` is a small
worker-oriented primitive: the caller supplies an already selected sandbox and
the code runs in a temporary directory, never in the user's project.

The local sandbox is a compatibility mode, not a security boundary.  The
Workbench therefore records its isolation flags and callers can require an OS
sandbox in deployment policy.  The runner also disables the most common
network/process escape hatches as defence in depth when a local sandbox is
explicitly selected.
"""
from __future__ import annotations

import ast
import csv
import io
import math
import os
import tempfile
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

__all__ = [
    "CodeToolError",
    "CodeExecutionResult",
    "CsvAnalysis",
    "calculate_expression",
    "analyze_csv",
    "execute_python",
]


class CodeToolError(ValueError):
    """A bounded computation request is invalid or could not complete."""


class _Sandbox(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def isolated(self) -> bool: ...

    @property
    def network_isolated(self) -> bool: ...

    async def run(
        self,
        argv: Sequence[str],
        *,
        workspace: Path,
        environment: Mapping[str, str],
        timeout_s: float,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class CodeExecutionResult:
    """Stable, non-sensitive result of one Python worker invocation."""

    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_seconds: float
    sandbox: str
    isolated: bool
    network_isolated: bool
    source_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "duration_seconds": self.duration_seconds,
            "sandbox": self.sandbox,
            "isolated": self.isolated,
            "network_isolated": self.network_isolated,
            "source_sha256": self.source_sha256,
        }


@dataclass(frozen=True, slots=True)
class CsvAnalysis:
    """Small deterministic CSV profile suitable for an LLM tool result."""

    columns: tuple[str, ...]
    rows: int
    numeric: Mapping[str, Mapping[str, float | int]]
    missing: Mapping[str, int]

    def as_dict(self) -> dict[str, object]:
        return {
            "columns": list(self.columns),
            "rows": self.rows,
            "numeric": {key: dict(value) for key, value in self.numeric.items()},
            "missing": dict(self.missing),
        }


def _validate_limit(name: str, value: int, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CodeToolError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise CodeToolError(f"{name} must be between {minimum} and {maximum}")
    return value


# ---------------------------------------------------------------------------
# Exact calculator
# ---------------------------------------------------------------------------

_ARITHMETIC_NODES = (
    ast.Expression, ast.Constant, ast.UnaryOp, ast.UAdd, ast.USub,
    ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv,
    ast.Mod, ast.Pow,
)


def _decimal_number(value: object) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CodeToolError("calculator accepts only numeric literals")
    if isinstance(value, float) and not math.isfinite(value):
        raise CodeToolError("calculator rejects non-finite numeric literals")
    return value


def _evaluate(node: ast.AST) -> int | float:
    if isinstance(node, ast.Constant):
        return _decimal_number(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _evaluate(node.operand)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp):
        left = _evaluate(node.left)
        right = _evaluate(node.right)
        try:
            if isinstance(node.op, ast.Add):
                value = left + right
            elif isinstance(node.op, ast.Sub):
                value = left - right
            elif isinstance(node.op, ast.Mult):
                value = left * right
            elif isinstance(node.op, ast.Div):
                value = left / right
            elif isinstance(node.op, ast.FloorDiv):
                value = left // right
            elif isinstance(node.op, ast.Mod):
                value = left % right
            elif isinstance(node.op, ast.Pow):
                if abs(right) > 100:
                    raise CodeToolError("calculator exponent is limited to 100")
                value = left ** right
            else:  # pragma: no cover - guarded by the AST walk
                raise CodeToolError("calculator operator is not allowed")
        except (ArithmeticError, OverflowError) as exc:
            raise CodeToolError(f"calculator operation failed: {exc}") from exc
        if isinstance(value, float) and not math.isfinite(value):
            raise CodeToolError("calculator result is not finite")
        return value
    raise CodeToolError("calculator expression contains a disallowed construct")


def calculate_expression(expression: str, *, max_length: int = 1_000) -> int | float:
    """Evaluate a finite arithmetic expression without names, calls, or I/O."""
    if not isinstance(expression, str) or not expression.strip():
        raise CodeToolError("expression must be a non-empty string")
    _validate_limit("max_length", max_length, minimum=1, maximum=10_000)
    if len(expression) > max_length:
        raise CodeToolError("expression exceeds the calculator length limit")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise CodeToolError(f"invalid arithmetic expression: {exc.msg}") from exc
    for node in ast.walk(tree):
        if not isinstance(node, _ARITHMETIC_NODES):
            raise CodeToolError("only arithmetic operators and numeric literals are allowed")
    result = _evaluate(tree.body)
    # Keep JSON output stable and avoid exposing -0.0 to callers.
    if isinstance(result, float) and result == 0:
        return 0
    return result


# ---------------------------------------------------------------------------
# CSV profiling
# ---------------------------------------------------------------------------


def analyze_csv(
    path: Path,
    *,
    max_bytes: int = 10 * 1024 * 1024,
    max_rows: int = 100_000,
    max_columns: int = 200,
) -> CsvAnalysis:
    """Return a bounded header, missing-value, and numeric CSV profile."""
    _validate_limit("max_bytes", max_bytes, minimum=1, maximum=100 * 1024 * 1024)
    _validate_limit("max_rows", max_rows, minimum=1, maximum=1_000_000)
    _validate_limit("max_columns", max_columns, minimum=1, maximum=1_000)
    try:
        if path.stat().st_size > max_bytes:
            raise CodeToolError("CSV exceeds the analysis size limit")
        text = path.read_text(encoding="utf-8")
    except CodeToolError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise CodeToolError(f"could not read CSV: {exc}") from exc
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        raw_fields = reader.fieldnames
        if not raw_fields:
            raise CodeToolError("CSV must contain a header row")
        if len(raw_fields) > max_columns:
            raise CodeToolError("CSV exceeds the column limit")
        fields = tuple(str(value) for value in raw_fields)
        missing = {field: 0 for field in fields}
        values: dict[str, list[float]] = {field: [] for field in fields}
        rows = 0
        for row in reader:
            rows += 1
            if rows > max_rows:
                raise CodeToolError("CSV exceeds the row limit")
            extra = row.get(None)
            if extra:
                raise CodeToolError("CSV row has more fields than its header")
            for field in fields:
                value = row.get(field)
                if value is None or not value.strip():
                    missing[field] += 1
                    continue
                try:
                    number = float(value)
                except ValueError:
                    continue
                if math.isfinite(number):
                    values[field].append(number)
        numeric: dict[str, Mapping[str, float | int]] = {}
        for field, numbers in values.items():
            if not numbers:
                continue
            total = sum(numbers)
            numeric[field] = {
                "count": len(numbers),
                "min": min(numbers),
                "max": max(numbers),
                "mean": total / len(numbers),
            }
        return CsvAnalysis(fields, rows, numeric, missing)
    except csv.Error as exc:
        raise CodeToolError(f"could not parse CSV: {exc}") from exc


# ---------------------------------------------------------------------------
# Sandboxed Python worker
# ---------------------------------------------------------------------------


_RUNNER = textwrap.dedent(
    r'''
    import contextlib
    import os
    import socket
    import subprocess
    import sys
    import traceback

    # These guards are defence in depth for the explicitly unsafe local
    # compatibility backend.  Bubblewrap still supplies the real boundary.
    def _blocked(*args, **kwargs):
        raise PermissionError("network and child-process access are disabled")
    socket.socket = _blocked
    socket.create_connection = _blocked
    subprocess.Popen = _blocked
    subprocess.run = _blocked
    subprocess.call = _blocked
    os.system = _blocked
    os.popen = _blocked

    class _LimitedWriter:
        def __init__(self, stream, limit):
            self.stream, self.limit, self.used = stream, limit, 0
        def write(self, value):
            remaining = max(0, self.limit - self.used)
            chunk = value[:remaining]
            self.used += len(chunk)
            self.stream.write(chunk)
            self.stream.flush()
            return len(value)
        def flush(self):
            self.stream.flush()
        def isatty(self):
            return False

    try:
        import resource
        cpu = int(os.environ.get("LIPAS_CPU_SECONDS", "10"))
        memory = int(os.environ.get("LIPAS_MEMORY_BYTES", "268435456"))
        output = int(os.environ.get("LIPAS_OUTPUT_BYTES", "65536"))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        resource.setrlimit(resource.RLIMIT_FSIZE, (output, output))
        with contextlib.suppress(Exception):
            resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
    except Exception:
        output = int(os.environ.get("LIPAS_OUTPUT_BYTES", "65536"))

    sys.stdout = _LimitedWriter(sys.stdout, output)
    sys.stderr = _LimitedWriter(sys.stderr, output)
    source = open(sys.argv[1], encoding="utf-8").read()
    namespace = {"__name__": "__main__", "__file__": sys.argv[1]}
    try:
        exec(compile(source, sys.argv[1], "exec"), namespace, namespace)
    except BaseException:
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
    ''',
)


async def execute_python(
    source: str,
    *,
    sandbox: _Sandbox,
    timeout_seconds: int = 30,
    max_output_bytes: int = 64_000,
    max_source_bytes: int = 100_000,
    max_memory_bytes: int = 256 * 1024 * 1024,
    max_cpu_seconds: int = 10,
) -> CodeExecutionResult:
    """Execute bounded Python source in an isolated temporary directory."""
    if not isinstance(source, str) or not source.strip():
        raise CodeToolError("source must be a non-empty string")
    _validate_limit("timeout_seconds", timeout_seconds, minimum=1, maximum=300)
    _validate_limit("max_output_bytes", max_output_bytes, minimum=1, maximum=1_000_000)
    _validate_limit("max_source_bytes", max_source_bytes, minimum=1, maximum=1_000_000)
    _validate_limit("max_memory_bytes", max_memory_bytes, minimum=16 * 1024 * 1024, maximum=2 * 1024 * 1024 * 1024)
    _validate_limit("max_cpu_seconds", max_cpu_seconds, minimum=1, maximum=300)
    encoded = source.encode("utf-8")
    if len(encoded) > max_source_bytes:
        raise CodeToolError("source exceeds the Python execution size limit")
    import hashlib
    source_sha256 = hashlib.sha256(encoded).hexdigest()
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="lipas-python-") as directory:
        root = Path(directory)
        script = root / "program.py"
        runner = root / "runner.py"
        script.write_bytes(encoded)
        runner.write_text(_RUNNER, encoding="utf-8")
        env = {
            "PATH": os.environ.get("PATH", ""),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "LIPAS_CPU_SECONDS": str(max_cpu_seconds),
            "LIPAS_MEMORY_BYTES": str(max_memory_bytes),
            "LIPAS_OUTPUT_BYTES": str(max_output_bytes),
        }
        try:
            result = await sandbox.run(
                ["python3", "runner.py", "program.py"],
                workspace=root,
                environment=env,
                timeout_s=float(timeout_seconds),
            )
        except Exception as exc:
            raise CodeToolError(f"Python worker could not start: {exc}") from exc
    stdout = str(result.stdout)[:max_output_bytes]
    stderr = str(result.stderr)[:max_output_bytes]
    return CodeExecutionResult(
        exit_code=result.exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=bool(result.timed_out),
        duration_seconds=time.monotonic() - started,
        sandbox=str(result.sandbox),
        isolated=bool(result.isolated),
        network_isolated=bool(result.network_isolated),
        source_sha256=source_sha256,
    )
