import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "phantom_trader.db")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS phantom_wallets (
            address       TEXT PRIMARY KEY,
            rank          INTEGER,
            pnl_7d        REAL,
            win_rate      REAL,
            trade_count   INTEGER,
            last_updated  TEXT
        );

        CREATE TABLE IF NOT EXISTS wallet_trades (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet        TEXT NOT NULL,
            token         TEXT,
            side          TEXT,
            size_usd      REAL,
            leverage      REAL,
            entry_price   REAL,
            exit_price    REAL,
            pnl           REAL,
            pnl_pct       REAL,
            loss_peak_min INTEGER,
            opened_at     TEXT,
            closed_at     TEXT,
            tx_sig        TEXT UNIQUE
        );

        CREATE TABLE IF NOT EXISTS copy_signals (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet        TEXT,
            trade_id      INTEGER,
            signal_at     TEXT,
            token         TEXT,
            side          TEXT,
            size_usd      REAL,
            entry_price   REAL,
            our_tx_sig    TEXT,
            status        TEXT DEFAULT 'PENDING'
        );

        CREATE TABLE IF NOT EXISTS paper_state (
            id            INTEGER PRIMARY KEY CHECK (id = 1),
            balance       REAL,
            total_pnl     REAL DEFAULT 0,
            total_trades  INTEGER DEFAULT 0,
            wins          INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_trades_wallet ON wallet_trades(wallet);
        CREATE INDEX IF NOT EXISTS idx_signals_status ON copy_signals(status);
        """)


# ── phantom_wallets ──────────────────────────────────────────────────────────

def upsert_wallet(address, rank, pnl_7d, win_rate, trade_count):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO phantom_wallets (address, rank, pnl_7d, win_rate, trade_count, last_updated)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(address) DO UPDATE SET
                rank=excluded.rank, pnl_7d=excluded.pnl_7d,
                win_rate=excluded.win_rate, trade_count=excluded.trade_count,
                last_updated=excluded.last_updated
        """, (address, rank, pnl_7d, win_rate, trade_count, now))


def get_tracked_wallets():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM phantom_wallets ORDER BY rank").fetchall()
    return [dict(r) for r in rows]


def remove_wallets_not_in(addresses: list[str]):
    if not addresses:
        return
    placeholders = ",".join("?" * len(addresses))
    with get_conn() as conn:
        conn.execute(f"DELETE FROM phantom_wallets WHERE address NOT IN ({placeholders})", addresses)


# ── wallet_trades ─────────────────────────────────────────────────────────────

def insert_trade(wallet, token, side, size_usd, leverage, entry_price, opened_at, tx_sig):
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT OR IGNORE INTO wallet_trades
                (wallet, token, side, size_usd, leverage, entry_price, opened_at, tx_sig)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (wallet, token, side, size_usd, leverage, entry_price, opened_at, tx_sig))
        return cur.lastrowid


def close_trade(tx_sig, exit_price, pnl, pnl_pct, loss_peak_min, closed_at):
    with get_conn() as conn:
        conn.execute("""
            UPDATE wallet_trades
            SET exit_price=?, pnl=?, pnl_pct=?, loss_peak_min=?, closed_at=?
            WHERE tx_sig=?
        """, (exit_price, pnl, pnl_pct, loss_peak_min, closed_at, tx_sig))


def get_wallet_trades(wallet):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM wallet_trades WHERE wallet=? ORDER BY opened_at DESC",
            (wallet,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_closed_trades(wallet):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM wallet_trades WHERE wallet=? AND closed_at IS NOT NULL",
            (wallet,)
        ).fetchall()
    return [dict(r) for r in rows]


# ── copy_signals ─────────────────────────────────────────────────────────────

def insert_signal(wallet, trade_id, token, side, size_usd, entry_price):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO copy_signals (wallet, trade_id, signal_at, token, side, size_usd, entry_price)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (wallet, trade_id, now, token, side, size_usd, entry_price))
        return cur.lastrowid


def update_signal(signal_id, status, our_tx_sig=None):
    with get_conn() as conn:
        conn.execute(
            "UPDATE copy_signals SET status=?, our_tx_sig=? WHERE id=?",
            (status, our_tx_sig, signal_id)
        )


def get_recent_signals(limit=50):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM copy_signals ORDER BY signal_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def has_open_copy_position(token):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM copy_signals WHERE token=? AND status='EXECUTED' ORDER BY signal_at DESC LIMIT 1",
            (token,)
        ).fetchone()
    return row is not None


def count_open_copy_positions():
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as c FROM copy_signals WHERE status='EXECUTED'"
        ).fetchone()
    return row["c"]


def last_copy_time(token) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT signal_at FROM copy_signals WHERE token=? ORDER BY signal_at DESC LIMIT 1",
            (token,)
        ).fetchone()
    return row["signal_at"] if row else None


# ── paper state ───────────────────────────────────────────────────────────────

def init_paper_state(starting_balance: float):
    with get_conn() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO paper_state (id, balance, total_pnl, total_trades, wins)
            VALUES (1, ?, 0, 0, 0)
        """, (starting_balance,))


def get_paper_state():
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM paper_state WHERE id=1").fetchone()
    return dict(row) if row else {}


def update_paper_state(pnl: float, won: bool):
    with get_conn() as conn:
        conn.execute("""
            UPDATE paper_state
            SET balance = balance + ?,
                total_pnl = total_pnl + ?,
                total_trades = total_trades + 1,
                wins = wins + ?
            WHERE id = 1
        """, (pnl, pnl, 1 if won else 0))
