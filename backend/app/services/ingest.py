"""
Data ingestion — pulls from pybaseball, MLB Stats API, OpenWeatherMap.
Run via cron (recommended: 06:00 ET daily, then again at 14:00 ET for lineups).

Computes the full Kasper-style stat set:
  Batter: Matchup, Test Score, Ceiling, Zone Fit, HR Form, kHR, ISO, xwOBA,
          xwOBAcon, SwStr%, PulledBrl%, SweetSpot%, HH%, LA, Brl/BIP%
  Pitcher: Pitch Score, Strikeout Score, xwOBA, CSW%, SwStr%, PutAway%, Ball%,
           SIERA, PulledBrl%, Brl/BIP%
"""

import logging
import os
import time
from datetime import date, datetime, timedelta
from typing import Optional

import httpx
import numpy as np
import pandas as pd
import pybaseball
from sqlalchemy.orm import Session

from app.constants import MLB_TEAM_IDS, PARK_CF_BEARING, STADIUM_COORDS
from app.models.db import BatterStats, Game, Lineup, PitcherStats, Player
from app.services.scoring import (
    compute_khr,
    compute_sample_tier,
    score_pitcher_quality,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Multi-year baseline config (Task 1)
# ---------------------------------------------------------------------------
SEASON = int(os.getenv("SEASON", "2026"))
WINDOW_YEARS = int(os.getenv("WINDOW_YEARS", "6"))
YEARS = list(range(SEASON - WINDOW_YEARS + 1, SEASON + 1))  # e.g. 2021-2026

# HR Form mode: "percentile" (league pull, heavy) or "baseline_ratio" (fast, default)
FORM_MODE = os.getenv("FORM_MODE", "baseline_ratio")
FORM_WINDOW_DAYS = int(os.getenv("FORM_WINDOW_DAYS", "21"))

# Module-level cache keyed by date — rebuilt once per cron run
_LEADERBOARD_CACHE: dict = {}
_LEADERBOARD_CACHE_DATE = None
_LEAGUE_FORM_CACHE: dict = {}


def _py(v):
    """
    Coerce numpy scalars / pandas NA to native Python types so psycopg2 can
    serialize them. Without this, np.float64 sneaks into SQL params and
    Postgres tries to parse `np.float64(72.9)` as a function call.

    Containers are handled before pd.isna() because pd.isna(some_dict) in
    pandas 2.x returns a dict of booleans, which is truthy, so the isna
    branch would incorrectly return None for every non-empty dict/list.
    """
    if v is None:
        return None
    # Handle containers before the scalar isna check
    if isinstance(v, (np.ndarray, list)):
        return [_py(x) for x in v]
    if isinstance(v, dict):
        return {k: _py(x) for k, x in v.items()}
    # Now safe to call pd.isna on a scalar
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.floating):
        return float(v)
    if isinstance(v, np.bool_):
        return bool(v)
    return v


# Without this, you'll get rate-limited by Baseball Savant within minutes.
pybaseball.cache.enable()

# ---------------------------------------------------------------------------
# Multi-year leaderboard helpers (Task 1)
# ---------------------------------------------------------------------------

def _agg_years(fetch, years, key, weight_col, rate_cols, sum_cols) -> pd.DataFrame:
    """
    Call fetch(year) for each year in years, aggregate per `key`:
      rate_cols  → sample-weighted mean (weight = weight_col)
      sum_cols   → sum across years
    Years that fail or return empty are skipped silently.
    """
    frames = []
    for y in years:
        try:
            df = fetch(y)
            if df is not None and not df.empty and key in df.columns:
                df = df.copy()
                df["_yr"] = y
                frames.append(df)
        except Exception as exc:
            logger.debug(f"_agg_years year={y} skip: {exc}")
        time.sleep(0.4)
    if not frames:
        return pd.DataFrame()

    allf = pd.concat(frames, ignore_index=True)
    allf[weight_col] = pd.to_numeric(allf[weight_col], errors="coerce").fillna(0)

    rows = []
    for k, g in allf.groupby(key):
        row: dict = {key: k}
        w = g[weight_col]
        wsum = float(w.sum())
        for c in rate_cols:
            if c in g.columns and wsum > 0:
                vals = pd.to_numeric(g[c], errors="coerce")
                row[c] = float(np.nansum(vals * w) / wsum)
            else:
                row[c] = np.nan
        for c in sum_cols:
            if c in g.columns:
                row[c] = float(pd.to_numeric(g[c], errors="coerce").fillna(0).sum())
        # carry representative name fields
        for nm in ("first_name", "last_name"):
            if nm in g.columns:
                row[nm] = g[nm].iloc[-1]
        rows.append(row)
    return pd.DataFrame(rows)


def _agg_arsenal(fetch, years) -> pd.DataFrame:
    """Aggregate a per-pitch-type arsenal table across years, weighted by pitches."""
    frames = []
    for y in years:
        try:
            df = fetch(y, minPA=10)
            if df is not None and not df.empty:
                frames.append(df.copy())
        except Exception as exc:
            logger.debug(f"_agg_arsenal year={y} skip: {exc}")
        time.sleep(0.4)
    if not frames:
        return pd.DataFrame()

    allf = pd.concat(frames, ignore_index=True)
    wcol = "pitches" if "pitches" in allf.columns else None
    if wcol is None:
        return allf

    allf[wcol] = pd.to_numeric(allf[wcol], errors="coerce").fillna(0)
    rate_cols = [c for c in ("est_woba", "woba", "hard_hit_percent", "pitch_usage",
                             "whiff_percent") if c in allf.columns]
    rows = []
    for (pid, pt), g in allf.groupby(["player_id", "pitch_type"]):
        w = g[wcol]
        wsum = float(w.sum())
        row = {"player_id": pid, "pitch_type": pt, "pitches": wsum}
        for c in rate_cols:
            vals = pd.to_numeric(g[c], errors="coerce")
            row[c] = float(np.nansum(vals * w) / wsum) if wsum else np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def _build_fg_id_map(fg_df: pd.DataFrame) -> dict:
    """
    Map FanGraphs playerid (IDfg) → mlbam id.
    Fixes the accented/suffixed name join bug (Marchán, Jr., etc.).
    """
    id_col = next((c for c in ("playerid", "IDfg", "xMLBAMID") if c in fg_df.columns), None)
    if id_col is None:
        return {}
    fg_ids = fg_df[id_col].dropna().astype(int).unique().tolist()
    if not fg_ids:
        return {}
    try:
        time.sleep(0.4)
        mapping = pybaseball.playerid_reverse_lookup(fg_ids, name_key="fangraphs")
        return {
            int(r["key_fangraphs"]): int(r["key_mlbam"])
            for _, r in mapping.iterrows()
            if pd.notna(r.get("key_fangraphs")) and pd.notna(r.get("key_mlbam"))
        }
    except Exception as exc:
        logger.warning(f"playerid_reverse_lookup failed: {exc}")
        return {}


