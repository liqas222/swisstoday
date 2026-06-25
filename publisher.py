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

# Peak posting windows: (start_hour, end_hour, max_per_window) Swiss time
# Spread evenly: 2 morning, 1 midday, 2 evening = 5/day
PEAK_WINDOWS = [
    (7, 9,   2),
    (12, 14, 1),
    (18, 21, 2),
]


def _swiss_now() -> datetime:
    return datetime.now(SWISS_TZ)


def _current_window() -> Optional[tuple[int, int, int]]:
    """Return the active (start, end, max) window, or None if outside all windows."""
    hour = _swiss_now().hour
    for w in PEAK_WINDOWS:
        if w[0] <= hour < w[1]:
            return w
    return None


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


def _build_api_v1(cfg: Config) -> Optional[tweepy.API]:
    """v1.1 API — needed for media upload."""
    if not cfg.x_api_key:
        return None
    auth = tweepy.OAuth1UserHandler(
        cfg.x_api_key, cfg.x_api_secret,
        cfg.x_access_token, cfg.x_access_token_secret,
    )
    return tweepy.API(auth)


def _upload_image(api_v1: tweepy.API, image_bytes: bytes) -> Optional[str]:
    """Upload image bytes and return media_id string, or None on failure."""
    import io
    try:
        media = api_v1.media_upload(filename="post.png", file=io.BytesIO(image_bytes))
        logger.info("Media uploaded: %s", media.media_id_string)
        return media.media_id_string
    except Exception as e:
        logger.warning("Media upload failed (posting without image): %s", e)
        return None


def post_tweet(
    client: tweepy.Client,
    text: str,
    dry_run: bool,
    quote_tweet_id: Optional[str] = None,
    media_id: Optional[str] = None,
) -> Optional[str]:
    if dry_run:
        extra = f" (quoting {quote_tweet_id})" if quote_tweet_id else ""
        extra += f" [image attached]" if media_id else ""
        logger.info("[DRY RUN] Would post%s:\n%s", extra, text)
        return "dry_run"
    kwargs = {"text": text}
    if quote_tweet_id:
        kwargs["quote_tweet_id"] = int(quote_tweet_id)
    if media_id:
        kwargs["media_ids"] = [media_id]
    try:
        response = client.create_tweet(**kwargs)
        tweet_id = str(response.data["id"])
        logger.info("Posted tweet %s%s%s", tweet_id,
                    f" (quoting {quote_tweet_id})" if quote_tweet_id else "",
                    " [+image]" if media_id else "")
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


def post_batch(cfg: Config, items: list[dict], posted_today: int = 0, posted_this_window: int = 0) -> dict[int, tuple[str, Optional[str]]]:
    """Returns {item_id: ('ok'|'error', tweet_id_or_error_msg)}

    posted_this_window: number of tweets already posted in the current time window.
    """
    window = _current_window()
    if not window:
        h = _swiss_now().hour
        logger.info("Outside peak window (%02d:00 Swiss) — %d items queued", h, len(items))
        return {}

    win_start, win_end, win_max = window

    if posted_today >= MAX_POSTS_PER_DAY:
        logger.info("Daily limit (%d) reached — skipping", MAX_POSTS_PER_DAY)
        return {}

    if posted_this_window >= win_max:
        logger.info("Window limit (%d/%d) reached for %02d-%02dh window — waiting for next window",
                    posted_this_window, win_max, win_start, win_end)
        return {}

    client   = _build_client(cfg)
    api_v1   = _build_api_v1(cfg)
    results  = {}

    for item in items[:1]:  # max 1 per run — best item (already ranked) goes first
        item_id       = item["id"]
        text          = item["post_text"]
        quote_tweet_id = item.get("quote_tweet_id")

        # Generate and upload image
        media_id = None
        if not cfg.dry_run and api_v1:
            try:
                import image_gen
                image_bytes = image_gen.generate_post_image(item)
                if image_bytes:
                    media_id = _upload_image(api_v1, image_bytes)
            except Exception as e:
                logger.warning("Image generation failed (posting without image): %s", e)

        try:
            tweet_id = post_tweet(client, text, cfg.dry_run,
                                  quote_tweet_id=quote_tweet_id, media_id=media_id)
            results[item_id] = ("ok", tweet_id)
        except Exception as exc:
            results[item_id] = ("error", str(exc))
        time.sleep(2)
    return results
