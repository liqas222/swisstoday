import time
import logging
import tweepy
from config import Config

logger = logging.getLogger(__name__)

_cache: dict = {"topics": [], "ts": 0}
CACHE_TTL = 900  # 15 min

SWITZERLAND_WOEID = 23424957
GLOBAL_WOEID = 1


def get_trending_topics(cfg: Config) -> list[str]:
    now = time.time()
    if now - _cache["ts"] < CACHE_TTL and _cache["topics"]:
        return _cache["topics"]
    if not cfg.x_api_key:
        return []
    try:
        auth = tweepy.OAuth1UserHandler(
            cfg.x_api_key, cfg.x_api_secret,
            cfg.x_access_token, cfg.x_access_token_secret,
        )
        api = tweepy.API(auth)
        topics = []
        try:
            ch = api.get_place_trends(id=SWITZERLAND_WOEID)
            topics += [f"[SCHWEIZ] {t['name']}" for t in ch[0]["trends"][:15]]
        except Exception as e:
            logger.warning("CH trends failed: %s", e)
        try:
            gl = api.get_place_trends(id=GLOBAL_WOEID)
            topics += [f"[GLOBAL] {t['name']}" for t in gl[0]["trends"][:10]]
        except Exception as e:
            logger.warning("Global trends failed: %s", e)
        if topics:
            _cache["topics"] = topics
            _cache["ts"] = now
            logger.info("Trends fetched: %d topics", len(topics))
        return topics
    except Exception as e:
        logger.error("Trends fetch error: %s", e)
        return _cache["topics"]
