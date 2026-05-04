import React, { useState, useMemo, useEffect } from "react";
import {
  TrendingUp,
  TrendingDown,
  Minus,
  Loader2,
  AlertCircle,
  Wind,
  Sun,
  ArrowUpDown,
  ArrowUp,
  ArrowDown,
  ChevronLeft,
} from "lucide-react";

const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000";

// =============================================================================
// HEAT-MAP COLOR SCALE — green (good) → yellow → red (bad), per-column ranges
// =============================================================================

function heatColor(value, min, max, invert = false) {
  if (value == null || isNaN(value)) return "transparent";
  if (max === min) return "rgba(120, 120, 120, 0.15)";
  let pct = (value - min) / (max - min);
  if (invert) pct = 1 - pct;
  pct = Math.max(0, Math.min(1, pct));

  let r, g, b;
  if (pct < 0.5) {
    const t = pct * 2;
    r = 190 + (220 - 190) * t;
    g = 50 + (180 - 50) * t;
    b = 50 + (60 - 50) * t;
  } else {
    const t = (pct - 0.5) * 2;
    r = 220 + (60 - 220) * t;
    g = 180 + (160 - 180) * t;
    b = 60 + (80 - 60) * t;
  }
  return `rgba(${Math.round(r)}, ${Math.round(g)}, ${Math.round(b)}, 0.85)`;
}

function textOnColor(bg) {
  if (bg === "transparent") return "#e7e5e4";
  const m = bg.match(/\d+/g);
  if (!m) return "#e7e5e4";
  const [r, g, b] = m.map(Number);
  const L = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  return L > 0.55 ? "#0c0a09" : "#fafaf9";
}

// =============================================================================
// COLUMN DEFINITIONS
// =============================================================================

const HITTER_COLS = [
  {
    key: "matchup",
    label: "Matchup",
    fmt: (v) => v?.toFixed(1),
    invert: false,
  },
  {
    key: "test_score",
    label: "Test",
    fmt: (v) => v?.toFixed(1),
    invert: false,
  },
  {
    key: "ceiling",
    label: "Ceiling",
    fmt: (v) => v?.toFixed(1),
    invert: false,
  },
  {
    key: "zone_fit",
    label: "Zone Fit",
    fmt: (v) => v?.toFixed(2),
    invert: false,
  },
  { key: "form_score", label: "Form", fmt: "form", invert: false },
  { key: "khr", label: "kHR", fmt: (v) => v?.toFixed(1), invert: false },
  {
    key: "pitches",
    label: "Pitches",
    fmt: (v) => v?.toLocaleString(),
    invert: false,
    neutral: true,
  },
  {
    key: "bip",
    label: "BIP",
    fmt: (v) => v?.toLocaleString(),
    invert: false,
    neutral: true,
  },
  { key: "iso", label: "ISO", fmt: (v) => v?.toFixed(3), invert: false },
  { key: "xwoba", label: "xwOBA", fmt: (v) => v?.toFixed(3), invert: false },
  {
    key: "xwoba_con",
    label: "xwOBAcon",
    fmt: (v) => v?.toFixed(3),
    invert: false,
  },
  {
    key: "swstr_rate",
    label: "SwStr%",
    fmt: (v) => `${v?.toFixed(1)}%`,
    invert: true,
  },
  {
    key: "pulled_barrel_rate",
    label: "PulledBrl%",
    fmt: (v) => `${v?.toFixed(1)}%`,
    invert: false,
  },
  {
    key: "sweet_spot_rate",
    label: "Sweet%",
    fmt: (v) => `${v?.toFixed(1)}%`,
    invert: false,
  },
  {
    key: "hard_hit_rate",
    label: "HH%",
    fmt: (v) => `${v?.toFixed(1)}%`,
    invert: false,
  },
  {
    key: "launch_angle",
    label: "LA",
    fmt: (v) => v?.toFixed(1),
    invert: false,
  },
  {
    key: "barrel_rate",
    label: "Brl%",
    fmt: (v) => `${v?.toFixed(1)}%`,
    invert: false,
  },
];

const PITCHER_COLS = [
  {
    key: "pitch_score",
    label: "Pitch",
    fmt: (v) => v?.toFixed(1),
    invert: false,
  },
  {
    key: "strikeout_score",
    label: "K Score",
    fmt: (v) => v?.toFixed(1),
    invert: false,
  },
  { key: "hr_per_9", label: "HR/9", fmt: (v) => v?.toFixed(2), invert: true },
  {
    key: "xwoba_against",
    label: "xwOBA",
    fmt: (v) => v?.toFixed(3),
    invert: true,
  },
  {
    key: "csw_rate",
    label: "CSW%",
    fmt: (v) => `${v?.toFixed(1)}%`,
    invert: false,
  },
  {
    key: "swstr_rate",
    label: "SwStr%",
    fmt: (v) => `${v?.toFixed(1)}%`,
    invert: false,
  },
  {
    key: "putaway_rate",
    label: "PutAway%",
    fmt: (v) => `${v?.toFixed(1)}%`,
    invert: false,
  },
  {
    key: "ball_rate",
    label: "Ball%",
    fmt: (v) => `${v?.toFixed(1)}%`,
    invert: true,
  },
  { key: "siera", label: "SIERA", fmt: (v) => v?.toFixed(2), invert: true },
  {
    key: "barrel_per_bip",
    label: "Brl/BIP%",
    fmt: (v) => `${v?.toFixed(1)}%`,
    invert: true,
  },
];

// =============================================================================
// HELPERS / TIER CONSTANTS
// =============================================================================

const TIER_COLOR = {
  high: { bg: "rgba(34, 197, 94, 0.15)", fg: "#4ade80", label: "HIGH" },
  medium: { bg: "rgba(120, 113, 108, 0.2)", fg: "#d6d3d1", label: "MED" },
  thin: { bg: "rgba(234, 179, 8, 0.18)", fg: "#facc15", label: "THIN" },
  very_thin: { bg: "rgba(239, 68, 68, 0.18)", fg: "#f87171", label: "V.THIN" },
};

