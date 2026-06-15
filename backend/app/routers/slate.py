"""
API routes — Kasper-style payload, grouped by game.

GET /api/slate           -> top-level slate summary + games list + top hitters/pitchers
GET /api/games/{game_pk} -> per-game detail: hitters table (away+home), pitcher table

Matchup is computed as 50 + 10 * weighted_z(components) over the slate/game
(Task 4 — Kasper-alignment). Zone-fit still uses ParkBlast's 3-component model.
HR Form displayed is the Kasper-style percentile+arrow; Form v2 kept as form_v2_*.
"""

import os
import secrets
import pandas as pd
from datetime import date

from fastapi import APIRouter, Depends, Header, HTTPException, Path
from sqlalchemy.orm import Session

from app.constants import PARK_HR_FACTORS, STADIUM_COORDS
from app.database import get_db
from app.models.db import BatterStats, Game, Lineup, PitcherStats, Player
from app.services.scoring import (
    compute_ceiling_kasper,
    compute_matchup_kasper,
    compute_pitcher_tier,
    compute_test_score_kasper,
    compute_zone_fit,
    compute_weather_score,
    pitcher_vuln_raw,
)

router = APIRouter()


@router.get("/api/health")
def health():
    return {"ok": True}


# ---------------------------------------------------------------------------
# Component gathering helpers (Task 4)
# ---------------------------------------------------------------------------

def _batter_components(entry, batter, opp_pitcher, game, weather: dict) -> dict:
    """
    Collect raw matchup components for one batter.
    Returns a flat dict ready for batch DataFrame construction.
    """
    zone_fit_score = compute_zone_fit(batter, opp_pitcher)
    weather_score = compute_weather_score(weather)
    park_factor = float(PARK_HR_FACTORS.get(game.venue, 100))
    vuln = pitcher_vuln_raw(opp_pitcher)

    return {
        "_batter_id": entry.batter_id,
        "_game_pk": game.game_pk,
        "_team": entry.team,
        "_zone_fit_raw": zone_fit_score,  # 0-100, stored for serialisation
        # Matchup components — z-scored across slate in compute_matchup_kasper
        "khr":            float(batter.khr or 50.0),
        "zone_fit":       zone_fit_score,
        "pitcher_vuln":   vuln,
        "park_hr_factor": park_factor,
        "environment":    weather_score,
        # Ceiling extras
        "fb_percent":     float(batter.fb_percent or 25.0),
        "avg_hit_speed":  float(batter.avg_exit_velo or 88.0),
        "sample_tier":    batter.sample_tier or "thin",
    }


def _serialize_batter_row(
    entry, batter, batter_meta, opp_pitcher,
    zone_fit_raw: float,
    matchup: float, test_score: float, ceiling: float,
) -> dict:
    """Assemble the full API row for one batter after batch scores are computed."""
    if entry.confirmed:
        lineup_status = "confirmed"
    elif entry.batting_order is not None:
        lineup_status = "projected"
    else:
        lineup_status = "bench"

    return {
        "id": entry.batter_id,
        "name": batter_meta.full_name if batter_meta else f"#{entry.batter_id}",
        "team": entry.team,
        "pos": batter_meta.position if batter_meta else "?",
        "bats": batter_meta.bats if batter_meta else "?",
        "batting_order": entry.batting_order,
        "lineup_status": lineup_status,
        "sample_tier": batter.sample_tier or "thin",
        # Headline scores (Kasper-style, Task 4)
        "matchup": matchup,
        "test_score": test_score,
        "ceiling": ceiling,
        "zone_fit": round(zone_fit_raw / 100, 3),  # Kasper shows as 0-1 decimal
        # Kasper-style HR Form (displayed column, Task 3)
        "hr_form_pct": batter.hr_form_kasper_pct,
        "hr_form_arrow": batter.hr_form_kasper_arrow,
        # Form v2 (internal / alternate signal — kept, not deleted, Task 3)
        "form_v2_score": batter.form_score,
        "form_v2_arrow": batter.form_arrow,
        "form_breakdown": batter.form_breakdown,
        "baseline_source": batter.baseline_source,
        # Legacy form (backwards compat)
        "form_score": batter.form_score,
        "form_arrow": batter.form_arrow,
        # Statcast
        "khr": batter.khr,
        "iso": batter.iso,
        "xwoba": batter.xwoba,
        "xwoba_con": batter.xwoba_con,
        "swstr_rate": batter.swstr_rate,
        "pulled_barrel_rate": batter.pulled_barrel_rate,
        "sweet_spot_rate": batter.sweet_spot_rate,
        "hard_hit_rate": batter.hard_hit_rate,
        "launch_angle": batter.avg_launch_angle,
        "barrel_rate": batter.barrel_rate,
        "fb_percent": batter.fb_percent,
        # Sample sizes (career-scale when leaderboard populated, Task 1)
        "pitches": batter.pitches,
        "bip": batter.bip,
        # Hot zones for the detail view
        "hot_zones": batter.hot_zones,
        # Pitcher matchup info
        "pitcher": {
            "id": opp_pitcher.mlbam_id if opp_pitcher else None,
            "throws": opp_pitcher.throws if opp_pitcher else "?",
        },
    }


