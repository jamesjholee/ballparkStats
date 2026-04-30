"""
API routes — Kasper-style payload, grouped by game.

GET /api/slate           -> top-level slate summary + games list + top hitters/pitchers
GET /api/games/{game_pk} -> per-game detail: hitters table (away+home), pitcher table
"""
from datetime import date
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.db import Game, Lineup, BatterStats, PitcherStats, Player
from app.services.scoring import score_batter
from app.constants import STADIUM_COORDS

router = APIRouter()


@router.get("/api/health")
def health():
    return {"ok": True}


def _serialize_batter_row(entry, batter, batter_meta, opp_pitcher, game, weather):
    """Build the full Kasper-style row for one batter."""
    score = score_batter(
        batter=batter,
        pitcher=opp_pitcher,
        team_code=game.venue,
        weather=weather,
    )
    return {
        "id": entry.batter_id,
        "name": batter_meta.full_name if batter_meta else f"#{entry.batter_id}",
        "team": entry.team,
        "pos": batter_meta.position if batter_meta else "?",
        "bats": batter_meta.bats if batter_meta else "?",
        "batting_order": entry.batting_order,
        "sample_tier": batter.sample_tier or "thin",
        # Headline scores
        "matchup": score["matchup"],
        "test_score": score["test_score"],
        "ceiling": score["ceiling"],
        "zone_fit": score["zone_fit"],
        # Form v2
        "form_score": batter.form_score,
        "form_arrow": batter.form_arrow,
        "form_breakdown": batter.form_breakdown,
        "baseline_source": batter.baseline_source,
        # Legacy form (kept for backwards compat)
        "hr_form_pct": batter.hr_form_pct,
        "hr_form_arrow": batter.hr_form_arrow,
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
        # Sample sizes
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


def _serialize_pitcher_row(pitcher, pitcher_meta, game, opponent_team):
    """Pitcher table row."""
    from app.services.scoring import compute_pitcher_tier
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


@router.get("/api/slate")
def get_slate(db: Session = Depends(get_db)):
    """
    Returns:
      - games[] : every game today with metadata and time
      - top_hitters[] : flat top-N batters across the slate (for the slate-summary view)
      - top_pitchers[] : top-N pitchers across the slate
    """
    today = date.today()
    games = db.query(Game).filter(Game.game_date == today).all()
    if not games:
        return {"date": today.isoformat(), "games": [], "top_hitters": [], "top_pitchers": []}

    games_payload = []
    all_hitters = []
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

        # Build hitter rows for this game
        for team_code, opp_pitcher_id in [
            (game.home_team, game.away_pitcher_id),
            (game.away_team, game.home_pitcher_id),
        ]:
            opp_pitcher = (
                db.query(PitcherStats)
                .filter(PitcherStats.mlbam_id == opp_pitcher_id)
                .order_by(PitcherStats.as_of.desc())
                .first()
            ) if opp_pitcher_id else None

            lineup = (
                db.query(Lineup)
                .filter(Lineup.game_pk == game.game_pk, Lineup.team == team_code)
                .order_by(Lineup.batting_order)
                .all()
            )
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
                row = _serialize_batter_row(entry, batter, batter_meta, opp_pitcher, game, game.weather_data)
                row["game_pk"] = game.game_pk
                all_hitters.append(row)

        # Pitchers for this game
        for pid, opp_team in [
            (game.home_pitcher_id, game.away_team),
            (game.away_pitcher_id, game.home_team),
        ]:
            if not pid:
                continue
            pitcher = (
                db.query(PitcherStats)
                .filter(PitcherStats.mlbam_id == pid)
                .order_by(PitcherStats.as_of.desc())
                .first()
            )
            if not pitcher:
                continue
            pitcher_meta = db.query(Player).filter(Player.mlbam_id == pid).first()
            row = _serialize_pitcher_row(pitcher, pitcher_meta, game, opp_team)
            row["game_pk"] = game.game_pk
            all_pitchers.append(row)

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
def get_game(game_pk: int, db: Session = Depends(get_db)):
    """Per-game detail: away hitters table, home hitters table, both pitchers."""
    game = db.query(Game).filter(Game.game_pk == game_pk).first()
    if not game:
        raise HTTPException(404, "game not found")

    _, _, park_name = STADIUM_COORDS.get(game.venue, (0, 0, "Unknown"))

    away_pitcher = (
        db.query(PitcherStats)
        .filter(PitcherStats.mlbam_id == game.away_pitcher_id)
        .order_by(PitcherStats.as_of.desc())
        .first()
    ) if game.away_pitcher_id else None
    home_pitcher = (
        db.query(PitcherStats)
        .filter(PitcherStats.mlbam_id == game.home_pitcher_id)
        .order_by(PitcherStats.as_of.desc())
        .first()
    ) if game.home_pitcher_id else None

    away_pitcher_meta = (
        db.query(Player).filter(Player.mlbam_id == game.away_pitcher_id).first()
        if game.away_pitcher_id else None
    )
    home_pitcher_meta = (
        db.query(Player).filter(Player.mlbam_id == game.home_pitcher_id).first()
        if game.home_pitcher_id else None
    )

    def _hitters_for_team(team_code, opp_pitcher):
        lineup = (
            db.query(Lineup)
            .filter(Lineup.game_pk == game_pk, Lineup.team == team_code)
            .order_by(Lineup.batting_order)
            .all()
        )
        rows = []
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
            row = _serialize_batter_row(entry, batter, batter_meta, opp_pitcher, game, game.weather_data)
            rows.append(row)
        return rows

    return {
        "game_pk": game_pk,
        "game_time_utc": game.game_time_utc,
        "home_team": game.home_team,
        "away_team": game.away_team,
        "venue": game.venue,
        "park_name": park_name,
        "weather": game.weather_data or {},
        "away_hitters": _hitters_for_team(game.away_team, home_pitcher),
        "home_hitters": _hitters_for_team(game.home_team, away_pitcher),
        "away_pitcher": (
            {
                "name": away_pitcher_meta.full_name if away_pitcher_meta else "TBD",
                **{k: getattr(away_pitcher, k) for k in [
                    "throws", "pitch_score", "strikeout_score", "xwoba_against",
                    "csw_rate", "swstr_rate", "putaway_rate", "ball_rate",
                    "siera", "pulled_barrel_rate", "barrel_per_bip", "hr_per_9",
                    "pitch_mix", "zone_profile",
                ]},
            } if away_pitcher else None
        ),
        "home_pitcher": (
            {
                "name": home_pitcher_meta.full_name if home_pitcher_meta else "TBD",
                **{k: getattr(home_pitcher, k) for k in [
                    "throws", "pitch_score", "strikeout_score", "xwoba_against",
                    "csw_rate", "swstr_rate", "putaway_rate", "ball_rate",
                    "siera", "pulled_barrel_rate", "barrel_per_bip", "hr_per_9",
                    "pitch_mix", "zone_profile",
                ]},
            } if home_pitcher else None
        ),
    }


@router.post("/api/refresh")
async def trigger_refresh(db: Session = Depends(get_db)):
    from app.services.ingest import run_daily_refresh
    await run_daily_refresh(db)
    return {"status": "complete"}
