"""Shared helpers: fetch and parse news feeds safely, and clean untrusted text
before it goes into a GitHub issue.

Feed content is untrusted. Titles and descriptions are stripped of HTML, URLs and
markdown characters, GitHub @mentions and #123 references are defused, and only
http(s) links are kept.
"""
import html
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

UA = "Mozilla/5.0 (compatible; newsletter-digest/1.0)"
TIMEOUT = 15
MAX_BYTES = 2_000_000
TAG_RE = re.compile(r"<[^>]*>")
URL_RE = re.compile(r"https?://\S+")
MD_RE = re.compile(r"[\[\]()<>`*_~|]")
SCHEME_RE = re.compile(r"\b(?:javascript|vbscript|data):", re.IGNORECASE)
ZWSP = "\u200b"


def local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_date(text):
    if not text:
        return None
    text = text.strip()
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def clean_text(text, limit=200):
    """Plain, harmless text: no HTML, URLs, markdown, mentions or issue references."""
    text = html.unescape(TAG_RE.sub(" ", text or ""))
    text = URL_RE.sub(" ", text)
    text = SCHEME_RE.sub(" ", text)
    text = MD_RE.sub(" ", text)
    text = " ".join(text.split())
    text = text.replace("@", "@" + ZWSP)
    text = re.sub(r"#(?=\d)", "#" + ZWSP, text)
    return text[:limit].rstrip()


def safe_link(url):
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme in ("http", "https") and parsed.netloc and " " not in url and ")" not in url and "(" not in url:
        return url
    return None


def parse_items(data):
    """Return [{'title','link','desc','date'}] from RSS 2.0, RSS 1.0 or Atom bytes."""
    if b"<!ENTITY" in data:
        raise ValueError("entity declarations refused")
    root = ET.fromstring(data)
    items = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        title = link = desc = date = None
        for child in el:
            name = local(child.tag)
            if name == "title" and title is None:
                title = child.text or ""
            elif name == "link" and link is None:
                rel = child.attrib.get("rel", "alternate")
                candidate = child.attrib.get("href") if child.attrib.get("href") else child.text
                if rel == "alternate" and safe_link(candidate):
                    link = safe_link(candidate)
            elif name in ("description", "summary", "content", "encoded") and not desc:
                desc = child.text or ""
            elif name in ("pubDate", "published", "updated", "date") and date is None:
                date = parse_date(child.text)
        items.append({"title": title or "", "link": link, "desc": desc or "", "date": date})
    return items


def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read(MAX_BYTES)
    except urllib.error.HTTPError as e:
        return e.code, b""


def load_feed(url):
    """Return (items, error). Exactly one of them is meaningful."""
    try:
        status, data = fetch(url)
    except Exception as e:
        return [], "network error: " + type(e).__name__
    if status != 200:
        return [], f"HTTP {status}"
    try:
        items = parse_items(data)
    except ValueError as e:
        if "entity" in str(e):
            return [], "refused: feed declares XML entities"
        return [], "not a valid feed (ValueError)"
    except Exception as e:
        return [], "not a valid feed (" + type(e).__name__ + ")"
    if not items:
        return [], "no items"
    return items, ""
