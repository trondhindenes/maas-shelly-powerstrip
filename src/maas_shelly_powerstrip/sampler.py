"""Background sampler that keeps a short history of per-outlet power readings."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Literal

from .shelly import ShellyClientProtocol, SwitchStatus

log = logging.getLogger(__name__)

PowerState = Literal["on", "off", "unknown"]


@dataclass(frozen=True)
class Sample:
    ts: float
    output: bool
    apower: float


@dataclass(frozen=True)
class OutletState:
    outlet: int
    status: PowerState
    output: bool | None
    apower: float | None
    window_seconds: float


class Sampler:
    def __init__(
        self,
        client: ShellyClientProtocol,
        *,
        interval: float,
        window: float,
        threshold: float,
        clock=time.monotonic,
    ):
        self._client = client
        self._interval = interval
        self._window = window
        self._threshold = threshold
        self._clock = clock
        # Keep a little more than the window so we can always span it.
        self._maxlen = max(2, int(window / interval) + 3)
        self._history: dict[int, deque[Sample]] = {}
        self._task: asyncio.Task | None = None
        self._updated = asyncio.Event()
        self.last_error: str | None = None

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        await self.sample_once()
        self._task = asyncio.create_task(self._run(), name="shelly-sampler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self.sample_once()

    async def sample_once(self) -> None:
        try:
            switches = await self._client.get_switches()
        except Exception as exc:  # noqa: BLE001
            self.last_error = repr(exc)
            log.warning("sampling shelly failed: %s", exc)
            return
        now = self._clock()
        for sid, sw in switches.items():
            self.record(sid, sw, now)
        self.last_error = None
        self._updated.set()
        self._updated.clear()

    def record(self, sid: int, sw: SwitchStatus, ts: float | None = None) -> None:
        ts = self._clock() if ts is None else ts
        hist = self._history.setdefault(sid, deque(maxlen=self._maxlen))
        hist.append(Sample(ts=ts, output=sw.output, apower=sw.apower))

    def reset(self, sid: int) -> None:
        """Forget history for an outlet, e.g. after we toggled its relay."""
        self._history.pop(sid, None)

    # -- queries -------------------------------------------------------------

    @property
    def outlets(self) -> list[int]:
        return sorted(self._history)

    def has(self, sid: int) -> bool:
        return sid in self._history

    def state(self, sid: int) -> OutletState:
        """Derive the effective power state of the PC attached to an outlet.

        - relay open                      -> "off"
        - relay closed, usage above the threshold at any point in the window -> "on"
        - relay closed, usage at/below threshold for the whole window        -> "off"
        - relay closed, but not enough history yet to fill the window        -> "on"
          (optimistic; a freshly powered PC draws current almost immediately)
        """
        hist = self._history.get(sid)
        if not hist:
            return OutletState(sid, "unknown", None, None, 0.0)
        latest = hist[-1]
        if not latest.output:
            return OutletState(sid, "off", False, latest.apower, 0.0)

        # Walk back over the contiguous run of relay-closed samples.
        run = []
        for s in reversed(hist):
            if not s.output:
                break
            run.append(s)
        span = latest.ts - run[-1].ts
        if any(s.apower > self._threshold for s in run):
            return OutletState(sid, "on", True, latest.apower, span)
        if span >= self._window:
            return OutletState(sid, "off", True, latest.apower, span)
        return OutletState(sid, "on", True, latest.apower, span)

    async def wait_for_window(self, sid: int, timeout: float | None = None) -> OutletState:
        """Wait until the relay-closed history spans the full window (or usage is
        clearly above threshold), so a decision can be made with confidence."""
        deadline = self._clock() + (timeout if timeout is not None else self._window + 2 * self._interval)
        while True:
            st = self.state(sid)
            if st.output is False or st.status == "unknown":
                return st
            if st.window_seconds >= self._window or st.apower is not None and st.apower > self._threshold:
                return st
            if self._clock() >= deadline:
                return st
            try:
                await asyncio.wait_for(self._updated.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass
