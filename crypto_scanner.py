"""Autonomer Memecoin-Scanner — Discovery, Filter, Risiko, Bewertung.

Portiert aus dem Next.js-Projekt memecoins-chatgpt-agent, hier aber im
laufenden SwissIntel-Server statt auf Vercel. Das ist die bessere Heimat: es
gibt bereits einen Scheduler, der rund um die Uhr läuft, und eine SQLite-Datei,
die wirklich bestehen bleibt — beides musste auf Vercel erst mühsam nachgebaut
werden.

Zwei Regeln ziehen sich durch alles:

* Fehlende Daten sind ``None``, niemals ``0``. Ein Token ohne bekannte
  Liquidität ist kein Token mit null Liquidität, und ein Filter darf darauf
  nicht ablehnen — sonst enthält der Datensatz am Ende nur noch Token, deren
  Daten zufällig geladen wurden.
* Billig vor teuer. Hunderte Kandidaten durch kostenlose Filter, Dutzende
  durch Risiko und Bewertung, und nur eine Handvoll käme je zur KI.

Es wird nichts gehandelt. Papier-Trades kommen in einer späteren Stufe.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

DEX_BASE = "https://api.dexscreener.com"
SOURCE = "dexscreener"
TIMEOUT = 15
HOST_SPACING_S = 0.25

WEIGHTS_VERSION = "v1"


# ── Konfiguration ──────────────────────────────────────────────────────────
# Nichts davon ist optimiert. Es sind Startwerte, und genau deshalb wird jede
# Bewertung mit ihrer Gewichtungs-Version gespeichert: um später zu prüfen,
# welche davon falsch lagen.

FILTERS: dict[str, Any] = {
    "min_liquidity_usd": 15_000,
    "max_liquidity_usd": None,
    "min_volume_h24_usd": 25_000,
    "min_txns_h24": 100,
    "min_buys_h1": 5,
    "min_market_cap_usd": 30_000,
    "max_market_cap_usd": 50_000_000,
    "max_pair_age_hours": 24 * 14,
    "min_pair_age_minutes": 10,
    "min_liquidity_to_mcap": 0.02,
    "max_volume_to_liquidity": 60,
    "min_buy_ratio_h1": 0.25,
    "blocked_symbols": [],
}

# Maximalpunkte je Kategorie, zusammen 100.
WEIGHTS = {
    "market_structure": 25,
    "momentum": 20,
    "liquidity": 15,
    "social": 15,      # Stufe 4
    "onchain": 15,     # Stufe 3
    "catalyst": 10,    # Stufe 5
}

RISK_PENALTY = {"max_penalty": 40, "ignore_below": 15, "critical_flag_penalty": 25}

THRESHOLDS = {
    "opportunity": 65,   # erscheint unter Chancen
    "paper_trade": 75,   # Stufe 2
    "ai_research": 70,   # erst darüber kostet ein Token Geld
    "max_risk_score": 80,
}

LIMITS = {"max_discovered": 300, "max_enriched": 60, "max_run_seconds": 90}

DISCOVERY_QUERIES = ["SOL", "pump", "WSOL", "USDC", "bonk"]

# Prüfungen, die einen On-Chain-Anbieter brauchen (Stufe 3). Sie werden als
# nicht verfügbar ausgewiesen statt stillschweigend übersprungen — sonst liest
# sich eine Lücke wie „kein Risiko gefunden".
ONCHAIN_CHECKS = [
    "MINT_AUTHORITY_ACTIVE", "FREEZE_AUTHORITY_ACTIVE", "HIGH_HOLDER_CONCENTRATION",
    "DEV_WALLET_HOLDINGS", "DEV_WALLET_SELLING", "LP_NOT_BURNED_OR_LOCKED",
    "SNIPER_CLUSTER", "HONEYPOT_TRANSFER_RESTRICTION",
]


# ── HTTP ───────────────────────────────────────────────────────────────────

_last_call: dict[str, float] = {}


def _get_json(url: str, params: Optional[dict] = None, retries: int = 2) -> Any:
    """Holt JSON. Gibt bei Fehlschlag None zurück und wirft nie."""
    host = url.split("/")[2] if "//" in url else url
    for attempt in range(retries + 1):
        wait = _last_call.get(host, 0) + HOST_SPACING_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call[host] = time.monotonic()
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT,
                             headers={"Accept": "application/json"})
            if r.status_code == 429 or r.status_code >= 500:
                if attempt < retries:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                logger.debug("DexScreener %s für %s", r.status_code, url)
                return None
            if r.status_code != 200:
                return None
            return r.json()
        except Exception as e:
            if attempt < retries:
                time.sleep(0.5 * (2 ** attempt))
                continue
            logger.debug("Abruf fehlgeschlagen %s: %s", url, e)
    return None


def _num(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if n == n and n not in (float("inf"), float("-inf")) else None


def _ratio(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b in (None, 0):
        return None
    return a / b


# ── Datenmodell ────────────────────────────────────────────────────────────

@dataclass
class Candidate:
    """Ein Token samt Momentaufnahme seines Marktes."""
    contract: str
    symbol: Optional[str]
    name: Optional[str]
    pair_address: Optional[str]
    dex: Optional[str]
    pair_created_at: Optional[str]
    discovery_source: str
    image_url: Optional[str]
    website: Optional[str]
    twitter: Optional[str]
    telegram: Optional[str]
    market: dict[str, Any] = field(default_factory=dict)

    @property
    def token_id(self) -> str:
        return f"solana:{self.contract}"


def _social(info: dict, kind: str) -> Optional[str]:
    for s in (info.get("socials") or []):
        if str(s.get("type") or s.get("platform") or "").lower() == kind:
            return s.get("url")
    return None


def map_pair(raw: dict, source: str, now_ms: Optional[float] = None) -> Optional[Candidate]:
    """DexScreener-Paar → Kandidat. Nicht-Solana wird verworfen."""
    base = raw.get("baseToken") or {}
    contract = base.get("address")
    if not contract or raw.get("chainId") != "solana":
        return None

    now_ms = now_ms if now_ms is not None else time.time() * 1000
    created = _num(raw.get("pairCreatedAt"))
    age_h = (now_ms - created) / 3_600_000 if created else None

    vol = raw.get("volume") or {}
    chg = raw.get("priceChange") or {}
    tx = raw.get("txns") or {}
    tx_h1, tx_h24 = tx.get("h1") or {}, tx.get("h24") or {}
    buys_h1, sells_h1 = _num(tx_h1.get("buys")), _num(tx_h1.get("sells"))
    buys_h24, sells_h24 = _num(tx_h24.get("buys")), _num(tx_h24.get("sells"))

    liquidity = _num((raw.get("liquidity") or {}).get("usd"))
    mcap = _num(raw.get("marketCap")) or _num(raw.get("fdv"))
    vol_h24 = _num(vol.get("h24"))
    info = raw.get("info") or {}

    market = {
        "captured_at": None,  # von der Ablage gesetzt
        "source": SOURCE,
        "price_usd": _num(raw.get("priceUsd")),
        "market_cap": mcap,
        "fdv": _num(raw.get("fdv")),
        "liquidity_usd": liquidity,
        "volume_m5": _num(vol.get("m5")),
        "volume_h1": _num(vol.get("h1")),
        "volume_h6": _num(vol.get("h6")),
        "volume_h24": vol_h24,
        "change_m5_pct": _num(chg.get("m5")),
        "change_h1_pct": _num(chg.get("h1")),
        "change_h6_pct": _num(chg.get("h6")),
        "change_h24_pct": _num(chg.get("h24")),
        "buys_h1": buys_h1,
        "sells_h1": sells_h1,
        "buys_h24": buys_h24,
        "sells_h24": sells_h24,
        "txns_h1": buys_h1 + sells_h1 if buys_h1 is not None and sells_h1 is not None else None,
        "txns_h24": buys_h24 + sells_h24 if buys_h24 is not None and sells_h24 is not None else None,
        "pair_age_hours": round(age_h, 2) if age_h is not None else None,
        "volume_to_liquidity": _ratio(vol_h24, liquidity),
        "liquidity_to_mcap": _ratio(liquidity, mcap),
        "buy_ratio_h1": (buys_h1 / (buys_h1 + sells_h1))
                        if buys_h1 is not None and sells_h1 is not None and buys_h1 + sells_h1 > 0
                        else None,
    }

    created_iso = None
    if created:
        created_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(created / 1000))

    return Candidate(
        contract=contract,
        symbol=base.get("symbol"),
        name=base.get("name"),
        pair_address=raw.get("pairAddress"),
        dex=raw.get("dexId"),
        pair_created_at=created_iso,
        discovery_source=source,
        image_url=info.get("imageUrl"),
        website=((info.get("websites") or [{}])[0] or {}).get("url"),
        twitter=_social(info, "twitter"),
        telegram=_social(info, "telegram"),
        market=market,
    )


# ── Anbieter: DexScreener ──────────────────────────────────────────────────

def search(query: str, limit: int = 30) -> list[Candidate]:
    data = _get_json(f"{DEX_BASE}/latest/dex/search", {"q": query})
    pairs = (data or {}).get("pairs") or []
    out = [map_pair(p, "dexscreener:search") for p in pairs[:limit]]
    return [c for c in out if c]


def by_contract(contract: str) -> list[Candidate]:
    data = _get_json(f"{DEX_BASE}/token-pairs/v1/solana/{contract}")
    pairs = data if isinstance(data, list) else (data or {}).get("pairs") or []
    out = [c for c in (map_pair(p, "dexscreener:search") for p in pairs) if c]
    return out or search(contract, 12)


def _addresses(path: str) -> list[str]:
    data = _get_json(f"{DEX_BASE}{path}")
    if not isinstance(data, list):
        return []
    return [d["tokenAddress"] for d in data
            if d.get("chainId") == "solana" and d.get("tokenAddress")]


def boosted() -> list[str]:
    """Token, deren Teams für Sichtbarkeit zahlen. Kein Qualitätssignal —
    aber eine lebende Liste von Token, die aktiv Aufmerksamkeit suchen."""
    return _addresses("/token-boosts/latest/v1")


def profiles() -> list[str]:
    return _addresses("/token-profiles/latest/v1")


# ── Filter ─────────────────────────────────────────────────────────────────

def apply_filters(c: Candidate, f: dict) -> Optional[tuple[str, str]]:
    """None wenn der Token durchkommt, sonst (Regel, Begründung)."""
    m = c.market

    def money(n: float) -> str:
        return f"${n:,.0f}"

    if c.symbol and c.symbol.upper() in [s.upper() for s in f["blocked_symbols"]]:
        return "BLOCKED_SYMBOL", f"{c.symbol} steht auf der Sperrliste"

    liq, mcap = m["liquidity_usd"], m["market_cap"]

    if liq is not None and liq < f["min_liquidity_usd"]:
        return "LOW_LIQUIDITY", f"{money(liq)} < {money(f['min_liquidity_usd'])}"
    if f["max_liquidity_usd"] is not None and liq is not None and liq > f["max_liquidity_usd"]:
        return "LIQUIDITY_TOO_HIGH", f"{money(liq)} > {money(f['max_liquidity_usd'])}"
    if m["volume_h24"] is not None and m["volume_h24"] < f["min_volume_h24_usd"]:
        return "LOW_VOLUME", f"{money(m['volume_h24'])} 24h < {money(f['min_volume_h24_usd'])}"
    if m["txns_h24"] is not None and m["txns_h24"] < f["min_txns_h24"]:
        return "FEW_TRANSACTIONS", f"{m['txns_h24']:.0f} in 24h < {f['min_txns_h24']}"
    if m["buys_h1"] is not None and m["buys_h1"] < f["min_buys_h1"]:
        return "FEW_BUYS", f"{m['buys_h1']:.0f} Käufe in 1h < {f['min_buys_h1']}"
    if mcap is not None and mcap < f["min_market_cap_usd"]:
        return "MCAP_TOO_LOW", f"{money(mcap)} < {money(f['min_market_cap_usd'])}"
    if f["max_market_cap_usd"] is not None and mcap is not None and mcap > f["max_market_cap_usd"]:
        return "MCAP_TOO_HIGH", f"{money(mcap)} > {money(f['max_market_cap_usd'])}"
    age = m["pair_age_hours"]
    if age is not None and age > f["max_pair_age_hours"]:
        return "TOO_OLD", f"{age:.1f}h > {f['max_pair_age_hours']}h"
    if age is not None and age * 60 < f["min_pair_age_minutes"]:
        return "TOO_YOUNG", f"{age * 60:.0f}min < {f['min_pair_age_minutes']}min"
    ltm = m["liquidity_to_mcap"]
    if ltm is not None and ltm < f["min_liquidity_to_mcap"]:
        return "THIN_RELATIVE_LIQUIDITY", (
            f"Liquidität ist {ltm * 100:.1f}% der Marktkapitalisierung "
            f"< {f['min_liquidity_to_mcap'] * 100:.1f}%")
    vtl = m["volume_to_liquidity"]
    if vtl is not None and vtl > f["max_volume_to_liquidity"]:
        return "IMPLAUSIBLE_TURNOVER", (
            f"24h-Volumen ist das {vtl:.0f}-fache der Liquidität "
            f"> {f['max_volume_to_liquidity']}x")
    br = m["buy_ratio_h1"]
    if br is not None and br < f["min_buy_ratio_h1"]:
        return "SELL_PRESSURE", (
            f"nur {br * 100:.0f}% der Trades der letzten Stunde sind Käufe "
            f"< {f['min_buy_ratio_h1'] * 100:.0f}%")
    return None


# ── Risiko ─────────────────────────────────────────────────────────────────

_SEVERITY_WEIGHT = {"low": 5, "medium": 12, "high": 22, "critical": 35}


def assess_risk(c: Candidate) -> dict:
    m = c.market
    flags: list[dict] = []

    def add(code: str, severity: str, description: str) -> None:
        flags.append({"code": code, "severity": severity, "description": description})

    liq, mcap = m["liquidity_usd"], m["market_cap"]

    if liq is not None:
        if liq < 5_000:
            add("CRITICAL_LOW_LIQUIDITY", "critical",
                f"Nur ${liq:,.0f} Liquidität — ein Ausstieg bewegt den Kurs gegen dich.")
        elif liq < 20_000:
            add("LOW_LIQUIDITY", "high",
                f"${liq:,.0f} Liquidität ist dünn für alles ausser einer kleinen Position.")

    ltm = m["liquidity_to_mcap"]
    if ltm is not None and ltm < 0.03:
        add("THIN_RELATIVE_LIQUIDITY", "high",
            f"Liquidität ist {ltm * 100:.1f}% der Marktkapitalisierung. "
            "Der grösste Teil des Werts lässt sich nicht verkaufen.")

    vtl = m["volume_to_liquidity"]
    if vtl is not None and vtl > 30:
        add("SUSPICIOUS_TURNOVER", "critical" if vtl > 80 else "high",
            f"24h-Volumen ist das {vtl:.0f}-fache des Pools. Das Muster passt zu Wash-Trading.")

    age = m["pair_age_hours"]
    if age is not None:
        if age < 1:
            add("EXTREMELY_YOUNG", "high",
                f"Das Paar ist {age * 60:.0f} Minuten alt. Fast nichts daran ist belegt.")
        elif age < 24:
            add("YOUNG_PAIR", "medium", f"Das Paar ist {age:.1f} Stunden alt.")

    br, tx1 = m["buy_ratio_h1"], m["txns_h1"]
    if br is not None and br < 0.35 and (tx1 or 0) >= 20:
        add("SELL_IMBALANCE", "high",
            f"Nur {br * 100:.0f}% der Trades der letzten Stunde waren Käufe — Halter steigen aus.")

    if m["change_h1_pct"] is not None and m["change_h1_pct"] > 300:
        add("VERTICAL_PRICE_SPIKE", "high",
            f"+{m['change_h1_pct']:.0f}% in einer Stunde. Wer danach einsteigt, "
            "kauft den Ausstieg eines anderen.")
    if m["change_h24_pct"] is not None and m["change_h24_pct"] < -70:
        add("COLLAPSING", "high", f"{m['change_h24_pct']:.0f}% über 24 Stunden.")

    if mcap is not None and liq is not None and mcap > 5_000_000 and liq < 50_000:
        add("MCAP_LIQUIDITY_MISMATCH", "critical",
            "Millionenbewertung auf einem winzigen Pool. Der Wert ist nicht realisierbar.")

    if not c.website and not c.twitter and not c.telegram:
        add("NO_PUBLIC_PRESENCE", "low",
            "Weder Website noch X-Konto noch Telegram im Listing hinterlegt.")

    if c.symbol and len(c.symbol) <= 2 and c.symbol.isalnum():
        add("SUSPICIOUS_TICKER", "low",
            f'Kürzel "{c.symbol}" ist kurz genug, um etwas anderes nachzuahmen.')

    raw = sum(_SEVERITY_WEIGHT[f["severity"]] for f in flags)
    risk_score = min(100, round(raw))
    if any(f["severity"] == "critical" for f in flags):
        risk_score = max(risk_score, 70)

    unavailable = list(ONCHAIN_CHECKS)
    if liq is None:
        unavailable.append("LIQUIDITY_UNKNOWN")
    if mcap is None:
        unavailable.append("MARKET_CAP_UNKNOWN")

    return {"risk_score": risk_score, "flags": flags, "unavailable_checks": unavailable}


# ── Bewertung ──────────────────────────────────────────────────────────────

def _band(v: Optional[float], lo: float, hi: float) -> Optional[float]:
    if v is None or hi == lo:
        return None
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


def _peak(v: Optional[float], ideal: float, tolerance: float) -> Optional[float]:
    if v is None:
        return None
    return max(0.0, 1 - abs(v - ideal) / tolerance)


def _avg(parts: list[Optional[float]]) -> Optional[float]:
    known = [p for p in parts if p is not None]
    return sum(known) / len(known) if known else None


def score_token(c: Candidate, risk: dict) -> dict:
    """Interne Forschungs-Rangfolge 0-100. KEINE Gewinnwahrscheinlichkeit.

    Kategorien ohne Datenquelle geben null Punkte und stehen in
    ``missing_inputs``; die Summe wird über die messbaren Kategorien
    normalisiert, damit ein Wert mit sich selbst vergleichbar bleibt statt
    unsichtbar bei 60 gedeckelt zu sein.
    """
    m = c.market
    missing: list[str] = []

    market_structure = _avg([
        _band(m["market_cap"], 30_000, 3_000_000),
        _band(m["txns_h24"], 100, 3_000),
        # Ein paar Stunden bis wenige Tage: nach dem Start-Chaos, vor dem
        # Vergessenwerden.
        _peak(m["pair_age_hours"], 36, 120),
    ])
    if market_structure is None:
        missing.append("market_structure")

    vol_accel = None
    if m["volume_h1"] is not None and m["volume_h24"]:
        vol_accel = _band((m["volume_h1"] * 24) / m["volume_h24"], 0.8, 4)
    momentum = _avg([
        _band(m["change_h1_pct"], 0, 60),
        _band(m["change_h6_pct"], 0, 150),
        vol_accel,
        _band(m["buy_ratio_h1"], 0.45, 0.7),
    ])
    if momentum is None:
        missing.append("momentum")

    liquidity = _avg([
        _band(m["liquidity_usd"], 15_000, 400_000),
        _band(m["liquidity_to_mcap"], 0.02, 0.25),
        _peak(m["volume_to_liquidity"], 6, 25),
    ])
    if liquidity is None:
        missing.append("liquidity")

    missing += ["social", "onchain", "catalyst"]

    components = {
        "market_structure": (market_structure or 0) * WEIGHTS["market_structure"],
        "momentum": (momentum or 0) * WEIGHTS["momentum"],
        "liquidity": (liquidity or 0) * WEIGHTS["liquidity"],
        "social": 0.0,
        "onchain": 0.0,
        "catalyst": 0.0,
    }

    available = sum(WEIGHTS[k] for k, v in
                    (("market_structure", market_structure), ("momentum", momentum),
                     ("liquidity", liquidity)) if v is not None)
    earned = components["market_structure"] + components["momentum"] + components["liquidity"]
    base = (earned / available) * 100 if available else 0.0

    penalty = 0.0
    if risk["risk_score"] > RISK_PENALTY["ignore_below"]:
        over = ((risk["risk_score"] - RISK_PENALTY["ignore_below"])
                / (100 - RISK_PENALTY["ignore_below"]))
        penalty = over * RISK_PENALTY["max_penalty"]
    if any(f["severity"] == "critical" for f in risk["flags"]):
        penalty = max(penalty, RISK_PENALTY["critical_flag_penalty"])

    components = {k: round(v, 1) for k, v in components.items()}
    components["risk_penalty"] = round(penalty, 1)

    return {
        "overall_score": max(0, min(100, round(base - penalty))),
        "components": components,
        "missing_inputs": missing,
        "weights_version": WEIGHTS_VERSION,
    }


# ── Ein Scanlauf ───────────────────────────────────────────────────────────

def discover(errors: list[str], max_items: int) -> list[Candidate]:
    """Kandidaten aus allen Quellen, dedupliziert über die Contract-Adresse."""
    found: dict[str, Candidate] = {}

    def add(cands: list[Candidate]) -> None:
        for c in cands:
            prev = found.get(c.contract)
            # Ein Token kann mehrere Pools haben — der tiefste zählt.
            if prev is None or (c.market["liquidity_usd"] or 0) > (prev.market["liquidity_usd"] or 0):
                found[c.contract] = c

    for q in DISCOVERY_QUERIES:
        if len(found) >= max_items:
            break
        try:
            add(search(q))
        except Exception as e:
            errors.append(f'Suche "{q}": {e}')

    try:
        addresses = list(dict.fromkeys(boosted() + profiles()))[:25]
        for address in addresses:
            if len(found) >= max_items:
                break
            try:
                add(by_contract(address)[:1])
            except Exception:
                pass  # eine tote Adresse darf den Durchlauf nicht stoppen
    except Exception as e:
        errors.append(f"Boosted/Profiles: {e}")

    return list(found.values())[:max_items]


def run_scan(db_path: str) -> dict:
    """Ein vollständiger Durchlauf. Idempotent: gleiche Token, neue Messpunkte."""
    import crypto_store

    started = time.time()
    deadline = started + LIMITS["max_run_seconds"]
    errors: list[str] = []
    rejections: list[dict] = []

    crypto_store.init_db(db_path)
    candidates = discover(errors, LIMITS["max_discovered"])

    enriched = scored = opportunities = 0

    for c in candidates:
        if time.time() > deadline:
            errors.append("Zeitlimit erreicht — restliche Kandidaten beim nächsten Lauf")
            break
        if enriched >= LIMITS["max_enriched"]:
            break

        verdict = apply_filters(c, FILTERS)
        if verdict:
            rejections.append({"token_id": c.token_id, "rule": verdict[0], "detail": verdict[1]})
            continue

        try:
            crypto_store.upsert_token(db_path, c)
            crypto_store.save_snapshot(db_path, c)
            enriched += 1

            risk = assess_risk(c)
            crypto_store.save_risk(db_path, c.token_id, risk)

            score = score_token(c, risk)
            crypto_store.save_score(db_path, c.token_id, score)
            scored += 1

            if (score["overall_score"] >= THRESHOLDS["opportunity"]
                    and risk["risk_score"] <= THRESHOLDS["max_risk_score"]):
                opportunities += 1
        except Exception as e:
            errors.append(f"{c.symbol or c.contract}: {e}")

    report = {
        "id": f"scan_{uuid.uuid4().hex[:10]}",
        "duration_ms": int((time.time() - started) * 1000),
        "discovered": len(candidates),
        "rejected_by_filters": len(rejections),
        "rejections": rejections[:50],
        "enriched": enriched,
        "scored": scored,
        "opportunities": opportunities,
        "errors": errors,
    }
    crypto_store.save_run(db_path, report)

    logger.info(
        "[SCAN] %s gefunden=%d gefiltert=%d bewertet=%d Chancen=%d Fehler=%d in %dms",
        report["id"], report["discovered"], report["rejected_by_filters"],
        report["scored"], report["opportunities"], len(errors), report["duration_ms"])

    return report
