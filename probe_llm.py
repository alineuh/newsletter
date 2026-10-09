#!/usr/bin/env python3
"""Probe an OpenAI-compatible LLM provider from the runner.

Environment:
  LLM_API_KEY   the provider's key (a GitHub secret)
  LLM_BASE_URL  for example https://api.groq.com/openai/v1 (a GitHub variable)
  LLM_MODEL     optional; if empty the script only lists the models

1. GET {base}/models        which model ids exist for this key.
2. One tiny chat call       real answer or not, time, token usage, rate-limit headers.
3. Bigger and bigger calls  how much text one request accepts, and how many tokens
                            that is, from the provider's own usage numbers.

Redirects are refused, the key is masked in everything printed, and failures are
results, not errors: the script always exits normally.
"""
import json
import os
import time
import urllib.error
import urllib.request

TIMEOUT = 90
MAX_BYTES = 2_000_000
SIZES = (6000, 12000, 24000, 36000)  # characters of filler text
KEY = ""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # never forward the key to another host


OPENER = urllib.request.build_opener(NoRedirect)


def scrub(text):
    return text.replace(KEY, "***") if KEY else text


def peek(text, n=200):
    return scrub(" ".join(text.split()))[:n]


def hget(headers, name):
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return ""


def request(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; newsletter-probe/1.0)",
        },
    )
    try:
        with OPENER.open(req, timeout=TIMEOUT) as resp:
            return resp.status, dict(resp.headers), resp.read(MAX_BYTES).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read(MAX_BYTES).decode("utf-8", "replace")
    except Exception as e:
        return 0, {}, type(e).__name__ + ": " + str(e)[:120]


def describe(status, headers, text):
    parts = [f"HTTP {status}"]
    if hget(headers, "content-type"):
        parts.append("type: " + hget(headers, "content-type"))
    if hget(headers, "location"):
        parts.append("redirects to: " + hget(headers, "location"))
    parts.append("body: " + (peek(text) or "(empty)"))
    return " | ".join(parts)


def parse_chat(text):
    """Return reply details, or None when this is not a chat completion."""
    try:
        data = json.loads(text)
        choice = data["choices"][0]
        content = choice["message"].get("content")
    except Exception:
        return None
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    usage = data.get("usage") or {}
    return {
        "content": (content or "").strip(),
        "finish": choice.get("finish_reason", ""),
        "prompt": usage.get("prompt_tokens", ""),
        "completion": usage.get("completion_tokens", ""),
    }


def chat(base, model, prompt, max_tokens=256):
    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens, "temperature": 0}
    t0 = time.time()
    status, headers, text = request("POST", base + "/chat/completions", body)
    return status, headers, text, time.time() - t0


def list_models(base):
    status, headers, text = request("GET", base + "/models")
    raw = describe(status, headers, text)
    if status != 200:
        return [], raw
    try:
        data = json.loads(text)
        items = data["data"] if isinstance(data, dict) else data
        return sorted(str(m["id"]) for m in items), raw
    except Exception:
        return [], raw


def rate_headers(headers):
    return {k.lower(): v for k, v in headers.items() if "ratelimit" in k.lower() or k.lower() == "retry-after"}


def esc(value):
    return str(value).replace("|", "/").replace("\n", " ")


def table(header, rows):
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(esc(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def main():
    global KEY
    KEY = os.environ.get("LLM_API_KEY", "")
    base = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    model = os.environ.get("LLM_MODEL", "").strip()
    problems = []
    if not KEY:
        problems.append("LLM_API_KEY is empty: add it as a repository secret")
    if not base:
        problems.append("LLM_BASE_URL is empty: add it as a repository variable")
    elif not (base.startswith("https://") or base.startswith("http://127.0.0.1")):
        problems.append("LLM_BASE_URL must start with https://")
    if problems:
        report = "## LLM probe\n\n" + "\n".join("- " + p for p in problems) + "\n"
        print(report)
        return
    out = ["## LLM probe", "", f"Provider address: {base}", ""]

    models, raw = list_models(base)
    out.append(f"Model list: {len(models)} models. " + ("" if models else "What came back: " + esc(raw)))
    out.append("")
    if models:
        out.append(", ".join(models[:80]) + (" ..." if len(models) > 80 else ""))
        out.append("")
    if not model:
        out.append("LLM_MODEL is empty, so no chat test was run. Pick a model id from the list and set LLM_MODEL.")
        report = scrub("\n".join(out)) + "\n"
        print(report)
        _summary(report)
        return
    out.append(f"Model under test: {model} ({'in the list' if model in models else 'NOT in the list'})")
    out.append("")

    status, headers, text, seconds = chat(base, model, "Reply with the single word OK.")
    parsed = parse_chat(text) if status == 200 else None
    shown = (parsed["content"] or "(empty text)") if parsed else describe(status, headers, text)
    out.append("### Tiny test call")
    out.append("")
    out.append(table(["Real answer", "HTTP", "Time", "Reply or what came back", "Prompt tokens", "Finish", "Rate-limit headers"],
                     [["yes" if parsed else "NO", status, f"{seconds:.1f}s", shown[:200],
                       parsed["prompt"] if parsed else "", parsed["finish"] if parsed else "",
                       json.dumps(rate_headers(headers))]]))
    out.append("")

    if parsed:
        rows = []
        for chars in SIZES:
            filler = ("Headline sample about markets, AI and cybersecurity. " * (chars // 50 + 1))[:chars]
            s, h, t, secs = chat(base, model, filler + "\n\nIgnore the text above and reply with the single word OK.")
            p = parse_chat(t) if s == 200 else None
            rows.append([chars, s, "real answer" if p else describe(s, h, t)[:160],
                         p["prompt"] if p else "", f"{secs:.1f}s"])
            if not p:
                break
        out.append("### Request size probe")
        out.append("")
        out.append(table(["Characters sent", "HTTP", "Result", "Prompt tokens", "Time"], rows))
        out.append("")
    else:
        out.append("No real answer to the tiny call, so the size probe was skipped.")
        out.append("")

    report = scrub("\n".join(out))
    print(report)
    _summary(report)


def _summary(report):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(report + "\n")


if __name__ == "__main__":
    main()
