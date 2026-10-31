"""The production JWT signing key is at least 32 bytes (RFC 7518 §3.2 for HS256), #137.

``require_api_secrets`` counts characters. Every character is at least one UTF-8 byte, so a
secret it accepts is never shorter than 32 bytes; these pin the boundary.
"""

from __future__ import annotations

import pytest

from quantlab_api.config import Settings
from tests.unit.test_api_secrets import API


def _check(secret: str) -> None:
    Settings(_env_file=None, **{**API, "jwt_secret": secret}).require_api_secrets()


def test_a_31_byte_secret_is_refused_and_32_accepted() -> None:
    with pytest.raises(ValueError, match="at least 32"):
        _check("s" * 31)
    _check("s" * 32)


@pytest.mark.parametrize("secret", ["s" * 32, "é" * 32, "€" * 40])
def test_an_accepted_secret_is_at_least_32_bytes(secret: str) -> None:
    _check(secret)
    assert len(secret.encode()) >= 32
