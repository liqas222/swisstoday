import sqlite3
import logging
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger(__name__)


def init_db(db_path: str) -> None:
    with _connect(db_path) as conn:
        conn.executescript("""
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS seen_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guid TEXT NOT NULL,
                source_id TEXT NOT NULL,
                title TEXT,
                url TEXT,
                summary TEXT,
                published_at TEXT,
                fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
                relevance TEXT,
                relevance_reason TEXT,
                viral_score INTEGER,
                post_text TEXT,
                posted_at TEXT,
                tweet_id TEXT,
                error TEXT,
                category TEXT,
                UNIQUE(guid, source_id)
            );
            CREATE INDEX IF NOT EXISTS idx_category ON seen_items(category);
            CREATE INDEX IF NOT EXISTS idx_posted ON seen_items(posted_at);


            CREATE TABLE IF NOT EXISTS run_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at TEXT DEFAULT CURRENT_TIMESTAMP,
                fetched INTEGER,
                new_items INTEGER,
                high_relevance INTEGER,
                posted INTEGER,
                errors INTEGER
            );

            CREATE TABLE IF NOT EXISTS follower_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT UNIQUE,
                count INTEGER,
                logged_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
    logger.info("Database initialised at %s", db_path)


@contextmanager
def _connect(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _normalize_title(title: str) -> str:
    import re
    t = title.lower().strip()
    t = re.sub(r'\s*[-–|·]\s*\w[\w\s]{0,30}$', '', t)  # strip " - Source Name" suffix
    t = re.sub(r'\s+', ' ', t)
    return t.strip()


def is_seen(db_path: str, guid: str, source_id: str, url: str = "", title: str = "") -> bool:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM seen_items WHERE guid=? AND source_id=?", (guid, source_id)
        ).fetchone()
        if row:
            return True
        if url:
            row = conn.execute(
                "SELECT 1 FROM seen_items WHERE url=?", (url,)
            ).fetchone()
            if row:
                return True
        if title:
            norm = _normalize_title(title)
            rows = conn.execute(
                "SELECT title FROM seen_items WHERE fetched_at > datetime('now', '-24 hours')"
            ).fetchall()
            for r in rows:
                if r[0] and _normalize_title(r[0]) == norm:
                    return True
        return False


def insert_item(db_path: str, item: dict) -> Optional[int]:
    with _connect(db_path) as conn:
        try:
            cur = conn.execute(
                """INSERT INTO seen_items (guid, source_id, title, url, summary, published_at)
                   VALUES (:guid, :source_id, :title, :url, :summary, :published_at)""",
                item,
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


def update_relevance(db_path: str, item_id: int, relevance: str, reason: str, category: str = "Sonstiges", viral_score: int = 0) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE seen_items SET relevance=?, relevance_reason=?, category=?, viral_score=? WHERE id=?",
            (relevance, reason, category, viral_score, item_id),
        )


def update_post_text(db_path: str, item_id: int, post_text: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE seen_items SET post_text=? WHERE id=?", (post_text, item_id)
        )


def get_recently_posted_items(db_path: str, hours: int = 24) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT title, post_text, url, tweet_id FROM seen_items
               WHERE posted_at IS NOT NULL
               AND tweet_id NOT IN ('skipped_history', 'dry_run', 'duplicate_topic')
               AND posted_at > datetime('now', ? || ' hours')""",
            (f"-{hours}",),
        ).fetchall()
        return [dict(r) for r in rows if r[0]]


def get_unscored_posted_items(db_path: str, hours: int = 48) -> list[dict]:
    """Return recently posted items that still have no viral_score."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT id, title, url, summary, source_id FROM seen_items
               WHERE posted_at IS NOT NULL
               AND tweet_id NOT IN ('skipped_history','dry_run','duplicate_topic','archived')
               AND (viral_score IS NULL OR viral_score = 0)
               AND posted_at > datetime('now', ? || ' hours')
               ORDER BY posted_at DESC LIMIT 10""",
            (f"-{hours}",),
        ).fetchall()
        return [dict(r) for r in rows]


