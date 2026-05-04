"""
Backfill historical picks and outcomes from Opening Day 2025 to yesterday.

For each game date:
  1. Fetch schedule + box-score lineups from MLB Stats API
  2. Write Game + Lineup rows (idempotent via db.merge)
  3. Write Player meta for any new MLBAM IDs
  4. Run snapshot_daily_picks  (scores current model against historical matchups)
  5. Run record_outcomes        (fetch actual box-score results)

NOTE: Batter/pitcher Statcast stats are NOT re-pulled here. Scores are computed
against whatever stats are currently in the DB. This is intentional — the
backtest shows how the current model would have performed on historical lineups.

Run from the backend/ directory:
    python3 backfill_history.py
    python3 backfill_history.py --start 2025-04-01 --end 2025-04-30
"""
import argparse
import asyncio
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

# Load .env from the backend directory so DATABASE_URL is available
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_path)

os.environ.setdefault("OPENWEATHER_API_KEY", "")

sys.path.insert(0, os.path.dirname(__file__))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

from app.database import SessionLocal, init_db
from app.models.db import Game, Lineup, Player
from app.services.ingest import fetch_todays_games, fetch_lineup, fetch_player_meta
from app.services.picks_log import snapshot_daily_picks, record_outcomes

OPENING_DAY_2025 = date(2025, 3, 27)


async def backfill_date(db, target_date: date) -> dict:
    log.info(f"── {target_date} {'─' * 40}")

    games = await fetch_todays_games(target_date=target_date)
    if not games:
        log.info("  no games scheduled, skipping")
        return {"date": target_date, "games": 0, "snaps": 0, "outcomes": 0}

    # 1. Store game rows
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
    log.info(f"  stored {len(games)} games")

    # 2. Confirmed lineups from box scores — past games have actual batting orders
    all_player_ids = set()
    for g in games:
        lineup = await fetch_lineup(g["game_pk"])
        for side, team_code in [("home", g["home_team"]), ("away", g["away_team"])]:
            for entry in lineup.get(side, []):
                db.merge(Lineup(
                    game_pk=g["game_pk"],
                    team=team_code,
                    batter_id=entry["batter_id"],
                    batting_order=entry["batting_order"],
                    confirmed=entry["confirmed"],
                ))
                all_player_ids.add(entry["batter_id"])
        for pid in (g["home_pitcher_id"], g["away_pitcher_id"]):
            if pid:
                all_player_ids.add(pid)
    db.commit()
    log.info(f"  stored lineups, {len(all_player_ids)} unique players")

    # 3. Player meta for any IDs we've never seen
    new_ids = [
        pid for pid in all_player_ids
        if not db.query(Player).filter_by(mlbam_id=pid).first()
    ]
    if new_ids:
        for pid in new_ids:
            meta = await fetch_player_meta(pid)
            if meta:
                db.merge(Player(**meta))
        db.commit()
        log.info(f"  added {len(new_ids)} new player records")

    # 4. Snapshot picks using current model stats against historical lineups
    snap = snapshot_daily_picks(db, target_date=target_date)
    log.info(f"  snapshots: {snap['batters']} batters, {snap['pitchers']} pitchers")

    # 5. Pull actual outcomes from MLB box scores (available for all past games)
    out = await record_outcomes(db, target_date=target_date)
    log.info(f"  outcomes:  {out['recorded']} recorded, {out['skipped']} already present")

    return {
        "date": target_date,
        "games": len(games),
        "snaps": snap["batters"] + snap["pitchers"],
        "outcomes": out["recorded"],
    }


async def main(start: date, end: date):
    init_db()
    db = SessionLocal()
    totals = {"days": 0, "games": 0, "snaps": 0, "outcomes": 0}
    d = start
    try:
        while d <= end:
            result = await backfill_date(db, d)
            totals["days"] += 1
            for k in ("games", "snaps", "outcomes"):
                totals[k] += result[k]
            d += timedelta(days=1)
            await asyncio.sleep(0.3)  # polite gap between dates
    finally:
        db.close()

    log.info("=" * 60)
    log.info(f"DONE  {start} → {end}  ({totals['days']} days)")
    log.info(f"  {totals['games']} games")
    log.info(f"  {totals['snaps']} snapshots")
    log.info(f"  {totals['outcomes']} outcomes recorded")
    log.info("=" * 60)


if __name__ == "__main__":
    yesterday = date.today() - timedelta(days=1)
    parser = argparse.ArgumentParser(
        description="Backfill picks + outcomes from Opening Day to yesterday"
    )
    parser.add_argument(
        "--start",
        default=OPENING_DAY_2025.isoformat(),
        help=f"First date to backfill (default: {OPENING_DAY_2025})",
    )
    parser.add_argument(
        "--end",
        default=yesterday.isoformat(),
        help=f"Last date to backfill inclusive (default: {yesterday})",
    )
    args = parser.parse_args()
    asyncio.run(main(date.fromisoformat(args.start), date.fromisoformat(args.end)))
