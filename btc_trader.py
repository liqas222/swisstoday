"""BTC-Dip-Handel mit Hebel — mehrere Strategien parallel, auf Papier.

Die Idee: fällt BTC innerhalb von X Minuten um Y Prozent, wird long gegangen.
Jede Strategie hat ihr eigenes Startkapital von 1000 $ und handelt unabhängig,
damit am Ende nicht eine Meinung, sondern eine Tabelle dasteht.

Warum der Hebel variiert
------------------------
Bei 100x liquidiert ein Gegenlauf von 1 Prozent die gesamte Position: die
Sicherheitsleistung beträgt genau 1/100 des Gegenwerts. Ein Einstieg nach
−1 % wird also von den nächsten −1 % vollständig ausgelöscht — und genau darauf
folgen Dips oft. Deshalb läuft dieselbe Auslösung mit 100x, 50x, 25x, 10x und
5x nebeneinander. Die Frage, ob sich das lohnt, beantworten die Zahlen.

Wie gerechnet wird
------------------
Der Motor läuft über Minutenkerzen, nicht über den Momentankurs. Das ist der
Unterschied zwischen Spielzeug und etwas Belastbarem: zwischen zwei Abfragen
im Fünf-Minuten-Takt kann eine Position längst liquidiert worden sein. Jede
Kerze wird einzeln abgearbeitet, und innerhalb einer Kerze wird immer zuerst
das Ungünstige geprüft — Liquidation vor Stop vor Ziel. Aus Hoch und Tief
allein lässt sich die Reihenfolge nicht rekonstruieren, also wird sie zu
unseren Ungunsten angenommen statt zu unseren Gunsten.

Dieselbe Mechanik verarbeitet Vergangenheit und Gegenwart. Beim ersten Start
werden einige Tage Historie nachgeholt, damit sofort Ergebnisse vorliegen
statt in drei Wochen.

Nicht modelliert: Funding-Gebühren (bei Haltedauern von Minuten bis Stunden
klein, aber nicht null), Slippage und Teilausführungen. Handelsgebühren sind
enthalten. Es wird nichts gehandelt, es fliesst kein Geld.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

SYMBOL = "BTCUSDT"
START_EQUITY = 1000.0

# Taker-Gebühr je Seite, an gängigen Futures-Börsen. Wird auf den vollen
# Gegenwert gerechnet, nicht auf die Sicherheitsleistung — bei 100x ist das
# der Unterschied zwischen 0,04 % und 4 % des Einsatzes.
TAKER_FEE = 0.0004

# Wartungsmarge: liquidiert wird kurz bevor die Sicherheitsleistung ganz weg
# ist, nicht erst danach.
MAINTENANCE_MARGIN = 0.005

BACKFILL_MINUTES = int(os.getenv("BTC_BACKFILL_MINUTES", "7200"))  # 5 Tage
MAX_CANDLES_PER_RUN = 3000


# ── Strategien ─────────────────────────────────────────────────────────────
# drop_pct / window_min: Auslösung, wenn der Kurs innerhalb des Fensters um
#   mindestens so viel unter seinem Höchststand liegt.
# margin_pct: Anteil des Kapitals, der als Sicherheitsleistung eingesetzt wird.
# sl_pct / tp_pct: Kursbewegung, nicht Ergebnis auf den Einsatz. Bei 100x sind
#   −0,35 % Kurs bereits −35 % auf den Einsatz.

STRATEGIES: list[dict[str, Any]] = [
    {
        "key": "d1_100x", "name": "Dip 1% · 100x",
        "note": "Der Wunschfall. Liquidation bereits bei −1 % ab Einstieg.",
        "drop_pct": 1.0, "window_min": 15, "leverage": 100,
        "margin_pct": 0.10, "sl_pct": 0.35, "tp_pct": 0.50,
        "max_hold_min": 60, "cooldown_min": 15,
    },
    {
        "key": "d1_50x", "name": "Dip 1% · 50x",
        "note": "Gleiche Auslösung, halber Hebel. Liquidation bei −2 %.",
        "drop_pct": 1.0, "window_min": 15, "leverage": 50,
        "margin_pct": 0.10, "sl_pct": 0.70, "tp_pct": 1.00,
        "max_hold_min": 120, "cooldown_min": 15,
    },
    {
        "key": "d1_25x", "name": "Dip 1% · 25x",
        "note": "Liquidation erst bei −4 %, der Stop greift lange vorher.",
        "drop_pct": 1.0, "window_min": 15, "leverage": 25,
        "margin_pct": 0.15, "sl_pct": 1.20, "tp_pct": 1.80,
        "max_hold_min": 240, "cooldown_min": 30,
    },
    {
        "key": "d1_10x", "name": "Dip 1% (60min) · 10x",
        "note": "Langsamerer Rückgang, moderater Hebel.",
        "drop_pct": 1.0, "window_min": 60, "leverage": 10,
        "margin_pct": 0.25, "sl_pct": 2.00, "tp_pct": 3.00,
        "max_hold_min": 480, "cooldown_min": 60,
    },
    {
        "key": "d2_50x", "name": "Dip 2% · 50x",
        "note": "Tieferer Rückgang, hoher Hebel.",
        "drop_pct": 2.0, "window_min": 30, "leverage": 50,
        "margin_pct": 0.10, "sl_pct": 0.70, "tp_pct": 1.20,
        "max_hold_min": 180, "cooldown_min": 30,
    },
    {
        "key": "d2_10x", "name": "Dip 2% · 10x",
        "note": "Derselbe Einstieg, Hebel klein genug zum Aushalten.",
        "drop_pct": 2.0, "window_min": 120, "leverage": 10,
        "margin_pct": 0.25, "sl_pct": 2.50, "tp_pct": 4.00,
        "max_hold_min": 720, "cooldown_min": 60,
    },
    {
        "key": "d3_5x", "name": "Dip 3% · 5x",
        "note": "Seltener Einstieg, viel Luft nach unten.",
        "drop_pct": 3.0, "window_min": 240, "leverage": 5,
        "margin_pct": 0.30, "sl_pct": 4.00, "tp_pct": 6.00,
        "max_hold_min": 1440, "cooldown_min": 120,
    },
]

STRATEGY_BY_KEY = {s["key"]: s for s in STRATEGIES}
STRATEGY_VERSION = "btc-v1"

# Unter diesem Kapital ist die Strategie erledigt. Weiterhandeln mit 3 Dollar
# würde die Auswertung nur verwässern.
RUIN_EQUITY = 25.0


# ── Ablage ─────────────────────────────────────────────────────────────────

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS btc_candles (
    open_time INTEGER PRIMARY KEY,   -- ms
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
    close REAL NOT NULL, volume REAL
);

CREATE TABLE IF NOT EXISTS btc_state (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS btc_strategies (
    key             TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    config          TEXT NOT NULL,
    start_equity    REAL NOT NULL,
    equity          REAL NOT NULL,
    peak_equity     REAL NOT NULL,
    max_drawdown_pct REAL NOT NULL DEFAULT 0,
    ruined_at       INTEGER
);

CREATE TABLE IF NOT EXISTS btc_trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_key  TEXT NOT NULL,
    opened_at     INTEGER NOT NULL,          -- ms
    entry_price   REAL NOT NULL,
    leverage      INTEGER NOT NULL,
    margin_usd    REAL NOT NULL,
    notional_usd  REAL NOT NULL,
    qty_btc       REAL NOT NULL,
    liq_price     REAL NOT NULL,
    sl_price      REAL NOT NULL,
    tp_price      REAL NOT NULL,
    trigger_detail TEXT,
    equity_before REAL NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open',
    highest_price REAL, lowest_price REAL,
    mfe_pct       REAL DEFAULT 0, mae_pct REAL DEFAULT 0,
    closed_at     INTEGER,
    exit_price    REAL,
    exit_reason   TEXT,
    fees_usd      REAL,
    pnl_usd       REAL,
    return_on_margin_pct REAL,
    equity_after  REAL,
    duration_min  REAL
);
CREATE INDEX IF NOT EXISTS btc_trades_strat_idx ON btc_trades(strategy_key, opened_at DESC);
CREATE INDEX IF NOT EXISTS btc_trades_status_idx ON btc_trades(status, opened_at DESC);

CREATE TABLE IF NOT EXISTS btc_equity (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_key TEXT NOT NULL,
    at_ms        INTEGER NOT NULL,
    equity       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS btc_equity_idx ON btc_equity(strategy_key, at_ms);
"""

