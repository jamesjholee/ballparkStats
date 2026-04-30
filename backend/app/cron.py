"""
Standalone refresh script — invoke from Railway cron, not the web service.

Railway cron config (railway.json / Settings -> Cron):
  schedule: "0 10 * * *"   (06:00 ET = 10:00 UTC)
  command:  "python -m app.cron"

Why not run this in the web process?
  - pybaseball calls take 10+ minutes for a full slate
  - blocks request handlers, exceeds Railway's startup timeout
  - you'd hammer Baseball Savant on every cold start
"""
import asyncio
import logging
from app.database import SessionLocal, init_db
from app.services.ingest import run_daily_refresh

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


async def main():
    init_db()
    db = SessionLocal()
    try:
        await run_daily_refresh(db)
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
