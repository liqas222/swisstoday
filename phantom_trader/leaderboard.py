"""
Fetch the top-N wallets from the Hyperliquid perps leaderboard.

Phantom Perps are powered by Hyperliquid (not Jupiter/Solana).
The leaderboard data is available from the public Hyperliquid info endpoint
with no API key required.
"""

import logging
import requests
from config import HL_API_URL, TOP_WALLET_COUNT

log = logging.getLogger(__name__)

_HEADERS = {"Content-Type": "application/json"}


def fetch_leaderboard() -> list[dict]:
    """
    Returns list of dicts: {address, rank, pnl_7d, win_rate, trade_count}
    """
    try:
        resp = requests.post(
            f"{HL_API_URL}/info",
            json={"type": "leaderboard", "timeWindow": "week"},
            headers=_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.error("Leaderboard fetch failed: %s", exc)
        return []

    # Response may be a list or {"leaderboardRows": [...]}
    rows = data if isinstance(data, list) else (
        data.get("leaderboardRows")
        or data.get("leaderboard")
        or data.get("data")
        or []
    )

    wallets = []
    for i, row in enumerate(rows[:TOP_WALLET_COUNT]):
        parsed = _parse_row(row, rank=i + 1)
        if parsed:
            wallets.append(parsed)

    if wallets:
        log.info("Leaderboard fetched — %d wallets", len(wallets))
    else:
        log.warning("Leaderboard returned no parseable rows")

    return wallets


def _parse_row(row: dict, rank: int) -> dict | None:
    address = (
        row.get("ethAddress")
        or row.get("address")
        or row.get("user")
    )
    if not address or len(address) < 10:
        return None

    # windowPnl is the 7d PnL for timeWindow="week"
    pnl_7d = float(
        row.get("windowPnl")
        or row.get("pnl7d")
        or row.get("pnl_7d")
        or (row.get("pnl") or {}).get("allTime")
        or 0
    )

    # win_rate is not in the leaderboard response; analyzer re-computes from fills
    win_rate = float(row.get("win_rate") or row.get("winRate") or 0)
    trade_count = int(row.get("tradeCount") or row.get("trade_count") or row.get("vlm") and 0 or 0)

    return {
        "address": address,
        "rank": rank,
        "pnl_7d": pnl_7d,
        "win_rate": win_rate,
        "trade_count": trade_count,
    }
