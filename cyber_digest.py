#!/usr/bin/env python3
"""Build the daily cyber digest (in French) and post it as a GitHub issue in this repo.

Phase 1: no AI. It reads the feeds listed in cyber_config.json, keeps what is recent,
removes duplicates, highlights keyword matches (regulation, post-quantum) and posts
the result. A broken feed is reported in the issue, and if no feed works the job
fails loudly instead of posting an empty digest.

The repo is public, so everything posted here is public: only news headlines, no
personal or employer data.

Env: DRY_RUN=1 prints instead of posting; FORCE=1 skips the time and duplicate checks.
"""
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import feedlib

TZ = ZoneInfo("Europe/Paris")
POST_FROM_HOUR = 6
ASSIGNEE = "alineuh"
LABEL = "cyber"
TITLE_PREFIX = "Veille cyber"
MAX_BODY = 60000  # GitHub allows 65,536 characters

DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
          "septembre", "octobre", "novembre", "décembre"]
FLAGS = {"fr": "🇫🇷", "en": "🇬🇧"}
# Dates repeated at the end of some titles (CERT-FR "02 novembre 2026", SANS "Mon, Nov 2nd")
TRAIL_DATE_RE = re.compile(
    r"[\s,]*(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),?\s+[A-Za-z]{3}\s+\d{1,2}(?:st|nd|rd|th)"
    r"|\d{1,2}(?:er)?\s+(?:" + "|".join(MONTHS) + r")\s+\d{4})\s*$", re.IGNORECASE)


def french_date(dt):
    return f"{DAYS[dt.weekday()]} {dt.day:02d} {MONTHS[dt.month - 1]} {dt.year}"


def issue_exists(title):
    out = subprocess.run(
        ["gh", "issue", "list", "--state", "all", "--search", f"{title} in:title",
         "--json", "title", "--limit", "20"],
        capture_output=True, text=True, check=True).stdout
    return any(i["title"] == title for i in json.loads(out))


def norm(text):
    return re.sub(r"\W+", " ", text.lower()).strip()


def keyword_hit(keyword, text):
    flags = 0 if keyword.isupper() else re.IGNORECASE  # acronyms must match their case
    return re.search(r"(?<!\w)" + re.escape(keyword) + r"(?!\w)", text, flags) is not None


def load_all(config):
    feeds = [f for g in config["groups"] for f in g["feeds"]]
    with ThreadPoolExecutor(max_workers=8) as pool:
        loaded = list(pool.map(lambda f: feedlib.load_feed(f["url"]), feeds))
    return {f["name"]: {"items": items, "error": error} for f, (items, error) in zip(feeds, loaded)}


def recent(items, cutoff):
    kept = [i for i in items if i["date"] is None or i["date"] >= cutoff]
    kept.sort(key=lambda i: i["date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return kept


def label(feed):
    flag = FLAGS.get(feed.get("lang", ""), "")
    return (flag + " " if flag else "") + feed["name"]


def line(item, feed, show_source=False):
    title = feedlib.clean_text(item["title"], 140)
    title = TRAIL_DATE_RE.sub("", title).strip() or "(sans titre)"
    link = item["link"]
    head = f"[{title}]({link})" if link else title
    date = item["date"].strftime("%Y-%m-%d") if item["date"] else ""
    meta = " · ".join(x for x in ((label(feed) if show_source else ""), date) if x)
    desc = feedlib.clean_text(item["desc"], 160)
    text = f"- {head}" + (f" — {meta}" if meta else "")
    if desc and norm(desc) != norm(title):
        text += f"\n  {desc}"
    return text


def build(config, results, now):
    cutoff = now - timedelta(hours=config["window_hours"])
    feeds = [f for g in config["groups"] for f in g["feeds"]]
    pool = []  # (feed, item) for everything recent, in config order
    for feed in feeds:
        pool += [(feed, i) for i in recent(results[feed["name"]]["items"], cutoff)]

    # Keyword radars come first; what they show is not repeated below.
    radar_lines, seen = [], set()
    for radar in config["radars"]:
        excluded = set(radar.get("exclude_sources", []))
        hits = []
        for feed, item in pool:
            if feed["name"] in excluded:
                continue
            key = item["link"] or norm(item["title"])
            text = item["title"] + " " + item["desc"]
            if key not in seen and any(keyword_hit(k, text) for k in radar["keywords"]):
                seen.add(key)
                hits.append((feed, item))
            if len(hits) >= config["max_radar_items"]:
                break
        if hits:
            radar_lines.append(f"### {radar['title']}\n\n" + "\n".join(line(i, f, show_source=True) for f, i in hits))

    group_lines = []
    for group in config["groups"]:
        block = []
        for feed in group["feeds"]:
            picked = []
            for item in recent(results[feed["name"]]["items"], cutoff):
                key = item["link"] or norm(item["title"])
                if key in seen or len(picked) >= feed["max"]:
                    continue
                seen.add(key)
                picked.append(item)
            if picked:
                block.append(f"**{label(feed)}**\n" + "\n".join(line(i, feed) for i in picked))
        if block:
            group_lines.append(f"### {group['title']}\n\n" + "\n\n".join(block))

    ok = sum(1 for r in results.values() if not r["error"])
    total = len(results)
    problems = [f"{name} : {r['error']}" for name, r in results.items() if r["error"]]
    parts = [f"## Veille cyber, {french_date(now.astimezone(TZ))}",
             f"_{ok} flux sur {total} lus · {len(pool)} articles sur les dernières {config['window_hours']} heures · titres seuls, sans sélection par IA_"]
    if ok * 2 < total:
        parts.append("> ⚠️ Moins de la moitié des flux ont répondu aujourd'hui : ce digest est incomplet.")
    parts += radar_lines + group_lines
    if not (radar_lines or group_lines):
        parts.append("_Aucun nouvel article sur la période._")
    parts.append("---")
    parts.append("_Flux en erreur : " + ("aucun" if not problems else " ; ".join(problems)) + "_")
    return "\n\n".join(parts)[:MAX_BODY], ok


def post(title, body):
    label_ok = subprocess.run(
        ["gh", "label", "create", LABEL, "--color", "1d76db", "--description", "Daily cyber digest", "--force"],
        capture_output=True, text=True).returncode == 0
    cmd = ["gh", "issue", "create", "--title", title, "--body-file", "-", "--assignee", ASSIGNEE]
    if label_ok:
        cmd += ["--label", LABEL]
    subprocess.run(cmd, input=body, text=True, check=True)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "cyber_config.json"), encoding="utf-8") as f:
        config = json.load(f)
    now = datetime.now(timezone.utc)
    paris = now.astimezone(TZ)
    title = f"{TITLE_PREFIX} {paris.date().isoformat()}"
    force = os.environ.get("FORCE") == "1"
    dry = os.environ.get("DRY_RUN") == "1"

    if not (force or dry):
        if paris.hour < POST_FROM_HOUR:
            print("Before 06:00 in Paris, skipping.")
            return 0
        if issue_exists(title):
            print("Already posted today, skipping.")
            return 0

    results = load_all(config)
    body, ok = build(config, results, now)
    if ok == 0:
        print("No feed could be read, so nothing is posted. Failing on purpose so you are notified.")
        for name, r in results.items():
            print(f"  {name}: {r['error']}")
        return 1
    if dry:
        print(title + "\n")
        print(body)
        return 0
    post(title, body)
    print("Posted:", title)
    return 0


if __name__ == "__main__":
    sys.exit(main())