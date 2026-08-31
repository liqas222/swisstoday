"""Fetch the full text of a news article.

RSS gives us little more than a headline, which is why generated posts stay thin
and repetitive. This module resolves the link (Google News hides the real URL
behind an encoded id) and pulls the article body so the model has actual
material: figures, names, dates, quotes.
"""
import base64
import json
import logging
import re

import requests

logger = logging.getLogger(__name__)

MAX_ARTICLE_CHARS = 6000
TIMEOUT = 12

# Consent cookies get us past Google's EU interstitial
_GOOGLE_COOKIES = {
    "CONSENT": "YES+cb.20210720-07-p0.en+FX+410",
    "SOCS": "CAISEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg",
}

# No brotli: requests cannot decode it without the brotli package, and the
# body would come back as binary noise.
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://www.google.com/",
    "Upgrade-Insecure-Requests": "1",
}


def _resolve_google_news(url: str) -> str:
    """Turn a news.google.com/rss/articles/<id> link into the real article URL."""
    try:
        art_id = url.split("/articles/")[1].split("?")[0]
    except IndexError:
        return url

    # 1) The id itself usually carries the URL, no network needed
    try:
        padded = art_id + "=" * ((4 - len(art_id) % 4) % 4)
        decoded = base64.urlsafe_b64decode(padded)
        idx = decoded.find(b"http")
        if idx >= 0:
            cand = decoded[idx:].split(b"\x00")[0].split(b" ")[0].decode("utf-8", "ignore")
            if cand.startswith("http") and "google.com" not in cand:
                return cand
    except Exception:
        pass

    # 2) Google's own decode endpoint
    try:
        r = requests.get(url, timeout=TIMEOUT, headers=_HEADERS, cookies=_GOOGLE_COOKIES)
        sig = re.search(r'data-n-a-sg="([^"]+)"', r.text)
        ts = re.search(r'data-n-a-ts="([^"]+)"', r.text)
        if sig and ts:
            payload = [
                "Fbv4je",
                f'["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,'
                f'null,null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],'
                f'"{art_id}",{ts.group(1)},"{sig.group(1)}"]',
            ]
            resp = requests.post(
                "https://news.google.com/_/DotsSplashUi/data/batchexecute",
                headers={**_HEADERS,
                         "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                data="f.req=" + requests.utils.quote(json.dumps([[payload]])),
                timeout=TIMEOUT, cookies=_GOOGLE_COOKIES,
            )
            clean = resp.text.replace("\\/", "/")
            chunk = clean.split("garturlres")[-1] if "garturlres" in clean else clean
            m = re.search(r'(https?://[^\s"\'<>]+)', chunk)
            if m and "google.com" not in m.group(1):
                return m.group(1).rstrip("\\,])")
    except Exception as e:
        logger.debug("Google News decode failed: %s", e)

    # 3) Plain redirect
    try:
        r = requests.get(url.replace("/rss/articles/", "/articles/"), timeout=TIMEOUT,
                         headers=_HEADERS, cookies=_GOOGLE_COOKIES, allow_redirects=True)
        if r.url.startswith("http") and "google.com" not in r.url:
            return r.url
    except Exception as e:
        logger.debug("Google News redirect failed: %s", e)

    return url


_DROP_BLOCKS = re.compile(
    r'<(script|style|noscript|nav|header|footer|aside|form|figure)\b.*?</\1>',
    re.S | re.I)


def _html_to_text(html: str) -> str:
    """Pull the readable body text out of an article page."""
    html = _DROP_BLOCKS.sub(" ", html)
    # Prefer real paragraphs — they are the article, unlike menus and teasers
    paras = re.findall(r'<p\b[^>]*>(.*?)</p>', html, re.S | re.I)
    chunks = []
    for p in paras:
        t = re.sub(r'<[^>]+>', ' ', p)
        t = re.sub(r'\s+', ' ', t).strip()
        if len(t) >= 40:  # skip captions, bylines, cookie notices
            chunks.append(t)
    text = "\n".join(chunks)
    if len(text) < 200:  # no usable <p> markup — fall back to the whole body
        t = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', t).strip()
    import html as _html
    return _html.unescape(text)[:MAX_ARTICLE_CHARS]


def fetch_article_text(url: str) -> str:
    """Full article text, or '' when it cannot be retrieved.

    Never raises: a failed fetch must not stop the pipeline — the caller then
    works with the RSS summary as before.
    """
    if not url:
        return ""
    try:
        if "news.google.com" in url:
            url = _resolve_google_news(url)
            if "news.google.com" in url:
                return ""
        r = requests.get(url, timeout=TIMEOUT, headers=_HEADERS, allow_redirects=True)
        if r.status_code != 200:
            logger.debug("Article fetch %s for %s", r.status_code, url[:80])
            return ""
        r.encoding = r.encoding or "utf-8"
        text = _html_to_text(r.text)
        if len(text) < 200:
            return ""
        logger.debug("Article fetched: %d chars from %s", len(text), url[:60])
        return text
    except Exception as e:
        logger.debug("Article fetch failed for %s: %s", url[:80], e)
        return ""
