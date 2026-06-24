import logging
import time
from datetime import datetime, timezone

import anthropic
import tweepy
from apscheduler.schedulers.blocking import BlockingScheduler

import ai_processor
import database
import monitor
import publisher
from config import load_config

logger = logging.getLogger(__name__)


def run_pipeline(cfg, anthropic_client):
    logger.info("=== Pipeline run started ===")
    run_started_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    stats = {"run_at": run_started_at, "fetched": 0, "new_items": 0, "high_relevance": 0, "posted": 0, "errors": 0}

    # 1. Fetch all sources
    all_items = monitor.fetch_all_sources()
    stats["fetched"] = len(all_items)

    # 2. Filter already-seen, insert new
    new_items = []
    for item in all_items:
        if not database.is_seen(cfg.db_path, item["guid"], item["source_id"], item.get("url", ""), item.get("title", "")):
            item_id = database.insert_item(cfg.db_path, item)
            if item_id:
                item["id"] = item_id
                new_items.append(item)
    stats["new_items"] = len(new_items)
    logger.info("New items this run: %d", len(new_items))

    # 3. Score relevance for each new item
    for item in new_items:
        relevance, reason, category, viral_score = ai_processor.score_relevance(anthropic_client, cfg.claude_model, item)
        database.update_relevance(cfg.db_path, item["id"], relevance, reason, category, viral_score)
        item["relevance"] = relevance
        logger.info("[%s] %s → %s (%s)", item["source_id"], item["title"][:60], relevance, reason)
        if relevance == "HIGH":
            stats["high_relevance"] += 1
            post_text = ai_processor.generate_post(anthropic_client, cfg.claude_model, item)
            if post_text:
                database.update_post_text(cfg.db_path, item["id"], post_text)
        time.sleep(0.5)

    # 4. Archive stale queue items and post best unposted HIGH item
    archived = database.archive_stale_queue_items(cfg.db_path, hours=24)
    if archived:
        logger.info("Archived %d stale queue items (>24h old)", archived)
    posted_today = database.get_posted_today_count(cfg.db_path)
    unposted = database.get_unposted_high_items(cfg.db_path)
    if unposted:
        logger.info("Posting %d HIGH items", len(unposted))
        # If many items queued, rank by engagement potential first
        if len(unposted) > 3:
            logger.info("Ranking %d queued items by engagement potential", len(unposted))
            unposted = ai_processor.rank_items_by_potential(anthropic_client, cfg.claude_model, unposted)
        recent_items = database.get_recently_posted_items(cfg.db_path, hours=24)
        to_post = []
        for item in unposted:
            if ai_processor.is_duplicate_topic(anthropic_client, cfg.claude_model, item, recent_items):
                logger.info("[SKIP duplicate topic] %s", item["title"][:60])
                database.update_posted(cfg.db_path, item["id"], "duplicate_topic")
            else:
                recent_items.append({"title": item["title"], "post_text": item.get("post_text", "")})
                to_post.append(item)
        results = publisher.post_batch(cfg, to_post, posted_today=posted_today)
        for item_id, (status, payload) in results.items():
            if status == "ok":
                database.update_posted(cfg.db_path, item_id, payload)
                stats["posted"] += 1
            else:
                database.update_error(cfg.db_path, item_id, payload)
                stats["errors"] += 1

    database.log_run(cfg.db_path, stats)
    logger.info("=== Run complete: %s ===", stats)


def sync_views(cfg):
    """Fetch impression counts from X API and store in DB."""
    try:
        import sqlite3
        conn = sqlite3.connect(cfg.db_path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(seen_items)").fetchall()]
        if "views" not in cols:
            conn.execute("ALTER TABLE seen_items ADD COLUMN views INTEGER")
            conn.commit()

        rows = conn.execute(
            "SELECT id, tweet_id FROM seen_items "
            "WHERE posted_at IS NOT NULL "
            "AND tweet_id NOT IN ('skipped_history','dry_run','duplicate_topic') "
            "AND tweet_id IS NOT NULL "
            "ORDER BY posted_at DESC LIMIT 500"
        ).fetchall()

        if not rows:
            conn.close()
            return

        client = tweepy.Client(
            bearer_token=cfg.x_bearer_token,
            consumer_key=cfg.x_api_key,
            consumer_secret=cfg.x_api_secret,
            access_token=cfg.x_access_token,
            access_token_secret=cfg.x_access_token_secret,
            wait_on_rate_limit=False,
        )

        id_map = {r[1]: r[0] for r in rows}
        updated = 0
        for i in range(0, len(rows), 100):
            batch = list(id_map.keys())[i:i+100]
            try:
                resp = client.get_tweets(
                    ids=batch,
                    tweet_fields=["public_metrics", "non_public_metrics", "organic_metrics"],
                    user_auth=True,
                )
                if resp.data:
                    for tweet in resp.data:
                        views = None
                        if tweet.non_public_metrics:
                            views = tweet.non_public_metrics.get("impression_count")
                        if views is None and tweet.organic_metrics:
                            views = tweet.organic_metrics.get("impression_count")
                        if views is None and tweet.public_metrics:
                            views = tweet.public_metrics.get("impression_count")
                        if views is not None:
                            db_id = id_map.get(str(tweet.id))
                            if db_id:
                                conn.execute("UPDATE seen_items SET views=? WHERE id=?", (views, db_id))
                                updated += 1
            except tweepy.TooManyRequests:
                logger.warning("Views sync: rate limit hit, skipping rest")
                break
            except tweepy.TweepyException as e:
                logger.error("Views sync error: %s", e)
            time.sleep(1)

        conn.commit()
        conn.close()
        logger.info("Views sync: updated %d/%d tweets", updated, len(rows))
    except Exception as e:
        logger.error("Views sync failed: %s", e)


def main():
    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    database.init_db(cfg.db_path)
    anthropic_client = anthropic.Anthropic(api_key=cfg.anthropic_api_key, max_retries=0)

    logger.info("SwissIntel bot starting (dry_run=%s, interval=%dm)", cfg.dry_run, cfg.check_interval_minutes)

    # Run once immediately on startup
    run_pipeline(cfg, anthropic_client)
    sync_views(cfg)

    scheduler = BlockingScheduler()
    scheduler.add_job(
        run_pipeline,
        "interval",
        minutes=cfg.check_interval_minutes,
        args=[cfg, anthropic_client],
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        sync_views,
        "interval",
        hours=6,
        args=[cfg],
        max_instances=1,
        coalesce=True,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down.")


if __name__ == "__main__":
    main()
