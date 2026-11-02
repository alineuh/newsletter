#!/usr/bin/env python3
"""Build the daily cyber digest and post it as an issue in this private repo.

Phase 1 (this file): no AI. It reads the feeds listed in cyber_config.json, keeps
what is recent, removes duplicates, highlights keyword matches (regulation,
post-quantum) and posts the result. A broken feed is reported in the issue, and if
no feed works the job fails loudly instead of posting an empty digest.

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
MAX_BODY = 60000  # GitHub allows 65,536 characters


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


def line(item, source, show_source=False):
    title = feedlib.clean_text(item["title"], 140) or "(untitled)"
    link = item["link"]
    head = f"[{title}]({link})" if link else title
    meta = " · ".join(x for x in ((source if show_source else ""), item["date"].strftime("%Y-%m-%d") if item["date"] else "") if x)
    desc = feedlib.clean_text(item["desc"], 160)
    text = f"- {head}" + (f" — {meta}" if meta else "")
    if desc and norm(desc) != norm(title):
        text += f"\n  {desc}"
    return text


def build(config, results, now):
    cutoff = now - timedelta(hours=config["window_hours"])
    pool = []  # (source, item) for everything recent, in config order
    for group in config["groups"]:
        for feed in group["feeds"]:
            pool += [(feed["name"], i) for i in recent(results[feed["name"]]["items"], cutoff)]

    # Keyword radars come first; what they show is not repeated below.
    radar_lines, seen = [], set()
    for radar in config["radars"]:
        hits = []
        for source, item in pool:
            key = item["link"] or norm(item["title"])
            text = item["title"] + " " + item["desc"]
            if key not in seen and any(keyword_hit(k, text) for k in radar["keywords"]):
                seen.add(key)
                hits.append((source, item))
            if len(hits) >= config["max_radar_items"]:
                break
        if hits:
            radar_lines.append(f"### {radar['title']}\n\n" + "\n".join(line(i, s, show_source=True) for s, i in hits))

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
                block.append(f"**{feed['name']}**\n" + "\n".join(line(i, feed["name"]) for i in picked))
        if block:
            group_lines.append(f"### {group['title']}\n\n" + "\n\n".join(block))

    ok = sum(1 for r in results.values() if not r["error"])
    total = len(results)
    problems = [f"{name}: {r['error']}" for name, r in results.items() if r["error"]]
    parts = [f"## Cyber digest, {now.astimezone(TZ).strftime('%A %d %B %Y')}",
             f"_{ok} of {total} feeds read · {len(pool)} items in the last {config['window_hours']} hours · headlines only, no AI selection yet_"]
    if ok * 2 < total:
        parts.append("> ⚠️ Fewer than half of the feeds worked today, so this digest is incomplete.")
    parts += radar_lines + group_lines
    if not (radar_lines or group_lines):
        parts.append("_No new items in the window._")
    parts.append("---")
    parts.append("_Feed problems: " + ("none" if not problems else "; ".join(problems)) + "_")
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
    title = f"Cyber digest {paris.date().isoformat()}"
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
