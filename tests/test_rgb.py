import types
from unittest import mock

from hypothesis import given, settings
from hypothesis import strategies as st

from cled import rgb
from tests.strategies import unit_interval, rgb_tuple, unit
from openrgb.utils import RGBColor, DeviceType


def _in_range(c):
    return all(isinstance(x, int) and 0 <= x <= 255 for x in c)


@given(h=unit, s=unit, v=unit)
@settings(max_examples=50)
def test_channel_bounds_hsv(h, s, v):
    # Property: channel bounds — hsv() output channels are ints in [0, 255]
    assert _in_range(rgb.hsv(h, s, v))


@given(t=unit_interval, c0=rgb_tuple, c1=rgb_tuple)
@settings(max_examples=50)
def test_lerp_bounds(t, c0, c1):
    # Property: gradient/lerp bounds — each channel stays within the endpoints
    out = rgb.lerp_color(t, c0, c1)
    assert _in_range(out)
    for o, a, b in zip(out, c0, c1):
        assert min(a, b) <= o <= max(a, b)


@given(t=unit_interval)
@settings(max_examples=50)
def test_load_gradient_bounds(t):
    # Property: gradient/lerp bounds — load_gradient stays in [0,255] across [0,1]
    assert _in_range(rgb.load_gradient(t))


def _fake_leds(names):
    return [types.SimpleNamespace(name=n) for n in names]


KEY_NAMES = st.lists(
    st.sampled_from(["Key: F1", "Key: 1", "Key: Enter", "Key: Number Pad 0", "Key: A"]),
    min_size=1, max_size=5, unique=True,
)


@given(names=KEY_NAMES)
@settings(max_examples=50)
def test_frame_from_map_full_coverage(names):
    # Property: full-frame coverage — one RGBColor per LED, never partial
    leds = _fake_leds(names)
    frame = rgb.frame_from_map(leds, {})
    assert len(frame) == len(leds)
    assert all(isinstance(c, RGBColor) for c in frame)


@given(names=KEY_NAMES, color=rgb_tuple)
@settings(max_examples=50)
def test_frame_from_map_fidelity(names, color):
    # Property: name-map fidelity — mapped key gets its color, others get DIM
    leds = _fake_leds(names)
    target = names[0]
    frame = rgb.frame_from_map(leds, {target: color})
    by_name = {led.name: c for led, c in zip(leds, frame)}
    assert by_name[target] == RGBColor(*color)
    for n in names[1:]:
        assert by_name[n] == RGBColor(*rgb.DIM)


class _FakeKeyboard:
    def __init__(self):
        self.name = "Fake K70"
        self.type = DeviceType.KEYBOARD
        self.leds = _fake_leds(["Key: F1", "Key: Enter"])
        self.mode = None
        self.last_colors = None

    def set_mode(self, m):
        self.mode = m

    def set_colors(self, colors, fast=False):
        self.last_colors = colors


class _FakeClient:
    def __init__(self, *a, **k):
        self._kb = _FakeKeyboard()

    def get_devices_by_type(self, t):
        return [self._kb] if t == DeviceType.KEYBOARD else []

    def disconnect(self):
        pass


def test_connect_starts_server_when_down():
    # self-heal: first client attempt fails, launcher runs, second attempt succeeds
    attempts = {"n": 0}
    launched = {"n": 0}

    def factory(*a, **k):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionRefusedError("no server")
        return _FakeClient()

    sess = rgb.RGBSession(
        client_factory=factory,
        server_launcher=lambda: launched.__setitem__("n", launched["n"] + 1),
        port_wait=lambda: True,
        retry_delay=0,
    )
    sess._connect()
    assert attempts["n"] == 2
    assert launched["n"] == 1


def test_tracer_real_server_detects_keyboard(live_session):
    # External dep: OpenRGBClient connect + get_devices_by_type + leds + matrix grid.
    kb = live_session._kb
    assert kb is not None
    assert len(kb.leds) > 0
    assert "Direct" in [m.name for m in kb.modes]
    cols = rgb.key_columns(kb)
    assert len(cols) > 0  # matrix grid yields columns


def test_run_pushes_then_reconnects_on_drop():
    sess = rgb.RGBSession(client_factory=_FakeClient, server_launcher=lambda: None,
                          port_wait=lambda: True, retry_delay=0)
    sess._connect(); sess.keyboard()
    calls = {"n": 0}

    def render(kb):
        calls["n"] += 1
        if calls["n"] == 1:
            return rgb.frame_from_map(kb.leds, {"Key: F1": (255, 0, 0)})
        raise KeyboardInterrupt  # exit the loop on the second frame

    try:
        sess.run(render, fps=1000)
    except KeyboardInterrupt:
        pass
    assert sess._kb.last_colors is not None
    assert sess._kb.last_colors[0] == RGBColor(255, 0, 0)


def test_tracer_real_set_colors(live_session):
    # External dep: Direct mode + set_colors(full frame) on the real keyboard.
    kb = live_session._kb
    frame = rgb.frame_from_map(kb.leds, {"Key: Escape": (0, 255, 0)})
    live_session.push(frame)  # must not raise; server accepts the wire format
    by_name = {led.name: c for led, c in zip(kb.leds, kb.colors)}
    assert by_name["Key: Escape"] == RGBColor(0, 255, 0)
    live_session.push(rgb.frame_from_map(kb.leds, {}))  # restore dim


@given(prev=st.floats(min_value=0, max_value=2e9, allow_nan=False, allow_infinity=False),
       delta=st.floats(min_value=0, max_value=rgb.WAKE_GAP_THRESHOLD, allow_nan=False, allow_infinity=False))
