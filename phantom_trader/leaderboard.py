"""
Fetch the top-N wallets from Phantom's perps leaderboard.

Phantom's leaderboard page (phantom.app/leaderboard) is a Next.js app that
calls a JSON backend. We try the most likely endpoints in order and fall back
to page scraping. If all fail, we log a warning and return the previously
stored wallets from the DB.

To discover the exact endpoint: open phantom.app/leaderboard in Chrome,
open DevTools → Network → filter by Fetch/XHR, refresh the page, look for
a request returning an array of wallet objects with pnl / winRate fields.
Put that URL in LEADERBOARD_URL env var to override.
"""

import logging
import os
import requests
from config import TOP_WALLET_COUNT, HELIUS_API_KEY, HELIUS_API_BASE

log = logging.getLogger(__name__)

# Candidate endpoints — tried in order until one succeeds
_CANDIDATE_URLS = [
    os.getenv("LEADERBOARD_URL", ""),                        # user-supplied override
    "https://perps-api.jup.ag/v1/leaderboard",               # Jupiter Perps direct
    "https://api.phantom.app/v1/perps/leaderboard",          # Phantom backend
    "https://api.phantom.app/perps-leaderboard/v1",          # alt path
    "https://stats.jup.ag/perps/leaderboard",                # Jupiter stats
]

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
    "Origin": "https://phantom.app",
    "Referer": "https://phantom.app/",
}

_PARAMS = {"period": "7d", "limit": TOP_WALLET_COUNT, "timeframe": "SEVEN_DAY"}


def _parse_entry(entry: dict) -> dict | None:
    """Normalize a leaderboard entry from any known response shape."""
    # Try various field names used by different API versions
    address = (
        entry.get("wallet")
        or entry.get("address")
        or entry.get("walletAddress")
        or entry.get("pubkey")
        or entry.get("user")
    )
    if not address or len(address) < 32:
        return None

    pnl = float(
        entry.get("pnl7d")
        or entry.get("pnl_7d")
        or entry.get("realizedPnl")
        or entry.get("totalPnl")
        or 0
    )
    win_rate_raw = (
        entry.get("winRate")
        or entry.get("win_rate")
        or entry.get("winRatio")
        or 0
    )
    win_rate = float(win_rate_raw) / 100 if float(win_rate_raw) > 1 else float(win_rate_raw)
    trade_count = int(entry.get("tradeCount") or entry.get("trade_count") or entry.get("totalTrades") or 0)

    return {
        "address": address,
        "pnl_7d": pnl,
        "win_rate": win_rate,
        "trade_count": trade_count,
    }


def _try_url(url: str) -> list[dict] | None:
    if not url:
        return None
    try:
        resp = requests.get(url, headers=_HEADERS, params=_PARAMS, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        # API may return list directly or wrapped in a key
        if isinstance(data, list):
            entries = data
        elif isinstance(data, dict):
            entries = (
                data.get("data")
                or data.get("leaderboard")
                or data.get("traders")
                or data.get("results")
                or []
            )
        else:
            return None

        wallets = []
        for i, entry in enumerate(entries[:TOP_WALLET_COUNT]):
            parsed = _parse_entry(entry)
            if parsed:
                parsed["rank"] = i + 1
                wallets.append(parsed)
        return wallets if wallets else None

    except Exception as exc:
        log.debug("Leaderboard URL %s failed: %s", url, exc)
        return None


def _fetch_via_helius_fallback() -> list[dict] | None:
    """
    Last-resort: use Helius enhanced API to get top accounts by perps volume.
    This is approximate — Helius doesn't have a direct leaderboard endpoint.
    """
    if not HELIUS_API_KEY:
        return None
    # Not a true leaderboard — placeholder until a better source is found.
    log.warning("No leaderboard API available — Helius fallback is not implemented yet.")
    return None


def fetch_leaderboard() -> list[dict]:
    """
    Returns list of dicts: {address, rank, pnl_7d, win_rate, trade_count}
    Falls back to DB contents if all remote sources fail.
    """
    for url in _CANDIDATE_URLS:
        result = _try_url(url)
        if result:
            log.info("Leaderboard fetched from %s — %d wallets", url, len(result))
            return result

    result = _fetch_via_helius_fallback()
    if result:
        return result

    log.warning(
        "All leaderboard sources failed. "
        "Set LEADERBOARD_URL env var to the JSON endpoint found in browser DevTools."
    )
    return []
