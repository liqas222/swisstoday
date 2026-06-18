import hashlib
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from sources import SOURCES

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "SwissIntelBot/1.0 (https://github.com/liqas222/swissintel)"
}
REQUEST_TIMEOUT = 15

# XML namespaces common in RSS/Atom feeds
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
}


def _make_guid(source_id: str, url: str, title: str) -> str:
    raw = f"{source_id}:{url}:{title}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _parse_date(date_str: Optional[str]) -> str:
    if not date_str:
        return datetime.now(timezone.utc).isoformat()
    try:
        return parsedate_to_datetime(date_str).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def _text(el, *tags) -> str:
    for tag in tags:
        # Try direct tag name first (handles plain RSS)
        child = el.find(tag)
        if child is None:
            # Try with common namespaces
            for ns_prefix in ("dc", "atom", "content"):
                child = el.find(f"{{{NS.get(ns_prefix, '')}}}{tag}")
                if child is not None:
                    break
        if child is not None and child.text:
            return child.text.strip()
    return ""


def _strip_html(raw: str) -> str:
    return BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)[:1000]


def _parse_rss(content: bytes, source_id: str) -> list[dict]:
    items = []
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        logger.warning("[%s] XML parse error: %s", source_id, exc)
        return []

    # Detect Atom vs RSS
    is_atom = root.tag == "{http://www.w3.org/2005/Atom}feed"

    if is_atom:
        entries = root.findall("{http://www.w3.org/2005/Atom}entry")
        for entry in entries:
            title = _text(entry, "{http://www.w3.org/2005/Atom}title")
            link_el = entry.find("{http://www.w3.org/2005/Atom}link[@rel='alternate']") \
                      or entry.find("{http://www.w3.org/2005/Atom}link")
            link = (link_el.get("href", "") if link_el is not None else "") or ""
            summary_el = entry.find("{http://www.w3.org/2005/Atom}summary") \
                         or entry.find("{http://www.w3.org/2005/Atom}content")
            summary = _strip_html(summary_el.text or "") if summary_el is not None else ""
            guid_el = entry.find("{http://www.w3.org/2005/Atom}id")
            guid = (guid_el.text.strip() if guid_el is not None else None) or _make_guid(source_id, link, title)
            date_el = entry.find("{http://www.w3.org/2005/Atom}published") \
                      or entry.find("{http://www.w3.org/2005/Atom}updated")
            date_str = date_el.text if date_el is not None else None
            items.append({
                "guid": guid,
                "source_id": source_id,
                "title": title,
                "url": link,
                "summary": summary,
                "published_at": _parse_date(date_str),
            })
    else:
        channel = root.find("channel") or root
        for item in channel.findall("item"):
            title = _text(item, "title")
            link = _text(item, "link")
            summary_raw = _text(item, "description", "{http://purl.org/rss/1.0/modules/content/}encoded")
            summary = _strip_html(summary_raw)
            guid_el = item.find("guid")
            guid = (guid_el.text.strip() if guid_el is not None else None) or _make_guid(source_id, link, title)
            date_str = _text(item, "pubDate", "{http://purl.org/dc/elements/1.1/}date")
            items.append({
                "guid": guid,
                "source_id": source_id,
                "title": title,
                "url": link,
                "summary": summary,
                "published_at": _parse_date(date_str),
            })

    return items


def _fetch_rss(source: dict) -> list[dict]:
    url = source["url"]
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        items = _parse_rss(resp.content, source["id"])
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
        time.sleep(0.3)
    logger.info("Total fetched across all sources: %d", len(all_items))
    return all_items
