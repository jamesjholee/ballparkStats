import React, { useState, useMemo, useEffect } from 'react';
import { TrendingUp, TrendingDown, Minus, Loader2, AlertCircle, Wind, Sun, ArrowUpDown, ArrowUp, ArrowDown, ChevronLeft } from 'lucide-react';

const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000';

// =============================================================================
// HEAT-MAP COLOR SCALE — green (good) → yellow → red (bad), per-column ranges
// =============================================================================

function heatColor(value, min, max, invert = false) {
  if (value == null || isNaN(value)) return 'transparent';
  if (max === min) return 'rgba(120, 120, 120, 0.15)';
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
  if (bg === 'transparent') return '#e7e5e4';
  const m = bg.match(/\d+/g);
  if (!m) return '#e7e5e4';
  const [r, g, b] = m.map(Number);
  const L = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  return L > 0.55 ? '#0c0a09' : '#fafaf9';
}

// =============================================================================
// COLUMN DEFINITIONS
// =============================================================================

const HITTER_COLS = [
  { key: 'matchup',           label: 'Matchup',       fmt: v => v?.toFixed(1),  invert: false },
  { key: 'test_score',        label: 'Test',          fmt: v => v?.toFixed(1),  invert: false },
  { key: 'ceiling',           label: 'Ceiling',       fmt: v => v?.toFixed(1),  invert: false },
  { key: 'zone_fit',          label: 'Zone Fit',      fmt: v => v?.toFixed(2),  invert: false },
  { key: 'hr_form_pct',       label: 'Form',          fmt: 'form',              invert: false },
  { key: 'khr',               label: 'kHR',           fmt: v => v?.toFixed(1),  invert: false },
  { key: 'pitches',           label: 'Pitches',       fmt: v => v?.toLocaleString(), invert: false, neutral: true },
  { key: 'bip',               label: 'BIP',           fmt: v => v?.toLocaleString(), invert: false, neutral: true },
  { key: 'iso',               label: 'ISO',           fmt: v => v?.toFixed(3),  invert: false },
  { key: 'xwoba',             label: 'xwOBA',         fmt: v => v?.toFixed(3),  invert: false },
  { key: 'xwoba_con',         label: 'xwOBAcon',      fmt: v => v?.toFixed(3),  invert: false },
  { key: 'swstr_rate',        label: 'SwStr%',        fmt: v => `${v?.toFixed(1)}%`, invert: true  },
  { key: 'pulled_barrel_rate',label: 'PulledBrl%',    fmt: v => `${v?.toFixed(1)}%`, invert: false },
  { key: 'sweet_spot_rate',   label: 'Sweet%',        fmt: v => `${v?.toFixed(1)}%`, invert: false },
  { key: 'hard_hit_rate',     label: 'HH%',           fmt: v => `${v?.toFixed(1)}%`, invert: false },
  { key: 'launch_angle',      label: 'LA',            fmt: v => v?.toFixed(1),  invert: false },
  { key: 'barrel_rate',       label: 'Brl%',          fmt: v => `${v?.toFixed(1)}%`, invert: false },
];

const PITCHER_COLS = [
  { key: 'pitch_score',       label: 'Pitch',         fmt: v => v?.toFixed(1),  invert: false },
  { key: 'strikeout_score',   label: 'K Score',       fmt: v => v?.toFixed(1),  invert: false },
  { key: 'hr_per_9',          label: 'HR/9',          fmt: v => v?.toFixed(2),  invert: true  },
  { key: 'xwoba_against',     label: 'xwOBA',         fmt: v => v?.toFixed(3),  invert: true  },
  { key: 'csw_rate',          label: 'CSW%',          fmt: v => `${v?.toFixed(1)}%`, invert: false },
  { key: 'swstr_rate',        label: 'SwStr%',        fmt: v => `${v?.toFixed(1)}%`, invert: false },
  { key: 'putaway_rate',      label: 'PutAway%',      fmt: v => `${v?.toFixed(1)}%`, invert: false },
  { key: 'ball_rate',         label: 'Ball%',         fmt: v => `${v?.toFixed(1)}%`, invert: true  },
  { key: 'siera',             label: 'SIERA',         fmt: v => v?.toFixed(2),  invert: true  },
  { key: 'barrel_per_bip',    label: 'Brl/BIP%',      fmt: v => `${v?.toFixed(1)}%`, invert: true  },
];

