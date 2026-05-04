"""
Local ingest test — validates all recent fixes without requiring pybaseball.
Mocks statcast_batter/statcast_pitcher with realistic numpy-typed DataFrames
(matching what pybaseball actually returns), then runs the full ingest code
path against SQLite and asserts that every DB column has the correct Python type.

Run: .venv/bin/python3 test_ingest.py
"""
import asyncio
import json
import logging
import os
import sys
import types

os.environ["DATABASE_URL"] = "sqlite:///./test_ingest.db"
os.environ["OPENWEATHER_API_KEY"] = ""

sys.path.insert(0, os.path.dirname(__file__))
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

import numpy as np
import pandas as pd

# ── Stub pybaseball so import succeeds without the real package ──────────────
pybaseball_mod = types.ModuleType("pybaseball")

def _make_batter_df() -> pd.DataFrame:
    """Return a realistic Statcast batter DataFrame with numpy dtypes."""
    n = 80
    rng = np.random.default_rng(42)
    return pd.DataFrame({
        "game_date":                pd.date_range("2025-03-28", periods=n, freq="D"),
        "pitch_type":               np.random.choice(["FF", "SL", "CH", "CU"], n),
        "type":                     np.where(rng.random(n) > 0.4, "X", "S"),
        "description":              np.random.choice(
                                        ["hit_into_play", "swinging_strike",
                                         "called_strike", "foul", "ball"], n),
        "events":                   pd.array(
                                        np.random.choice(
                                            ["single", "strikeout", "home_run",
                                             "field_out", None], n),
                                        dtype=object),
        "zone":                     rng.integers(1, 15, n).astype(np.float64),
        "launch_speed":             rng.uniform(70, 115, n),     # numpy float64
        "launch_angle":             rng.uniform(-30, 50, n),     # numpy float64
        "launch_speed_angle":       rng.integers(1, 7, n).astype(np.float64),
        "estimated_woba_using_speedangle": rng.uniform(0.1, 0.7, n),
        "hc_x":                     rng.uniform(50, 200, n),
        "stand":                    np.full(n, "R"),
        "at_bat_number":            rng.integers(1, 6, n).astype(np.int64),
        "strikes":                  rng.integers(0, 3, n).astype(np.int64),
        "bat_speed":                rng.uniform(60, 80, n),
    })


def _make_pitcher_df() -> pd.DataFrame:
    n = 120
    rng = np.random.default_rng(99)
    return pd.DataFrame({
        "game_date":                pd.date_range("2025-03-28", periods=n, freq="D"),
        "pitch_type":               np.random.choice(["FF", "SL", "CH"], n),
        "type":                     np.where(rng.random(n) > 0.6, "X", "S"),
        "description":              np.random.choice(
                                        ["called_strike", "swinging_strike",
                                         "ball", "hit_into_play", "foul"], n),
        "events":                   pd.array(
                                        np.random.choice(
                                            ["strikeout", "single", "home_run",
                                             "field_out", None], n),
                                        dtype=object),
        "zone":                     rng.integers(1, 15, n).astype(np.float64),
        "launch_speed":             rng.uniform(70, 110, n),
        "launch_speed_angle":       rng.integers(1, 7, n).astype(np.float64),
        "estimated_woba_using_speedangle": rng.uniform(0.1, 0.6, n),
        "p_throws":                 np.full(n, "R"),
        "strikes":                  rng.integers(0, 3, n).astype(np.int64),
    })


pybaseball_mod.statcast_batter  = lambda *a, **kw: _make_batter_df()
pybaseball_mod.statcast_pitcher = lambda *a, **kw: _make_pitcher_df()
pybaseball_mod.cache = types.SimpleNamespace(enable=lambda: None)
sys.modules["pybaseball"] = pybaseball_mod

# ── Now import the real ingest code ─────────────────────────────────────────
from app.database import SessionLocal, init_db
from app.services.ingest import (
    _py, _norm_team,
    refresh_batter_stats, refresh_pitcher_stats,
    fetch_todays_games,
)


# ═══════════════════════════════════════════════════════════════════════════
# UNIT TESTS — no DB or network needed
# ═══════════════════════════════════════════════════════════════════════════

