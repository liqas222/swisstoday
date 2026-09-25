"""Papier-Handel: der Scanner eröffnet und schliesst Positionen selbständig.

Kein echtes Geld, keine Wallet, keine Signatur. Eine Position ist eine
Buchung: Einstiegskurs, Stop, drei Ziele, und danach wird verfolgt was
tatsächlich passiert ist.

Der Zweck ist nicht Gewinn, sondern Messbarkeit. Solange niemand mitschreibt,
was aus einer Bewertung von 85 geworden ist, ist die Bewertung eine Meinung.
Erst wenn hundert geschlossene Positionen vorliegen, lässt sich fragen, ob 85
je besser war als 65 — und genau dafür wird jeder Einstieg mit dem
Punktestand, den Risiko-Signalen und der Gewichtungs-Version festgehalten,
die zu diesem Zeitpunkt galten.

Zwei Regeln, ohne die alles wertlos wäre:

* **Keine Information aus der Zukunft.** Ein Einstieg benutzt ausschliesslich
  Daten, die zum Zeitpunkt des Einstiegs vorlagen.
* **Keine Überlebenden-Auswahl.** Tote Token bleiben im Datensatz. Wer
  Verluste wegräumt, misst am Ende nur noch die Gewinner.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

import crypto_scanner as scanner
import crypto_store as store

logger = logging.getLogger(__name__)


# ── Strategie ──────────────────────────────────────────────────────────────
# Nichts davon ist optimiert — es sind Startwerte. Sie werden mit jeder
# Position gespeichert, damit später überhaupt etwas zu vergleichen ist.

CONFIG: dict[str, Any] = {
    # Virtuelle Positionsgrösse in Dollar. Es fliesst kein echtes Geld.
    "position_usd": 100.0,

    # Ab diesem Punktestand wird eine Position eröffnet. Bewusst nicht höher:
    # drei der sechs Bewertungskategorien haben noch keine Datenquelle, und
    # ein System, das nie einsteigt, sammelt auch nie die Daten, mit denen
    # sich die Schwelle später begründen liesse.
    "min_score": int(os.getenv("CRYPTO_PAPER_MIN_SCORE", "70")),
    # Und nur wenn das Risiko darunter liegt.
    "max_risk": int(os.getenv("CRYPTO_PAPER_MAX_RISK", "60")),

    # Memecoins schwanken zweistellig pro Stunde. Ein enger Stop wird vom
    # Rauschen abgeräumt, bevor die These überhaupt eine Chance hatte.
    "stop_pct": -35.0,

    # Teilverkäufe: je ein Drittel, der Rest läuft.
    "tp1_pct": 50.0,
    "tp2_pct": 150.0,
    "tp3_pct": 400.0,
    "tp1_fraction": 1 / 3,
    "tp2_fraction": 1 / 3,

    # Nach dem ersten Ziel wandert der Stop auf den Einstieg. Ab da kann die
    # Position nominal nichts mehr verlieren.
    "breakeven_after_tp1": True,

    # Spätestens danach wird geschlossen. Eine Position, die nach drei Tagen
    # nichts getan hat, sagt uns bereits alles.
    "max_hold_hours": 72,

    # Bricht die Liquidität unter diesen Anteil des Einstiegswerts, ist der
    # Pool praktisch weg — das zählt als Totalverlust, nicht als offene Wette.
    "rug_liquidity_fraction": 0.25,

    # Gleichzeitig offene Positionen.
    "max_open_trades": 25,
    # Pro Token nur eine offene Position, und nach dem Schliessen eine Sperre.
    "reentry_cooldown_hours": 24,
}

STRATEGY_VERSION = "paper-v1"


# ── Schema ─────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_trades (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id            TEXT NOT NULL,
    contract            TEXT NOT NULL,
    symbol              TEXT,
    name                TEXT,
    opened_at           TEXT NOT NULL DEFAULT (datetime('now')),
    strategy_version    TEXT NOT NULL,

    -- Der Wissensstand im Moment des Einstiegs. Nie nachträglich anfassen.
    score_at_entry      INTEGER,
    risk_at_entry       INTEGER,
    weights_version     TEXT,
    risk_flags_at_entry TEXT NOT NULL DEFAULT '[]',
    entry_price         REAL NOT NULL,
    entry_mcap          REAL,
    entry_liquidity     REAL,
    entry_age_hours     REAL,
    reason              TEXT,

    position_usd        REAL NOT NULL,
    stop_price          REAL NOT NULL,
    tp1_price           REAL NOT NULL,
    tp2_price           REAL NOT NULL,
    tp3_price           REAL NOT NULL,

    status              TEXT NOT NULL DEFAULT 'open',   -- open | closed
    remaining_fraction  REAL NOT NULL DEFAULT 1.0,
    realized_pnl_usd    REAL NOT NULL DEFAULT 0.0,

    last_checked_at     TEXT,
    last_price          REAL,
    last_liquidity      REAL,
    highest_price       REAL,
    lowest_price        REAL,
    highest_mcap        REAL,
    lowest_mcap         REAL,
    mfe_pct             REAL,   -- grösster Buchgewinn (maximum favorable excursion)
    mae_pct             REAL,   -- grösster Buchverlust (maximum adverse excursion)

    tp1_hit_at          TEXT,
    tp2_hit_at          TEXT,
    tp3_hit_at          TEXT,
    minutes_to_tp1      REAL,
    minutes_to_stop     REAL,

    closed_at           TEXT,
    exit_price          REAL,
    exit_reason         TEXT,
    return_pct          REAL,
    pnl_usd             REAL,
    duration_minutes    REAL
);
CREATE INDEX IF NOT EXISTS paper_status_idx ON paper_trades(status, opened_at DESC);
CREATE INDEX IF NOT EXISTS paper_token_idx  ON paper_trades(token_id, opened_at DESC);
CREATE INDEX IF NOT EXISTS paper_score_idx  ON paper_trades(score_at_entry);

CREATE TABLE IF NOT EXISTS paper_trade_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id      INTEGER NOT NULL,
    captured_at   TEXT NOT NULL DEFAULT (datetime('now')),
    price_usd     REAL,
    market_cap    REAL,
    liquidity_usd REAL,
    return_pct    REAL
);
CREATE INDEX IF NOT EXISTS paper_snap_idx ON paper_trade_snapshots(trade_id, id);
"""

