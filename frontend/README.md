# ParkBlast Frontend

React dashboard for the ParkBlast daily HR & K intelligence tool.

## Local development

```bash
npm install
echo "VITE_API_BASE=http://localhost:8000" > .env.local
npm run dev
```

Then open http://localhost:5173.

## Production deployment (Vercel)

This project is set up to deploy directly from GitHub to Vercel.

Required environment variable on Vercel:
- `VITE_API_BASE` — full URL of the backend API (e.g. `https://parkblast-api.onrender.com`)

That's it. Vercel auto-detects Vite and runs `npm install && npm run build`.
