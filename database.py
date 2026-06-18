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
                post_text TEXT,
                posted_at TEXT,
                tweet_id TEXT,
                error TEXT,
                UNIQUE(guid, source_id)
            );

            CREATE TABLE IF NOT EXISTS run_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at TEXT DEFAULT CURRENT_TIMESTAMP,
                fetched INTEGER,
                new_items INTEGER,
                high_relevance INTEGER,
                posted INTEGER,
                errors INTEGER
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


def update_relevance(db_path: str, item_id: int, relevance: str, reason: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE seen_items SET relevance=?, relevance_reason=? WHERE id=?",
            (relevance, reason, item_id),
        )


def update_post_text(db_path: str, item_id: int, post_text: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE seen_items SET post_text=? WHERE id=?", (post_text, item_id)
        )


def get_recently_posted_titles(db_path: str, hours: int = 6) -> list[str]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT title FROM seen_items
               WHERE posted_at IS NOT NULL AND tweet_id NOT IN ('skipped_history', 'dry_run')
               AND posted_at > datetime('now', ? || ' hours')""",
            (f"-{hours}",),
        ).fetchall()
        return [r[0] for r in rows if r[0]]


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


def log_run(db_path: str, stats: dict) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT INTO run_log (fetched, new_items, high_relevance, posted, errors)
               VALUES (:fetched, :new_items, :high_relevance, :posted, :errors)""",
            stats,
        )
