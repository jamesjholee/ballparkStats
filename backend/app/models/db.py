"""
SQLAlchemy models — Postgres schema for cached data.
Extended to match Kasper-style column set.
"""

from datetime import datetime, timezone


def _utcnow():
    return datetime.now(timezone.utc)

from sqlalchemy import (
    JSON,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Player(Base):
    __tablename__ = "players"
    mlbam_id = Column(Integer, primary_key=True)
    full_name = Column(String, nullable=False)
    team = Column(String(4))
    position = Column(String(4))
    bats = Column(String(2))
    throws = Column(String(2))
    updated_at = Column(DateTime, default=_utcnow)


class BatterStats(Base):
    """30-day rolling Statcast metrics + Kasper-style derived stats."""

    __tablename__ = "batter_stats"
    mlbam_id = Column(Integer, primary_key=True)
    as_of = Column(Date, primary_key=True)

    # Sample size — drives the player-name color (High/Medium/Thin/Very Thin)
    pitches = Column(Integer, default=0)
    bip = Column(Integer, default=0)  # batted balls in play
    pa = Column(Integer, default=0)
    sample_tier = Column(String(20))  # "high" | "medium" | "thin" | "very_thin"

    # Core Statcast
    barrel_rate = Column(Float)  # Brl/BIP%
    hard_hit_rate = Column(Float)  # HH% (>=95mph)
    sweet_spot_rate = Column(Float)  # SweetSpot% (8-32deg LA)
    avg_exit_velo = Column(Float)
    max_exit_velo = Column(Float)
    avg_launch_angle = Column(Float)  # LA
    bat_speed = Column(Float)
    pulled_barrel_rate = Column(Float)  # PulledBrl%
    swstr_rate = Column(Float)  # SwStr%

    # Slash-line / expected
    iso = Column(Float)
    xwoba = Column(Float)  # expected wOBA
    xwoba_con = Column(Float)  # xwOBA on contact only

    # HR specific
    hr_total = Column(Integer)
    khr = Column(Float)  # strikeout-adjusted HR rate score (0-100)

    # Hot zones (3x3 % of HRs by zone) — kept for backwards compat
    hot_zones = Column(JSON)

    # Option-B zone_fit upgrade — quality-of-contact and swing-discipline inputs
    woba_zones = Column(JSON)  # 3x3 grid of xwOBA by zone
    z_swing_rate = Column(Float)  # in-zone swing rate
    o_swing_rate = Column(Float)  # chase rate (out-of-zone swing)

    # Multi-year leaderboard stats (FanGraphs, aggregated over WINDOW_YEARS)
    fb_percent = Column(Float)  # FB% from FanGraphs leaderboard

    # Form v2 — multi-component "is this batter hot?" analysis
    # Recent window = last ~25 batted balls in play
    # Baseline = full-season (when ≥50 BBE) else prior 20 days
    form_score = Column(Float)  # 0-100 unified Form v2 score
    form_arrow = Column(String(8))  # "up" | "down" | "flat"
    form_breakdown = Column(JSON)  # per-component scores for explainability
    baseline_source = Column(String(8))  # "season" | "prior" | "none"

    # Kasper-style HR Form (recent power composite vs league percentile + arrow)
    hr_form_kasper_pct = Column(Float)  # 0-100 percentile (displayed "HR Form" column)
    hr_form_kasper_arrow = Column(String(8))  # "up" | "down" | "flat"

    # Form
    hr_l7 = Column(Integer, default=0)
    hr_l15 = Column(Integer, default=0)
    hr_l30 = Column(Integer, default=0)
    hr_form_pct = Column(Float)  # season HR-rate trend %
    hr_form_arrow = Column(String(8))  # "up" | "down" | "flat"


class PitcherStats(Base):
    """Pitcher metrics — Kasper-style."""

    __tablename__ = "pitcher_stats"
    mlbam_id = Column(Integer, primary_key=True)
    as_of = Column(Date, primary_key=True)
    throws = Column(String(2))

    # Sample size
    pitches = Column(Integer, default=0)

    # Headline scores (0-100)
    pitch_score = Column(Float)  # composite quality
    strikeout_score = Column(Float)

    # Rate stats
    hr_per_9 = Column(Float)
    era = Column(Float)
    fip = Column(Float)
    siera = Column(Float)
    xwoba_against = Column(Float)
    csw_rate = Column(Float)  # called-strikes + whiffs / pitches
    swstr_rate = Column(Float)
    putaway_rate = Column(Float)
    ball_rate = Column(Float)
    pulled_barrel_rate = Column(Float)
    barrel_per_bip = Column(Float)

    # Pitch mix (by pitch type) and zone profile
    pitch_mix = Column(JSON)
    zone_profile = Column(JSON)  # combined-pitch 3x3 grid (kept for compat)

    # Option-B zone_fit upgrade
    zone_profile_by_pitch_type = Column(
        JSON
    )  # {"FF": 3x3, "SL": 3x3, "CH": 3x3, ...} top 3
    edge_pct = Column(Float)  # % pitches on edges (corners + just-off-plate)
    heart_pct = Column(Float)  # % pitches in zone 5 (middle-middle)

    # Multi-year leaderboard vulnerability (feeds pitcher_vuln in Kasper matchup)
    brl_allowed_rate = Column(Float)  # multi-year Brl/BIP% allowed
    hh_allowed_rate = Column(Float)   # multi-year HH% allowed


class Game(Base):
    __tablename__ = "games"
    game_pk = Column(Integer, primary_key=True)
    game_date = Column(Date, index=True)
    game_time_utc = Column(String)
    home_team = Column(String(4))
    away_team = Column(String(4))
    home_pitcher_id = Column(Integer)
    away_pitcher_id = Column(Integer)
    venue = Column(String(4))
    status = Column(String)
    weather_data = Column(JSON)


class Lineup(Base):
    __tablename__ = "lineups"
    game_pk = Column(Integer, ForeignKey("games.game_pk"), primary_key=True)
    team = Column(String(4), primary_key=True)
    batter_id = Column(Integer, primary_key=True)
    batting_order = Column(Integer)
    confirmed = Column(Integer, default=0)


class HRPropOdds(Base):
    __tablename__ = "hr_prop_odds"
    batter_id = Column(Integer, primary_key=True)
    game_pk = Column(Integer, primary_key=True)
    book = Column(String, primary_key=True)
    odds_american = Column(Integer)
    implied_prob = Column(Float)
    fetched_at = Column(DateTime, default=_utcnow)


class PickSnapshot(Base):
    """
    Daily snapshot of every player's scores. Captured at ingest time.
    One row per (game_date, player_id, prop_type).

    Used for backtesting: pair these scores against PickOutcome rows
    to measure whether each component (form, kHR, zone_fit, etc.)
    actually predicts outcomes.
    """

    __tablename__ = "pick_snapshots"
    game_date = Column(Date, primary_key=True)
    game_pk = Column(Integer, primary_key=True)
    player_id = Column(Integer, primary_key=True)
    prop_type = Column(String(8), primary_key=True)  # "hr" | "k"

    # Player + game context
    player_name = Column(String)
    team = Column(String(4))
    opponent_team = Column(String(4))
    opponent_pitcher_id = Column(Integer)
    venue = Column(String(4))
    batting_order = Column(Integer)

    # Scores at the moment of snapshot (the prediction)
    matchup_score = Column(Float)  # for batters
    test_score = Column(Float)  # for batters
    ceiling = Column(Float)  # for batters
    zone_fit = Column(Float)  # for batters
    form_score = Column(Float)  # for batters
    khr = Column(Float)  # for batters
    pitch_score = Column(Float)  # for pitchers
    strikeout_score = Column(Float)  # for pitchers
    pitcher_tier = Column(String(8))  # "fade" | "neutral" | "attack"

    # Sample tier so we can stratify backtest by sample quality
    sample_tier = Column(String(20))

    # Optional odds at snapshot time (when odds feed wired up)
    odds_american = Column(Integer, nullable=True)
    implied_prob = Column(Float, nullable=True)

    snapshot_at = Column(DateTime, default=_utcnow)


class PickOutcome(Base):
    """
    Actual game outcome for each (date, player, prop_type) in PickSnapshot.
    Populated by a separate "outcomes" job that runs ~6 hours after first pitch.
    """

    __tablename__ = "pick_outcomes"
    game_date = Column(Date, primary_key=True)
    game_pk = Column(Integer, primary_key=True)
    player_id = Column(Integer, primary_key=True)
    prop_type = Column(String(8), primary_key=True)

    # The outcome
    hit_hr = Column(Integer)  # 0 or 1 (for HR props)
    hr_count = Column(Integer)  # actual HRs that game (usually 0 or 1)
    strikeouts = Column(Integer)  # for pitcher K props
    pa = Column(Integer)  # plate appearances (didn't bat = 0)

    # Context
    game_status = Column(String)  # "completed" | "postponed" | "did_not_play"
    recorded_at = Column(DateTime, default=_utcnow)


Index("ix_lineups_game", Lineup.game_pk)
Index("ix_batter_stats_date", BatterStats.as_of)
Index("ix_pick_snapshots_date", PickSnapshot.game_date)
Index("ix_pick_outcomes_date", PickOutcome.game_date)
