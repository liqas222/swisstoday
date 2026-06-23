"""
Trade scoring — decides whether to copy a trade.

All rules must pass (AND logic). Reasons for rejection are logged so the
dashboard can show why a signal was skipped.
"""

import logging
from datetime import datetime, timezone, timedelta

import database as db
import executor
from config import (
    MIN_WIN_RATE, MIN_ROI_RATE, MIN_ROI_THRESHOLD,
    MAX_LOSS_DURATION_MIN, MAX_LEVERAGE,
    MAX_OPEN_POSITIONS, COPY_COOLDOWN_SEC,
)

log = logging.getLogger(__name__)


def score_wallet(wallet: str) -> tuple[bool, str]:
    """
    Evaluate a wallet's historical trades against all quality rules.
    Returns (passes: bool, reason: str).
    """
    wallet_info = _get_wallet_info(wallet)
    if not wallet_info:
        return False, "wallet not in tracked list"

    closed_trades = db.get_closed_trades(wallet)

    # Rule 1: win rate from leaderboard data or computed from history
    win_rate = wallet_info.get("win_rate") or _compute_win_rate(closed_trades)
    if win_rate < MIN_WIN_RATE:
        return False, f"win rate {win_rate:.0%} < {MIN_WIN_RATE:.0%} threshold"

    # Rule 2: 90% of closed trades must have ROI > 30%
    if closed_trades:
        high_roi_count = sum(
            1 for t in closed_trades
            if t.get("pnl_pct") is not None and t["pnl_pct"] > MIN_ROI_THRESHOLD
        )
        high_roi_rate = high_roi_count / len(closed_trades)
        if high_roi_rate < MIN_ROI_RATE:
            return False, (
                f"only {high_roi_rate:.0%} of trades exceed {MIN_ROI_THRESHOLD:.0f}% ROI "
                f"(need {MIN_ROI_RATE:.0%})"
            )

    # Rule 3: no prolonged losing holds
    if closed_trades:
        bad_holds = [
            t for t in closed_trades
            if t.get("loss_peak_min") is not None and t["loss_peak_min"] > MAX_LOSS_DURATION_MIN
        ]
        if bad_holds:
            worst = max(t["loss_peak_min"] for t in bad_holds)
            return False, f"wallet held a losing trade for {worst} min (limit {MAX_LOSS_DURATION_MIN} min)"

    return True, "ok"


def _get_wallet_info(wallet: str) -> dict | None:
    wallets = db.get_tracked_wallets()
    for w in wallets:
        if w["address"] == wallet:
            return w
    return None


def _compute_win_rate(closed_trades: list[dict]) -> float:
    if not closed_trades:
        return 0.0
    wins = sum(1 for t in closed_trades if (t.get("pnl") or 0) > 0)
    return wins / len(closed_trades)


def _check_position_rules(token: str, leverage: float) -> tuple[bool, str]:
    """Runtime risk checks at signal time."""
    if leverage > MAX_LEVERAGE:
        return False, f"leverage {leverage}x > max {MAX_LEVERAGE}x"

    if db.count_open_copy_positions() >= MAX_OPEN_POSITIONS:
        return False, f"already at max {MAX_OPEN_POSITIONS} open positions"

    if db.has_open_copy_position(token):
        return False, f"already have an open copy position for {token}"

    last = db.last_copy_time(token)
    if last:
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
        if elapsed < COPY_COOLDOWN_SEC:
            return False, f"cooldown: {int(COPY_COOLDOWN_SEC - elapsed)}s remaining for {token}"

    return True, "ok"


def analyze_and_signal(wallet: str, trade_id: int, trade: dict):
    """
    Called by monitor.py after a new trade is detected.
    If all rules pass, inserts a copy signal and triggers the executor.
    """
    wallet_ok, wallet_reason = score_wallet(wallet)
    if not wallet_ok:
        log.info("Skip copy for %s: %s", wallet[:8], wallet_reason)
        return

    position_ok, position_reason = _check_position_rules(
        trade.get("token", ""), trade.get("leverage", 1)
    )
    if not position_ok:
        log.info("Skip copy for %s/%s: %s", wallet[:8], trade.get("token"), position_reason)
        return

    signal_id = db.insert_signal(
        wallet=wallet,
        trade_id=trade_id,
        token=trade["token"],
        side=trade["side"],
        size_usd=trade["size_usd"],
        entry_price=trade["entry_price"],
    )
    log.info(
        "Copy signal #%d: %s %s %s @ $%.2f (wallet %s)",
        signal_id, trade["side"], trade["token"],
        f"${trade['size_usd']:.0f}", trade["entry_price"], wallet[:8]
    )

    executor.execute(signal_id, trade)
