#!/usr/bin/env python3
"""
quantum_watch.py
Fetches quantum computing news, deduplicates against the last 7 days,
writes new items to items.csv, and sends a digest to Telegram.
Traces all LLM calls to Langfuse when LANGFUSE_PUBLIC_KEY is set.
"""
import csv
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

# Windows terminals default to cp1252 which can't print many Unicode chars.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Langfuse tracing — optional, silently disabled when keys are not set
# (Langfuse v4 API: from langfuse import observe, get_client)
# ---------------------------------------------------------------------------
_LANGFUSE_ENABLED = False
_langfuse_client = None

try:
    if os.environ.get("LANGFUSE_PUBLIC_KEY"):
        import warnings
        # Suppress Pydantic v1 / Python 3.14 compatibility warning (cosmetic only)
        warnings.filterwarnings("ignore", category=UserWarning, module="langfuse")
        from langfuse import Langfuse, get_client, observe  # type: ignore[import]
        _langfuse_client = Langfuse()   # initialises the global singleton
        _LANGFUSE_ENABLED = True
        print("  Langfuse tracing enabled.", file=sys.stderr)
except ImportError as e:
    print(f"[warn] Langfuse import error ({e}) — tracing disabled.", file=sys.stderr)
except Exception as e:
    print(f"[warn] Langfuse init failed ({e}) — tracing disabled.", file=sys.stderr)

if not _LANGFUSE_ENABLED:
    # No-op shims — code is identical whether tracing is on or off
    def observe(_fn=None, **kwargs):  # type: ignore[misc]
        def decorator(fn):
            return fn
        return decorator(_fn) if _fn is not None else decorator

    class _NoopClient:
        def update_current_span(self, **kw): pass
        def update_current_generation(self, **kw): pass
        def score_current_trace(self, **kw): pass

    def get_client():  # type: ignore[misc]
        return _NoopClient()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MODEL = "claude-sonnet-4-5"
ITEMS_CSV = Path(__file__).parent / "items.csv"
CSV_COLUMNS = ["date_reported", "title", "summary", "url", "category"]

_SEARCH_TEMPLATE = """\
Today is {today}. Search the web for quantum computing news published in the last 48 hours.

Focus on:
- New companies emerging from stealth mode
- Technical milestones (qubit counts, error rates, coherence, gate fidelity, etc.)
- Scientific discoveries and breakthroughs
- Funding rounds and investments
- New research papers or pre-prints

Include any relevant item dated within the last 48 hours even if it is not from today exactly.
{dedup_section}\
Return ONLY a raw JSON array — no markdown code fences, no preamble, no trailing text.
Each element must have exactly these four keys:
  "title"    — short headline (15 words or fewer)
  "summary"  — one sentence explaining why this matters
  "url"      — direct link to the source article
  "category" — one of exactly: company  milestone  research  funding  other

If you find no relevant news, return exactly: []
"""

_DEDUP_BLOCK = """\
The following stories were already reported in the last 7 days.
Skip any item that is substantially the same story, even if the wording differs:
{lines}

"""

CATEGORY_ORDER = ["company", "milestone", "research", "funding", "other"]
CATEGORY_LABELS = {
    "company":   "🏢  Companies",
    "milestone": "🚀  Milestones",
    "research":  "🔬  Research",
    "funding":   "💰  Funding",
    "other":     "📌  Other",
}


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def load_recent_items(days: int = 7) -> list[dict]:
    """Return rows from items.csv reported within the last `days` days."""
    if not ITEMS_CSV.exists():
        return []
    cutoff = datetime.now() - timedelta(days=days)
    recent: list[dict] = []
    with ITEMS_CSV.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                if datetime.strptime(row["date_reported"], "%Y-%m-%d") >= cutoff:
                    recent.append(row)
            except (ValueError, KeyError):
                continue
    return recent


def url_dedup(new_items: list[dict], recent: list[dict]) -> list[dict]:
    """Drop items whose URL exactly matches one already in recent."""
    seen = {r["url"].strip() for r in recent if r.get("url")}
    return [item for item in new_items if item.get("url", "").strip() not in seen]


