"""Single source of the current time for the plugin.

Every module that needs "now" calls ``clock.now()`` instead of
``datetime.now()`` so tests can pin or advance time by monkeypatching one
function (see the ``fake_clock`` fixture in tests/conftest.py).
"""
from datetime import datetime


def now() -> datetime:
    """Return the current local time."""
    return datetime.now()
