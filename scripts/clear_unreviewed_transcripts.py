#!/usr/bin/env python3
"""
clear_unreviewed_transcripts.py — Delete all 'raw' and 'rejected' transcript
files and reset those entries back to status='none' in entries.jsonl.

Entries with status='reviewed' or 'aligned' are untouched.

Usage:
    python3 scripts/clear_unreviewed_transcripts.py [--dry-run]
"""

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
REPO = HERE.parent
DATA = REPO / "data"
ENTRIES_PATH = DATA / "index" / "entries.jsonl"
TRANSCRIPTS_DIR = DATA / "transcripts"

CLEAR_STATUSES = {"raw", "rejected"}
KEEP_STATUSES  = {"reviewed", "aligned"}

BLANK_TRANSCRIPTION = {
    "status": "none",
    "text_path": None,
    "alto_path": None,
    "hocr_path": None,
    "source_url": None,
    "created_by": "unknown",
    "rights": {
        "rights_basis": "unknown",
        "license_expression": None,
        "commercial_use_allowed": None,
        "derivatives_allowed": None,
        "redistribution_allowed": None,
        "attribution_required": None,
        "verification_status": "unverified",
        "evidence_text": None,
        "verified_at": None,
    },
}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    entries = []
    with open(ENTRIES_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))

    cleared = 0
    files_deleted = 0

    for i, entry in enumerate(entries):
        t = entry.get("transcription") or {}
        status = t.get("status", "none")
        if status not in CLEAR_STATUSES:
            continue

        eid = entry["entry_id"]

        # Delete the transcript .txt file
        txt_path = TRANSCRIPTS_DIR / f"{eid}.txt"
        if txt_path.exists():
            if not args.dry_run:
                txt_path.unlink()
            files_deleted += 1
            print(f"  del  {txt_path.name}")
        else:
            print(f"  miss {eid}.txt")

        # Reset entry transcription to blank
        if not args.dry_run:
            entries[i] = dict(entry)
            entries[i]["transcription"] = dict(BLANK_TRANSCRIPTION)
            # Preserve the scan-derived rights basis from the entry-level rights
            rb = (entry.get("rights") or {}).get("rights_basis", "unknown")
            entries[i]["transcription"]["rights"]["rights_basis"] = rb

        cleared += 1

    print(f"\n{'[DRY RUN] ' if args.dry_run else ''}Cleared {cleared} entries, "
          f"deleted {files_deleted} .txt files.")

    if not args.dry_run and cleared > 0:
        with open(ENTRIES_PATH, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        print("✓  entries.jsonl updated.")


if __name__ == "__main__":
    main()
