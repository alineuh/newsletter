#!/usr/bin/env python3
"""Check which candidate news feeds work from this environment.

Reads feeds.json, fetches each feed, and prints a markdown report. When run in
GitHub Actions the report is also written to the job summary. It never fails
the job: a broken feed is a result, not an error.

An entry may have "url" (a feed address) or "handle" (a YouTube @handle whose
channel feed address is looked up from the channel page).

Feeds are untrusted input, so XML that declares entities is refused.
"""
import json
import os
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

UA = "Mozilla/5.0 (compatible; newsletter-feed-check/1.0)"
TIMEOUT = 15
MAX_BYTES = 2_000_000
STALE_DAYS = 7

# Ordered from most to least reliable way of finding a channel's own ID.
CHANNEL_PATTERNS = [
    re.compile(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[0-9A-Za-z_-]{22})"'),
    re.compile(r'"externalId":"(UC[0-9A-Za-z_-]{22})"'),
    re.compile(r'"channelId":"(UC[0-9A-Za-z_-]{22})"'),
]


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


def feed_title(root):
    for child in root:
        name = local(child.tag)
        if name == "title":
            return (child.text or "").strip()
        if name == "channel":
            for c in child:
                if local(c.tag) == "title":
                    return (c.text or "").strip()
    return ""


def parse_feed_full(data):
    """Return (feed title, items) from RSS 2.0, RSS 1.0 or Atom bytes."""
    if b"<!ENTITY" in data:
        raise ValueError("entity declarations refused")
    root = ET.fromstring(data)
    items = []
    for el in root.iter():
        if local(el.tag) in ("item", "entry"):
            title, date = None, None
            for child in el:
                name = local(child.tag)
                if name == "title" and title is None:
                    title = (child.text or "").strip()
                elif name in ("pubDate", "published", "updated", "date") and date is None:
                    date = parse_date(child.text)
            items.append({"title": title or "", "date": date})
    return feed_title(root), items


def parse_feed(data):
    return parse_feed_full(data)[1]


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, text/html, */*"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read(MAX_BYTES)
    except urllib.error.HTTPError as e:
        return e.code, b""


def resolve_handle(handle):
    """Find a YouTube channel's feed address from its @handle page."""
    code, data = fetch("https://www.youtube.com/" + handle.lstrip("/"))
    if code != 200:
        raise ValueError(f"handle page HTTP {code}")
    page = data.decode("utf-8", "replace")
    for pattern in CHANNEL_PATTERNS:
        m = pattern.search(page)
        if m:
            return "https://www.youtube.com/feeds/videos.xml?channel_id=" + m.group(1), m.group(1)
    raise ValueError("channel id not found on the page")


def check(entry):
    row = {"topic": entry["topic"], "name": entry["name"], "access": entry["access"],
           "status_tag": entry["status"], "result": "", "items": "", "newest": "",
           "headline": "", "note": ""}
    url = entry.get("url")
    try:
        if not url and entry.get("handle"):
            url, channel_id = resolve_handle(entry["handle"])
            row["note"] = channel_id
        if not url:
            row["result"] = "URL needed"
            return row
        code, data = fetch(url)
    except Exception as e:  # network errors, timeouts, bad URLs
        row["result"] = "network error: " + (str(e)[:40] if isinstance(e, ValueError) else type(e).__name__)
        return row
    if code != 200:
        row["result"] = f"HTTP {code}"
        return row
    try:
        title, items = parse_feed_full(data)
    except Exception as e:
        row["result"] = "not a valid feed (" + type(e).__name__ + ")"
        return row
    if not items:
        row["result"] = "no items (not a feed?)"
        return row
    if "youtube.com" in url:
        row["note"] = (row["note"] + " " if row["note"] else "") + "channel: " + title[:40]
    dates = [i["date"] for i in items if i["date"]]
    newest = max(dates) if dates else None
    row["items"] = str(len(items))
    row["newest"] = newest.strftime("%Y-%m-%d") if newest else "no dates"
    row["headline"] = items[0]["title"][:60]
    stale = newest is not None and (datetime.now(timezone.utc) - newest).days > STALE_DAYS
    row["result"] = "stale" if stale else "OK"
    return row


def esc(text):
    return str(text).replace("|", "/").replace("\n", " ")


def render(rows):
    ok = sum(1 for r in rows if r["result"] == "OK")
    lines = [f"## Feed check: {ok} OK out of {len(rows)}", "",
             "| Topic | Source | Origin | Access | Result | Items | Newest | First headline | Note |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append("| " + " | ".join(esc(r[k]) for k in
                     ("topic", "name", "status_tag", "access", "result", "items", "newest", "headline", "note")) + " |")
    return "\n".join(lines) + "\n"


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "feeds.json"), encoding="utf-8") as f:
        entries = json.load(f)
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(check, entries))
    report = render(rows)
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(report)


if __name__ == "__main__":
    main()