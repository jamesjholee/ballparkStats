"""
calibrate.py — Fit kHR / Matchup / HR-Form weights to Kasper's published values.

Usage (CLI):
    python -m app.services.calibrate

Workflow:
  1. fit_khr_weights()     — NNLS fit of kHR weights to the 6/15 labeled rows.
  2. fit_matchup_weights() — requires YOUR computed components for those rows.
  3. fit_hr_form()         — grid-search HR-Form window by r vs published form %.
  4. check_arrow_accuracy()— % of arrows matching Kasper's direction.

All labeled data is hardcoded from the 6/15/2026 Kasper screenshots.
"""

import numpy as np
import pandas as pd
from scipy.optimize import nnls

# ---------------------------------------------------------------------------
# Kasper's published rows (6/15/2026 screenshots)
# ---------------------------------------------------------------------------

LABELS = pd.DataFrame(
    [
        # name,               kHR,    ISO,   brl,   fb,    hh,    matchup, xwoba
        ("Kyle Schwarber",  63.898, 0.310, 24.5, 36.2, 74.0, 78.371, 0.388),
        ("Bryce Harper",    61.255, 0.269, 14.4, 28.3, 57.9, 58.143, 0.400),
        ("Bryson Stott",    53.615, 0.142,  5.7, 27.1, 40.3, 58.094, 0.310),
        ("Brandon Marsh",   57.321, 0.178,  9.1, 27.3, 53.3, 57.967, 0.339),
        ("J.T. Realmuto",   47.620, 0.162,  9.7, 24.7, 52.8, 54.529, 0.327),
        ("Alec Bohm",       48.760, 0.114,  4.7, 20.0, 47.3, 53.252, 0.325),
        ("Trea Turner",     40.196, 0.158,  6.9, 24.6, 48.9, 46.183, 0.317),
        ("Kyle Stowers",    49.441, 0.220, 17.2, 31.3, 62.0, 49.103, 0.346),
        ("Owen Caissie",    50.695, 0.182, 14.0, 28.6, 47.8, 48.552, 0.296),
        ("Jakob Marsee",    50.846, 0.108,  5.9, 24.3, 46.6, 47.823, 0.327),
        ("Liam Hicks",      46.685, 0.160,  4.4, 23.9, 38.1, 46.320, 0.326),
        ("Joe Mack",        47.099, 0.087,  8.3, 22.9, 52.1, 39.136, 0.288),
        ("Otto Lopez",      40.040, 0.134,  6.1, 22.1, 44.5, 37.918, 0.341),
        ("Connor Norby",    41.228, 0.159, 10.1, 31.8, 43.5, 32.171, 0.298),
    ],
    columns=["name", "kHR", "iso", "brl", "fb", "hh", "matchup", "xwoba"],
)

