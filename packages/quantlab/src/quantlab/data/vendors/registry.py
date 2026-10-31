"""Registered market-data vendors (#11).

The vendor and its licence are the user's choice, so nothing here is used unless a
deployment names a registered vendor *and* supplies its key (``quantlab_api.marketdata``
enforces that). Adding a vendor means adding an adapter and one entry below; no vendor is
preferred by the code.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from quantlab.data.providers import TokenBucket
from quantlab.data.vendors import massive
from quantlab.data.vendors.http import JsonApi, Transport, urllib_transport


@dataclass(frozen=True)
class Vendor:
    """How to reach one vendor. ``auth`` is the documented key placement."""

    name: str
    base_url: str
    auth: Literal["query", "bearer"]
    docs: str
    adapter: Callable[..., massive.MassiveProvider]

    def build(
        self,
        api_key: str,
        *,
        license: str,
        plan: str,
        requests_per_minute: float | None = None,
        transport: Transport = urllib_transport,
    ) -> massive.MassiveProvider:
        """The vendor's provider. The key goes only into the HTTP client, never elsewhere."""
        limiter = TokenBucket(requests_per_minute / 60.0) if requests_per_minute else None
        api = JsonApi(self.base_url, api_key, auth=self.auth, transport=transport, limiter=limiter)
        return self.adapter(api, license=license, plan=plan)


VENDORS: dict[str, Vendor] = {
    "massive": Vendor(
        name="massive",
        base_url=massive.BASE_URL,
        # Massive documents both ?apiKey= and "Authorization: Bearer"; the header keeps
        # the key out of every URL (https://massive.com/docs/rest/quickstart).
        auth="bearer",
        docs="docs/MARKET_DATA_PROVIDERS.md",
        adapter=massive.MassiveProvider,
    ),
}


def get_vendor(name: str) -> Vendor:
    """The registered vendor ``name`` (case-insensitive); ValueError if there is none."""
    try:
        return VENDORS[name.strip().lower()]
    except KeyError:
        raise ValueError(
            f"unknown market-data provider {name!r}; registered: {sorted(VENDORS)}"
        ) from None
