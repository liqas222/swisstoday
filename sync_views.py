"""
Fetches impression/view counts from X API for all posted tweets
and stores them in the DB. Run manually or add to cron.

Usage: python sync_views.py
"""
import sqlite3
import time
import logging

import tweepy
from config import load_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def fetch_views(client: tweepy.Client, tweet_ids: list[str]) -> dict[str, int]:
    """Returns {tweet_id: impression_count}. Fetches in batches of 100."""
    results = {}
    for i in range(0, len(tweet_ids), 100):
        batch = tweet_ids[i:i+100]
        try:
            resp = client.get_tweets(
                ids=batch,
                tweet_fields=["public_metrics", "non_public_metrics", "organic_metrics"],
                user_auth=True,
            )
            if resp.data:
                for tweet in resp.data:
                    # impression_count can live in any of the three metric
                    # blocks depending on API tier; take the highest non-zero.
                    counts = []
                    for m in (tweet.public_metrics, tweet.non_public_metrics, tweet.organic_metrics):
                        if m and m.get("impression_count") is not None:
                            counts.append(m["impression_count"])
                    if counts:
                        results[str(tweet.id)] = max(counts)
            if resp.errors:
                for err in resp.errors:
                    logger.warning("Tweet error: %s", err)
        except tweepy.TooManyRequests:
            logger.warning("Rate limit hit, sleeping 60s")
            time.sleep(60)
        except tweepy.TweepyException as e:
            logger.error("API error: %s", e)
        time.sleep(1)
    return results


def main():
    cfg = load_config()

    # Ensure views column exists
    conn = sqlite3.connect(cfg.db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(seen_items)").fetchall()]
    if "views" not in cols:
        conn.execute("ALTER TABLE seen_items ADD COLUMN views INTEGER")
        conn.commit()
        logger.info("Added views column")

    # Get all real posted tweet IDs
    rows = conn.execute(
        "SELECT id, tweet_id FROM seen_items "
        "WHERE posted_at IS NOT NULL "
        "AND tweet_id IS NOT NULL "
        "AND tweet_id GLOB '[0-9]*' "  # only real numeric tweet IDs (skip sentinels)
        "ORDER BY posted_at DESC LIMIT 500"
    ).fetchall()

    if not rows:
        logger.info("No posted tweets found")
        conn.close()
        return

    logger.info("Fetching views for %d tweets", len(rows))

    client = tweepy.Client(
        bearer_token=cfg.x_bearer_token,
        consumer_key=cfg.x_api_key,
        consumer_secret=cfg.x_api_secret,
        access_token=cfg.x_access_token,
        access_token_secret=cfg.x_access_token_secret,
        wait_on_rate_limit=False,
    )

    id_map = {r[1]: r[0] for r in rows}  # tweet_id -> db_id
    views_map = fetch_views(client, list(id_map.keys()))

    updated = 0
    for tweet_id, views in views_map.items():
        db_id = id_map.get(tweet_id)
        if db_id:
            conn.execute("UPDATE seen_items SET views=? WHERE id=?", (views, db_id))
            updated += 1

    conn.commit()
    conn.close()
    logger.info("Updated views for %d/%d tweets", updated, len(rows))

    if updated == 0:
        logger.warning(
            "No views fetched. This may require Elevated X API access "
            "(non_public_metrics/organic_metrics need user auth with write permissions)."
        )


if __name__ == "__main__":
    main()
