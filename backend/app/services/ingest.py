"""
Data ingestion — pulls from pybaseball, MLB Stats API, OpenWeatherMap.
Run via cron (recommended: 06:00 ET daily, then again at 14:00 ET for lineups).

Computes the full Kasper-style stat set:
  Batter: Matchup, Test Score, Ceiling, Zone Fit, HR Form, kHR, ISO, xwOBA,
          xwOBAcon, SwStr%, PulledBrl%, SweetSpot%, HH%, LA, Brl/BIP%
  Pitcher: Pitch Score, Strikeout Score, xwOBA, CSW%, SwStr%, PutAway%, Ball%,
           SIERA, PulledBrl%, Brl/BIP%
"""
import os
import logging
from datetime import date, datetime, timedelta
from typing import Optional

import httpx
import pandas as pd
import pybaseball
from sqlalchemy.orm import Session

from app.constants import STADIUM_COORDS, PARK_CF_BEARING
from app.models.db import Player, BatterStats, PitcherStats, Game, Lineup
from app.services.scoring import (
    compute_khr,
    compute_sample_tier,
    score_pitcher_quality,
)

logger = logging.getLogger(__name__)

# Without this, you'll get rate-limited by Baseball Savant within minutes.
pybaseball.cache.enable()

MLB_API_BASE = "https://statsapi.mlb.com/api/v1"
OWM_BASE = "https://api.openweathermap.org/data/2.5/weather"


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
            games.append({
                "game_pk": g["gamePk"],
                "game_date": target_date,
                "game_time_utc": g.get("gameDate"),
                "home_team": g["teams"]["home"]["team"]["abbreviation"],
                "away_team": g["teams"]["away"]["team"]["abbreviation"],
                "home_pitcher_id": g["teams"]["home"].get("probablePitcher", {}).get("id"),
                "away_pitcher_id": g["teams"]["away"].get("probablePitcher", {}).get("id"),
                "venue_team": g["teams"]["home"]["team"]["abbreviation"],
                "status": g["status"]["abstractGameState"],
            })
    return games


