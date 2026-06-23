"""
Main Flask application + APScheduler.

Routes:
  GET  /           → leaderboard dashboard
  GET  /wallet/<a> → per-wallet trade history
  GET  /signals    → copy signals log
  GET  /api/status → JSON health check
  POST /webhook    → Helius real-time transaction hook
"""

import logging
import threading
from datetime import datetime, timezone

from flask import Flask, render_template, request, jsonify, abort
from apscheduler.schedulers.background import BackgroundScheduler

import config
import database as db
import leaderboard as lb
import monitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)

_known_wallets: set[str] = set()   # track wallets we've already backfilled
_scheduler: BackgroundScheduler | None = None


# ── Scheduler jobs ────────────────────────────────────────────────────────────

def refresh_leaderboard():
    """Fetch top-20 from Phantom leaderboard, update DB, re-register webhook."""
    global _known_wallets
    log.info("Refreshing leaderboard...")
    entries = lb.fetch_leaderboard()
    if not entries:
        log.warning("Leaderboard returned no data — keeping existing wallets")
        return

    addresses = [e["address"] for e in entries]

    # New wallets get a history backfill
    new_wallets = [a for a in addresses if a not in _known_wallets]
    for addr in new_wallets:
        threading.Thread(target=monitor.backfill_wallet_history, args=(addr,), daemon=True).start()
    _known_wallets.update(addresses)

    # Persist to DB
    for entry in entries:
        db.upsert_wallet(
            address=entry["address"],
            rank=entry["rank"],
            pnl_7d=entry["pnl_7d"],
            win_rate=entry["win_rate"],
            trade_count=entry["trade_count"],
        )
    db.remove_wallets_not_in(addresses)

    # Re-register Helius webhook
    monitor.register_webhook(addresses)
    log.info("Leaderboard updated — tracking %d wallets", len(addresses))


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    wallets = db.get_tracked_wallets()
    signals = db.get_recent_signals(20)
    paper   = db.get_paper_state()
    return render_template("index.html", wallets=wallets, signals=signals, paper=paper,
                           paper_trading=config.PAPER_TRADING, now=_now())


@app.route("/wallet/<address>")
def wallet_detail(address):
    wallets = db.get_tracked_wallets()
    info = next((w for w in wallets if w["address"] == address), None)
    if not info:
        abort(404)
    trades = db.get_wallet_trades(address)
    return render_template("wallet.html", wallet=info, trades=trades, now=_now())


@app.route("/signals")
def signals_page():
    signals = db.get_recent_signals(100)
    paper   = db.get_paper_state()
    return render_template("signals.html", signals=signals, paper=paper, now=_now())


@app.route("/api/status")
def api_status():
    wallets = db.get_tracked_wallets()
    paper   = db.get_paper_state()
    return jsonify({
        "status": "running",
        "paper_trading": config.PAPER_TRADING,
        "tracked_wallets": len(wallets),
        "paper_balance": paper.get("balance"),
        "total_pnl": paper.get("total_pnl"),
        "total_trades": paper.get("total_trades"),
        "timestamp": _now(),
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    # Optional secret header validation
    if config.WEBHOOK_SECRET:
        auth = request.headers.get("Authorization") or request.headers.get("auth-header", "")
        if auth != config.WEBHOOK_SECRET:
            log.warning("Webhook rejected — invalid secret")
            abort(401)

    payload = request.get_json(force=True, silent=True)
    if payload is None:
        abort(400)

    threading.Thread(target=monitor.handle_webhook_payload, args=(payload,), daemon=True).start()
    return jsonify({"ok": True}), 200


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ── Startup ───────────────────────────────────────────────────────────────────

def start():
    db.init_db()
    db.init_paper_state(config.PAPER_BALANCE)

    global _scheduler
    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(refresh_leaderboard, "interval", hours=1, id="leaderboard",
                       next_run_time=datetime.now(timezone.utc))   # run immediately at start
    _scheduler.start()
    log.info(
        "Phantom Copy Trader starting — paper_trading=%s, balance=$%.0f, port=%d",
        config.PAPER_TRADING, config.PAPER_BALANCE, config.DASHBOARD_PORT,
    )

    app.run(host="0.0.0.0", port=config.DASHBOARD_PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    start()