HR_FORM_LABELS = pd.DataFrame(
    [
        ("Kyle Schwarber",    42, "down"),
        ("Bryce Harper",      66, "up"),
        ("Bryson Stott",      47, "down"),
        ("Brandon Marsh",     56, "down"),
        ("J.T. Realmuto",     37, "down"),
        ("Alec Bohm",         42, "down"),
        ("Rafael Marchan",    59, "flat"),
        ("Trea Turner",       31, "down"),
        ("Justin Crawford",   50, "down"),
        ("Kyle Stowers",      50, "down"),
        ("Owen Caissie",      54, "up"),
        ("Jakob Marsee",      55, "flat"),
        ("Liam Hicks",        47, "up"),
        ("Joe Mack",          59, "flat"),
        ("Otto Lopez",        43, "up"),
        ("Connor Norby",      55, "down"),
    ],
    columns=["name", "form_pct", "arrow"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _z(a: np.ndarray) -> np.ndarray:
    a = a.astype(float)
    sd = float(a.std()) or 1.0
    return (a - a.mean()) / sd


# ---------------------------------------------------------------------------
# Fitting functions
# ---------------------------------------------------------------------------

def fit_khr_weights(df: pd.DataFrame = LABELS) -> dict:
    """
    Recover non-negative weights for (ISO, Brl, FB, HH, xwOBA) z-scores
    that reproduce Kasper's kHR via: kHR = 50 + 10 * (w · z(features)).

    Returns {'weights': dict, 'rmse': float, 'r': float}.
    Acceptance: r ≥ 0.70.
    """
    feats = ["iso", "brl", "fb", "hh", "xwoba"]
    X = np.column_stack([_z(df[c].values) for c in feats])
    y = (df["kHR"].values - 50.0) / 10.0
    w, _ = nnls(X, y)
    w = w / w.sum() if w.sum() else w
    weights = dict(zip(feats, np.round(w, 3)))
    pred = 50.0 + 10.0 * (X @ w)
    rmse = float(np.sqrt(np.mean((pred - df["kHR"].values) ** 2)))
    corr = float(np.corrcoef(pred, df["kHR"].values)[0, 1])
    print(f"[kHR fit]  weights={weights}  RMSE={rmse:.2f}  r={corr:.3f}")
    return {"weights": weights, "rmse": rmse, "r": corr}


def fit_matchup_weights(components: pd.DataFrame) -> dict:
    """
    Fit Matchup blend weights to Kasper's published Matchup values.

    components: DataFrame with columns
        ['khr','zone_fit','pitcher_vuln','park_hr_factor','environment','matchup_kasper']
    where each row is one of the LABELS hitters, computed by YOUR model.

    Returns {'weights': dict, 'rmse': float, 'r': float}.
    """
    feats = ["khr", "zone_fit", "pitcher_vuln", "park_hr_factor", "environment"]
    X = np.column_stack([_z(components[c].values) for c in feats])
    y = (components["matchup_kasper"].values - 50.0) / 10.0
    w, _ = nnls(X, y)
    w = w / w.sum() if w.sum() else w
    weights = dict(zip(feats, np.round(w, 3)))
    pred = 50.0 + 10.0 * (X @ w)
    rmse = float(np.sqrt(np.mean((pred - components["matchup_kasper"].values) ** 2)))
    corr = float(np.corrcoef(pred, components["matchup_kasper"].values)[0, 1])
    print(f"[Matchup fit]  weights={weights}  RMSE={rmse:.2f}  r={corr:.3f}")
    return {"weights": weights, "rmse": rmse, "r": corr}


def fit_hr_form(
    form_table: pd.DataFrame,
    windows: tuple = (10, 14, 21, 25, 30),
) -> dict:
    """
    Grid-search the HR-Form window by correlation to Kasper's published form %.

    form_table: one row per labeled hitter, columns 'name' plus computed
        form_pct per window named 'form_w10', 'form_w14', … (from _hr_form_kasper
        at each window length). Merges on name with HR_FORM_LABELS.

    Returns dict of {window: {'r', 'rmse', 'n'}}; also prints the best window.
    """
    merged = HR_FORM_LABELS.merge(form_table, on="name", how="inner")
    if merged.empty:
        print("[HR Form] no name matches — check name spelling vs HR_FORM_LABELS")
        return {}

    results = {}
    for w in windows:
        col = f"form_w{w}"
        if col not in merged.columns:
            continue
        a = merged[col].astype(float).values
        b = merged["form_pct"].astype(float).values
        mask = ~np.isnan(a)
        if mask.sum() < 3:
            continue
        r = float(np.corrcoef(a[mask], b[mask])[0, 1])
        rmse = float(np.sqrt(np.mean((a[mask] - b[mask]) ** 2)))
        results[w] = {"r": round(r, 3), "rmse": round(rmse, 2), "n": int(mask.sum())}
        print(f"[HR Form] window={w:>2}d  r={r:+.3f}  RMSE={rmse:5.2f}  n={mask.sum()}")

    if results:
        best = max(results, key=lambda k: results[k]["r"])
        print(f"[HR Form] best window ~= {best}d  → set FORM_WINDOW_DAYS={best}")
    return results


def check_arrow_accuracy(arrow_table: pd.DataFrame) -> float:
    """
    arrow_table: ['name', 'trend'] where trend is '↑'/'↓'/'→' or 'up'/'down'/'flat'.
    Returns the fraction of arrows that match Kasper's published direction.
    """
    sym = {"↑": "up", "↓": "down", "→": "flat"}
    m = HR_FORM_LABELS.merge(arrow_table, on="name", how="inner")
    m["mine"] = m["trend"].map(sym).fillna(m["trend"])
    acc = float((m["mine"] == m["arrow"]).mean())
    print(f"[HR Form] arrow direction accuracy = {acc:.0%} on {len(m)} hitters")
    return acc


def fit_window_years(
    fetch_khr_for_window,
    windows: tuple = (3, 4, 5, 6, 7),
) -> dict:
    """
    Grid-search WINDOW_YEARS by re-running fit_khr_weights at each window size.

    fetch_khr_for_window(n) must return a DataFrame with columns
        ['name','kHR_pred','iso','brl','fb','hh','xwoba'] for the LABELS rows,
        computed from the n-year leaderboard.

    The window maximising r is recommended as WINDOW_YEARS.
    """
    results = {}
    for n in windows:
        try:
            df = fetch_khr_for_window(n)
            out = fit_khr_weights(df)
            results[n] = out
            print(f"  WINDOW_YEARS={n}  r={out['r']:.3f}  RMSE={out['rmse']:.2f}")
        except Exception as exc:
            print(f"  WINDOW_YEARS={n}  error: {exc}")
    if results:
        best = max(results, key=lambda k: results[k]["r"])
        print(f"[window grid] best WINDOW_YEARS={best}  (set env WINDOW_YEARS={best})")
    return results


if __name__ == "__main__":
    print("=== kHR weight fit on 6/15 labeled rows ===")
    fit_khr_weights()
    print()
    print("For Matchup: compute your components for the LABELS rows, then call")
    print("  fit_matchup_weights(components_df)")
    print()
    print("For HR Form: compute form_pct per window, then call")
    print("  fit_hr_form(form_table)  to find the best FORM_WINDOW_DAYS")
