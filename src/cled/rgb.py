"""RGB primitives for the OpenRGB-backed CLED daemon."""

import colorsys
import socket
import subprocess
import time

from openrgb import OpenRGBClient
from openrgb.utils import RGBColor, DeviceType

__all__ = [
    "RGBSession", "DIM", "SERVER_HOST", "SERVER_PORT",
    "hsv", "lerp_color", "load_gradient", "rainbow",
    "frame_from_map", "key_columns",
    "WAKE_GAP_THRESHOLD", "is_wake_gap",
]

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 6742

# Near-black baseline for unused keys.
DIM = (8, 8, 8)


def hsv(h: float, s: float = 1.0, v: float = 1.0) -> tuple[int, int, int]:
    """HSV -> (r, g, b) each 0-255. h/s/v in [0, 1]."""
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


def lerp_color(t, c0, c1) -> tuple[int, int, int]:
    """Linear interpolate between two RGB tuples. t in [0, 1]."""
    t = max(0.0, min(1.0, t))
    return (
        int(c0[0] + (c1[0] - c0[0]) * t),
        int(c0[1] + (c1[1] - c0[1]) * t),
        int(c0[2] + (c1[2] - c0[2]) * t),
    )


def load_gradient(t: float) -> tuple[int, int, int]:
    """Green -> yellow -> red gradient for load levels. t in [0, 1]."""
    t = max(0.0, min(1.0, t))
    if t < 0.5:
        return lerp_color(t * 2, (0, 255, 0), (255, 255, 0))
    return lerp_color((t - 0.5) * 2, (255, 255, 0), (255, 0, 0))


def rainbow(period_s: float = 4.0) -> tuple[int, int, int]:
    """A hue-cycling color — one cycle per period_s seconds."""
    return hsv((time.monotonic() / period_s) % 1.0)


def frame_from_map(leds, led_map: dict, default=DIM) -> list:
    """Build a full frame: one RGBColor per LED, keyed by LED name.

    `leds` is the device's `.leds` list. Keys absent from `led_map` get `default`,
    so every LED is always assigned — frames are never partial.
    """
    return [RGBColor(*led_map.get(led.name, default)) for led in leds]


SERVER_START_TIMEOUT = 30.0  # OpenRGB HID detection on macOS takes ~10-12s; 30s gives headroom
WAKE_GAP_THRESHOLD = 60.0  # seconds; a frame-to-frame wall-clock jump beyond this means the machine slept


def _default_server_launcher() -> None:
    # LaunchServices launch — the GUI-session context that has HID access.
    subprocess.run(
        ["open", "-a", "OpenRGB", "--args", "--server", "--noautoconnect"],
        check=False,
    )


def is_wake_gap(prev_wall: float, now: float, threshold: float = WAKE_GAP_THRESHOLD) -> bool:
    """True if the wall-clock jump since the previous frame indicates a sleep/wake.

    Uses wall clock (time.time), not monotonic: on macOS time.monotonic() is
    suspended during sleep and would not see the gap.
    """
    return now - prev_wall > threshold


