"""DB session management."""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from app.models.db import Base

import logging as _logging

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    _logging.warning(
        "DATABASE_URL not set — falling back to local SQLite. "
        "Set DATABASE_URL in production (Render environment variables panel)."
    )
    DATABASE_URL = "sqlite:///./parkblast.db"
# Railway provides DATABASE_URL like postgres://... which SQLAlchemy 2.x needs as postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def init_db():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
