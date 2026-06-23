"""
Hyperliquid real-time monitoring via WebSocket.

Subscribes to userFills for each top-20 wallet address.
Backfills historical fills via the REST info endpoint.
"""

import json
import logging
import threading
import time
from datetime import datetime, timezone

import requests

import database as db
from config import HL_API_URL, HL_WS_URL

log = logging.getLogger(__name__)

_HEADERS = {"Content-Type": "application/json"}
_subscribed: set[str] = set()
_ws_lock = threading.Lock()
_ws = None   # active websocket-client WebSocketApp


# ── Backfill ──────────────────────────────────────────────────────────────────

def backfill_wallet_history(wallet: str, limit: int = 200):
    """Fetch historical fills for a wallet and store them in the DB."""
    try:
        resp = requests.post(
            f"{HL_API_URL}/info",
            json={"type": "userFills", "user": wallet},
            headers=_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        fills = resp.json()
        if not isinstance(fills, list):
            fills = fills.get("fills", [])

        count = 0
        for fill in fills[:limit]:
            trade = _parse_fill(wallet, fill)
            if trade:
                _save_trade(trade)
                count += 1
        log.info("Backfilled %d fills for %s", count, wallet[:10])
    except Exception as exc:
        log.error("Backfill error for %s: %s", wallet[:10], exc)


# ── WebSocket ─────────────────────────────────────────────────────────────────

def start_ws_listener():
    """Start the WebSocket listener in a background daemon thread."""
    t = threading.Thread(target=_ws_loop, daemon=True, name="hl-ws")
    t.start()


def update_ws_subscriptions(addresses: list[str]):
    """Subscribe to userFills for any new addresses (safe to call repeatedly)."""
    global _ws
    with _ws_lock:
        new = [a for a in addresses if a not in _subscribed]
    if not new:
        return
    ws = _ws
    if ws:
        for addr in new:
            _send_subscribe(ws, addr)
    # _ws_loop will pick up any unsubscribed addresses on next reconnect too


def _ws_loop():
    """Persistent WebSocket loop — reconnects automatically on disconnect."""
    try:
        import websocket  # websocket-client
    except ImportError:
        log.error("websocket-client not installed. Run: pip install websocket-client")
        return

    backoff = 2
    while True:
        try:
            log.info("Connecting to Hyperliquid WebSocket...")
            ws = websocket.WebSocketApp(
                HL_WS_URL,
                on_open=_on_open,
                on_message=_on_message,
                on_error=lambda ws, e: log.warning("WS error: %s", e),
                on_close=lambda ws, c, m: log.info("WS closed (%s)", c),
            )
            global _ws
            _ws = ws
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as exc:
            log.error("WS loop error: %s", exc)
        finally:
            _ws = None

        log.info("WebSocket disconnected — reconnecting in %ds", backoff)
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


def _on_open(ws):
    global _ws
    _ws = ws
    with _ws_lock:
        to_sub = list(_subscribed)
    for addr in to_sub:
        _send_subscribe(ws, addr)
    log.info("WS connected — resubscribed %d wallets", len(to_sub))


def _send_subscribe(ws, addr: str):
    msg = json.dumps({
        "method": "subscribe",
        "subscription": {"type": "userFills", "user": addr},
    })
    try:
        ws.send(msg)
        with _ws_lock:
            _subscribed.add(addr)
        log.debug("Subscribed to userFills for %s", addr[:10])
    except Exception as exc:
        log.warning("Subscribe failed for %s: %s", addr[:10], exc)


def _on_message(ws, raw: str):
    from analyzer import analyze_and_signal
    try:
        msg = json.loads(raw)
        if msg.get("channel") != "userFills":
            return
        data = msg.get("data", {})
        if data.get("isSnapshot"):
            return   # skip historical snapshot delivered at subscribe time

        fills = data.get("fills", [])
        user = data.get("user", "")
        for fill in fills:
            trade = _parse_fill(user, fill)
            if trade:
                trade_id = _save_trade(trade)
                if trade_id and _is_open(fill):
                    analyze_and_signal(user, trade_id, trade)
    except Exception as exc:
        log.error("WS message error: %s", exc)


def subscribe_wallet(address: str):
    """Called from app.py when a new wallet appears on the leaderboard."""
    ws = _ws
    if ws and address not in _subscribed:
        _send_subscribe(ws, address)
    else:
        with _ws_lock:
            _subscribed.add(address)   # will subscribe on next connect


# ── Fill parsing ──────────────────────────────────────────────────────────────

def _is_open(fill: dict) -> bool:
    direction = (fill.get("dir") or "").lower()
    return "open" in direction


def _parse_fill(wallet: str, fill: dict) -> dict | None:
    """
    Hyperliquid fill fields:
      coin, side (B=buy/long, A=ask/short), px, sz, dir, closedPnl, time (ms), hash
    """
    coin = fill.get("coin")
    if not coin:
        return None

    hl_side = fill.get("side", "")
    direction = (fill.get("dir") or "").lower()

    if hl_side == "B":
        side = "LONG"
    elif hl_side == "A":
        side = "SHORT"
    else:
        return None

    is_close = "close" in direction
    price = float(fill.get("px") or 0)
    size_usd = float(fill.get("sz") or 0) * price
    pnl = float(fill.get("closedPnl") or 0) if is_close else None

    ts_ms = fill.get("time")
    if ts_ms:
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
    else:
        dt = datetime.now(timezone.utc).isoformat()

    return {
        "wallet": wallet,
        "token": coin,
        "side": side,
        "size_usd": size_usd,
        "leverage": 1.0,     # HL fills don't carry leverage; analyzer uses it as filter only
        "entry_price": price if not is_close else 0.0,
        "exit_price": price if is_close else None,
        "pnl": pnl,
        "opened_at": dt if not is_close else None,
        "closed_at": dt if is_close else None,
        "tx_sig": fill.get("hash") or fill.get("tid") or "",
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
            opened_at=trade.get("opened_at") or datetime.now(timezone.utc).isoformat(),
            tx_sig=str(trade["tx_sig"]),
        )
    except Exception as exc:
        log.debug("Duplicate or error saving fill %s: %s", trade.get("tx_sig"), exc)
        return None