def _build_hitter_rows(
    entries_data: list[tuple],   # (entry, batter, batter_meta, opp_pitcher, game, weather)
) -> list[dict]:
    """
    Two-pass: collect components → batch-compute Kasper matchup → serialize.
    Z-scoring is over the full set of rows passed in (slate-level or game-level).
    """
    if not entries_data:
        return []

    raw_components = [
        _batter_components(entry, batter, opp_pitcher, game, weather)
        for entry, batter, batter_meta, opp_pitcher, game, weather in entries_data
    ]

    board_df = pd.DataFrame(raw_components)
    matchup_vals  = compute_matchup_kasper(board_df)
    test_vals     = compute_test_score_kasper(board_df)
    ceiling_vals  = compute_ceiling_kasper(board_df)

    rows = []
    for i, (entry, batter, batter_meta, opp_pitcher, game, weather) in enumerate(entries_data):
        row = _serialize_batter_row(
            entry, batter, batter_meta, opp_pitcher,
            zone_fit_raw=raw_components[i]["_zone_fit_raw"],
            matchup=float(matchup_vals.iloc[i]),
            test_score=float(test_vals.iloc[i]),
            ceiling=float(ceiling_vals.iloc[i]),
        )
        rows.append(row)
    return rows


def _serialize_pitcher_row(pitcher, pitcher_meta, game, opponent_team):
    tier = compute_pitcher_tier(pitcher.pitch_score, pitcher.hr_per_9)
    return {
        "id": pitcher.mlbam_id,
        "name": pitcher_meta.full_name if pitcher_meta else f"#{pitcher.mlbam_id}",
        "throws": pitcher.throws,
        "away_team": opponent_team,
        "home_team": game.home_team,
        "venue": game.venue,
        "tier": tier,
        "pitch_score": pitcher.pitch_score,
        "strikeout_score": pitcher.strikeout_score,
        "xwoba_against": pitcher.xwoba_against,
        "csw_rate": pitcher.csw_rate,
        "swstr_rate": pitcher.swstr_rate,
        "putaway_rate": pitcher.putaway_rate,
        "ball_rate": pitcher.ball_rate,
        "siera": pitcher.siera,
        "pulled_barrel_rate": pitcher.pulled_barrel_rate,
        "barrel_per_bip": pitcher.barrel_per_bip,
        "hr_per_9": pitcher.hr_per_9,
        "pitch_mix": pitcher.pitch_mix,
        "zone_profile": pitcher.zone_profile,
    }


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def _get_pitcher(db, pitcher_id):
    if not pitcher_id:
        return None
    return (
        db.query(PitcherStats)
        .filter(PitcherStats.mlbam_id == pitcher_id)
        .order_by(PitcherStats.as_of.desc())
        .first()
    )


