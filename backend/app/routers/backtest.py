"""
Backtest endpoints — query the picks log to evaluate model performance.

GET /api/backtest/summary?days=30
  → overall hit rate, broken down by score-tier and component

GET /api/backtest/component/{component}?days=30
  → bucketed performance for a specific component (form_score, khr, etc.)
    Tells you whether high values of that component actually predict outcomes.
"""
from datetime import date, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import get_db
from app.models.db import PickSnapshot, PickOutcome

router = APIRouter()

VALID_COMPONENTS = {"matchup_score", "test_score", "ceiling", "zone_fit",
                    "form_score", "khr", "pitch_score", "strikeout_score"}


@router.get("/api/backtest/summary")
def backtest_summary(
    days: int = Query(30, ge=1, le=365),
    prop_type: str = Query("hr", pattern="^(hr|k)$"),
    db: Session = Depends(get_db),
):
    """
    Overall pick performance over the last N days.
    Joins PickSnapshot to PickOutcome and reports hit rates.
    """
    cutoff = date.today() - timedelta(days=days)

    # Inner join — only count picks where we have an outcome
    rows = (
        db.query(PickSnapshot, PickOutcome)
        .join(
            PickOutcome,
            (PickSnapshot.game_date == PickOutcome.game_date) &
            (PickSnapshot.game_pk == PickOutcome.game_pk) &
            (PickSnapshot.player_id == PickOutcome.player_id) &
            (PickSnapshot.prop_type == PickOutcome.prop_type)
        )
        .filter(
            PickSnapshot.game_date >= cutoff,
            PickSnapshot.prop_type == prop_type,
            PickOutcome.game_status == "completed",
        )
        .all()
    )

    if not rows:
        return {
            "days": days,
            "prop_type": prop_type,
            "total_picks": 0,
            "message": "No completed outcomes yet. Wait for games to be played and outcomes recorded.",
        }

    total = len(rows)
    if prop_type == "hr":
        hits = sum(1 for _, o in rows if o.hit_hr == 1)
        hit_rate = round(hits / total * 100, 2) if total else 0
        # Break down by matchup score tier
        tiers = {"A (70+)": [], "B (60-70)": [], "C (50-60)": [], "D (<50)": []}
        for snap, out in rows:
            score = snap.matchup_score or 0
            tier_key = "A (70+)" if score >= 70 else "B (60-70)" if score >= 60 else "C (50-60)" if score >= 50 else "D (<50)"
            tiers[tier_key].append(out.hit_hr or 0)
        tier_breakdown = {
            t: {
                "picks": len(v),
                "hit_rate": round(sum(v) / len(v) * 100, 2) if v else None,
            }
            for t, v in tiers.items()
        }
        return {
            "days": days,
            "prop_type": prop_type,
            "total_picks": total,
            "hits": hits,
            "hit_rate_pct": hit_rate,
            "by_tier": tier_breakdown,
        }
    else:  # k props — placeholder until we have line odds + thresholds
        return {
            "days": days,
            "prop_type": prop_type,
            "total_picks": total,
            "note": "K-prop backtesting requires line odds to evaluate over/under outcomes.",
        }


