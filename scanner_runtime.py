"""Primitives réseau : Token Bucket, budget API, DNS et session aiohttp."""

from __future__ import annotations

import asyncio
import logging
import random
import socket
import time
import urllib.parse
from contextlib import asynccontextmanager

import aiohttp


LOGGER = logging.getLogger("vinted_api_light")


class ApiBudgetExceeded(RuntimeError):
    pass


class ApiCostController:
    """Budget dur partagé par toutes les recherches d'un cycle."""

    def __init__(self, max_requests: int, max_units: float):
        self.max_requests = max(1, int(max_requests))
        self.max_units = max(1.0, float(max_units))
        self.requests = 0
        self.units = 0.0
        self.blocked = 0
        self._lock = asyncio.Lock()

    async def spend(self, units: float = 1.0, kind: str = "catalog") -> None:
        units = max(0.0, float(units))
        async with self._lock:
            if (self.requests + 1 > self.max_requests or
                    self.units + units > self.max_units):
                self.blocked += 1
                raise ApiBudgetExceeded(
                    f"budget API épuisé avant {kind}: "
                    f"{self.requests}/{self.max_requests} requêtes, "
                    f"{self.units:.1f}/{self.max_units:.1f} unités"
                )
            self.requests += 1
            self.units += units

    def snapshot(self) -> dict:
        return {
            "requests": self.requests,
            "max_requests": self.max_requests,
            "units": round(self.units, 2),
            "max_units": self.max_units,
            "blocked": self.blocked,
        }


class TokenBucketRateLimiter:
    """Limiteur à jetons avec jitter et backoff HTTP 429 partagé."""

    def __init__(self, capacity: float, refill_per_second: float,
                 min_jitter: float = 0.0, max_jitter: float = 0.0,
                 max_backoff: float = 60.0):
        self.capacity = max(1.0, float(capacity))
        self.refill_per_second = max(0.05, float(refill_per_second))
        self.tokens = self.capacity
        self.min_jitter = max(0.0, float(min_jitter))
        self.max_jitter = max(self.min_jitter, float(max_jitter))
        self.max_backoff = max(1.0, float(max_backoff))
        self._updated_at = time.monotonic()
        self._blocked_until = 0.0
        self._consecutive_429 = 0
        self._lock = asyncio.Lock()

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._updated_at)
        self.tokens = min(
            self.capacity,
            self.tokens + elapsed * self.refill_per_second,
        )
        self._updated_at = now

    async def wait(self, kind: str = "request", cost: float = 1.0) -> None:
        cost = max(0.05, min(float(cost), self.capacity))
        while True:
            async with self._lock:
                now = time.monotonic()
                self._refill(now)
                backoff_wait = max(0.0, self._blocked_until - now)
                token_wait = max(
                    0.0,
                    (cost - self.tokens) / self.refill_per_second,
                )
                delay = max(backoff_wait, token_wait)
                if delay <= 0:
                    self.tokens -= cost
                    jitter = random.uniform(self.min_jitter, self.max_jitter)
                    break
            await asyncio.sleep(delay)
        if jitter:
            await asyncio.sleep(jitter)

    async def register_response(self, status, headers=None,
                                kind: str = "request") -> float:
        try:
            status = int(status)
        except (TypeError, ValueError):
            return 0.0
        async with self._lock:
            now = time.monotonic()
            if status == 429:
                self._consecutive_429 += 1
                retry_after = self._parse_retry_after(headers) or 0.0
                exponential = 2 ** self._consecutive_429
                delay = min(self.max_backoff, max(retry_after, exponential))
                self._blocked_until = max(self._blocked_until, now + delay)
                self.tokens = 0.0
                LOGGER.warning("HTTP 429 | %s | backoff %.1f s", kind, delay)
                return delay
            if 200 <= status < 400 and now >= self._blocked_until:
                self._consecutive_429 = 0
            return 0.0

    @staticmethod
    def _parse_retry_after(headers):
        if not headers:
            return None
        raw = headers.get("Retry-After") or headers.get("retry-after")
        try:
            return max(0.0, float(str(raw).strip())) if raw is not None else None
        except ValueError:
            return None


async def dns_healthcheck(base_url: str, timeout_seconds: float = 3.0) -> dict:
    host = urllib.parse.urlparse(base_url).hostname
    if not host:
        raise RuntimeError(f"URL sans nom d'hôte: {base_url}")
    started = time.perf_counter()
    loop = asyncio.get_running_loop()
    try:
        answers = await asyncio.wait_for(
            loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM),
            timeout=max(0.5, float(timeout_seconds)),
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise RuntimeError(f"DNS indisponible pour {host}: {exc}") from exc
    addresses = sorted({answer[4][0] for answer in answers if answer[4]})
    return {
        "host": host,
        "addresses": addresses[:4],
        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
    }


@asynccontextmanager
async def managed_http_session(base_url: str, cfg: dict, headers: dict):
    """Session keep-alive avec DNS cache et initialisation bornée."""
    concurrency = max(1, int(cfg.get("api_max_concurrency", 3)))
    connector = aiohttp.TCPConnector(
        limit=max(4, concurrency * 2),
        limit_per_host=concurrency,
        ttl_dns_cache=max(30, int(cfg.get("dns_cache_ttl_seconds", 300))),
        keepalive_timeout=max(5.0, float(cfg.get("keepalive_timeout_seconds", 20))),
        enable_cleanup_closed=True,
    )
    timeout = aiohttp.ClientTimeout(
        total=max(5.0, float(cfg.get("http_total_timeout_seconds", 15))),
        connect=max(2.0, float(cfg.get("http_connect_timeout_seconds", 5))),
    )
    async with aiohttp.ClientSession(
            connector=connector, timeout=timeout, headers=headers) as session:
        attempts = max(1, min(int(cfg.get("session_init_attempts", 2)), 3))
        for attempt in range(1, attempts + 1):
            try:
                async with session.get(
                        base_url + "/",
                        headers={"Accept": "text/html,application/xhtml+xml"},
                        timeout=max(4.0, float(cfg.get("session_init_timeout_seconds", 12))),
                ) as response:
                    LOGGER.info(
                        "Initialisation session HTTP %s (tentative %s/%s)",
                        response.status, attempt, attempts,
                    )
                    if response.status < 500:
                        break
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt == attempts:
                    LOGGER.warning("Initialisation session impossible: %s", exc)
                else:
                    await asyncio.sleep(0.4 * attempt)
        yield session


def run_async(entrypoint) -> str:
    """Utilise uvloop quand disponible, sinon asyncio standard."""
    try:
        import uvloop
    except ImportError:
        asyncio.run(entrypoint())
        return "asyncio"
    uvloop.run(entrypoint())
    return "uvloop"