def _collect_entries_for_team(db, game, team_code, opp_pitcher):
    """Return list of (entry, batter, batter_meta, opp_pitcher, game, weather) tuples."""
    lineup = (
        db.query(Lineup)
        .filter(Lineup.game_pk == game.game_pk, Lineup.team == team_code)
        .order_by(Lineup.batting_order.is_(None), Lineup.batting_order)
        .all()
    )
    result = []
    for entry in lineup:
        batter = (
            db.query(BatterStats)
            .filter(BatterStats.mlbam_id == entry.batter_id)
            .order_by(BatterStats.as_of.desc())
            .first()
        )
        if not batter:
            continue
        batter_meta = db.query(Player).filter(Player.mlbam_id == entry.batter_id).first()
        result.append((entry, batter, batter_meta, opp_pitcher, game, game.weather_data or {}))
    return result


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/api/slate")
def get_slate(db: Session = Depends(get_db)):
    """
    Returns:
      - games[] : every game today with metadata and time
      - top_hitters[] : flat list across the full slate, sorted by matchup
      - top_pitchers[] : sorted by pitch_score
    Matchup z-scores across all hitters in the full slate (Task 4).
    """
    today = date.today()
    games = db.query(Game).filter(Game.game_date == today).all()
    if not games:
        return {"date": today.isoformat(), "games": [], "top_hitters": [], "top_pitchers": []}

    games_payload = []
    all_entries_data = []
    all_pitchers = []

    for game in games:
        _, _, park_name = STADIUM_COORDS.get(game.venue, (0, 0, "Unknown"))
        games_payload.append({
            "game_pk": game.game_pk,
            "game_time_utc": game.game_time_utc,
            "home_team": game.home_team,
            "away_team": game.away_team,
            "venue": game.venue,
            "park_name": park_name,
            "status": game.status,
            "weather": game.weather_data or {},
        })

        home_pitcher = _get_pitcher(db, game.home_pitcher_id)
        away_pitcher = _get_pitcher(db, game.away_pitcher_id)

        # Home hitters face away pitcher; away hitters face home pitcher
        all_entries_data.extend(
            _collect_entries_for_team(db, game, game.home_team, away_pitcher))
        all_entries_data.extend(
            _collect_entries_for_team(db, game, game.away_team, home_pitcher))

        # Pitchers
        for pid, opp_team, pitcher_obj in [
            (game.home_pitcher_id, game.away_team, home_pitcher),
            (game.away_pitcher_id, game.home_team, away_pitcher),
        ]:
            if not pid or not pitcher_obj:
                continue
            pitcher_meta = db.query(Player).filter(Player.mlbam_id == pid).first()
            row = _serialize_pitcher_row(pitcher_obj, pitcher_meta, game, opp_team)
            row["game_pk"] = game.game_pk
            all_pitchers.append(row)

    # Two-pass batch matchup over the full slate
    all_hitters = _build_hitter_rows(all_entries_data)

    # Attach game_pk to each hitter row
    for row, (entry, *_) in zip(all_hitters, all_entries_data):
        # game_pk comes from entry → we stored _game_pk in components, match by position
        pass  # entry is the first item in each tuple
    for i, row in enumerate(all_hitters):
        row["game_pk"] = all_entries_data[i][4].game_pk  # game is index 4

    all_hitters.sort(key=lambda x: x["matchup"], reverse=True)
    all_pitchers.sort(key=lambda x: x["pitch_score"] or 0, reverse=True)

    return {
        "date": today.isoformat(),
        "games": games_payload,
        "top_hitters": all_hitters,
        "top_pitchers": all_pitchers,
        "count": len(all_hitters),
    }


@router.get("/api/games/{game_pk}")
def get_game(game_pk: int = Path(..., ge=1), db: Session = Depends(get_db)):
    """Per-game detail. Matchup z-scores within this game's hitters (Task 4)."""
    game = db.query(Game).filter(Game.game_pk == game_pk).first()
    if not game:
        raise HTTPException(404, "game not found")

    _, _, park_name = STADIUM_COORDS.get(game.venue, (0, 0, "Unknown"))

    away_pitcher = _get_pitcher(db, game.away_pitcher_id)
    home_pitcher = _get_pitcher(db, game.home_pitcher_id)
    away_pitcher_meta = (
        db.query(Player).filter(Player.mlbam_id == game.away_pitcher_id).first()
        if game.away_pitcher_id else None)
    home_pitcher_meta = (
        db.query(Player).filter(Player.mlbam_id == game.home_pitcher_id).first()
        if game.home_pitcher_id else None)

    # Collect both teams' entries, then batch-score together
    away_entries = _collect_entries_for_team(db, game, game.away_team, home_pitcher)
    home_entries = _collect_entries_for_team(db, game, game.home_team, away_pitcher)
    all_entries = away_entries + home_entries
    all_rows = _build_hitter_rows(all_entries)

    n_away = len(away_entries)
    away_hitters = all_rows[:n_away]
    home_hitters = all_rows[n_away:]

    def _pitcher_detail(pitcher, pitcher_meta, opp_team):
        if not pitcher:
            return None
        return {
            "name": pitcher_meta.full_name if pitcher_meta else "TBD",
            **{
                k: getattr(pitcher, k)
                for k in [
                    "throws", "pitch_score", "strikeout_score", "xwoba_against",
                    "csw_rate", "swstr_rate", "putaway_rate", "ball_rate",
                    "siera", "pulled_barrel_rate", "barrel_per_bip", "hr_per_9",
                    "pitch_mix", "zone_profile",
                ]
            },
        }

    return {
        "game_pk": game_pk,
        "game_time_utc": game.game_time_utc,
        "home_team": game.home_team,
        "away_team": game.away_team,
        "venue": game.venue,
        "park_name": park_name,
        "weather": game.weather_data or {},
        "away_hitters": away_hitters,
        "home_hitters": home_hitters,
        "away_pitcher": _pitcher_detail(away_pitcher, away_pitcher_meta, game.home_team),
        "home_pitcher": _pitcher_detail(home_pitcher, home_pitcher_meta, game.away_team),
    }


@router.post("/api/refresh")
async def trigger_refresh(
    x_refresh_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    expected = os.getenv("REFRESH_SECRET", "")
    if not expected or not secrets.compare_digest(x_refresh_token or "", expected):
        raise HTTPException(status_code=403, detail="forbidden")
    from app.services.ingest import run_daily_refresh
    await run_daily_refresh(db)
    return {"status": "complete"}
