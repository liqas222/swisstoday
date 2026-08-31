import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import anthropic
import tweepy
from apscheduler.schedulers.blocking import BlockingScheduler

import ai_processor
import article_fetcher
import database
import monitor
import publisher
from config import load_config

logger = logging.getLogger(__name__)

# Items with viral_score >= this are posted as a thread, else as a single post
THREAD_MIN_SCORE = 75

# Only post items scoring at least this. Volume alone does not grow an account:
# a weak post costs reach on the next one. Raise or lower via .env.
try:
    POST_MIN_SCORE = max(0, int(os.getenv("POST_MIN_SCORE", "60")))
except ValueError:
    POST_MIN_SCORE = 60

# How often to check GitHub for new commits (independent of the pipeline interval).
# Costs nothing but a tiny git fetch; the point is that urgent fixes land fast.
try:
    AUTO_UPDATE_INTERVAL_MINUTES = max(1, int(os.getenv("AUTO_UPDATE_INTERVAL_MINUTES", "15")))
except ValueError:
    AUTO_UPDATE_INTERVAL_MINUTES = 15


REPO_DIR = os.path.dirname(os.path.abspath(__file__))


def _git(*args, timeout=60):
    """Run a git command in the repo dir; returns stripped stdout ('' on failure)."""
    r = subprocess.run(["git", *args], cwd=REPO_DIR, capture_output=True,
                       text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:200]}")
    return r.stdout.strip()


