import random
from collections.abc import Callable


def backoff_delay(
    attempt: int,
    base: float,
    cap: float,
    retry_after: float | None = None,
    rand: Callable[[], float] = random.random,
) -> float:
    """Full-jitter exponential backoff: uniform in [0, min(cap, base * 2^(attempt-1))].

    Jitter spreads out rows that failed together so they don't retry in lockstep. An upstream
    Retry-After is treated as a floor, never shortened by jitter.
    """
    delay = rand() * min(cap, base * 2 ** (attempt - 1))
    return max(delay, retry_after or 0.0)
