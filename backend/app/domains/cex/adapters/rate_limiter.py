"""Asynchronous Token-Bucket Rate Limiter and Idempotent Order Retry Executor."""

import asyncio
from collections.abc import Awaitable, Callable
import logging
import random
import time
from typing import TypeVar

from app.domains.cex.adapters.base import (
    ExchangeNetworkError,
    ExchangeRateLimitError,
    NormalizedOrderResponse,
    OrderNotFoundError,
)

logger = logging.getLogger(__name__)
T = TypeVar("T")


class AsyncTokenBucketRateLimiter:
    """Thread/coroutine-safe token bucket rate limiter with 429/418 cooldown support."""

    def __init__(
        self,
        capacity: float = 20.0,
        refill_rate_per_sec: float = 10.0,
        time_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if capacity <= 0 or refill_rate_per_sec <= 0:
            raise ValueError("capacity and refill_rate_per_sec must be positive")
        self._capacity = float(capacity)
        self._refill_rate = float(refill_rate_per_sec)
        self._tokens = float(capacity)
        self._time_fn = time_fn
        self._sleep_fn = sleep_fn
        self._last_refill = self._time_fn()
        self._cooldown_until: float = 0.0
        self._lock = asyncio.Lock()
        self._total_acquired_weight: float = 0.0
        self._cooldown_events: int = 0

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._last_refill)
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_rate)
            self._last_refill = now

    async def acquire(self, weight: float = 1.0) -> float:
        """Acquire `weight` tokens, sleeping if necessary. Returns total seconds waited."""
        if weight <= 0:
            return 0.0
        effective_weight = min(float(weight), self._capacity)
        waited = 0.0

        async with self._lock:
            now = self._time_fn()
            if now < self._cooldown_until:
                cooldown_wait = self._cooldown_until - now
                waited += cooldown_wait
                await self._sleep_fn(cooldown_wait)
                now = self._time_fn()

            self._refill(now)
            if self._tokens < effective_weight:
                deficit = effective_weight - self._tokens
                sleep_seconds = deficit / self._refill_rate
                waited += sleep_seconds
                await self._sleep_fn(sleep_seconds)
                now = self._time_fn()
                self._refill(now)

            self._tokens = max(0.0, self._tokens - effective_weight)
            self._total_acquired_weight += effective_weight
            return waited

    def penalize_cooldown(self, seconds: float) -> None:
        """Enforce a global cooldown window after receiving HTTP 429 or 418."""
        if seconds <= 0:
            return
        now = self._time_fn()
        self._cooldown_until = max(self._cooldown_until, now + seconds)
        self._tokens = 0.0
        self._cooldown_events += 1
        logger.warning("Rate limiter entered cooldown for %.2fs", seconds)

    @property
    def available_tokens(self) -> float:
        self._refill(self._time_fn())
        return self._tokens

    @property
    def stats(self) -> dict[str, float | int]:
        return {
            "available_tokens": round(self.available_tokens, 3),
            "total_acquired_weight": round(self._total_acquired_weight, 3),
            "cooldown_events": self._cooldown_events,
        }


async def execute_with_retry(
    operation: Callable[[], Awaitable[T]],
    rate_limiter: AsyncTokenBucketRateLimiter | None = None,
    weight: float = 1.0,
    max_retries: int = 3,
    base_delay_sec: float = 0.25,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Execute a read/idempotent exchange operation with token acquisition and exponential backoff."""
    attempt = 0
    while True:
        if rate_limiter is not None:
            await rate_limiter.acquire(weight)
        try:
            return await operation()
        except ExchangeRateLimitError as exc:
            attempt += 1
            if rate_limiter is not None:
                rate_limiter.penalize_cooldown(exc.retry_after_seconds)
            if attempt > max_retries:
                raise
            delay = max(exc.retry_after_seconds, base_delay_sec * (2 ** (attempt - 1)))
            await sleep_fn(delay)
        except ExchangeNetworkError:
            attempt += 1
            if attempt > max_retries:
                raise
            jitter = random.uniform(0.0, 0.1 * base_delay_sec)
            delay = base_delay_sec * (2 ** (attempt - 1)) + jitter
            await sleep_fn(delay)


async def submit_order_idempotently(
    submit_fn: Callable[[], Awaitable[NormalizedOrderResponse]],
    query_existing_fn: Callable[[], Awaitable[NormalizedOrderResponse]],
    rate_limiter: AsyncTokenBucketRateLimiter | None = None,
    weight: float = 1.0,
    max_retries: int = 3,
    base_delay_sec: float = 0.25,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> NormalizedOrderResponse:
    """Submit an order with clientOrderId reconciliation on transient network timeouts.

    If `submit_fn` fails with a network timeout or rate-limit error, we MUST NOT blindly
    re-POST the order, because the initial packet may have already been matched on the
    exchange. Instead, on retry attempts (`attempt >= 1`), we first call `query_existing_fn`
    using the deterministic `client_order_id`. Only if the exchange confirms `OrderNotFoundError`
    do we re-invoke `submit_fn`.
    """
    attempt = 0
    while True:
        try:
            if attempt > 0:
                if rate_limiter is not None:
                    await rate_limiter.acquire(1.0)
                try:
                    existing = await query_existing_fn()
                    logger.info(
                        "Recovered in-flight order %s (exchange_order_id=%s) on retry attempt %d",
                        existing.client_order_id,
                        existing.exchange_order_id,
                        attempt,
                    )
                    return existing
                except OrderNotFoundError:
                    logger.info("Order not found on exchange after timeout; safe to re-submit (attempt %d)", attempt)

            if rate_limiter is not None:
                await rate_limiter.acquire(weight)
            return await submit_fn()

        except ExchangeRateLimitError as exc:
            attempt += 1
            if rate_limiter is not None:
                rate_limiter.penalize_cooldown(exc.retry_after_seconds)
            if attempt > max_retries:
                raise
            await sleep_fn(exc.retry_after_seconds)

        except ExchangeNetworkError:
            attempt += 1
            if attempt > max_retries:
                raise
            delay = base_delay_sec * (2 ** (attempt - 1))
            await sleep_fn(delay)
