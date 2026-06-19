import logging
import time

import anthropic
from apscheduler.schedulers.blocking import BlockingScheduler

import ai_processor
import database
import monitor
import publisher
from config import load_config

logger = logging.getLogger(__name__)


def run_pipeline(cfg, anthropic_client):
    logger.info("=== Pipeline run started ===")
    stats = {"fetched": 0, "new_items": 0, "high_relevance": 0, "posted": 0, "errors": 0}

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
        relevance, reason, category = ai_processor.score_relevance(anthropic_client, cfg.claude_model, item)
        database.update_relevance(cfg.db_path, item["id"], relevance, reason, category)
        item["relevance"] = relevance
        logger.info("[%s] %s → %s (%s)", item["source_id"], item["title"][:60], relevance, reason)
        if relevance == "HIGH":
            stats["high_relevance"] += 1
            post_text = ai_processor.generate_post(anthropic_client, cfg.claude_model, item)
            if post_text:
                database.update_post_text(cfg.db_path, item["id"], post_text)
        time.sleep(0.5)

    # 4. Post all unposted HIGH items (skip duplicate topics)
    unposted = database.get_unposted_high_items(cfg.db_path)
    if unposted:
        logger.info("Posting %d HIGH items", len(unposted))
        recent_items = database.get_recently_posted_items(cfg.db_path, hours=24)
        to_post = []
        for item in unposted:
            if ai_processor.is_duplicate_topic(anthropic_client, cfg.claude_model, item, recent_items):
                logger.info("[SKIP duplicate topic] %s", item["title"][:60])
                database.update_posted(cfg.db_path, item["id"], "duplicate_topic")
            else:
                recent_items.append({"title": item["title"], "post_text": item.get("post_text", "")})
                to_post.append(item)
        results = publisher.post_batch(cfg, to_post)
        for item_id, (status, payload) in results.items():
            if status == "ok":
                database.update_posted(cfg.db_path, item_id, payload)
                stats["posted"] += 1
            else:
                database.update_error(cfg.db_path, item_id, payload)
                stats["errors"] += 1

    database.log_run(cfg.db_path, stats)
    logger.info("=== Run complete: %s ===", stats)


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

    scheduler = BlockingScheduler()
    scheduler.add_job(
        run_pipeline,
        "interval",
        minutes=cfg.check_interval_minutes,
        args=[cfg, anthropic_client],
        max_instances=1,
        coalesce=True,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down.")


if __name__ == "__main__":
    main()
