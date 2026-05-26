#!/usr/bin/env python3
"""
vision_transcribe.py — Use GPT-4o Vision to generate best-effort transcripts
for all corpus entries that have local scan images but no transcript yet.

Usage:
    python3 scripts/vision_transcribe.py [--limit N] [--entry-id ENTRY_ID] [--dry-run]

For each entry without a transcript:
  1. Finds the local scan file (jpg / png / pdf-thumb)
  2. Sends it to GPT-4o (vision) with a Hebrew/Yiddish HTR prompt
  3. Saves the result to data/transcripts/<entry_id>.txt
  4. Updates the transcription field in entries.jsonl

All AI-generated transcripts are marked status='raw', created_by='ocr',
verification_status='unverified' — ready for human review.
"""

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

from openai import OpenAI

HERE = Path(__file__).parent
REPO = HERE.parent
DATA = REPO / "data"
INDEX = DATA / "index"
ENTRIES_PATH = INDEX / "entries.jsonl"
TRANSCRIPTS_DIR = DATA / "transcripts"
SCANS_DIR = DATA / "scans"

SYSTEM_PROMPT = """You are an expert paleographer and Hebraist specialising in handwritten Hebrew, Aramaic, Yiddish, and Judaeo-Arabic manuscripts, primarily 19th–20th century.

Your task: produce a plain-text diplomatic transcription of the handwritten text in the image.

RULES:
1. Transcribe EXACTLY what is written — do not correct, translate, or paraphrase.
2. Use standard Unicode Hebrew characters. Preserve niqqud (vowel points) where clearly visible.
3. Preserve the original line structure. Each line in the manuscript = one line in the output.
4. For uncertain readings, surround with ⟨ ⟩ angle brackets.
5. For a completely illegible word, write [?].
6. For a completely illegible passage, write [illegible].
7. Output ONLY the transcribed text — no headers, labels, commentary, or translation.
8. If the image contains no readable handwritten text, output the single token: [blank]
9. If in Yiddish: transcribe in Yiddish Hebrew characters as written (do not normalise to YIVO).
"""

USER_PROMPT = "Transcribe all handwritten text in this manuscript image, line by line, exactly as written."


def load_entries() -> list[dict]:
    rows = []
    with open(ENTRIES_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def save_entries(entries: list[dict]):
    with open(ENTRIES_PATH, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


# ── Scan path resolution ──────────────────────────────────────────────────────

def find_scan_path(entry: dict) -> Path | None:
    """Return the best available local scan file for an entry, or None."""
    eid = entry["entry_id"]
    seq = entry.get("sequence") or {}
    label = seq.get("label", "p1")      # e.g. "p4"
    src = entry.get("source_id", "")

    if eid.startswith("loc__"):
        loc_id = src.replace("loc__", "")
        scan_dir = SCANS_DIR / f"loc__{loc_id}"
        page_num = int(label[1:])
        for prefix in ("web", "thumb"):
            p = scan_dir / f"{prefix}_p{page_num:04d}.jpg"
            if p.exists():
                return p
        return None

    if eid.startswith("nli__"):
        parts = eid.split("__")
        archive_key = "__".join(parts[:-1])
        scan_dir = SCANS_DIR / archive_key
        p = scan_dir / f"{eid}.jpg"
        if p.exists():
            return p
        return None

    if eid.startswith("commons__"):
        parts = eid.split("__")
        source = "__".join(parts[:-1])
        scan_dir = SCANS_DIR / source
        for ext in (".jpg", ".jpeg", ".png"):
            p = scan_dir / f"{eid}{ext}"
            if p.exists():
                return p
        # PDF thumbnail fallback (better than rendering full PDF)
        thumb = scan_dir / f"{eid}_thumb.jpg"
        if thumb.exists():
            return thumb
        # Render first page of PDF with pdf2image
        pdf = scan_dir / f"{eid}.pdf"
        if pdf.exists():
            return _pdf_to_img(pdf, scan_dir, eid)
        return None

    if eid.startswith("openn__"):
        parts = eid.split("__")
        source = "__".join(parts[:-1])
        scan_dir = SCANS_DIR / source
        if scan_dir.exists():
            for f in sorted(scan_dir.iterdir()):
                if "web" in f.name and f.suffix == ".jpg":
                    return f
            for f in sorted(scan_dir.iterdir()):
                if f.suffix in (".jpg", ".jpeg", ".png"):
                    return f
        return None

    return None


def _pdf_to_img(pdf_path: Path, out_dir: Path, stem: str) -> Path | None:
    try:
        from pdf2image import convert_from_path
        pages = convert_from_path(str(pdf_path), first_page=1, last_page=1, dpi=150)
        if pages:
            out = out_dir / f"{stem}_rendered.jpg"
            pages[0].save(str(out), "JPEG", quality=85)
            return out
    except Exception as e:
        print(f"  [pdf2image error: {e}]", file=sys.stderr)
    return None


# ── Vision call ───────────────────────────────────────────────────────────────

def image_to_b64url(path: Path) -> str:
    ext = path.suffix.lower()
    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".webp": "image/webp"}.get(ext, "image/jpeg")
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    return f"data:{mime};base64,{data}"


def build_user_prompt(entry: dict) -> str:
    """Contextualise the prompt with what we know about the entry."""
    title = entry.get("title", "")
    doc_type = entry.get("document_type", "")
    creators = ", ".join(c.get("name", "") for c in (entry.get("creators") or []))
    langs = ", ".join(entry.get("languages") or [])
    context_parts = []
    if title:
        context_parts.append(f"Document title: {title}")
    if creators:
        context_parts.append(f"Creator(s): {creators}")
    if langs:
        context_parts.append(f"Language(s): {langs}")
    if doc_type:
        context_parts.append(f"Document type: {doc_type}")
    context = "\n".join(context_parts)
    if context:
        return f"{context}\n\n{USER_PROMPT}"
    return USER_PROMPT


def transcribe_image(
    path: Path,
    entry: dict,
    client: OpenAI,
) -> str:
    b64url = image_to_b64url(path)
    prompt = build_user_prompt(entry)
    resp = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=4096,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "image_url",
                     "image_url": {"url": b64url, "detail": "high"}},
                    {"type": "text", "text": prompt},
                ],
            },
        ],
    )
    return resp.choices[0].message.content.strip()


