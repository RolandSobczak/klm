"""Talking to suppliers politely, and surviving them being down (docs/07 §4).

Three things wrap every supplier request, and they solve three different
problems:

* **The disk cache** exists because an offer is a cache of a remote fact whose
  acceptable age depends entirely on what you are doing with it — 30 days while
  browsing, one hour while committing to an order. TTL is therefore the
  caller's decision, per request, not a property of this module.
* **The token bucket** exists because klm's natural access pattern is bursty
  (refresh a 200-line BOM) and a supplier's is not. It defaults well below any
  published limit; being throttled is a self-inflicted outage.
* **The circuit breaker** exists because retrying into a supplier that is down
  turns their bad minute into klm's bad afternoon. After enough consecutive
  failures it opens and fails fast, and callers degrade to cache.

Clock, sleep and transport are all injected. A rate limiter you cannot test
without waiting is a rate limiter that does not get tested.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from klm.suppliers.base import SupplierUnavailable

__all__ = [
    "CacheEntry",
    "CachedHttp",
    "CircuitBreaker",
    "HttpResponse",
    "TokenBucket",
    "Transport",
    "urllib_transport",
]

DEFAULT_TIMEOUT = 20.0
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
USER_AGENT = "klm/0.1 (+https://github.com/RolandSobczak/klm)"


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> object:
        return json.loads(self.body)


Transport = Callable[[str, str, bytes | None, Mapping[str, str], float], HttpResponse]
"""``(method, url, body, headers, timeout) -> HttpResponse``."""


def urllib_transport(
    method: str, url: str, body: bytes | None, headers: Mapping[str, str], timeout: float
) -> HttpResponse:
    """The real transport: the standard library, no dependency added.

    An HTTP error status is a response, not an exception — the retry and
    circuit-breaker logic above needs the status code to decide, and cannot if
    urllib has already thrown it away.
    """
    request = urllib.request.Request(url, data=body, method=method)
    for key, value in {"User-Agent": USER_AGENT, **headers}.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return HttpResponse(int(response.status), response.read().decode(charset, "replace"))
    except urllib.error.HTTPError as exc:
        return HttpResponse(int(exc.code), exc.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SupplierUnavailable(f"{url}: {exc}") from exc


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


@dataclass
class TokenBucket:
    """Classic token bucket: ``rate`` tokens per second, ``capacity`` of burst."""

    rate: float
    capacity: float
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    _tokens: float = field(init=False, default=0.0)
    _last: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        if self.rate <= 0 or self.capacity <= 0:
            raise ValueError("rate and capacity must be positive")
        self._tokens = self.capacity
        self._last = self.clock()

    def acquire(self, tokens: float = 1.0) -> float:
        """Take ``tokens``, sleeping if they are not yet available.

        Returns how long it waited, which is what makes "klm is slow" and
        "the supplier is slow" distinguishable in a diagnostic bundle.
        """
        if tokens > self.capacity:
            raise ValueError(f"cannot acquire {tokens} tokens from a bucket of {self.capacity}")
        self._refill()
        waited = 0.0
        if self._tokens < tokens:
            waited = (tokens - self._tokens) / self.rate
            self.sleep(waited)
            self._refill()
        self._tokens -= tokens
        return waited

    def _refill(self) -> None:
        now = self.clock()
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


@dataclass
class CircuitBreaker:
    """Fail fast while a supplier is down, then probe once to see if it is back.

    Two states matter and the third is the interesting one: ``closed`` passes
    everything, ``open`` refuses immediately, and after ``reset_after`` seconds
    a single request is let through. If it succeeds the breaker closes; if it
    fails the timer restarts. One probe, not a thundering herd.
    """

    threshold: int = 5
    reset_after: float = 300.0
    clock: Callable[[], float] = time.monotonic
    _failures: int = field(init=False, default=0)
    _opened_at: float | None = field(init=False, default=None)

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        return (self.clock() - self._opened_at) < self.reset_after

    def check(self, supplier: str) -> None:
        """Raise if the breaker is open."""
        if self.is_open:
            remaining = self.reset_after - (self.clock() - (self._opened_at or 0.0))
            raise SupplierUnavailable(
                f"{supplier} is in degraded mode after {self._failures} failures; "
                f"retrying in {remaining:.0f}s"
            )

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.threshold:
            self._opened_at = self.clock()


# ---------------------------------------------------------------------------
# Disk cache
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CacheEntry:
    fetched_at: float
    status: int
    body: str

    def age(self, now: float) -> float:
        return max(0.0, now - self.fetched_at)


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


class CachedHttp:
    """A rate-limited, cached, circuit-broken HTTP client for one supplier."""

    def __init__(
        self,
        supplier: str,
        cache_dir: Path,
        *,
        transport: Transport = urllib_transport,
        bucket: TokenBucket | None = None,
        breaker: CircuitBreaker | None = None,
        offline: bool = False,
        max_retries: int = 3,
        timeout: float = DEFAULT_TIMEOUT,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = lambda: 0.0,
    ) -> None:
        self.supplier = supplier
        self.cache_dir = cache_dir / supplier
        self.transport = transport
        self.bucket = bucket or TokenBucket(rate=2.0, capacity=4.0)
        self.breaker = breaker or CircuitBreaker()
        self.offline = offline
        self.max_retries = max_retries
        self.timeout = timeout
        self.clock = clock
        self.sleep = sleep
        self.jitter = jitter

    # -- cache ---------------------------------------------------------

    def cache_key(self, method: str, url: str, body: bytes | None) -> str:
        digest = hashlib.sha256()
        for part in (method, url):
            digest.update(part.encode("utf-8"))
            digest.update(b"\0")
        digest.update(body or b"")
        return digest.hexdigest()

    def _cache_path(self, key: str) -> Path:
        # Two levels, like the asset store: a flat directory of ten thousand
        # files is slow to list on every filesystem that matters.
        return self.cache_dir / key[:2] / f"{key}.json"

    def read_cache(self, key: str) -> CacheEntry | None:
        path = self._cache_path(key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return CacheEntry(float(data["fetched_at"]), int(data["status"]), str(data["body"]))
        except (OSError, ValueError, KeyError, TypeError):
            # A corrupt cache entry is a cache miss. It is not worth an error:
            # the authoritative copy is one request away.
            return None

    def write_cache(self, key: str, response: HttpResponse) -> None:
        path = self._cache_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"fetched_at": self.clock(), "status": response.status, "body": response.body}
        path.write_text(json.dumps(payload), encoding="utf-8")

    # -- requests ------------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        ttl: float | None = None,
        force: bool = False,
    ) -> HttpResponse:
        """Fetch ``url``, serving from cache when the entry is younger than ``ttl``.

        ``ttl`` of ``None`` means "do not use the cache for reading" — the
        caller wants the current truth. It is still written, so a later request
        with a generous TTL benefits.
        """
        key = self.cache_key(method, url, body)
        cached = self.read_cache(key)

        fresh_enough = (
            cached is not None
            and not force
            and ttl is not None
            and cached.age(self.clock()) <= ttl
        )
        if cached is not None and fresh_enough:
            return HttpResponse(cached.status, cached.body)

        if self.offline:
            if cached is not None:
                return HttpResponse(cached.status, cached.body)
            raise SupplierUnavailable(f"{self.supplier}: offline and not in cache: {url}")

        try:
            response = self._fetch(method, url, body, headers or {})
        except SupplierUnavailable:
            # Stale beats absent. The caller is told how stale by the
            # `fetched_at` on whatever offer it builds from this.
            if cached is not None:
                return HttpResponse(cached.status, cached.body)
            raise

        self.write_cache(key, response)
        return response

    def _fetch(
        self, method: str, url: str, body: bytes | None, headers: Mapping[str, str]
    ) -> HttpResponse:
        self.breaker.check(self.supplier)

        last: HttpResponse | None = None
        for attempt in range(self.max_retries):
            self.bucket.acquire()
            try:
                response = self.transport(method, url, body, headers, self.timeout)
            except SupplierUnavailable:
                self.breaker.record_failure()
                if attempt == self.max_retries - 1:
                    raise
                self.sleep(self._backoff(attempt))
                continue

            if response.ok or response.status not in RETRYABLE_STATUS:
                # A 404 is an answer. Only the retryable statuses count against
                # the breaker; a malformed query must not open it.
                self.breaker.record_success()
                return response

            last = response
            self.breaker.record_failure()
            if attempt < self.max_retries - 1:
                self.sleep(self._backoff(attempt))

        status = last.status if last else 0
        raise SupplierUnavailable(
            f"{self.supplier}: {url} returned {status} after {self.max_retries} attempts"
        )

    def _backoff(self, attempt: int) -> float:
        """Exponential, with jitter so parallel refreshes do not resynchronise."""
        return float(2**attempt) + self.jitter()