def build_season_leaderboards(years=None) -> dict:
    """
    Build multi-year aggregated leaderboard tables (once per cron day).
    Returns {'batters': DataFrame, 'pitchers': DataFrame}.
    Batters table includes a 'khr_v2' column (50/10 index across all MLB players).
    """
    global _LEADERBOARD_CACHE, _LEADERBOARD_CACHE_DATE
    today = date.today()
    if _LEADERBOARD_CACHE_DATE == today and _LEADERBOARD_CACHE:
        return _LEADERBOARD_CACHE

    yrs = tuple(years or YEARS)
    logger.info(f"Building {len(yrs)}-year leaderboards ({yrs[0]}-{yrs[-1]})…")

    # --- Batter exit-velo/barrel leaderboard ---
    ev = _agg_years(
        lambda y: pybaseball.statcast_batter_exitvelo_barrels(y, minBBE=50).rename(
            columns={"ev95percent": "hh_percent",
                     "anglesweetspotpercent": "sweetspot_percent",
                     "avg_hit_angle": "la"}),
        yrs, key="player_id", weight_col="attempts",
        rate_cols=["brl_percent", "hh_percent", "sweetspot_percent", "la", "avg_hit_speed"],
        sum_cols=["attempts"])

    # --- Batter expected-stats leaderboard ---
    xw = _agg_years(
        lambda y: pybaseball.statcast_batter_expected_stats(y, minPA=50).rename(
            columns={"est_woba": "xwoba"}),
        yrs, key="player_id", weight_col="pa",
        rate_cols=["xwoba"], sum_cols=["pa", "bip"])

    # --- FanGraphs multi-year aggregate (ISO, FB%, SwStr%) ---
    fg = pd.DataFrame(columns=["player_id", "iso", "fb_percent", "swstr_percent"])
    try:
        fg_raw = pybaseball.batting_stats(int(yrs[0]), int(yrs[-1]), qual=50, ind=0)
        fg_raw = fg_raw.rename(columns={
            "ISO": "iso", "FB%": "fb_percent", "SwStr%": "swstr_percent"})
        fg_id_map = _build_fg_id_map(fg_raw)
        id_col = next((c for c in ("playerid", "IDfg") if c in fg_raw.columns), None)
        if id_col and fg_id_map:
            fg_raw["player_id"] = fg_raw[id_col].map(
                lambda x: fg_id_map.get(int(x)) if pd.notna(x) else None)
            fg = (fg_raw[fg_raw["player_id"].notna()]
                  [["player_id", "iso", "fb_percent", "swstr_percent"]]
                  .copy())
            fg["player_id"] = fg["player_id"].astype(int)
    except Exception as exc:
        logger.warning(f"FanGraphs leaderboard failed: {exc}")

    # Merge batter tables
    batters = ev.merge(
        xw[["player_id", "xwoba", "pa", "bip"]], on="player_id", how="left")
    batters = batters.merge(fg, on="player_id", how="left")

    # Compute kHR v2 across the full leaderboard (stable MLB-wide index)
    from app.services.scoring import compute_khr_v2
    batters["khr_v2"] = compute_khr_v2(batters).values

    # --- Pitcher vulnerability leaderboard ---
    p_ev = _agg_years(
        lambda y: pybaseball.statcast_pitcher_exitvelo_barrels(y, minBBE=30).rename(
            columns={"brl_percent": "brl_allowed_rate", "ev95percent": "hh_allowed_rate"}),
        yrs, key="player_id", weight_col="attempts",
        rate_cols=["brl_allowed_rate", "hh_allowed_rate"], sum_cols=["attempts"])

    p_xw = _agg_years(
        lambda y: pybaseball.statcast_pitcher_expected_stats(y, minPA=30).rename(
            columns={"est_woba": "xwoba_allowed"}),
        yrs, key="player_id", weight_col="pa",
        rate_cols=["xwoba_allowed"], sum_cols=["pa"])

    pitchers = p_ev.merge(
        p_xw[["player_id", "xwoba_allowed"]] if not p_xw.empty else pd.DataFrame(columns=["player_id"]),
        on="player_id", how="left")

    _LEADERBOARD_CACHE = {"batters": batters, "pitchers": pitchers}
    _LEADERBOARD_CACHE_DATE = today
    logger.info(
        f"Leaderboards built: {len(batters)} batters, {len(pitchers)} pitchers")
    return _LEADERBOARD_CACHE


# ---------------------------------------------------------------------------
# Kasper-style HR Form helpers (Task 3)
# ---------------------------------------------------------------------------

def _compute_power_composite(bbe: pd.DataFrame) -> float:
    """
    Recent-power composite: barrel_rate*0.40 + xwobacon*0.30 +
    hardhit_rate*0.20 + avg_ev/100*0.10.
    Returns NaN when bbe is empty.
    """
    if bbe is None or bbe.empty:
        return np.nan
    n = len(bbe)
    barrel = (
        float(bbe["launch_speed_angle"].eq(6).sum() / n)
        if "launch_speed_angle" in bbe.columns else np.nan)
    hh = (
        float((bbe["launch_speed"] >= 95).sum() / n)
        if "launch_speed" in bbe.columns else np.nan)
    ev = (
        float(bbe["launch_speed"].mean() / 100.0)
        if "launch_speed" in bbe.columns else np.nan)
    xwobacon = (
        float(pd.to_numeric(bbe["estimated_woba_using_speedangle"],
                            errors="coerce").mean())
        if "estimated_woba_using_speedangle" in bbe.columns else np.nan)

    weights = {"barrel_rate": 0.40, "xwobacon": 0.30, "hardhit_rate": 0.20, "avg_ev": 0.10}
    parts = {"barrel_rate": barrel, "xwobacon": xwobacon, "hardhit_rate": hh, "avg_ev": ev}
    num = sum(weights[k] * v for k, v in parts.items() if pd.notna(v))
    den = sum(weights[k] for k, v in parts.items() if pd.notna(v))
    return num / den if den else np.nan


