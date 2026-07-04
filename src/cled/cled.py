#!/usr/bin/env python3
"""Ambient daemon — paints the keyboard with system + agent-session info.

  Number row 1-9: iTerm2 tabs (red=busy, green=idle, yellow=stale, blue=other)
  Numpad 0-9    : per-core CPU load (green->yellow->red)
  F1-F12        : RAM usage (fills left-to-right)
  Enter         : rainbow heartbeat
  Everything else: dim

Run: uv run cled
"""

import time

import psutil

from cled import agent_tabs
from cled.agent_tabs import AgentTabMonitor
from cled.rgb import DIM, RGBSession, frame_from_map, load_gradient, rainbow

F_KEYS = [f"Key: F{i}" for i in range(1, 13)]
NUMBER_ROW = [f"Key: {n}" for n in (1, 2, 3, 4, 5, 6, 7, 8, 9, 0)]  # 10th tab -> '0' key
NUMPAD_DIGITS = [f"Key: Number Pad {n}" for n in range(0, 10)]
ENTER = "Key: Enter"

AGENT_STATUS_COLORS = {
    agent_tabs.STATUS_BUSY:       (255,   0,   0),
    agent_tabs.STATUS_IDLE:       (  0, 255,   0),
    agent_tabs.STATUS_IDLE_STALE: (255, 200,   0),
    agent_tabs.STATUS_OTHER:      (  0,  80, 255),
}

# Fixed green->red palette for the F-key RAM bar: one tint per key by position.
# The bar's fill length encodes load; these per-key colors are constant.
F_KEY_GRADIENT = [load_gradient(i / (len(F_KEYS) - 1)) for i in range(len(F_KEYS))]

CPU_SAMPLE_INTERVAL_S = 0.5
_cpu_sample = (0.0, [])
psutil.cpu_percent(percpu=True)  # prime baseline


def _cores():
    global _cpu_sample
    now = time.monotonic()
    if now - _cpu_sample[0] >= CPU_SAMPLE_INTERVAL_S:
        _cpu_sample = (now, psutil.cpu_percent(percpu=True))
    return _cpu_sample[1]


def apply_numpad_cpu(led_map):
    cores = _cores()
    for i, name in enumerate(NUMPAD_DIGITS):
        led_map[name] = load_gradient(cores[i] / 100.0) if i < len(cores) else DIM


def apply_ram_f_keys(led_map):
    load = psutil.virtual_memory().percent / 100.0
    lit = round(load * len(F_KEYS))
    for i, name in enumerate(F_KEYS):
        led_map[name] = F_KEY_GRADIENT[i] if i < lit else DIM


def apply_agent_tabs(led_map, monitor):
    for i, status in enumerate(monitor.slots()):
        if status is not None:
            led_map[NUMBER_ROW[i]] = AGENT_STATUS_COLORS[status]


def make_render(monitor):
    def render(kb):
        led_map = {}
        apply_numpad_cpu(led_map)
        apply_ram_f_keys(led_map)
        apply_agent_tabs(led_map, monitor)
        led_map[ENTER] = rainbow()
        return frame_from_map(kb.leds, led_map)
    return render


def main():
    monitor = AgentTabMonitor()
    monitor.start()
    with RGBSession() as session:
        print(f"Running on {session.name}. Ctrl-C to quit.")
        session.run(make_render(monitor), fps=10)


if __name__ == "__main__":
    main()
