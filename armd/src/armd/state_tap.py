"""Bounded, loss-aware fan-out ring for HardwareLoop measured states."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar


class SequencedSample(Protocol):
    sequence: int


SampleT = TypeVar("SampleT", bound=SequencedSample)


@dataclass(frozen=True, slots=True)
class StateTapStats:
    capacity: int
    size: int
    oldest_sequence: int
    newest_sequence: int
    overwritten_samples_total: int
    closed: bool


class StateTapDataLoss(RuntimeError):
    def __init__(
        self,
        *,
        requested_sequence: int,
        oldest_available_sequence: int,
        source: str = "measured-state",
    ) -> None:
        self.requested_sequence = requested_sequence
        self.oldest_available_sequence = oldest_available_sequence
        self.source = source
        super().__init__(
            f"{source} tap data loss: requested sequence {requested_sequence}, "
            f"oldest available {oldest_available_sequence}"
        )


class StateTap(Generic[SampleT]):
    """One-producer, multi-reader fixed-capacity sequence ring.

    Publishing is bounded O(1). Readers hold independent sequence cursors and
    receive an explicit ``StateTapDataLoss`` if they fall behind retention.
    """

    def __init__(self, capacity: int = 4096) -> None:
        if capacity <= 0:
            raise ValueError("state tap capacity must be positive")
        self.capacity = int(capacity)
        self._samples: deque[SampleT] = deque()
        self._condition = threading.Condition()
        self._overwritten_samples_total = 0
        self._closed = False

    def publish(self, sample: SampleT) -> None:
        with self._condition:
            if self._closed:
                return
            if self._samples and sample.sequence != self._samples[-1].sequence + 1:
                raise ValueError(
                    "state tap sequence must be contiguous: "
                    f"latest={self._samples[-1].sequence}, new={sample.sequence}"
                )
            if len(self._samples) == self.capacity:
                self._samples.popleft()
                self._overwritten_samples_total += 1
            self._samples.append(sample)
            self._condition.notify_all()

    def read_after(self, after_sequence: int, timeout: float | None = None) -> SampleT | None:
        if after_sequence < 0:
            raise ValueError("after_sequence cannot be negative")
        if timeout is not None and timeout < 0:
            raise ValueError("timeout cannot be negative")
        deadline = None if timeout is None else time.monotonic() + timeout
        requested_sequence = after_sequence + 1
        with self._condition:
            while True:
                if self._samples:
                    oldest = self._samples[0].sequence
                    newest = self._samples[-1].sequence
                    if requested_sequence < oldest:
                        raise StateTapDataLoss(
                            requested_sequence=requested_sequence,
                            oldest_available_sequence=oldest,
                        )
                    if requested_sequence <= newest:
                        sample = self._samples[requested_sequence - oldest]
                        if sample.sequence != requested_sequence:
                            raise StateTapDataLoss(
                                requested_sequence=requested_sequence,
                                oldest_available_sequence=oldest,
                            )
                        return sample
                if self._closed:
                    return None
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)

    def get(self, sequence: int) -> SampleT | None:
        """Return one retained sequence without advancing any reader cursor."""
        if sequence <= 0:
            return None
        with self._condition:
            if not self._samples:
                return None
            oldest = self._samples[0].sequence
            newest = self._samples[-1].sequence
            if sequence < oldest or sequence > newest:
                return None
            sample = self._samples[sequence - oldest]
            return sample if sample.sequence == sequence else None

    def stats(self) -> StateTapStats:
        with self._condition:
            return StateTapStats(
                capacity=self.capacity,
                size=len(self._samples),
                oldest_sequence=self._samples[0].sequence if self._samples else 0,
                newest_sequence=self._samples[-1].sequence if self._samples else 0,
                overwritten_samples_total=self._overwritten_samples_total,
                closed=self._closed,
            )

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