const LINEUP_STATUS = {
  confirmed: { fg: "#4ade80", label: "IN" },
  projected: { fg: "#facc15", label: "PROJ" },
  bench: { fg: "#78716c", label: "BENCH" },
};

const PITCHER_TIER = {
  fade: {
    bg: "rgba(34, 197, 94, 0.18)",
    fg: "#4ade80",
    label: "FADE",
    border: "#16a34a",
  },
  neutral: {
    bg: "rgba(120, 113, 108, 0.2)",
    fg: "#d6d3d1",
    label: "NEUTRAL",
    border: "#78716c",
  },
  attack: {
    bg: "rgba(239, 68, 68, 0.18)",
    fg: "#f87171",
    label: "ATTACK",
    border: "#dc2626",
  },
};

function FormCell({ value, arrow }) {
  const Icon =
    arrow === "up" ? TrendingUp : arrow === "down" ? TrendingDown : Minus;
  const color =
    arrow === "up" ? "#4ade80" : arrow === "down" ? "#f87171" : "#a8a29e";
  return (
    <span
      style={{ display: "inline-flex", alignItems: "center", gap: 4, color }}
    >
      <Icon size={11} strokeWidth={2.5} />
      <span style={{ fontFamily: "JetBrains Mono, monospace", fontSize: 11 }}>
        {value != null ? value.toFixed(0) : "–"}
      </span>
    </span>
  );
}

function computeRanges(rows, cols) {
  const ranges = {};
  cols.forEach((c) => {
    if (c.neutral) return;
    const vals = rows
      .map((r) => r[c.key])
      .filter((v) => v != null && !isNaN(v));
    if (!vals.length) return;
    ranges[c.key] = { min: Math.min(...vals), max: Math.max(...vals) };
  });
  return ranges;
}

function formatGameTime(utcStr) {
  if (!utcStr) return "";
  try {
    const d = new Date(utcStr);
    return (
      d.toLocaleTimeString("en-US", {
        hour: "numeric",
        minute: "2-digit",
        timeZone: "America/New_York",
      }) + " ET"
    );
  } catch {
    return utcStr;
  }
}

function useSlate() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetch(`${API_BASE}/api/slate`)
      .then((r) => {
        if (!r.ok) throw new Error(`API ${r.status}`);
        return r.json();
      })
      .then((d) => {
        if (!cancelled) {
          setData(d);
          setLoading(false);
        }
      })
      .catch((e) => {
        if (!cancelled) {
          setError(e.message);
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return { data, error, loading };
}

function useGameDetail(gamePk) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!gamePk) return;
    let cancelled = false;
    setLoading(true);
    fetch(`${API_BASE}/api/games/${gamePk}`)
      .then((r) => {
        if (!r.ok) throw new Error(`API ${r.status}`);
        return r.json();
      })
      .then((d) => {
        if (!cancelled) {
          setData(d);
          setLoading(false);
        }
      })
      .catch((e) => {
        if (!cancelled) {
          setError(e.message);
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [gamePk]);

  return { data, error, loading };
}

function useBacktestLog(days, propType) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setData(null);
    setError(null);
    fetch(`${API_BASE}/api/backtest/log?days=${days}&prop_type=${propType}`)
      .then((r) => {
        if (!r.ok) throw new Error(`API ${r.status}`);
        return r.json();
      })
      .then((d) => {
        if (!cancelled) { setData(d); setLoading(false); }
      })
      .catch((e) => {
        if (!cancelled) { setError(e.message); setLoading(false); }
      });
    return () => { cancelled = true; };
  }, [days, propType]);

  return { data, error, loading };
}

// =============================================================================
// SHARED SORT HEADER
// =============================================================================

function SortHeader({ col, sortKey, sortDir, onClick }) {
  const active = sortKey === col.key;
  return (
    <th
      onClick={() => onClick(col.key)}
      style={{
        padding: "8px 10px",
        textAlign: "left",
        fontSize: 10,
        fontWeight: 600,
        letterSpacing: "0.08em",
        textTransform: "uppercase",
        cursor: "pointer",
        userSelect: "none",
        color: active ? "#fafaf9" : "#a8a29e",
        background: active ? "rgba(251, 146, 60, 0.08)" : "#1c1917",
        borderBottom: active ? "2px solid #fb923c" : "1px solid #292524",
        whiteSpace: "nowrap",
        position: "sticky",
        top: 0,
        zIndex: 2,
      }}
    >
      <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
        {col.label}
        {active ? (
          sortDir === "desc" ? (
            <ArrowDown size={11} strokeWidth={2.5} />
          ) : (
            <ArrowUp size={11} strokeWidth={2.5} />
          )
        ) : (
          <ArrowUpDown size={10} strokeWidth={1.8} style={{ opacity: 0.4 }} />
        )}
      </span>
    </th>
  );
}

// =============================================================================
// HITTER TABLE
// =============================================================================

