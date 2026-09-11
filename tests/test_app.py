import asyncio

import httpx
import pytest
from httpx import ASGITransport

from maas_shelly_powerstrip.config import Settings
from maas_shelly_powerstrip.main import create_app
from maas_shelly_powerstrip.sampler import Sampler
from maas_shelly_powerstrip.shelly import SwitchStatus


class FakeShelly:
    """In-memory stand-in for the strip. `power` maps outlet -> watts drawn when relay closed."""

    def __init__(self, outlets=(0, 1, 2, 3), power=None):
        self.output = {o: False for o in outlets}
        self.power = power or {}
        self.calls: list[tuple[int, bool]] = []

    async def get_switches(self):
        return {
            o: SwitchStatus(id=o, output=on, apower=self.power.get(o, 0.0) if on else 0.0)
            for o, on in self.output.items()
        }

    async def set_switch(self, switch_id, on):
        self.calls.append((switch_id, on))
        self.output[switch_id] = on

    async def aclose(self):
        pass


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# --- Sampler state logic -----------------------------------------------------


def make_sampler(clock, window=5.0, threshold=5.0):
    return Sampler(FakeShelly(), interval=1.0, window=window, threshold=threshold, clock=clock)


def test_relay_open_is_off():
    clock = FakeClock()
    s = make_sampler(clock)
    s.record(0, SwitchStatus(0, output=False, apower=0.0))
    assert s.state(0).status == "off"


def test_relay_closed_drawing_power_is_on():
    clock = FakeClock()
    s = make_sampler(clock)
    s.record(0, SwitchStatus(0, output=True, apower=40.0))
    assert s.state(0).status == "on"


def test_relay_closed_idle_is_on_until_window_full_then_off():
    clock = FakeClock()
    s = make_sampler(clock)
    for _ in range(5):
        s.record(0, SwitchStatus(0, output=True, apower=1.2))
        clock.t += 1
    assert s.state(0).status == "on"  # only 4s spanned
    s.record(0, SwitchStatus(0, output=True, apower=1.2))
    assert s.state(0).status == "off"  # 5s spanned


def test_threshold_is_inclusive():
    clock = FakeClock()
    s = make_sampler(clock, threshold=5.0)
    for _ in range(7):
        s.record(0, SwitchStatus(0, output=True, apower=5.0))
        clock.t += 1
    assert s.state(0).status == "off"
    s.record(0, SwitchStatus(0, output=True, apower=5.1))
    assert s.state(0).status == "on"


def test_unknown_outlet():
    s = make_sampler(FakeClock())
    assert s.state(9).status == "unknown"


# --- HTTP API ----------------------------------------------------------------


def fast_settings(**overrides):
    return Settings(
        shelly_host="fake",
        sample_interval=0.02,
        idle_window_seconds=0.1,
        cycle_off_seconds=0.05,
        idle_watt_threshold=5.0,
        _env_file=None,
        **overrides,
    )


async def run_app(shelly, settings=None):
    app = create_app(settings or fast_settings(), client=shelly)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c, app


@pytest.fixture
async def api():
    shelly = FakeShelly(power={0: 60.0, 1: 1.0})
    async for c, app in run_app(shelly):
        yield c, shelly, app


async def test_status_off_when_relay_open(api):
    c, shelly, _ = api
    r = await c.get("/outlets/0/status")
    assert r.status_code == 200
    assert r.json()["status"] == "off"


async def test_on_from_relay_open_turns_on(api):
    c, shelly, _ = api
    r = await c.post("/outlets/0/on")
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "powered_on"
    assert body["status"] == "on"
    assert shelly.calls == [(0, True)]


async def test_on_when_already_drawing_is_noop(api):
    c, shelly, _ = api
    shelly.output[0] = True
    await asyncio.sleep(0.05)
    r = await c.post("/outlets/0/on")
    assert r.json()["action"] == "already_on"
    assert shelly.calls == []


async def test_on_when_idle_power_cycles(api):
    c, shelly, _ = api
    shelly.output[1] = True  # 1 W draw, below threshold
    await asyncio.sleep(0.2)  # let the window fill
    assert (await c.get("/outlets/1/status")).json()["status"] == "off"
    r = await c.post("/outlets/1/on")
    assert r.json()["action"] == "power_cycled"
    assert shelly.calls == [(1, False), (1, True)]


async def test_off(api):
    c, shelly, _ = api
    shelly.output[2] = True
    r = await c.post("/outlets/2/off")
    assert r.json()["action"] == "powered_off"
    assert r.json()["status"] == "off"
    assert shelly.output[2] is False


async def test_unknown_outlet_404(api):
    c, _, _ = api
    assert (await c.get("/outlets/7/status")).status_code == 404


async def test_documented_maas_regexes_match():
    import re

    on_re = re.compile(r'"status":"on"')  # as documented in README
    off_re = re.compile(r'"status":"off"')
    shelly = FakeShelly(power={0: 60.0})
    async for c, _ in run_app(shelly):
        off_body = (await c.get("/outlets/0/status")).text
        assert off_re.search(off_body) and not on_re.search(off_body)
        await c.post("/outlets/0/on")
        on_body = (await c.get("/outlets/0/status")).text
        assert on_re.search(on_body) and not off_re.search(on_body)


async def test_bearer_token_required_when_configured():
    shelly = FakeShelly()
    async for c, _ in run_app(shelly, fast_settings(api_token="s3cret")):
        assert (await c.get("/outlets/0/status")).status_code == 401
        r = await c.get("/outlets/0/status", headers={"Authorization": "Bearer s3cret"})
        assert r.status_code == 200
        assert (await c.get("/healthz")).status_code == 200  # health is unauthenticated


# --- HTML UI -----------------------------------------------------------------


async def test_ui_index_and_partial(api):
    c, shelly, _ = api
    r = await c.get("/")
    assert r.status_code == 200
    assert "<title>MAAS Shelly Power Strip</title>" in r.text
    assert 'hx-get="/ui/outlets"' in r.text
    assert "/static/htmx.min.js" in r.text
    r = await c.get("/ui/outlets")
    assert r.status_code == 200
    assert r.text.count('class="badge off"') == 4


async def test_ui_on_and_off_buttons(api):
    c, shelly, _ = api
    r = await c.post("/ui/outlets/0/on")
    assert r.status_code == 200
    assert "outlet 0: powered_on" in r.text
    assert 'class="badge on"' in r.text
    assert shelly.output[0] is True
    r = await c.post("/ui/outlets/0/off")
    assert "outlet 0: powered_off" in r.text
    assert shelly.output[0] is False


async def test_static_htmx_served(api):
    c, _, _ = api
    r = await c.get("/static/htmx.min.js")
    assert r.status_code == 200
    assert r.text.startswith("var htmx=")


async def test_ui_partial_requires_token_when_configured():
    shelly = FakeShelly()
    async for c, _ in run_app(shelly, fast_settings(api_token="s3cret")):
        assert (await c.get("/")).status_code == 200  # page itself is public
        assert (await c.get("/ui/outlets")).status_code == 401
        assert (await c.post("/ui/outlets/0/on")).status_code == 401
        r = await c.get("/ui/outlets", headers={"Authorization": "Bearer s3cret"})
        assert r.status_code == 200
