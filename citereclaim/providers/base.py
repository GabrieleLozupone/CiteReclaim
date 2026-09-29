"""Shared HTTP machinery: caching, per-provider rate limiting, retries, typed errors."""

from __future__ import annotations

import email.utils
import hashlib
import json
import logging
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import Settings
from ..db import Database

log = logging.getLogger(__name__)


class ProviderError(Exception):
    """Base error for provider failures."""

    def __init__(self, provider: str, message: str, status: int | None = None):
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.status = status


class ProviderUnavailable(ProviderError):
    """Network failure, 5xx after retries, or offline mode cache miss."""


class PermissionDenied(ProviderError):
    """401/403: credentials missing, invalid or not entitled. Never retried aggressively."""


class RateLimited(ProviderError):
    """429 after exhausting retries (or quota exhausted)."""


@dataclass
class Response:
    status: int
    body: bytes
    from_cache: bool
    url: str

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8")) if self.body else None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class RateLimiter:
    """Minimum interval between requests with random jitter (single-process)."""

    def __init__(
        self,
        min_interval: float,
        jitter: float = 0.1,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.min_interval = min_interval
        self.jitter = jitter
        self._clock = clock
        self._sleep = sleep
        self._next_allowed = 0.0

    def wait(self) -> float:
        now = self._clock()
        delay = max(0.0, self._next_allowed - now)
        if delay > 0:
            self._sleep(delay)
        self._next_allowed = (
            max(now, self._next_allowed)
            + self.min_interval
            + random.uniform(0, self.jitter * self.min_interval)
        )
        return delay

    def penalize(self, seconds: float) -> None:
        """Push the next slot out (e.g. after a 429)."""
        self._next_allowed = max(self._next_allowed, self._clock() + seconds)


def parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, dt.timestamp() - (now if now is not None else time.time()))


SECRET_PARAMS = {"api_key", "apikey", "insttoken", "mailto"}


def cache_key(provider: str, method: str, url: str, params: Mapping[str, Any] | None) -> str:
    clean = sorted((k, str(v)) for k, v in (params or {}).items() if k.lower() not in SECRET_PARAMS)
    raw = json.dumps([provider, method.upper(), url, clean], separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


class HttpClient:
    """HTTP client for one provider.

    - Responses (2xx and 404) are cached in SQLite with a caller-supplied TTL.
    - 429 / 5xx / transport errors: exponential backoff with jitter, honouring Retry-After.
    - 401 / 403: raised immediately as :class:`PermissionDenied` (no retry storm).
    """

    RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        provider: str,
        settings: Settings,
        db: Database,
        *,
        min_interval: float,
        max_retries: int = 4,
        backoff_base: float = 1.0,
        backoff_cap: float = 60.0,
        max_retry_after: float = 120.0,
        headers: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.provider = provider
        self.settings = settings
        self.db = db
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.max_retry_after = max_retry_after
        self._sleep = sleep
        self.limiter = RateLimiter(min_interval, sleep=sleep)
        self.requests_made = 0
        self.cache_hits = 0
        self.client = httpx.Client(
            headers={
                "User-Agent": settings.user_agent(),
                "Accept": "application/json",
                **(headers or {}),
            },
            timeout=settings.http_timeout,
            follow_redirects=True,
            transport=transport,
        )

    def close(self) -> None:
        self.client.close()

    def _backoff(self, attempt: int) -> float:
        delay = min(self.backoff_cap, self.backoff_base * (2**attempt))
        return delay + random.uniform(0, delay * 0.25)

    def get(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        *,
        ttl: float,
        headers: Mapping[str, str] | None = None,
        use_cache: bool = True,
    ) -> Response:
        key = cache_key(self.provider, "GET", url, params)
        if use_cache and not self.settings.refresh:
            cached = self.db.cache_get(key)
            if cached and (cached[2] > time.time() or self.settings.offline):
                self.cache_hits += 1
                return Response(cached[0], cached[1], True, url)
        if self.settings.offline:
            raise ProviderUnavailable(
                self.provider, f"offline mode and no cached response for {url}"
            )

        last_error: str = ""
        for attempt in range(self.max_retries + 1):
            self.limiter.wait()
            try:
                self.requests_made += 1
                resp = self.client.get(url, params=params, headers=headers)
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.max_retries:
                    self._sleep(self._backoff(attempt))
                    continue
                raise ProviderUnavailable(self.provider, last_error) from exc

            status = resp.status_code
            if status in (401, 403):
                raise PermissionDenied(self.provider, _error_text(resp), status)
            if 200 <= status < 300 or status == 404:
                if use_cache:
                    ttl_used = ttl if status != 404 else min(ttl, self.settings.ttl.not_found)
                    self.db.cache_put(
                        key, self.provider, str(resp.url), status, resp.content, ttl_used
                    )
                return Response(status, resp.content, False, str(resp.url))
            if status in self.RETRY_STATUSES:
                retry_after = parse_retry_after(resp.headers.get("Retry-After"))
                last_error = f"HTTP {status}: {_error_text(resp)}"
                if attempt >= self.max_retries or (
                    retry_after is not None and retry_after > self.max_retry_after
                ):
                    break
                delay = retry_after if retry_after is not None else self._backoff(attempt)
                log.info("%s: HTTP %s, retrying in %.1fs", self.provider, status, delay)
                self.limiter.penalize(delay)
                continue
            # Other 4xx: client error, do not retry.
            return Response(status, resp.content, False, str(resp.url))

        if "429" in last_error:
            raise RateLimited(self.provider, last_error, 429)
        raise ProviderUnavailable(self.provider, last_error)


def _error_text(resp: httpx.Response) -> str:
    text = resp.text[:300].strip().replace("\n", " ")
    return text or resp.reason_phrase
