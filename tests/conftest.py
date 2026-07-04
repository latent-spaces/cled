import pytest

from cled import rgb


@pytest.fixture(scope="session")
def live_session():
    """Real RGBSession against the real OpenRGB server + keyboard.

    NOT mocked, NOT skipped — the external-dependency tracer bullet. Self-heals
    the server via `open -a` if it isn't already running.
    """
    sess = rgb.RGBSession()
    sess.__enter__()
    yield sess
    sess.__exit__()
