"""
migrate_kasper.py — Add new columns for Kasper-alignment tasks.

Run once against your Supabase/Postgres database:
    python migrate_kasper.py

Safe to re-run (ADD COLUMN IF NOT EXISTS).
SQLite note: SQLite doesn't support IF NOT EXISTS on ALTER TABLE;
the script catches those errors and continues.
"""
import os
import sys

from sqlalchemy import create_engine, text

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./parkblast.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

MIGRATIONS = [
    # Task 1 + 2: multi-year leaderboard FB% fed into kHR v2
    "ALTER TABLE batter_stats ADD COLUMN IF NOT EXISTS fb_percent FLOAT",
    # Task 3: Kasper-style HR Form (displayed column)
    "ALTER TABLE batter_stats ADD COLUMN IF NOT EXISTS hr_form_kasper_pct FLOAT",
    "ALTER TABLE batter_stats ADD COLUMN IF NOT EXISTS hr_form_kasper_arrow VARCHAR(8)",
    # Task 4: multi-year pitcher vulnerability inputs
    "ALTER TABLE pitcher_stats ADD COLUMN IF NOT EXISTS brl_allowed_rate FLOAT",
    "ALTER TABLE pitcher_stats ADD COLUMN IF NOT EXISTS hh_allowed_rate FLOAT",
]

# SQLite uses a slightly different syntax
MIGRATIONS_SQLITE = [
    "ALTER TABLE batter_stats ADD COLUMN fb_percent REAL",
    "ALTER TABLE batter_stats ADD COLUMN hr_form_kasper_pct REAL",
    "ALTER TABLE batter_stats ADD COLUMN hr_form_kasper_arrow TEXT",
    "ALTER TABLE pitcher_stats ADD COLUMN brl_allowed_rate REAL",
    "ALTER TABLE pitcher_stats ADD COLUMN hh_allowed_rate REAL",
]

is_sqlite = DATABASE_URL.startswith("sqlite")
stmts = MIGRATIONS_SQLITE if is_sqlite else MIGRATIONS

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

print(f"Connecting to: {DATABASE_URL[:60]}…")
with engine.begin() as conn:
    for stmt in stmts:
        try:
            conn.execute(text(stmt))
            print(f"  OK  {stmt[:80]}")
        except Exception as e:
            msg = str(e)
            if "already exists" in msg or "duplicate column" in msg.lower():
                print(f"  SKIP (already exists): {stmt[:60]}")
            else:
                print(f"  ERROR: {e}", file=sys.stderr)

print("Migration complete.")
