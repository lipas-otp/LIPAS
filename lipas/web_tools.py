"""Small, policy-preserving web retrieval helpers.

This module intentionally does not contain a search-provider API key or a
second HTTP stack.  Hosts configure :class:`lipas.http_client.HttpClient` with
an explicit egress allowlist and pass it to ``fetch_url_tool``.  The returned
page text is untrusted input and should be quoted or otherwise delimited in a
model prompt; it is never treated as an instruction or a Claim authority.
"""
from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Mapping

from .http_client import HttpClient, HttpClientError
from .tools import Tool, tool

__all__ = [
    "FetchedPage",
    "WebToolError",
    "extract_page_text",
    "fetch_url",
    "fetch_url_tool",
]


class WebToolError(ValueError):
    """A bounded page retrieval or extraction request failed."""


@dataclass(frozen=True, slots=True)
class FetchedPage:
    """Bounded page metadata and visible text."""

    url: str
    status_code: int
    content_type: str
    title: str
    text: str
    sha256: str
    bytes: int
    truncated: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "url": self.url,
            "status_code": self.status_code,
            "content_type": self.content_type,
            "title": self.title,
            "text": self.text,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "truncated": self.truncated,
        }


def _limit(name: str, value: int, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WebToolError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise WebToolError(f"{name} must be between {minimum} and {maximum}")
    return value


class _VisibleTextParser(HTMLParser):
    _ignored = frozenset({"script", "style", "noscript", "template", "svg"})
    _line_breaks = frozenset({"br", "p", "div", "li", "tr", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.parts: list[str] = []
        self._ignored_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        normalized = tag.lower()
        if normalized in self._ignored:
            self._ignored_depth += 1
        if normalized == "title" and self._ignored_depth == 0:
            self._in_title = True
        if normalized in self._line_breaks and self._ignored_depth == 0:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized == "title":
            self._in_title = False
        if normalized in self._line_breaks and self._ignored_depth == 0:
            self.parts.append("\n")
        if normalized in self._ignored and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
            return
        self.parts.append(data)


def _normalize_text(value: str) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def extract_page_text(
    body: bytes,
    *,
    content_type: str = "text/plain",
    max_chars: int = 120_000,
) -> tuple[str, str, bool]:
    """Decode visible HTML/text, returning ``(text, title, truncated)``."""
    _limit("max_chars", max_chars, minimum=1, maximum=1_000_000)
    if not isinstance(body, bytes):
        raise WebToolError("page body must be bytes")
    media_type = content_type.split(";", 1)[0].strip().lower()
    try:
        decoded = body.decode("utf-8")
    except UnicodeDecodeError:
        decoded = body.decode("utf-8", errors="replace")
    if media_type in {"text/html", "application/xhtml+xml"}:
        parser = _VisibleTextParser()
        try:
            parser.feed(decoded)
            parser.close()
        except Exception as exc:
            raise WebToolError(f"could not parse HTML response: {exc}") from exc
        text = _normalize_text("".join(parser.parts))
        title = _normalize_text("".join(parser.title_parts))
    else:
        text = _normalize_text(html.unescape(decoded))
        title = ""
    truncated = len(text) > max_chars
    return text[:max_chars], title[:500], truncated


async def fetch_url(
    client: HttpClient,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    max_bytes: int = 2 * 1024 * 1024,
    max_chars: int = 120_000,
) -> FetchedPage:
    """Fetch one allowlisted URL through an existing :class:`HttpClient`."""
    if not isinstance(client, HttpClient):
        raise TypeError("client must be an HttpClient")
    _limit("max_bytes", max_bytes, minimum=1, maximum=20 * 1024 * 1024)
    _limit("max_chars", max_chars, minimum=1, maximum=1_000_000)
    try:
        response = await client.request("GET", url, params=params)
    except HttpClientError as exc:
        raise WebToolError(str(exc)) from exc
    body = response.body
    if len(body) > max_bytes:
        raise WebToolError("response exceeds the web retrieval size limit")
    content_type = next(
        (str(value) for key, value in response.headers.items()
         if str(key).lower() == "content-type"),
        "text/plain",
    )
    text, title, truncated = extract_page_text(
        body, content_type=content_type, max_chars=max_chars,
    )
    return FetchedPage(
        url=response.url,
        status_code=response.status_code,
        content_type=content_type,
        title=title,
        text=text,
        sha256=hashlib.sha256(body).hexdigest(),
        bytes=len(body),
        truncated=truncated,
    )


def fetch_url_tool(client: HttpClient) -> Tool:
    """Create a read-only ``fetch_url`` Tool over a host-configured client."""
    if not isinstance(client, HttpClient):
        raise TypeError("client must be an HttpClient")

    @tool(name="fetch_url", side_effect="read_only")
    async def fetch(
        url: str,
        params: dict[str, str] | None = None,
        max_chars: int = 120_000,
    ) -> dict[str, object]:
        """Fetch one allowlisted URL and extract bounded visible text."""
        return (await fetch_url(client, url, params=params, max_chars=max_chars)).as_dict()

    return fetch
