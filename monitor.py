import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import feedparser
import requests
from bs4 import BeautifulSoup

from sources import SOURCES

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "SwissIntelBot/1.0 (https://github.com/liqas222/swissintel)"
}
REQUEST_TIMEOUT = 15


def _make_guid(source_id: str, url: str, title: str) -> str:
    raw = f"{source_id}:{url}:{title}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _parse_date(entry) -> Optional[str]:
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            dt = datetime(*t[:6], tzinfo=timezone.utc)
            return dt.isoformat()
    return datetime.now(timezone.utc).isoformat()


def _fetch_rss(source: dict) -> list[dict]:
    url = source["url"]
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        items = []
        for entry in feed.entries:
            link = getattr(entry, "link", "") or ""
            title = getattr(entry, "title", "") or ""
            summary = getattr(entry, "summary", "") or getattr(entry, "description", "") or ""
            guid = getattr(entry, "id", None) or _make_guid(source["id"], link, title)
            items.append({
                "guid": guid,
                "source_id": source["id"],
                "title": title.strip(),
                "url": link.strip(),
                "summary": BeautifulSoup(summary, "html.parser").get_text(" ", strip=True)[:1000],
                "published_at": _parse_date(entry),
            })
        logger.info("[%s] RSS: %d items fetched", source["id"], len(items))
        return items
    except Exception as exc:
        logger.warning("[%s] RSS fetch failed: %s", source["id"], exc)
        return []


def _fetch_scrape(source: dict) -> list[dict]:
    url = source.get("fallback_scrape_url")
    if not url:
        return []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        items = []
        for a in soup.select("a[href]")[:30]:
            href = a["href"]
            if not href.startswith("http"):
                from urllib.parse import urljoin
                href = urljoin(url, href)
            title = a.get_text(strip=True)
            if len(title) < 20:
                continue
            guid = _make_guid(source["id"], href, title)
            items.append({
                "guid": guid,
                "source_id": source["id"],
                "title": title,
                "url": href,
                "summary": "",
                "published_at": datetime.now(timezone.utc).isoformat(),
            })
        logger.info("[%s] scrape fallback: %d items", source["id"], len(items))
        return items
    except Exception as exc:
        logger.warning("[%s] scrape failed: %s", source["id"], exc)
        return []


def fetch_source(source: dict) -> list[dict]:
    items = _fetch_rss(source)
    if not items and source.get("fallback_scrape_url"):
        items = _fetch_scrape(source)
    return items


def fetch_all_sources() -> list[dict]:
    all_items = []
    for source in SOURCES:
        items = fetch_source(source)
        all_items.extend(items)
        time.sleep(0.5)
    logger.info("Total fetched across all sources: %d", len(all_items))
    return all_items