def _port_open(host=SERVER_HOST, port=SERVER_PORT, timeout=0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def key_columns(kb) -> list:
    """Return columns of LED names, left-to-right, from the zone matrix grid.

    Each column is a list of LED names (top-to-bottom). Used for spatial effects;
    generalizes across keyboards via OpenRGB's matrix_map.
    """
    zone = kb.zones[0]
    mm = zone.matrix_map
    cols = []
    for col in range(zone.mat_width):
        names = []
        for row in range(zone.mat_height):
            idx = mm[row][col]
            if idx is not None:
                names.append(kb.leds[idx].name)
        if names:
            cols.append(names)
    return cols


class RGBSession:
    """Owns the OpenRGB client connection and keyboard control."""

    def __init__(self, model_hint="K70", client_factory=OpenRGBClient,
                 server_launcher=_default_server_launcher, port_wait=None,
                 retry_delay=1.0):
        self._model_hint = model_hint
        self._client_factory = client_factory
        self._server_launcher = server_launcher
        self._port_wait = port_wait or self._wait_for_port
        self._retry_delay = retry_delay
        self._client = None
        self._kb = None
        self._launched_server = False  # True once we start the server ourselves

    def _wait_for_port(self) -> bool:
        deadline = time.monotonic() + SERVER_START_TIMEOUT
        while time.monotonic() < deadline:
            if _port_open():
                return True
            time.sleep(0.5)
        return False

    def _connect(self) -> None:
        try:
            self._client = self._client_factory(SERVER_HOST, SERVER_PORT)
            self._launched_server = False  # a server was already up; we inherited it
            return
        except (ConnectionError, OSError, TimeoutError) as e:
            print(f"rgb: no OpenRGB server ({type(e).__name__}); starting one", flush=True)
        self._server_launcher()
        if not self._port_wait():
            raise RuntimeError("OpenRGB server did not come up; is OpenRGB installed?")
        time.sleep(self._retry_delay)
        self._client = self._client_factory(SERVER_HOST, SERVER_PORT)
        self._launched_server = True  # we started this server; its HID handle is fresh

    def keyboard(self):
        """Acquire the keyboard, retrying with backoff until one appears.

        A freshly (re)launched OpenRGB server can report no devices (a stale HID
        handle, or post-sleep USB timing). A background daemon must not die on
        that, so restart the server and retry instead of raising. (A server that
        won't start at all still raises from _connect — a genuine install issue.)
        """
        delay = 1.0
        while True:
            if self._client is None:
                self._connect()
            devices = self._client.get_devices_by_type(DeviceType.KEYBOARD)
            if devices:
                kb = next((d for d in devices if self._model_hint in d.name), devices[0])
                kb.set_mode("Direct")
                self._kb = kb
                return kb
            print(f"rgb: server reports no keyboard; restarting it, retrying in {delay:.0f}s", flush=True)
            self._restart_server()
            time.sleep(delay)
            delay = min(delay * 2.0, 30.0)

    @property
    def name(self) -> str:
        return self._kb.name

    def __enter__(self):
        self.keyboard()          # keyboard() connects (and retries) on its own
        if not self._launched_server:
            # We inherited a server we didn't start. If it slept unattended its
            # USB HID handle is stale — it still enumerates the keyboard but
            # silently drops writes, and neither self-heal path (socket error,
            # >60s wake gap) fires. Bounce it once for a clean handle so we never
            # start out painting into the void.
            print("rgb: inherited a running server; refreshing for a clean HID handle", flush=True)
            self._refresh_server()
        return self

    def push(self, frame) -> None:
        self._kb.set_colors(frame)

    def _reconnect(self) -> None:
        print("rgb: reconnecting to OpenRGB server...", flush=True)
        self._client = None          # drop the dead client; keyboard() reconnects
        self.keyboard()
        print("rgb: reconnected", flush=True)

    def _restart_server(self) -> None:
        """Kill the OpenRGB server and drop the client so the next connect
        relaunches a fresh one with a clean HID handle.

        Disconnect the client first: an abrupt kill while the socket is open
        causes macOS to strand the HID handle, and the relaunched process then
        exits silently without opening the port.
        """
        if self._client is not None:
            try:
                self._client.disconnect()
            except (ConnectionError, OSError) as e:
                print(f"rgb: pre-restart disconnect failed (ignored): {type(e).__name__}", flush=True)
            self._client = None
        subprocess.run(["pkill", "-f", "OpenRGB --server"], check=False)
        deadline = time.monotonic() + SERVER_START_TIMEOUT
        while _port_open() and time.monotonic() < deadline:
            time.sleep(0.25)

    def _refresh_server(self) -> None:
        """Recover a stale USB HID handle after sleep: restart the server, then
        re-acquire the keyboard (keyboard() retries until it reappears)."""
        print("rgb: refreshing OpenRGB server...", flush=True)
        self._restart_server()
        self.keyboard()

    def run(self, render_fn, fps: int = 10, clock=time.time) -> None:
        """Render loop at `fps`. Self-heals on connection errors, and refreshes
        the server when a wall-clock gap between frames indicates the machine
        slept and woke (the server's HID handle goes stale across sleep).
        """
        interval = 1.0 / fps
        last_wall = clock()
        while True:
            now = clock()
            if is_wake_gap(last_wall, now):
                print(f"rgb: {now - last_wall:.0f}s wall-clock gap; refreshing server (woke from sleep?)", flush=True)
                self._refresh_server()
                last_wall = clock()
                continue
            last_wall = now
            t0 = time.monotonic()
            try:
                self.push(render_fn(self._kb))
            except (ConnectionError, OSError, TimeoutError) as e:
                print(f"rgb: connection error ({type(e).__name__}); self-healing", flush=True)
                self._reconnect()
                continue
            nap = interval - (time.monotonic() - t0)
            if nap > 0:
                time.sleep(nap)

    def __exit__(self, *exc):
        if self._client is not None:
            try:
                self._client.disconnect()   # leave the server running
            except (ConnectionError, OSError) as e:
                print(f"rgb: disconnect error (ignored, server stays up): {e}", flush=True)
        return False
