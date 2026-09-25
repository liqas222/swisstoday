"""Ablage für den Memecoin-Scanner, in derselben SQLite-Datei wie SwissIntel.

Momentaufnahmen, Risiko-Einschätzungen und Bewertungen werden nur angehängt,
nie überschrieben. Wir müssen wissen, was das System zu dem Zeitpunkt geglaubt
hat, als es das glaubte — sonst lässt sich ein Signal später nie überprüfen.

Tote Token werden ebenfalls nie gelöscht. Ein Datensatz aus lauter Überlebenden
würde jede spätere Statistik in dieselbe optimistische Richtung verzerren.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from typing import Any, Optional

logger = logging.getLogger(__name__)

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS scan_tokens (
    id               TEXT PRIMARY KEY,          -- "solana:<contract>"
    chain            TEXT NOT NULL DEFAULT 'solana',
    contract         TEXT NOT NULL,
    symbol           TEXT,
    name             TEXT,
    pair_address     TEXT,
    dex              TEXT,
    pair_created_at  TEXT,
    discovered_at    TEXT NOT NULL DEFAULT (datetime('now')),
    discovery_source TEXT NOT NULL,
    image_url        TEXT,
    website          TEXT,
    twitter          TEXT,
    telegram         TEXT
);
CREATE INDEX IF NOT EXISTS scan_tokens_disc_idx ON scan_tokens(discovered_at DESC);
CREATE INDEX IF NOT EXISTS scan_tokens_contract_idx ON scan_tokens(contract);

CREATE TABLE IF NOT EXISTS scan_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id            TEXT NOT NULL,
    captured_at         TEXT NOT NULL DEFAULT (datetime('now')),
    source              TEXT NOT NULL,
    price_usd           REAL, market_cap REAL, fdv REAL, liquidity_usd REAL,
    volume_m5           REAL, volume_h1 REAL, volume_h6 REAL, volume_h24 REAL,
    change_m5_pct       REAL, change_h1_pct REAL, change_h6_pct REAL, change_h24_pct REAL,
    buys_h1             REAL, sells_h1 REAL, buys_h24 REAL, sells_h24 REAL,
    txns_h1             REAL, txns_h24 REAL, pair_age_hours REAL,
    volume_to_liquidity REAL, liquidity_to_mcap REAL, buy_ratio_h1 REAL
);
CREATE INDEX IF NOT EXISTS scan_snap_idx ON scan_snapshots(token_id, captured_at DESC);

CREATE TABLE IF NOT EXISTS scan_risk (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id           TEXT NOT NULL,
    assessed_at        TEXT NOT NULL DEFAULT (datetime('now')),
    risk_score         INTEGER NOT NULL,
    flags              TEXT NOT NULL DEFAULT '[]',
    unavailable_checks TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS scan_risk_idx ON scan_risk(token_id, assessed_at DESC);

CREATE TABLE IF NOT EXISTS scan_scores (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id        TEXT NOT NULL,
    scored_at       TEXT NOT NULL DEFAULT (datetime('now')),
    overall_score   INTEGER NOT NULL,
    components      TEXT NOT NULL,
    missing_inputs  TEXT NOT NULL DEFAULT '[]',
    weights_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS scan_scores_idx ON scan_scores(token_id, scored_at DESC);
CREATE INDEX IF NOT EXISTS scan_scores_overall_idx ON scan_scores(overall_score DESC);

CREATE TABLE IF NOT EXISTS scan_runs (
    id                  TEXT PRIMARY KEY,
    ran_at              TEXT NOT NULL DEFAULT (datetime('now')),
    duration_ms         INTEGER NOT NULL,
    discovered          INTEGER NOT NULL DEFAULT 0,
    rejected_by_filters INTEGER NOT NULL DEFAULT 0,
    enriched            INTEGER NOT NULL DEFAULT 0,
    scored              INTEGER NOT NULL DEFAULT 0,
    opportunities       INTEGER NOT NULL DEFAULT 0,
    errors              TEXT NOT NULL DEFAULT '[]',
    rejections          TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS scan_runs_idx ON scan_runs(ran_at DESC);
"""

_initialised: set[str] = set()


@contextmanager
def _connect(db_path: str):
    # Bot und Dashboard schreiben beide in dieselbe Datei; WAL plus Wartezeit
    # verhindert "database is locked".
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str) -> None:
    if db_path in _initialised:
        return
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)
    _initialised.add(db_path)


# ── Schreiben ──────────────────────────────────────────────────────────────

