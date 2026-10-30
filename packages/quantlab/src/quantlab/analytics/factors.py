"""Time-series factor regressions.

``exposures`` regresses daily strategy returns on factor returns by OLS with Newey-West
(HAC) standard errors, which are robust to the heteroskedasticity and short-range
autocorrelation typical of daily returns. Alpha is the intercept annualized
arithmetically (x 252).

``sector_factors`` builds factors available for any dataset with sector metadata: the
market (benchmark total return) and, for each sector, the equal-weighted sector return
minus the market. Sector returns on day t use only assets trading on day t.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
import statsmodels.api as sm

from quantlab.data.adjust import total_returns


def sector_factors(
    bars: pd.DataFrame,
    actions: pd.DataFrame,
    sectors: pd.Series,
    benchmark: str,
    min_members: int = 3,
) -> pd.DataFrame:
    rets = total_returns(bars, actions)
    if benchmark not in rets:
        raise ValueError(f"benchmark {benchmark} not in data")
    market = rets[benchmark]
    out = {"market": market}
    members = sectors[sectors.index.isin(rets.columns) & (sectors.index != benchmark)]
    for name, group in members.groupby(members):
        cols = list(group.index)
        if len(cols) < min_members:
            continue
        out[f"sector:{name}"] = rets[cols].mean(axis=1, skipna=True) - market
    return pd.DataFrame(out)


def exposures(
    returns: pd.Series, factors: pd.DataFrame, periods: int = 252, min_obs: int = 60
) -> dict[str, Any]:
    data = pd.concat([returns.rename("y"), factors], axis=1, join="inner").dropna()
    if len(data) < min_obs:
        return {"error": f"need at least {min_obs} overlapping observations, got {len(data)}"}
    y = data["y"].to_numpy()
    X = sm.add_constant(data.drop(columns="y"), has_constant="add")
    lags = math.floor(4 * (len(data) / 100) ** (2 / 9))  # Newey-West rule of thumb
    fit = sm.OLS(y, X).fit(cov_type="HAC", cov_kwds={"maxlags": lags})
    params, tvalues = fit.params, fit.tvalues
    rows = [
        {"factor": str(name), "beta": float(params[name]), "t_stat": float(tvalues[name])}
        for name in X.columns
        if name != "const"
    ]
    resid_vol = float(np.std(fit.resid, ddof=X.shape[1]) * math.sqrt(periods))
    return {
        "method": f"OLS with Newey-West standard errors ({lags} lags)",
        "observations": len(data),
        "alpha_annualized": float(params["const"] * periods),
        "alpha_t_stat": float(tvalues["const"]),
        "r_squared": float(fit.rsquared),
        "residual_volatility": resid_vol,
        "loadings": rows,
    }
