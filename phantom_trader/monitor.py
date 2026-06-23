"""
Helius webhook management.

Registers the top-20 wallet addresses with Helius so that Helius POSTs to
WEBHOOK_URL whenever any of those wallets makes a transaction. Also provides
the Flask endpoint handler that parses incoming webhook payloads.

Webhook registration docs:
  https://docs.helius.dev/webhooks-and-websockets/what-are-webhooks
"""

import logging
import requests
from datetime import datetime, timezone

import database as db
from config import HELIUS_API_KEY, HELIUS_API_BASE, WEBHOOK_URL, WEBHOOK_SECRET, JUPITER_PERPS_PROGRAM

log = logging.getLogger(__name__)

_webhook_id: str | None = None   # Helius webhook ID, stored in memory


# ── Helius registration ───────────────────────────────────────────────────────

def _helius_headers():
    return {"Content-Type": "application/json"}


def register_webhook(wallet_addresses: list[str]) -> str | None:
    """Create or update the Helius webhook to watch the given addresses."""
    global _webhook_id
    if not HELIUS_API_KEY or not WEBHOOK_URL:
        log.warning("HELIUS_API_KEY or WEBHOOK_URL not set — skipping webhook registration")
        return None

    payload = {
        "webhookURL": WEBHOOK_URL,
        "transactionTypes": ["ANY"],
        "accountAddresses": wallet_addresses,
        "webhookType": "enhanced",
        "authHeader": WEBHOOK_SECRET or None,
    }

    if _webhook_id:
        # Update existing
        url = f"{HELIUS_API_BASE}/webhooks/{_webhook_id}?api-key={HELIUS_API_KEY}"
        resp = requests.put(url, json=payload, headers=_helius_headers(), timeout=10)
    else:
        # Create new
        url = f"{HELIUS_API_BASE}/webhooks?api-key={HELIUS_API_KEY}"
        resp = requests.post(url, json=payload, headers=_helius_headers(), timeout=10)

    if resp.status_code in (200, 201):
        data = resp.json()
        _webhook_id = data.get("webhookID") or data.get("id")
        log.info("Helius webhook registered (id=%s) watching %d wallets", _webhook_id, len(wallet_addresses))
        return _webhook_id
    else:
        log.error("Helius webhook registration failed: %s %s", resp.status_code, resp.text[:200])
        return None


# ── Historical trade backfill ─────────────────────────────────────────────────

def backfill_wallet_history(wallet: str, limit: int = 100):
    """
    Fetch the last `limit` transactions for a wallet via Helius and parse any
    Jupiter Perps trades. Called when a wallet first appears on the leaderboard
    so the analyzer has enough history to make copy decisions.
    """
    if not HELIUS_API_KEY:
        return

    url = f"{HELIUS_API_BASE}/addresses/{wallet}/transactions"
    params = {"api-key": HELIUS_API_KEY, "limit": limit, "type": "ANY"}
    try:
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code != 200:
            log.warning("Backfill failed for %s: %s", wallet, resp.status_code)
            return
        txns = resp.json()
        count = 0
        for txn in txns:
            if _is_perps_transaction(txn):
                trade = _parse_perps_trade(wallet, txn)
                if trade:
                    _save_trade(trade)
                    count += 1
        log.info("Backfilled %d perps trades for %s", count, wallet)
    except Exception as exc:
        log.error("Backfill error for %s: %s", wallet, exc)


# ── Webhook payload handler ───────────────────────────────────────────────────

def handle_webhook_payload(payload: dict | list):
    """
    Called by the Flask route when Helius POSTs a transaction.
    payload may be a single transaction dict or a list of transactions.
    """
    from analyzer import analyze_and_signal

    transactions = payload if isinstance(payload, list) else [payload]
    for txn in transactions:
        try:
            fee_payer = txn.get("feePayer", "")
            if not _is_perps_transaction(txn):
                continue

            trade = _parse_perps_trade(fee_payer, txn)
            if trade:
                trade_id = _save_trade(trade)
                if trade_id and trade.get("side"):   # only signal on open
                    analyze_and_signal(fee_payer, trade_id, trade)
        except Exception as exc:
            log.error("Error handling webhook payload: %s", exc)


# ── Transaction parsing ───────────────────────────────────────────────────────

def _is_perps_transaction(txn: dict) -> bool:
    instructions = txn.get("instructions", [])
    for instr in instructions:
        if instr.get("programId") == JUPITER_PERPS_PROGRAM:
            return True
        for inner in instr.get("innerInstructions", []):
            if inner.get("programId") == JUPITER_PERPS_PROGRAM:
                return True
    # Also check via source field
    return txn.get("source") in ("JUPITER", "JUPITER_PERPS")


def _parse_perps_trade(wallet: str, txn: dict) -> dict | None:
    """
    Extract trade fields from a Helius enhanced transaction.
    Best-effort — Jupiter Perps instruction layouts can change.
    """
    sig = txn.get("signature", "")
    ts = txn.get("timestamp")
    opened_at = (
        datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts
        else datetime.now(timezone.utc).isoformat()
    )

    token = None
    side = None
    size_usd = 0.0
    leverage = 1.0
    entry_price = 0.0

    # Helius enhanced transactions include a description and events
    desc = txn.get("description", "").lower()
    events = txn.get("events", {})
    perps_event = events.get("perps") or events.get("perpetuals")

    if perps_event:
        token       = perps_event.get("market") or perps_event.get("token") or "UNKNOWN"
        side        = (perps_event.get("side") or perps_event.get("direction") or "").upper()
        size_usd    = float(perps_event.get("sizeUsd") or perps_event.get("size") or 0)
        leverage    = float(perps_event.get("leverage") or 1)
        entry_price = float(perps_event.get("price") or perps_event.get("entryPrice") or 0)
    else:
        # Fallback: infer from description text
        if "long" in desc:
            side = "LONG"
        elif "short" in desc:
            side = "SHORT"
        # Try to find token from account keys
        for instr in txn.get("instructions", []):
            if instr.get("programId") == JUPITER_PERPS_PROGRAM:
                accounts = instr.get("accounts", [])
                if accounts:
                    token = accounts[0][:8] + "..."  # partial address as placeholder

    if not side or not token:
        return None

    return {
        "wallet": wallet,
        "token": token,
        "side": side,
        "size_usd": size_usd,
        "leverage": leverage,
        "entry_price": entry_price,
        "opened_at": opened_at,
        "tx_sig": sig,
    }


def _save_trade(trade: dict) -> int | None:
    try:
        return db.insert_trade(
            wallet=trade["wallet"],
            token=trade["token"],
            side=trade["side"],
            size_usd=trade["size_usd"],
            leverage=trade["leverage"],
            entry_price=trade["entry_price"],
            opened_at=trade["opened_at"],
            tx_sig=trade["tx_sig"],
        )
    except Exception as exc:
        log.debug("Duplicate or error saving trade %s: %s", trade.get("tx_sig"), exc)
        return None
