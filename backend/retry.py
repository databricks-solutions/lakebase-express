"""Shared transient-retry policy.

Holds the mechanism (attempts, backoff, logging); each driver classifies its own
errors via a ``transient_reason`` function, since only it can tell a resuming
database from a bad password.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

T = TypeVar("T")

# Short reason when the error is worth retrying, else None.
TransientCheck = Callable[[BaseException], "str | None"]


@dataclass(frozen=True)
class RetryPolicy:
    """``backoff`` holds the waits *between* attempts (one shorter than
    ``attempts``; the last value repeats). ``jitter`` adds up to that fraction at
    random, so concurrent retries don't land in lockstep."""

    attempts: int
    backoff: tuple[float, ...]
    jitter: float = 0.0

    def delay(self, attempt: int) -> float:
        base = self.backoff[min(attempt - 1, len(self.backoff) - 1)]
        return base * (1.0 + self.jitter * random.random()) if self.jitter else base


# Waits cover a ~30-60s serverless resume.
DB_POLICY = RetryPolicy(attempts=4, backoff=(5.0, 10.0, 20.0))

# Rate limits clear in seconds; jitter because translate_all fans workers onto one endpoint.
FM_POLICY = RetryPolicy(attempts=4, backoff=(2.0, 6.0, 15.0), jitter=0.25)

# Fewer attempts: each one re-copies the whole table, and source *connect* failures
# were already retried inside the connector. This covers mid-stream drops.
LOAD_POLICY = RetryPolicy(attempts=3, backoff=(5.0, 15.0))


def call_with_retry(
    fn: Callable[[], T],
    *,
    policy: RetryPolicy,
    transient: TransientCheck,
    what: str,
    log: logging.Logger,
    before_retry: Callable[[], None] | None = None,
) -> T:
    """Call ``fn``, retrying only what ``transient`` recognises. Everything else —
    and the last attempt — raises the original exception. ``before_retry`` runs
    before each wait, for callers that must reset state first.
    """
    for attempt in range(1, policy.attempts + 1):
        try:
            return fn()
        except Exception as exc:
            reason = transient(exc)
            if reason is None or attempt == policy.attempts:
                raise
            delay = policy.delay(attempt)
            log.warning(
                "Transient failure on %s (%s) — retrying in %.1fs (attempt %d/%d)",
                what, reason, delay, attempt, policy.attempts,
            )
            if before_retry is not None:
                before_retry()
            time.sleep(delay)
    raise AssertionError("unreachable")
