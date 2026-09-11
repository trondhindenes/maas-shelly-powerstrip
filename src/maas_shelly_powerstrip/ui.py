"""Minimal HTMX web UI. Shares the bearer-token auth with the JSON API: when a
token is configured the page asks for it once and stores it in localStorage."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


async def _auth(request: Request) -> None:
    from .main import require_auth  # avoid circular import at module load

    await require_auth(request)


def _ctx(request: Request, action: str | None = None, error: str | None = None) -> dict:
    ctl = request.app.state.controller
    settings = request.app.state.settings
    return {
        "states": ctl.states(),
        "busy": {o: ctl.busy(o) for o in ctl.outlets},
        "error": error or ctl.sampler.last_error,
        "action": action,
        "settings": settings,
        "auth_required": bool(settings.api_token),
        "shelly_host": settings.shelly_host,
    }


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", _ctx(request))


@router.get("/ui/outlets", response_class=HTMLResponse, dependencies=[Depends(_auth)])
async def outlets_partial(request: Request):
    return templates.TemplateResponse(request, "outlets.html", _ctx(request))


@router.post("/ui/outlets/{outlet}/on", response_class=HTMLResponse, dependencies=[Depends(_auth)])
async def ui_on(request: Request, outlet: int):
    _, action = await request.app.state.controller.power_on(outlet)
    return templates.TemplateResponse(request, "outlets.html", _ctx(request, action=f"outlet {outlet}: {action}"))


@router.post("/ui/outlets/{outlet}/off", response_class=HTMLResponse, dependencies=[Depends(_auth)])
async def ui_off(request: Request, outlet: int):
    _, action = await request.app.state.controller.power_off(outlet)
    return templates.TemplateResponse(request, "outlets.html", _ctx(request, action=f"outlet {outlet}: {action}"))