def _hr_form_kasper(
    df: pd.DataFrame,
    league_ref: Optional[np.ndarray] = None,
    window_days: Optional[int] = None,
    asof: Optional[date] = None,
) -> tuple:
    """
    Kasper-style HR Form: (form_pct, arrow).
    percentile mode: form_pct = % of league below this hitter's recent composite.
    baseline_ratio mode (default): recent composite / full-window composite → %.
    Arrow: sign(last-7d composite − prior-14d composite), thresholds ±5%.
    """
    window = window_days or FORM_WINDOW_DAYS
    bbe = df[df["type"] == "X"].copy()
    if bbe.empty:
        return None, "flat"

    bbe["game_date"] = pd.to_datetime(bbe["game_date"])
    asof_dt = pd.to_datetime(asof or date.today())
    cutoff = asof_dt - pd.Timedelta(days=window)
    recent_bbe = bbe[bbe["game_date"] >= cutoff]

    if recent_bbe.empty:
        return None, "flat"

    comp = _compute_power_composite(recent_bbe)

    if FORM_MODE == "percentile" and league_ref is not None and len(league_ref) > 0 and not np.isnan(comp):
        form_pct = float((league_ref < comp).mean() * 100)
    else:
        baseline_comp = _compute_power_composite(bbe)
        if pd.isna(baseline_comp) or baseline_comp == 0 or pd.isna(comp):
            form_pct = 50.0
        else:
            ratio = comp / baseline_comp
            form_pct = float(100.0 * ratio / (ratio + 1.0))

    # Arrow: last 7d vs prior 14d within the window
    cutoff_short = asof_dt - pd.Timedelta(days=7)
    short_bbe = bbe[bbe["game_date"] >= cutoff_short]
    prior_bbe = bbe[
        (bbe["game_date"] >= cutoff) & (bbe["game_date"] < cutoff_short)]

    short_comp = _compute_power_composite(short_bbe) if not short_bbe.empty else np.nan
    prior_comp = _compute_power_composite(prior_bbe) if not prior_bbe.empty else np.nan

    if pd.isna(short_comp) or pd.isna(prior_comp) or prior_comp == 0:
        arrow = "flat"
    elif short_comp > prior_comp * 1.05:
        arrow = "up"
    elif short_comp < prior_comp * 0.95:
        arrow = "down"
    else:
        arrow = "flat"

    return (round(form_pct, 1) if not np.isnan(form_pct) else None), arrow

MLB_API_BASE = "https://statsapi.mlb.com/api/v1"

# MLB Stats API uses different abbreviations than our internal convention in a
# few cases. Normalize at the boundary so the rest of the code stays clean.
_ABBREV_NORMALIZE: dict[str, str] = {
    "AZ": "ARI",   # Diamondbacks — API returns "AZ", we use "ARI" everywhere
    "CWS": "CHW",  # White Sox — API sometimes returns "CWS"
    "WAS": "WSH",  # Nationals — API sometimes returns "WAS"
}


def _norm_team(code: Optional[str]) -> Optional[str]:
    if not code:
        return code
    return _ABBREV_NORMALIZE.get(code, code)


OWM_BASE = "https://api.openweathermap.org/data/2.5/weather"

# Lookback window for player stats refreshes
# Self-adjusts year-round: in early April pulls from opening day (~7-10 days);
# mid-summer caps at 60 days; protects against pulling stale data in late season.
LOOKBACK_DAYS = 60


def _lookback_start(end: date) -> date:
    """Return max(season_start_for_year, end - LOOKBACK_DAYS)."""
    season_start = date(end.year, 3, 15)  # spring + early-season buffer
    cap = end - timedelta(days=LOOKBACK_DAYS)
    return max(season_start, cap)


# =============================================================================
# MLB STATS API
# =============================================================================


async def fetch_todays_games(target_date: Optional[date] = None) -> list[dict]:
    target_date = target_date or date.today()
    url = (
        f"{MLB_API_BASE}/schedule"
        f"?sportId=1&date={target_date.isoformat()}"
        f"&hydrate=probablePitcher,lineups,team"
    )
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()

    games = []
    for date_block in data.get("dates", []):
        for g in date_block.get("games", []):
            # Projected lineups from hydrated schedule (available before game start)
            lineups_block = g.get("lineups", {}) or {}
            projected = {
                "home": _extract_projected_lineup(lineups_block.get("homePlayers", [])),
                "away": _extract_projected_lineup(lineups_block.get("awayPlayers", [])),
            }
            home_abbr = _norm_team(g["teams"]["home"]["team"]["abbreviation"])
            away_abbr = _norm_team(g["teams"]["away"]["team"]["abbreviation"])
            games.append(
                {
                    "game_pk": g["gamePk"],
                    "game_date": target_date,
                    "game_time_utc": g.get("gameDate"),
                    "home_team": home_abbr,
                    "away_team": away_abbr,
                    "home_pitcher_id": g["teams"]["home"]
                    .get("probablePitcher", {})
                    .get("id"),
                    "away_pitcher_id": g["teams"]["away"]
                    .get("probablePitcher", {})
                    .get("id"),
                    "venue_team": home_abbr,
                    "status": g["status"]["abstractGameState"],
                    "projected_lineup": projected,
                }
            )
    return games


def _extract_projected_lineup(players: list) -> list[dict]:
    """
    The schedule's lineups.homePlayers / awayPlayers is a list of player dicts
    in batting-order sequence. Each has at least an `id` (mlbam_id).
    """
    out = []
    for i, p in enumerate(players[:9]):
        pid = p.get("id") if isinstance(p, dict) else None
        if pid:
            out.append(
                {
                    "batter_id": int(pid),
                    "batting_order": i + 1,
                    "confirmed": 0,  # projected, not confirmed
                }
            )
    return out


async def fetch_team_hitters(team_code: str) -> list[int]:
    """
    Get all active position players (non-pitchers) on a team's 26-man roster.
    Returns a list of MLBAM IDs.

    Uses the MLB Stats API roster endpoint with rosterType=active. Filters out
    pitchers by position code. Returns [] on any failure (caller should treat
    this as a soft miss and continue).
    """
    team_id = MLB_TEAM_IDS.get(team_code)
    if not team_id:
        logger.warning(f"no MLB team ID known for {team_code}")
        return []

    url = f"{MLB_API_BASE}/teams/{team_id}/roster?rosterType=active"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        logger.warning(f"roster fetch failed for {team_code} ({team_id}): {e}")
        return []

    hitters = []
    for entry in data.get("roster", []):
        # Position codes: P=Pitcher, TWP=Two-way (e.g. Ohtani — keep as hitter)
        pos = entry.get("position", {}).get("abbreviation", "")
        if pos == "P":
            continue
        person = entry.get("person", {})
        pid = person.get("id")
        if pid:
            hitters.append(int(pid))
    return hitters