async def fetch_lineup(game_pk: int) -> dict:
    url = f"{MLB_API_BASE}/game/{game_pk}/boxscore"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url)
        if r.status_code != 200:
            return {"home": [], "away": []}
        data = r.json()

    result = {"home": [], "away": []}
    for side in ("home", "away"):
        team_data = data.get("teams", {}).get(side, {})
        batting_order = team_data.get("battingOrder", [])
        for i, pid in enumerate(batting_order[:9]):
            result[side].append({
                "batter_id": int(pid),
                "batting_order": i + 1,
                "confirmed": 1 if batting_order else 0,
            })
    return result


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
        "team": p.get("currentTeam", {}).get("abbreviation"),
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
        "swinging_strike", "swinging_strike_blocked", "foul", "foul_tip",
        "hit_into_play", "hit_into_play_no_out", "hit_into_play_score",
    }
    df_z = df[df["zone"].between(1, 9)]
    df_o = df[df["zone"].between(11, 14)]

    z_swing = (df_z["description"].isin(swing_descs).sum() / len(df_z) * 100) if len(df_z) else 67.0
    o_swing = (df_o["description"].isin(swing_descs).sum() / len(df_o) * 100) if len(df_o) else 28.0
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
    Form v2 (`_compute_form_v2_inputs`) is what actually drives ranking.

    HR Form % — share of recent batted balls that have HR-shaped contact
    (barrels). Arrow compares last-15-day rate to the prior 15.
    """
    bbe = df[df["type"] == "X"]
    if bbe.empty:
        return 0.0, "flat"


def _bbe_metrics(bbe: pd.DataFrame, hrs_count: int = None) -> dict:
    """Compute the five Form v2 metric values from a batted-balls dataframe."""
    if bbe is None or bbe.empty:
        return {}
    n = len(bbe)
    barrel_rate = (bbe["launch_speed_angle"].eq(6).sum() / n * 100) if "launch_speed_angle" in bbe.columns else None
    hard_hit = ((bbe["launch_speed"] >= 95).sum() / n * 100) if "launch_speed" in bbe.columns else None
    xwoba_con = float(bbe["estimated_woba_using_speedangle"].mean()) if "estimated_woba_using_speedangle" in bbe.columns else None
    if xwoba_con is not None and pd.isna(xwoba_con):
        xwoba_con = None
    bat_speed = None
    if "bat_speed" in bbe.columns:
        v = bbe["bat_speed"].mean()
        bat_speed = float(v) if not pd.isna(v) else None
    if hrs_count is None:
        hrs_count = int((bbe["events"] == "home_run").sum()) if "events" in bbe.columns else 0
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
        df = pybaseball.statcast_batter(season_start.isoformat(), today.isoformat(), mlbam_id)
    except Exception as e:
        logger.warning(f"season baseline fetch failed for {mlbam_id}: {e}")
        return None
    if df.empty:
        return None
    bbe = df[df["type"] == "X"]
    if len(bbe) < 50:
        return None
    return _bbe_metrics(bbe)
    # Recent vs prior split
    end = pd.to_datetime(df["game_date"]).max()
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
    return round(overall, 1), arrow


def refresh_batter_stats(db: Session, mlbam_id: int) -> Optional[BatterStats]:
    """Pull last 30 days for a batter, compute the full Kasper stat set."""
    end = date.today()
    start = end - timedelta(days=30)
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
        ((bbe["launch_angle"] >= 8) & (bbe["launch_angle"] <= 32)).sum() / n_bbe * 100
    ) if n_bbe else 0

    # Pulled barrels — pulled = hc_x position relative to handedness
    # Simpler heuristic: barrels with hit_distance > 350ft to pull side
    pulled_barrels = 0
    if n_bbe and "launch_speed_angle" in bbe.columns:
        barrels_df = bbe[bbe["launch_speed_angle"] == 6]
        if not barrels_df.empty and "hc_x" in barrels_df.columns:
            stand = (df["stand"].iloc[0] if not df["stand"].empty else "R")
            if stand == "R":
                pulled = barrels_df[barrels_df["hc_x"] < 125]
            else:
                pulled = barrels_df[barrels_df["hc_x"] > 125]
            pulled_barrels = len(pulled)
    pulled_barrel_pct = (pulled_barrels / n_bbe * 100) if n_bbe else 0

    # Whiff / SwStr%
    swings = df[df["description"].isin(["swinging_strike", "swinging_strike_blocked",
                                          "foul", "foul_tip", "hit_into_play",
                                          "hit_into_play_no_out", "hit_into_play_score"])]
    whiffs = df[df["description"].isin(["swinging_strike", "swinging_strike_blocked"])]
    swstr_pct = (len(whiffs) / len(df) * 100) if len(df) else 0

    # Expected stats
    xwoba = df["estimated_woba_using_speedangle"].mean() if "estimated_woba_using_speedangle" in df else 0
    xwoba_con = bbe["estimated_woba_using_speedangle"].mean() if n_bbe and "estimated_woba_using_speedangle" in bbe else 0

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

    khr = compute_khr(barrel_pct, swstr_pct, hard_hit_pct, sweet_spot_pct)

    # Option-B zone_fit inputs
    woba_zones = _woba_zones(df)
    z_swing_rate, o_swing_rate = _swing_rates(df)

    # Form v2 — recent vs baseline comparison (hybrid framing)
    recent_bbe = _recent_window(df, n_bbe_target=25)
    recent_metrics = _bbe_metrics(recent_bbe)

    # Try season baseline first; fall back to prior-window within 30-day pull
    season_baseline = fetch_season_baseline(mlbam_id) if len(recent_bbe) >= 8 else None
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
        form_v2 = {"form_score": 50.0, "form_arrow": "flat",
                   "form_breakdown": {}, "baseline_source": "none"}
    else:
        from app.services.scoring import compute_form_v2
        form_v2 = compute_form_v2(recent_metrics, baseline_metrics, baseline_source)

    stats = BatterStats(
        mlbam_id=mlbam_id,
        as_of=end,
        pitches=pitches,
        bip=n_bbe,
        pa=pa,
        sample_tier=sample_tier,
        barrel_rate=round(barrel_pct, 1),
        hard_hit_rate=round(hard_hit_pct, 1),
        sweet_spot_rate=round(sweet_spot_pct, 1),
        avg_exit_velo=round(bbe["launch_speed"].mean(), 1) if n_bbe else 0,
        max_exit_velo=round(bbe["launch_speed"].max(), 1) if n_bbe else 0,
        avg_launch_angle=round(bbe["launch_angle"].mean(), 1) if n_bbe else 0,
        bat_speed=round(float(bat_speed), 1),
        pulled_barrel_rate=round(pulled_barrel_pct, 1),
        swstr_rate=round(swstr_pct, 1),
        iso=round(float(iso), 3),
        xwoba=round(float(xwoba), 3) if not pd.isna(xwoba) else 0,
        xwoba_con=round(float(xwoba_con), 3) if not pd.isna(xwoba_con) else 0,
        hr_total=len(hrs),
        khr=round(khr, 1),
        hot_zones=_hot_zones(df),
        woba_zones=woba_zones,
        z_swing_rate=z_swing_rate,
        o_swing_rate=o_swing_rate,
        # Form v2
        form_score=form_v2["form_score"],
        form_arrow=form_v2["form_arrow"],
        form_breakdown=form_v2["form_breakdown"],
        baseline_source=form_v2["baseline_source"],
        # Legacy form fields (still populated for backwards compat)
        hr_l7=_hrs_in_window(7),
        hr_l15=_hrs_in_window(15),
        hr_l30=_hrs_in_window(30),
        hr_form_pct=hr_form_pct,
        hr_form_arrow=hr_form_arrow,
    )
    db.merge(stats)
    db.commit()
    return stats


def refresh_pitcher_stats(db: Session, mlbam_id: int) -> Optional[PitcherStats]:
    end = date.today()
    start = end - timedelta(days=30)
    try:
        df = pybaseball.statcast_pitcher(start.isoformat(), end.isoformat(), mlbam_id)
    except Exception as e:
        logger.error(f"statcast_pitcher failed for {mlbam_id}: {e}")
        return None
    if df.empty:
        return None

    bbe = df[df["type"] == "X"]
    pitches = len(df)

    # Pitch mix
    mix = df["pitch_type"].value_counts(normalize=True).head(5)
    pitch_mix = {k: round(v, 3) for k, v in mix.items() if pd.notna(k)}

    # CSW%, SwStr%, Ball%, PutAway%
    called_strikes = (df["description"] == "called_strike").sum()
    whiffs = df["description"].isin(["swinging_strike", "swinging_strike_blocked"]).sum()
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
    xwoba = df["estimated_woba_using_speedangle"].mean() if "estimated_woba_using_speedangle" in df else 0
    xwoba = float(xwoba) if not pd.isna(xwoba) else 0

    # Pulled barrels & Brl/BIP
    barrels = (bbe["launch_speed_angle"] == 6).sum() if "launch_speed_angle" in bbe else 0
    brl_bip = (barrels / len(bbe) * 100) if len(bbe) else 0

    # SIERA — best from pybaseball seasonal leaderboard, fall back to estimate
    siera = 4.5  # neutral fallback; ideally fetched from pitching_stats() yearly cache

    # Option-B zone_fit inputs (pitcher side)
    zone_by_type = _pitcher_zone_profile_by_pitch_type(df, top_n=3)
    edge_pct, heart_pct = _edge_and_heart_pct(df)

    pitcher_obj = type("P", (), {
        "csw_rate": csw_rate,
        "swstr_rate": swstr_rate,
        "putaway_rate": putaway_rate,
        "ball_rate": ball_rate,
        "xwoba_against": xwoba,
        "siera": siera,
    })()
    quality = score_pitcher_quality(pitcher_obj)

    stats = PitcherStats(
        mlbam_id=mlbam_id,
        as_of=end,
        throws=df["p_throws"].iloc[0] if not df.empty else "R",
        pitches=pitches,
        pitch_score=quality["pitch_score"],
        strikeout_score=quality["strikeout_score"],
        hr_per_9=round(hr_per_9, 2),
        era=0.0,
        fip=0.0,
        siera=siera,
        xwoba_against=round(xwoba, 3),
        csw_rate=round(csw_rate, 1),
        swstr_rate=round(swstr_rate, 1),
        putaway_rate=round(putaway_rate, 1),
        ball_rate=round(ball_rate, 1),
        pulled_barrel_rate=0,
        barrel_per_bip=round(brl_bip, 1),
        pitch_mix=pitch_mix,
        zone_profile=_pitcher_zone_profile(df),
        zone_profile_by_pitch_type=zone_by_type,
        edge_pct=edge_pct,
        heart_pct=heart_pct,
    )
    db.merge(stats)
    db.commit()
    return stats


# =============================================================================
# DAILY ORCHESTRATION
# =============================================================================

async def run_daily_refresh(db: Session):
    games = await fetch_todays_games()
    logger.info(f"Found {len(games)} games today")

    for g in games:
        db.merge(Game(
            game_pk=g["game_pk"],
            game_date=g["game_date"],
            game_time_utc=g["game_time_utc"],
            home_team=g["home_team"],
            away_team=g["away_team"],
            home_pitcher_id=g["home_pitcher_id"],
            away_pitcher_id=g["away_pitcher_id"],
            venue=g["venue_team"],
            status=g["status"],
        ))
    db.commit()

    all_batter_ids = set()
    all_pitcher_ids = set()
    for g in games:
        lineup = await fetch_lineup(g["game_pk"])
        for side, team_code in [("home", g["home_team"]), ("away", g["away_team"])]:
            for entry in lineup[side]:
                db.merge(Lineup(
                    game_pk=g["game_pk"],
                    team=team_code,
                    batter_id=entry["batter_id"],
                    batting_order=entry["batting_order"],
                    confirmed=entry["confirmed"],
                ))
                all_batter_ids.add(entry["batter_id"])
        if g["home_pitcher_id"]:
            all_pitcher_ids.add(g["home_pitcher_id"])
        if g["away_pitcher_id"]:
            all_pitcher_ids.add(g["away_pitcher_id"])
    db.commit()
    logger.info(f"Refreshing {len(all_batter_ids)} batters, {len(all_pitcher_ids)} pitchers")

    # Player meta — fast, but ~150 calls
    for pid in list(all_batter_ids) + list(all_pitcher_ids):
        if not db.query(Player).filter_by(mlbam_id=pid).first():
            meta = await fetch_player_meta(pid)
            if meta:
                db.merge(Player(**meta))
    db.commit()

    # Statcast pulls — slow
    for bid in all_batter_ids:
        refresh_batter_stats(db, bid)
    for pid in all_pitcher_ids:
        refresh_pitcher_stats(db, pid)

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
