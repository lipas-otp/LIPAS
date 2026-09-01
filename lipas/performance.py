"""Small SQLite performance probes for the 0.40 local-runtime beta.

The benchmark measures only local durable Task/Run transitions.  It is not a
claim about distributed throughput, model latency, or a production SLA.  The
result is deliberately a value object so CI and operators can compare runs
without adding a metrics database.
"""
from __future__ import annotations

import tempfile
import time
import uuid
import math
import json
import hashlib
import re
import contextlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any, Callable, Mapping
from types import MappingProxyType

from .execution import ExecutionStore, RunState


def _finite_float(value: Any) -> float | None:
    """Return a finite float for numeric input, without leaking OverflowError."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        converted = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return converted if math.isfinite(converted) else None


def _finite_nonnegative(value: Any) -> bool:
    numeric = _finite_float(value)
    return numeric is not None and numeric >= 0


_EVIDENCE_SENSITIVE_KEY = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
    r"password|private[_-]?key|secret|authorization|cookie)",
)
_EVIDENCE_SECRET_VALUE = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~+/=-]{12,}|sk-[A-Za-z0-9_-]{12,}|"
    r"AKIA[A-Z0-9]{16}|-----BEGIN [^-]*PRIVATE KEY-----|"
    r"(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
    r"password|private[_-]?key|secret)\s*[:=]\s*\S+)",
)
_MAX_PARTNER_EVIDENCE_BYTES = 64 * 1024 * 1024


def _redact_evidence(value: Any, *, depth: int = 0) -> Any:
    """Keep partner evidence bounded and safe to print or archive."""
    if depth > 8:
        return "[TRUNCATED]"
    if isinstance(value, str):
        if _EVIDENCE_SECRET_VALUE.search(value):
            return "[REDACTED SECRET]"
        return value[:2048] + ("…" if len(value) > 2048 else "")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (raw_key, item) in enumerate(value.items()):
            if index >= 128:
                result["[TRUNCATED_KEYS]"] = True
                break
            key = raw_key if isinstance(raw_key, str) else str(raw_key)
            key = key[:128]
            result[key] = (
                "[REDACTED SECRET]" if _EVIDENCE_SENSITIVE_KEY.search(key)
                else _redact_evidence(item, depth=depth + 1)
            )
        return result
    if isinstance(value, (list, tuple)):
        items = [_redact_evidence(item, depth=depth + 1) for item in value[:128]]
        if len(value) > 128:
            items.append("[TRUNCATED_ITEMS]")
        return items
    return f"[{type(value).__name__} omitted]"

__all__ = [
    "ExecutionBenchmark", "ExecutionMetrics", "SLOReport",
    "CostEntry", "CostLedger", "IncidentRecord", "EvaluationCase",
    "EvaluationReport", "benchmark_execution_store", "measure_execution",
    "project_cost_ledger", "project_incidents", "evaluate_execution",
    "DesignPartnerCase", "DesignPartnerRun", "DesignPartnerSignoff", "DesignPartnerReport",
    "run_design_partner_validation",
    "ExecutionSoakReport", "run_execution_soak", "run_soak",
]


@dataclass(frozen=True, slots=True)
class CostEntry:
    """One auditable usage/cost projection for a terminal Run."""

    run_id: str
    dimensions: Mapping[str, str]
    usage: Mapping[str, float]
    amount: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("CostEntry.run_id must be non-empty")
        if not isinstance(self.dimensions, Mapping):
            raise TypeError("CostEntry.dimensions must be a mapping")
        normalized_dimensions: dict[str, str] = {}
        for key, value in self.dimensions.items():
            if not isinstance(key, str) or not key.strip() or not isinstance(value, str):
                raise ValueError("CostEntry.dimensions must map non-empty strings to strings")
            normalized_key = key.strip()
            if normalized_key in normalized_dimensions:
                raise ValueError("CostEntry.dimensions contains duplicate keys after normalization")
            normalized_dimensions[normalized_key] = value
        if not isinstance(self.usage, Mapping):
            raise TypeError("CostEntry.usage must be a mapping")
        for usage_key, usage_value in self.usage.items():
            numeric = _finite_float(usage_value)
            if not isinstance(usage_key, str) or not usage_key.strip() or numeric is None or numeric < 0:
                raise ValueError("CostEntry.usage must contain finite non-negative numbers")
        amount = _finite_float(self.amount)
        if amount is None or amount < 0:
            raise ValueError("CostEntry.amount must be finite and non-negative")
        object.__setattr__(self, "run_id", self.run_id.strip())
        object.__setattr__(self, "amount", amount)
        object.__setattr__(self, "dimensions", MappingProxyType(normalized_dimensions))
        normalized_usage: dict[str, float] = {}
        for usage_key, usage_value in self.usage.items():
            normalized_key = usage_key.strip()
            if normalized_key in normalized_usage:
                raise ValueError("CostEntry.usage contains duplicate keys after normalization")
            normalized_usage[normalized_key] = float(usage_value)
        object.__setattr__(self, "usage", MappingProxyType({
            key: value for key, value in normalized_usage.items()
        }))


@dataclass(frozen=True, slots=True)
class CostLedger:
    entries: tuple[CostEntry, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.entries, tuple) or any(
            not isinstance(entry, CostEntry) for entry in self.entries
        ):
            raise TypeError("CostLedger.entries must be a tuple of CostEntry")
        # Individual entries are bounded, but a ledger can still overflow
        # when many very large finite values are aggregated.  Keep the
        # projection's advertised finite-number contract true at the
        # aggregate boundary as well.
        totals: dict[str, float] = {}
        amount = 0.0
        for entry in self.entries:
            amount = _finite_sum(amount, float(entry.amount), "CostLedger.amount")
            for key, value in entry.usage.items():
                totals[key] = _finite_sum(
                    totals.get(key, 0.0), float(value),
                    f"CostLedger.totals[{key!r}]",
                )

    @property
    def totals(self) -> Mapping[str, float]:
        total: dict[str, float] = {}
        for entry in self.entries:
            for key, value in entry.usage.items():
                total[key] = _finite_sum(
                    total.get(key, 0.0), float(value),
                    f"CostLedger.totals[{key!r}]",
                )
        return total

    @property
    def amount(self) -> float:
        total = 0.0
        for entry in self.entries:
            total = _finite_sum(total, float(entry.amount), "CostLedger.amount")
        return total

    def as_dict(self) -> dict[str, Any]:
        return {
            "entries": [
                {"run_id": item.run_id, "dimensions": dict(item.dimensions),
                 "usage": dict(item.usage), "amount": item.amount}
                for item in self.entries
            ],
            "totals": dict(self.totals),
            "amount": self.amount,
        }


@dataclass(frozen=True, slots=True)
class IncidentRecord:
    run_id: str
    severity: str
    kind: str
    message: str
    recovery_required: bool = False

    def __post_init__(self) -> None:
        for name in ("run_id", "severity", "kind", "message"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"IncidentRecord.{name} must be non-empty")
        if not isinstance(self.recovery_required, bool):
            raise TypeError("IncidentRecord.recovery_required must be bool")

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "severity": self.severity,
            "kind": self.kind,
            "message": self.message,
            "recovery_required": self.recovery_required,
        }


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    case_id: str
    run_id: str
    expected_state: str = "completed"
    expected_result: Any = None
    compare_result: bool = False

    def __post_init__(self) -> None:
        for name in ("case_id", "run_id", "expected_state"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"EvaluationCase.{name} must be non-empty")
        if not isinstance(self.compare_result, bool):
            raise TypeError("EvaluationCase.compare_result must be bool")


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    passed: int
    failed: int
    details: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        for name in ("passed", "failed"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"EvaluationReport.{name} must be non-negative int")
        if not isinstance(self.details, tuple):
            raise TypeError("EvaluationReport.details must be a tuple")
        if self.passed + self.failed != len(self.details):
            raise ValueError("EvaluationReport counts must match details length")

    @property
    def success_rate(self) -> float:
        return self.passed / (self.passed + self.failed) if self.passed + self.failed else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "failed": self.failed, "success_rate": self.success_rate, "details": [dict(item) for item in self.details]}


@dataclass(frozen=True, slots=True)
class DesignPartnerCase:
    """A bounded scenario definition for repeatable partner validation."""

    case_id: str
    name: str
    description: str

    def __post_init__(self) -> None:
        for field_name in ("case_id", "name", "description"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be non-empty")


@dataclass(frozen=True, slots=True)
class DesignPartnerRun:
    """Evidence returned by one local fixture or real partner execution."""

    partner_id: str
    case_id: str
    run_id: str | None
    started_at: float
    finished_at: float
    success: bool
    unsafe_delivery: bool
    reconciliation_seconds: float | None
    operator_accepted: bool
    failure_categories: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("partner_id", "case_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"DesignPartnerRun.{name} must be non-empty")
        if self.run_id is not None and (
            not isinstance(self.run_id, str) or not self.run_id.strip()
        ):
            raise ValueError("DesignPartnerRun.run_id must be non-empty or None")
        for name in ("started_at", "finished_at"):
            value = getattr(self, name)
            normalized_time = _finite_float(value)
            if normalized_time is None:
                raise ValueError(f"DesignPartnerRun.{name} must be finite")
            object.__setattr__(self, name, normalized_time)
        if self.finished_at < self.started_at:
            raise ValueError("DesignPartnerRun.finished_at cannot precede started_at")
        if not math.isfinite(self.finished_at - self.started_at):
            raise ValueError("DesignPartnerRun duration must be finite")
        if not isinstance(self.success, bool) or not isinstance(self.unsafe_delivery, bool):
            raise TypeError("DesignPartnerRun success flags must be bool")
        if self.reconciliation_seconds is not None:
            reconciliation = _finite_float(self.reconciliation_seconds)
            if reconciliation is None or reconciliation < 0:
                raise ValueError("reconciliation_seconds must be finite and non-negative")
        if not isinstance(self.operator_accepted, bool):
            raise TypeError("operator_accepted must be bool")
        if not isinstance(self.failure_categories, tuple) or any(
            not isinstance(item, str) or not item.strip()
            for item in self.failure_categories
        ):
            raise ValueError("failure_categories must be a tuple of non-empty strings")
        if not isinstance(self.evidence, Mapping):
            raise TypeError("DesignPartnerRun.evidence must be a mapping")
        object.__setattr__(
            self,
            "evidence",
            MappingProxyType(_strict_json_copy(dict(self.evidence), "partner evidence")),
        )
        object.__setattr__(self, "partner_id", self.partner_id.strip())
        object.__setattr__(self, "case_id", self.case_id.strip())
        if self.run_id is not None:
            object.__setattr__(self, "run_id", self.run_id.strip())
        object.__setattr__(self, "started_at", float(self.started_at))
        object.__setattr__(self, "finished_at", float(self.finished_at))

    @property
    def duration_s(self) -> float:
        duration = self.finished_at - self.started_at
        if not math.isfinite(duration):
            raise ValueError("DesignPartnerRun duration overflowed to a non-finite value")
        return max(0.0, duration)

    def as_dict(self) -> dict[str, Any]:
        return {
            "partner_id": self.partner_id, "case_id": self.case_id,
            "run_id": self.run_id, "started_at": self.started_at,
            "finished_at": self.finished_at, "duration_s": self.duration_s,
            "success": self.success, "unsafe_delivery": self.unsafe_delivery,
            "reconciliation_seconds": self.reconciliation_seconds,
            "operator_accepted": self.operator_accepted,
            "failure_categories": list(self.failure_categories),
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class DesignPartnerSignoff:
    """Explicit external acceptance attached to a partner evidence artifact."""

    partner_id: str
    reviewer: str
    statement_id: str
    evidence_path: str
    evidence_sha256: str
    accepted_at: float

    def __post_init__(self) -> None:
        for name in ("partner_id", "reviewer", "statement_id", "evidence_path"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"DesignPartnerSignoff.{name} must be non-empty")
            object.__setattr__(self, name, value.strip())
        if not isinstance(self.evidence_sha256, str) or len(self.evidence_sha256) != 64:
            raise ValueError("evidence_sha256 must be a 64-character digest")
        try:
            int(self.evidence_sha256, 16)
        except ValueError as exc:
            raise ValueError("evidence_sha256 must be hexadecimal") from exc
        # Canonicalise case so manually supplied upper-case digests verify in
        # the same way as the lower-case hexdigest emitted by hashlib.
        object.__setattr__(self, "evidence_sha256", self.evidence_sha256.lower())
        accepted = _finite_float(self.accepted_at)
        if accepted is None:
            raise ValueError("accepted_at must be finite")
        object.__setattr__(self, "accepted_at", accepted)

    @classmethod
    def from_file(
        cls,
        partner_id: str,
        reviewer: str,
        statement_id: str,
        evidence_path: str | Path,
        *,
        accepted_at: float | None = None,
    ) -> "DesignPartnerSignoff":
        raw_path = Path(evidence_path).expanduser()
        # Check the path before resolving it. ``Path.resolve()`` follows a
        # symlink, so checking ``is_symlink`` afterwards would accept a link
        # that can be swapped to different evidence by an untrusted process.
        if any(component.is_symlink() for component in (raw_path, *raw_path.parents)):
            raise ValueError("partner evidence must not be a symbolic link")
        path = raw_path.resolve()
        if not path.is_file() or path.is_symlink():
            raise ValueError("partner evidence must be a regular file")
        try:
            if path.stat().st_size > _MAX_PARTNER_EVIDENCE_BYTES:
                raise ValueError("partner evidence exceeds 64 MiB")
        except OSError as exc:
            raise ValueError("partner evidence cannot be inspected") from exc
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return cls(
            partner_id,
            reviewer,
            statement_id,
            str(path),
            digest,
            time.time() if accepted_at is None else accepted_at,
        )

    def verify(self) -> bool:
        path = Path(self.evidence_path)
        if path.is_symlink() or not path.is_file():
            return False
        try:
            if path.stat().st_size > _MAX_PARTNER_EVIDENCE_BYTES:
                return False
            return hashlib.sha256(path.read_bytes()).hexdigest() == self.evidence_sha256
        except OSError:
            return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "partner_id": self.partner_id,
            "reviewer": self.reviewer,
            "statement_id": self.statement_id,
            "evidence_path": self.evidence_path,
            "evidence_sha256": self.evidence_sha256,
            "accepted_at": self.accepted_at,
        }


@dataclass(frozen=True, slots=True)
class DesignPartnerReport:
    """Validation report; local fixtures are never counted as partner proof."""

    partner_id: str
    runs: tuple[DesignPartnerRun, ...]
    generated_at: float
    evidence_scope: str = "local_fixture"
    external_partner_evidence_required: bool = True
    signoff: DesignPartnerSignoff | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.partner_id, str) or not self.partner_id.strip():
            raise ValueError("DesignPartnerReport.partner_id must be non-empty")
        if not isinstance(self.runs, tuple) or any(
            not isinstance(run, DesignPartnerRun) for run in self.runs
        ):
            raise TypeError("DesignPartnerReport.runs must be a tuple of DesignPartnerRun")
        if any(run.partner_id != self.partner_id.strip() for run in self.runs):
            raise ValueError("DesignPartnerReport runs must belong to partner_id")
        if _finite_float(self.generated_at) is None:
            raise ValueError("DesignPartnerReport.generated_at must be finite")
        if self.evidence_scope not in {"local_fixture", "external_adapter"}:
            raise ValueError("evidence_scope must be local_fixture or external_adapter")
        if not isinstance(self.external_partner_evidence_required, bool):
            raise TypeError("external_partner_evidence_required must be bool")
        if self.signoff is not None:
            if not isinstance(self.signoff, DesignPartnerSignoff):
                raise TypeError("signoff must be DesignPartnerSignoff or None")
            if self.signoff.partner_id != self.partner_id.strip():
                raise ValueError("signoff partner_id does not match report")
            if self.evidence_scope != "external_adapter":
                raise ValueError(
                    "partner signoff requires evidence_scope='external_adapter'",
                )
            if not self.signoff.verify():
                raise ValueError("partner signoff evidence no longer matches its digest")
        if not self.external_partner_evidence_required and self.signoff is None:
            raise ValueError("external acceptance requires an explicit signoff")
        object.__setattr__(self, "partner_id", self.partner_id.strip())
        object.__setattr__(self, "generated_at", float(self.generated_at))

    @property
    def passed(self) -> bool:
        return bool(self.runs) and all(
            run.success and not run.unsafe_delivery and run.operator_accepted
            for run in self.runs
        )

    @property
    def externally_accepted(self) -> bool:
        """Whether a verified partner signoff exists for this report."""
        return (
            self.evidence_scope == "external_adapter"
            and self.signoff is not None
            and self.signoff.verify()
            and self.passed
        )

    def with_signoff(self, signoff: DesignPartnerSignoff) -> "DesignPartnerReport":
        """Attach verified external evidence without mutating the report."""
        if not isinstance(signoff, DesignPartnerSignoff):
            raise TypeError("signoff must be DesignPartnerSignoff")
        if signoff.partner_id != self.partner_id:
            raise ValueError("signoff partner_id does not match report")
        if self.evidence_scope != "external_adapter":
            raise ValueError("external signoff requires evidence_scope='external_adapter'")
        if not signoff.verify():
            raise ValueError("partner signoff evidence does not match its digest")
        return DesignPartnerReport(
            self.partner_id,
            self.runs,
            self.generated_at,
            self.evidence_scope,
            False,
            signoff,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "partner_id": self.partner_id,
            "passed": self.passed,
            "evidence_scope": self.evidence_scope,
            "external_partner_evidence_required": self.external_partner_evidence_required,
            "externally_accepted": self.externally_accepted,
            "generated_at": self.generated_at,
            "signoff": None if self.signoff is None else self.signoff.as_dict(),
            "runs": [run.as_dict() for run in self.runs],
        }


def run_design_partner_validation(
    partner_id: str,
    cases: tuple[DesignPartnerCase, ...] | list[DesignPartnerCase],
    runner: Callable[[DesignPartnerCase], Mapping[str, Any]],
    *,
    now: Callable[[], float] = time.time,
    evidence_scope: str = "local_fixture",
) -> DesignPartnerReport:
    """Run deterministic validation fixtures and normalize their evidence.

    ``runner`` is deliberately injected: a local fixture can exercise the
    harness in CI, while a real design partner can supply an adapter later.
    ``evidence_scope`` distinguishes those inputs, but neither scope itself
    is a partner sign-off; an external actor must still approve the evidence.
    """
    if not isinstance(partner_id, str) or not partner_id.strip():
        raise ValueError("partner_id must be non-empty")
    if not callable(runner):
        raise TypeError("runner must be callable")
    if evidence_scope not in {"local_fixture", "external_adapter"}:
        raise ValueError("evidence_scope must be local_fixture or external_adapter")
    started_report = _finite_float(now())
    if started_report is None:
        raise ValueError("now() must return a finite timestamp")
    runs: list[DesignPartnerRun] = []
    for case in cases:
        if not isinstance(case, DesignPartnerCase):
            raise TypeError("cases must contain DesignPartnerCase values")
        started = _finite_float(now())
        if started is None:
            raise ValueError("now() must return a finite timestamp")
        try:
            raw = runner(case)
        except Exception as exc:
            # A failed case is evidence, not a reason to discard the whole
            # report. Process-level interrupts still propagate.
            raw = {
                "success": False,
                "unsafe_delivery": False,
                "operator_accepted": False,
                "failure_categories": (type(exc).__name__,),
                # Exception text frequently contains provider URLs or
                # credentials. Keep the category as evidence and require an
                # operator to inspect the original system's private logs.
                "error": {"type": type(exc).__name__},
            }
        finished = _finite_float(now())
        if finished is None:
            raise ValueError("now() must return a finite timestamp")
        if not isinstance(raw, Mapping):
            raise TypeError("design partner runner must return a mapping")
        categories = raw.get("failure_categories", ())
        if isinstance(categories, str) or not isinstance(categories, (tuple, list)):
            raise TypeError("failure_categories must be a sequence of strings")
        if any(not isinstance(item, str) or not item.strip() for item in categories):
            raise ValueError("failure_categories must contain non-empty strings")
        reconciliation = raw.get("reconciliation_seconds")
        if reconciliation is not None:
            reconciliation_numeric = _finite_float(reconciliation)
            if reconciliation_numeric is None or reconciliation_numeric < 0:
                raise ValueError("reconciliation_seconds must be non-negative or None")
        runs.append(DesignPartnerRun(
            partner_id=partner_id.strip(), case_id=case.case_id,
            run_id=raw.get("run_id") if isinstance(raw.get("run_id"), str) else None,
            started_at=started, finished_at=finished,
            success=raw.get("success") is True,
            unsafe_delivery=raw.get("unsafe_delivery") is True,
            reconciliation_seconds=None if reconciliation is None else reconciliation_numeric,
            operator_accepted=raw.get("operator_accepted") is True,
            failure_categories=tuple(categories),
            evidence=_redact_evidence(dict(raw)),
        ))
    return DesignPartnerReport(
        partner_id.strip(), tuple(runs), started_report,
        evidence_scope=evidence_scope,
    )


@dataclass(frozen=True, slots=True)
class ExecutionMetrics:
    """Bounded, read-only metrics projected from ExecutionStore."""

    runs: int
    completed: int
    failed: int
    cancelled: int
    waiting: int
    running: int
    uncertain: int
    event_count: int
    durations_s: tuple[float, ...] = ()
    usage: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "runs", "completed", "failed", "cancelled", "waiting",
            "running", "uncertain", "event_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"ExecutionMetrics.{name} must be non-negative int")
        if self.completed + self.failed + self.cancelled + self.waiting + self.running > self.runs:
            raise ValueError("ExecutionMetrics state counts exceed runs")
        if not isinstance(self.durations_s, tuple) or any(
            not _finite_nonnegative(value) for value in self.durations_s
        ):
            raise ValueError("ExecutionMetrics.durations_s must contain finite non-negative values")
        if not isinstance(self.usage, Mapping):
            raise TypeError("ExecutionMetrics.usage must be a mapping")
        if any(
            not isinstance(key, str) or not key.strip()
            or not _finite_nonnegative(value)
            for key, value in self.usage.items()
        ):
            raise ValueError("ExecutionMetrics.usage must contain finite non-negative values")
        normalized_usage: dict[str, float] = {}
        for key, value in self.usage.items():
            normalized_key = key.strip()
            if normalized_key in normalized_usage:
                raise ValueError(
                    "ExecutionMetrics.usage contains duplicate keys after normalization",
                )
            normalized_usage[normalized_key] = float(value)
        object.__setattr__(self, "usage", MappingProxyType(normalized_usage))

    @property
    def success_rate(self) -> float:
        terminal = self.completed + self.failed + self.cancelled
        return self.completed / terminal if terminal else 0.0

    @property
    def p95_duration_s(self) -> float:
        if not self.durations_s:
            return 0.0
        ordered = sorted(self.durations_s)
        return ordered[min(len(ordered) - 1, int(round((len(ordered) - 1) * .95)))]

    def as_dict(self) -> dict[str, Any]:
        return {
            "runs": self.runs, "completed": self.completed, "failed": self.failed,
            "cancelled": self.cancelled, "waiting": self.waiting,
            "running": self.running, "uncertain": self.uncertain,
            "event_count": self.event_count, "success_rate": self.success_rate,
            "p95_duration_s": self.p95_duration_s, "usage": dict(self.usage),
        }


@dataclass(frozen=True, slots=True)
class SLOReport:
    """Simple SLO evaluation without creating a metrics authority."""

    success_rate: float
    target_success_rate: float
    p95_duration_s: float
    target_p95_duration_s: float
    uncertainty_count: int
    terminal_count: int = 0

    def __post_init__(self) -> None:
        for name in (
            "success_rate", "target_success_rate", "p95_duration_s",
            "target_p95_duration_s",
        ):
            value = getattr(self, name)
            converted = _finite_float(value)
            if converted is None:
                raise ValueError(f"{name} must be finite")
            object.__setattr__(self, name, converted)
        if not 0 <= self.success_rate <= 1 or not 0 <= self.target_success_rate <= 1:
            raise ValueError("success rates must be between 0 and 1")
        if self.p95_duration_s < 0 or self.target_p95_duration_s <= 0:
            raise ValueError("durations must be non-negative and target positive")
        if (
            isinstance(self.uncertainty_count, bool)
            or not isinstance(self.uncertainty_count, int)
            or self.uncertainty_count < 0
        ):
            raise ValueError("uncertainty_count must be a non-negative int")
        if (
            isinstance(self.terminal_count, bool)
            or not isinstance(self.terminal_count, int)
            or self.terminal_count < 0
        ):
            raise ValueError("terminal_count must be a non-negative int")

    @property
    def healthy(self) -> bool:
        return (
            self.terminal_count > 0
            and self.success_rate >= self.target_success_rate
            and self.p95_duration_s <= self.target_p95_duration_s
            and self.uncertainty_count == 0
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "success_rate": self.success_rate,
            "target_success_rate": self.target_success_rate,
            "p95_duration_s": self.p95_duration_s,
            "target_p95_duration_s": self.target_p95_duration_s,
            "uncertainty_count": self.uncertainty_count,
            "terminal_count": self.terminal_count,
        }


def measure_execution(
    execution: ExecutionStore,
    *,
    target_success_rate: float = 0.99,
    target_p95_duration_s: float = 60.0,
    since: float | None = None,
    until: float | None = None,
) -> tuple[ExecutionMetrics, SLOReport]:
    """Project metrics and an SLO verdict from the existing control store."""
    if not isinstance(execution, ExecutionStore):
        raise TypeError("execution must be an ExecutionStore")
    target_success_numeric = _finite_float(target_success_rate)
    if target_success_numeric is None or not 0 <= target_success_numeric <= 1:
        raise ValueError("target_success_rate must be between 0 and 1")
    target_p95_numeric = _finite_float(target_p95_duration_s)
    if target_p95_numeric is None or target_p95_numeric <= 0:
        raise ValueError("target_p95_duration_s must be finite and positive")
    since_numeric = _finite_float(since) if since is not None else None
    until_numeric = _finite_float(until) if until is not None else None
    if since is not None and since_numeric is None:
        raise ValueError("since must be a finite timestamp or None")
    if until is not None and until_numeric is None:
        raise ValueError("until must be a finite timestamp or None")
    if since_numeric is not None and until_numeric is not None and until_numeric < since_numeric:
        raise ValueError("until cannot be before since")
    runs = tuple(
        run for run in execution.list_runs()
        if (since_numeric is None or run.created_at >= since_numeric)
        and (until_numeric is None or run.created_at <= until_numeric)
    )
    counts = {state: sum(run.state is state for run in runs) for state in RunState}
    durations = tuple(
        max(0.0, run.updated_at - run.created_at)
        for run in runs if run.state in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}
    )
    event_count = sum(len(execution.agent_events(run.id)) for run in runs)
    uncertain = sum(
        isinstance(run.error, Mapping) and run.error.get("recovery_required") is True
        for run in runs
    )
    usage: dict[str, float] = {}
    for run in runs:
        if not isinstance(run.result, Mapping):
            continue
        raw_usage = run.result.get("usage")
        if not isinstance(raw_usage, Mapping):
            continue
        for key, value in raw_usage.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("run usage keys must be non-empty strings")
            key = key.strip()
            numeric = _finite_float(value)
            if numeric is None or numeric < 0:
                raise ValueError("run usage values must be finite and non-negative")
            usage[key] = _finite_sum(
                usage.get(key, 0.0), numeric,
                f"run usage aggregate[{key!r}]",
            )
    metrics = ExecutionMetrics(
        runs=len(runs), completed=counts[RunState.COMPLETED],
        failed=counts[RunState.FAILED], cancelled=counts[RunState.CANCELLED],
        waiting=counts[RunState.WAITING], running=counts[RunState.RUNNING],
        uncertain=uncertain, event_count=event_count, durations_s=durations,
        usage=usage,
    )
    return metrics, SLOReport(
        metrics.success_rate, target_success_numeric, metrics.p95_duration_s,
        target_p95_numeric, uncertain,
        len(durations),
    )


def project_cost_ledger(
    execution: ExecutionStore,
    *,
    price_per_unit: Mapping[str, float] | None = None,
) -> CostLedger:
    """Project provider/tool usage from Run results without a new authority."""
    if not isinstance(execution, ExecutionStore):
        raise TypeError("execution must be an ExecutionStore")
    raw_prices = dict(price_per_unit or {})
    prices: dict[str, float] = {}
    for key, value in raw_prices.items():
        if not isinstance(key, str) or not key.strip() or not _finite_nonnegative(value):
            raise ValueError("price_per_unit must contain finite non-negative numeric values")
        normalized_key = key.strip()
        if normalized_key in prices:
            raise ValueError("price_per_unit contains duplicate keys after normalization")
        prices[normalized_key] = float(value)
    entries: list[CostEntry] = []
    for run in execution.list_runs():
        if not isinstance(run.result, Mapping):
            continue
        raw = run.result.get("usage")
        if not isinstance(raw, Mapping):
            continue
        usage: dict[str, float] = {}
        for key, value in raw.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("run usage keys must be non-empty strings")
            key = key.strip()
            numeric = _finite_float(value)
            if numeric is None or numeric < 0:
                raise ValueError("run usage values must be finite and non-negative")
            usage[key] = numeric
        if not usage:
            continue
        dimensions = {
            key: str(run.result[key]) for key in ("provider", "model", "worker_id", "connector")
            if key in run.result and isinstance(run.result[key], str)
        }
        amount = 0.0
        for bucket, value in usage.items():
            price = float(prices.get(bucket, 0.0))
            try:
                charge = value * price
            except OverflowError as exc:
                raise ValueError(
                    f"cost for usage bucket {bucket!r} is not finite",
                ) from exc
            if not math.isfinite(charge):
                raise ValueError(f"cost for usage bucket {bucket!r} is not finite")
            amount = _finite_sum(amount, charge, "cost total")
        entries.append(CostEntry(run.id, dimensions, usage, amount))
    return CostLedger(tuple(entries))


def project_incidents(execution: ExecutionStore) -> tuple[IncidentRecord, ...]:
    """Return failed/uncertain Runs as operator-facing incident records."""
    if not isinstance(execution, ExecutionStore):
        raise TypeError("execution must be an ExecutionStore")
    incidents: list[IncidentRecord] = []
    for run in execution.list_runs():
        if run.state not in {RunState.FAILED, RunState.CANCELLED} and not (
            isinstance(run.error, Mapping) and run.error.get("recovery_required") is True
        ):
            continue
        error = dict(run.error or {})
        uncertain = bool(error.get("recovery_required") is True or error.get("uncertain") is True)
        incidents.append(IncidentRecord(
            run.id, "high" if uncertain else "error",
            str(error.get("type", run.state.value)),
            str(error.get("message", "Run requires operator attention")),
            uncertain,
        ))
    return tuple(incidents)


def evaluate_execution(execution: ExecutionStore, cases: tuple[EvaluationCase, ...] | list[EvaluationCase]) -> EvaluationReport:
    """Compare recorded Runs with a small deterministic evaluation fixture."""
    if not isinstance(execution, ExecutionStore):
        raise TypeError("execution must be an ExecutionStore")
    passed = 0
    details: list[Mapping[str, Any]] = []
    for case in cases:
        if not isinstance(case, EvaluationCase):
            raise TypeError("cases must contain EvaluationCase values")
        run = execution.get_run(case.run_id)
        state_ok = run is not None and run.state.value == case.expected_state
        result_ok = not case.compare_result or (run is not None and run.result == case.expected_result)
        ok = state_ok and result_ok
        passed += int(ok)
        details.append({"case_id": case.case_id, "run_id": case.run_id, "passed": ok, "state": None if run is None else run.state.value, "state_ok": state_ok, "result_ok": result_ok})
    return EvaluationReport(passed, len(details) - passed, tuple(details))


@dataclass(frozen=True, slots=True)
class ExecutionBenchmark:
    """Summary of one bounded local ExecutionStore benchmark."""

    operations: int
    elapsed_s: float
    samples_ms: tuple[float, ...]
    workers: int = 1

    def __post_init__(self) -> None:
        for name in ("operations", "workers"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"ExecutionBenchmark.{name} must be positive int")
            # Counts are eventually used in float-derived throughput and in
            # bounded local loops.  Reject integers that cannot be represented
            # as a finite IEEE-754 value instead of allowing a later property
            # access to leak OverflowError or infinity.
            if _finite_float(value) is None:
                raise ValueError(
                    f"ExecutionBenchmark.{name} must be a finite positive int",
                )
        elapsed = _finite_float(self.elapsed_s)
        if elapsed is None or elapsed < 0:
            raise ValueError("ExecutionBenchmark.elapsed_s must be finite and non-negative")
        object.__setattr__(self, "elapsed_s", elapsed)
        if not isinstance(self.samples_ms, tuple) or any(
            not _finite_nonnegative(value)
            for value in self.samples_ms
        ):
            raise ValueError("ExecutionBenchmark.samples_ms must contain finite non-negative values")
        object.__setattr__(
            self,
            "samples_ms",
            tuple(float(value) for value in self.samples_ms),
        )
        if self.workers > self.operations:
            raise ValueError("ExecutionBenchmark.workers cannot exceed operations")

    @property
    def throughput_per_s(self) -> float:
        if not self.elapsed_s:
            return 0.0
        try:
            throughput = self.operations / self.elapsed_s
        except (OverflowError, ZeroDivisionError) as exc:
            raise ValueError(
                "ExecutionBenchmark.throughput_per_s is not finite",
            ) from exc
        if not math.isfinite(throughput):
            raise ValueError("ExecutionBenchmark.throughput_per_s is not finite")
        return throughput

    @property
    def mean_ms(self) -> float:
        return mean(self.samples_ms) if self.samples_ms else 0.0

    @property
    def p50_ms(self) -> float:
        return _percentile(self.samples_ms, 0.50)

    @property
    def p95_ms(self) -> float:
        return _percentile(self.samples_ms, 0.95)

    def as_dict(self) -> dict[str, Any]:
        return {
            "operations": self.operations,
            "workers": self.workers,
            "elapsed_s": self.elapsed_s,
            "throughput_per_s": self.throughput_per_s,
            "mean_ms": self.mean_ms,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
        }


@dataclass(frozen=True, slots=True)
class ExecutionSoakReport:
    """Evidence from a bounded repeated local Task/Run transition soak.

    This report describes only the local ExecutionStore path.  It is useful
    for CI, pre-release soak, and operator rehearsal, but is not a claim about
    model/provider availability or an external SLA.
    """

    requested_iterations: int
    executed_iterations: int
    succeeded: int
    failed: int
    elapsed_s: float
    max_latency_ms: float
    p95_latency_ms: float
    invariant_failures: tuple[str, ...] = ()
    scope: str = "local_execution_store"

    def __post_init__(self) -> None:
        for name in ("requested_iterations", "executed_iterations", "succeeded", "failed"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"ExecutionSoakReport.{name} must be a non-negative int")
        if self.executed_iterations > self.requested_iterations:
            raise ValueError("executed_iterations cannot exceed requested_iterations")
        if self.succeeded + self.failed != self.executed_iterations:
            raise ValueError("soak result counts must match executed_iterations")
        for name in ("elapsed_s", "max_latency_ms", "p95_latency_ms"):
            value = _finite_float(getattr(self, name))
            if value is None or value < 0:
                raise ValueError(f"ExecutionSoakReport.{name} must be finite and non-negative")
            object.__setattr__(self, name, value)
        if not isinstance(self.invariant_failures, tuple) or any(
            not isinstance(item, str) or not item.strip()
            for item in self.invariant_failures
        ):
            raise ValueError("invariant_failures must be a tuple of non-empty strings")
        if not isinstance(self.scope, str) or not self.scope.strip():
            raise ValueError("scope must be non-empty")
        object.__setattr__(self, "scope", self.scope.strip())

    @property
    def healthy(self) -> bool:
        return (
            self.executed_iterations > 0
            and self.failed == 0
            and not self.invariant_failures
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "requested_iterations": self.requested_iterations,
            "executed_iterations": self.executed_iterations,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "elapsed_s": self.elapsed_s,
            "max_latency_ms": self.max_latency_ms,
            "p95_latency_ms": self.p95_latency_ms,
            "invariant_failures": list(self.invariant_failures),
            "healthy": self.healthy,
        }


def run_execution_soak(
    execution: ExecutionStore,
    *,
    iterations: int = 100,
    duration_s: float | None = None,
    workspace: str | Path | None = None,
    task_prefix: str = "soak",
) -> ExecutionSoakReport:
    """Run repeated durable transitions and check terminal invariants.

    ``iterations`` is a hard upper bound; ``duration_s`` is an optional wall
    clock cap.  Supplying both lets an operator stop a large soak by time or
    count, whichever comes first.  No provider or network call is made.
    """
    if not isinstance(execution, ExecutionStore):
        raise TypeError("execution must be an ExecutionStore")
    if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations < 1:
        raise ValueError("iterations must be a positive int")
    if duration_s is not None:
        duration_numeric = _finite_float(duration_s)
        if duration_numeric is None or duration_numeric <= 0:
            raise ValueError("duration_s must be finite and positive or None")
    else:
        duration_numeric = None
    if not isinstance(task_prefix, str) or not task_prefix.strip():
        raise ValueError("task_prefix must be non-empty")
    root = Path.cwd() if workspace is None else Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"workspace must be an existing directory: {root}")
    started = time.perf_counter()
    deadline = None if duration_numeric is None else started + duration_numeric
    run_tag = uuid.uuid4().hex
    latencies: list[float] = []
    invariant_failures: list[str] = []
    succeeded = 0
    failed = 0
    executed = 0
    while executed < iterations and (deadline is None or time.perf_counter() < deadline or executed == 0):
        index = executed
        operation_started = time.perf_counter()
        created_run: Any = None
        claimed_run: Any = None
        try:
            task = execution.create_task(
                f"{task_prefix.strip()}-{index}",
                root,
                task_id=f"{task_prefix.strip()}_{run_tag}_{index}",
            )
            run = execution.create_run(task.id)
            created_run = run
            claimed = execution.claim_run(run.id)
            claimed_run = claimed
            execution.complete_run(
                run.id,
                claimed.lease_token or "",
                result={"soak": True, "index": index},
            )
            settled = execution.get_run(run.id)
            if settled is None or settled.state is not RunState.COMPLETED:
                raise RuntimeError("soak Run did not settle as completed")
            succeeded += 1
        except Exception as exc:
            failed += 1
            # A fault during the transition must not leave a live lease in
            # the production workspace. Best-effort settlement is itself
            # checked below; if the lease already expired, report the
            # non-terminal run as an invariant failure instead of hiding it.
            if created_run is not None and claimed_run is None:
                try:
                    execution.request_cancel(created_run.id)
                except Exception as settle_exc:
                    if len(invariant_failures) < 50:
                        invariant_failures.append(
                            f"pending settlement {type(settle_exc).__name__}: "
                            f"{str(settle_exc)[:256]}"
                        )
            if claimed_run is not None and claimed_run.lease_token:
                try:
                    execution.fail_run(
                        claimed_run.id,
                        claimed_run.lease_token,
                        error={"type": "soak_iteration_failed"},
                    )
                except Exception as settle_exc:
                    if len(invariant_failures) < 50:
                        invariant_failures.append(
                            f"settlement {type(settle_exc).__name__}: "
                            f"{str(settle_exc)[:256]}"
                        )
            if len(invariant_failures) < 50:
                invariant_failures.append(f"{type(exc).__name__}: {str(exc)[:256]}")
        except BaseException:
            # Preserve Ctrl-C/SystemExit semantics while still releasing a
            # lease so an interrupted soak cannot strand a live Run.
            if claimed_run is not None and claimed_run.lease_token:
                with contextlib.suppress(Exception):
                    execution.fail_run(
                        claimed_run.id,
                        claimed_run.lease_token,
                        error={"type": "soak_interrupted"},
                    )
            elif created_run is not None:
                with contextlib.suppress(Exception):
                    execution.request_cancel(created_run.id)
            raise
        finally:
            observed_run = claimed_run if claimed_run is not None else created_run
            if observed_run is not None:
                settled = execution.get_run(observed_run.id)
                if settled is None or settled.state not in {
                    RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED,
                }:
                    if len(invariant_failures) < 50:
                        state = "missing" if settled is None else settled.state.value
                        invariant_failures.append(
                            f"run {observed_run.id!r} remained non-terminal ({state})",
                        )
            executed += 1
            latency = (time.perf_counter() - operation_started) * 1_000
            if len(latencies) < 10_000:
                latencies.append(latency)
    elapsed = time.perf_counter() - started
    ordered = sorted(latencies)
    p95 = ordered[min(len(ordered) - 1, int(round((len(ordered) - 1) * 0.95)))] if ordered else 0.0
    return ExecutionSoakReport(
        iterations,
        executed,
        succeeded,
        failed,
        elapsed,
        max(latencies, default=0.0),
        p95,
        tuple(invariant_failures),
    )


run_soak = run_execution_soak


def benchmark_execution_store(
    path: str | Path = ":memory:",
    *,
    operations: int = 100,
    workspace: str | Path | None = None,
    workers: int = 1,
) -> ExecutionBenchmark:
    """Measure ``operations`` create/claim/complete transitions.

    A temporary workspace is used when none is supplied.  A file path is left
    in place for inspection; ``:memory:`` remains the convenient CI default
    for one worker.  With multiple workers, use a file path (or let this
    helper create a temporary one) so every connection observes one authority.
    Each worker owns its SQLite connection; this intentionally exercises the
    same bounded writer contention as independent local processes.
    """
    if isinstance(operations, bool) or not isinstance(operations, int) or operations < 1:
        raise ValueError("operations must be a positive int")
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("workers must be a positive int")
    workers = min(workers, operations)
    temporary: tempfile.TemporaryDirectory[str] | None = None
    if workspace is None:
        temporary = tempfile.TemporaryDirectory(prefix="lipas-benchmark-")
        root = Path(temporary.name)
    else:
        root = Path(workspace).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    run_tag = uuid.uuid4().hex
    database_path: str | Path = path
    database_temporary: tempfile.TemporaryDirectory[str] | None = None
    if workers > 1 and str(path) == ":memory:":
        database_temporary = tempfile.TemporaryDirectory(prefix="lipas-benchmark-db-")
        database_path = Path(database_temporary.name) / "execution.db"

    def worker(indices: tuple[int, ...]) -> list[float]:
        local_samples: list[float] = []
        with ExecutionStore(database_path) as execution:
            for index in indices:
                operation_started = time.perf_counter()
                task = execution.create_task(
                    f"benchmark-{index}",
                    root,
                    task_id=f"benchmark_{run_tag}_{index}",
                )
                run = execution.create_run(task.id)
                claimed = execution.claim_run(run.id)
                execution.complete_run(
                    run.id,
                    claimed.lease_token or "",
                    result={"index": index},
                )
                local_samples.append((time.perf_counter() - operation_started) * 1_000)
        return local_samples

    indices = tuple(tuple(range(worker_index, operations, workers)) for worker_index in range(workers))
    try:
        if workers == 1:
            samples = worker(indices[0])
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                batches = pool.map(worker, indices)
                samples = [sample for batch in batches for sample in batch]
    finally:
        if temporary is not None:
            temporary.cleanup()
        if database_temporary is not None:
            database_temporary.cleanup()
    return ExecutionBenchmark(
        operations,
        time.perf_counter() - started,
        tuple(samples),
        workers,
    )


def _percentile(values: tuple[float, ...], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return ordered[index]


def _finite_sum(left: float, right: float, label: str) -> float:
    """Add two finite numbers while rejecting IEEE-754 overflow."""
    total = left + right
    if not math.isfinite(total):
        raise ValueError(f"{label} overflowed to a non-finite value")
    return total


def _strict_json_copy(value: Any, name: str) -> dict[str, Any]:
    """Detach report evidence and reject coercive/non-finite JSON shapes."""
    def validate(item: Any, path: str, active: set[int]) -> None:
        if item is None or isinstance(item, (bool, int, str)):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError(f"{path} must contain finite numbers")
            return
        if not isinstance(item, (list, tuple, Mapping)):
            raise TypeError(f"{path} contains unsupported {type(item).__name__}")
        marker = id(item)
        if marker in active:
            raise ValueError(f"{path} must not contain reference cycles")
        active.add(marker)
        try:
            if isinstance(item, Mapping):
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise ValueError(f"{path} must use string object keys")
                    validate(child, f"{path}.{key}", active)
            else:
                for index, child in enumerate(item):
                    validate(child, f"{path}[{index}]", active)
        finally:
            active.remove(marker)

    validate(value, name, set())
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(f"{name} must be strict JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"{name} must be a JSON object")
    return decoded
