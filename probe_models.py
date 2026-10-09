#!/usr/bin/env python3
"""Probe GitHub Models from the runner.

1. Lists the model catalog (id, publisher, tier, token limits when present).
2. Sends one tiny chat request to a few candidate models and reports the HTTP
   status, the time taken, the reply and any rate-limit headers.
3. Sends bigger and bigger requests to the first model that works, to learn how
   much text one call accepts.

The report goes to stdout and to the job summary. The token is never printed.
Failures are results, not errors: the script always exits normally.
"""
import json
import os
import time
import urllib.error
import urllib.request

BASE = "https://models.github.ai"
API_VERSION = "2022-11-28"
TIMEOUT = 90
MAX_TESTS = 4
PREFERRED = [
    "openai/gpt-4.1-mini",
    "openai/gpt-4o-mini",
    "deepseek/deepseek-v3-0324",
    "deepseek/deepseek-r1",
    "meta/llama-3.3-70b-instruct",
    "mistral-ai/mistral-small-2503",
    "microsoft/phi-4",
]
SIZES = (12000, 24000, 48000)  # characters of filler text


def request(method, path, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "Content-Type": "application/json",
            "User-Agent": "newsletter-probe",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")
    except Exception as e:  # network problems, timeouts
        return 0, {}, type(e).__name__ + ": " + str(e)[:120]


def get_catalog(token):
    status, _, text = request("GET", "/catalog/models", token)
    if status != 200:
        return status, [], text[:300]
    try:
        data = json.loads(text)
    except ValueError:
        return status, [], "catalog was not JSON"
    if isinstance(data, dict):
        data = data.get("models") or data.get("data") or []
    return status, data, ""


def describe(model):
    limits = model.get("limits") or {}
    return {
        "id": model.get("id", "?"),
        "publisher": model.get("publisher", ""),
        "tier": model.get("rate_limit_tier", ""),
        "max_in": limits.get("max_input_tokens", ""),
        "max_out": limits.get("max_output_tokens", ""),
    }


def pick_candidates(ids):
    by_lower = {i.lower(): i for i in ids}
    chosen = [by_lower[p] for p in PREFERRED if p in by_lower][:MAX_TESTS]
    return chosen or list(ids)[:2]


def try_model(token, model):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the single word OK."}],
        "max_tokens": 16,
        "temperature": 0,
    }
    t0 = time.time()
    status, headers, text = request("POST", "/inference/chat/completions", token, body)
    seconds = time.time() - t0
    if status == 200:
        try:
            reply = json.loads(text)["choices"][0]["message"]["content"].strip()[:40]
        except Exception:
            reply = "unexpected response shape"
    else:
        reply = text[:160].replace("\n", " ")
    limits = {k.lower(): v for k, v in headers.items()
              if "ratelimit" in k.lower() or k.lower() == "retry-after"}
    return status, seconds, reply, limits


def size_probe(token, model):
    results = []
    for chars in SIZES:
        filler = ("Headline sample about markets, AI and cybersecurity. " * (chars // 50 + 1))[:chars]
        body = {
            "model": model,
            "messages": [{"role": "user",
                          "content": filler + "\n\nIgnore the text above and reply with the single word OK."}],
            "max_tokens": 8,
            "temperature": 0,
        }
        status, _, text = request("POST", "/inference/chat/completions", token, body)
        results.append((chars, status, "accepted" if status == 200 else text[:140].replace("\n", " ")))
        if status != 200:
            break
    return results


def esc(value):
    return str(value).replace("|", "/").replace("\n", " ")


def table(header, rows):
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(esc(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def main():
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GITHUB_TOKEN is not set")
        return
    out = ["## GitHub Models probe", ""]

    status, catalog, error = get_catalog(token)
    out.append(f"Catalog request: HTTP {status}, {len(catalog)} models. {esc(error)}".strip())
    out.append("")
    rows = [describe(m) for m in catalog]
    if rows:
        out.append(table(["Model", "Publisher", "Tier", "Max input tokens", "Max output tokens"],
                         [[r["id"], r["publisher"], r["tier"], r["max_in"], r["max_out"]] for r in rows]))
        out.append("")

    candidates = pick_candidates([r["id"] for r in rows]) if rows else PREFERRED[:3]
    test_rows, first_ok = [], None
    for model in candidates:
        status, seconds, reply, limits = try_model(token, model)
        if status == 200 and first_ok is None:
            first_ok = model
        test_rows.append([model, status, f"{seconds:.1f}s", reply, json.dumps(limits)])
    out.append("### Tiny test call per model")
    out.append("")
    out.append(table(["Model", "HTTP", "Time", "Reply or error", "Rate-limit headers"], test_rows))
    out.append("")

    if first_ok:
        out.append(f"### Request size probe on {first_ok}")
        out.append("")
        out.append(table(["Characters sent", "HTTP", "Result"],
                         [[c, s, r] for c, s, r in size_probe(token, first_ok)]))
        out.append("")
    else:
        out.append("No model answered a tiny test call, so the size probe was skipped.")
        out.append("")

    report = "\n".join(out)
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(report + "\n")


if __name__ == "__main__":
    main()