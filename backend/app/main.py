"""
FastAPI entry point.
Run locally:  uvicorn app.main:app --reload
Deploy on Render: see backend/Procfile (web: uvicorn app.main:app --host 0.0.0.0 --port $PORT)
"""
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers.slate import router as slate_router
from app.routers.backtest import router as backtest_router
from app.database import init_db

app = FastAPI(title="Parkblast HR API", version="0.1.0")

# CORS — let the Vercel frontend hit this
allowed_origins = os.getenv("ALLOWED_ORIGINS", "*").split(",")
# Spec forbids allow_credentials=True with wildcard origin; browsers reject it.
allow_credentials = "*" not in allowed_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(slate_router)
app.include_router(backtest_router)


@app.on_event("startup")
def on_startup():
    init_db()


@app.get("/")
def root():
    return {"service": "parkblast-api", "status": "ok"}