async def fetch_lineup(game_pk: int, projected: Optional[dict] = None) -> dict:
    """
    Get lineups for a game. Prefers confirmed (boxscore) lineups when the game
    has started. Falls back to projected lineups passed in from the schedule
    (which are populated by MLB hours before first pitch).
    """
    url = f"{MLB_API_BASE}/game/{game_pk}/boxscore"
    confirmed = {"home": [], "away": []}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url)
            if r.status_code == 200:
                data = r.json()
                for side in ("home", "away"):
                    team_data = data.get("teams", {}).get(side, {})
                    batting_order = team_data.get("battingOrder", [])
                    for i, pid in enumerate(batting_order[:9]):
                        confirmed[side].append(
                            {
                                "batter_id": int(pid),
                                "batting_order": i + 1,
                                "confirmed": 1,
                            }
                        )
    except Exception as e:
        logger.warning(f"boxscore fetch failed for {game_pk}: {e}")

    # Use confirmed if either side has data; otherwise fall back to projected
    has_confirmed = bool(confirmed["home"]) or bool(confirmed["away"])
    if has_confirmed:
        # Fill in missing side from projected if needed
        for side in ("home", "away"):
            if not confirmed[side] and projected and projected.get(side):
                confirmed[side] = projected[side]
        return confirmed

    if projected:
        return projected
    return {"home": [], "away": []}


async def fetch_player_meta(mlbam_id: int) -> Optional[dict]:
    """Get name, team, position, batting hand."""
    url = f"{MLB_API_BASE}/people/{mlbam_id}"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url)
        if r.status_code != 200:
            return None
        data = r.json()
    if not data.get("people"):
        return None
    p = data["people"][0]
    return {
        "mlbam_id": p["id"],
        "full_name": p["fullName"],
        "position": p.get("primaryPosition", {}).get("abbreviation", "?"),
        "bats": p.get("batSide", {}).get("code", "?"),
        "throws": p.get("pitchHand", {}).get("code", "?"),
        "team": _norm_team(p.get("currentTeam", {}).get("abbreviation")),
    }


# =============================================================================
# WEATHER
# =============================================================================


async def fetch_weather(team_code: str) -> dict:
    api_key = os.getenv("OPENWEATHER_API_KEY")
    if not api_key or team_code not in STADIUM_COORDS:
        return {}
    lat, lon, _ = STADIUM_COORDS[team_code]
    url = f"{OWM_BASE}?lat={lat}&lon={lon}&appid={api_key}&units=imperial"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url)
        if r.status_code != 200:
            return {}
        data = r.json()

    wind_deg = data.get("wind", {}).get("deg", 0)
    cf_bearing = PARK_CF_BEARING.get(team_code, 0)
    blow_to = (wind_deg + 180) % 360
    relative = (blow_to - cf_bearing) % 360
    if relative > 180:
        relative -= 360
    if -45 <= relative <= 45:
        wind_dir = "out to CF"
    elif 45 < relative <= 135:
        wind_dir = "out to RF"
    elif -135 <= relative < -45:
        wind_dir = "out to LF"
    else:
        wind_dir = "in from CF"

    return {
        "temp": round(data["main"]["temp"]),
        "wind": round(data["wind"]["speed"]),
        "wind_dir": wind_dir,
        "condition": data.get("weather", [{}])[0].get("main", "Clear").lower(),
        "humidity": data["main"]["humidity"],
    }


# =============================================================================
# STATCAST INGESTION
# =============================================================================


def _hot_zones(df: pd.DataFrame) -> list[list[float]]:
    """3x3 grid of % of HRs by zone (1-9 grid)."""
    hrs = df[df["events"] == "home_run"]
    if hrs.empty:
        return [[0.0] * 3 for _ in range(3)]
    grid = [[0.0] * 3 for _ in range(3)]
    total = 0
    for _, row in hrs.iterrows():
        z = row.get("zone")
        if pd.isna(z) or z < 1 or z > 9:
            continue
        z = int(z)
        r, c = (z - 1) // 3, (z - 1) % 3
        grid[r][c] += 1
        total += 1
    if total == 0:
        return [[0.0] * 3 for _ in range(3)]
    return [[round(v / total * 100, 1) for v in row] for row in grid]


def _woba_zones(df: pd.DataFrame) -> list[list[float]]:
    """
    3x3 grid of batter's xwOBA on pitches in each zone.
    Uses estimated_woba_using_speedangle. Falls back to 0.320 (league avg)
    when a zone has fewer than 8 pitches (sample too thin to trust).
    """
    grid = [[0.320] * 3 for _ in range(3)]
    if "zone" not in df.columns or "estimated_woba_using_speedangle" not in df.columns:
        return grid
    in_zone = df[df["zone"].between(1, 9)]
    if in_zone.empty:
        return grid
    # Aggregate by zone using PA-ending events only (events column non-null)
    plate_app = in_zone[in_zone["events"].notna()]
    for z in range(1, 10):
        cell = plate_app[plate_app["zone"] == z]
        if len(cell) >= 8:
            v = cell["estimated_woba_using_speedangle"].mean()
            if not pd.isna(v):
                r, c = (z - 1) // 3, (z - 1) % 3
                grid[r][c] = round(float(v), 3)
    return grid


def _swing_rates(df: pd.DataFrame) -> tuple[float, float]:
    """
    Returns (z_swing_rate, o_swing_rate) as percentages.
    Z = zones 1-9 (in strike zone). O = zones 11-14 (just outside).
    """
    if "zone" not in df.columns or "description" not in df.columns:
        return 67.0, 28.0  # league averages

    swing_descs = {
        "swinging_strike",
        "swinging_strike_blocked",
        "foul",
        "foul_tip",
        "hit_into_play",
        "hit_into_play_no_out",
        "hit_into_play_score",
    }
    df_z = df[df["zone"].between(1, 9)]
    df_o = df[df["zone"].between(11, 14)]

    z_swing = (
        (df_z["description"].isin(swing_descs).sum() / len(df_z) * 100)
        if len(df_z)
        else 67.0
    )
    o_swing = (
        (df_o["description"].isin(swing_descs).sum() / len(df_o) * 100)
        if len(df_o)
        else 28.0
    )
    return round(float(z_swing), 1), round(float(o_swing), 1)


def _pitcher_zone_profile(df: pd.DataFrame) -> list[list[float]]:
    if df.empty:
        return [[11.1] * 3 for _ in range(3)]
    in_zone = df[df["zone"].between(1, 9)]
    total = len(in_zone) or 1
    grid = [[0.0] * 3 for _ in range(3)]
    for z in range(1, 10):
        cnt = (in_zone["zone"] == z).sum()
        r, c = (z - 1) // 3, (z - 1) % 3
        grid[r][c] = round(cnt / total * 100, 1)
    return grid


def _pitcher_zone_profile_by_pitch_type(df: pd.DataFrame, top_n: int = 3) -> dict:
    """
    Per-pitch-type 3x3 zone profile for the pitcher's top N pitch types.
    Returns {"FF": [[...], [...], [...]], "SL": ...} keyed by pitch_type code.
    """
    if df.empty or "pitch_type" not in df.columns or "zone" not in df.columns:
        return {}
    top_types = df["pitch_type"].value_counts().head(top_n).index.tolist()
    out = {}
    for pt in top_types:
        if pd.isna(pt):
            continue
        subset = df[df["pitch_type"] == pt]
        out[str(pt)] = _pitcher_zone_profile(subset)
    return out