def get_unscored_queue_items(db_path: str) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT id, title, url, summary, source_id FROM seen_items
               WHERE relevance='HIGH' AND posted_at IS NULL AND error IS NULL
               AND (viral_score IS NULL OR viral_score = 0)
               ORDER BY id ASC LIMIT 20"""
        ).fetchall()
        return [dict(r) for r in rows]


def update_viral_score(db_path: str, item_id: int, viral_score: int) -> None:
    with _connect(db_path) as conn:
        conn.execute("UPDATE seen_items SET viral_score=? WHERE id=?", (viral_score, item_id))


def get_posted_today_count(db_path: str) -> int:
    with _connect(db_path) as conn:
        row = conn.execute(
            """SELECT COUNT(*) FROM seen_items
               WHERE posted_at > date('now')
               AND tweet_id NOT IN ('skipped_history', 'dry_run', 'duplicate_topic', 'archived')
               AND tweet_id IS NOT NULL"""
        ).fetchone()
        return row[0] if row else 0


def archive_stale_queue_items(db_path: str, hours: int = 24) -> int:
    """Mark unposted HIGH items older than `hours` as archived so the queue doesn't bloat."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            """UPDATE seen_items
               SET posted_at=CURRENT_TIMESTAMP, tweet_id='archived'
               WHERE relevance='HIGH' AND posted_at IS NULL AND error IS NULL
               AND fetched_at < datetime('now', ? || ' hours')""",
            (f"-{hours}",),
        )
        return cur.rowcount


def get_today_slots(db_path: str, max_slots: int = 5) -> list[dict]:
    """Return today's posting slots: posted items (fixed) + best pending queue items."""
    with _connect(db_path) as conn:
        posted = conn.execute(
            """SELECT id, title, post_text, source_id, category, viral_score,
                      relevance_reason, posted_at, tweet_id
               FROM seen_items
               WHERE posted_at IS NOT NULL
               AND tweet_id NOT IN ('skipped_history','dry_run','duplicate_topic','archived')
               AND date(posted_at, '+2 hours') = date('now', '+2 hours')
               ORDER BY posted_at DESC LIMIT ?""",
            (max_slots,),
        ).fetchall()
        posted = list(reversed([dict(r) for r in posted]))

        remaining = max_slots - len(posted)
        pending = []
        if remaining > 0:
            pending = conn.execute(
                """SELECT id, title, post_text, source_id, category, viral_score,
                          relevance_reason, fetched_at
                   FROM seen_items
                   WHERE relevance='HIGH' AND posted_at IS NULL
                   AND error IS NULL AND post_text IS NOT NULL
                   ORDER BY COALESCE(viral_score, 0) DESC, id ASC
                   LIMIT ?""",
                (remaining,),
            ).fetchall()
            pending = [dict(r) for r in pending]

    slots = []
    for i, item in enumerate(posted):
        slots.append({**item, "slot": i + 1, "status": "posted"})
    for i, item in enumerate(pending):
        slots.append({**item, "slot": len(posted) + i + 1, "status": "pending"})
    # Fill remaining empty slots
    for i in range(len(slots), max_slots):
        slots.append({"slot": i + 1, "status": "empty"})
    return slots


def get_unposted_high_items(db_path: str) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT id, title, url, summary, post_text, source_id
               FROM seen_items
               WHERE relevance='HIGH' AND posted_at IS NULL AND error IS NULL AND post_text IS NOT NULL
               ORDER BY id ASC"""
        ).fetchall()
        return [dict(r) for r in rows]


def update_posted(db_path: str, item_id: int, tweet_id: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE seen_items SET posted_at=CURRENT_TIMESTAMP, tweet_id=? WHERE id=?",
            (tweet_id, item_id),
        )


def update_error(db_path: str, item_id: int, error: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE seen_items SET error=? WHERE id=?", (error, item_id)
        )


def log_follower_count(db_path: str, count: int) -> None:
    from datetime import date
    today = date.today().isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO follower_log (date, count) VALUES (?, ?)",
            (today, count),
        )


def log_run(db_path: str, stats: dict) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT INTO run_log (run_at, fetched, new_items, high_relevance, posted, errors)
               VALUES (:run_at, :fetched, :new_items, :high_relevance, :posted, :errors)""",
            stats,
        )
