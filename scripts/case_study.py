"""Acceptance case study, driven entirely through the public HTTP API.

Question: can a cross-sectional momentum strategy beat a passive benchmark after
transaction costs and out-of-sample validation?

Requires a running stack (API + worker): ``make api-dev`` and ``make worker``.
Usage: ``uv run python scripts/case_study.py [--api http://localhost:8000]``.
Outputs are written to ``docs/case-study/``.
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

OUT = Path(__file__).resolve().parents[1] / "docs" / "case-study"
COSTS = {"commission_bps": 5, "slippage_bps": 10}


class Client:
    def __init__(self, base: str) -> None:
        self.http = httpx.Client(base_url=base, timeout=60)
        self.headers: dict[str, str] = {}

    def register(self, email: str, name: str) -> None:
        r = self.http.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "Case-study-2026", "display_name": name},
        )
        r.raise_for_status()
        self.headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

    def get(self, path: str) -> Any:
        r = self.http.get(f"/api/v1{path}", headers=self.headers)
        r.raise_for_status()
        return r.json() if "json" in r.headers.get("content-type", "") else r.text

    def post(self, path: str, body: dict[str, Any] | None = None, key: bool = True) -> Any:
        h = dict(self.headers)
        if key:
            h["Idempotency-Key"] = uuid.uuid4().hex
        r = self.http.post(f"/api/v1{path}", headers=h, json=body or {})
        if r.status_code >= 400:
            raise RuntimeError(f"{path}: {r.status_code} {r.text}")
        return r.json()

    def wait_job(self, job_id: str, timeout: float = 600) -> dict[str, Any]:
        t0 = time.time()
        while time.time() - t0 < timeout:
            job = self.get(f"/jobs/{job_id}")
            if job["status"] not in ("queued", "running"):
                if job["status"] != "completed":
                    raise RuntimeError(f"job {job_id} {job['status']}: {job['error']}")
                return job
            time.sleep(1)
        raise TimeoutError(job_id)


def log(step: str, detail: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {step}: {detail}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    tag = uuid.uuid4().hex[:6]
    a = Client(args.api)
    a.register(f"researcher-a-{tag}@example.com", "Researcher A")
    ws = a.get("/workspaces")[0]["id"]

    # 1-2. ingest, validate and version the dataset
    job = a.post(
        f"/workspaces/{ws}/datasets/synthetic",
        {
            "slug": f"case-{tag}",
            "name": "Case study universe (synthetic)",
            "n_assets": 200,
            "start": "2014-01-02",
            "end": "2023-12-29",
            "seed": 2026,
        },
    )
    a.wait_job(job["id"])
    dv = a.get(f"/workspaces/{ws}/dataset-versions")[0]
    detail = a.get(f"/dataset-versions/{dv['id']}")
    log(
        "dataset",
        f"{dv['rows']} rows, {dv['symbol_count']} symbols, quality "
        f"{dv['quality_status']}, version {dv['version_hash'][:12]}",
    )

    # 3-6. configure and run the strategy and a naive diversified comparison
    base_cfg = {
        "start": "2015-01-02",
        "rebalance": "monthly",
        "benchmark": "SYNIDX",
        "max_weight": 0.1,
        **COSTS,
    }
    runs = {}
    for name, key, params in [
        ("Momentum 12-1, top 20", "momentum", {"lookback": 252, "skip": 21, "top_n": 20}),
        ("Equal weight (all names)", "equal_weight", {}),
    ]:
        sub = a.post(
            f"/workspaces/{ws}/experiments",
            {
                "name": name,
                "dataset_version_id": dv["id"],
                "strategy_key": key,
                "strategy_params": params,
                "config": base_cfg,
                "notes": "Case study: does momentum beat passive after costs?",
            },
        )
        a.wait_job(sub["job"]["id"])
        runs[key] = a.get(f"/experiments/{sub['experiment']['id']}")
        s = runs[key]["summary"]
        log(
            "backtest",
            f"{name}: CAGR {s['cagr']:.2%}, Sharpe {s['sharpe']:.2f}, "
            f"excess vs benchmark {s['excess_total_return']:+.2%}",
        )
    mom = runs["momentum"]
    exp_id = mom["id"]

    # 7-9. reconstruct trades/positions, equity and risk
    trades = a.get(f"/experiments/{exp_id}/trades?limit=5")
    positions = a.get(f"/experiments/{exp_id}/positions")
    analysis = a.get(f"/experiments/{exp_id}/analysis")
    log(
        "reconstruction",
        f"{trades['total']} fills; {len(positions['items'])} positions on "
        f"{positions['date']}; P&L attribution total {analysis['contributions']['total']:,.0f}",
    )

    # 10-11. cost sensitivity and walk-forward validation
    val = a.post(f"/experiments/{exp_id}/validation")
    a.wait_job(val["job"]["id"], timeout=1800)
    val = a.get(f"/experiments/{exp_id}/validation")["result"]
    wf = val["walk_forward"]
    log(
        "validation",
        f"walk-forward IS Sharpe {wf['mean_in_sample_sharpe']:.2f} -> OOS "
        f"{wf['mean_oos_sharpe']:.2f}; breakeven cost x{val['cost']['breakeven_multiplier']}",
    )

    # 15. a second researcher reproduces the experiment from its saved metadata
    b = Client(args.api)
    b.register(f"researcher-b-{tag}@example.com", "Researcher B")
    # Membership by invitation (#5): A invites B, who accepts with the one-time link.
    invite = a.post(
        f"/workspaces/{ws}/invitations",
        {"email": f"researcher-b-{tag}@example.com", "role": "editor"},
        key=False,
    )
    token = invite["accept_path"].split("#token=", 1)[1]
    b.post("/auth/invitations/accept", {"token": token}, key=False)
    rep = b.post(f"/experiments/{exp_id}/reproduce")
    b.wait_job(rep["job"]["id"])
    repro = a.get(f"/experiments/{exp_id}/reproducibility")
    match = all(c["fingerprint_match"] for c in repro["comparisons"])
    log("reproduction", f"by researcher B: fingerprint match = {match}")

    # 12. integrity report (after validation and reproduction)
    integrity = a.get(f"/experiments/{exp_id}/integrity")
    log("integrity", json.dumps(integrity["summary"]))

    # 13. grounded explanation
    answers = [
        a.post(f"/experiments/{exp_id}/assistant", {"question": q}, key=False)
        for q in (
            "Did the strategy beat the benchmark after costs?",
            "Is the result robust out-of-sample or just luck?",
            "Should I invest in this strategy?",
        )
    ]
    log(
        "assistant",
        f"{len(answers)} answers; grounded={[x['verification'].get('grounded') for x in answers]}",
    )

    # 14. export the report
    (OUT / "report.md").write_text(a.get(f"/experiments/{exp_id}/report?format=md"))
    (OUT / "report.html").write_text(a.get(f"/experiments/{exp_id}/report?format=html"))
    (OUT / "manifest.json").write_text(
        json.dumps(a.get(f"/experiments/{exp_id}/manifest"), indent=2, default=str)
    )
    summary = {
        "dataset": {
            k: dv[k]
            for k in (
                "version_hash",
                "rows",
                "symbol_count",
                "start_date",
                "end_date",
                "is_synthetic",
                "quality_status",
            )
        },
        "quality_checks": [
            (c["name"], c["status"], c["count"]) for c in detail["quality"]["checks"]
        ],
        "config": base_cfg,
        "experiments": {
            k: {"name": v["name"], "summary": v["summary"], "fingerprint": v["fingerprint"]}
            for k, v in runs.items()
        },
        "benchmark": analysis["benchmark_performance"]["values"],
        "costs": analysis["costs"]["values"],
        "risk": analysis["risk"],
        "drawdowns": analysis["drawdowns"][:3],
        "validation": {
            "walk_forward": {
                k: wf[k]
                for k in (
                    "method",
                    "train_sessions",
                    "test_sessions",
                    "mean_in_sample_sharpe",
                    "mean_oos_sharpe",
                    "distinct_params_selected",
                )
            },
            "folds": [
                {
                    k: f.get(k)
                    for k in (
                        "fold",
                        "test",
                        "selected_params",
                        "in_sample_sharpe",
                        "oos_sharpe",
                        "oos_return",
                        "oos_benchmark_return",
                    )
                }
                for f in wf["folds"]
            ],
            "cost": val["cost"],
            "bootstrap": val["bootstrap"],
            "deflated": val["deflated"],
            "leakage": val["leakage"],
            "sensitivity": val["sensitivity"]["summary"],
        },
        "integrity": integrity,
        "assistant": [
            {
                "question": q,
                "answer": x["text"],
                "provider": x["provider"],
                "grounded": x["verification"].get("grounded"),
                "declined": x["declined"],
            }
            for q, x in zip(
                (
                    "Did the strategy beat the benchmark after costs?",
                    "Is the result robust out-of-sample or just luck?",
                    "Should I invest in this strategy?",
                ),
                answers,
                strict=True,
            )
        ],
        "reproduction": repro,
    }
    (OUT / "results.json").write_text(json.dumps(summary, indent=2, default=str))
    log("done", f"outputs in {OUT}")


if __name__ == "__main__":
    main()
