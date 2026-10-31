"""JSON-over-HTTP access for market-data vendors: retries, throttling, paging, redaction.

The network is behind an injectable ``Transport`` (a function from URL and timeout to an
``HttpResponse``), so every behaviour here is tested against scripted responses and no
test or CI run ever calls a vendor. The default transport uses only the standard library.

Status handling:

* 2xx with a JSON object body: returned.
* 429 and 5xx, timeouts and connection errors: :class:`TransientProviderError`, retried
  with exponential backoff and jitter. A ``Retry-After`` header (seconds) is honoured as a
  minimum delay.
* 401 and 403: :class:`VendorAuthError` (a key, plan or entitlement problem; never
  retried).
* Other 4xx, and a body that is not a JSON object: permanent errors.

The API key is added to every request, including ``next_url`` pages, and is replaced by
``***`` in every error message.
"""

from __future__ import annotations

import http.client
import json
import random
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from quantlab.data.providers import (
    PermanentProviderError,
    ProviderError,
    RetryPolicy,
    TokenBucket,
    TransientProviderError,
)


class VendorAuthError(PermanentProviderError):
    """The vendor rejected the credentials or the plan does not cover the request."""


class VendorSchemaError(PermanentProviderError):
    """A response does not match the vendor's documented schema."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


# (url, timeout, request headers) -> response
Transport = Callable[[str, float, Mapping[str, str]], HttpResponse]
# Larger than any documented page (aggregates: 50,000 bars), small enough to be safe.
MAX_RESPONSE_BYTES = 64 * 2**20


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the API key (in the URL or a header) would go along.

    Returning None makes urllib surface the 3xx as an ``HTTPError``, which becomes a
    permanent error below.
    """

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_RefuseRedirects)