def auto_update():
    """Pull new commits from the tracked branch and restart the services.

    Runs before each pipeline tick so pushes go live without server access.
    Safety: skips when the working tree is dirty, byte-compiles the new code
    before restarting, and rolls back if it doesn't compile.
    Disable by setting AUTO_UPDATE=false in .env.
    """
    if os.getenv("AUTO_UPDATE", "true").strip().lower() not in ("1", "true", "yes"):
        return
    try:
        # -uno: untracked files (logs, db, scratch) must not block a fast-forward;
        # only genuine edits to tracked files do.
        dirty = _git("status", "--porcelain", "-uno")
        if dirty:
            logger.warning("Auto-update skipped: locally modified files: %s",
                           dirty.replace("\n", ", ")[:200])
            return
        branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        if branch == "HEAD":
            logger.warning("Auto-update skipped: detached HEAD")
            return
        _git("fetch", "origin", branch)
        local = _git("rev-parse", "HEAD")
        remote = _git("rev-parse", f"origin/{branch}")
        if local == remote:
            return

        logger.info("Auto-update: %s → %s (%s)", local[:7], remote[:7], branch)
        _git("pull", "--ff-only", "origin", branch)

        # Verify the new code compiles before we restart anything
        py_files = [f for f in os.listdir(REPO_DIR) if f.endswith(".py")]
        check = subprocess.run([sys.executable, "-m", "py_compile", *py_files],
                               cwd=REPO_DIR, capture_output=True, text=True, timeout=120)
        if check.returncode != 0:
            logger.error("Auto-update: new code does not compile, rolling back:\n%s",
                         check.stderr.strip()[:500])
            _git("reset", "--hard", local)
            return

        # Dashboard is a separate process — restart it so it picks up the changes
        subprocess.run(["systemctl", "restart", "swissintel-dash"],
                       capture_output=True, timeout=60)

        # --no-block: systemd queues the restart, so it survives us being killed.
        # Only exit if systemd actually accepted the job — otherwise we would
        # quit with nothing bringing us back.
        r = subprocess.run(["systemctl", "restart", "--no-block", "swissintel-bot"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            logger.info("Auto-update applied — restarting bot")
            raise SystemExit(0)
        logger.error("Auto-update: restart failed (%s) — new code applies on next restart",
                     (r.stderr or "").strip()[:200])
    except SystemExit:
        raise
    except Exception as e:
        logger.warning("Auto-update failed (continuing with current code): %s", e)


def auto_update_job():
    """Scheduler entry point. Swallows the restart signal so APScheduler does not
    log it as a job failure — systemd is already bringing the new code up."""
    try:
        auto_update()
    except SystemExit:
        pass


def run_pipeline(cfg, anthropic_client):
    """Wrapper that guarantees one pipeline failure never kills the scheduler."""
    try:
        _run_pipeline(cfg, anthropic_client)
    except Exception as e:
        logger.exception("Pipeline run crashed (continuing): %s", e)


def _run_pipeline(cfg, anthropic_client):
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

    # 2b. Fetch the actual article. RSS gives us little more than a headline;
    # with the full text the model has real material to judge and write from.
    fetched = 0
    for item in new_items:
        text = article_fetcher.fetch_article_text(item.get("url", ""))
        if text:
            item["article_text"] = text
            fetched += 1
    if new_items:
        logger.info("Artikeltext geholt: %d von %d", fetched, len(new_items))

    # 3. Score relevance for each new item
    for item in new_items:
        try:
            relevance, reason, category, viral_score = ai_processor.score_relevance(anthropic_client, cfg.claude_model, item)
            database.update_relevance(cfg.db_path, item["id"], relevance, reason, category, viral_score)
            item["relevance"] = relevance
            logger.info("[%s] %s → %s (%s)", item["source_id"], item["title"][:60], relevance, reason)
            if relevance == "HIGH":
                stats["high_relevance"] += 1
                # Strong topics (viral_score >= 50) go out as a 3-tweet thread;
                # weaker ones as a single post. Falls back to single if the
                # thread generation fails.
                post_text = None
                if viral_score >= THREAD_MIN_SCORE:
                    post_text = ai_processor.generate_thread(
                        anthropic_client, cfg.claude_model, item, viral_score)
                    if post_text:
                        n = len(post_text.split(ai_processor.THREAD_SEP_TOKEN))
                        logger.info("[THREAD] %s (viral=%d, %d Tweets)",
                                    item["title"][:50], viral_score, n)
                if not post_text:
                    post_text = ai_processor.generate_post(anthropic_client, cfg.claude_model, item)
                if post_text:
                    database.update_post_text(cfg.db_path, item["id"], post_text)
        except Exception as e:
            logger.error("Scoring failed for item %s: %s", item.get("id"), e)
            stats["errors"] += 1
        time.sleep(0.5)

    # 3b. Re-score viral_score for unscored items (queue + recently posted slots)
    unscored = database.get_unscored_queue_items(cfg.db_path)
    unscored_slots = database.get_unscored_posted_items(cfg.db_path)
    to_rescore = (unscored + unscored_slots)[:10]
    for item in to_rescore:
        _, _, _, viral_score = ai_processor.score_relevance(anthropic_client, cfg.claude_model, item)
        if viral_score:
            database.update_viral_score(cfg.db_path, item["id"], viral_score)
            logger.info("[RE-SCORE] %s → viral=%d", item["title"][:50], viral_score)
        time.sleep(0.3)

    # 4. Archive stale queue items and post best unposted HIGH item
    archived = database.archive_stale_queue_items(cfg.db_path, hours=24)
    if archived:
        logger.info("Archived %d stale queue items (>24h old)", archived)
    posted_today = database.get_posted_today_count(cfg.db_path)
    unposted = database.get_unposted_high_items(cfg.db_path)
    if unposted and POST_MIN_SCORE:
        strong = [i for i in unposted if (i.get("viral_score") or 0) >= POST_MIN_SCORE]
        if len(strong) < len(unposted):
            logger.info("Score-Filter: %d von %d Artikeln erreichen %d Punkte nicht — bleiben liegen",
                        len(unposted) - len(strong), len(unposted), POST_MIN_SCORE)
        unposted = strong
    if unposted:
        logger.info("Posting %d HIGH items", len(unposted))
        # If many items queued, rank by engagement potential first
        if len(unposted) > 3:
            logger.info("Ranking %d queued items by engagement potential", len(unposted))
            unposted = ai_processor.rank_items_by_potential(anthropic_client, cfg.claude_model, unposted)
        recent_items = database.get_recently_posted_items(cfg.db_path, hours=72)
        posted_this_window = database.get_posted_this_window_count(cfg.db_path)

        # Post one at a time: a follow-up can only quote the item before it once
        # that one actually has a tweet id. Batching lost that link and turned
        # several reports on the same event into separate standalone posts.
        for item in unposted:
            status, quote_tweet_id = ai_processor.check_topic_overlap(
                anthropic_client, cfg.claude_model, item, recent_items)
            if status == "duplicate":
                logger.info("[SKIP duplicate] %s", item["title"][:60])
                database.update_posted(cfg.db_path, item["id"], "duplicate_topic")
                continue
            if status == "update" and quote_tweet_id:
                logger.info("[UPDATE] %s → quoting %s", item["title"][:60], quote_tweet_id)
                item = {**item, "quote_tweet_id": quote_tweet_id}
                if item.get("post_text") and not item["post_text"].startswith("🔄"):
                    item = {**item, "post_text": "🔄 Update:\n\n" + item["post_text"]}

            results = publisher.post_batch(
                cfg, [item], posted_today=posted_today,
                posted_this_window=posted_this_window)
            if not results:
                break  # a posting limit kicked in — leave the rest queued

            for item_id, (res_status, payload) in results.items():
                if res_status == "ok":
                    database.update_posted(cfg.db_path, item_id, payload)
                    stats["posted"] += 1
                    posted_today += 1
                    posted_this_window += 1
                    # Remember the real tweet id so the next report on this
                    # event can attach to it instead of starting over
                    recent_items.append({
                        "title": item["title"],
                        "post_text": item.get("post_text", ""),
                        "tweet_id": payload,
                    })
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
            "AND tweet_id IS NOT NULL "
            "AND tweet_id GLOB '[0-9]*' "  # only real numeric tweet IDs (skip sentinels)
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
                        counts = []
                        for m in (tweet.public_metrics, tweet.non_public_metrics, tweet.organic_metrics):
                            if m and m.get("impression_count") is not None:
                                counts.append(m["impression_count"])
                        if counts:
                            db_id = id_map.get(str(tweet.id))
                            if db_id:
                                conn.execute("UPDATE seen_items SET views=? WHERE id=?", (max(counts), db_id))
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

    # Pick up any commit pushed while we were down, then run once immediately
    auto_update_job()
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
    # Check for new commits far more often than the pipeline runs, so a push
    # goes live within minutes instead of waiting for the next 15-min tick.
    scheduler.add_job(
        auto_update_job,
        "interval",
        minutes=AUTO_UPDATE_INTERVAL_MINUTES,
        max_instances=1,
        coalesce=True,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down.")


if __name__ == "__main__":
    main()
