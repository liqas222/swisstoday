import logging
import time
from typing import Optional

import tweepy

from config import Config

logger = logging.getLogger(__name__)


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


def post_tweet(client: tweepy.Client, text: str, dry_run: bool) -> Optional[str]:
    if dry_run:
        logger.info("[DRY RUN] Would post:\n%s", text)
        return "dry_run"
    try:
        response = client.create_tweet(text=text)
        tweet_id = str(response.data["id"])
        logger.info("Posted tweet %s", tweet_id)
        return tweet_id
    except tweepy.TooManyRequests as exc:
        reset = getattr(exc.response, "headers", {}).get("x-rate-limit-reset")
        if reset:
            sleep_for = max(0, int(reset) - int(time.time())) + 5
            logger.warning("X rate limit hit, sleeping %ds", sleep_for)
            time.sleep(sleep_for)
        try:
            response = client.create_tweet(text=text)
            return str(response.data["id"])
        except Exception as retry_exc:
            logger.error("Retry failed: %s", retry_exc)
            raise
    except tweepy.TweepyException as exc:
        logger.error("Tweet failed: %s", exc)
        raise


def post_batch(cfg: Config, items: list[dict]) -> dict[int, tuple[str, Optional[str]]]:
    """Returns {item_id: ('ok'|'error', tweet_id_or_error_msg)}"""
    client = _build_client(cfg)
    results = {}
    for item in items:
        item_id = item["id"]
        text = item["post_text"]
        try:
            tweet_id = post_tweet(client, text, cfg.dry_run)
            results[item_id] = ("ok", tweet_id)
        except Exception as exc:
            results[item_id] = ("error", str(exc))
        time.sleep(2)
    return results