def urllib_transport(
    url: str, timeout: float, headers: Mapping[str, str] | None = None
) -> HttpResponse:
    """Plain HTTPS GET with the standard library, never following redirects.

    Network errors become transient errors, except those no retry can fix (an invalid
    URL, a certificate that fails verification), which are permanent. Errors are raised
    outside the ``except`` block with the URL removed from their text, so neither the
    message nor the chained ``__context__`` carries the API key.
    """
    if not url.startswith("https://"):
        raise PermanentProviderError("vendor URLs must use https")
    req = urllib.request.Request(  # noqa: S310 - https only, checked above
        url, headers={"Accept": "application/json", **(headers or {})}
    )
    failure: tuple[type[ProviderError], str] | None = None
    try:
        with _OPENER.open(req, timeout=timeout) as resp:  # https only, no redirects
            return HttpResponse(resp.status, dict(resp.headers), _read(resp))
    except urllib.error.HTTPError as exc:
        try:
            body = _read(exc)
        except (OSError, http.client.HTTPException, TransientProviderError):
            body = b""
        return HttpResponse(exc.code, dict(exc.headers or {}), body)
    except (OSError, http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", exc)  # URLError wraps the underlying error
        kind: type[ProviderError] = (
            PermanentProviderError
            if isinstance(exc, http.client.InvalidURL)
            or isinstance(reason, ssl.SSLCertVerificationError)
            else TransientProviderError
        )
        text = f"{type(reason).__name__}: {reason}"
        failure = (kind, text.replace(url, "<url>").replace(url.split("?", 1)[-1], "<query>"))
    assert failure is not None
    raise failure[0](f"network error: {failure[1]}")


def _read(resp: Any) -> bytes:
    """The response body, refusing anything larger than ``MAX_RESPONSE_BYTES``."""
    body: bytes = resp.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise PermanentProviderError(f"response larger than {MAX_RESPONSE_BYTES} bytes")
    # read(n) on http.client returns a short body at early EOF instead of raising
    # IncompleteRead; unread Content-Length means the body was truncated.
    if (getattr(resp, "length", None) or 0) > 0:
        raise TransientProviderError("response body truncated before Content-Length")
    return body


@dataclass
class JsonApi:
    """A vendor's JSON REST API: one base URL and one API key.

    ``auth="bearer"`` sends the key as ``Authorization: Bearer <key>``, so it never
    appears in a URL (logs, proxies, error text); ``"query"`` adds it as ``key_param``.
    Use the header whenever the vendor documents it.
    """

    base_url: str
    api_key: str = field(repr=False)
    key_param: str = "apiKey"
    auth: Literal["query", "bearer"] = "query"
    transport: Transport = urllib_transport
    limiter: TokenBucket | None = None
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    sleep: Callable[[float], None] = time.sleep
    timeout: float = 30.0
    max_pages: int = 10_000
    seed: int = 0
    stats: dict[str, int] = field(default_factory=lambda: {"requests": 0, "retries": 0})

    def __post_init__(self) -> None:
        if not self.api_key:
            raise VendorAuthError("no API key configured")
        self._rng = random.Random(self.seed)  # noqa: S311 - backoff jitter, not crypto

    def _redact(self, text: str) -> str:
        # The key also appears URL-encoded in request URLs (e.g. "+" as "%2B").
        for form in {
            self.api_key,
            urllib.parse.quote(self.api_key, safe=""),
            urllib.parse.quote_plus(self.api_key),
        }:
            text = text.replace(form, "***")
        return text

    def _url(self, path_or_url: str, params: Mapping[str, Any] | None = None) -> str:
        url = path_or_url if "://" in path_or_url else self.base_url.rstrip("/") + path_or_url
        parts = urllib.parse.urlsplit(url)
        base = urllib.parse.urlsplit(self.base_url)
        if (parts.scheme, parts.netloc) != (base.scheme, base.netloc):
            # A next_url must stay on the vendor's host; never send the key elsewhere.
            raise VendorSchemaError(f"unexpected host in {parts.scheme}://{parts.netloc}")
        query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        query.update({k: str(v) for k, v in (params or {}).items() if v is not None})
        if self.auth == "query":
            query[self.key_param] = self.api_key
        elif self.key_param in query:  # a next_url must not carry a key either
            raise VendorSchemaError(f"unexpected {self.key_param!r} in a request URL")
        return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))

    def get(self, path_or_url: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        url = self._url(path_or_url, params)
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.auth == "bearer" else {}
        attempt = 0
        while True:
            attempt += 1
            if self.limiter is not None:
                self.limiter.acquire()
            self.stats["requests"] += 1
            delay_floor = 0.0
            try:
                resp = self.transport(url, self.timeout, headers)
                if resp.status == 429 or resp.status >= 500:
                    delay_floor = _retry_after(resp.headers)
                    raise TransientProviderError(f"HTTP {resp.status}")
                return self._decode(resp)
            except TransientProviderError as exc:
                if attempt >= self.retry.max_attempts:
                    raise TransientProviderError(
                        self._redact(f"{exc} after {attempt} attempts: {url}")
                    ) from None
                self.stats["retries"] += 1
                self.sleep(max(delay_floor, self.retry.delay(attempt, self._rng)))

    def _decode(self, resp: HttpResponse) -> dict[str, Any]:
        if resp.status in (401, 403):
            raise VendorAuthError(f"HTTP {resp.status}: check the API key and plan entitlements")
        if not 200 <= resp.status < 300:
            snippet = self._redact(resp.body[:200].decode("utf-8", "replace"))
            raise PermanentProviderError(f"HTTP {resp.status}: {snippet}")
        try:
            data = json.loads(resp.body)
        except ValueError as exc:
            raise VendorSchemaError("response body is not JSON") from exc
        if not isinstance(data, dict):
            raise VendorSchemaError("response body is not a JSON object")
        return data

    def pages(self, path: str, params: Mapping[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        """Yield each page, following ``next_url`` until it is absent."""
        url: str | None = path
        first = True
        seen: set[str] = set()
        for _ in range(self.max_pages):
            assert url is not None
            page = self.get(url, params if first else None)
            yield page
            first = False
            nxt = page.get("next_url")
            if not nxt:
                return
            if not isinstance(nxt, str) or nxt in seen:
                raise VendorSchemaError("invalid or repeating next_url")
            seen.add(nxt)
            url = nxt
        raise VendorSchemaError(f"more than {self.max_pages} pages")


def _retry_after(headers: Mapping[str, str]) -> float:
    for k, v in headers.items():
        if k.lower() == "retry-after":
            try:
                return max(0.0, min(float(v), 300.0))
            except ValueError:
                return 0.0
    return 0.0
