# Cyber digest

A daily cybersecurity news digest, posted as a GitHub issue every morning (06:15, Paris time). French and English sources are mixed, and each source is marked 🇫🇷 or 🇬🇧.

This repository is public, so every issue is public too. The digest contains news headlines only: no personal data and no employer data.

## What an issue contains

- **Title:** `Veille cyber YYYY-MM-DD`, labelled `cyber`.
- **Keyword radars first** (regulation, post-quantum). An item shown in a radar is not repeated below.
- **Then the groups:** alerts and vulnerabilities, French news, international news, analysis, standards and research. Each source is capped, so a noisy feed cannot drown the others.
- **Last line:** the feeds that failed that day, if any.

Phase 1 has no AI: headlines and short descriptions only, from the last 48 hours.

## How it works

1. `.github/workflows/cyber-digest.yml` runs `cyber_digest.py` every day. There are two cron entries so that 06:15 Paris is hit in both winter and summer time; the script itself refuses to post before 06:00 Paris and posts only once per day.
2. `cyber_digest.py` reads the feeds listed in `cyber_config.json`, keeps recent items, removes duplicates and builds the issue.
3. `feedlib.py` fetches and parses the feeds and cleans their text.

Behaviour on failure:

- A broken feed is listed at the bottom of the issue and the others still work.
- If fewer than half of the feeds answer, the issue carries a warning.
- If no feed answers, the job fails on purpose so GitHub notifies the owner.

## Configuration (`cyber_config.json`)

| Key | Meaning |
|---|---|
| `window_hours` | How far back to look (48 by default). |
| `groups[].title` | Section title in the issue. |
| `groups[].feeds[]` | `name`, `url`, `max` (items shown per day) and `lang` (`fr` or `en`). |
| `radars[]` | `title`, `keywords`, and optionally `exclude_sources` (feed names to ignore for that radar). |
| `max_radar_items` | Cap per radar. |

Keywords written in capitals (`DORA`, `NIS2`) match case-sensitively, so they do not match ordinary words. Other keywords ignore case.

To add a source, add one line to a group in `cyber_config.json`, commit and push. Check on the next run that the feed is not listed as failing.

## Running it

- **On GitHub:** Actions → `cyber-digest` → *Run workflow*. A manual run skips the time and duplicate checks, so running it twice on the same day creates two issues.
- **Locally, without posting:**

```bash
DRY_RUN=1 python3 cyber_digest.py
```

No dependencies are needed beyond Python 3. Posting uses the GitHub CLI (`gh`) inside the workflow.

## Security

- The workflow needs no secret. It uses the automatic `GITHUB_TOKEN` with `contents: read` and `issues: write` only.
- **Feed content is untrusted.** Before it goes into an issue, titles and descriptions are stripped of HTML, URLs and markdown, `@mentions` and `#123` references are defused, and only `http(s)` links are kept.
- Feeds that declare XML entities are refused.
- Never commit secrets, API keys or internal company information to this repository.

## Known limits

- Headlines only; no AI selection or summary yet.
- The CISA advisories feed returns HTTP 403 to automated requests, so it is not used. CERT-FR covers alerts in French.
- The ANSSI news feed publishes only a few items per month, so it rarely appears within a 48-hour window.
- Some feeds change their address or stop publishing; the digest reports them but does not repair them.

## Other files

The other workflows in `.github/workflows/` (`daily.yml`, `check-feeds.yml`, `probe-llm.yml`) and their scripts are separate from the cyber digest and are not described here.