def append_to_csv(items: list[dict]) -> None:
    """Append items to items.csv, creating it with a header row if new."""
    write_header = not ITEMS_CSV.exists()
    today = datetime.now().strftime("%Y-%m-%d")
    with ITEMS_CSV.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        for item in items:
            writer.writerow({
                "date_reported": today,
                "title":         item.get("title", ""),
                "summary":       item.get("summary", ""),
                "url":           item.get("url", ""),
                "category":      item.get("category", "other"),
            })


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_prompt(today: str, recent: list[dict]) -> str:
    if recent:
        lines = "\n".join(f"- {r['title']} | {r['url']}" for r in recent)
        dedup_section = _DEDUP_BLOCK.format(lines=lines)
    else:
        dedup_section = ""
    return _SEARCH_TEMPLATE.format(today=today, dedup_section=dedup_section)


# ---------------------------------------------------------------------------
# Claude API calls — each is a traced generation span
# ---------------------------------------------------------------------------

@observe(as_type="generation")
def _call_claude_main(client: anthropic.Anthropic, prompt: str) -> anthropic.types.Message:
    """Primary Claude call: web search + news extraction."""
    get_client().update_current_generation(
        name="news-fetch",
        model=MODEL,
        input=[{"role": "user", "content": prompt}],
        model_parameters={"max_tokens": 4096, "tool_choice": "any"},
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        tool_choice={"type": "any"},
        messages=[{"role": "user", "content": prompt}],
    )

    searches = [b for b in response.content if b.type == "server_tool_use"]
    text_out = "".join(b.text for b in response.content if b.type == "text")

    get_client().update_current_generation(
        output=text_out,
        usage_details={
            "input":  response.usage.input_tokens,
            "output": response.usage.output_tokens,
        },
        metadata={"web_searches": len(searches)},
    )
    return response