def _edge_and_heart_pct(df: pd.DataFrame) -> tuple[float, float]:
    """
    Edge% = pitches in zones 1, 3, 7, 9 (in-zone corners) + 11-14 (just-off-plate).
    Heart% = pitches in zone 5 (middle-middle).
    """
    if df.empty or "zone" not in df.columns:
        return 38.0, 12.0
    total = len(df)
    edge_zones = {1, 3, 7, 9, 11, 12, 13, 14}
    edge_cnt = df["zone"].isin(edge_zones).sum()
    heart_cnt = (df["zone"] == 5).sum()
    return (
        round(float(edge_cnt / total * 100), 1) if total else 38.0,
        round(float(heart_cnt / total * 100), 1) if total else 12.0,
    )


def _hr_form_pct_and_arrow(df: pd.DataFrame) -> tuple[float, str]:
    """
    LEGACY display metric — kept for backwards compat in API payload.
    Form v2 is what actually drives ranking.

    HR Form % — share of recent batted balls that have HR-shaped contact
    (barrels). Arrow compares last-15-day rate to the prior 15.
    """
    bbe = df[df["type"] == "X"]
    if bbe.empty:
        return 0.0, "flat"

    end = pd.to_datetime(bbe["game_date"]).max()
    if pd.isna(end):
        return 0.0, "flat"
    cutoff_recent = end - pd.Timedelta(days=15)
    cutoff_prior = end - pd.Timedelta(days=30)

    recent = bbe[pd.to_datetime(bbe["game_date"]) >= cutoff_recent]
    prior = bbe[
        (pd.to_datetime(bbe["game_date"]) < cutoff_recent)
        & (pd.to_datetime(bbe["game_date"]) >= cutoff_prior)
    ]

    def _barrel_pct(d):
        if d.empty:
            return 0.0
        if "launch_speed_angle" not in d.columns:
            return 0.0
        return d["launch_speed_angle"].eq(6).sum() / len(d) * 100

    recent_pct = _barrel_pct(recent)
    prior_pct = _barrel_pct(prior)

    if recent_pct > prior_pct + 3:
        arrow = "up"
    elif recent_pct < prior_pct - 3:
        arrow = "down"
    else:
        arrow = "flat"

    overall = _barrel_pct(bbe)
    return round(float(overall), 1), arrow


def _bbe_metrics(bbe: pd.DataFrame, hrs_count: int = None) -> dict:
    """Compute the five Form v2 metric values from a batted-balls dataframe."""
    if bbe is None or bbe.empty:
        return {}
    n = len(bbe)
    barrel_rate = (
        (bbe["launch_speed_angle"].eq(6).sum() / n * 100)
        if "launch_speed_angle" in bbe.columns
        else None
    )
    hard_hit = (
        ((bbe["launch_speed"] >= 95).sum() / n * 100)
        if "launch_speed" in bbe.columns
        else None
    )
    xwoba_con = (
        float(bbe["estimated_woba_using_speedangle"].mean())
        if "estimated_woba_using_speedangle" in bbe.columns
        else None
    )
    if xwoba_con is not None and pd.isna(xwoba_con):
        xwoba_con = None
    bat_speed = None
    if "bat_speed" in bbe.columns:
        v = bbe["bat_speed"].mean()
        bat_speed = float(v) if not pd.isna(v) else None
    if hrs_count is None:
        hrs_count = (
            int((bbe["events"] == "home_run").sum()) if "events" in bbe.columns else 0
        )
    hr_per_bbe = hrs_count / n if n else 0.0
    return {
        "barrel_rate": barrel_rate,
        "hard_hit_rate": hard_hit,
        "xwoba_con": xwoba_con,
        "bat_speed": bat_speed,
        "hr_per_bbe": hr_per_bbe,
    }


def _recent_window(df: pd.DataFrame, n_bbe_target: int = 25) -> pd.DataFrame:
    """
    Return the most-recent N batted balls in play.
    Sorted by game_date descending, then take first N rows of BIP.
    """
    if df.empty:
        return df
    bbe = df[df["type"] == "X"].copy()
    if bbe.empty:
        return bbe
    bbe = bbe.sort_values("game_date", ascending=False)
    return bbe.head(n_bbe_target)


def _prior_window(df: pd.DataFrame, recent_n: int = 25) -> pd.DataFrame:
    """Batted balls older than the recent window — used as fallback baseline."""
    if df.empty:
        return df
    bbe = df[df["type"] == "X"].copy()
    if bbe.empty or len(bbe) <= recent_n:
        return bbe.iloc[0:0]  # empty
    bbe = bbe.sort_values("game_date", ascending=False)
    return bbe.iloc[recent_n:]


def fetch_season_baseline(mlbam_id: int) -> Optional[dict]:
    """
    Pull season-to-date Statcast for this batter and compute baseline metrics.
    Returns None if season sample is too thin (<50 BBE).

    Cached by pybaseball, so subsequent calls in the same day are cheap.
    """
    today = date.today()
    season_start = date(today.year, 3, 15)  # pre-season + spring
    if today <= season_start:
        return None
    try:
        df = pybaseball.statcast_batter(
            season_start.isoformat(), today.isoformat(), mlbam_id
        )
    except Exception as e:
        logger.warning(f"season baseline fetch failed for {mlbam_id}: {e}")
        return None
    if df.empty:
        return None
    bbe = df[df["type"] == "X"]
    if len(bbe) < 50:
        return None
    return _bbe_metrics(bbe)


