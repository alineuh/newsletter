#!/usr/bin/env python3
"""Probe GitHub Models from the runner.

1. Lists the model catalog (id, publisher, tier, token limits when present).
2. Sends one tiny chat request to a few candidate models and reports the HTTP
   status, the time taken, the reply and any rate-limit headers.
3. Sends bigger and bigger requests to the first model that works, to learn how
   much text one call accepts.
4. Shows a raw peek (status, content type, redirect target, first characters)
   of the catalog answer and of the first chat answer, so odd answers can be
   understood.

5. Runs a diagnosis (DNS and TLS of the host, header variants, a plain curl) so
   that an odd answer can be traced to its source.

Redirects are never followed, so the Authorization header can never be forwarded
to another host. The token is never printed. Failures are results, not errors.
"""
import json
import os
import shutil
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

BASE = "https://models.github.ai"
API_VERSION = "2022-11-28"
TIMEOUT = 90
MAX_BYTES = 2_000_000
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


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # a 3xx surfaces as an error we can report


OPENER = urllib.request.build_opener(NoRedirect)


def hget(headers, name):
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return ""


def request(method, path, token, body=None, version=API_VERSION):
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "newsletter-probe",
    }
    if version:
        headers["X-GitHub-Api-Version"] = version
    url = path if path.startswith("https://") else BASE + path
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with OPENER.open(req, timeout=TIMEOUT) as resp:
            return resp.status, dict(resp.headers), resp.read(MAX_BYTES).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read(MAX_BYTES).decode("utf-8", "replace")
    except Exception as e:  # network problems, timeouts
        return 0, {}, type(e).__name__ + ": " + str(e)[:120]


TOKEN = ""


def scrub(text):
    """Never let the token appear in a report, even if a server echoes it."""
    return text.replace(TOKEN, "***") if TOKEN else text


def peek(text, n=200):
    return scrub(" ".join(text.split()))[:n]


def describe_headers(headers, limit=14):
    return "; ".join(f"{k}: {str(v)[:50]}" for k, v in list(headers.items())[:limit])


def describe_response(status, headers, text):
    parts = [f"HTTP {status}"]
    content_type = hget(headers, "content-type")
    if content_type:
        parts.append("type: " + content_type)
    location = hget(headers, "location")
    if location:
        parts.append("redirects to: " + location)
    parts.append("body: " + (peek(text) or "(empty)"))
    return " | ".join(parts)


def extract_reply(text):
    """Return the model's reply text, or None if this is not a chat answer."""
    try:
        content = json.loads(text)["choices"][0]["message"]["content"]
    except Exception:
        return None
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content.strip() if isinstance(content, str) else None


def get_catalog(token):
    status, headers, text = request("GET", "/catalog/models", token)
    raw = describe_response(status, headers, text)
    if status != 200:
        return [], raw
    try:
        data = json.loads(text)
    except ValueError:
        return [], raw
    if isinstance(data, dict):
        data = data.get("models") or data.get("data") or []
    return data if isinstance(data, list) else [], raw


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
    reply = extract_reply(text) if status == 200 else None
    ok = reply is not None
    shown = reply[:40] if ok else describe_response(status, headers, text)[:240]
    limits = {k.lower(): v for k, v in headers.items()
              if "ratelimit" in k.lower() or k.lower() == "retry-after"}
    raw = describe_response(status, headers, text)
    return ok, status, seconds, shown, limits, raw


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
        status, headers, text = request("POST", "/inference/chat/completions", token, body)
        ok = status == 200 and extract_reply(text) is not None
        results.append((chars, status, "accepted, real answer" if ok else describe_response(status, headers, text)[:200]))
        if not ok:
            break
    return results


def run_curl(args):
    try:
        res = subprocess.run(["curl", "-sS", "-i", "--max-time", "30", "--max-redirs", "0"] + args,
                             capture_output=True, text=True, timeout=45)
        return scrub((res.stdout + res.stderr))[:900]
    except Exception as e:
        return "curl failed: " + type(e).__name__


