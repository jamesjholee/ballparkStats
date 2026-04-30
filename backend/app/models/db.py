"""
SQLAlchemy models — Postgres schema for cached data.
Extended to match Kasper-style column set.
"""
from sqlalchemy import Column, Integer, String, Float, Date, DateTime, JSON, ForeignKey, Index
from sqlalchemy.orm import declarative_base
from datetime import datetime

Base = declarative_base()


class Player(Base):
    __tablename__ = "players"
    mlbam_id = Column(Integer, primary_key=True)
    full_name = Column(String, nullable=False)
    team = Column(String(4))
    position = Column(String(4))
    bats = Column(String(2))
    throws = Column(String(2))
    updated_at = Column(DateTime, default=datetime.utcnow)


class BatterStats(Base):
    """30-day rolling Statcast metrics + Kasper-style derived stats."""
    __tablename__ = "batter_stats"
    mlbam_id = Column(Integer, primary_key=True)
    as_of = Column(Date, primary_key=True)

    # Sample size — drives the player-name color (High/Medium/Thin/Very Thin)
    pitches = Column(Integer, default=0)
    bip = Column(Integer, default=0)         # batted balls in play
    pa = Column(Integer, default=0)
    sample_tier = Column(String(20))         # "high" | "medium" | "thin" | "very_thin"

    # Core Statcast
    barrel_rate = Column(Float)              # Brl/BIP%
    hard_hit_rate = Column(Float)            # HH% (>=95mph)
    sweet_spot_rate = Column(Float)          # SweetSpot% (8-32deg LA)
    avg_exit_velo = Column(Float)
    max_exit_velo = Column(Float)
    avg_launch_angle = Column(Float)         # LA
    bat_speed = Column(Float)
    pulled_barrel_rate = Column(Float)       # PulledBrl%
    swstr_rate = Column(Float)               # SwStr%

    # Slash-line / expected
    iso = Column(Float)
    xwoba = Column(Float)                    # expected wOBA
    xwoba_con = Column(Float)                # xwOBA on contact only

    # HR specific
    hr_total = Column(Integer)
    khr = Column(Float)                      # strikeout-adjusted HR rate score (0-100)

    # Hot zones (3x3 % of HRs by zone)
    hot_zones = Column(JSON)

    # Form
    hr_l7 = Column(Integer, default=0)
    hr_l15 = Column(Integer, default=0)
    hr_l30 = Column(Integer, default=0)
    hr_form_pct = Column(Float)              # season HR-rate trend %
    hr_form_arrow = Column(String(2))        # "up" | "down" | "flat"


class PitcherStats(Base):
    """Pitcher metrics — Kasper-style."""
    __tablename__ = "pitcher_stats"
    mlbam_id = Column(Integer, primary_key=True)
    as_of = Column(Date, primary_key=True)
    throws = Column(String(2))

    # Sample size
    pitches = Column(Integer, default=0)

    # Headline scores (0-100)
    pitch_score = Column(Float)              # composite quality
    strikeout_score = Column(Float)

    # Rate stats
    hr_per_9 = Column(Float)
    era = Column(Float)
    fip = Column(Float)
    siera = Column(Float)
    xwoba_against = Column(Float)
    csw_rate = Column(Float)                 # called-strikes + whiffs / pitches
    swstr_rate = Column(Float)
    putaway_rate = Column(Float)
    ball_rate = Column(Float)
    pulled_barrel_rate = Column(Float)
    barrel_per_bip = Column(Float)

    # Pitch mix (by pitch type) and zone profile
    pitch_mix = Column(JSON)
    zone_profile = Column(JSON)


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
    fetched_at = Column(DateTime, default=datetime.utcnow)


Index("ix_lineups_game", Lineup.game_pk)
Index("ix_batter_stats_date", BatterStats.as_of)