_initialised: set[str] = set()


def init_db(db_path: str) -> None:
    if db_path in _initialised:
        return
    store.init_db(db_path)
    with store._connect(db_path) as conn:
        conn.executescript(SCHEMA)
    _initialised.add(db_path)


# ── Eröffnen ───────────────────────────────────────────────────────────────

def _open_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM paper_trades WHERE status='open'").fetchone()["n"]


def _blocked(conn, token_id: str) -> Optional[str]:
    """Schon offen, oder noch in der Sperrfrist nach dem letzten Ausstieg?"""
    row = conn.execute(
        "SELECT status, closed_at FROM paper_trades WHERE token_id=? "
        "ORDER BY id DESC LIMIT 1", (token_id,)).fetchone()
    if not row:
        return None
    if row["status"] == "open":
        return "bereits offen"
    cd = conn.execute(
        "SELECT datetime(?, ? || ' hours') > datetime('now') AS blocked",
        (row["closed_at"], f"+{CONFIG['reentry_cooldown_hours']}")).fetchone()
    return "Sperrfrist nach letztem Ausstieg" if cd and cd["blocked"] else None


def open_trades(db_path: str) -> dict:
    """Eröffnet Positionen für alles, was die Schwelle erreicht.

    Läuft direkt nach einem Scan, damit Einstiegskurs und Bewertung aus
    derselben Momentaufnahme stammen.
    """
    init_db(db_path)
    opened: list[dict] = []
    skipped = 0

    candidates = store.list_tokens(db_path, CONFIG["min_score"], limit=100)

    with store._connect(db_path) as conn:
        room = CONFIG["max_open_trades"] - _open_count(conn)

        for t in candidates:
            if room <= 0:
                break
            risk = t.get("risk_score")
            # Ausdrücklich gegen None prüfen: ein Risiko von 0 ist der beste
            # Fall, nicht der fehlende — "or" hätte ihn aussortiert.
            if risk is None or risk > CONFIG["max_risk"]:
                skipped += 1
                continue
            price = t.get("price_usd")
            if not price or price <= 0:
                skipped += 1  # ohne Einstiegskurs keine nachvollziehbare Position
                continue
            if _blocked(conn, t["id"]):
                skipped += 1
                continue

            stop = price * (1 + CONFIG["stop_pct"] / 100)
            reason = (f"Score {t.get('overall_score')} bei Risiko {t.get('risk_score')} · "
                      f"Liquidität {t.get('liquidity_usd') or 0:,.0f} $ · "
                      f"{(t.get('buy_ratio_h1') or 0) * 100:.0f}% Käufe 1h")

            cur = conn.execute(
                """INSERT INTO paper_trades
                     (token_id, contract, symbol, name, strategy_version,
                      score_at_entry, risk_at_entry, weights_version, risk_flags_at_entry,
                      entry_price, entry_mcap, entry_liquidity, entry_age_hours, reason,
                      position_usd, stop_price, tp1_price, tp2_price, tp3_price,
                      last_price, last_liquidity, highest_price, lowest_price,
                      highest_mcap, lowest_mcap, mfe_pct, mae_pct, last_checked_at)
                   VALUES (?,?,?,?,?, ?,?,?,?, ?,?,?,?,?, ?,?,?,?,?, ?,?,?,?, ?,?,?,?, datetime('now'))""",
                (t["id"], t["contract"], t.get("symbol"), t.get("name"), STRATEGY_VERSION,
                 t.get("overall_score"), t.get("risk_score"), t.get("weights_version"),
                 json.dumps(t.get("flags") or [], ensure_ascii=False),
                 price, t.get("market_cap"), t.get("liquidity_usd"), t.get("pair_age_hours"),
                 reason, CONFIG["position_usd"], stop,
                 price * (1 + CONFIG["tp1_pct"] / 100),
                 price * (1 + CONFIG["tp2_pct"] / 100),
                 price * (1 + CONFIG["tp3_pct"] / 100),
                 price, t.get("liquidity_usd"), price, price,
                 t.get("market_cap"), t.get("market_cap"), 0.0, 0.0))

            opened.append({"id": cur.lastrowid, "symbol": t.get("symbol"),
                           "score": t.get("overall_score"), "entry_price": price})
            room -= 1

    if opened:
        logger.info("[PAPER] %d Position(en) eröffnet: %s", len(opened),
                    ", ".join(f"{o['symbol']}@{o['score']}" for o in opened))

    # Wenn nichts aufgeht, ist die nächste Frage immer "warum nicht" — also
    # gleich mitliefern, wie weit der beste Kandidat entfernt war.
    best = store.list_tokens(db_path, 0, limit=1)
    return {
        "opened": len(opened),
        "skipped": skipped,
        "trades": opened,
        "min_score": CONFIG["min_score"],
        "best_score_now": best[0].get("overall_score") if best else None,
    }


