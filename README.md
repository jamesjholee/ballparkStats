# Parkblast — MLB HR prop dashboard

Daily home-run prop intelligence: Statcast metrics, zone-fit scoring, recent form,
park factors, and weather, weighted into a Kasper-style heat-mapped table for every
starter in today's MLB slate.

## What it shows

**Hitter table** (heat-mapped, sortable, per-game tabs):
Matchup · Test Score · Ceiling · Zone Fit · HR Form (with ↑/↓/→ arrow) · kHR ·
Pitches · BIP · ISO · xwOBA · xwOBAcon · SwStr% · PulledBrl% · SweetSpot% · HH% ·
LA · Brl/BIP%

Player names are colored by sample size: **High** (green) · **Medium** (black) ·
**Thin** (orange) · **Very Thin** (red).

**Pitcher table:**
Pitch Score · Strikeout Score · xwOBA · CSW% · SwStr% · PutAway% · Ball% · SIERA
· PulledBrl% · Brl/BIP% · HR/9

## Architecture

```
Frontend (Vercel)  →  Backend API (Railway)  →  Postgres (Railway)
                              ↑
                    Daily cron (Railway)
                              ↓
                    pybaseball  +  MLB Stats API  +  OpenWeatherMap
```

## What's where

```
backend/
├── app/
│   ├── main.py             FastAPI entry point
│   ├── database.py         SQLAlchemy session
│   ├── constants.py        Park factors, stadium coords (update yearly)
│   ├── cron.py             Daily refresh runner (Railway cron entry)
│   ├── models/db.py        DB schema
│   ├── routers/slate.py    GET /api/slate — what the frontend calls
│   └── services/
│       ├── ingest.py       pybaseball / MLB API / weather pulls
│       └── scoring.py      The HR score algorithm
├── requirements.txt
└── Procfile

frontend/
└── HRPropDashboard.jsx     React dashboard (already built)
```

## Local development

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Set env vars
export DATABASE_URL="sqlite:///./parkblast.db"   # or your Postgres URL
export OPENWEATHER_API_KEY="get-from-openweathermap.org"

# Initialize and pull data (slow — 10–20 min for full slate)
python -m app.cron

# Run the API
uvicorn app.main:app --reload

# Hit the endpoint
curl http://localhost:8000/api/slate
```

## Deploying

### 1. Backend on Railway

1. Push this repo to GitHub.
2. New project on [railway.app](https://railway.app) → Deploy from GitHub.
3. Set the root directory to `backend/`.
4. Add a Postgres plugin to the project. Railway injects `DATABASE_URL` automatically.
5. Add env var `OPENWEATHER_API_KEY` (free tier — 1,000 calls/day, you'll use ~30).
6. Add env var `ALLOWED_ORIGINS=https://your-frontend.vercel.app` once the frontend is up.
7. Railway auto-detects the `Procfile`. Service should boot.
8. **Add a cron service** (Railway Settings → Cron):
   - Schedule: `0 10 * * *` (06:00 ET)
   - Command: `python -m app.cron`
   - Optionally a second one at `0 18 * * *` (14:00 ET) for confirmed lineups.

### 2. Frontend on Vercel

1. Update `HRPropDashboard.jsx` to fetch from `/api/slate` against your Railway URL:
   ```js
   const API_BASE = import.meta.env.VITE_API_BASE || 'https://your-app.railway.app';
   const res = await fetch(`${API_BASE}/api/slate`);
   ```
2. Wrap it in a Vite or Next.js project (`npm create vite@latest parkblast -- --template react`).
3. Drop the JSX in, `npm install lucide-react tailwindcss`.
4. Push to GitHub, deploy on Vercel, set env var `VITE_API_BASE`.

## Cost expectations (monthly)

| Service        | Tier         | Cost     |
| -------------- | ------------ | -------- |
| Railway web    | Hobby        | $5/mo    |
| Railway DB     | Hobby        | $5/mo    |
| Railway cron   | included     | $0       |
| Vercel         | Hobby        | $0       |
| OpenWeatherMap | Free (1K/day)| $0       |
| **Total**      |              | **~$10/mo** |

## Known limitations

- **Park factors are static.** Update `constants.py` once a year using FanGraphs.
- **No odds feed wired up.** The `HRPropOdds` model exists; you'd need a sportsbook
  API (The Odds API, OddsJam, etc) to populate it. Free tier of The Odds API gives
  500 calls/month which is plenty for HR props.
- **bat_speed coverage is partial.** Statcast started tracking it mid-2024;
  some batters have no data. Score falls back to neutral (50) when missing.
- **Hot zones get noisy with low HR totals.** A batter with 2 HRs in 30 days has
  a misleading hot-zone grid. Consider falling back to season-long zones for
  batters with <5 HR in window.
- **MLB Stats API lineups are projected pre-game.** They flip to confirmed ~30
  minutes before first pitch. Your second cron run should capture the confirmed set.
- **Reminder:** Cross-reference with PropFinder before placing any picks.
# ballparkStats