// =============================================================================
// HELPERS / TIER CONSTANTS
// =============================================================================

const TIER_COLOR = {
  high:      { bg: 'rgba(34, 197, 94, 0.15)',  fg: '#4ade80', label: 'HIGH' },
  medium:    { bg: 'rgba(120, 113, 108, 0.2)', fg: '#d6d3d1', label: 'MED'  },
  thin:      { bg: 'rgba(234, 179, 8, 0.18)',  fg: '#facc15', label: 'THIN' },
  very_thin: { bg: 'rgba(239, 68, 68, 0.18)',  fg: '#f87171', label: 'V.THIN' },
};

const PITCHER_TIER = {
  fade:    { bg: 'rgba(34, 197, 94, 0.18)',   fg: '#4ade80', label: 'FADE',    border: '#16a34a' },
  neutral: { bg: 'rgba(120, 113, 108, 0.2)',  fg: '#d6d3d1', label: 'NEUTRAL', border: '#78716c' },
  attack:  { bg: 'rgba(239, 68, 68, 0.18)',   fg: '#f87171', label: 'ATTACK',  border: '#dc2626' },
};

function FormCell({ value, arrow }) {
  const Icon = arrow === 'up' ? TrendingUp : arrow === 'down' ? TrendingDown : Minus;
  const color = arrow === 'up' ? '#4ade80' : arrow === 'down' ? '#f87171' : '#a8a29e';
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, color }}>
      <Icon size={11} strokeWidth={2.5} />
      <span style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 11 }}>{value?.toFixed(1)}%</span>
    </span>
  );
}

function computeRanges(rows, cols) {
  const ranges = {};
  cols.forEach(c => {
    if (c.neutral) return;
    const vals = rows.map(r => r[c.key]).filter(v => v != null && !isNaN(v));
    if (!vals.length) return;
    ranges[c.key] = { min: Math.min(...vals), max: Math.max(...vals) };
  });
  return ranges;
}

function formatGameTime(utcStr) {
  if (!utcStr) return '';
  try {
    const d = new Date(utcStr);
    return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', timeZone: 'America/New_York' }) + ' ET';
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
      .then(r => { if (!r.ok) throw new Error(`API ${r.status}`); return r.json(); })
      .then(d => { if (!cancelled) { setData(d); setLoading(false); } })
      .catch(e => { if (!cancelled) { setError(e.message); setLoading(false); } });
    return () => { cancelled = true; };
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
      .then(r => { if (!r.ok) throw new Error(`API ${r.status}`); return r.json(); })
      .then(d => { if (!cancelled) { setData(d); setLoading(false); } })
      .catch(e => { if (!cancelled) { setError(e.message); setLoading(false); } });
    return () => { cancelled = true; };
  }, [gamePk]);

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
        padding: '8px 10px',
        textAlign: 'left',
        fontSize: 10,
        fontWeight: 600,
        letterSpacing: '0.08em',
        textTransform: 'uppercase',
        cursor: 'pointer',
        userSelect: 'none',
        color: active ? '#fafaf9' : '#a8a29e',
        background: active ? 'rgba(251, 146, 60, 0.08)' : '#1c1917',
        borderBottom: active ? '2px solid #fb923c' : '1px solid #292524',
        whiteSpace: 'nowrap',
        position: 'sticky',
        top: 0,
        zIndex: 2,
      }}
    >
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
        {col.label}
        {active
          ? (sortDir === 'desc' ? <ArrowDown size={11} strokeWidth={2.5} /> : <ArrowUp size={11} strokeWidth={2.5} />)
          : <ArrowUpDown size={10} strokeWidth={1.8} style={{ opacity: 0.4 }} />}
      </span>
    </th>
  );
}

// =============================================================================
// HITTER TABLE
// =============================================================================