@settings(max_examples=50)
def test_no_false_trigger_within_threshold(prev, delta):
    # is_wake_gap matches the exact computed gap; sound across float rounding, covers [0, threshold]
    now = prev + delta
    assert rgb.is_wake_gap(prev, now) is (now - prev > rgb.WAKE_GAP_THRESHOLD)


@given(prev=st.floats(min_value=0, max_value=2e9, allow_nan=False, allow_infinity=False),
       delta=st.floats(min_value=rgb.WAKE_GAP_THRESHOLD, max_value=1e7, exclude_min=True, allow_nan=False, allow_infinity=False))
@settings(max_examples=50)
def test_wake_fires_above_threshold(prev, delta):
    # same exact-gap oracle, covering deltas strictly above the threshold including (60, 61)
    now = prev + delta
    assert rgb.is_wake_gap(prev, now) is (now - prev > rgb.WAKE_GAP_THRESHOLD)


def test_run_refreshes_once_on_wake_gap():
    # Property "Single-fire per wake": one over-threshold gap -> exactly one refresh
    sess = rgb.RGBSession(client_factory=_FakeClient, server_launcher=lambda: None,
                          port_wait=lambda: True, retry_delay=0)
    sess._connect(); sess.keyboard()
    sess._refresh_server = mock.Mock()
    reads = iter([0.0, 1.0, 2000.0, 2000.0, 2001.0])  # frame, frame, WAKE, reset, frame
    calls = {"n": 0}

    def render(kb):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise KeyboardInterrupt  # exit after the post-wake frame
        return rgb.frame_from_map(kb.leds, {})

    try:
        sess.run(render, fps=1000, clock=lambda: next(reads))
    except KeyboardInterrupt:
        pass
    sess._refresh_server.assert_called_once()


def test_run_does_not_refresh_under_normal_deltas():
    # Property "No false trigger": normal frame deltas never refresh
    sess = rgb.RGBSession(client_factory=_FakeClient, server_launcher=lambda: None,
                          port_wait=lambda: True, retry_delay=0)
    sess._connect(); sess.keyboard()
    sess._refresh_server = mock.Mock()
    reads = iter([0.0, 0.1, 0.2, 0.3])
    calls = {"n": 0}

    def render(kb):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise KeyboardInterrupt
        return rgb.frame_from_map(kb.leds, {})

    try:
        sess.run(render, fps=1000, clock=lambda: next(reads))
    except KeyboardInterrupt:
        pass
    sess._refresh_server.assert_not_called()


class _FakeClientNoKeyboard:
    """A connected client whose server reports no keyboard (empty device list)."""

    def __init__(self, *a, **k):
        pass

    def get_devices_by_type(self, t):
        return []

    def disconnect(self):
        pass


def test_keyboard_retries_when_server_reports_no_keyboard():
    # Regression: an empty server must trigger a server restart + retry, NOT a
    # RuntimeError that kills the daemon. The first client reports no keyboard;
    # after the restart the next client has one.
    clients = iter([_FakeClientNoKeyboard(), _FakeClient()])
    sess = rgb.RGBSession(
        client_factory=lambda *a, **k: next(clients),
        server_launcher=lambda: None,
        port_wait=lambda: True,
        retry_delay=0,
    )
    with mock.patch.object(rgb.subprocess, "run"), \
         mock.patch.object(rgb, "_port_open", return_value=False), \
         mock.patch.object(rgb.time, "sleep"):
        sess._connect()          # connects to the first (no-keyboard) client
        kb = sess.keyboard()     # current code raises RuntimeError here

    assert kb is not None         # acquired only because it restarted + retried
    assert sess._kb is kb


def test_enter_refreshes_inherited_server():
    # Regression: attaching to a pre-existing server (one we did NOT launch) can
    # inherit a stale USB HID handle — it still enumerates the keyboard but
    # silently drops writes (no exception), so neither self-heal path fires and
    # the daemon paints into the void. __enter__ must bounce an inherited server
    # once for a guaranteed-clean handle before the first frame.
    sess = rgb.RGBSession(client_factory=_FakeClient, server_launcher=lambda: None,
                          port_wait=lambda: True, retry_delay=0)
    sess._refresh_server = mock.Mock()
    sess.__enter__()  # first connect succeeds -> server was already running (inherited)
    sess._refresh_server.assert_called_once()


def test_enter_does_not_refresh_self_launched_server():
    # Guard against over-correction: if we launched the server ourselves, its
    # handle is fresh by definition — bouncing it would only cost a needless
    # restart on every startup.
    attempts = {"n": 0}

    def factory(*a, **k):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionRefusedError("no server")
        return _FakeClient()

    sess = rgb.RGBSession(client_factory=factory, server_launcher=lambda: None,
                          port_wait=lambda: True, retry_delay=0)
    sess._refresh_server = mock.Mock()
    sess.__enter__()  # first connect fails -> launcher runs -> we own a fresh server
    sess._refresh_server.assert_not_called()


def test_tracer_refresh_server_recovers(live_session):
    # External deps: pkill the server + `open -a` relaunch (via _reconnect), un-mocked,
    # on real hardware. Also covers "Recovery" and "Idempotent/harmless" properties:
    # refreshing a healthy server leaves the keyboard controllable.
    live_session._refresh_server()
    kb = live_session._kb
    live_session.push(rgb.frame_from_map(kb.leds, {"Key: Escape": (0, 0, 255)}))
    by_name = {led.name: c for led, c in zip(kb.leds, kb.colors)}
    assert by_name["Key: Escape"] == RGBColor(0, 0, 255)
    live_session.push(rgb.frame_from_map(kb.leds, {}))  # restore dim
