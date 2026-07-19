"""X/Twitter trending topics without the X API (tier blocks GET trends/place).

Both sources mirror the REAL X trends (same list you see in the X app):
- trends24.in       — hourly snapshots per country
- getdaytrends.com  — live X trends per country (fallback)

Interface: get_trending_topics(cfg) -> ["[SCHWEIZ] ...", "[GLOBAL] ..."].
"""
import re
import time
import html as html_mod
import logging

import requests

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


def _clean(names: list[str], limit: int) -> list[str]:
    out = []
    for n in names:
        n = html_mod.unescape(n).strip()
        if not n or len(n) > 60:
            continue
        if any(b in n.lower() for b in ("trends24", "getdaytrends", "archive")):
            continue
        if n not in out:
            out.append(n)
        if len(out) >= limit:
            break
    return out


def _trends24(path: str, limit: int) -> list[str]:
    """X trends from trends24.in ('' = worldwide, 'switzerland' = CH)."""
    url = f"https://trends24.in/{path}/" if path else "https://trends24.in/"
    r = requests.get(url, headers=_HEADERS, timeout=10)
    r.raise_for_status()
    r.encoding = "utf-8"  # server omits charset; requests would guess latin-1
    # First trend-card__list block = the newest hourly snapshot
    m = re.search(r'trend-card__list(.*?)</ol>', r.text, re.DOTALL)
    if not m:
        return []
    names = re.findall(r'<li[^>]*>\s*<a[^>]*>([^<]+)</a>', m.group(1))
    return _clean(names, limit)


def _getdaytrends(path: str, limit: int) -> list[str]:
    """X trends from getdaytrends.com ('' = worldwide, 'switzerland' = CH)."""
    url = f"https://getdaytrends.com/{path}/" if path else "https://getdaytrends.com/"
    r = requests.get(url, headers=_HEADERS, timeout=10)
    r.raise_for_status()
    r.encoding = "utf-8"
    # Trend links look like <a href=".../trend/NAME/" class="string ...">NAME</a>
    names = re.findall(r'href="[^"]*/trend/[^"]*"[^>]*>([^<]+)<', r.text)
    return _clean(names, limit)


def _fetch(label: str, path: str, limit: int) -> list[str]:
    """Try trends24 first, fall back to getdaytrends."""
    for fn in (_trends24, _getdaytrends):
        try:
            names = fn(path, limit)
            if names:
                return names
        except Exception as e:
            logger.warning("%s trends via %s failed: %s", label, fn.__name__, e)
    return []


def get_trending_topics(cfg) -> list[str]:
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

    add("SCHWEIZ", _fetch("CH", "switzerland", 15))
    add("GLOBAL", _fetch("Global", "", 10))

    if topics:
        _cache["topics"] = topics
        _cache["ts"] = now
        logger.info("Trends fetched: %d topics", len(topics))
        return topics
    return _cache["topics"]