def upsert_token(db_path: str, c: Any) -> None:
    """Token anlegen oder auffrischen. ``discovered_at`` bleibt erhalten —
    wann wir ein Token zuerst gesehen haben, ist selbst eine Information."""
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT INTO scan_tokens
                 (id, chain, contract, symbol, name, pair_address, dex,
                  pair_created_at, discovery_source, image_url, website, twitter, telegram)
               VALUES (?, 'solana', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 symbol=excluded.symbol, name=excluded.name,
                 pair_address=excluded.pair_address, dex=excluded.dex,
                 image_url=excluded.image_url, website=excluded.website,
                 twitter=excluded.twitter, telegram=excluded.telegram""",
            (c.token_id, c.contract, c.symbol, c.name, c.pair_address, c.dex,
             c.pair_created_at, c.discovery_source, c.image_url, c.website,
             c.twitter, c.telegram))


_SNAP_COLS = [
    "price_usd", "market_cap", "fdv", "liquidity_usd", "volume_m5", "volume_h1",
    "volume_h6", "volume_h24", "change_m5_pct", "change_h1_pct", "change_h6_pct",
    "change_h24_pct", "buys_h1", "sells_h1", "buys_h24", "sells_h24", "txns_h1",
    "txns_h24", "pair_age_hours", "volume_to_liquidity", "liquidity_to_mcap",
    "buy_ratio_h1",
]


def save_snapshot(db_path: str, c: Any) -> None:
    init_db(db_path)
    cols = ", ".join(_SNAP_COLS)
    holes = ", ".join("?" * len(_SNAP_COLS))
    with _connect(db_path) as conn:
        conn.execute(
            f"INSERT INTO scan_snapshots (token_id, source, {cols}) VALUES (?, ?, {holes})",
            (c.token_id, c.market.get("source", "dexscreener"),
             *[c.market.get(k) for k in _SNAP_COLS]))


def save_risk(db_path: str, token_id: str, risk: dict) -> None:
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO scan_risk (token_id, risk_score, flags, unavailable_checks) "
            "VALUES (?, ?, ?, ?)",
            (token_id, risk["risk_score"], json.dumps(risk["flags"], ensure_ascii=False),
             json.dumps(risk["unavailable_checks"])))


def save_score(db_path: str, token_id: str, score: dict) -> None:
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO scan_scores (token_id, overall_score, components, "
            "missing_inputs, weights_version) VALUES (?, ?, ?, ?, ?)",
            (token_id, score["overall_score"], json.dumps(score["components"]),
             json.dumps(score["missing_inputs"]), score["weights_version"]))


def save_run(db_path: str, report: dict) -> None:
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO scan_runs
                 (id, duration_ms, discovered, rejected_by_filters, enriched,
                  scored, opportunities, errors, rejections)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (report["id"], report["duration_ms"], report["discovered"],
             report["rejected_by_filters"], report["enriched"], report["scored"],
             report["opportunities"], json.dumps(report["errors"], ensure_ascii=False),
             json.dumps(report["rejections"], ensure_ascii=False)))


# ── Lesen ──────────────────────────────────────────────────────────────────

# Neuester Stand je Token. Die Unterabfragen holen aus den reinen
# Anhänge-Tabellen jeweils die jüngste Zeile.
_LATEST_SQL = """
SELECT t.*,
       s.captured_at, s.price_usd, s.market_cap, s.fdv, s.liquidity_usd,
       s.volume_h1, s.volume_h24, s.change_h1_pct, s.change_h6_pct, s.change_h24_pct,
       s.buys_h1, s.sells_h1, s.txns_h24, s.pair_age_hours,
       s.volume_to_liquidity, s.liquidity_to_mcap, s.buy_ratio_h1,
       r.risk_score, r.flags, r.unavailable_checks, r.assessed_at,
       sc.overall_score, sc.components, sc.missing_inputs, sc.weights_version, sc.scored_at
FROM scan_tokens t
LEFT JOIN scan_snapshots s ON s.id = (
    SELECT id FROM scan_snapshots WHERE token_id = t.id ORDER BY id DESC LIMIT 1)
LEFT JOIN scan_risk r ON r.id = (
    SELECT id FROM scan_risk WHERE token_id = t.id ORDER BY id DESC LIMIT 1)
LEFT JOIN scan_scores sc ON sc.id = (
    SELECT id FROM scan_scores WHERE token_id = t.id ORDER BY id DESC LIMIT 1)
"""


def _view(r: sqlite3.Row) -> dict:
    d = dict(r)
    for key in ("flags", "unavailable_checks", "components", "missing_inputs"):
        if d.get(key):
            try:
                d[key] = json.loads(d[key])
            except (TypeError, ValueError):
                d[key] = [] if key != "components" else {}
        else:
            d[key] = {} if key == "components" else []
    return d


def list_tokens(db_path: str, min_score: int = 0, limit: int = 200) -> list[dict]:
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            _LATEST_SQL +
            " WHERE COALESCE(sc.overall_score, -1) >= ?"
            " ORDER BY COALESCE(sc.overall_score, 0) DESC LIMIT ?",
            (min_score, limit)).fetchall()
    return [_view(r) for r in rows]


def get_token(db_path: str, token_id: str) -> Optional[dict]:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(_LATEST_SQL + " WHERE t.id = ?", (token_id,)).fetchone()
    return _view(row) if row else None


def score_history(db_path: str, token_id: str, limit: int = 100) -> list[dict]:
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT scored_at, overall_score, components, weights_version "
            "FROM scan_scores WHERE token_id = ? ORDER BY id DESC LIMIT ?",
            (token_id, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["components"] = json.loads(d["components"])
        except (TypeError, ValueError):
            d["components"] = {}
        out.append(d)
    return out


def price_history(db_path: str, token_id: str, limit: int = 120) -> list[dict]:
    """Die Kursreihe, seit wir das Token beobachten — der eigentliche Zweck
    der Momentaufnahmen."""
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT captured_at, price_usd, market_cap, liquidity_usd "
            "FROM scan_snapshots WHERE token_id = ? ORDER BY id DESC LIMIT ?",
            (token_id, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def latest_run(db_path: str) -> Optional[dict]:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM scan_runs ORDER BY ran_at DESC, rowid DESC "
                           "LIMIT 1").fetchone()
    if not row:
        return None
    d = dict(row)
    for key in ("errors", "rejections"):
        try:
            d[key] = json.loads(d[key])
        except (TypeError, ValueError):
            d[key] = []
    return d


def count_tokens_since(db_path: str, hours: int = 24) -> int:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM scan_tokens "
            "WHERE discovered_at > datetime('now', ? || ' hours')", (f"-{hours}",)).fetchone()
    return row["n"] if row else 0
