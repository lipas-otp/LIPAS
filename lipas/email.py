"""Provider-neutral, idempotent email delivery connector."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from .operations import (
    Operation, OperationJournal, OperationStateError, PendingOperation,
)

__all__ = [
    "EmailApprovalRequired", "EmailDelivery", "EmailMessage", "EmailProvider",
    "EmailConnector",
]


class EmailApprovalRequired(PermissionError):
    """A delivery must be explicitly approved by the host/operator."""


@dataclass(frozen=True, slots=True)
class EmailMessage:
    sender: str
    recipients: tuple[str, ...]
    subject: str
    text: str
    html: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.sender, str) or not _valid_address(self.sender):
            raise ValueError("sender must be an email address")
        object.__setattr__(self, "sender", self.sender.strip())
        self._validate_header_text(self.sender, "sender")
        if not self.recipients or any(
            not isinstance(value, str) or not _valid_address(value)
            for value in self.recipients
        ):
            raise ValueError("recipients must contain email addresses")
        normalized_recipients = tuple(value.strip() for value in self.recipients)
        if len(set(normalized_recipients)) != len(normalized_recipients):
            raise ValueError("recipients must not contain duplicates")
        object.__setattr__(self, "recipients", normalized_recipients)
        for recipient in self.recipients:
            self._validate_header_text(recipient, "recipient")
        if not isinstance(self.subject, str) or not self.subject.strip():
            raise ValueError("subject must be non-empty")
        self._validate_header_text(self.subject, "subject")
        if not isinstance(self.text, str):
            raise TypeError("text must be a string")
        if self.html is not None and not isinstance(self.html, str):
            raise TypeError("html must be a string or None")
        if not isinstance(self.headers, Mapping):
            raise TypeError("headers must be a mapping")
        for key, value in self.headers.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise TypeError("email headers must map strings to strings")
            self._validate_header_text(key, "header name")
            self._validate_header_text(value, "header value")
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))

    @staticmethod
    def _validate_header_text(value: str, field_name: str) -> None:
        if "\r" in value or "\n" in value:
            raise ValueError(f"email {field_name} must not contain CR/LF")

    def as_request(self, *, include_content: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "sender": self.sender,
            "recipients": list(self.recipients),
            "subject": self.subject,
            "headers": dict(self.headers),
        }
        if include_content:
            payload.update({"text": self.text, "html": self.html})
        return payload


@dataclass(frozen=True, slots=True)
class EmailDelivery:
    provider_reference: str
    accepted: bool = True
    provider_request_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.provider_reference, str) or not self.provider_reference.strip():
            raise ValueError("provider_reference must be non-empty")
        if not isinstance(self.accepted, bool):
            raise TypeError("accepted must be bool")
        if self.provider_request_id is not None and (
            not isinstance(self.provider_request_id, str)
            or not self.provider_request_id.strip()
        ):
            raise ValueError("provider_request_id must be non-empty or None")
        object.__setattr__(self, "provider_reference", self.provider_reference.strip())
        if self.provider_request_id is not None:
            object.__setattr__(self, "provider_request_id", self.provider_request_id.strip())


class EmailProvider(Protocol):
    def send(self, message: EmailMessage, *, idempotency_key: str) -> EmailDelivery | Mapping[str, Any]: ...
    def lookup(self, *, idempotency_key: str) -> tuple[bool, Any, str | None]: ...


class EmailConnector:
    """One approval/reconciliation boundary for all email providers."""

    def __init__(self, provider: EmailProvider, journal: OperationJournal) -> None:
        if provider is None:
            raise TypeError("provider must not be None")
        if not isinstance(journal, OperationJournal):
            raise TypeError("journal must be OperationJournal")
        self.provider = provider
        self.journal = journal

    def send(
        self,
        message: EmailMessage,
        *,
        idempotency_key: str,
        effect_id: str | None = None,
        approved: bool = False,
    ) -> Operation:
        if not isinstance(message, EmailMessage):
            raise TypeError("message must be an EmailMessage")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("email idempotency_key must be non-empty")
        idempotency_key = idempotency_key.strip()
        if not isinstance(approved, bool):
            raise TypeError("approved must be bool")
        if not approved:
            raise EmailApprovalRequired(
                "email delivery requires explicit approval; draft the message first",
            )
        request = message.as_request()
        content = message.as_request(include_content=True)
        request["content_sha256"] = hashlib.sha256(
            json.dumps(
                content, sort_keys=True, ensure_ascii=False, allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        operation, owns = self.journal._prepare(
            key=idempotency_key,
            kind="email_send",
            request=request,
            effect_id=effect_id,
            provider_request_id=idempotency_key,
        )
        if operation.state == "succeeded":
            return operation
        if not owns:
            # pending/uncertain/failed must be reconciled or deliberately
            # superseded by a new key; never silently resend the message.
            raise PendingOperation(
                f"email operation {idempotency_key!r} is {operation.state}; reconcile first",
            )
        try:
            raw = self.provider.send(message, idempotency_key=idempotency_key)
            reference: str | None
            if isinstance(raw, EmailDelivery):
                result: Mapping[str, Any] = {
                    "accepted": raw.accepted,
                    "provider_reference": raw.provider_reference,
                    "provider_request_id": raw.provider_request_id or idempotency_key,
                }
                reference = raw.provider_reference
            elif isinstance(raw, Mapping):
                result = dict(raw)
                reference_value = raw.get("provider_reference") or raw.get("id")
                reference = reference_value if isinstance(reference_value, str) else None
            else:
                raise TypeError("email provider must return EmailDelivery or mapping")
            accepted = result.get("accepted", True)
            if not isinstance(accepted, bool):
                raise TypeError("email provider accepted must be bool")
            if not accepted:
                return self.journal.fail(
                    idempotency_key,
                    error={
                        "type": "provider_rejected",
                        "provider_reference": reference,
                        "result": dict(result),
                    },
                )
            if reference is None or not reference.strip():
                raise ValueError(
                    "email provider must return a non-empty provider_reference",
                )
            return self.journal.settle(
                idempotency_key, result=result, provider_reference=reference,
            )
        except Exception as exc:
            self._mark_uncertain(idempotency_key, exc)
            raise RuntimeError(
                f"email delivery {idempotency_key!r} is uncertain; reconcile first",
            ) from exc
        except BaseException as exc:
            # Preserve process/task cancellation semantics while retaining the
            # safety invariant that an interrupted send cannot be retried
            # blindly.
            self._mark_uncertain(idempotency_key, exc)
            raise

    def reconcile(self, idempotency_key: str) -> Operation:
        def lookup(key: str) -> tuple[bool, Any, str | None]:
            found, result, reference = self.provider.lookup(idempotency_key=key)
            if not isinstance(found, bool):
                raise ValueError("email provider lookup found flag must be bool")
            if found and (
                not isinstance(reference, str) or not reference.strip()
            ):
                # An accepted email without a provider reference cannot be
                # safely distinguished from an incomplete lookup. Keep the
                # operation uncertain rather than manufacturing success.
                raise ValueError(
                    "email provider lookup must return a non-empty "
                    "provider_reference when found",
                )
            return found, result, reference

        return self.journal.reconcile(
            idempotency_key,
            lookup,
        )

    def _mark_uncertain(self, key: str, cause: BaseException) -> Operation:
        try:
            return self.journal.mark_uncertain(
                key,
                error={"type": type(cause).__name__, "message": str(cause)},
            )
        except OperationStateError:
            latest = self.journal.get(key)
            if latest is None:
                raise
            return latest


def _valid_address(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    candidate = value.strip()
    if not candidate or any(char.isspace() for char in candidate):
        return False
    local, separator, domain = candidate.partition("@")
    return bool(separator and local and domain and "@" not in domain)