@observe(as_type="generation")
def _call_claude_retry(client: anthropic.Anthropic, bad_json: str) -> str:
    """Fallback call that asks Claude to fix malformed JSON."""
    content = (
        "The text below should be a JSON array but it is not valid JSON.\n"
        "Return ONLY the corrected JSON array and absolutely nothing else:\n\n"
        + bad_json
    )
    get_client().update_current_generation(
        name="json-fix-retry",
        model=MODEL,
        input=[{"role": "user", "content": content}],
        model_parameters={"max_tokens": 4096},
        metadata={"reason": "malformed_json"},
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": content}],
    )

    text_out = "".join(b.text for b in response.content if b.type == "text")
    get_client().update_current_generation(
        output=text_out,
        usage_details={
            "input":  response.usage.input_tokens,
            "output": response.usage.output_tokens,
        },
    )
    return text_out


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def _extract_array(text: str) -> str:
    """Pull the first [...] block out of text, stripping any preamble/postamble."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        end = len(lines) - 1 if lines[-1].strip().startswith("```") else len(lines)
        text = "\n".join(lines[1:end]).strip()
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


def _parse_json(raw: str, client: anthropic.Anthropic) -> list[dict]:
    text = _extract_array(raw)
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        print("[warn] Malformed JSON — retrying once with stricter instruction…",
              file=sys.stderr)

    retry_text = _call_claude_retry(client, raw)
    try:
        data2 = json.loads(_extract_array(retry_text))
        return data2 if isinstance(data2, list) else []
    except json.JSONDecodeError:
        pass

    print("[error] JSON still malformed after retry — returning empty list.",
          file=sys.stderr)
    return []


# ---------------------------------------------------------------------------
# News fetcher
# ---------------------------------------------------------------------------

def fetch_news(recent: list[dict]) -> list[dict]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("[error] ANTHROPIC_API_KEY not set in environment / .env file.")

    client = anthropic.Anthropic(api_key=api_key)
    today = datetime.now().strftime("%B %d, %Y")
    prompt = _build_prompt(today, recent)

    print("  Sending request to Claude…", file=sys.stderr)
    response = _call_claude_main(client, prompt)

    searches = [b for b in response.content if b.type == "server_tool_use"]
    print(f"  Web searches performed: {len(searches)}", file=sys.stderr)

    raw = "".join(
        b.text for b in response.content if b.type == "text"
    ).strip()

    if not raw:
        print("[warn] Claude returned no text. Returning empty list.", file=sys.stderr)
        return []

    return _parse_json(raw, client)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _format_telegram(items: list[dict]) -> str:
    date_str = datetime.now().strftime("%Y-%m-%d")
    if not items:
        return f"⚛ <b>Quantum Digest — {date_str}</b>\n\nNo new quantum news today 🌙"

    lines = [f"⚛ <b>Quantum Digest — {date_str}</b>"]

    grouped: dict[str, list] = {c: [] for c in CATEGORY_ORDER}
    for item in items:
        cat = item.get("category", "other").lower()
        if cat not in grouped:
            cat = "other"
        grouped[cat].append(item)

    for cat in CATEGORY_ORDER:
        bucket = grouped[cat]
        if not bucket:
            continue
        lines.append(f"\n<b>{CATEGORY_LABELS[cat]}</b>")
        for item in bucket:
            title   = _html_escape(item.get("title", "(no title)"))
            summary = _html_escape(item.get("summary", ""))
            url     = item.get("url", "")
            if url:
                lines.append(f'• {title} — {summary} <a href="{url}">link</a>')
            else:
                lines.append(f"• {title} — {summary}")

    return "\n".join(lines)


def send_telegram(text: str) -> None:
    """POST a message to Telegram. Logs failures but never raises."""
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        print("[warn] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — skipping send.",
              file=sys.stderr)
        return

    url     = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id":                  chat_id,
        "text":                     text,
        "parse_mode":               "HTML",
        "disable_web_page_preview": False,
    }).encode("utf-8")

    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        if result.get("ok"):
            print("  Telegram message sent.", file=sys.stderr)
        else:
            print(f"[warn] Telegram API error: {result.get('description')}", file=sys.stderr)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"[warn] Telegram HTTP {e.code}: {body}", file=sys.stderr)
    except Exception as e:
        print(f"[warn] Telegram send failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Terminal output
# ---------------------------------------------------------------------------

def print_digest(items: list[dict]) -> None:
    date_str = datetime.now().strftime("%Y-%m-%d")
    sep = "=" * 62
    print(f"\n{sep}")
    print(f"  ⚛  Quantum Computing Digest — {date_str}")
    print(f"{sep}\n")

    if not items:
        print("  No new quantum news today 🌙\n")
        return

    grouped: dict[str, list] = {c: [] for c in CATEGORY_ORDER}
    for item in items:
        cat = item.get("category", "other").lower()
        if cat not in grouped:
            cat = "other"
        grouped[cat].append(item)

    for cat in CATEGORY_ORDER:
        bucket = grouped[cat]
        if not bucket:
            continue
        print(CATEGORY_LABELS[cat])
        print("-" * 42)
        for item in bucket:
            title   = item.get("title", "(no title)")
            summary = item.get("summary", "")
            url     = item.get("url", "")
            print(f"• {title}")
            if summary:
                print(f"  {summary}")
            if url:
                print(f"  {url}")
            print()

    print(f"  Total: {len(items)} item(s)")
    print()


# ---------------------------------------------------------------------------
# Entry point — wrapped in @observe() so the whole run is one Langfuse trace
# ---------------------------------------------------------------------------

@observe()
def run() -> None:
    get_client().update_current_span(
        name="quantum-digest",
        metadata={
            "date":  datetime.now().strftime("%Y-%m-%d"),
            "model": MODEL,
            "tags":  ["scheduled"],
        },
    )

    print("Searching for quantum computing news…", file=sys.stderr)

    recent = load_recent_items()
    print(f"  Recent items loaded for dedup: {len(recent)}", file=sys.stderr)

    new_items = fetch_news(recent)

    # URL-exact safety-net dedup
    before = len(new_items)
    new_items = url_dedup(new_items, recent)
    dropped = before - len(new_items)
    if dropped:
        print(f"  Dropped {dropped} duplicate URL(s).", file=sys.stderr)

    # Write CSV before Telegram so it's always saved even if Telegram fails
    if new_items:
        append_to_csv(new_items)
        print(f"  Wrote {len(new_items)} item(s) to {ITEMS_CSV}.", file=sys.stderr)

    send_telegram(_format_telegram(new_items))
    print_digest(new_items)

    # Score so the trace is filterable in Langfuse by outcome
    get_client().score_current_trace(
        name="items_found",
        value=len(new_items),
        comment=f"{len(new_items)} new item(s) reported",
    )


if __name__ == "__main__":
    run()
    if _LANGFUSE_ENABLED and _langfuse_client:
        _langfuse_client.flush()  # Required in batch scripts — sends buffered events
        print("  Langfuse trace flushed.", file=sys.stderr)