def refresh_batter_stats(
    db: Session,
    mlbam_id: int,
    leaderboard: Optional[dict] = None,
    league_form_ref: Optional[np.ndarray] = None,
) -> Optional[BatterStats]:
    """Pull rolling lookback window for a batter, compute the full Kasper stat set."""
    end = date.today()
    start = _lookback_start(end)
    try:
        df = pybaseball.statcast_batter(start.isoformat(), end.isoformat(), mlbam_id)
    except Exception as e:
        logger.error(f"statcast_batter failed for {mlbam_id}: {e}")
        return None
    if df.empty:
        return None

    bbe = df[df["type"] == "X"].copy()
    n_bbe = len(bbe)

    # Core rates
    barrel_pct = (bbe["launch_speed_angle"].eq(6).sum() / n_bbe * 100) if n_bbe else 0
    hard_hit_pct = ((bbe["launch_speed"] >= 95).sum() / n_bbe * 100) if n_bbe else 0
    sweet_spot_pct = (
        (((bbe["launch_angle"] >= 8) & (bbe["launch_angle"] <= 32)).sum() / n_bbe * 100)
        if n_bbe
        else 0
    )

    # Pulled barrels — pulled = hc_x position relative to handedness
    # Simpler heuristic: barrels with hit_distance > 350ft to pull side
    pulled_barrels = 0
    if n_bbe and "launch_speed_angle" in bbe.columns:
        barrels_df = bbe[bbe["launch_speed_angle"] == 6]
        if not barrels_df.empty and "hc_x" in barrels_df.columns:
            _stand_vals = df["stand"].dropna()
            stand = str(_stand_vals.iloc[0]) if len(_stand_vals) else "R"
            if stand == "R":
                pulled = barrels_df[barrels_df["hc_x"] < 125]
            else:
                pulled = barrels_df[barrels_df["hc_x"] > 125]
            pulled_barrels = len(pulled)
    pulled_barrel_pct = (pulled_barrels / n_bbe * 100) if n_bbe else 0

    # Whiff / SwStr%
    swings = df[
        df["description"].isin(
            [
                "swinging_strike",
                "swinging_strike_blocked",
                "foul",
                "foul_tip",
                "hit_into_play",
                "hit_into_play_no_out",
                "hit_into_play_score",
            ]
        )
    ]
    whiffs = df[df["description"].isin(["swinging_strike", "swinging_strike_blocked"])]
    swstr_pct = (len(whiffs) / len(df) * 100) if len(df) else 0

    # Expected stats
    xwoba = (
        df["estimated_woba_using_speedangle"].mean()
        if "estimated_woba_using_speedangle" in df
        else 0
    )
    xwoba_con = (
        bbe["estimated_woba_using_speedangle"].mean()
        if n_bbe and "estimated_woba_using_speedangle" in bbe
        else 0
    )

    # ISO — slugging minus avg, but we approximate from events directly
    hits = df[df["events"].isin(["single", "double", "triple", "home_run"])]
    pa = int(df["at_bat_number"].nunique())
    abs_count = len(df[df["events"].notna()])
    if abs_count:
        total_bases = (
            len(hits[hits["events"] == "single"])
            + 2 * len(hits[hits["events"] == "double"])
            + 3 * len(hits[hits["events"] == "triple"])
            + 4 * len(hits[hits["events"] == "home_run"])
        )
        avg = len(hits) / abs_count
        slg = total_bases / abs_count
        iso = slg - avg
    else:
        iso = 0

    # Form
    hrs = df[df["events"] == "home_run"]
    end_dt = pd.to_datetime(end)

    def _hrs_in_window(days):
        cutoff = end_dt - pd.Timedelta(days=days)
        if hrs.empty:
            return 0
        return int((pd.to_datetime(hrs["game_date"]) >= cutoff).sum())

    hr_form_pct, hr_form_arrow = _hr_form_pct_and_arrow(df)

    # bat speed (Statcast post-2024)
    bat_speed = bbe["bat_speed"].mean() if "bat_speed" in bbe.columns and n_bbe else 0
    if pd.isna(bat_speed):
        bat_speed = 0

    pitches = len(df)
    sample_tier = compute_sample_tier(pitches, n_bbe)

    khr = compute_khr(barrel_pct, hard_hit_pct, sweet_spot_pct, swstr_pct,
                      xwoba=float(xwoba) if not pd.isna(xwoba) else None,
                      pulled_barrel_rate=pulled_barrel_pct)

    # Option-B zone_fit inputs
    woba_zones = _woba_zones(df)
    z_swing_rate, o_swing_rate = _swing_rates(df)

    # Form v2 — recent vs baseline comparison (hybrid framing)
    recent_bbe = _recent_window(df, n_bbe_target=25)
    recent_metrics = _bbe_metrics(recent_bbe)

    # Try season baseline; falls back to prior-window within the rolling pull.
    # NOTE: Disabled for now — calling fetch_season_baseline() for every batter
    # doubles the Statcast call volume and triggers rate limits at 400+ players.
    # With the lookback already up to 60 days (~the season so far), the prior
    # window inside that pull is a reasonable baseline. A separate weekly job
    # to populate season aggregates is the proper long-term fix.
    season_baseline = None
    if season_baseline:
        baseline_metrics = season_baseline
        baseline_source = "season"
    else:
        prior_bbe = _prior_window(df, recent_n=25)
        if len(prior_bbe) >= 15:
            baseline_metrics = _bbe_metrics(prior_bbe)
            baseline_source = "prior"
        else:
            baseline_metrics = {}
            baseline_source = "none"

    if baseline_source == "none" or not recent_metrics:
        # Not enough data — neutral form
        form_v2 = {
            "form_score": 50.0,
            "form_arrow": "flat",
            "form_breakdown": {},
            "baseline_source": "none",
        }
    else:
        from app.services.scoring import compute_form_v2

        form_v2 = compute_form_v2(recent_metrics, baseline_metrics, baseline_source)

    # --- Multi-year leaderboard enrichment (Task 1 + 2) ---
    lb_row = None
    if leaderboard and "batters" in leaderboard:
        bt = leaderboard["batters"]
        match = bt[bt["player_id"] == mlbam_id]
        if not match.empty:
            lb_row = match.iloc[0]

    if lb_row is not None:
        iso_final      = _py(float(lb_row["iso"])) if pd.notna(lb_row.get("iso")) else _py(round(float(iso), 3))
        xwoba_final    = _py(float(lb_row["xwoba"])) if pd.notna(lb_row.get("xwoba")) else (_py(round(float(xwoba), 3)) if not pd.isna(xwoba) else 0)
        fb_pct_final   = _py(float(lb_row["fb_percent"])) if pd.notna(lb_row.get("fb_percent")) else None
        hh_final       = _py(float(lb_row["hh_percent"])) if pd.notna(lb_row.get("hh_percent")) else _py(round(hard_hit_pct, 1))
        brl_final      = _py(float(lb_row["brl_percent"])) if pd.notna(lb_row.get("brl_percent")) else _py(round(barrel_pct, 1))
        khr_final      = _py(float(lb_row["khr_v2"])) if pd.notna(lb_row.get("khr_v2")) else _py(round(khr, 1))
        pitches_final  = _py(int(lb_row["pa"])) if pd.notna(lb_row.get("pa")) else _py(pitches)
        bip_final      = _py(int(lb_row["bip"])) if pd.notna(lb_row.get("bip")) else _py(n_bbe)
    else:
        iso_final      = _py(round(float(iso), 3))
        xwoba_final    = _py(round(float(xwoba), 3)) if not pd.isna(xwoba) else 0
        fb_pct_final   = None
        hh_final       = _py(round(hard_hit_pct, 1))
        brl_final      = _py(round(barrel_pct, 1))
        khr_final      = _py(round(khr, 1))
        pitches_final  = _py(pitches)
        bip_final      = _py(n_bbe)

    # Sample tier uses leaderboard career counts when available (career-scale = correct)
    sample_tier = compute_sample_tier(pitches_final, bip_final)

    # Kasper-style HR Form (Task 3) — short-window BBE, not multi-year
    kasper_pct, kasper_arrow = _hr_form_kasper(df, league_ref=league_form_ref)

    stats = BatterStats(
        mlbam_id=mlbam_id,
        as_of=end,
        # Use leaderboard career counts when available (Task 1)
        pitches=pitches_final,
        bip=bip_final,
        pa=_py(pa),
        sample_tier=sample_tier,
        # Core rates — prefer leaderboard for kHR inputs (Task 1)
        barrel_rate=brl_final,
        hard_hit_rate=hh_final,
        sweet_spot_rate=_py(round(sweet_spot_pct, 1)),
        avg_exit_velo=_py(round(bbe["launch_speed"].mean(), 1)) if n_bbe else 0,
        max_exit_velo=_py(round(bbe["launch_speed"].max(), 1)) if n_bbe else 0,
        avg_launch_angle=_py(round(bbe["launch_angle"].mean(), 1)) if n_bbe else 0,
        bat_speed=_py(round(float(bat_speed), 1)),
        pulled_barrel_rate=_py(round(pulled_barrel_pct, 1)),
        swstr_rate=_py(round(swstr_pct, 1)),
        # Expected stats — prefer leaderboard (Task 1)
        iso=iso_final,
        xwoba=xwoba_final,
        xwoba_con=_py(round(float(xwoba_con), 3)) if not pd.isna(xwoba_con) else 0,
        # kHR v2 from leaderboard-wide z-score (Task 2)
        hr_total=_py(len(hrs)),
        khr=khr_final,
        # Multi-year leaderboard extras
        fb_percent=fb_pct_final,
        # Zone data (still short-window; needed for zone_fit)
        hot_zones=_py(_hot_zones(df)),
        woba_zones=_py(woba_zones),
        z_swing_rate=_py(z_swing_rate),
        o_swing_rate=_py(o_swing_rate),
        # Form v2 (Task 3 — kept as internal signal)
        form_score=_py(form_v2["form_score"]),
        form_arrow=form_v2["form_arrow"],
        form_breakdown=_py(form_v2["form_breakdown"]),
        baseline_source=form_v2["baseline_source"],
        # Kasper-style HR Form displayed column (Task 3)
        hr_form_kasper_pct=_py(kasper_pct),
        hr_form_kasper_arrow=kasper_arrow,
        # Legacy form fields (kept for backwards compat)
        hr_l7=_py(_hrs_in_window(7)),
        hr_l15=_py(_hrs_in_window(15)),
        hr_l30=_py(_hrs_in_window(30)),
        hr_form_pct=_py(hr_form_pct),
        hr_form_arrow=hr_form_arrow,
    )
    db.merge(stats)
    db.commit()
    return stats


