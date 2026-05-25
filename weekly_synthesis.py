#!/usr/bin/env python3
"""
weekly_synthesis.py
Every Sunday: reads the last 7 days from items.csv, synthesises with Claude,
sends to Telegram, appends to weeklies.md, and traces the run in Langfuse.
"""
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Reuse daily-agent helpers — no duplication
from quantum_watch import load_recent_items, send_telegram  # noqa: E402

MODEL      = "claude-sonnet-4-5"
WEEKLIES   = Path(__file__).parent / "weeklies.md"

# ---------------------------------------------------------------------------
# Langfuse (same graceful-degradation pattern as quantum_watch.py)
# ---------------------------------------------------------------------------
_LANGFUSE_ENABLED = False
_langfuse_client  = None

try:
    if os.environ.get("LANGFUSE_PUBLIC_KEY"):
        import warnings
        warnings.filterwarnings("ignore", category=UserWarning, module="langfuse")
        from langfuse import Langfuse, get_client, observe  # type: ignore[import]
        _langfuse_client = Langfuse()
        _LANGFUSE_ENABLED = True
except ImportError as e:
    print(f"[warn] Langfuse import error ({e}) — tracing disabled.", file=sys.stderr)
except Exception as e:
    print(f"[warn] Langfuse init failed ({e}) — tracing disabled.", file=sys.stderr)

if not _LANGFUSE_ENABLED:
    def observe(_fn=None, **kwargs):          # type: ignore[misc]
        def decorator(fn): return fn
        return decorator(_fn) if _fn is not None else decorator

    class _NoopClient:
        def update_current_span(self, **kw): pass
        def update_current_generation(self, **kw): pass
        def score_current_trace(self, **kw): pass

    def get_client():                          # type: ignore[misc]
        return _NoopClient()

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------
_PROMPT = """\
You are a sharp, opinionated analyst covering quantum computing.
Below are {n} news items from the past week. Write a weekly synthesis in under 400 words.

Use these exact section headers:

**Top 3 Themes**
Three one-line observations about patterns across multiple items. Be specific — name companies, technologies, or trends.

**Who's Making Noise**
Which companies, labs, or people appeared most or most notably this week. One short paragraph.

**Bold Prediction for Next Week**
One specific, falsifiable prediction. Clearly label it as speculation.

**One Thing Being Overhyped**
Push back on something. What's getting more attention than it deserves?

---
Items this week:
{items}
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _items_to_text(items: list[dict]) -> str:
    lines = []
    for i, row in enumerate(items, 1):
        date     = row.get("date_reported", "")
        cat      = row.get("category", "")
        title    = row.get("title", "")
        summary  = row.get("summary", "")
        url      = row.get("url", "")
        lines.append(f"{i}. [{date}] ({cat}) {title} — {summary} {url}")
    return "\n".join(lines)


def _to_telegram_html(synthesis: str, date_str: str) -> str:
    """Escape HTML special chars and convert **bold** → <b>bold</b>."""
    safe = synthesis.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    safe = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", safe)
    return f"⚛ <b>Quantum Weekly — {date_str}</b>\n\n{safe}"


def _append_to_weeklies(date_str: str, synthesis: str) -> None:
    with WEEKLIES.open("a", encoding="utf-8") as f:
        f.write(f"\n## Week of {date_str}\n\n{synthesis}\n")


# ---------------------------------------------------------------------------
# Claude generation span
# ---------------------------------------------------------------------------

@observe(as_type="generation")
def _call_claude(client: anthropic.Anthropic, prompt: str) -> str:
    get_client().update_current_generation(
        name="synthesis-generation",
        model=MODEL,
        input=[{"role": "user", "content": prompt}],
        model_parameters={"max_tokens": 1024},
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    text = "".join(b.text for b in response.content if b.type == "text").strip()

    get_client().update_current_generation(
        output=text,
        usage_details={
            "input":  response.usage.input_tokens,
            "output": response.usage.output_tokens,
        },
    )
    return text


# ---------------------------------------------------------------------------
# Entry point — one Langfuse trace per run
# ---------------------------------------------------------------------------

@observe()
def run() -> int:
    """Returns 0 on success/skip, 1 if Claude failed."""
    items = load_recent_items(days=7)
    n     = len(items)

    get_client().update_current_span(
        name="weekly-synthesis",
        metadata={"run_type": "weekly", "items_count": n},
    )

    print(f"  Items in last 7 days: {n}", file=sys.stderr)

    # --- Too few items: send quiet-week message and exit cleanly ---
    if n < 3:
        msg = f"Quiet week in quantum 🌙 — only {n} item{'s' if n != 1 else ''} this week."
        send_telegram(msg)
        print(msg)
        get_client().score_current_trace(name="sent", value=0, comment="fewer than 3 items")
        return 0

    # --- Build synthesis ---
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("[error] ANTHROPIC_API_KEY not set.")

    client = anthropic.Anthropic(api_key=api_key)
    prompt = _PROMPT.format(n=n, items=_items_to_text(items))

    print(f"  Requesting synthesis of {n} items…", file=sys.stderr)
    synthesis = _call_claude(client, prompt)

    if not synthesis:
        send_telegram("Weekly synthesis failed — check Langfuse trace")
        get_client().score_current_trace(name="sent", value=0, comment="Claude returned empty")
        return 1

    date_str = datetime.now().strftime("%Y-%m-%d")

    # Send to Telegram (failure logged, not raised — synthesis still written below)
    send_telegram(_to_telegram_html(synthesis, date_str))

    # Persist to weeklies.md
    _append_to_weeklies(date_str, synthesis)
    print(f"  Appended to {WEEKLIES}.", file=sys.stderr)

    # Print to terminal
    sep = "=" * 62
    print(f"\n{sep}\n  ⚛  Quantum Weekly — {date_str}\n{sep}\n")
    print(synthesis)
    print()

    get_client().score_current_trace(
        name="sent", value=1, comment=f"synthesis of {n} items"
    )
    return 0


if __name__ == "__main__":
    print("Running weekly synthesis…", file=sys.stderr)
    exit_code = run()
    if _LANGFUSE_ENABLED and _langfuse_client:
        _langfuse_client.flush()
        print("  Langfuse trace flushed.", file=sys.stderr)
    sys.exit(exit_code)
