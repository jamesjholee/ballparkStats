"""
HR scoring engine — Kasper-style outputs.

Three batter scores per matchup:
  - Matchup    : weighted composite of HR-relevant inputs (our headline)
  - Test Score : same recipe with a hold-out adjustment (sample-size penalty)
  - Ceiling    : 90th-percentile / best-case version (de-emphasizes form, leans on raw power)

Plus the supporting metrics: kHR, zone_fit, etc.
"""
from typing import Optional
from app.constants import PARK_HR_FACTORS

WEIGHTS = {
    "barrel": 22,
    "zone_fit": 20,
    "form": 15,
    "park": 12,
    "weather": 8,
    "exit_velo": 8,
    "pitcher_hr": 10,
    "bat_speed": 5,
}


def compute_zone_fit(batter, pitcher) -> float:
    """
    Three-component zone-fit score (0-100).

    1. Quality-of-contact overlap (40%) — batter's xwOBA-by-zone × pitcher's
       location density. If the batter crushes pitches in zones the pitcher
       lives in, score is high. (When pitch-type-specific zones are available,
       average across the pitcher's top pitches weighted by usage.)

    2. Swing-discipline edge (30%) — does the batter swing at zones the pitcher
       attacks (good fit), and avoid chasing what's off the plate (good plate
       discipline = harder for pitcher to manage).

    3. Heart vs edge (30%) — pitchers who paint edges are hard to barrel;
       pitchers living in the heart of the plate are HR fodder.

    Falls back gracefully when any input is missing.
    """
    # --- Component 1: Quality-of-contact overlap ---
    woba_zones = getattr(batter, "woba_zones", None) or [[0.320] * 3 for _ in range(3)]
    zone_by_type = getattr(pitcher, "zone_profile_by_pitch_type", None) if pitcher else None
    pitch_mix = (pitcher.pitch_mix if pitcher and pitcher.pitch_mix else {}) or {}
    fallback_zones = (pitcher.zone_profile if pitcher else None) or [[11.1] * 3 for _ in range(3)]

    # Build effective pitcher zone density: weighted average across top pitch types
    if zone_by_type and pitch_mix:
        eff_zones = [[0.0] * 3 for _ in range(3)]
        total_w = 0.0
        for pt, grid in zone_by_type.items():
            w = pitch_mix.get(pt, 0)
            if w <= 0 or not grid:
                continue
            for i in range(3):
                for j in range(3):
                    eff_zones[i][j] += grid[i][j] * w
            total_w += w
        if total_w > 0:
            eff_zones = [[v / total_w for v in row] for row in eff_zones]
        else:
            eff_zones = fallback_zones
    else:
        eff_zones = fallback_zones

    # Dot product: sum (xwOBA-in-zone × pitcher-density-in-zone)
    # eff_zones cells are in pct-of-pitches (sum to ~100 over 9 cells if all in-zone,
    # or less if pitcher works heavily off-plate). Weighted-avg xwOBA = c1_raw / sum(density)/100.
    weighted_woba = 0.0
    weight_sum = 0.0
    for i in range(3):
        for j in range(3):
            woba = woba_zones[i][j] or 0.320
            density = eff_zones[i][j] / 100
            weighted_woba += woba * density
            weight_sum += density
    if weight_sum > 0:
        c1_raw = weighted_woba / weight_sum  # batter's xwOBA weighted by where pitcher throws
    else:
        c1_raw = 0.320
    # Map .200 → 0, .320 → 50, .440 → 100 (each .024 of xwOBA = 10 points)
    c1 = max(0.0, min(100.0, (c1_raw - 0.200) * 417))

    # --- Component 2: Swing-discipline edge ---
    z_swing = getattr(batter, "z_swing_rate", None)
    o_swing = getattr(batter, "o_swing_rate", None)
    if z_swing is None or o_swing is None:
        c2 = 50.0
    else:
        # Reward in-zone aggression (good batters swing at hittable pitches)
        # Penalize chase rate (chasers get exploited)
        # League avg z-swing ~67%, o-swing ~28%
        z_score = max(0, min(100, (z_swing - 50) * 2.5))
        o_score = max(0, min(100, (40 - o_swing) * 3))   # lower chase = higher score
        c2 = 0.6 * z_score + 0.4 * o_score

    # --- Component 3: Heart vs edge ---
    if pitcher is None:
        c3 = 50.0
    else:
        heart = pitcher.heart_pct if pitcher.heart_pct is not None else 12.0
        edge = pitcher.edge_pct if pitcher.edge_pct is not None else 38.0
        # League: heart ~12%, edge ~38%. Map heart 4→0, 12→50, 20→100. Edge 25→100, 50→0.
        heart_score = max(0, min(100, (heart - 4) * 6.25))
        edge_score = max(0, min(100, 100 - (edge - 25) * 4))
        # Heart density is the stronger HR signal — weight it more
        c3 = 0.65 * heart_score + 0.35 * edge_score

    # Final blend
    fit = 0.4 * c1 + 0.3 * c2 + 0.3 * c3
    return round(max(0.0, min(100.0, fit)), 2)