def refresh_pitcher_stats(
    db: Session,
    mlbam_id: int,
    pitcher_leaderboard: Optional[dict] = None,
) -> Optional[PitcherStats]:
    end = date.today()
    start = _lookback_start(end)
    try:
        df = pybaseball.statcast_pitcher(start.isoformat(), end.isoformat(), mlbam_id)
    except Exception as e:
        logger.error(f"statcast_pitcher failed for {mlbam_id}: {e}")
        return None
    if df.empty:
        return None

    bbe = df[df["type"] == "X"]
    pitches = len(df)

    # Pitch mix — explicit str/float coercion so json.dumps never sees numpy types
    mix = df["pitch_type"].value_counts(normalize=True).head(5)
    pitch_mix = {str(k): round(float(v), 3) for k, v in mix.items() if pd.notna(k)}

    # CSW%, SwStr%, Ball%, PutAway%
    called_strikes = (df["description"] == "called_strike").sum()
    whiffs = (
        df["description"].isin(["swinging_strike", "swinging_strike_blocked"]).sum()
    )
    balls = df["description"].isin(["ball", "blocked_ball", "hit_by_pitch"]).sum()
    csw_rate = (called_strikes + whiffs) / pitches * 100 if pitches else 0
    swstr_rate = whiffs / pitches * 100 if pitches else 0
    ball_rate = balls / pitches * 100 if pitches else 0

    # PutAway% — Ks per 2-strike pitch
    two_strike = df[df["strikes"] == 2]
    ks = (df["events"] == "strikeout").sum()
    putaway_rate = (ks / len(two_strike) * 100) if len(two_strike) else 0

    # HR/9
    hrs_allowed = (df["events"] == "home_run").sum()
    est_ip = pitches / 15
    hr_per_9 = (hrs_allowed / max(est_ip, 1)) * 9

    # xwOBA against
    xwoba = (
        df["estimated_woba_using_speedangle"].mean()
        if "estimated_woba_using_speedangle" in df
        else 0
    )
    xwoba = float(xwoba) if not pd.isna(xwoba) else 0

    # Pulled barrels & Brl/BIP
    barrels = (
        (bbe["launch_speed_angle"] == 6).sum() if "launch_speed_angle" in bbe else 0
    )
    brl_bip = (barrels / len(bbe) * 100) if len(bbe) else 0

    # SIERA — best from pybaseball seasonal leaderboard, fall back to estimate
    siera = 4.5  # neutral fallback; ideally fetched from pitching_stats() yearly cache

    # Option-B zone_fit inputs (pitcher side)
    zone_by_type = _pitcher_zone_profile_by_pitch_type(df, top_n=3)
    edge_pct, heart_pct = _edge_and_heart_pct(df)

    pitcher_obj = type(
        "P",
        (),
        {
            "csw_rate": csw_rate,
            "swstr_rate": swstr_rate,
            "putaway_rate": putaway_rate,
            "ball_rate": ball_rate,
            "xwoba_against": xwoba,
            "siera": siera,
        },
    )()
    quality = score_pitcher_quality(pitcher_obj)

    _throws_vals = df["p_throws"].dropna()
    throws = str(_throws_vals.iloc[0]) if len(_throws_vals) else "R"

    # Multi-year pitcher vulnerability from leaderboard (Task 4)
    p_lb_row = None
    if pitcher_leaderboard and "pitchers" in pitcher_leaderboard:
        pt = pitcher_leaderboard["pitchers"]
        match = pt[pt["player_id"] == mlbam_id]
        if not match.empty:
            p_lb_row = match.iloc[0]

    brl_allowed = (
        _py(float(p_lb_row["brl_allowed_rate"]))
        if p_lb_row is not None and pd.notna(p_lb_row.get("brl_allowed_rate"))
        else None)
    hh_allowed = (
        _py(float(p_lb_row["hh_allowed_rate"]))
        if p_lb_row is not None and pd.notna(p_lb_row.get("hh_allowed_rate"))
        else None)

    stats = PitcherStats(
        mlbam_id=mlbam_id,
        as_of=end,
        throws=throws,
        pitches=_py(pitches),
        pitch_score=_py(quality["pitch_score"]),
        strikeout_score=_py(quality["strikeout_score"]),
        hr_per_9=_py(round(hr_per_9, 2)),
        era=0.0,
        fip=0.0,
        siera=_py(siera),
        xwoba_against=_py(round(xwoba, 3)),
        csw_rate=_py(round(csw_rate, 1)),
        swstr_rate=_py(round(swstr_rate, 1)),
        putaway_rate=_py(round(putaway_rate, 1)),
        ball_rate=_py(round(ball_rate, 1)),
        pulled_barrel_rate=0,
        barrel_per_bip=_py(round(brl_bip, 1)),
        pitch_mix=_py(pitch_mix),
        zone_profile=_py(_pitcher_zone_profile(df)),
        zone_profile_by_pitch_type=_py(zone_by_type),
        edge_pct=_py(edge_pct),
        heart_pct=_py(heart_pct),
        # Multi-year vulnerability (Task 4)
        brl_allowed_rate=brl_allowed,
        hh_allowed_rate=hh_allowed,
    )
    db.merge(stats)
    db.commit()
    return stats


