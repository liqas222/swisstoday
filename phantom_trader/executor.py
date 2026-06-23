"""
Trade execution.

PAPER_TRADING=true  → simulates the trade and updates virtual balance.
PAPER_TRADING=false → opens a real position on Hyperliquid via the Python SDK.
"""

import logging
from datetime import datetime, timezone

import requests

import database as db
from config import PAPER_TRADING, MAX_POSITION_USD, HL_API_URL, HL_PRIVATE_KEY

log = logging.getLogger(__name__)

_HEADERS = {"Content-Type": "application/json"}


def execute(signal_id: int, trade: dict):
    if PAPER_TRADING:
        _execute_paper(signal_id, trade)
    else:
        _execute_live(signal_id, trade)


# ── Paper trading ─────────────────────────────────────────────────────────────

def _execute_paper(signal_id: int, trade: dict):
    size = min(trade.get("size_usd", MAX_POSITION_USD), MAX_POSITION_USD)
    fake_sig = f"PAPER_{signal_id}_{int(datetime.now(timezone.utc).timestamp())}"

    db.update_signal(signal_id, status="EXECUTED", our_tx_sig=fake_sig)
    log.info(
        "[PAPER] Executed copy: %s %s $%.0f (signal #%d)",
        trade.get("side"), trade.get("token"), size, signal_id
    )


def close_paper_position(signal_id: int, exit_price: float, entry_price: float,
                          size_usd: float, leverage: float, side: str):
    if side == "LONG":
        pnl = (exit_price - entry_price) / entry_price * leverage * size_usd
    else:
        pnl = (entry_price - exit_price) / entry_price * leverage * size_usd

    won = pnl > 0
    db.update_paper_state(pnl=pnl, won=won)
    db.update_signal(signal_id, status="CLOSED")
    log.info(
        "[PAPER] Closed position (signal #%d): PnL $%.2f (%s)",
        signal_id, pnl, "WIN" if won else "LOSS"
    )


# ── Live trading via Hyperliquid ──────────────────────────────────────────────

def _execute_live(signal_id: int, trade: dict):
    """
    Opens a real position on Hyperliquid.

    Requires:
      - HL_PRIVATE_KEY env var (EVM 0x... private key)
      - hyperliquid-python-sdk: pip install hyperliquid-python-sdk eth-account
    """
    if not HL_PRIVATE_KEY:
        log.error("HL_PRIVATE_KEY not set — cannot execute live trade")
        db.update_signal(signal_id, status="FAILED")
        return

    try:
        from hyperliquid.exchange import Exchange       # type: ignore
        from hyperliquid.utils import constants        # type: ignore
        from eth_account import Account                # type: ignore

        wallet = Account.from_key(HL_PRIVATE_KEY)
        exchange = Exchange(wallet, constants.MAINNET_API_URL)

        size_usd = min(trade.get("size_usd", MAX_POSITION_USD), MAX_POSITION_USD)
        coin = trade["token"]
        is_buy = trade["side"] == "LONG"

        # Convert USD → coin quantity using current mid price
        price = _get_mid_price(coin)
        if not price:
            raise RuntimeError(f"Could not get price for {coin}")
        size_coins = round(size_usd / price, 5)

        result = exchange.market_open(coin, is_buy, size_coins)

        if result.get("status") == "ok":
            tx_hash = (result.get("response") or {}).get("data", {}).get("statuses", [{}])[0].get("resting", {}).get("oid") or "ok"
            db.update_signal(signal_id, status="EXECUTED", our_tx_sig=str(tx_hash))
            log.info("Live trade executed: %s %s %.5f %s (~$%.0f)", trade["side"], coin, size_coins, coin, size_usd)
        else:
            raise RuntimeError(f"Exchange error: {result}")

    except ImportError:
        log.error("hyperliquid-python-sdk or eth-account not installed. "
                  "Run: pip install hyperliquid-python-sdk eth-account")
        db.update_signal(signal_id, status="FAILED")
    except Exception as exc:
        log.error("Live execution failed (signal #%d): %s", signal_id, exc)
        db.update_signal(signal_id, status="FAILED")


def _get_mid_price(coin: str) -> float | None:
    try:
        resp = requests.post(
            f"{HL_API_URL}/info",
            json={"type": "allMids"},
            headers=_HEADERS,
            timeout=5,
        )
        mids = resp.json()
        return float(mids.get(coin) or mids.get(coin.split("-")[0]) or 0) or None
    except Exception:
        return None
