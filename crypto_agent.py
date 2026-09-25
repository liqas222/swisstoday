"""Crypto/Memecoin-Recherche — portiert aus liqas222/memecoins-chatgpt-agent.

Das Original ist eine Next.js-App auf Vercel mit OpenAI und Supabase. Hier läuft
dieselbe Logik direkt im bestehenden Dashboard: DexScreener liefert die
Marktdaten, Claude schreibt die Analyse (der Schlüssel liegt schon in der .env),
die Historie landet in der vorhandenen SQLite-Datei. Damit braucht der zweite
Tab weder einen Node-Prozess noch einen zusätzlichen API-Schlüssel.

Ausdrücklich nur Recherche und Papier-Trades. Es wird nichts gehandelt.
"""
import json
import logging
import os
import sqlite3
import time

import requests

logger = logging.getLogger(__name__)

DEX_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"
DEX_TIMEOUT = 15
MAX_PAIRS = 12
HISTORY_LIMIT = 20

# Kein Haiku: die Analyse soll Marktdaten wirklich durchdenken.
CRYPTO_MODEL = os.getenv("CRYPTO_MODEL", "claude-sonnet-5")

SYSTEM = """Du bist ein Research-Agent für Krypto und Memecoins.
Nur Recherche und Papier-Trade-Planung. Behaupte nie Gewissheit und führe nie einen echten Trade aus.

Für jede Analyse:
1. Nenne das relevanteste Handelspaar mit Chain und Contract-Adresse, sofern vorhanden.
2. Trenne beobachtete Daten klar von Schlussfolgerungen.
3. Analysiere Liquidität, Volumen, Kauf-/Verkaufsaktivität, Market Cap/FDV, Alter des Paars und kurzfristiges Momentum.
4. Nutze die Websuche für aktuelle Auslöser, Aufmerksamkeit in sozialen Medien, Projektbehauptungen, Betrug, Hacks, Listings oder wichtige Nachrichten.
5. Behandle Behauptungen aus sozialen Medien als unbestätigt, solange sie nicht belegt sind.
6. Benenne fehlende Daten ausdrücklich.
7. Vergib einen RESEARCH SCORE von 0-100. Er bewertet die Qualität des Setups, nicht die Gewinnwahrscheinlichkeit.
8. Gib einen hypothetischen Papier-Trade-Plan: Einstiegszone/-bedingung, Invalidierung, TP1/TP2/TP3 und Bedingungen zum Fernbleiben.
9. Betone Slippage, Liquidität und Ausstiegsrisiko bei jungen oder illiquiden Token.
10. Erfinde niemals Angaben zu Holder-Konzentration, Contract-Rechten, Steuern, gesperrter Liquidität, Insider-Wallets oder Dev-Anteilen.

Verwende genau diese Abschnitte:
# Überblick
# Beobachtete Daten
# Auslöser / Aufmerksamkeit
# Risiko-Signale
# Research Score
# Papier-Trade-Plan
# Was meine Einschätzung ändern würde"""


def search_dex(query: str, limit: int = MAX_PAIRS) -> list[dict]:
    """Handelspaare zu einem Ticker oder einer Contract-Adresse."""
    r = requests.get(DEX_SEARCH_URL, params={"q": query},
                     headers={"Accept": "application/json"}, timeout=DEX_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"DexScreener antwortet mit {r.status_code}")
    pairs = (r.json() or {}).get("pairs") or []

    out = []
    now_ms = time.time() * 1000
    for p in pairs[:limit]:
        created = p.get("pairCreatedAt")
        base = p.get("baseToken") or {}
        vol = p.get("volume") or {}
        chg = p.get("priceChange") or {}
        txns = (p.get("txns") or {}).get("h1") or {}
        out.append({
            "chain": p.get("chainId"), "dex": p.get("dexId"),
            "pair_address": p.get("pairAddress"), "url": p.get("url"),
            "base_name": base.get("name"), "base_symbol": base.get("symbol"),
            "base_address": base.get("address"),
            "quote_symbol": (p.get("quoteToken") or {}).get("symbol"),
            "price_usd": p.get("priceUsd"), "market_cap": p.get("marketCap"),
            "fdv": p.get("fdv"), "liquidity_usd": (p.get("liquidity") or {}).get("usd"),
            "volume_h1": vol.get("h1"), "volume_h6": vol.get("h6"), "volume_h24": vol.get("h24"),
            "buys_h1": txns.get("buys"), "sells_h1": txns.get("sells"),
            "change_m5_pct": chg.get("m5"), "change_h1_pct": chg.get("h1"),
            "change_h6_pct": chg.get("h6"), "change_h24_pct": chg.get("h24"),
            "pair_age_hours": round((now_ms - float(created)) / 3600000, 2) if created else None,
        })
    return out


def _client():
    import anthropic
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY fehlt in der .env")
    return anthropic.Anthropic(api_key=key)


def _text_of(msg) -> str:
    return "\n".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()


def run_agent(query: str, market: list[dict]) -> str:
    """Analyse schreiben lassen — mit Websuche, sofern das Konto sie erlaubt."""
    client = _client()
    prompt = (f"Analysiere diesen Token bzw. diese Suchanfrage: {query}\n\n"
              f"Aktuelle DexScreener-Kandidaten:\n{json.dumps(market, indent=2, ensure_ascii=False)}")
    kwargs = dict(model=CRYPTO_MODEL, max_tokens=3000, system=SYSTEM,
                  messages=[{"role": "user", "content": prompt}])
    try:
        msg = client.messages.create(
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
            **kwargs)
    except Exception as e:
        # Ältere SDKs oder Konten ohne Websuche: lieber eine Analyse allein aus
        # den Marktdaten als gar keine.
        logger.warning("Websuche nicht verfügbar (%s) — Analyse ohne Websuche", e)
        msg = client.messages.create(**kwargs)
    text = _text_of(msg)
    if not text:
        raise RuntimeError("Die KI hat keine Analyse zurückgegeben")
    return text


# ── Historie (ersetzt Supabase) ────────────────────────────────────────────

def init_db(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS crypto_analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            query TEXT NOT NULL,
            report TEXT NOT NULL,
            market_snapshot TEXT NOT NULL DEFAULT '[]')""")
        conn.execute("CREATE INDEX IF NOT EXISTS crypto_created_idx "
                     "ON crypto_analyses(created_at DESC)")


def save_analysis(db_path: str, query: str, report: str, market: list[dict]) -> None:
    try:
        init_db(db_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute("INSERT INTO crypto_analyses (query, report, market_snapshot) "
                         "VALUES (?,?,?)",
                         (query, report, json.dumps(market, ensure_ascii=False)))
    except Exception as e:  # eine Analyse geht nicht verloren, nur weil das Speichern klemmt
        logger.warning("Analyse konnte nicht gespeichert werden: %s", e)


def recent_analyses(db_path: str, limit: int = HISTORY_LIMIT) -> list[dict]:
    try:
        init_db(db_path)
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, query, report, created_at FROM crypto_analyses "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("Historie nicht lesbar: %s", e)
        return []


def delete_analysis(db_path: str, analysis_id: int) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM crypto_analyses WHERE id = ?", (analysis_id,))