# ── Verfolgen und Schliessen ───────────────────────────────────────────────

def _fetch_prices(contracts: list[str]) -> dict[str, dict]:
    """Aktuelle Marktdaten für mehrere Adressen. Fehlende bleiben fehlend."""
    out: dict[str, dict] = {}
    for i in range(0, len(contracts), 25):
        chunk = contracts[i:i + 25]
        data = scanner._get_json(f"{scanner.DEX_BASE}/tokens/v1/solana/{','.join(chunk)}")
        pairs = data if isinstance(data, list) else (data or {}).get("pairs") or []
        for raw in pairs:
            c = scanner.map_pair(raw, "paper:track")
            if not c:
                continue
            prev = out.get(c.contract)
            # Mehrere Pools je Token — der tiefste ist der massgebliche.
            if prev is None or (c.market["liquidity_usd"] or 0) > (prev["liquidity_usd"] or 0):
                out[c.contract] = c.market
    return out


def _ret(price: float, entry: float) -> float:
    return (price / entry - 1) * 100


def track_trades(db_path: str) -> dict:
    """Aktualisiert alle offenen Positionen und schliesst, was fertig ist."""
    init_db(db_path)

    with store._connect(db_path) as conn:
        open_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM paper_trades WHERE status='open' ORDER BY id").fetchall()]

    if not open_rows:
        return {"tracked": 0, "closed": 0, "stale": 0, "closures": []}

    prices = _fetch_prices(sorted({r["contract"] for r in open_rows}))

    tracked = stale = 0
    closures: list[dict] = []
    now = time.time()

    with store._connect(db_path) as conn:
        for tr in open_rows:
            m = prices.get(tr["contract"])
            age_h = conn.execute(
                "SELECT (julianday('now') - julianday(?)) * 24 AS h", (tr["opened_at"],)
            ).fetchone()["h"]

            if not m or not m.get("price_usd"):
                # Keine Daten mehr: erst nach der Haltefrist als tot verbuchen,
                # vorher kann es ein Aussetzer beim Anbieter sein.
                stale += 1
                if age_h >= CONFIG["max_hold_hours"]:
                    # Über die ganze Haltefrist keine Daten: DexScreener wirft
                    # ein Paar raus, wenn die Liquidität weg ist. Den Rest mit
                    # dem letzten bekannten Kurs zu bewerten wäre geschönt —
                    # er ist nicht mehr verkäuflich.
                    _close(conn, tr, None,
                           "keine Marktdaten mehr — als Totalverlust verbucht",
                           age_h, closures)
                continue

            price = m["price_usd"]
            liq = m.get("liquidity_usd")
            mcap = m.get("market_cap")
            entry = tr["entry_price"]
            ret = _ret(price, entry)
            tracked += 1

            high = max(tr["highest_price"] or price, price)
            low = min(tr["lowest_price"] or price, price)
            mfe = max(tr["mfe_pct"] or 0.0, _ret(high, entry))
            mae = min(tr["mae_pct"] or 0.0, _ret(low, entry))

            conn.execute(
                """UPDATE paper_trades SET last_checked_at=datetime('now'), last_price=?,
                     last_liquidity=?, highest_price=?, lowest_price=?,
                     highest_mcap=MAX(COALESCE(highest_mcap, ?), ?),
                     lowest_mcap=MIN(COALESCE(lowest_mcap, ?), ?),
                     mfe_pct=?, mae_pct=? WHERE id=?""",
                (price, liq, high, low, mcap or 0, mcap or 0, mcap or 1e18, mcap or 1e18,
                 mfe, mae, tr["id"]))
            conn.execute(
                "INSERT INTO paper_trade_snapshots (trade_id, price_usd, market_cap, "
                "liquidity_usd, return_pct) VALUES (?,?,?,?,?)",
                (tr["id"], price, mcap, liq, ret))

            # ── Teilverkäufe ──
            remaining = tr["remaining_fraction"]
            realized = tr["realized_pnl_usd"]
            pos = tr["position_usd"]

            if not tr["tp1_hit_at"] and price >= tr["tp1_price"]:
                part = CONFIG["tp1_fraction"]
                realized += pos * part * (CONFIG["tp1_pct"] / 100)
                remaining -= part
                stop_now = entry if CONFIG["breakeven_after_tp1"] else tr["stop_price"]
                conn.execute(
                    "UPDATE paper_trades SET tp1_hit_at=datetime('now'), minutes_to_tp1=?, "
                    "remaining_fraction=?, realized_pnl_usd=?, stop_price=? WHERE id=?",
                    (age_h * 60, remaining, realized, stop_now, tr["id"]))
                tr["stop_price"] = stop_now
                logger.info("[PAPER] %s TP1 erreicht (+%.0f%%)", tr["symbol"], CONFIG["tp1_pct"])

            if not tr["tp2_hit_at"] and price >= tr["tp2_price"]:
                part = CONFIG["tp2_fraction"]
                realized += pos * part * (CONFIG["tp2_pct"] / 100)
                remaining -= part
                conn.execute(
                    "UPDATE paper_trades SET tp2_hit_at=datetime('now'), "
                    "remaining_fraction=?, realized_pnl_usd=? WHERE id=?",
                    (remaining, realized, tr["id"]))
                logger.info("[PAPER] %s TP2 erreicht (+%.0f%%)", tr["symbol"], CONFIG["tp2_pct"])

            tr["remaining_fraction"] = remaining
            tr["realized_pnl_usd"] = realized

            # ── Ausstiegsregeln, in dieser Reihenfolge ──
            if price >= tr["tp3_price"]:
                _close(conn, tr, price, "Ziel 3 erreicht", age_h, closures)
            elif price <= tr["stop_price"]:
                reason = "Stop auf Einstieg" if tr["tp1_hit_at"] else "Stop ausgelöst"
                conn.execute("UPDATE paper_trades SET minutes_to_stop=? WHERE id=?",
                             (age_h * 60, tr["id"]))
                _close(conn, tr, price, reason, age_h, closures)
            elif (liq is not None and tr["entry_liquidity"]
                  and liq < tr["entry_liquidity"] * CONFIG["rug_liquidity_fraction"]):
                _close(conn, tr, price, "Liquidität zusammengebrochen", age_h, closures)
            elif age_h >= CONFIG["max_hold_hours"]:
                _close(conn, tr, price, "Haltefrist abgelaufen", age_h, closures)

    if closures:
        logger.info("[PAPER] %d Position(en) geschlossen: %s", len(closures),
                    ", ".join(f"{c['symbol']} {c['return_pct']:+.1f}%" for c in closures))

    return {"tracked": tracked, "closed": len(closures), "stale": stale, "closures": closures}