_initialised: set[str] = set()


@contextmanager
def _connect(db_path: str):
    conn = sqlite3.connect(db_path, timeout=20)
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
        for s in STRATEGIES:
            conn.execute(
                """INSERT INTO btc_strategies (key, name, config, start_equity, equity, peak_equity)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET name=excluded.name, config=excluded.config""",
                (s["key"], s["name"], json.dumps(s, ensure_ascii=False),
                 START_EQUITY, START_EQUITY, START_EQUITY))
    _initialised.add(db_path)


def _get_state(conn, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM btc_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _set_state(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO btc_state (key, value) VALUES (?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


# ── Kursquelle ─────────────────────────────────────────────────────────────
# Mehrere Anbieter, weil einer immer ausfällt. Alle liefern Minutenkerzen
# ohne Schlüssel.

def _fetch(url: str, params: Optional[dict] = None, timeout: int = 15) -> Any:
    try:
        r = requests.get(url, params=params, timeout=timeout,
                         headers={"Accept": "application/json"})
        if r.status_code != 200:
            logger.debug("Kursabruf %s -> %s", url, r.status_code)
            return None
        return r.json()
    except Exception as e:
        logger.debug("Kursabruf fehlgeschlagen %s: %s", url, e)
        return None


def _from_binance(start_ms: int, limit: int) -> list[tuple]:
    data = _fetch("https://api.binance.com/api/v3/klines",
                  {"symbol": SYMBOL, "interval": "1m",
                   "startTime": start_ms, "limit": min(limit, 1000)})
    if not isinstance(data, list):
        return []
    return [(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]))
            for k in data]


def _from_bybit(start_ms: int, limit: int) -> list[tuple]:
    data = _fetch("https://api.bybit.com/v5/market/kline",
                  {"category": "spot", "symbol": SYMBOL, "interval": "1",
                   "start": start_ms, "limit": min(limit, 1000)})
    rows = ((data or {}).get("result") or {}).get("list") or []
    out = [(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]))
           for k in rows]
    return sorted(out)  # Bybit liefert absteigend