@router.get("/api/backtest/component/{component}")
def backtest_component(
    component: str,
    days: int = Query(30, ge=1, le=365),
    bucket_size: int = Query(10, ge=5, le=25),
    prop_type: str = Query("hr", pattern="^(hr|k)$"),
    db: Session = Depends(get_db),
):
    """
    Bucketed hit-rate by component value.
    Example: form_score 70-80 → 18% HR rate, 80-90 → 22% HR rate.
    Tells you whether the component is actually predictive.
    """
    if component not in VALID_COMPONENTS:
        raise HTTPException(status_code=400, detail=f"unknown component. Valid: {sorted(VALID_COMPONENTS)}")

    cutoff = date.today() - timedelta(days=days)
    rows = (
        db.query(PickSnapshot, PickOutcome)
        .join(
            PickOutcome,
            (PickSnapshot.game_date == PickOutcome.game_date) &
            (PickSnapshot.game_pk == PickOutcome.game_pk) &
            (PickSnapshot.player_id == PickOutcome.player_id) &
            (PickSnapshot.prop_type == PickOutcome.prop_type)
        )
        .filter(
            PickSnapshot.game_date >= cutoff,
            PickSnapshot.prop_type == prop_type,
            PickOutcome.game_status == "completed",
        )
        .all()
    )

    if not rows:
        return {"component": component, "days": days, "total": 0}

    buckets = {}
    for snap, out in rows:
        v = getattr(snap, component, None)
        if v is None:
            continue
        bucket = int(v // bucket_size) * bucket_size
        key = f"{bucket}-{bucket + bucket_size}"
        if key not in buckets:
            buckets[key] = {"picks": 0, "hits": 0}
        buckets[key]["picks"] += 1
        if prop_type == "hr":
            buckets[key]["hits"] += (out.hit_hr or 0)

    output = []
    for key in sorted(buckets.keys(), key=lambda k: int(k.split("-")[0])):
        b = buckets[key]
        output.append({
            "bucket": key,
            "picks": b["picks"],
            "hit_rate_pct": round(b["hits"] / b["picks"] * 100, 2) if b["picks"] else None,
        })

    return {
        "component": component,
        "days": days,
        "bucket_size": bucket_size,
        "buckets": output,
    }


@router.get("/api/backtest/log")
def backtest_log(
    days: int = Query(7, ge=1, le=30),
    prop_type: str = Query("hr", pattern="^(hr|k)$"),
    db: Session = Depends(get_db),
):
    """
    Row-level picks log for the last N days.
    Left-joins PickSnapshot to PickOutcome so pending picks (no outcome yet)
    are included with game_status='pending'. HR props are filtered to lineup
    players only (batting_order IS NOT NULL).
    """
    cutoff = date.today() - timedelta(days=days)

    rows = (
        db.query(PickSnapshot, PickOutcome)
        .outerjoin(
            PickOutcome,
            (PickSnapshot.game_date == PickOutcome.game_date)
            & (PickSnapshot.game_pk == PickOutcome.game_pk)
            & (PickSnapshot.player_id == PickOutcome.player_id)
            & (PickSnapshot.prop_type == PickOutcome.prop_type),
        )
        .filter(
            PickSnapshot.game_date >= cutoff,
            PickSnapshot.prop_type == prop_type,
            PickSnapshot.batting_order.isnot(None),
        )
        .order_by(PickSnapshot.game_date.desc(), PickSnapshot.matchup_score.desc())
        .all()
    )

    picks = []
    completed = pending = hits = 0
    tier_counts = {
        "A": {"picks": 0, "hits": 0},
        "B": {"picks": 0, "hits": 0},
        "C": {"picks": 0, "hits": 0},
        "D": {"picks": 0, "hits": 0},
    }

    for snap, out in rows:
        ms = snap.matchup_score or 0
        tier = "A" if ms >= 70 else "B" if ms >= 60 else "C" if ms >= 50 else "D"

        if out is None:
            game_status = "pending"
            hit_hr = hr_count = pa = None
            pending += 1
        elif out.game_status == "completed":
            game_status = "completed"
            hit_hr = out.hit_hr
            hr_count = out.hr_count
            pa = out.pa
            completed += 1
            if hit_hr == 1:
                hits += 1
                tier_counts[tier]["hits"] += 1
            tier_counts[tier]["picks"] += 1
        else:
            game_status = out.game_status  # "postponed" | "did_not_play"
            hit_hr = None
            hr_count = None
            pa = out.pa

        picks.append({
            "game_date": snap.game_date.isoformat(),
            "game_pk": snap.game_pk,
            "player_id": snap.player_id,
            "player_name": snap.player_name,
            "team": snap.team,
            "opponent_team": snap.opponent_team,
            "venue": snap.venue,
            "batting_order": snap.batting_order,
            "matchup_score": snap.matchup_score,
            "test_score": snap.test_score,
            "ceiling": snap.ceiling,
            "zone_fit": snap.zone_fit,
            "form_score": snap.form_score,
            "khr": snap.khr,
            "sample_tier": snap.sample_tier,
            "tier": tier,
            "game_status": game_status,
            "hit_hr": hit_hr,
            "hr_count": hr_count,
            "pa": pa,
        })

    by_tier = {
        t: {
            "picks": v["picks"],
            "hits": v["hits"],
            "hit_rate_pct": round(v["hits"] / v["picks"] * 100, 1) if v["picks"] else None,
        }
        for t, v in tier_counts.items()
    }

    return {
        "days": days,
        "prop_type": prop_type,
        "total": len(rows),
        "completed": completed,
        "pending": pending,
        "hits": hits,
        "hit_rate_pct": round(hits / completed * 100, 1) if completed else None,
        "by_tier": by_tier,
        "picks": picks,
    }