def _close(conn, tr: dict, exit_price: Optional[float], reason: str,
           age_h: float, closures: list) -> None:
    """Schliesst den Rest der Position und rechnet ab."""
    entry = tr["entry_price"]
    remaining = tr["remaining_fraction"]
    realized = tr["realized_pnl_usd"]
    pos = tr["position_usd"]

    if exit_price and entry:
        rest_pnl = pos * remaining * (_ret(exit_price, entry) / 100)
    else:
        rest_pnl = -pos * remaining  # keine Daten mehr, Rest als Totalverlust
        exit_price = None

    pnl = realized + rest_pnl
    return_pct = (pnl / pos) * 100 if pos else 0.0

    conn.execute(
        """UPDATE paper_trades SET status='closed', closed_at=datetime('now'),
             exit_price=?, exit_reason=?, pnl_usd=?, return_pct=?,
             remaining_fraction=0, realized_pnl_usd=?, duration_minutes=?
           WHERE id=?""",
        (exit_price, reason, pnl, return_pct, pnl, age_h * 60, tr["id"]))

    closures.append({"id": tr["id"], "symbol": tr["symbol"], "reason": reason,
                     "return_pct": return_pct, "pnl_usd": pnl,
                     "score_at_entry": tr["score_at_entry"]})


# ── Lesen ──────────────────────────────────────────────────────────────────

