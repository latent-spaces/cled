import types
from unittest import mock

from hypothesis import given, settings
from hypothesis import strategies as st
from cled import rgb
from cled import cled


def _fake_kb(names):
    return types.SimpleNamespace(leds=[types.SimpleNamespace(name=n) for n in names])


CORE = st.lists(st.floats(min_value=0, max_value=100), min_size=10, max_size=10)


@given(cores=CORE, ram=st.floats(min_value=0, max_value=100))
@settings(max_examples=50)
def test_render_deterministic(cores, ram):
    # Property: render determinism — same state + same time -> same frame
    kb = _fake_kb(cled.NUMPAD_DIGITS + cled.F_KEYS + cled.NUMBER_ROW + ["Key: Enter", "Key: A"])
    monitor = types.SimpleNamespace(slots=lambda: [None] * 10)
    with mock.patch.object(cled, "_cores", lambda: cores), \
         mock.patch.object(cled.psutil, "virtual_memory",
                           lambda: types.SimpleNamespace(percent=ram)), \
         mock.patch.object(rgb.time, "monotonic", lambda: 123.0):
        render = cled.make_render(monitor)
        first = render(kb)
        assert first == render(kb)          # deterministic
        assert len(first) == len(kb.leds)   # full frame
