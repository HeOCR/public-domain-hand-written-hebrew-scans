#!/usr/bin/env python3
"""
consolidate_transcripts.py — Use GPT-4o (text-only) to reconcile the
Transkribus and eScriptorium candidate transcripts into a single improved
candidate.

This is NOT an image-to-text task (no hallucination risk). GPT-4o acts as
a text editor/reconciler: it receives two independent HTR outputs and
produces the most defensible merged reading, flagging disagreements with
⟨…⟩ brackets.

Requires:
    OPENAI_API_KEY

Usage:
    python3 scripts/consolidate_transcripts.py [--limit N] [--entry-id ENTRY_ID]
                                               [--dry-run] [--delay SECS]

Outputs → data/transcripts/candidates/<entry_id>.consolidated.txt
Skips entries where both source candidates are absent.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from openai import OpenAI

HERE = Path(__file__).parent
REPO = HERE.parent
DATA = REPO / "data"
ENTRIES_PATH   = DATA / "index" / "entries.jsonl"
CANDIDATES_DIR = DATA / "transcripts" / "candidates"

SYSTEM_PROMPT = """\
You are an expert paleographer specialising in Hebrew, Yiddish, and Aramaic manuscripts
of the 19th–20th centuries.

You will receive two independent Handwritten Text Recognition (HTR) outputs for the
same manuscript page. Your task is to produce a single consolidated best-effort
diplomatic transcription.

RULES:
1. Where both sources agree verbatim, keep that reading.
2. Where they differ, choose the reading that is linguistically, orthographically,
   or contextually more plausible for the language/period.
3. For genuinely uncertain words (both sources disagree AND neither is clearly better),
   write the most likely reading inside ⟨ ⟩ angle brackets.
4. For passages both sources leave blank or mark illegible, write [illegible].
5. Preserve the original line structure (one manuscript line = one output line).
6. Output ONLY the consolidated transcription — no commentary, no labels, no preamble.
7. Do NOT add any content not present in at least one of the sources.
8. If both sources are empty or contain only [blank] / [illegible], output [blank].
"""


def build_user_prompt(entry: dict, text_a: str, text_b: str) -> str:
    title    = entry.get("title", "")
    creators = ", ".join(c.get("name", "") for c in (entry.get("creators") or []))
    langs    = ", ".join(entry.get("languages") or [])
    doc_type = entry.get("document_type", "")

    ctx_parts = []
    if title:
        ctx_parts.append(f"Document title: {title}")
    if creators:
        ctx_parts.append(f"Creator(s): {creators}")
    if langs:
        ctx_parts.append(f"Language(s): {langs}")
    if doc_type:
        ctx_parts.append(f"Document type: {doc_type}")

    context = "\n".join(ctx_parts)
    return f"""{context}

--- HTR Source A (Transkribus) ---
{text_a or '[no output]'}

--- HTR Source B (eScriptorium / Kraken) ---
{text_b or '[no output]'}

Produce the consolidated transcription:"""


def load_entries() -> list[dict]:
    rows = []
    with open(ENTRIES_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def needs_transcription(entry: dict) -> bool:
    t = entry.get("transcription") or {}
    return t.get("status", "none") in ("none", "raw", "rejected")


def read_candidate(entry_id: str, source: str) -> str | None:
    p = CANDIDATES_DIR / f"{entry_id}.{source}.txt"
    return p.read_text(encoding="utf-8") if p.exists() else None


def candidate_path(entry_id: str) -> Path:
    return CANDIDATES_DIR / f"{entry_id}.consolidated.txt"


def consolidate(entry: dict, client: OpenAI) -> str:
    eid    = entry["entry_id"]
    text_a = read_candidate(eid, "transkribus")
    text_b = read_candidate(eid, "escriptorium")

    if text_a is None and text_b is None:
        raise ValueError("no source candidates available")

    prompt = build_user_prompt(entry, text_a or "", text_b or "")
    resp   = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=4096,
        temperature=0,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ],
    )
    return resp.choices[0].message.content.strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--entry-id")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--delay", type=float, default=0.3)
    ap.add_argument("--skip-existing", action="store_true", default=True)
    ap.add_argument("--require-both", action="store_true", default=False,
                    help="Only consolidate when BOTH source candidates exist")
    args = ap.parse_args()

    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)

    entries = load_entries()

    if args.entry_id:
        to_process = [e for e in entries if e["entry_id"] == args.entry_id]
        if not to_process:
            sys.exit(f"Entry not found: {args.entry_id}")
    else:
        to_process = [e for e in entries if needs_transcription(e)]

    # Filter to entries with at least one (or both, if --require-both) candidates
    def has_sources(e: dict) -> bool:
        eid = e["entry_id"]
        has_a = (CANDIDATES_DIR / f"{eid}.transkribus.txt").exists()
        has_b = (CANDIDATES_DIR / f"{eid}.escriptorium.txt").exists()
        return (has_a and has_b) if args.require_both else (has_a or has_b)

    to_process = [e for e in to_process if has_sources(e)]

    if args.skip_existing:
        to_process = [e for e in to_process
                      if not candidate_path(e["entry_id"]).exists()]

    if args.limit:
        to_process = to_process[:args.limit]

    print(f"Entries to consolidate: {len(to_process)}")
    if args.dry_run:
        for e in to_process:
            eid = e["entry_id"]
            has_a = (CANDIDATES_DIR / f"{eid}.transkribus.txt").exists()
            has_b = (CANDIDATES_DIR / f"{eid}.escriptorium.txt").exists()
            print(f"  {eid}  T={'✓' if has_a else '✗'}  E={'✓' if has_b else '✗'}")
        return

    client = OpenAI()

    done = skipped = errors = 0

    for i, entry in enumerate(to_process, 1):
        eid = entry["entry_id"]
        print(f"[{i}/{len(to_process)}] {eid}")
        try:
            text = consolidate(entry, client)
            candidate_path(eid).write_text(text, encoding="utf-8")
            print(f"  → {len(text)} chars")
            done += 1
        except ValueError as e:
            print(f"  skip: {e}")
            skipped += 1
        except Exception as e:
            print(f"  [ERROR] {e}", file=sys.stderr)
            errors += 1

        if i < len(to_process):
            time.sleep(args.delay)

    print(f"\nDone: {done}  Skipped: {skipped}  Errors: {errors}")


if __name__ == "__main__":
    main()
