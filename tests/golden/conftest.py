"""Golden tests: results pinned to values computed by hand or published by a third party.

Every test in this directory is marked ``golden`` (``pytest -m golden``). A change that
breaks one changes a published definition, not just an implementation detail.
"""

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "golden: pinned to an independent reference value")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "/tests/golden/" in str(item.fspath):
            item.add_marker(pytest.mark.golden)