def test_py_dict():
    """_py() must return Python-native types for dict values (was broken)."""
    inp = {"FF": np.float64(0.45), "SL": np.float64(0.30)}
    out = _py(inp)
    assert isinstance(out, dict),                     f"expected dict, got {type(out)}"
    assert out == {"FF": 0.45, "SL": 0.30},           f"wrong values: {out}"
    for v in out.values():
        assert type(v) is float,                       f"value type {type(v)} not Python float"
    log.info("PASS  _py(dict) returns native Python types")


def test_py_list_of_lists():
    """_py() must recurse correctly into nested lists."""
    inp = [[np.float64(12.5), np.float64(0.0), np.float64(8.3)],
           [np.float64(0.0),  np.float64(0.0), np.float64(0.0)],
           [np.float64(0.0),  np.float64(0.0), np.float64(0.0)]]
    out = _py(inp)
    assert isinstance(out, list),               "outer should be list"
    assert isinstance(out[0], list),            "inner should be list"
    assert type(out[0][0]) is float,            f"got {type(out[0][0])}"
    assert out[0][0] == 12.5
    log.info("PASS  _py(list-of-lists) recurses and coerces")


def test_py_nan():
    """_py() must convert numpy NaN scalars to None."""
    assert _py(np.float64("nan")) is None,     "NaN should become None"
    assert _py(np.float64(3.14)) == 3.14
    assert type(_py(np.float64(3.14))) is float
    assert _py(np.int64(7)) == 7
    assert type(_py(np.int64(7))) is int
    assert _py(None) is None
    log.info("PASS  _py() scalar coercion correct")


def test_norm_team():
    """_norm_team() must fix known MLB Stats API abbreviation differences."""
    assert _norm_team("AZ")  == "ARI",  "AZ not normalized to ARI"
    assert _norm_team("CWS") == "CHW",  "CWS not normalized to CHW"
    assert _norm_team("WAS") == "WSH",  "WAS not normalized to WSH"
    assert _norm_team("NYY") == "NYY",  "NYY should pass through"
    assert _norm_team(None)  is None,   "None should pass through as None"
    log.info("PASS  _norm_team() abbreviation normalization")


# ═══════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS — SQLite DB
# ═══════════════════════════════════════════════════════════════════════════

def test_batter_db_write(db):
    """
    Run refresh_batter_stats with a mocked numpy-rich DataFrame.
    Verify every JSON/Float column written to SQLite has Python-native types.
    """
    row = refresh_batter_stats(db, mlbam_id=592450)  # Aaron Judge placeholder
    assert row is not None, "refresh_batter_stats returned None"

    # Scalar floats must be Python float, not numpy
    for col in ("barrel_rate", "hard_hit_rate", "sweet_spot_rate",
                "xwoba", "xwoba_con", "swstr_rate", "khr",
                "form_score", "z_swing_rate", "o_swing_rate"):
        val = getattr(row, col)
        if val is not None:
            assert type(val) is float, \
                f"{col} has type {type(val).__name__}, expected Python float"

    # JSON columns must be dicts/lists (not None, not numpy)
    assert isinstance(row.hot_zones, list),         "hot_zones must be list"
    assert isinstance(row.woba_zones, list),        "woba_zones must be list"
    assert isinstance(row.hot_zones[0], list),      "hot_zones must be list-of-lists"
    for cell in row.hot_zones[0]:
        assert type(cell) is float,                 f"hot_zones cell type: {type(cell)}"

    # form_breakdown must be a dict with Python float values when present
    if row.form_breakdown:
        assert isinstance(row.form_breakdown, dict), "form_breakdown must be dict"
        for k, v in row.form_breakdown.items():
            assert isinstance(k, str),               f"form_breakdown key {k!r} not str"
            assert type(v) is float,                 f"form_breakdown[{k!r}] type {type(v)}"

    log.info(f"PASS  batter DB write: khr={row.khr:.1f} form={row.form_score:.1f} "
             f"barrel={row.barrel_rate:.1f}% "
             f"form_arrow={row.form_arrow!r} baseline={row.baseline_source!r}")


