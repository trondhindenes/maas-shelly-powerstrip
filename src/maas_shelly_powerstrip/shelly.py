"""Minimal client for the Shelly Gen2+ RPC API (Switch component)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx


@dataclass(frozen=True)
class SwitchStatus:
    id: int
    output: bool
    apower: float


class ShellyClientProtocol(Protocol):
    async def get_switches(self) -> dict[int, SwitchStatus]: ...
    async def set_switch(self, switch_id: int, on: bool) -> None: ...
    async def aclose(self) -> None: ...


class ShellyClient:
    def __init__(self, host: str, password: str | None = None, timeout: float = 5.0):
        auth = httpx.DigestAuth("admin", password) if password else None
        self._http = httpx.AsyncClient(
            base_url=f"http://{host}", auth=auth, timeout=timeout
        )

    async def _rpc(self, method: str, **params) -> dict:
        resp = await self._http.get(f"/rpc/{method}", params=params)
        resp.raise_for_status()
        return resp.json()

    async def get_switches(self) -> dict[int, SwitchStatus]:
        status = await self._rpc("Shelly.GetStatus")
        switches: dict[int, SwitchStatus] = {}
        for key, value in status.items():
            if not key.startswith("switch:"):
                continue
            sid = int(key.split(":", 1)[1])
            switches[sid] = SwitchStatus(
                id=sid,
                output=bool(value.get("output", False)),
                apower=float(value.get("apower") or 0.0),
            )
        return switches

    async def set_switch(self, switch_id: int, on: bool) -> None:
        await self._rpc("Switch.Set", id=switch_id, on="true" if on else "false")

    async def aclose(self) -> None:
        await self._http.aclose()