def _from_kraken(start_ms: int, limit: int) -> list[tuple]:
    data = _fetch("https://api.kraken.com/0/public/OHLC",
                  {"pair": "XBTUSD", "interval": 1, "since": start_ms // 1000})
    result = (data or {}).get("result") or {}
    series = next((v for k, v in result.items() if k != "last"), [])
    return [(int(k[0]) * 1000, float(k[1]), float(k[2]), float(k[3]), float(k[4]),
             float(k[6])) for k in series][:limit]


PROVIDERS = [("binance", _from_binance), ("bybit", _from_bybit), ("kraken", _from_kraken)]


def fetch_candles(start_ms: int, limit: int = 1000) -> tuple[list[tuple], str]:
    """Minutenkerzen ab start_ms. Erster Anbieter, der liefert, gewinnt."""
    for name, fn in PROVIDERS:
        try:
            rows = fn(start_ms, limit)
        except Exception as e:
            logger.debug("Anbieter %s: %s", name, e)
            continue
        if rows:
            return rows, name
    return [], "keiner"


def sync_candles(db_path: str) -> dict:
    """Holt alle Kerzen seit der letzten gespeicherten. Beim ersten Mal wird
    Historie nachgeholt, damit sofort Ergebnisse vorliegen."""
    init_db(db_path)
    now_ms = int(time.time() * 1000)

    with _connect(db_path) as conn:
        row = conn.execute("SELECT MAX(open_time) AS t FROM btc_candles").fetchone()
        last = row["t"] if row and row["t"] else None

    start = (last + 60_000) if last else now_ms - BACKFILL_MINUTES * 60_000
    added, source, requests_made = 0, "keiner", 0

    while start < now_ms and added < MAX_CANDLES_PER_RUN and requests_made < 8:
        rows, source = fetch_candles(start, 1000)
        requests_made += 1
        if not rows:
            break
        with _connect(db_path) as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO btc_candles (open_time, open, high, low, close, volume) "
                "VALUES (?,?,?,?,?,?)", rows)
        added += len(rows)
        start = rows[-1][0] + 60_000
        if len(rows) < 100:
            break

    return {"added": added, "source": source, "backfilled": last is None}


# ── Auslösung ──────────────────────────────────────────────────────────────

def _drop_from_window_high(closes: list[float], window: int) -> Optional[float]:
    """Wie weit liegt der letzte Kurs unter dem Höchststand des Fensters?

    Gegen den Höchststand, nicht gegen den Kurs von genau vor X Minuten: ein
    Rückgang ist ein Rückgang, auch wenn er zwei Minuten früher begann.
    """
    if len(closes) < 2:
        return None
    window_closes = closes[-(window + 1):]
    high = max(window_closes)
    if high <= 0:
        return None
    return (window_closes[-1] / high - 1) * 100


# ── Motor ──────────────────────────────────────────────────────────────────

def _open_position(conn, strat: dict, equity: float, candle: sqlite3.Row,
                   drop: float) -> None:
    entry = candle["close"]
    lev = strat["leverage"]
    margin = equity * strat["margin_pct"]
    notional = margin * lev
    qty = notional / entry

    # Liquidation: der Kurs, bei dem die Sicherheitsleistung bis auf die
    # Wartungsmarge aufgebraucht ist.
    liq = entry * (1 - (1 - MAINTENANCE_MARGIN) / lev)
    sl = entry * (1 - strat["sl_pct"] / 100)
    tp = entry * (1 + strat["tp_pct"] / 100)

    # Ein Stop hinter der Liquidation wäre wirkungslos — dann liegt die
    # eigentliche Grenze bei der Liquidation, und das soll man sehen.
    if sl <= liq:
        sl = liq

    conn.execute(
        """INSERT INTO btc_trades
             (strategy_key, opened_at, entry_price, leverage, margin_usd, notional_usd,
              qty_btc, liq_price, sl_price, tp_price, trigger_detail, equity_before,
              highest_price, lowest_price)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (strat["key"], candle["open_time"], entry, lev, margin, notional, qty,
         liq, sl, tp, f"{drop:.2f}% unter dem Hoch der letzten {strat['window_min']} Min",
         equity, entry, entry))


def _close_position(conn, tr: sqlite3.Row, exit_price: float, reason: str,
                    at_ms: int, strat_row: sqlite3.Row) -> dict:
    qty = tr["qty_btc"]
    margin = tr["margin_usd"]
    fees = tr["notional_usd"] * TAKER_FEE + (qty * exit_price) * TAKER_FEE

    if reason == "liquidiert":
        # Bei einer Liquidation ist die Sicherheitsleistung weg, mehr aber
        # auch nicht — mehr als den Einsatz kann man nicht verlieren.
        pnl = -margin
        fees = tr["notional_usd"] * TAKER_FEE
    else:
        pnl = qty * (exit_price - tr["entry_price"]) - fees
        pnl = max(pnl, -margin)

    equity_after = strat_row["equity"] + pnl
    ret_margin = (pnl / margin) * 100 if margin else 0.0
    duration = (at_ms - tr["opened_at"]) / 60_000

    conn.execute(
        """UPDATE btc_trades SET status='closed', closed_at=?, exit_price=?, exit_reason=?,
             fees_usd=?, pnl_usd=?, return_on_margin_pct=?, equity_after=?, duration_min=?
           WHERE id=?""",
        (at_ms, exit_price, reason, fees, pnl, ret_margin, equity_after, duration, tr["id"]))

    peak = max(strat_row["peak_equity"], equity_after)
    dd = (1 - equity_after / peak) * 100 if peak > 0 else 0.0
    ruined = at_ms if equity_after < RUIN_EQUITY and not strat_row["ruined_at"] else strat_row["ruined_at"]

    conn.execute(
        "UPDATE btc_strategies SET equity=?, peak_equity=?, "
        "max_drawdown_pct=MAX(max_drawdown_pct, ?), ruined_at=? WHERE key=?",
        (equity_after, peak, dd, ruined, tr["strategy_key"]))
    conn.execute("INSERT INTO btc_equity (strategy_key, at_ms, equity) VALUES (?,?,?)",
                 (tr["strategy_key"], at_ms, equity_after))

    return {"strategy": tr["strategy_key"], "reason": reason, "pnl": pnl,
            "return_pct": ret_margin, "equity": equity_after}


def process_candles(db_path: str, limit: int = MAX_CANDLES_PER_RUN) -> dict:
    """Arbeitet alle noch nicht verarbeiteten Minutenkerzen ab.

    Dieselbe Schleife für Historie und Gegenwart — was beim Nachholen gerechnet
    wird, wird im Livebetrieb identisch gerechnet.
    """
    init_db(db_path)

    with _connect(db_path) as conn:
        processed_until = int(_get_state(conn, "processed_until") or 0)
        candles = conn.execute(
            "SELECT * FROM btc_candles WHERE open_time > ? ORDER BY open_time LIMIT ?",
            (processed_until, limit)).fetchall()
        if not candles:
            return {"processed": 0, "opened": 0, "closed": 0, "closures": []}

        # Vorlauf für die Auslöse-Fenster: das längste Fenster plus Reserve.
        longest = max(s["window_min"] for s in STRATEGIES)
        history = [r["close"] for r in conn.execute(
            "SELECT close FROM btc_candles WHERE open_time <= ? "
            "ORDER BY open_time DESC LIMIT ?", (processed_until, longest + 5)).fetchall()][::-1]

        strategies = {r["key"]: r for r in
                      conn.execute("SELECT * FROM btc_strategies").fetchall()}
        open_trades = {r["strategy_key"]: r for r in conn.execute(
            "SELECT * FROM btc_trades WHERE status='open'").fetchall()}
        last_close_at = {r["strategy_key"]: r["closed_at"] for r in conn.execute(
            "SELECT strategy_key, MAX(closed_at) AS closed_at FROM btc_trades "
            "WHERE status='closed' GROUP BY strategy_key").fetchall()}

        opened = 0
        closures: list[dict] = []

        for c in candles:
            history.append(c["close"])
            ts = c["open_time"]

            # 1) Offene Positionen gegen diese Kerze prüfen
            for key, tr in list(open_trades.items()):
                entry = tr["entry_price"]
                high = max(tr["highest_price"] or c["high"], c["high"])
                low = min(tr["lowest_price"] or c["low"], c["low"])
                mfe = (high / entry - 1) * 100
                mae = (low / entry - 1) * 100
                conn.execute(
                    "UPDATE btc_trades SET highest_price=?, lowest_price=?, mfe_pct=?, "
                    "mae_pct=? WHERE id=?", (high, low, mfe, mae, tr["id"]))

                strat_row = strategies[key]
                result = None

                # Ungünstiges zuerst: aus Hoch und Tief einer Kerze lässt sich
                # die Reihenfolge nicht ablesen, also nicht zu unseren Gunsten
                # raten.
                if c["low"] <= tr["liq_price"]:
                    result = _close_position(conn, tr, tr["liq_price"], "liquidiert", ts, strat_row)
                elif c["low"] <= tr["sl_price"]:
                    result = _close_position(conn, tr, tr["sl_price"], "Stop", ts, strat_row)
                elif c["high"] >= tr["tp_price"]:
                    result = _close_position(conn, tr, tr["tp_price"], "Ziel erreicht", ts, strat_row)
                elif (ts - tr["opened_at"]) / 60_000 >= STRATEGY_BY_KEY[key]["max_hold_min"]:
                    result = _close_position(conn, tr, c["close"], "Haltefrist abgelaufen", ts, strat_row)

                if result:
                    closures.append({**result, "at": ts})
                    del open_trades[key]
                    last_close_at[key] = ts
                    strategies[key] = conn.execute(
                        "SELECT * FROM btc_strategies WHERE key=?", (key,)).fetchone()

            # 2) Einstiege prüfen
            for strat in STRATEGIES:
                key = strat["key"]
                if key in open_trades:
                    continue
                srow = strategies[key]
                if srow["ruined_at"]:
                    continue
                if srow["equity"] < RUIN_EQUITY:
                    continue
                last = last_close_at.get(key)
                if last and (ts - last) / 60_000 < strat["cooldown_min"]:
                    continue

                drop = _drop_from_window_high(history, strat["window_min"])
                if drop is None or drop > -strat["drop_pct"]:
                    continue

                _open_position(conn, strat, srow["equity"], c, drop)
                opened += 1
                open_trades[key] = conn.execute(
                    "SELECT * FROM btc_trades WHERE strategy_key=? AND status='open'",
                    (key,)).fetchone()

            if len(history) > longest + 10:
                history = history[-(longest + 10):]

        _set_state(conn, "processed_until", candles[-1]["open_time"])

    logger.info("[BTC] %d Kerzen verarbeitet, %d eröffnet, %d geschlossen",
                len(candles), opened, len(closures))
    return {"processed": len(candles), "opened": opened,
            "closed": len(closures), "closures": closures[-20:]}


def run_cycle(db_path: Optional[str] = None) -> dict:
    """Kurse holen, dann rechnen. Der Job für den Scheduler."""
    db_path = db_path or os.getenv("DB_PATH", "swissintel.db")
    out: dict[str, Any] = {}
    try:
        out["sync"] = sync_candles(db_path)
    except Exception as e:
        logger.exception("BTC-Kursabruf fehlgeschlagen")
        out["sync"] = {"error": str(e)}
    try:
        # Nach dem ersten Nachholen liegen tausende Kerzen bereit; die werden
        # über mehrere Durchläufe abgearbeitet statt in einem langen Block.
        out["engine"] = process_candles(db_path)
    except Exception as e:
        logger.exception("BTC-Motor fehlgeschlagen")
        out["engine"] = {"error": str(e)}
    return out


# ── Auswertung ─────────────────────────────────────────────────────────────

def _median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def overview(db_path: str) -> dict:
    init_db(db_path)
    with _connect(db_path) as conn:
        strategies = conn.execute("SELECT * FROM btc_strategies ORDER BY equity DESC").fetchall()
        rows = []
        for s in strategies:
            cfg = json.loads(s["config"])
            trades = conn.execute(
                "SELECT * FROM btc_trades WHERE strategy_key=? AND status='closed'",
                (s["key"],)).fetchall()
            rets = [t["return_on_margin_pct"] or 0 for t in trades]
            pnls = [t["pnl_usd"] or 0 for t in trades]
            wins = [r for r in rets if r > 0]
            liqs = sum(1 for t in trades if t["exit_reason"] == "liquidiert")
            gross_win = sum(p for p in pnls if p > 0)
            gross_loss = abs(sum(p for p in pnls if p < 0))
            open_row = conn.execute(
                "SELECT * FROM btc_trades WHERE strategy_key=? AND status='open'",
                (s["key"],)).fetchone()

            rows.append({
                "key": s["key"], "name": s["name"], "note": cfg.get("note"),
                "leverage": cfg["leverage"], "drop_pct": cfg["drop_pct"],
                "window_min": cfg["window_min"], "sl_pct": cfg["sl_pct"],
                "tp_pct": cfg["tp_pct"], "margin_pct": cfg["margin_pct"],
                "start_equity": s["start_equity"], "equity": s["equity"],
                "total_return_pct": (s["equity"] / s["start_equity"] - 1) * 100,
                "max_drawdown_pct": s["max_drawdown_pct"],
                "ruined": bool(s["ruined_at"]),
                "trades": len(trades),
                "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else None,
                "liquidations": liqs,
                "liquidation_rate": round(liqs / len(trades) * 100, 1) if trades else None,
                "avg_return_pct": round(sum(rets) / len(rets), 1) if rets else None,
                "median_return_pct": round(_median(rets) or 0, 1) if rets else None,
                "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
                "avg_hold_min": round(sum(t["duration_min"] or 0 for t in trades) / len(trades), 1)
                                if trades else None,
                "open_trade": dict(open_row) if open_row else None,
            })

        last = conn.execute("SELECT * FROM btc_candles ORDER BY open_time DESC LIMIT 1").fetchone()
        h1 = conn.execute("SELECT close FROM btc_candles WHERE open_time <= ? "
                          "ORDER BY open_time DESC LIMIT 1",
                          ((last["open_time"] - 3_600_000) if last else 0,)).fetchone()
        h24 = conn.execute("SELECT close FROM btc_candles WHERE open_time <= ? "
                           "ORDER BY open_time DESC LIMIT 1",
                           ((last["open_time"] - 86_400_000) if last else 0,)).fetchone()
        n_candles = conn.execute("SELECT COUNT(*) AS n FROM btc_candles").fetchone()["n"]

    price = last["close"] if last else None
    return {
        "price": price,
        "price_at": last["open_time"] if last else None,
        "change_1h": ((price / h1["close"] - 1) * 100) if (price and h1) else None,
        "change_24h": ((price / h24["close"] - 1) * 100) if (price and h24) else None,
        "candles": n_candles,
        "hours_covered": round(n_candles / 60, 1),
        "strategies": rows,
        "start_equity": START_EQUITY,
        "taker_fee_pct": TAKER_FEE * 100,
        "strategy_version": STRATEGY_VERSION,
    }


def trades(db_path: str, strategy_key: Optional[str] = None,
           status: Optional[str] = None, limit: int = 200) -> list[dict]:
    init_db(db_path)
    where, args = [], []
    if strategy_key:
        where.append("strategy_key=?")
        args.append(strategy_key)
    if status:
        where.append("status=?")
        args.append(status)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM btc_trades {clause} ORDER BY opened_at DESC, id DESC LIMIT ?",
            (*args, limit)).fetchall()
    return [dict(r) for r in rows]


def equity_curves(db_path: str, points: int = 300) -> dict[str, list[dict]]:
    init_db(db_path)
    out: dict[str, list[dict]] = {}
    with _connect(db_path) as conn:
        for s in STRATEGIES:
            rows = conn.execute(
                "SELECT at_ms, equity FROM btc_equity WHERE strategy_key=? "
                "ORDER BY at_ms DESC LIMIT ?", (s["key"], points)).fetchall()
            curve = [{"at": r["at_ms"], "equity": r["equity"]} for r in reversed(rows)]
            out[s["key"]] = [{"at": None, "equity": START_EQUITY}] + curve
    return out