# ── Entry update ──────────────────────────────────────────────────────────────

def build_transcript_rights(entry: dict) -> dict:
    entry_rights = entry.get("rights", {})
    rb_expr = entry_rights.get("license_expression", "")
    basis_map = {
        "PD-old-70": "public_domain",
        "PD-old-100": "public_domain",
        "PDM-1.0": "public_domain",
        "LicenseRef-Public-Domain-Israel": "public_domain",
        "CC-BY-SA-3.0": "cc_by_sa",
        "CC-BY-4.0": "cc_by",
        "CC-BY-SA-4.0": "cc_by_sa",
        "CC0-1.0": "cc0",
    }
    return {
        "rights_basis": basis_map.get(rb_expr, "unknown"),
        "license_expression": rb_expr or None,
        # Per schema: unverified status → commercial/derivatives/redistribution must not be True
        "commercial_use_allowed": None,
        "derivatives_allowed": None,
        "redistribution_allowed": None,
        "attribution_required": None,
        "verification_status": "unverified",
        "evidence_text": (
            f"AI transcription (GPT-4o vision, best-effort). "
            f"Inherits scan license: {rb_expr}. Needs human review."
        ),
        "verified_at": None,
    }


def apply_transcription(entry: dict, rel_path: str) -> dict:
    entry = dict(entry)
    entry["transcription"] = {
        "status": "raw",
        "text_path": rel_path,
        "alto_path": None,
        "hocr_path": None,
        "source_url": None,
        "created_by": "ocr",       # "ocr" covers AI-based HTR per schema
        "rights": build_transcript_rights(entry),
    }
    return entry


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--entry-id", default=None,
                        help="Process only this single entry ID")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--delay", type=float, default=0.5,
                        help="Seconds between API calls (default: 0.5)")
    args = parser.parse_args()

    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    client = OpenAI()   # reads OPENAI_API_KEY from env

    entries = load_entries()
    entry_index = {e["entry_id"]: i for i, e in enumerate(entries)}

    if args.entry_id:
        to_process = [e for e in entries if e["entry_id"] == args.entry_id]
        if not to_process:
            sys.exit(f"Entry not found: {args.entry_id}")
    else:
        to_process = [
            e for e in entries
            if e.get("transcription", {}).get("status", "none") == "none"
        ]

    if args.limit:
        to_process = to_process[: args.limit]

    print(f"Entries to transcribe: {len(to_process)}")
    if args.dry_run:
        print("[DRY RUN — no API calls, no writes]")
    print()

    done = skipped = errors = 0

    for i, entry in enumerate(to_process, 1):
        eid = entry["entry_id"]
        scan_path = find_scan_path(entry)

        if scan_path is None:
            print(f"[{i}/{len(to_process)}] {eid} — no scan, skip")
            skipped += 1
            continue

        size_kb = scan_path.stat().st_size // 1024
        print(f"[{i}/{len(to_process)}] {eid}")
        print(f"  scan: {scan_path.name} ({size_kb} KB)")

        if args.dry_run:
            print("  [dry-run]")
            done += 1
            continue

        try:
            text = transcribe_image(scan_path, entry, client)
            char_count = len(text)
            print(f"  → {char_count} chars")

            rel_path = f"data/transcripts/{eid}.txt"
            abs_path = TRANSCRIPTS_DIR / f"{eid}.txt"
            abs_path.write_text(text, encoding="utf-8")

            idx = entry_index[eid]
            entries[idx] = apply_transcription(entry, rel_path)
            done += 1

        except Exception as e:
            print(f"  [ERROR] {e}", file=sys.stderr)
            errors += 1

        if i < len(to_process):
            time.sleep(args.delay)

    if not args.dry_run and done > 0:
        save_entries(entries)
        print()
        print(f"✓  Updated entries.jsonl")

    print()
    print(f"Done:    {done}")
    print(f"Skipped: {skipped}")
    if errors:
        print(f"Errors:  {errors}")


if __name__ == "__main__":
    main()
