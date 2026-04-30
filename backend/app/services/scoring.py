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


def compute_zone_fit(hot_zones, pitcher_profile) -> float:
    """Dot product of batter HR-zone distribution × pitcher location distribution."""
    if not hot_zones or not pitcher_profile:
        return 0.0
    fit = 0.0
    for i in range(3):
        for j in range(3):
            fit += (hot_zones[i][j] / 100) * (pitcher_profile[i][j] / 100)
    return min(100, fit * 1400)


def compute_form_score(hr_l7, hr_l15, hr_l30) -> float:
    """Recency-weighted form score 0-100."""
    w7 = ((hr_l7 or 0) / 7) * 100 * 4
    w15 = ((hr_l15 or 0) / 15) * 100 * 2
    w30 = ((hr_l30 or 0) / 30) * 100
    raw = (w7 + w15 + w30) / 7
    return max(0, min(100, raw * 18))


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
    hot_zones = batter.hot_zones or [[11.1] * 3 for _ in range(3)]
    pitcher_profile = pitcher.zone_profile if pitcher else [[11.1] * 3 for _ in range(3)]
    pitcher_hr9 = pitcher.hr_per_9 if pitcher else 1.2

    park_factor = PARK_HR_FACTORS.get(team_code, 100)
    zone_fit = compute_zone_fit(hot_zones, pitcher_profile)
    form_score = compute_form_score(batter.hr_l7, batter.hr_l15, batter.hr_l30)
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
