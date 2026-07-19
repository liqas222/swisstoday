"""Trending topics without the X API (tier blocks GET trends/place).

Sources:
- trends24.in — mirrors the real X/Twitter trends per country (scraped)
- Google Trends RSS — official feed, trending searches per country

Interface is unchanged: get_trending_topics(cfg) -> ["[SCHWEIZ] ...", "[GLOBAL] ..."].
"""
import re
import time
import html as html_mod
import logging

import requests

from config import Config

logger = logging.getLogger(__name__)

_cache: dict = {"topics": [], "ts": 0}
CACHE_TTL = 900  # 15 min

# No brotli in Accept-Encoding — requests can't decode it without the package
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}


def _trends24(path: str, limit: int) -> list[str]:
    """Scrape X trends from trends24.in ('' = worldwide, 'switzerland' = CH)."""
    url = f"https://trends24.in/{path}/" if path else "https://trends24.in/"
    r = requests.get(url, headers=_HEADERS, timeout=10)
    r.raise_for_status()
    r.encoding = "utf-8"  # server omits charset; requests would guess latin-1
    # First trend-card__list block = the most recent hourly snapshot
    m = re.search(r'trend-card__list(.*?)</ol>', r.text, re.DOTALL)
    if not m:
        return []
    # Only anchors inside list items — skips nav/header links
    names = re.findall(r'<li[^>]*>\s*<a[^>]*>([^<]+)</a>', m.group(1))
    out = []
    for n in names:
        n = html_mod.unescape(n).strip()
        if not n or len(n) > 60 or "trends24" in n.lower():
            continue
        if n not in out:
            out.append(n)
        if len(out) >= limit:
            break
    return out


def _google_trends(geo: str, limit: int) -> list[str]:
    """Official Google Trends RSS: trending searches for a country."""
    url = f"https://trends.google.com/trending/rss?geo={geo}"
    r = requests.get(url, headers=_HEADERS, timeout=10)
    r.raise_for_status()
    titles = re.findall(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", r.text)
    out = []
    for t in titles[1:]:  # first <title> is the feed name
        t = html_mod.unescape(t).strip()
        if t and t not in out:
            out.append(t)
        if len(out) >= limit:
            break
    return out


def get_trending_topics(cfg: Config) -> list[str]:
    now = time.time()
    if now - _cache["ts"] < CACHE_TTL and _cache["topics"]:
        return _cache["topics"]

    topics: list[str] = []
    seen: set[str] = set()

    def add(label: str, names: list[str]):
        for n in names:
            key = n.lower()
            if key not in seen:
                seen.add(key)
                topics.append(f"[{label}] {n}")

    # Switzerland: X trends (trends24) + Google Trends CH
    try:
        add("SCHWEIZ", _trends24("switzerland", 12))
    except Exception as e:
        logger.warning("CH trends (trends24) failed: %s", e)
    try:
        add("SCHWEIZ", _google_trends("CH", 8))
    except Exception as e:
        logger.warning("CH trends (Google) failed: %s", e)

    # Global: worldwide X trends, Google US as fallback
    try:
        add("GLOBAL", _trends24("", 10))
    except Exception as e:
        logger.warning("Global trends (trends24) failed: %s", e)
        try:
            add("GLOBAL", _google_trends("US", 8))
        except Exception as e2:
            logger.warning("Global trends (Google) failed: %s", e2)

    if topics:
        _cache["topics"] = topics
        _cache["ts"] = now
        logger.info("Trends fetched: %d topics", len(topics))
        return topics
    return _cache["topics"]
