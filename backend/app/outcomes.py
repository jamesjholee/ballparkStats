"""
Outcomes runner — pulls actual game results and joins them to PickSnapshots.

Run this AFTER games are complete. Suggested schedule:
  - Late night ET (e.g. `0 6 * * *` UTC = 02:00 ET) catches the prior day's results
  - Idempotent: safe to re-run; skips snapshots that already have outcomes
"""
import asyncio
import logging
from datetime import date, timedelta

from app.database import SessionLocal, init_db
from app.services.picks_log import record_outcomes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


async def main():
    init_db()
    db = SessionLocal()
    try:
        # Record yesterday's outcomes (games are completed by the time this runs)
        yesterday = date.today() - timedelta(days=1)
        await record_outcomes(db, target_date=yesterday)
        # Also try today in case of early-finish day games
        await record_outcomes(db, target_date=date.today())
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
