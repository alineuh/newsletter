#!/usr/bin/env python3
"""Build today's digest and post it as an issue in this private repo.

Env: DRY_RUN=1 prints instead of posting; FORCE=1 skips the time/duplicate checks.
"""
import json
import os
import subprocess
from datetime import date, datetime
from zoneinfo import ZoneInfo

START_DATE = date(2026, 10, 10)  # day 1 of words.json
POST_FROM_HOUR = 6               # never post before 06:00 Paris time
ASSIGNEE = "alineuh"
TZ = ZoneInfo("Europe/Paris")


def issue_exists(title):
    out = subprocess.run(
        ["gh", "issue", "list", "--state", "all", "--search", f"{title} in:title",
         "--json", "title", "--limit", "20"],
        capture_output=True, text=True, check=True,
    ).stdout
    return any(i["title"] == title for i in json.loads(out))


def chinese_section(today):
    with open("words.json", encoding="utf-8") as f:
        days = json.load(f)
    raw = max((today - START_DATE).days, 0)
    day = days[raw % len(days)]
    lines = ["### 🇹🇼 Chinese words", ""]
    for n, w in enumerate(day, 1):
        lines += [
            f"**{n}. {w['zh']}** — {w['py']} — *{w['en']}*",
            f"> {w['ex_zh']}",
            f"> {w['ex_py']}",
            f"> {w['ex_en']}",
            "",
        ]
    if raw >= len(days):
        lines += ["> ⚠️ The word list has run out, so these words are repeated. Add more days to words.json.", ""]
    return "\n".join(lines)


def main():
    now = datetime.now(TZ)
    today = now.date()
    title = f"Daily digest {today.isoformat()}"
    force = os.environ.get("FORCE") == "1"
    dry = os.environ.get("DRY_RUN") == "1"

    if not (force or dry):
        if now.hour < POST_FROM_HOUR:
            print("Before 06:00 in Paris, skipping.")
            return
        if issue_exists(title):
            print("Already posted today, skipping.")
            return

    body = "\n".join([
        f"## {today.strftime('%A %d %B %Y')}",
        "",
        chinese_section(today),
        "_Markets and news: coming in the next phase._",
        "",
    ])

    if dry:
        print(title)
        print()
        print(body)
        return

    subprocess.run(
        ["gh", "issue", "create", "--title", title, "--body", body, "--assignee", ASSIGNEE],
        check=True,
    )
    print("Posted:", title)


if __name__ == "__main__":
    main()
