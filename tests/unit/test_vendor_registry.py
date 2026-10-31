"""Vendor registry, bearer auth and the market-data settings (#11). No network calls."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from quantlab.data.vendors.http import JsonApi, VendorSchemaError
from quantlab.data.vendors.registry import VENDORS, get_vendor
from tests.unit.test_vendor_massive import BASE_URL, KEY, FakeMassive


def _settings(**kw):
    from quantlab_api.config import Settings

    return Settings(_env_file=None, environment="test", **kw)


CONFIGURED = {
    "market_data_provider": "massive",
    "market_data_api_key": KEY,
    "market_data_license": "test licence (synthetic fixtures)",
    "market_data_plan": "test",
}


def test_bearer_auth_keeps_the_key_out_of_every_url():
    fake = FakeMassive()
    api = JsonApi(BASE_URL, KEY, auth="bearer", transport=fake, sleep=lambda s: None)
    pages = list(api.pages("/v3/reference/tickers", {"market": "stocks", "active": "true"}))
    assert pages and fake.requests
    assert all(KEY not in u and "apiKey" not in u for u in fake.requests)
    assert all(h["Authorization"] == f"Bearer {KEY}" for h in fake.headers)


def test_bearer_auth_refuses_a_url_that_carries_a_key():
    api = JsonApi(BASE_URL, KEY, auth="bearer", transport=FakeMassive(), sleep=lambda s: None)
    with pytest.raises(VendorSchemaError, match="apiKey"):
        api.get(f"{BASE_URL}/v3/reference/tickers?apiKey=other")


def test_the_registry_holds_massive_only_and_rejects_unknown_names():
    assert sorted(VENDORS) == ["massive"]
    assert get_vendor(" Massive ").auth == "bearer"
    with pytest.raises(ValueError, match="registered"):
        get_vendor("polygon")


def test_market_data_is_disabled_by_default():
    from quantlab_api.marketdata import (
        MarketDataNotConfiguredError,
        configured_vendor,
        vendor_status,
    )

    s = _settings()
    status = vendor_status(s)
    assert status["enabled"] is False and len(status["missing"]) == 4
    with pytest.raises(MarketDataNotConfiguredError, match="not configured"):
        configured_vendor(s, transport=FakeMassive())


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_key_counts_as_unset(blank):
    from quantlab_api.marketdata import vendor_status

    s = _settings(**{**CONFIGURED, "market_data_api_key": blank})
    assert s.market_data_api_key is None
    assert vendor_status(s)["missing"] == ["QUANTLAB_MARKET_DATA_API_KEY"]


def test_an_unknown_provider_fails_at_startup():
    with pytest.raises(ValidationError, match="unknown market-data provider"):
        _settings(market_data_provider="polgon")


def test_production_refuses_a_provider_without_a_key():
    from quantlab_api.config import Settings

    prod = {
        "environment": "production",
        "jwt_secret": "x" * 40,
        "metrics_token": "m" * 20,
        "cookie_secure": True,
    }
    with pytest.raises(ValidationError, match="MARKET_DATA_API_KEY"):
        Settings(_env_file=None, **prod, market_data_provider="massive", market_data_api_key=" ")
    Settings(_env_file=None, **prod)  # no provider: inert, and fine


def test_a_configured_vendor_never_exposes_its_key():
    from quantlab_api.marketdata import configured_vendor, vendor_status

    s = _settings(**CONFIGURED)
    assert isinstance(s.market_data_api_key, SecretStr)
    assert KEY not in repr(s) and KEY not in str(s.model_dump())
    assert KEY not in str(vendor_status(s))
    provider = configured_vendor(s, transport=FakeMassive())
    assert provider.api.auth == "bearer"
    assert KEY not in repr(provider.api) and KEY not in str(provider.describe())
    assert provider.describe()["license"] == CONFIGURED["market_data_license"]