def list_trades(db_path: str, status: str = "open", limit: int = 200) -> list[dict]:
    init_db(db_path)
    with store._connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM paper_trades WHERE status=? ORDER BY "
            + ("opened_at DESC" if status == "open" else "closed_at DESC")
            + " LIMIT ?", (status, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["risk_flags_at_entry"] = json.loads(d["risk_flags_at_entry"])
        except (TypeError, ValueError):
            d["risk_flags_at_entry"] = []
        if d["status"] == "open" and d.get("last_price") and d.get("entry_price"):
            d["unrealized_return_pct"] = _ret(d["last_price"], d["entry_price"])
            d["open_value_pnl_usd"] = (
                d["realized_pnl_usd"]
                + d["position_usd"] * d["remaining_fraction"] * d["unrealized_return_pct"] / 100)
        out.append(d)
    return out


def trade_snapshots(db_path: str, trade_id: int, limit: int = 200) -> list[dict]:
    init_db(db_path)
    with store._connect(db_path) as conn:
        rows = conn.execute(
            "SELECT captured_at, price_usd, market_cap, liquidity_usd, return_pct "
            "FROM paper_trade_snapshots WHERE trade_id=? ORDER BY id DESC LIMIT ?",
            (trade_id, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def _median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def _summarise(rows: list[dict]) -> dict:
    """Kennzahlen einer Menge geschlossener Positionen."""
    n = len(rows)
    if n == 0:
        return {"trades": 0}

    rets = [r["return_pct"] or 0.0 for r in rows]
    pnls = [r["pnl_usd"] or 0.0 for r in rows]
    winners = [r for r in rets if r > 0]
    losers = [r for r in rets if r <= 0]
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))

    return {
        "trades": n,
        "win_rate": round(len(winners) / n * 100, 1),
        "avg_return": round(sum(rets) / n, 1),
        "median_return": round(_median(rets) or 0, 1),
        "avg_winner": round(sum(winners) / len(winners), 1) if winners else None,
        "avg_loser": round(sum(losers) / len(losers), 1) if losers else None,
        "total_pnl_usd": round(sum(pnls), 2),
        # Verhältnis von Bruttogewinn zu Bruttoverlust. Ohne Verlust nicht
        # definiert — dann lieber None als eine erfundene Unendlichkeit.
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "expectancy_usd": round(sum(pnls) / n, 2),
        "avg_mfe": round(sum(r["mfe_pct"] or 0 for r in rows) / n, 1),
        "avg_mae": round(sum(r["mae_pct"] or 0 for r in rows) / n, 1),
        "avg_hold_hours": round(sum((r["duration_minutes"] or 0) / 60 for r in rows) / n, 1),
    }


# Unter dieser Zahl geschlossener Positionen ist eine Trefferquote Rauschen.
MIN_TRADES_FOR_STATS = 10


def performance(db_path: str) -> dict:
    """Auswertung. Gruppen mit zu wenig Positionen werden als solche
    ausgewiesen statt mit einer Zahl versehen, die nichts bedeutet."""
    init_db(db_path)
    closed = list_trades(db_path, "closed", limit=5000)
    open_rows = list_trades(db_path, "open", limit=500)

    def bucket(r: dict) -> str:
        s = r.get("score_at_entry")
        if s is None:
            return "ohne Punktestand"
        for lo in (90, 80, 70, 60, 50):
            if s >= lo:
                return f"{lo}-{lo + 9}" if lo != 90 else "90-100"
        return "unter 50"

    by_score: dict[str, dict] = {}
    for r in closed:
        by_score.setdefault(bucket(r), []).append(r)

    groups = {}
    for key, rows in sorted(by_score.items(), reverse=True):
        summary = _summarise(rows)
        summary["enough_data"] = len(rows) >= MIN_TRADES_FOR_STATS
        groups[key] = summary

    def by(field: str, label) -> dict:
        out: dict[str, list] = {}
        for r in closed:
            out.setdefault(label(r.get(field)), []).append(r)
        return {k: {**_summarise(v), "enough_data": len(v) >= MIN_TRADES_FOR_STATS}
                for k, v in sorted(out.items())}

    def mcap_band(v):
        if not v:
            return "unbekannt"
        for lim, name in ((100_000, "< 100k"), (1_000_000, "100k-1M"), (10_000_000, "1M-10M")):
            if v < lim:
                return name
        return "> 10M"

    def age_band(v):
        if v is None:
            return "unbekannt"
        if v < 6:
            return "< 6h"
        if v < 24:
            return "6-24h"
        if v < 72:
            return "1-3 Tage"
        return "> 3 Tage"

    def risk_band(v):
        if v is None:
            return "unbekannt"
        for lim, name in ((25, "0-24"), (45, "25-44"), (70, "45-69")):
            if v < lim:
                return name
        return "70+"

    exit_reasons: dict[str, int] = {}
    for r in closed:
        exit_reasons[r.get("exit_reason") or "unbekannt"] = \
            exit_reasons.get(r.get("exit_reason") or "unbekannt", 0) + 1

    open_pnl = sum(r.get("open_value_pnl_usd") or 0 for r in open_rows)

    return {
        "overall": {**_summarise(closed), "enough_data": len(closed) >= MIN_TRADES_FOR_STATS},
        "open_trades": len(open_rows),
        "open_unrealised_pnl_usd": round(open_pnl, 2),
        "by_score": groups,
        "by_market_cap": by("entry_mcap", mcap_band),
        "by_age": by("entry_age_hours", age_band),
        "by_risk": by("risk_at_entry", risk_band),
        "exit_reasons": exit_reasons,
        "min_trades_for_stats": MIN_TRADES_FOR_STATS,
        "strategy_version": STRATEGY_VERSION,
        "config": CONFIG,
    }


# ── Der Job ────────────────────────────────────────────────────────────────

def run_cycle(db_path: Optional[str] = None) -> dict:
    """Ein vollständiger Durchgang: scannen, eröffnen, verfolgen.

    Das Verfolgen läuft zuerst — eine Position, die gerade ihren Stop erreicht
    hat, soll geschlossen werden, bevor neue Plätze vergeben werden.
    """
    db_path = db_path or os.getenv("DB_PATH", "swissintel.db")
    result: dict[str, Any] = {}

    try:
        result["tracking"] = track_trades(db_path)
    except Exception as e:
        logger.exception("Verfolgen der Papier-Positionen fehlgeschlagen")
        result["tracking"] = {"error": str(e)}

    try:
        result["scan"] = scanner.run_scan(db_path)
    except Exception as e:
        logger.exception("Scan fehlgeschlagen")
        result["scan"] = {"error": str(e)}

    try:
        result["opening"] = open_trades(db_path)
    except Exception as e:
        logger.exception("Eröffnen von Papier-Positionen fehlgeschlagen")
        result["opening"] = {"error": str(e)}

    return result
