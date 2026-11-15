"""Cooperative limits at durable dispatch boundaries, never mid-request."""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import math
import time


class WorkerPaused(OSError):
    """No new dispatch was started; existing attempts and caches remain intact."""


@dataclass
class Slice:
    max_dispatches: int | None = None
    max_seconds: float | None = None
    started: float = field(default_factory=time.monotonic)
    dispatches: int = 0

    def __post_init__(self):
        if self.max_dispatches is not None and (type(self.max_dispatches) is not int or self.max_dispatches < 1):
            raise ValueError("max_dispatches must be a positive integer")
        if self.max_seconds is not None and (isinstance(self.max_seconds, bool)
                or not math.isfinite(self.max_seconds) or self.max_seconds <= 0):
            raise ValueError("max_seconds must be positive and finite")

    def checkpoint(self):
        if self.max_dispatches is not None and self.dispatches >= self.max_dispatches:
            raise WorkerPaused("Dispatch slice exhausted; resume the same task without another start")
        if self.max_seconds is not None and time.monotonic() - self.started >= self.max_seconds:
            raise WorkerPaused("Time slice exhausted before dispatch; resume the same task without another start")
        self.dispatches += 1


ACTIVE = ContextVar("podcast_worker_slice", default=None)


def checkpoint():
    budget = ACTIVE.get()
    if budget is not None:
        budget.checkpoint()


@contextmanager
def limit(max_dispatches=None, max_seconds=None):
    budget = Slice(max_dispatches, max_seconds)
    token = ACTIVE.set(budget)
    try:
        yield budget
    finally:
        ACTIVE.reset(token)