function HitterTable({ rows, defaultSort = "khr" }) {
  const [sortKey, setSortKey] = useState(defaultSort);
  const [sortDir, setSortDir] = useState("desc");

  const sorted = useMemo(() => {
    const arr = [...rows];
    arr.sort((a, b) => {
      const av = a[sortKey] ?? -Infinity;
      const bv = b[sortKey] ?? -Infinity;
      const cmp = av < bv ? -1 : av > bv ? 1 : 0;
      return sortDir === "desc" ? -cmp : cmp;
    });
    return arr;
  }, [rows, sortKey, sortDir]);

  const ranges = useMemo(() => computeRanges(sorted, HITTER_COLS), [sorted]);

  function onSort(key) {
    if (key === sortKey) setSortDir((d) => (d === "desc" ? "asc" : "desc"));
    else {
      setSortKey(key);
      setSortDir("desc");
    }
  }

  if (!rows.length) {
    return (
      <div
        style={{
          padding: 24,
          color: "#a8a29e",
          fontSize: 13,
          fontStyle: "italic",
          textAlign: "center",
        }}
      >
        No batter data yet.
      </div>
    );
  }

  return (
    <>
      <div
        style={{
          overflowX: "auto",
          maxHeight: "70vh",
          overflowY: "auto",
          border: "1px solid #292524",
          borderRadius: 4,
        }}
      >
        <table
          style={{
            borderCollapse: "collapse",
            width: "100%",
            fontFamily: "Inter, sans-serif",
            fontSize: 11,
          }}
        >
          <thead>
            <tr>
              <th
                style={{
                  padding: "8px 10px",
                  textAlign: "left",
                  fontSize: 10,
                  fontWeight: 600,
                  letterSpacing: "0.08em",
                  textTransform: "uppercase",
                  color: "#a8a29e",
                  borderBottom: "1px solid #292524",
                  position: "sticky",
                  left: 0,
                  top: 0,
                  background: "#1c1917",
                  zIndex: 3,
                  minWidth: 240,
                }}
              >
                Batter
              </th>
              {HITTER_COLS.map((c) => (
                <SortHeader
                  key={c.key}
                  col={c}
                  sortKey={sortKey}
                  sortDir={sortDir}
                  onClick={onSort}
                />
              ))}
            </tr>
          </thead>
          <tbody>
            {sorted.map((row, idx) => {
              const tier = TIER_COLOR[row.sample_tier] || TIER_COLOR.medium;
              return (
                <tr
                  key={row.id}
                  style={{ borderBottom: "1px solid rgba(41,37,36,0.6)" }}
                >
                  <td
                    style={{
                      padding: "7px 10px",
                      position: "sticky",
                      left: 0,
                      background: idx % 2 ? "#171413" : "#0c0a09",
                      zIndex: 1,
                      borderRight: "1px solid #292524",
                      whiteSpace: "nowrap",
                    }}
                  >
                    <div
                      style={{ display: "flex", alignItems: "center", gap: 8 }}
                    >
                      <span
                        style={{
                          fontSize: 9,
                          padding: "1px 5px",
                          borderRadius: 2,
                          background: tier.bg,
                          color: tier.fg,
                          fontWeight: 700,
                          letterSpacing: "0.05em",
                          minWidth: 36,
                          textAlign: "center",
                        }}
                      >
                        {tier.label}
                      </span>
                      {row.lineup_status &&
                        LINEUP_STATUS[row.lineup_status] && (
                          <span
                            style={{
                              fontSize: 9,
                              padding: "1px 5px",
                              borderRadius: 2,
                              border: `1px solid ${LINEUP_STATUS[row.lineup_status].fg}`,
                              color: LINEUP_STATUS[row.lineup_status].fg,
                              fontWeight: 700,
                              letterSpacing: "0.05em",
                              opacity: row.lineup_status === "bench" ? 0.65 : 1,
                            }}
                          >
                            {LINEUP_STATUS[row.lineup_status].label}
                          </span>
                        )}
                      <span
                        style={{
                          color: "#fafaf9",
                          fontWeight: 500,
                          fontSize: 12,
                        }}
                      >
                        {row.name}
                      </span>
                      <span
                        style={{
                          color: "#78716c",
                          fontSize: 10,
                          fontFamily: "JetBrains Mono, monospace",
                        }}
                      >
                        {row.team}
                        {row.batting_order
                          ? ` · #${row.batting_order}`
                          : ""} · {row.bats}
                      </span>
                    </div>
                  </td>
                  {HITTER_COLS.map((c) => {
                    let display, bg;
                    if (c.fmt === "form") {
                      display = (
                        <FormCell
                          value={row.form_score}
                          arrow={row.form_arrow}
                        />
                      );
                      bg = "transparent";
                    } else {
                      const v = row[c.key];
                      display = v == null ? "–" : c.fmt(v);
                      if (c.neutral || ranges[c.key] == null)
                        bg = "transparent";
                      else
                        bg = heatColor(
                          v,
                          ranges[c.key].min,
                          ranges[c.key].max,
                          c.invert,
                        );
                    }
                    return (
                      <td
                        key={c.key}
                        style={{
                          padding: "6px 10px",
                          fontFamily: "JetBrains Mono, monospace",
                          fontSize: 11,
                          whiteSpace: "nowrap",
                          background: bg,
                          color:
                            c.fmt === "form"
                              ? undefined
                              : bg === "transparent"
                                ? "#d6d3d1"
                                : textOnColor(bg),
                        }}
                      >
                        {display}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}

// =============================================================================
// PITCHER TABLE
// =============================================================================

function PitcherTable({
  rows,
  defaultSort = "pitch_score",
  defaultDir = "desc",
}) {
  const [sortKey, setSortKey] = useState(defaultSort);
  const [sortDir, setSortDir] = useState(defaultDir);

  const sorted = useMemo(() => {
    const arr = [...rows];
    arr.sort((a, b) => {
      const av = a[sortKey] ?? -Infinity;
      const bv = b[sortKey] ?? -Infinity;
      const cmp = av < bv ? -1 : av > bv ? 1 : 0;
      return sortDir === "desc" ? -cmp : cmp;
    });
    return arr;
  }, [rows, sortKey, sortDir]);

  const ranges = useMemo(() => computeRanges(sorted, PITCHER_COLS), [sorted]);

  function onSort(key) {
    if (key === sortKey) setSortDir((d) => (d === "desc" ? "asc" : "desc"));
    else {
      setSortKey(key);
      setSortDir("desc");
    }
  }

  if (!rows.length) {
    return (
      <div
        style={{
          padding: 24,
          color: "#a8a29e",
          fontSize: 13,
          fontStyle: "italic",
          textAlign: "center",
        }}
      >
        No pitcher data — probables not yet announced.
      </div>
    );
  }

  return (
    <div
      style={{
        overflowX: "auto",
        maxHeight: "75vh",
        overflowY: "auto",
        border: "1px solid #292524",
        borderRadius: 4,
      }}
    >
      <table
        style={{
          borderCollapse: "collapse",
          width: "100%",
          fontFamily: "Inter, sans-serif",
          fontSize: 11,
        }}
      >
        <thead>
          <tr>
            <th
              style={{
                padding: "8px 10px",
                textAlign: "left",
                fontSize: 10,
                fontWeight: 600,
                letterSpacing: "0.08em",
                textTransform: "uppercase",
                color: "#a8a29e",
                borderBottom: "1px solid #292524",
                position: "sticky",
                left: 0,
                top: 0,
                background: "#1c1917",
                zIndex: 3,
                minWidth: 240,
              }}
            >
              Pitcher
            </th>
            <th
              style={{
                padding: "8px 10px",
                textAlign: "left",
                fontSize: 10,
                fontWeight: 600,
                letterSpacing: "0.08em",
                textTransform: "uppercase",
                color: "#a8a29e",
                borderBottom: "1px solid #292524",
                position: "sticky",
                top: 0,
                background: "#1c1917",
                zIndex: 2,
              }}
            >
              Tier
            </th>
            {PITCHER_COLS.map((c) => (
              <SortHeader
                key={c.key}
                col={c}
                sortKey={sortKey}
                sortDir={sortDir}
                onClick={onSort}
              />
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((row, idx) => {
            const tier = PITCHER_TIER[row.tier] || PITCHER_TIER.neutral;
            return (
              <tr
                key={row.id}
                style={{ borderBottom: "1px solid rgba(41,37,36,0.6)" }}
              >
                <td
                  style={{
                    padding: "7px 10px",
                    position: "sticky",
                    left: 0,
                    background: idx % 2 ? "#171413" : "#0c0a09",
                    zIndex: 1,
                    borderRight: "1px solid #292524",
                    whiteSpace: "nowrap",
                  }}
                >
                  <div
                    style={{ display: "flex", alignItems: "baseline", gap: 8 }}
                  >
                    <span
                      style={{
                        color: "#fafaf9",
                        fontWeight: 500,
                        fontSize: 12,
                      }}
                    >
                      {row.name}
                    </span>
                    <span
                      style={{
                        color: "#78716c",
                        fontSize: 10,
                        fontFamily: "JetBrains Mono, monospace",
                      }}
                    >
                      {row.throws}HP · {row.away_team}@{row.home_team}
                    </span>
                  </div>
                </td>
                <td style={{ padding: "7px 10px", whiteSpace: "nowrap" }}>
                  <span
                    style={{
                      fontSize: 10,
                      padding: "2px 8px",
                      borderRadius: 3,
                      background: tier.bg,
                      color: tier.fg,
                      fontWeight: 700,
                      letterSpacing: "0.08em",
                      border: `1px solid ${tier.border}`,
                    }}
                  >
                    {tier.label}
                  </span>
                </td>
                {PITCHER_COLS.map((c) => {
                  const v = row[c.key];
                  const display = v == null ? "–" : c.fmt(v);
                  const bg =
                    ranges[c.key] == null
                      ? "transparent"
                      : heatColor(
                          v,
                          ranges[c.key].min,
                          ranges[c.key].max,
                          c.invert,
                        );
                  return (
                    <td
                      key={c.key}
                      style={{
                        padding: "6px 10px",
                        fontFamily: "JetBrains Mono, monospace",
                        fontSize: 11,
                        whiteSpace: "nowrap",
                        background: bg,
                        color:
                          bg === "transparent" ? "#d6d3d1" : textOnColor(bg),
                      }}
                    >
                      {display}
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// =============================================================================
// GAME CARD (slate browser)
// =============================================================================

function GameCard({ game, topHitters, topPitchers, onClick }) {
  const ourPitchers = topPitchers.filter(
    (p) =>
      (p.home_team === game.home_team && p.away_team === game.away_team) ||
      (p.away_team === game.home_team && p.home_team === game.away_team),
  );
  const gameHitters = topHitters
    .filter((h) => h.game_pk === game.game_pk)
    .sort((a, b) => (b.khr ?? 0) - (a.khr ?? 0))
    .slice(0, 3);
  const wx = game.weather || {};

  return (
    <div
      onClick={onClick}
      style={{
        background: "linear-gradient(180deg, #1c1917 0%, #0c0a09 100%)",
        border: "1px solid #292524",
        borderRadius: 6,
        padding: 18,
        cursor: "pointer",
        transition: "all 0.15s",
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.borderColor = "#fb923c";
        e.currentTarget.style.transform = "translateY(-1px)";
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.borderColor = "#292524";
        e.currentTarget.style.transform = "translateY(0)";
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "baseline",
          marginBottom: 10,
        }}
      >
        <div
          style={{
            fontFamily: "JetBrains Mono, monospace",
            fontSize: 13,
            color: "#fb923c",
            fontWeight: 600,
            letterSpacing: "0.05em",
          }}
        >
          {game.away_team} @ {game.home_team}
        </div>
        <div
          style={{
            fontFamily: "JetBrains Mono, monospace",
            fontSize: 10,
            color: "#a8a29e",
          }}
        >
          {formatGameTime(game.game_time_utc)}
        </div>
      </div>

      <div
        style={{
          fontSize: 11,
          color: "#78716c",
          marginBottom: 12,
          fontFamily: "JetBrains Mono, monospace",
        }}
      >
        {game.park_name}
      </div>

      {ourPitchers.length > 0 && (
        <div
          style={{
            marginBottom: 12,
            paddingBottom: 12,
            borderBottom: "1px solid #292524",
          }}
        >
          {ourPitchers.map((p) => {
            const tier = PITCHER_TIER[p.tier] || PITCHER_TIER.neutral;
            return (
              <div
                key={p.id}
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  marginBottom: 4,
                }}
              >
                <span style={{ fontSize: 11, color: "#d6d3d1" }}>{p.name}</span>
                <span
                  style={{
                    fontSize: 9,
                    padding: "1px 6px",
                    borderRadius: 2,
                    background: tier.bg,
                    color: tier.fg,
                    fontWeight: 700,
                    letterSpacing: "0.08em",
                  }}
                >
                  {tier.label}
                </span>
              </div>
            );
          })}
        </div>
      )}

      {gameHitters.length > 0 && (
        <div style={{ marginBottom: 10 }}>
          <div
            style={{
              fontSize: 9,
              color: "#78716c",
              textTransform: "uppercase",
              letterSpacing: "0.1em",
              marginBottom: 4,
            }}
          >
            Top kHR
          </div>
          {gameHitters.map((h) => (
            <div
              key={h.id}
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "baseline",
                fontSize: 11,
                marginBottom: 2,
              }}
            >
              <span style={{ color: "#e7e5e4" }}>{h.name}</span>
              <span
                style={{
                  fontFamily: "JetBrains Mono, monospace",
                  color: "#fb923c",
                  fontWeight: 600,
                }}
              >
                {h.khr?.toFixed(1)}
              </span>
            </div>
          ))}
        </div>
      )}

      {(wx.temp != null || wx.wind != null) && (
        <div
          style={{
            display: "flex",
            gap: 12,
            fontSize: 10,
            color: "#78716c",
            fontFamily: "JetBrains Mono, monospace",
            marginTop: 8,
            flexWrap: "wrap",
          }}
        >
          {wx.temp != null && (
            <span>
              <Sun
                size={10}
                style={{ display: "inline", verticalAlign: "middle" }}
              />{" "}
              {wx.temp}°F
            </span>
          )}
          {wx.wind != null && (
            <span>
              <Wind
                size={10}
                style={{ display: "inline", verticalAlign: "middle" }}
              />{" "}
              {wx.wind} mph {wx.wind_dir}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

// =============================================================================
// GAME DETAIL VIEW
// =============================================================================

function deriveTier(p) {
  const hr9 = p.hr_per_9 ?? 1.2;
  const ps = p.pitch_score ?? 50;
  if (ps >= 65 && hr9 <= 1.2) return "fade";
  if (ps <= 45 || hr9 >= 1.6) return "attack";
  return "neutral";
}

function GameDetail({ gamePk, onBack }) {
  const { data, error, loading } = useGameDetail(gamePk);
  const [hitterTab, setHitterTab] = useState("away");

  if (loading) return <CenteredLoader text="Loading game..." />;
  if (error) return <ErrorPanel error={error} />;
  if (!data) return null;

  const pitcherRows = [
    data.away_pitcher && {
      ...data.away_pitcher,
      id: `away-${data.away_pitcher.name}`,
      home_team: data.home_team,
      away_team: data.away_team,
      venue: data.venue,
      tier: deriveTier(data.away_pitcher),
    },
    data.home_pitcher && {
      ...data.home_pitcher,
      id: `home-${data.home_pitcher.name}`,
      home_team: data.home_team,
      away_team: data.away_team,
      venue: data.venue,
      tier: deriveTier(data.home_pitcher),
    },
  ].filter(Boolean);

  const hitterTabs = [
    { key: "away", label: data.away_team, rows: data.away_hitters || [] },
    { key: "home", label: data.home_team, rows: data.home_hitters || [] },
  ];
  const activeHitters = hitterTabs.find((t) => t.key === hitterTab)?.rows || [];

  return (
    <div>
      <button
        onClick={onBack}
        style={{
          background: "transparent",
          border: "1px solid #292524",
          color: "#a8a29e",
          fontSize: 11,
          padding: "6px 12px",
          borderRadius: 4,
          cursor: "pointer",
          fontFamily: "JetBrains Mono, monospace",
          letterSpacing: "0.05em",
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
          marginBottom: 16,
        }}
        onMouseEnter={(e) => (e.currentTarget.style.borderColor = "#fb923c")}
        onMouseLeave={(e) => (e.currentTarget.style.borderColor = "#292524")}
      >
        <ChevronLeft size={12} /> ALL GAMES
      </button>

      <div style={{ marginBottom: 20 }}>
        <h2
          style={{
            fontFamily: "JetBrains Mono, monospace",
            fontSize: 22,
            color: "#fafaf9",
            fontWeight: 700,
            letterSpacing: "-0.02em",
            margin: "0 0 4px 0",
          }}
        >
          {data.away_team} @ {data.home_team}
        </h2>
        <div
          style={{
            fontSize: 12,
            color: "#a8a29e",
            fontFamily: "JetBrains Mono, monospace",
          }}
        >
          {data.park_name} · {formatGameTime(data.game_time_utc)}
          {data.weather &&
            (data.weather.temp != null || data.weather.wind != null) && (
              <span style={{ marginLeft: 14 }}>
                {data.weather.temp != null && `${data.weather.temp}°F`}
                {data.weather.wind != null &&
                  ` · ${data.weather.wind} mph ${data.weather.wind_dir || ""}`}
              </span>
            )}
        </div>
      </div>

      <SectionHeader>Pitchers</SectionHeader>
      <PitcherTable
        rows={pitcherRows}
        defaultSort="pitch_score"
        defaultDir="desc"
      />

      <div style={{ height: 24 }} />

      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 0,
          marginBottom: 12,
          borderBottom: "1px solid #292524",
        }}
      >
        <span
          style={{
            fontFamily: "JetBrains Mono, monospace",
            fontSize: 11,
            color: "#a8a29e",
            textTransform: "uppercase",
            letterSpacing: "0.12em",
            marginRight: 16,
          }}
        >
          Batters
        </span>
        <div style={{ display: "flex", gap: 0 }}>
          {hitterTabs.map((t) => (
            <button
              key={t.key}
              onClick={() => setHitterTab(t.key)}
              style={{
                padding: "6px 16px",
                fontFamily: "JetBrains Mono, monospace",
                fontSize: 11,
                fontWeight: 600,
                letterSpacing: "0.08em",
                background: "transparent",
                color: hitterTab === t.key ? "#fb923c" : "#78716c",
                border: "none",
                borderBottom:
                  hitterTab === t.key
                    ? "2px solid #fb923c"
                    : "2px solid transparent",
                cursor: "pointer",
                transition: "all 0.1s",
                marginBottom: -1,
              }}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>
      <HitterTable rows={activeHitters} defaultSort="khr" />
    </div>
  );
}

// =============================================================================
// SLATE BROWSER (landing)
// =============================================================================

function SlateBrowser({ slate, onSelectGame }) {
  if (!slate.games?.length) {
    return (
      <div
        style={{
          padding: 40,
          textAlign: "center",
          color: "#a8a29e",
          fontSize: 13,
        }}
      >
        No games scheduled today, or data not yet refreshed. Run the cron.
      </div>
    );
  }

  return (
    <div>
      <SectionHeader>
        {slate.games.length} games · {slate.date}
      </SectionHeader>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))",
          gap: 14,
        }}
      >
        {slate.games.map((g) => (
          <GameCard
            key={g.game_pk}
            game={g}
            topHitters={slate.top_hitters || []}
            topPitchers={slate.top_pitchers || []}
            onClick={() => onSelectGame(g.game_pk)}
          />
        ))}
      </div>
    </div>
  );
}

// =============================================================================
// PITCHER LEADERBOARD
// =============================================================================

function PitcherLeaderboard({ slate }) {
  const rows = slate.top_pitchers || [];
  if (!rows.length) {
    return (
      <div
        style={{
          padding: 40,
          textAlign: "center",
          color: "#a8a29e",
          fontSize: 13,
        }}
      >
        No pitcher data yet. Refresh after probables are announced.
      </div>
    );
  }

  return (
    <div>
      <SectionHeader>
        All Pitchers · FADE-first (high pitch score = avoid)
      </SectionHeader>
      <div
        style={{
          fontSize: 11,
          color: "#78716c",
          marginBottom: 12,
          lineHeight: 1.6,
        }}
      >
        FADE pitchers are dealing — don't bet HR props against their lineup.
        ATTACK pitchers are getting hit hard — target their opposing hitters.
        Click any column header to re-sort.
      </div>
      <PitcherTable rows={rows} defaultSort="pitch_score" defaultDir="desc" />
    </div>
  );
}

// =============================================================================
// BACKTEST VIEW
// =============================================================================

const TIER_COLORS = {
  A: "#22c55e",
  B: "#fb923c",
  C: "#facc15",
  D: "#78716c",
};

function BacktestStatBar({ data }) {
  const edge =
    data.hit_rate_pct != null ? (data.hit_rate_pct - 7.0).toFixed(1) : null;
  const cells = [
    { label: "TOTAL PICKS", value: data.total },
    { label: "COMPLETED", value: data.completed },
    { label: "PENDING", value: data.pending },
    {
      label: "HR RATE",
      value: data.hit_rate_pct != null ? `${data.hit_rate_pct}%` : "—",
      color:
        data.hit_rate_pct == null
          ? "#a8a29e"
          : data.hit_rate_pct >= 10
          ? "#22c55e"
          : data.hit_rate_pct >= 7
          ? "#fb923c"
          : "#f87171",
    },
    { label: "BASELINE", value: "7.0%" },
    {
      label: "EDGE",
      value: edge != null ? `${edge > 0 ? "+" : ""}${edge}%` : "—",
      color:
        edge == null ? "#a8a29e" : Number(edge) >= 0 ? "#22c55e" : "#f87171",
    },
  ];
  return (
    <div
      style={{
        display: "flex",
        gap: 1,
        marginBottom: 20,
        background: "#1c1917",
        borderRadius: 4,
        overflow: "hidden",
        border: "1px solid #292524",
      }}
    >
      {cells.map((c) => (
        <div
          key={c.label}
          style={{
            flex: 1,
            padding: "10px 14px",
            borderRight: "1px solid #292524",
          }}
        >
          <div
            style={{
              fontSize: 9,
              fontFamily: "JetBrains Mono, monospace",
              color: "#57534e",
              letterSpacing: "0.1em",
              marginBottom: 4,
            }}
          >
            {c.label}
          </div>
          <div
            style={{
              fontSize: 18,
              fontWeight: 700,
              color: c.color || "#fafaf9",
              fontFamily: "JetBrains Mono, monospace",
            }}
          >
            {c.value}
          </div>
        </div>
      ))}
    </div>
  );
}

function BacktestTierTable({ byTier }) {
  return (
    <div style={{ marginBottom: 24 }}>
      <div
        style={{
          fontFamily: "JetBrains Mono, monospace",
          fontSize: 11,
          color: "#a8a29e",
          letterSpacing: "0.12em",
          textTransform: "uppercase",
          marginBottom: 10,
          paddingBottom: 6,
          borderBottom: "1px solid #292524",
        }}
      >
        Performance by Tier (completed only)
      </div>
      <div style={{ display: "flex", gap: 8 }}>
        {["A", "B", "C", "D"].map((t) => {
          const v = byTier[t] || {};
          return (
            <div
              key={t}
              style={{
                flex: 1,
                padding: "10px 12px",
                background: "#1c1917",
                border: `1px solid ${TIER_COLORS[t]}44`,
                borderRadius: 4,
              }}
            >
              <div
                style={{
                  fontSize: 18,
                  fontWeight: 800,
                  color: TIER_COLORS[t],
                  fontFamily: "JetBrains Mono, monospace",
                }}
              >
                {t}
              </div>
              <div
                style={{
                  fontSize: 11,
                  color: "#a8a29e",
                  fontFamily: "JetBrains Mono, monospace",
                  marginTop: 4,
                }}
              >
                {v.picks || 0} picks
              </div>
              <div
                style={{
                  fontSize: 16,
                  fontWeight: 700,
                  color: "#fafaf9",
                  fontFamily: "JetBrains Mono, monospace",
                  marginTop: 4,
                }}
              >
                {v.hit_rate_pct != null ? `${v.hit_rate_pct}%` : "—"}
              </div>
              <div
                style={{
                  fontSize: 10,
                  color: "#57534e",
                  fontFamily: "JetBrains Mono, monospace",
                }}
              >
                {v.hits || 0} HRs
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function BacktestPickRow({ pick }) {
  const resultColor =
    pick.game_status === "pending"
      ? "#44403c"
      : pick.game_status === "did_not_play"
      ? "#44403c"
      : pick.hit_hr === 1
      ? "#166534"
      : "#7f1d1d";

  const resultText =
    pick.game_status === "pending"
      ? "PENDING"
      : pick.game_status === "did_not_play"
      ? "DNP"
      : pick.game_status === "postponed"
      ? "PPD"
      : pick.hit_hr === 1
      ? `HR ${pick.hr_count > 1 ? `(${pick.hr_count})` : "✓"}`
      : "MISS";

  const resultTextColor =
    pick.game_status === "pending" || pick.game_status === "did_not_play"
      ? "#78716c"
      : pick.hit_hr === 1
      ? "#4ade80"
      : "#f87171";

  const cell = {
    padding: "6px 10px",
    fontSize: 12,
    fontFamily: "JetBrains Mono, monospace",
    color: "#e7e5e4",
    borderRight: "1px solid #292524",
    verticalAlign: "middle",
  };

  return (
    <tr
      style={{
        borderLeft: `3px solid ${resultColor}`,
        background:
          pick.game_status === "pending"
            ? "transparent"
            : pick.hit_hr === 1
            ? "rgba(34,197,94,0.06)"
            : pick.game_status === "completed"
            ? "rgba(239,68,68,0.04)"
            : "transparent",
      }}
    >
      <td style={{ ...cell, color: "#a8a29e" }}>#{pick.batting_order}</td>
      <td style={{ ...cell, fontWeight: 600, color: "#fafaf9" }}>
        {pick.player_name}
      </td>
      <td style={{ ...cell, color: "#78716c" }}>
        {pick.team} vs {pick.opponent_team}
      </td>
      <td style={{ ...cell, textAlign: "right" }}>
        {pick.matchup_score?.toFixed(1) ?? "—"}
      </td>
      <td style={{ ...cell, textAlign: "right", color: "#a8a29e" }}>
        {pick.zone_fit?.toFixed(2) ?? "—"}
      </td>
      <td style={{ ...cell, textAlign: "right", color: "#a8a29e" }}>
        {pick.form_score?.toFixed(1) ?? "—"}
      </td>
      <td style={{ ...cell, textAlign: "right", color: "#a8a29e" }}>
        {pick.khr?.toFixed(1) ?? "—"}
      </td>
      <td
        style={{
          ...cell,
          textAlign: "center",
          color: TIER_COLORS[pick.tier] || "#a8a29e",
          fontWeight: 700,
        }}
      >
        {pick.tier}
      </td>
      <td
        style={{
          ...cell,
          textAlign: "right",
          color: resultTextColor,
          fontWeight: 700,
          borderRight: "none",
        }}
      >
        {resultText}
      </td>
    </tr>
  );
}

function BacktestDateGroup({ dateStr, picks }) {
  const [year, month, day] = dateStr.split("-").map(Number);
  const d = new Date(year, month - 1, day);
  const label = d.toLocaleDateString("en-US", {
    weekday: "short",
    month: "short",
    day: "numeric",
  });
  const completed = picks.filter((p) => p.game_status === "completed");
  const hits = completed.filter((p) => p.hit_hr === 1).length;

  const thStyle = {
    padding: "5px 10px",
    fontSize: 9,
    fontFamily: "JetBrains Mono, monospace",
    color: "#57534e",
    fontWeight: 600,
    letterSpacing: "0.08em",
    textTransform: "uppercase",
    textAlign: "left",
    borderBottom: "1px solid #292524",
    whiteSpace: "nowrap",
  };

  return (
    <div style={{ marginBottom: 28 }}>
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          gap: 12,
          marginBottom: 8,
          paddingBottom: 6,
          borderBottom: "1px solid #292524",
        }}
      >
        <span
          style={{
            fontFamily: "JetBrains Mono, monospace",
            fontSize: 12,
            fontWeight: 700,
            color: "#fafaf9",
          }}
        >
          {label}
        </span>
        {completed.length > 0 && (
          <span
            style={{
              fontFamily: "JetBrains Mono, monospace",
              fontSize: 11,
              color: "#78716c",
            }}
          >
            {hits}/{completed.length} HR
          </span>
        )}
        {picks.some((p) => p.game_status === "pending") && (
          <span
            style={{
              fontFamily: "JetBrains Mono, monospace",
              fontSize: 10,
              color: "#44403c",
            }}
          >
            {picks.filter((p) => p.game_status === "pending").length} pending
          </span>
        )}
      </div>
      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr>
              <th style={{ ...thStyle, textAlign: "right" }}>#</th>
              <th style={thStyle}>Player</th>
              <th style={thStyle}>Matchup</th>
              <th style={{ ...thStyle, textAlign: "right" }}>Score</th>
              <th style={{ ...thStyle, textAlign: "right" }}>Zone</th>
              <th style={{ ...thStyle, textAlign: "right" }}>Form</th>
              <th style={{ ...thStyle, textAlign: "right" }}>kHR</th>
              <th style={{ ...thStyle, textAlign: "center" }}>Tier</th>
              <th style={{ ...thStyle, textAlign: "right", borderRight: "none" }}>
                Result
              </th>
            </tr>
          </thead>
          <tbody>
            {picks.map((p) => (
              <BacktestPickRow
                key={`${p.game_pk}-${p.player_id}`}
                pick={p}
              />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function BacktestView() {
  const [days, setDays] = useState(7);
  const { data, error, loading } = useBacktestLog(days, "hr");

  const grouped = useMemo(() => {
    if (!data?.picks) return [];
    const map = {};
    for (const p of data.picks) {
      if (!map[p.game_date]) map[p.game_date] = [];
      map[p.game_date].push(p);
    }
    return Object.entries(map).sort(([a], [b]) => b.localeCompare(a));
  }, [data]);

  return (
    <div>
      <div
        style={{
          display: "flex",
          gap: 8,
          alignItems: "center",
          marginBottom: 20,
        }}
      >
        <span
          style={{
            fontSize: 10,
            fontFamily: "JetBrains Mono, monospace",
            color: "#57534e",
            letterSpacing: "0.1em",
          }}
        >
          SHOW LAST
        </span>
        {[2, 7, 14, 30].map((d) => (
          <button
            key={d}
            onClick={() => setDays(d)}
            style={{
              padding: "4px 12px",
              fontFamily: "JetBrains Mono, monospace",
              fontSize: 11,
              fontWeight: 600,
              background: days === d ? "#fb923c" : "transparent",
              color: days === d ? "#0c0a09" : "#a8a29e",
              border: `1px solid ${days === d ? "#fb923c" : "#292524"}`,
              borderRadius: 2,
              cursor: "pointer",
            }}
          >
            {d}D
          </button>
        ))}
      </div>

      {loading && <CenteredLoader text="Loading backtest log..." />}
      {error && <ErrorPanel error={error} />}

      {data && !loading && (
        <>
          <BacktestStatBar data={data} />
          {data.completed > 0 && <BacktestTierTable byTier={data.by_tier} />}
          {grouped.length === 0 ? (
            <div
              style={{
                padding: "40px 0",
                color: "#57534e",
                fontFamily: "JetBrains Mono, monospace",
                fontSize: 12,
              }}
            >
              No picks in the last {days} day{days > 1 ? "s" : ""}. Run the
              daily ingest and snapshot to populate.
            </div>
          ) : (
            grouped.map(([dateStr, picks]) => (
              <BacktestDateGroup key={dateStr} dateStr={dateStr} picks={picks} />
            ))
          )}
        </>
      )}
    </div>
  );
}

// =============================================================================
// COMMON UI
// =============================================================================

function SectionHeader({ children }) {
  return (
    <div
      style={{
        fontFamily: "JetBrains Mono, monospace",
        fontSize: 11,
        color: "#a8a29e",
        textTransform: "uppercase",
        letterSpacing: "0.12em",
        marginBottom: 12,
        paddingBottom: 6,
        borderBottom: "1px solid #292524",
      }}
    >
      {children}
    </div>
  );
}

function CenteredLoader({ text }) {
  return (
    <div style={{ padding: 60, textAlign: "center", color: "#a8a29e" }}>
      <Loader2
        size={20}
        style={{ animation: "spin 1s linear infinite", marginBottom: 8 }}
      />
      <div style={{ fontSize: 12, fontFamily: "JetBrains Mono, monospace" }}>
        {text}
      </div>
      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  );
}

function ErrorPanel({ error }) {
  return (
    <div
      style={{
        padding: 20,
        border: "1px solid #7f1d1d",
        borderRadius: 4,
        background: "rgba(127, 29, 29, 0.15)",
        color: "#fca5a5",
        display: "flex",
        gap: 10,
        alignItems: "flex-start",
      }}
    >
      <AlertCircle size={16} style={{ flexShrink: 0, marginTop: 2 }} />
      <div>
        <div style={{ fontWeight: 600, marginBottom: 4 }}>API error</div>
        <div style={{ fontSize: 12, fontFamily: "JetBrains Mono, monospace" }}>
          {error}
        </div>
        <div style={{ fontSize: 11, color: "#a8a29e", marginTop: 8 }}>
          Make sure backend is running:{" "}
          <code>uvicorn app.main:app --reload</code>
        </div>
      </div>
    </div>
  );
}

// =============================================================================
// ROOT
// =============================================================================

export default function HRPropDashboard() {
  const { data: slate, error, loading } = useSlate();
  const [view, setView] = useState("games");
  const [selectedGame, setSelectedGame] = useState(null);

  return (
    <div
      style={{
        minHeight: "100vh",
        background: "#0c0a09",
        color: "#e7e5e4",
        fontFamily: "Inter, system-ui, sans-serif",
        padding: "20px 28px 60px",
      }}
    >
      <header
        style={{
          marginBottom: 24,
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-end",
          flexWrap: "wrap",
          gap: 16,
        }}
      >
        <div>
          <div
            style={{
              fontFamily: "JetBrains Mono, monospace",
              fontSize: 11,
              color: "#fb923c",
              letterSpacing: "0.2em",
              marginBottom: 4,
            }}
          >
            ▌ PARKBLAST
          </div>
          <h1
            style={{
              margin: 0,
              fontSize: 28,
              fontWeight: 800,
              letterSpacing: "-0.03em",
              color: "#fafaf9",
            }}
          >
            Daily HR &amp; K Intelligence
          </h1>
        </div>
        <nav
          style={{
            display: "flex",
            gap: 0,
            border: "1px solid #292524",
            borderRadius: 4,
            overflow: "hidden",
          }}
        >
          {[
            { key: "games", label: "GAMES" },
            { key: "pitchers", label: "PITCHERS" },
            { key: "backtest", label: "BACKTEST" },
          ].map((t) => (
            <button
              key={t.key}
              onClick={() => {
                setView(t.key);
                setSelectedGame(null);
              }}
              style={{
                padding: "8px 16px",
                fontFamily: "JetBrains Mono, monospace",
                fontSize: 11,
                fontWeight: 600,
                letterSpacing: "0.1em",
                background: view === t.key ? "#fb923c" : "transparent",
                color: view === t.key ? "#0c0a09" : "#a8a29e",
                border: "none",
                cursor: "pointer",
                transition: "all 0.1s",
              }}
            >
              {t.label}
            </button>
          ))}
        </nav>
      </header>

      {view !== "backtest" && loading && <CenteredLoader text="Loading slate..." />}
      {view !== "backtest" && error && <ErrorPanel error={error} />}
      {view !== "backtest" && slate && !error && (
        <>
          {view === "games" && !selectedGame && (
            <SlateBrowser slate={slate} onSelectGame={setSelectedGame} />
          )}
          {view === "games" && selectedGame && (
            <GameDetail
              gamePk={selectedGame}
              onBack={() => setSelectedGame(null)}
            />
          )}
          {view === "pitchers" && <PitcherLeaderboard slate={slate} />}
        </>
      )}
      {view === "backtest" && <BacktestView />}

      <footer
        style={{
          marginTop: 60,
          paddingTop: 20,
          borderTop: "1px solid #1c1917",
          fontSize: 10,
          color: "#57534e",
          fontFamily: "JetBrains Mono, monospace",
          letterSpacing: "0.05em",
        }}
      >
        PARKBLAST · v0.2 · for entertainment purposes only · 21+ ·
        cross-reference with PropFinder
      </footer>
    </div>
  );
}