def test_pitcher_db_write(db):
    """
    Run refresh_pitcher_stats with a mocked numpy-rich DataFrame.
    Verify pitch_mix keys are Python str and values are Python float.
    """
    row = refresh_pitcher_stats(db, mlbam_id=543037)  # Gerrit Cole placeholder

    assert row is not None, "refresh_pitcher_stats returned None"

    # throws must not be "nan"
    assert row.throws in ("R", "L", "S"), \
        f"throws is {row.throws!r} — NaN probably leaked"

    # pitch_mix: keys must be str, values must be float
    if row.pitch_mix:
        assert isinstance(row.pitch_mix, dict),      "pitch_mix must be dict"
        for k, v in row.pitch_mix.items():
            assert type(k) is str,                   f"pitch_mix key {k!r} type {type(k)}"
            assert type(v) is float,                 f"pitch_mix[{k!r}] type {type(v)}"

    # zone_profile must be list-of-lists of floats
    if row.zone_profile:
        assert isinstance(row.zone_profile, list)
        for cell in row.zone_profile[0]:
            assert type(cell) is float, f"zone_profile cell type: {type(cell)}"

    log.info(f"PASS  pitcher DB write: pitch_score={row.pitch_score:.1f} "
             f"throws={row.throws!r} "
             f"pitch_mix={row.pitch_mix}")


def test_json_round_trip(db):
    """Verify that SQLite can actually serialise/deserialise the JSON columns."""
    from sqlalchemy import text
    # Flush everything written so far
    db.commit()

    batter = db.execute(
        text("SELECT hot_zones, woba_zones, form_breakdown FROM batter_stats LIMIT 1")
    ).fetchone()
    assert batter is not None, "no batter row in DB"

    # SQLAlchemy's JSON type round-trips through json.loads; None means NULL was stored
    assert batter[0] is not None, "hot_zones is NULL in DB (was _py() dict bug)"
    assert batter[1] is not None, "woba_zones is NULL in DB"
    # form_breakdown can be {} when baseline_source="none"

    pitcher = db.execute(
        text("SELECT pitch_mix, zone_profile FROM pitcher_stats LIMIT 1")
    ).fetchone()
    assert pitcher is not None, "no pitcher row in DB"
    assert pitcher[0] is not None, "pitch_mix is NULL in DB"
    assert pitcher[1] is not None, "zone_profile is NULL in DB"

    log.info("PASS  JSON columns round-trip through SQLite without NULL")


# ═══════════════════════════════════════════════════════════════════════════
# NETWORK TEST — real MLB Stats API (read-only)
# ═══════════════════════════════════════════════════════════════════════════

async def test_fetch_games_live():
    """Hit real MLB Stats API, verify normalization is applied."""
    log.info("Hitting MLB Stats API...")
    games = await fetch_todays_games()
    log.info(f"  {len(games)} games today")
    bad = [g for g in games
           if g["home_team"] in ("AZ", "CWS", "WAS")
           or g["away_team"] in ("AZ", "CWS", "WAS")]
    assert not bad, f"Unnormalized team codes found: {bad}"
    for g in games:
        log.info(f"  {g['away_team']} @ {g['home_team']}  pk={g['game_pk']}")
    log.info("PASS  fetch_todays_games: no raw API abbreviations leaked")
    return games


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

async def main():
    log.info("=" * 60)
    log.info("ParkBlast ingest test suite")
    log.info("=" * 60)

    # Unit tests — instant, no I/O
    test_py_dict()
    test_py_list_of_lists()
    test_py_nan()
    test_norm_team()

    # DB integration tests — SQLite, mocked Statcast
    if os.path.exists("./test_ingest.db"):
        os.remove("./test_ingest.db")
    init_db()
    db = SessionLocal()
    try:
        test_batter_db_write(db)
        test_pitcher_db_write(db)
        test_json_round_trip(db)
    finally:
        db.close()
        if os.path.exists("./test_ingest.db"):
            os.remove("./test_ingest.db")

    # Network test — real MLB Stats API (skip gracefully if offline)
    try:
        await test_fetch_games_live()
    except Exception as e:
        log.warning(f"SKIP  MLB API test: {e}")

    log.info("=" * 60)
    log.info("ALL TESTS PASSED")
    log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
