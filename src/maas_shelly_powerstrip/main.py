"""FastAPI app exposing MAAS "webhook" power-driver endpoints for a Shelly strip,
plus a small HTMX web UI at "/".

MAAS webhook power type:
  power_on_uri    POST  http://<host>:<port>/outlets/<n>/on
  power_off_uri   POST  http://<host>:<port>/outlets/<n>/off
  power_query_uri GET   http://<host>:<port>/outlets/<n>/status
  power_on_regex  status.*\\bon\\b      (MAAS default, matches our JSON)
  power_off_regex status.*\\boff\\b     (MAAS default, matches our JSON)
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import Settings
from .controller import PowerController, ShellyUnreachable, UnknownOutlet
from .sampler import OutletState, Sampler
from .shelly import ShellyClient, ShellyClientProtocol
from .ui import router as ui_router

log = logging.getLogger("maas_shelly_powerstrip")
HERE = Path(__file__).parent


class OutletResponse(BaseModel):
    outlet: int
    status: str
    output: bool | None
    apower: float | None
    action: str | None = None

    @classmethod
    def from_state(cls, st: OutletState, action: str | None = None) -> "OutletResponse":
        return cls(outlet=st.outlet, status=st.status, output=st.output, apower=st.apower, action=action)


async def require_auth(request: Request) -> None:
    token = request.app.state.settings.api_token
    if not token:
        return
    header = request.headers.get("authorization", "")
    if header != f"Bearer {token}":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing bearer token")


def get_controller(request: Request) -> PowerController:
    return request.app.state.controller


def create_app(settings: Settings | None = None, client: ShellyClientProtocol | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        logging.getLogger("httpx").setLevel(logging.WARNING)
        shelly = client or ShellyClient(settings.shelly_host, settings.shelly_password, settings.shelly_timeout)
        sampler = Sampler(
            shelly,
            interval=settings.sample_interval,
            window=settings.idle_window_seconds,
            threshold=settings.idle_watt_threshold,
        )
        await sampler.start()
        if sampler.last_error:
            log.warning("could not reach shelly at %s on startup: %s", settings.shelly_host, sampler.last_error)
        else:
            log.info("connected to shelly at %s, outlets: %s", settings.shelly_host, sampler.outlets)
        app.state.settings = settings
        app.state.controller = PowerController(settings, shelly, sampler)
        try:
            yield
        finally:
            await sampler.stop()
            await shelly.aclose()

    app = FastAPI(title="MAAS Shelly Power Strip", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.include_router(ui_router)

    @app.exception_handler(UnknownOutlet)
    async def _unknown(request: Request, exc: UnknownOutlet):
        ctl = get_controller(request)
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown outlet {exc.args[0]}; known: {ctl.outlets}")

    @app.exception_handler(ShellyUnreachable)
    async def _unreachable(request: Request, exc: ShellyUnreachable):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"shelly unreachable: {exc.args[0]}")

    auth = [Depends(require_auth)]

    @app.get("/healthz")
    async def healthz(request: Request):
        ctl = get_controller(request)
        if ctl.sampler.last_error:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"shelly unreachable: {ctl.sampler.last_error}")
        return {"ok": True, "outlets": ctl.outlets}

    @app.get("/outlets", response_model=list[OutletResponse], dependencies=auth)
    async def list_outlets(request: Request):
        return [OutletResponse.from_state(st) for st in get_controller(request).states()]

    @app.get("/outlets/{outlet}/status", response_model=OutletResponse, dependencies=auth)
    async def outlet_status(outlet: int, request: Request):
        return OutletResponse.from_state(get_controller(request).state(outlet))

    @app.post("/outlets/{outlet}/on", response_model=OutletResponse, dependencies=auth)
    async def outlet_on(outlet: int, request: Request):
        st, action = await get_controller(request).power_on(outlet)
        return OutletResponse.from_state(st, action)

    @app.post("/outlets/{outlet}/off", response_model=OutletResponse, dependencies=auth)
    async def outlet_off(outlet: int, request: Request):
        st, action = await get_controller(request).power_off(outlet)
        return OutletResponse.from_state(st, action)

    return app


app = create_app()