def compute_form_score(hr_l7, hr_l15, hr_l30) -> float:
    """
    LEGACY recency-weighted form score 0-100. Kept for backwards compat
    with any caller still using it. Form v2 supersedes this.
    """
    w7 = ((hr_l7 or 0) / 7) * 100 * 4
    w15 = ((hr_l15 or 0) / 15) * 100 * 2
    w30 = ((hr_l30 or 0) / 30) * 100
    raw = (w7 + w15 + w30) / 7
    return max(0, min(100, raw * 18))


def _z_to_score(z: float) -> float:
    """
    Convert a z-score (recent vs baseline, in std-devs) to a 0-100 form scale.
    z = 0 → 50 (matches baseline)
    z = +1.5 → ~85 (decisively hot)
    z = -1.5 → ~15 (decisively cold)
    Clipped at 0 and 100.
    """
    return max(0.0, min(100.0, 50.0 + z * 23.0))


def _trend_component(recent_val, baseline_val, typical_std) -> float:
    """One metric's contribution. Returns 0-100 where 50 = matches baseline."""
    if recent_val is None or baseline_val is None or typical_std <= 0:
        return 50.0
    z = (recent_val - baseline_val) / typical_std
    return _z_to_score(z)


def compute_form_v2(recent: dict, baseline: dict, baseline_source: str) -> dict:
    """
    Form v2 — multi-component "is this batter hot?" score.

    Compares recent (~25 BBE) performance against baseline (full season or
    prior-20 fallback). Returns 0-100 score, up/down/flat arrow, and a
    component breakdown for explainability.

    Inputs are dicts with keys: barrel_rate, xwoba_con, hard_hit_rate,
    bat_speed, hr_per_bbe. Any missing key falls back to neutral.

    `typical_std` values are league-wide standard deviations of these stats
    over a 25-BBE window — i.e., "how much does this stat normally fluctuate
    just from random sample variation?" Hardcoded from Statcast research.
    """
    # League-wide standard deviations over a ~25-BBE window
    STD = {
        "barrel_rate":     5.0,    # percentage points
        "xwoba_con":       0.060,  # xwOBA points
        "hard_hit_rate":   8.0,    # percentage points
        "bat_speed":       1.2,    # mph
        "hr_per_bbe":      0.04,   # rate (4 percentage points)
    }

    components = {
        "barrel_trend":    _trend_component(recent.get("barrel_rate"), baseline.get("barrel_rate"), STD["barrel_rate"]),
        "xwoba_con_trend": _trend_component(recent.get("xwoba_con"),   baseline.get("xwoba_con"),   STD["xwoba_con"]),
        "hard_hit_trend":  _trend_component(recent.get("hard_hit_rate"), baseline.get("hard_hit_rate"), STD["hard_hit_rate"]),
        "bat_speed_trend": _trend_component(recent.get("bat_speed"),   baseline.get("bat_speed"),   STD["bat_speed"]),
        "hr_recency":      _trend_component(recent.get("hr_per_bbe"),  baseline.get("hr_per_bbe"),  STD["hr_per_bbe"]),
    }

    weights = {
        "barrel_trend":    0.35,
        "xwoba_con_trend": 0.25,
        "hard_hit_trend":  0.15,
        "bat_speed_trend": 0.10,
        "hr_recency":      0.15,
    }

    score = sum(components[k] * weights[k] for k in weights)

    if score >= 60:
        arrow = "up"
    elif score <= 40:
        arrow = "down"
    else:
        arrow = "flat"

    return {
        "form_score": round(score, 1),
        "form_arrow": arrow,
        "form_breakdown": {k: round(v, 1) for k, v in components.items()},
        "baseline_source": baseline_source,
    }


def compute_weather_score(wx) -> float:
    if not wx:
        return 50.0
    s = 50.0
    wd = wx.get("wind_dir", "")
    wind = wx.get("wind", 0)
    temp = wx.get("temp", 70)
    if "out" in wd:
        s += wind * 1.5
    if "in" in wd:
        s -= wind * 1.8
    if temp >= 80:
        s += 8
    if temp >= 90:
        s += 5
    if temp < 60:
        s -= 8
    if wx.get("condition") == "rain":
        s -= 10
    return max(0, min(100, s))


def compute_khr(barrel_rate, swstr_rate, hard_hit_rate, sweet_spot_rate) -> float:
    """
    kHR — strikeout-adjusted HR-likelihood score.
    Combines power inputs with whiff penalty: a high-K hitter sees their HR
    upside discounted because more PAs end without contact.
    """
    barrel = barrel_rate or 0
    sweet = sweet_spot_rate or 0
    hh = hard_hit_rate or 0
    whiff_penalty = (swstr_rate or 0) * 0.6
    raw = (barrel * 2.5) + (sweet * 0.5) + (hh * 0.4) - whiff_penalty
    return max(0, min(100, raw))


