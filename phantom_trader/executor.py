"""
Trade execution.

PAPER_TRADING=true  → simulates the trade and updates virtual balance.
PAPER_TRADING=false → signs and submits a real Jupiter Perps transaction.
"""

import logging
from datetime import datetime, timezone

import database as db
from config import PAPER_TRADING, MAX_POSITION_USD

log = logging.getLogger(__name__)


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
    """Call this when the source wallet closes the position we copied."""
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


# ── Live trading (stub — fill in when going live) ─────────────────────────────

def _execute_live(signal_id: int, trade: dict):
    """
    Real trade execution via Jupiter Perps.

    Requires:
      - WALLET_PRIVATE_KEY env var (base58 Solana private key)
      - solders library for transaction signing
      - Jupiter Perps SDK or REST API for position construction

    Implementation outline:
      1. Build position params (token, side, size, leverage, slippage)
      2. Call Jupiter Perps API to get transaction bytes
      3. Sign with local keypair using solders
      4. Submit via Helius RPC sendTransaction
      5. Confirm and update signal status
    """
    from config import WALLET_PRIVATE_KEY, HELIUS_RPC_URL, MAX_POSITION_USD
    if not WALLET_PRIVATE_KEY:
        log.error("WALLET_PRIVATE_KEY not set — cannot execute live trade")
        db.update_signal(signal_id, status="FAILED")
        return

    try:
        from solders.keypair import Keypair  # type: ignore
        import base58, requests as req

        keypair = Keypair.from_base58_string(WALLET_PRIVATE_KEY)
        size = min(trade.get("size_usd", MAX_POSITION_USD), MAX_POSITION_USD)

        # --- Step 1: Request transaction from Jupiter Perps API ---
        jup_resp = req.post(
            "https://perps-api.jup.ag/v1/order",
            json={
                "wallet": str(keypair.pubkey()),
                "market": trade["token"],
                "side": trade["side"],
                "sizeUsd": size,
                "leverage": trade.get("leverage", 2),
                "slippageBps": 50,
            },
            timeout=10,
        )
        if jup_resp.status_code != 200:
            raise RuntimeError(f"Jupiter API error: {jup_resp.status_code} {jup_resp.text[:200]}")

        tx_data = jup_resp.json()
        tx_bytes = base58.b58decode(tx_data["transaction"])

        # --- Step 2: Sign ---
        from solders.transaction import VersionedTransaction  # type: ignore
        tx = VersionedTransaction.from_bytes(tx_bytes)
        tx.sign([keypair])

        # --- Step 3: Submit ---
        rpc_resp = req.post(
            HELIUS_RPC_URL,
            json={
                "jsonrpc": "2.0", "id": 1,
                "method": "sendTransaction",
                "params": [base58.b58encode(bytes(tx)).decode(), {"encoding": "base58"}],
            },
            timeout=15,
        )
        result = rpc_resp.json()
        if "error" in result:
            raise RuntimeError(f"RPC error: {result['error']}")

        tx_sig = result["result"]
        db.update_signal(signal_id, status="EXECUTED", our_tx_sig=tx_sig)
        log.info("Live trade submitted: %s", tx_sig)

    except ImportError:
        log.error("solders / base58 not installed. Run: pip install solders base58")
        db.update_signal(signal_id, status="FAILED")
    except Exception as exc:
        log.error("Live execution failed (signal #%d): %s", signal_id, exc)
        db.update_signal(signal_id, status="FAILED")
