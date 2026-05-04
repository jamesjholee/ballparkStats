"""
Picks-log service — snapshots daily scores for backtesting, records outcomes after games.

Two functions:
  - snapshot_daily_picks() : run after the daily ingest. Records every player's
    scores with the prediction date. Idempotent (safe to re-run).
  - record_outcomes()      : run ~6 hours after first pitch. Joins MLB Stats
    API box scores against pending PickSnapshot rows.

The point: build a paired dataset of (predicted score, actual outcome) for every
day of every component, so we can later answer "does form_score actually predict
HRs above random?"
"""
import logging
from datetime import date, datetime
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from app.models.db import (
    Game, Lineup, BatterStats, PitcherStats, Player,
    PickSnapshot, PickOutcome,
)
from app.services.scoring import score_batter, compute_pitcher_tier

logger = logging.getLogger(__name__)
MLB_API_BASE = "https://statsapi.mlb.com/api/v1"


def snapshot_daily_picks(db: Session, target_date: Optional[date] = None) -> dict:
    """
    Walk today's slate and record a PickSnapshot row for every batter and pitcher.
    Captures the model's prediction at the moment of ingest.
    """
    target_date = target_date or date.today()
    games = db.query(Game).filter(Game.game_date == target_date).all()
    n_batters = 0
    n_pitchers = 0

    for game in games:
        # ---- Batter snapshots ----
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
                .all()
            )
            opp_team = game.away_team if team_code == game.home_team else game.home_team
            for entry in lineup:
                batter = (
                    db.query(BatterStats)
                    .filter(BatterStats.mlbam_id == entry.batter_id)
                    .order_by(BatterStats.as_of.desc())
                    .first()
                )
                if not batter:
                    continue
                meta = db.query(Player).filter(Player.mlbam_id == entry.batter_id).first()
                score = score_batter(batter, opp_pitcher, game.venue, game.weather_data)

                snap = PickSnapshot(
                    game_date=target_date,
                    game_pk=game.game_pk,
                    player_id=entry.batter_id,
                    prop_type="hr",
                    player_name=meta.full_name if meta else f"#{entry.batter_id}",
                    team=team_code,
                    opponent_team=opp_team,
                    opponent_pitcher_id=opp_pitcher_id,
                    venue=game.venue,
                    batting_order=entry.batting_order,
                    matchup_score=score["matchup"],
                    test_score=score["test_score"],
                    ceiling=score["ceiling"],
                    zone_fit=score["zone_fit"] * 100,  # store on 0-100 scale
                    form_score=batter.form_score,
                    khr=batter.khr,
                    pitch_score=None,
                    strikeout_score=None,
                    pitcher_tier=None,
                    sample_tier=batter.sample_tier,
                    snapshot_at=datetime.utcnow(),
                )
                db.merge(snap)
                n_batters += 1

        # ---- Pitcher snapshots (for K props) ----
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
            meta = db.query(Player).filter(Player.mlbam_id == pid).first()
            tier = compute_pitcher_tier(pitcher.pitch_score, pitcher.hr_per_9)
            team = game.home_team if pid == game.home_pitcher_id else game.away_team

            snap = PickSnapshot(
                game_date=target_date,
                game_pk=game.game_pk,
                player_id=pid,
                prop_type="k",
                player_name=meta.full_name if meta else f"#{pid}",
                team=team,
                opponent_team=opp_team,
                opponent_pitcher_id=None,
                venue=game.venue,
                batting_order=None,
                matchup_score=None,
                test_score=None,
                ceiling=None,
                zone_fit=None,
                form_score=None,
                khr=None,
                pitch_score=pitcher.pitch_score,
                strikeout_score=pitcher.strikeout_score,
                pitcher_tier=tier,
                sample_tier=None,
                snapshot_at=datetime.utcnow(),
            )
            db.merge(snap)
            n_pitchers += 1

    db.commit()
    logger.info(f"Snapshotted {n_batters} batter picks and {n_pitchers} pitcher picks for {target_date}")
    return {"batters": n_batters, "pitchers": n_pitchers, "date": target_date.isoformat()}


async def record_outcomes(db: Session, target_date: Optional[date] = None) -> dict:
    """
    Walk PickSnapshot rows that don't yet have a matching PickOutcome and pull
    actual results from the MLB Stats API box score.
    """
    target_date = target_date or date.today()
    snapshots = (
        db.query(PickSnapshot)
        .filter(PickSnapshot.game_date == target_date)
        .all()
    )
    if not snapshots:
        return {"recorded": 0, "skipped": 0, "date": target_date.isoformat()}

    # Cache one box-score fetch per game_pk
    boxscores = {}
    async with httpx.AsyncClient(timeout=20) as client:
        for game_pk in {s.game_pk for s in snapshots}:
            try:
                r = await client.get(f"{MLB_API_BASE}/game/{game_pk}/boxscore")
                if r.status_code == 200:
                    boxscores[game_pk] = r.json()
            except Exception as e:
                logger.warning(f"box fetch failed for {game_pk}: {e}")

    recorded = 0
    skipped = 0
    for snap in snapshots:
        existing = (
            db.query(PickOutcome)
            .filter(
                PickOutcome.game_date == snap.game_date,
                PickOutcome.game_pk == snap.game_pk,
                PickOutcome.player_id == snap.player_id,
                PickOutcome.prop_type == snap.prop_type,
            )
            .first()
        )
        if existing:
            skipped += 1
            continue

        box = boxscores.get(snap.game_pk)
        if not box:
            continue  # game not yet completed or fetch failed

        # Find the player's stats in the box
        outcome = _extract_player_outcome(box, snap.player_id, snap.prop_type)
        if outcome is None:
            continue

        row = PickOutcome(
            game_date=snap.game_date,
            game_pk=snap.game_pk,
            player_id=snap.player_id,
            prop_type=snap.prop_type,
            **outcome,
            recorded_at=datetime.utcnow(),
        )
        db.merge(row)
        recorded += 1

    db.commit()
    logger.info(f"Recorded {recorded} outcomes ({skipped} skipped) for {target_date}")
    return {"recorded": recorded, "skipped": skipped, "date": target_date.isoformat()}


def _extract_player_outcome(box: dict, player_id: int, prop_type: str) -> Optional[dict]:
    """Find a player in a box score and return the outcome dict for the given prop type."""
    teams = box.get("teams", {})
    for side in ("home", "away"):
        players = teams.get(side, {}).get("players", {})
        key = f"ID{player_id}"
        if key not in players:
            continue
        p = players[key]
        stats = p.get("stats", {})

        if prop_type == "hr":
            batting = stats.get("batting", {})
            try:
                hr_count = int(batting.get("homeRuns", 0))
                pa = int(batting.get("plateAppearances", 0))
            except (TypeError, ValueError):
                hr_count, pa = 0, 0
            return {
                "hit_hr": 1 if hr_count >= 1 else 0,
                "hr_count": hr_count,
                "strikeouts": None,
                "pa": pa,
                "game_status": "completed" if pa > 0 else "did_not_play",
            }
        if prop_type == "k":
            pitching = stats.get("pitching", {})
            try:
                ks = int(pitching.get("strikeOuts", 0))
            except (TypeError, ValueError):
                ks = 0
            ip_str = pitching.get("inningsPitched", "0.0")
            try:
                ip = float(ip_str)
            except (TypeError, ValueError):
                ip = 0.0
            return {
                "hit_hr": None,
                "hr_count": None,
                "strikeouts": ks,
                "pa": None,
                "game_status": "completed" if ip > 0 else "did_not_play",
            }
    return None
