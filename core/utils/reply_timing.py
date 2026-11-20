from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from time import perf_counter


@dataclass(slots=True)
class ReplyTiming:
    """Accumulate timing for one decision-driven reply."""

    decision_response_at: float | None = None
    recall_api_seconds: float = 0.0
    recall_local_query_seconds: float = 0.0


_recall_timing: ContextVar[ReplyTiming | None] = ContextVar(
    "_recall_timing", default=None
)


@contextmanager
def collect_recall_timing(timing: ReplyTiming | None) -> Iterator[None]:
    """Share the reply's timing with the memory operations in this scope.

    Args:
        timing: Timing for the current reply, if a decision model was called.

    Yields:
        Control while memory recall runs.
    """
    token = _recall_timing.set(timing)
    try:
        yield
    finally:
        _recall_timing.reset(token)


@contextmanager
def measure_recall(*, api: bool = False) -> Iterator[None]:
    """Accumulate the elapsed time of one recall operation.

    Args:
        api: Whether the operation calls an external model instead of querying
            local storage.

    Yields:
        Control while the lookup or API call runs.
    """
    timing = _recall_timing.get()
    if timing is None:
        yield
        return

    started = perf_counter()
    try:
        yield
    finally:
        elapsed = perf_counter() - started
        if api:
            timing.recall_api_seconds += elapsed
        else:
            timing.recall_local_query_seconds += elapsed