def diagnose(token):
    out = ["### Diagnosis", ""]
    parsed = urlparse(BASE)
    if parsed.scheme == "https":
        try:
            host = parsed.hostname
            ip = socket.gethostbyname(host)
            ctx = ssl.create_default_context()
            with socket.create_connection((host, 443), timeout=15) as raw:
                with ctx.wrap_socket(raw, server_hostname=host) as tls:
                    cert, version = tls.getpeercert(), tls.version()
            subject = dict(x[0] for x in cert.get("subject", ()))
            issuer = dict(x[0] for x in cert.get("issuer", ()))
            out.append(f"DNS and TLS: {host} resolves to {ip}; {version}; certificate for "
                       f"{subject.get('commonName', '?')}, issued by {issuer.get('organizationName', '?')} "
                       f"({issuer.get('commonName', '?')}), valid until {cert.get('notAfter', '?')}")
        except Exception as e:
            out.append(f"DNS and TLS check failed: {type(e).__name__}: {str(e)[:120]}")
        out.append("")

    chat_body = {"model": "openai/gpt-4.1-mini",
                 "messages": [{"role": "user", "content": "Reply with the single word OK."}],
                 "max_tokens": 16}
    rows = []
    for label, version in (("no version header", None), ("2022-11-28", "2022-11-28"), ("2026-03-10", "2026-03-10")):
        status, headers, text = request("GET", "/catalog/models", token, version=version)
        rows.append([f"catalog, {label}", status, hget(headers, "content-type"), peek(text, 70), describe_headers(headers)])
        status, headers, text = request("POST", "/inference/chat/completions", token, chat_body, version=version)
        rows.append([f"chat, {label}", status, hget(headers, "content-type"), peek(text, 70), describe_headers(headers)])
    out.append(table(["Request", "HTTP", "Type", "Body", "All response headers"], rows))
    out.append("")

    status, headers, text = request("GET", "https://api.github.com/rate_limit", token)
    out.append(f"api.github.com with the same token: HTTP {status}; server: {hget(headers, 'server') or '(none)'}; "
               f"github request id present: {bool(hget(headers, 'x-github-request-id'))}")
    out.append("")

    if shutil.which("curl") and parsed.scheme == "https":
        out.append("Plain curl, catalog:")
        out.append("")
        out.append("```")
        out.append(run_curl(["-H", f"Authorization: Bearer {token}", "-H", "Accept: application/vnd.github+json",
                             "-H", f"X-GitHub-Api-Version: {API_VERSION}", BASE + "/catalog/models"]))
        out.append("```")
        out.append("")
    return "\n".join(out)


def esc(value):
    return str(value).replace("|", "/").replace("\n", " ")


def table(header, rows):
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(esc(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def main():
    global TOKEN
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GITHUB_TOKEN is not set")
        return
    TOKEN = token
    out = ["## GitHub Models probe", ""]

    catalog, catalog_raw = get_catalog(token)
    out.append(f"Catalog: {len(catalog)} models listed.")
    out.append("")
    rows = [describe(m) for m in catalog]
    if rows:
        out.append(table(["Model", "Publisher", "Tier", "Max input tokens", "Max output tokens"],
                         [[r["id"], r["publisher"], r["tier"], r["max_in"], r["max_out"]] for r in rows]))
        out.append("")

    candidates = pick_candidates([r["id"] for r in rows]) if rows else PREFERRED[:3]
    test_rows, first_ok, first_raw = [], None, ""
    for model in candidates:
        ok, status, seconds, shown, limits, raw = try_model(token, model)
        if not first_raw:
            first_raw = raw
        if ok and first_ok is None:
            first_ok = model
        test_rows.append([model, "yes" if ok else "NO", status, f"{seconds:.1f}s", shown, json.dumps(limits)])
    out.append("### Tiny test call per model")
    out.append("")
    out.append(table(["Model", "Real answer", "HTTP", "Time", "Reply or what came back", "Rate-limit headers"], test_rows))
    out.append("")

    if first_ok:
        out.append(f"### Request size probe on {first_ok}")
        out.append("")
        out.append(table(["Characters sent", "HTTP", "Result"],
                         [[c, s, r] for c, s, r in size_probe(token, first_ok)]))
        out.append("")
    else:
        out.append("No model gave a real answer, so the size probe was skipped.")
        out.append("")

    out.append("### Raw peek")
    out.append("")
    out.append("Catalog answer: " + esc(catalog_raw))
    out.append("")
    out.append("First chat answer: " + esc(first_raw))
    out.append("")
    out.append(diagnose(token))

    report = "\n".join(out)
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(report + "\n")


if __name__ == "__main__":
    main()