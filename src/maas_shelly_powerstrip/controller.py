"""Power-control logic shared by the JSON API and the HTML UI."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from .config import Settings
from .sampler import OutletState, Sampler
from .shelly import ShellyClientProtocol

log = logging.getLogger("maas_shelly_powerstrip")


class UnknownOutlet(Exception):
    pass


class ShellyUnreachable(Exception):
    pass


class PowerController:
    def __init__(self, settings: Settings, shelly: ShellyClientProtocol, sampler: Sampler):
        self.settings = settings
        self.shelly = shelly
        self.sampler = sampler
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    @property
    def outlets(self) -> list[int]:
        return self.sampler.outlets

    def busy(self, outlet: int) -> bool:
        return self._locks[outlet].locked()

    def check(self, outlet: int) -> None:
        if not self.sampler.has(outlet):
            if self.sampler.last_error:
                raise ShellyUnreachable(self.sampler.last_error)
            raise UnknownOutlet(outlet)

    def state(self, outlet: int) -> OutletState:
        self.check(outlet)
        return self.sampler.state(outlet)

    def states(self) -> list[OutletState]:
        return [self.sampler.state(o) for o in self.outlets]

    async def power_on(self, outlet: int) -> tuple[OutletState, str]:
        self.check(outlet)
        s = self.settings
        async with self._locks[outlet]:
            st = await self.sampler.wait_for_window(outlet)
            if st.output is False:
                log.info("outlet %d: relay open, turning on", outlet)
                await self.shelly.set_switch(outlet, True)
                action = "powered_on"
            elif st.status == "off":
                log.info(
                    "outlet %d: relay closed but idle (%.1f W <= %.1f W for %.0fs), power-cycling",
                    outlet, st.apower or 0.0, s.idle_watt_threshold, st.window_seconds,
                )
                await self.shelly.set_switch(outlet, False)
                await asyncio.sleep(s.cycle_off_seconds)
                await self.shelly.set_switch(outlet, True)
                action = "power_cycled"
            else:
                log.info("outlet %d: already on (%.1f W), nothing to do", outlet, st.apower or 0.0)
                action = "already_on"
            self.sampler.reset(outlet)
            await self.sampler.sample_once()
        return self.sampler.state(outlet), action

    async def power_off(self, outlet: int) -> tuple[OutletState, str]:
        self.check(outlet)
        async with self._locks[outlet]:
            log.info("outlet %d: turning off", outlet)
            await self.shelly.set_switch(outlet, False)
            self.sampler.reset(outlet)
            await self.sampler.sample_once()
        return self.sampler.state(outlet), "powered_off"