function HitterTable({ rows, defaultSort = 'khr' }) {
  const [sortKey, setSortKey] = useState(defaultSort);
  const [sortDir, setSortDir] = useState('desc');

  const sorted = useMemo(() => {
    const arr = [...rows];
    arr.sort((a, b) => {
      const av = a[sortKey] ?? -Infinity;
      const bv = b[sortKey] ?? -Infinity;
      const cmp = (av < bv) ? -1 : (av > bv) ? 1 : 0;
      return sortDir === 'desc' ? -cmp : cmp;
    });
    return arr;
  }, [rows, sortKey, sortDir]);

  const ranges = useMemo(() => computeRanges(sorted, HITTER_COLS), [sorted]);

  function onSort(key) {
    if (key === sortKey) setSortDir(d => d === 'desc' ? 'asc' : 'desc');
    else { setSortKey(key); setSortDir('desc'); }
  }

  if (!rows.length) {
    return (
      <div style={{ padding: 24, color: '#a8a29e', fontSize: 13, fontStyle: 'italic', textAlign: 'center' }}>
        No batter data — lineups not yet posted.
      </div>
    );
  }

  return (
    <div style={{ overflowX: 'auto', maxHeight: '70vh', overflowY: 'auto', border: '1px solid #292524', borderRadius: 4 }}>
      <table style={{ borderCollapse: 'collapse', width: '100%', fontFamily: 'Inter, sans-serif', fontSize: 11 }}>
        <thead>
          <tr>
            <th style={{ padding: '8px 10px', textAlign: 'left', fontSize: 10, fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', color: '#a8a29e', borderBottom: '1px solid #292524', position: 'sticky', left: 0, top: 0, background: '#1c1917', zIndex: 3, minWidth: 220 }}>
              Batter
            </th>
            {HITTER_COLS.map(c => (
              <SortHeader key={c.key} col={c} sortKey={sortKey} sortDir={sortDir} onClick={onSort} />
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((row, idx) => {
            const tier = TIER_COLOR[row.sample_tier] || TIER_COLOR.medium;
            return (
              <tr key={row.id} style={{ borderBottom: '1px solid rgba(41,37,36,0.6)' }}>
                <td style={{
                  padding: '7px 10px',
                  position: 'sticky',
                  left: 0,
                  background: idx % 2 ? '#171413' : '#0c0a09',
                  zIndex: 1,
                  borderRight: '1px solid #292524',
                  whiteSpace: 'nowrap',
                }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span style={{
                      fontSize: 9,
                      padding: '1px 5px',
                      borderRadius: 2,
                      background: tier.bg,
                      color: tier.fg,
                      fontWeight: 700,
                      letterSpacing: '0.05em',
                      minWidth: 36,
                      textAlign: 'center',
                    }}>{tier.label}</span>
                    <span style={{ color: '#fafaf9', fontWeight: 500, fontSize: 12 }}>{row.name}</span>
                    <span style={{ color: '#78716c', fontSize: 10, fontFamily: 'JetBrains Mono, monospace' }}>
                      {row.team} · #{row.batting_order} · {row.bats}
                    </span>
                  </div>
                </td>
                {HITTER_COLS.map(c => {
                  let display, bg;
                  if (c.fmt === 'form') {
                    display = <FormCell value={row.hr_form_pct} arrow={row.hr_form_arrow} />;
                    bg = 'transparent';
                  } else {
                    const v = row[c.key];
                    display = v == null ? '–' : c.fmt(v);
                    if (c.neutral || ranges[c.key] == null) bg = 'transparent';
                    else bg = heatColor(v, ranges[c.key].min, ranges[c.key].max, c.invert);
                  }
                  return (
                    <td key={c.key} style={{
                      padding: '6px 10px',
                      fontFamily: 'JetBrains Mono, monospace',
                      fontSize: 11,
                      whiteSpace: 'nowrap',
                      background: bg,
                      color: c.fmt === 'form' ? undefined : (bg === 'transparent' ? '#d6d3d1' : textOnColor(bg)),
                    }}>{display}</td>
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
// PITCHER TABLE
// =============================================================================

function PitcherTable({ rows, defaultSort = 'pitch_score', defaultDir = 'desc' }) {
  const [sortKey, setSortKey] = useState(defaultSort);
  const [sortDir, setSortDir] = useState(defaultDir);

  const sorted = useMemo(() => {
    const arr = [...rows];
    arr.sort((a, b) => {
      const av = a[sortKey] ?? -Infinity;
      const bv = b[sortKey] ?? -Infinity;
      const cmp = (av < bv) ? -1 : (av > bv) ? 1 : 0;
      return sortDir === 'desc' ? -cmp : cmp;
    });
    return arr;
  }, [rows, sortKey, sortDir]);

  const ranges = useMemo(() => computeRanges(sorted, PITCHER_COLS), [sorted]);

  function onSort(key) {
    if (key === sortKey) setSortDir(d => d === 'desc' ? 'asc' : 'desc');
    else { setSortKey(key); setSortDir('desc'); }
  }

  if (!rows.length) {
    return (
      <div style={{ padding: 24, color: '#a8a29e', fontSize: 13, fontStyle: 'italic', textAlign: 'center' }}>
        No pitcher data — probables not yet announced.
      </div>
    );
  }

  return (
    <div style={{ overflowX: 'auto', maxHeight: '75vh', overflowY: 'auto', border: '1px solid #292524', borderRadius: 4 }}>
      <table style={{ borderCollapse: 'collapse', width: '100%', fontFamily: 'Inter, sans-serif', fontSize: 11 }}>
        <thead>
          <tr>
            <th style={{ padding: '8px 10px', textAlign: 'left', fontSize: 10, fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', color: '#a8a29e', borderBottom: '1px solid #292524', position: 'sticky', left: 0, top: 0, background: '#1c1917', zIndex: 3, minWidth: 240 }}>
              Pitcher
            </th>
            <th style={{ padding: '8px 10px', textAlign: 'left', fontSize: 10, fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', color: '#a8a29e', borderBottom: '1px solid #292524', position: 'sticky', top: 0, background: '#1c1917', zIndex: 2 }}>
              Tier
            </th>
            {PITCHER_COLS.map(c => (
              <SortHeader key={c.key} col={c} sortKey={sortKey} sortDir={sortDir} onClick={onSort} />
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((row, idx) => {
            const tier = PITCHER_TIER[row.tier] || PITCHER_TIER.neutral;
            return (
              <tr key={row.id} style={{ borderBottom: '1px solid rgba(41,37,36,0.6)' }}>
                <td style={{
                  padding: '7px 10px',
                  position: 'sticky',
                  left: 0,
                  background: idx % 2 ? '#171413' : '#0c0a09',
                  zIndex: 1,
                  borderRight: '1px solid #292524',
                  whiteSpace: 'nowrap',
                }}>
                  <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
                    <span style={{ color: '#fafaf9', fontWeight: 500, fontSize: 12 }}>{row.name}</span>
                    <span style={{ color: '#78716c', fontSize: 10, fontFamily: 'JetBrains Mono, monospace' }}>
                      {row.throws}HP · {row.away_team}@{row.home_team}
                    </span>
                  </div>
                </td>
                <td style={{ padding: '7px 10px', whiteSpace: 'nowrap' }}>
                  <span style={{
                    fontSize: 10,
                    padding: '2px 8px',
                    borderRadius: 3,
                    background: tier.bg,
                    color: tier.fg,
                    fontWeight: 700,
                    letterSpacing: '0.08em',
                    border: `1px solid ${tier.border}`,
                  }}>{tier.label}</span>
                </td>
                {PITCHER_COLS.map(c => {
                  const v = row[c.key];
                  const display = v == null ? '–' : c.fmt(v);
                  const bg = ranges[c.key] == null ? 'transparent' : heatColor(v, ranges[c.key].min, ranges[c.key].max, c.invert);
                  return (
                    <td key={c.key} style={{
                      padding: '6px 10px',
                      fontFamily: 'JetBrains Mono, monospace',
                      fontSize: 11,
                      whiteSpace: 'nowrap',
                      background: bg,
                      color: bg === 'transparent' ? '#d6d3d1' : textOnColor(bg),
                    }}>{display}</td>
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
  const ourPitchers = topPitchers.filter(p =>
    (p.home_team === game.home_team && p.away_team === game.away_team) ||
    (p.away_team === game.home_team && p.home_team === game.away_team)
  );
  const gameHitters = topHitters
    .filter(h => h.game_pk === game.game_pk)
    .sort((a, b) => (b.khr ?? 0) - (a.khr ?? 0))
    .slice(0, 3);
  const wx = game.weather || {};

  return (
    <div
      onClick={onClick}
      style={{
        background: 'linear-gradient(180deg, #1c1917 0%, #0c0a09 100%)',
        border: '1px solid #292524',
        borderRadius: 6,
        padding: 18,
        cursor: 'pointer',
        transition: 'all 0.15s',
      }}
      onMouseEnter={e => {
        e.currentTarget.style.borderColor = '#fb923c';
        e.currentTarget.style.transform = 'translateY(-1px)';
      }}
      onMouseLeave={e => {
        e.currentTarget.style.borderColor = '#292524';
        e.currentTarget.style.transform = 'translateY(0)';
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 10 }}>
        <div style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 13, color: '#fb923c', fontWeight: 600, letterSpacing: '0.05em' }}>
          {game.away_team} @ {game.home_team}
        </div>
        <div style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 10, color: '#a8a29e' }}>
          {formatGameTime(game.game_time_utc)}
        </div>
      </div>

      <div style={{ fontSize: 11, color: '#78716c', marginBottom: 12, fontFamily: 'JetBrains Mono, monospace' }}>
        {game.park_name}
      </div>

      {ourPitchers.length > 0 && (
        <div style={{ marginBottom: 12, paddingBottom: 12, borderBottom: '1px solid #292524' }}>
          {ourPitchers.map(p => {
            const tier = PITCHER_TIER[p.tier] || PITCHER_TIER.neutral;
            return (
              <div key={p.id} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
                <span style={{ fontSize: 11, color: '#d6d3d1' }}>{p.name}</span>
                <span style={{
                  fontSize: 9,
                  padding: '1px 6px',
                  borderRadius: 2,
                  background: tier.bg,
                  color: tier.fg,
                  fontWeight: 700,
                  letterSpacing: '0.08em',
                }}>{tier.label}</span>
              </div>
            );
          })}
        </div>
      )}

      {gameHitters.length > 0 && (
        <div style={{ marginBottom: 10 }}>
          <div style={{ fontSize: 9, color: '#78716c', textTransform: 'uppercase', letterSpacing: '0.1em', marginBottom: 4 }}>Top kHR</div>
          {gameHitters.map(h => (
            <div key={h.id} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', fontSize: 11, marginBottom: 2 }}>
              <span style={{ color: '#e7e5e4' }}>{h.name}</span>
              <span style={{ fontFamily: 'JetBrains Mono, monospace', color: '#fb923c', fontWeight: 600 }}>{h.khr?.toFixed(1)}</span>
            </div>
          ))}
        </div>
      )}

      {(wx.temp != null || wx.wind != null) && (
        <div style={{ display: 'flex', gap: 12, fontSize: 10, color: '#78716c', fontFamily: 'JetBrains Mono, monospace', marginTop: 8, flexWrap: 'wrap' }}>
          {wx.temp != null && <span><Sun size={10} style={{ display: 'inline', verticalAlign: 'middle' }} /> {wx.temp}°F</span>}
          {wx.wind != null && <span><Wind size={10} style={{ display: 'inline', verticalAlign: 'middle' }} /> {wx.wind} mph {wx.wind_dir}</span>}
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
  if (ps >= 65 && hr9 <= 1.2) return 'fade';
  if (ps <= 45 || hr9 >= 1.6) return 'attack';
  return 'neutral';
}

function GameDetail({ gamePk, onBack }) {
  const { data, error, loading } = useGameDetail(gamePk);

  if (loading) return <CenteredLoader text="Loading game..." />;
  if (error) return <ErrorPanel error={error} />;
  if (!data) return null;

  const allHitters = [...(data.away_hitters || []), ...(data.home_hitters || [])];
  const pitcherRows = [
    data.away_pitcher && { ...data.away_pitcher, id: `away-${data.away_pitcher.name}`, home_team: data.home_team, away_team: data.away_team, venue: data.venue, tier: deriveTier(data.away_pitcher) },
    data.home_pitcher && { ...data.home_pitcher, id: `home-${data.home_pitcher.name}`, home_team: data.home_team, away_team: data.away_team, venue: data.venue, tier: deriveTier(data.home_pitcher) },
  ].filter(Boolean);

  return (
    <div>
      <button
        onClick={onBack}
        style={{
          background: 'transparent',
          border: '1px solid #292524',
          color: '#a8a29e',
          fontSize: 11,
          padding: '6px 12px',
          borderRadius: 4,
          cursor: 'pointer',
          fontFamily: 'JetBrains Mono, monospace',
          letterSpacing: '0.05em',
          display: 'inline-flex',
          alignItems: 'center',
          gap: 6,
          marginBottom: 16,
        }}
        onMouseEnter={e => e.currentTarget.style.borderColor = '#fb923c'}
        onMouseLeave={e => e.currentTarget.style.borderColor = '#292524'}
      >
        <ChevronLeft size={12} /> ALL GAMES
      </button>

      <div style={{ marginBottom: 20 }}>
        <h2 style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 22, color: '#fafaf9', fontWeight: 700, letterSpacing: '-0.02em', margin: '0 0 4px 0' }}>
          {data.away_team} @ {data.home_team}
        </h2>
        <div style={{ fontSize: 12, color: '#a8a29e', fontFamily: 'JetBrains Mono, monospace' }}>
          {data.park_name} · {formatGameTime(data.game_time_utc)}
          {data.weather && (data.weather.temp != null || data.weather.wind != null) && (
            <span style={{ marginLeft: 14 }}>
              {data.weather.temp != null && `${data.weather.temp}°F`}
              {data.weather.wind != null && ` · ${data.weather.wind} mph ${data.weather.wind_dir || ''}`}
            </span>
          )}
        </div>
      </div>

      <SectionHeader>Pitchers</SectionHeader>
      <PitcherTable rows={pitcherRows} defaultSort="pitch_score" defaultDir="desc" />

      <div style={{ height: 24 }} />

      <SectionHeader>Batters · sorted by kHR · click any header to re-sort</SectionHeader>
      <HitterTable rows={allHitters} defaultSort="khr" />
    </div>
  );
}

// =============================================================================
// SLATE BROWSER (landing)
// =============================================================================

function SlateBrowser({ slate, onSelectGame }) {
  if (!slate.games?.length) {
    return (
      <div style={{ padding: 40, textAlign: 'center', color: '#a8a29e', fontSize: 13 }}>
        No games scheduled today, or data not yet refreshed. Run the cron.
      </div>
    );
  }

  return (
    <div>
      <SectionHeader>{slate.games.length} games · {slate.date}</SectionHeader>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: 14 }}>
        {slate.games.map(g => (
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
      <div style={{ padding: 40, textAlign: 'center', color: '#a8a29e', fontSize: 13 }}>
        No pitcher data yet. Refresh after probables are announced.
      </div>
    );
  }

  return (
    <div>
      <SectionHeader>All Pitchers · FADE-first (high pitch score = avoid)</SectionHeader>
      <div style={{ fontSize: 11, color: '#78716c', marginBottom: 12, lineHeight: 1.6 }}>
        FADE pitchers are dealing — don't bet HR props against their lineup. ATTACK pitchers are getting hit hard — target their opposing hitters.
        Click any column header to re-sort.
      </div>
      <PitcherTable rows={rows} defaultSort="pitch_score" defaultDir="desc" />
    </div>
  );
}

// =============================================================================
// COMMON UI
// =============================================================================

function SectionHeader({ children }) {
  return (
    <div style={{
      fontFamily: 'JetBrains Mono, monospace',
      fontSize: 11,
      color: '#a8a29e',
      textTransform: 'uppercase',
      letterSpacing: '0.12em',
      marginBottom: 12,
      paddingBottom: 6,
      borderBottom: '1px solid #292524',
    }}>{children}</div>
  );
}

function CenteredLoader({ text }) {
  return (
    <div style={{ padding: 60, textAlign: 'center', color: '#a8a29e' }}>
      <Loader2 size={20} style={{ animation: 'spin 1s linear infinite', marginBottom: 8 }} />
      <div style={{ fontSize: 12, fontFamily: 'JetBrains Mono, monospace' }}>{text}</div>
      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  );
}

function ErrorPanel({ error }) {
  return (
    <div style={{
      padding: 20,
      border: '1px solid #7f1d1d',
      borderRadius: 4,
      background: 'rgba(127, 29, 29, 0.15)',
      color: '#fca5a5',
      display: 'flex',
      gap: 10,
      alignItems: 'flex-start',
    }}>
      <AlertCircle size={16} style={{ flexShrink: 0, marginTop: 2 }} />
      <div>
        <div style={{ fontWeight: 600, marginBottom: 4 }}>API error</div>
        <div style={{ fontSize: 12, fontFamily: 'JetBrains Mono, monospace' }}>{error}</div>
        <div style={{ fontSize: 11, color: '#a8a29e', marginTop: 8 }}>
          Make sure backend is running: <code>uvicorn app.main:app --reload</code>
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
  const [view, setView] = useState('games');
  const [selectedGame, setSelectedGame] = useState(null);

  return (
    <div style={{
      minHeight: '100vh',
      background: '#0c0a09',
      color: '#e7e5e4',
      fontFamily: 'Inter, system-ui, sans-serif',
      padding: '20px 28px 60px',
    }}>
      <header style={{ marginBottom: 24, display: 'flex', justifyContent: 'space-between', alignItems: 'flex-end', flexWrap: 'wrap', gap: 16 }}>
        <div>
          <div style={{
            fontFamily: 'JetBrains Mono, monospace',
            fontSize: 11,
            color: '#fb923c',
            letterSpacing: '0.2em',
            marginBottom: 4,
          }}>▌ PARKBLAST</div>
          <h1 style={{
            margin: 0,
            fontSize: 28,
            fontWeight: 800,
            letterSpacing: '-0.03em',
            color: '#fafaf9',
          }}>Daily HR &amp; K Intelligence</h1>
        </div>
        <nav style={{ display: 'flex', gap: 0, border: '1px solid #292524', borderRadius: 4, overflow: 'hidden' }}>
          {[
            { key: 'games',    label: 'GAMES' },
            { key: 'pitchers', label: 'PITCHERS' },
          ].map(t => (
            <button
              key={t.key}
              onClick={() => { setView(t.key); setSelectedGame(null); }}
              style={{
                padding: '8px 16px',
                fontFamily: 'JetBrains Mono, monospace',
                fontSize: 11,
                fontWeight: 600,
                letterSpacing: '0.1em',
                background: view === t.key ? '#fb923c' : 'transparent',
                color: view === t.key ? '#0c0a09' : '#a8a29e',
                border: 'none',
                cursor: 'pointer',
                transition: 'all 0.1s',
              }}
            >{t.label}</button>
          ))}
        </nav>
      </header>

      {loading && <CenteredLoader text="Loading slate..." />}
      {error && <ErrorPanel error={error} />}
      {slate && !error && (
        <>
          {view === 'games' && !selectedGame && (
            <SlateBrowser slate={slate} onSelectGame={setSelectedGame} />
          )}
          {view === 'games' && selectedGame && (
            <GameDetail gamePk={selectedGame} onBack={() => setSelectedGame(null)} />
          )}
          {view === 'pitchers' && (
            <PitcherLeaderboard slate={slate} />
          )}
        </>
      )}

      <footer style={{
        marginTop: 60,
        paddingTop: 20,
        borderTop: '1px solid #1c1917',
        fontSize: 10,
        color: '#57534e',
        fontFamily: 'JetBrains Mono, monospace',
        letterSpacing: '0.05em',
      }}>
        PARKBLAST · v0.2 · for entertainment purposes only · 21+ · cross-reference with PropFinder
      </footer>
    </div>
  );
}
