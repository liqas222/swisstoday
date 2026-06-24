import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional

import tweepy

from config import Config

logger = logging.getLogger(__name__)

SWISS_TZ = ZoneInfo("Europe/Zurich")
MAX_POSTS_PER_DAY = 5

# Peak posting windows: (start_hour, end_hour) Swiss time
PEAK_WINDOWS = [
    (7, 9),
    (12, 14),
    (18, 21),
]


def _swiss_now() -> datetime:
    return datetime.now(SWISS_TZ)


def _is_peak_window() -> bool:
    hour = _swiss_now().hour
    return any(start <= hour < end for start, end in PEAK_WINDOWS)


def _build_client(cfg: Config) -> Optional[tweepy.Client]:
    if not cfg.x_api_key:
        return None
    return tweepy.Client(
        bearer_token=cfg.x_bearer_token,
        consumer_key=cfg.x_api_key,
        consumer_secret=cfg.x_api_secret,
        access_token=cfg.x_access_token,
        access_token_secret=cfg.x_access_token_secret,
        wait_on_rate_limit=False,
    )


def post_tweet(client: tweepy.Client, text: str, dry_run: bool, quote_tweet_id: Optional[str] = None) -> Optional[str]:
    if dry_run:
        if quote_tweet_id:
            logger.info("[DRY RUN] Would quote-tweet %s:\n%s", quote_tweet_id, text)
        else:
            logger.info("[DRY RUN] Would post:\n%s", text)
        return "dry_run"
    kwargs = {"text": text}
    if quote_tweet_id:
        kwargs["quote_tweet_id"] = int(quote_tweet_id)
    try:
        response = client.create_tweet(**kwargs)
        tweet_id = str(response.data["id"])
        logger.info("Posted tweet %s%s", tweet_id, f" (quoting {quote_tweet_id})" if quote_tweet_id else "")
        return tweet_id
    except tweepy.TooManyRequests as exc:
        reset = getattr(exc.response, "headers", {}).get("x-rate-limit-reset")
        if reset:
            sleep_for = max(0, int(reset) - int(time.time())) + 5
            logger.warning("X rate limit hit, sleeping %ds", sleep_for)
            time.sleep(sleep_for)
        try:
            response = client.create_tweet(**kwargs)
            return str(response.data["id"])
        except Exception as retry_exc:
            logger.error("Retry failed: %s", retry_exc)
            raise
    except tweepy.TweepyException as exc:
        logger.error("Tweet failed: %s", exc)
        raise


def post_batch(cfg: Config, items: list[dict], posted_today: int = 0) -> dict[int, tuple[str, Optional[str]]]:
    """Returns {item_id: ('ok'|'error', tweet_id_or_error_msg)}"""
    if not _is_peak_window():
        h = _swiss_now().hour
        logger.info("Outside peak window (%02d:00 Swiss) — %d items queued", h, len(items))
        return {}
    if posted_today >= MAX_POSTS_PER_DAY:
        logger.info("Daily limit (%d) reached — skipping", MAX_POSTS_PER_DAY)
        return {}
    client = _build_client(cfg)
    results = {}
    for item in items[:1]:  # max 1 per run — best item (already ranked) goes first
        item_id = item["id"]
        text = item["post_text"]
        quote_tweet_id = item.get("quote_tweet_id")
        try:
            tweet_id = post_tweet(client, text, cfg.dry_run, quote_tweet_id=quote_tweet_id)
            results[item_id] = ("ok", tweet_id)
        except Exception as exc:
            results[item_id] = ("error", str(exc))
        time.sleep(2)
    return results