# =============================================================================
# DAILY ORCHESTRATION
# =============================================================================


async def run_daily_refresh(db: Session):
    # Build multi-year leaderboards once for the whole cron run (Task 1)
    leaderboard = {}
    try:
        leaderboard = build_season_leaderboards()
    except Exception:
        logger.exception("build_season_leaderboards failed — falling back to short-window only")

    games = await fetch_todays_games()
    logger.info(f"Found {len(games)} games today")

    for g in games:
        db.merge(
            Game(
                game_pk=g["game_pk"],
                game_date=g["game_date"],
                game_time_utc=g["game_time_utc"],
                home_team=g["home_team"],
                away_team=g["away_team"],
                home_pitcher_id=g["home_pitcher_id"],
                away_pitcher_id=g["away_pitcher_id"],
                venue=g["venue_team"],
                status=g["status"],
            )
        )
    db.commit()

    all_batter_ids = set()
    all_pitcher_ids = set()

    # 1. Roster-based batter discovery — every active position player on every
    #    team playing today. Gives us morning availability and bench coverage.
    teams_today = set()
    for g in games:
        teams_today.add(g["home_team"])
        teams_today.add(g["away_team"])

    team_to_hitters = {}
    for team in teams_today:
        hitters = await fetch_team_hitters(team)
        team_to_hitters[team] = hitters
        all_batter_ids.update(hitters)
    logger.info(
        f"Pulled rosters for {len(teams_today)} teams, {len(all_batter_ids)} unique hitters"
    )

    # 2. Lineup recording — try to fetch confirmed/projected lineups.
    #    Bench players get a row too (batting_order=None, confirmed=0) so the
    #    dashboard can show the full picture.
    for g in games:
        lineup = await fetch_lineup(g["game_pk"], projected=g.get("projected_lineup"))
        confirmed_ids = {"home": set(), "away": set()}
        for side, team_code in [("home", g["home_team"]), ("away", g["away_team"])]:
            for entry in lineup[side]:
                db.merge(
                    Lineup(
                        game_pk=g["game_pk"],
                        team=team_code,
                        batter_id=entry["batter_id"],
                        batting_order=entry["batting_order"],
                        confirmed=entry["confirmed"],
                    )
                )
                confirmed_ids[side].add(entry["batter_id"])

            # Add bench rows for rostered hitters not in lineup
            for bid in team_to_hitters.get(team_code, []):
                if bid in confirmed_ids[side]:
                    continue
                db.merge(
                    Lineup(
                        game_pk=g["game_pk"],
                        team=team_code,
                        batter_id=bid,
                        batting_order=None,
                        confirmed=0,
                    )
                )

        if g["home_pitcher_id"]:
            all_pitcher_ids.add(g["home_pitcher_id"])
        if g["away_pitcher_id"]:
            all_pitcher_ids.add(g["away_pitcher_id"])
    db.commit()
    logger.info(
        f"Refreshing {len(all_batter_ids)} batters, {len(all_pitcher_ids)} pitchers"
    )

    # Player meta — fast, but ~150 calls
    for pid in list(all_batter_ids) + list(all_pitcher_ids):
        if not db.query(Player).filter_by(mlbam_id=pid).first():
            meta = await fetch_player_meta(pid)
            if meta:
                db.merge(Player(**meta))
    db.commit()

    # Statcast pulls — slow. Throttle between calls to avoid rate-limit pauses.
    # Baseball Savant tolerates ~1 req/sec sustained. When pybaseball cache is
    # warm a cached call returns in <50ms, so 1.0s sleep ensures we never exceed
    # that rate even on cache-hit bursts. Adds ~7 min for 400 batters vs 0.4s.
    THROTTLE_SECS = 1.0

    n_batter_ok = 0
    n_batter_fail = 0
    total_batters = len(all_batter_ids)
    for i, bid in enumerate(all_batter_ids, 1):
        try:
            result = refresh_batter_stats(db, bid, leaderboard=leaderboard)
            if result is not None:
                n_batter_ok += 1
        except Exception:
            n_batter_fail += 1
            logger.exception(f"refresh_batter_stats crashed for {bid}")
            db.rollback()
        # Progress log every 25 players so we can see it's alive
        if i % 25 == 0:
            logger.info(
                f"Batter progress: {i}/{total_batters} ({n_batter_ok} ok, {n_batter_fail} failed)"
            )
        time.sleep(THROTTLE_SECS)
    logger.info(f"Batter ingest: {n_batter_ok} ok, {n_batter_fail} failed")

    n_pitcher_ok = 0
    n_pitcher_fail = 0
    total_pitchers = len(all_pitcher_ids)
    for i, pid in enumerate(all_pitcher_ids, 1):
        try:
            result = refresh_pitcher_stats(db, pid, pitcher_leaderboard=leaderboard)
            if result is not None:
                n_pitcher_ok += 1
        except Exception:
            n_pitcher_fail += 1
            logger.exception(f"refresh_pitcher_stats crashed for {pid}")
            db.rollback()
        if i % 10 == 0:
            logger.info(
                f"Pitcher progress: {i}/{total_pitchers} ({n_pitcher_ok} ok, {n_pitcher_fail} failed)"
            )
        time.sleep(THROTTLE_SECS)
    logger.info(f"Pitcher ingest: {n_pitcher_ok} ok, {n_pitcher_fail} failed")

    # Weather
    for g in games:
        wx = await fetch_weather(g["venue_team"])
        if wx:
            game_row = db.query(Game).filter_by(game_pk=g["game_pk"]).first()
            if game_row:
                game_row.weather_data = wx
    db.commit()

    # Snapshot today's picks for backtesting
    from app.services.picks_log import snapshot_daily_picks

    snapshot_daily_picks(db)

    logger.info("Daily refresh complete")
