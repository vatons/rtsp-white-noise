"""Home Assistant's test harness, for the tests that need it.

The integration tests (test_init.py) run in Linux - Home Assistant does not
install on Windows; see README. test_talk.py and test_session.py need none
of this and run anywhere.
"""

from __future__ import annotations

try:
    import pytest_homeassistant_custom_component  # noqa: F401
except ImportError:  # plain Python, no Home Assistant
    collect_ignore = ["test_init.py"]
else:
    pytest_plugins = "pytest_homeassistant_custom_component"