def compute_ceiling(barrel_rate, max_exit_velo, pulled_barrel_rate, park_factor) -> float:
    """
    Ceiling — 90th-percentile outcome.
    Leans on raw power (max EV, barrel rate, pulled-air contact) and park.
    """
    barrel = (barrel_rate or 0) * 3
    pull = (pulled_barrel_rate or 0) * 2.5
    max_ev_score = max(0, min(100, ((max_exit_velo or 100) - 100) * 5))
    park = max(0, min(100, ((park_factor or 100) - 80) * 2))
    raw = (barrel + pull + max_ev_score + park) / 3.5
    return max(0, min(100, raw))


def compute_sample_tier(pitches, bip) -> str:
    """Kasper's High/Medium/Thin/Very Thin tiers — drives player-name color."""
    p = pitches or 0
    b = bip or 0
    if p >= 1500 and b >= 200:
        return "high"
    if p >= 600 and b >= 80:
        return "medium"
    if p >= 200 and b >= 25:
        return "thin"
    return "very_thin"


def score_batter(batter, pitcher, team_code: str, weather: Optional[dict]) -> dict:
    """Compute Matchup / Test Score / Ceiling + breakdown."""
    barrel = batter.barrel_rate or 0
    exit_velo = batter.avg_exit_velo or 0
    bat_speed = batter.bat_speed or 0
    pitcher_hr9 = pitcher.hr_per_9 if pitcher else 1.2

    park_factor = PARK_HR_FACTORS.get(team_code, 100)
    zone_fit = compute_zone_fit(batter, pitcher)
    # Use Form v2 score from BatterStats (computed during ingest); fallback for safety
    form_score = batter.form_score if batter.form_score is not None else compute_form_score(batter.hr_l7, batter.hr_l15, batter.hr_l30)
    weather_score = compute_weather_score(weather)

    norm = {
        "barrel": min(100, barrel * 4),
        "zone_fit": zone_fit,
        "form": form_score,
        "park": min(100, max(0, (park_factor - 80) * 2)),
        "weather": weather_score,
        "exit_velo": min(100, max(0, (exit_velo - 85) * 7)),
        "pitcher_hr": min(100, pitcher_hr9 * 50),
        "bat_speed": min(100, max(0, (bat_speed - 65) * 5)) if bat_speed else 50,
    }

    total_w = sum(WEIGHTS.values())
    matchup = sum(norm[k] * w for k, w in WEIGHTS.items()) / total_w

    # Test Score — apply sample-size haircut for thin samples
    tier = batter.sample_tier or "thin"
    haircut = {"high": 1.0, "medium": 0.985, "thin": 0.96, "very_thin": 0.92}[tier]
    test_score = matchup * haircut

    ceiling = compute_ceiling(
        barrel,
        batter.max_exit_velo,
        batter.pulled_barrel_rate,
        park_factor,
    )

    grade = "A" if matchup >= 70 else "B" if matchup >= 60 else "C" if matchup >= 50 else "D"

    return {
        "matchup": round(matchup, 3),
        "test_score": round(test_score, 3),
        "ceiling": round(ceiling, 3),
        "grade": grade,
        "breakdown": {k: round(v, 1) for k, v in norm.items()},
        "zone_fit": round(zone_fit / 100, 3),  # Kasper shows as 0-1 decimal
        "park_factor": park_factor,
    }


def compute_pitcher_tier(pitch_score: float, hr_per_9: float) -> str:
    """
    ATTACK / NEUTRAL / FADE classification for the pitcher leaderboard.

    FADE   = avoid HR plays against this pitcher's lineup (he's good)
    ATTACK = target his lineup for HR plays (he's giving up bombs)
    NEUTRAL = mixed signal

    Combines pitch quality + HR vulnerability — a high pitch_score pitcher
    who's also somehow giving up HRs (Skenes-style bad luck) shouldn't auto-FADE.
    """
    ps = pitch_score or 0
    hr9 = hr_per_9 if hr_per_9 is not None else 1.2

    if ps >= 65 and hr9 <= 1.2:
        return "fade"
    if ps <= 45 or hr9 >= 1.6:
        return "attack"
    return "neutral"


def score_pitcher_quality(p) -> dict:
    """Compute Pitch Score + Strikeout Score for the pitcher table."""
    csw = p.csw_rate or 0
    swstr = p.swstr_rate or 0
    putaway = p.putaway_rate or 0
    xwoba = p.xwoba_against or 0.320
    siera = p.siera or 4.5
    ball = p.ball_rate or 35

    # Pitch Score — overall command/quality. Lower xwOBA and SIERA = better.
    pitch_score = (
        (csw * 1.0)
        + ((100 - ball) * 0.4)
        + (max(0, (0.350 - xwoba)) * 200)
        + (max(0, (5.5 - siera)) * 8)
    )
    pitch_score = max(0, min(100, pitch_score))

    # Strikeout Score — leans on whiff and putaway
    strikeout_score = (swstr * 3.5) + (putaway * 1.2) + (csw * 0.3)
    strikeout_score = max(0, min(100, strikeout_score))

    return {
        "pitch_score": round(pitch_score, 1),
        "strikeout_score": round(strikeout_score, 1),
    }
