"""Populate a running local QuantLab with a demonstration workspace. Synthetic data only.

    make app-up                                   # or QUANTLAB_WORKERS=1 make app-up
    uv run python scripts/demo_seed.py            # against http://localhost:3000
    uv run python scripts/demo_seed.py --url http://localhost:21581 --email you@example.com

It drives the public API through the web tier, as the browser does, and creates:
* the account (or signs in to it);
* a 100-asset synthetic dataset, 2018-01-02 to 2023-12-29, seed 7;
* three experiments (cross-sectional momentum, equal weight, minimum variance), from
  2019-01-02 against the synthetic index;
* the validation suite on the momentum experiment, and one reproduction of it;
* a portfolio with two recorded purchases.

Then it prints where to look. A second run against the same account adds new experiments
and reuses the dataset. Only http://localhost and 127.0.0.1 URLs are accepted: the stack
is local by design (docs/RUNBOOK.md, "Exposing a deployment").
"""

from __future__ import annotations

import argparse
import time
from typing import Any
from urllib.parse import urlparse

import httpx

DATASET = {
    "slug": "demo-us",
    "name": "Synthetic US equities (demo)",
    "n_assets": 100,
    "start": "2018-01-02",
    "end": "2023-12-29",
    "seed": 7,
}
EXPERIMENTS: list[tuple[str, str, dict[str, Any]]] = [
    ("Momentum, 12-1, top 10", "momentum", {}),
    ("Equal weight", "equal_weight", {}),
    ("Minimum variance", "min_variance", {}),
]
CONFIG = {"start": "2019-01-02", "benchmark": "SYNIDX", "rebalance": "monthly"}
TERMINAL = {"completed", "failed", "cancelled", "timed_out"}


class Api:
    def __init__(self, base: str) -> None:
        self.http = httpx.Client(base_url=base.rstrip("/") + "/api/v1", timeout=60)

    def call(self, method: str, path: str, ok: tuple[int, ...] = (200, 201, 202), **kw: Any) -> Any:
        r = self.http.request(method, path, **kw)
        if r.status_code not in ok:
            raise SystemExit(f"{method} {path}: {r.status_code} {r.text[:300]}")
        return r.json() if r.content else None

    def wait(self, path: str, what: str, timeout: float = 600) -> dict[str, Any]:
        """Poll ``path`` until its ``status`` is terminal."""
        deadline = time.monotonic() + timeout
        while True:
            body = self.call("GET", path)
            if body["status"] in TERMINAL:
                if body["status"] != "completed":
                    raise SystemExit(f"{what}: {body['status']}")
                return body
            if time.monotonic() > deadline:
                raise SystemExit(f"{what}: still {body['status']} after {timeout:.0f} s")
            time.sleep(1.5)


def sign_in(api: Api, email: str, password: str) -> None:
    creds = {"email": email, "password": password}
    r = api.http.post("/auth/register", json={**creds, "display_name": "Demo"})
    if r.status_code != 201:  # an existing account (or a refused registration): sign in
        r = api.http.post("/auth/login", json=creds)
        if r.status_code != 200:
            raise SystemExit(f"could not register or sign in as {email}: {r.text[:300]}")
    api.http.headers["Authorization"] = f"Bearer {r.json()['access_token']}"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--url", default="http://localhost:3000", help="the web tier")
    p.add_argument("--email", default="demo@example.com")
    p.add_argument("--password", default="Demo-workspace-2026")
    a = p.parse_args()
    if urlparse(a.url).hostname not in {"localhost", "127.0.0.1"}:
        raise SystemExit("refusing a non-local URL: this seeds a local demonstration stack")

    api = Api(a.url)
    sign_in(api, a.email, a.password)
    ws = api.call("GET", "/workspaces")[0]["id"]
    print(f"signed in as {a.email}; workspace {ws}")

    def demo_versions() -> list[dict[str, Any]]:
        sets = api.call("GET", f"/workspaces/{ws}/datasets")
        ids = {d["id"] for d in sets if d["slug"] == DATASET["slug"]}
        rows = api.call("GET", f"/workspaces/{ws}/dataset-versions")
        return [v for v in rows if v["dataset_id"] in ids]

    versions = demo_versions()
    if not versions:
        job = api.call("POST", f"/workspaces/{ws}/datasets/synthetic", json=DATASET)
        api.wait(f"/jobs/{job['id']}", "dataset generation")
        versions = demo_versions()
    dv = versions[0]["id"]
    print(f"dataset version {versions[0]['version_hash'][:12]} (synthetic)")

    ids = {}
    for name, key, params in EXPERIMENTS:
        sub = api.call(
            "POST",
            f"/workspaces/{ws}/experiments",
            json={
                "name": name,
                "dataset_version_id": dv,
                "strategy_key": key,
                "strategy_params": params,
                "config": CONFIG,
            },
        )
        ids[key] = sub["experiment"]["id"]
    for key, eid in ids.items():
        api.wait(f"/experiments/{eid}", f"backtest {key}")
        print(f"backtest {key}: completed")

    mom = ids["momentum"]
    api.call("POST", f"/experiments/{mom}/validation")
    api.wait(f"/experiments/{mom}/validation", "validation suite")
    print("validation suite on momentum: completed")
    rep = api.call("POST", f"/experiments/{mom}/reproduce")["experiment"]["id"]
    api.wait(f"/experiments/{rep}", "reproduction")
    match = api.call("GET", f"/experiments/{rep}/reproducibility")["comparisons"][0]
    same = "match" if match["fingerprint_match"] else "DIFFER"
    print(f"reproduction: output fingerprints {same}")

    pf = api.call(
        "POST",
        f"/workspaces/{ws}/portfolios",
        json={
            "name": "Demo book",
            "dataset_version_id": dv,
            "initial_deposit": 100_000,
            "start_date": "2019-01-02",
        },
    )
    for symbol, qty in (("SYN001", 200), ("SYN002", 150)):
        api.call(
            "POST",
            f"/portfolios/{pf['id']}/transactions",
            json={"trade_date": "2019-01-03", "kind": "buy", "symbol": symbol, "quantity": qty},
        )
    print("portfolio: 2 purchases recorded")

    web = a.url.rstrip("/")
    print("\nOpen, signed in as the account above:")
    print(f"  {web}/experiments/{mom}      results, validation and the integrity report")
    print(f"  {web}/compare                select the three experiments")
    print(f"  {web}/portfolios/{pf['id']}")
    print(f"  {web}/optimize               portfolio construction")
    print("All of it is synthetic data: it shows how the platform works, not a real result.")


if __name__ == "__main__":
    main()
