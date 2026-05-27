#!/usr/bin/env python3
"""
kraken_transcribe.py — Run Kraken HTR (the engine behind eScriptorium) on
corpus scans using the BiblIA Hebrew manuscript model.

No credentials required. Models are downloaded from Zenodo on first run
and cached in ~/.config/kraken/.

Models used:
  Segmentation:  10.5281/zenodo.14602568  (general print+handwriting blla)
  Recognition:   10.5281/zenodo.5468286   (BiblIA_01: Ashkenazi+Sephardi+Italian
                                            medieval Hebrew bookhand — best available
                                            for 19th-20th c. Ashkenazic scripts)

Usage:
    python3 scripts/kraken_transcribe.py [--limit N] [--entry-id ENTRY_ID]
                                         [--dry-run]

Outputs → data/transcripts/candidates/<entry_id>.escriptorium.txt

NOTE: These models were trained on medieval manuscript scripts, not modern
cursive. Output quality will vary; the consolidation pipeline reconciles
this with the Transkribus output.
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

# Suppress noisy urllib3 version warning from requests/kraken
warnings.filterwarnings("ignore", category=UserWarning, module="requests")

HERE = Path(__file__).parent
REPO = HERE.parent
DATA = REPO / "data"
ENTRIES_PATH   = DATA / "index" / "entries.jsonl"
SCANS_DIR      = DATA / "scans"
CANDIDATES_DIR = DATA / "transcripts" / "candidates"

# Zenodo DOIs for the models we want
SEG_MODEL_DOI = "10.5281/zenodo.14602568"   # general segmentation
REC_MODEL_DOI = "10.5281/zenodo.5468286"    # BiblIA_01 Hebrew recognition

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


# ── Scan path resolution (mirrors vision_transcribe.py) ──────────────────────

def find_scan_path(entry: dict) -> Path | None:
    eid   = entry["entry_id"]
    seq   = entry.get("sequence") or {}
    label = seq.get("label", "p1")

    if eid.startswith("loc__"):
        src      = entry.get("source_id", "").replace("loc__", "")
        scan_dir = SCANS_DIR / f"loc__{src}"
        page_num = int(label[1:])
        for prefix in ("web", "thumb"):
            p = scan_dir / f"{prefix}_p{page_num:04d}.jpg"
            if p.exists():
                return p
        return None

    if eid.startswith("nli__"):
        parts       = eid.split("__")
        archive_key = "__".join(parts[:-1])
        p = SCANS_DIR / archive_key / f"{eid}.jpg"
        return p if p.exists() else None

    if eid.startswith("commons__"):
        parts    = eid.split("__")
        source   = "__".join(parts[:-1])
        scan_dir = SCANS_DIR / source
        for ext in (".jpg", ".jpeg", ".png"):
            p = scan_dir / f"{eid}{ext}"
            if p.exists():
                return p
        thumb = scan_dir / f"{eid}_thumb.jpg"
        if thumb.exists():
            return thumb
        return None

    if eid.startswith("openn__"):
        parts    = eid.split("__")
        source   = "__".join(parts[:-1])
        scan_dir = SCANS_DIR / source
        if scan_dir.exists():
            for f in sorted(scan_dir.iterdir()):
                if f.suffix in _IMAGE_EXTS:
                    return f
        return None

    return None


# ── Model management ──────────────────────────────────────────────────────────

def _find_model_file(model_dir: Path) -> Path:
    """Find the .mlmodel (or .mlpackage) file inside a downloaded model directory."""
    for suffix in (".mlmodel", ".mlpackage", ".pt", ".pth"):
        hits = list(model_dir.glob(f"*{suffix}"))
        if hits:
            return hits[0]
    # Fallback: return the dir itself (kraken may handle it)
    return model_dir


def ensure_models() -> tuple[Path, Path]:
    """Download models if not cached; return (seg_path, rec_path)."""
    from htrmopo import get_model
    print("Checking/downloading Kraken models …")
    seg_dir = Path(get_model(SEG_MODEL_DOI))
    rec_dir = Path(get_model(REC_MODEL_DOI))
    seg_path = _find_model_file(seg_dir)
    rec_path = _find_model_file(rec_dir)
    print(f"  segmentation: {seg_path.name}")
    print(f"  recognition:  {rec_path.name}")
    return seg_path, rec_path


# ── HTR pipeline ──────────────────────────────────────────────────────────────

def transcribe_image(image_path: Path, seg_model, rec_model) -> str:
    """Segment and recognise a single image; return plain text."""
    from PIL import Image
    from kraken import blla, rpred

    im = Image.open(image_path).convert("RGB")

    # Baseline segmentation
    baseline_seg = blla.segment(im, model=seg_model)

    if not baseline_seg.lines:
        return "[blank]"

    # Recognition (RTL — Hebrew is right-to-left)
    preds = rpred.rpred(rec_model, im, baseline_seg, bidi_reordering=True)

    lines = []
    for pred in preds:
        text = pred.prediction.strip()
        if text:
            lines.append(text)

    return "\n".join(lines) if lines else "[blank]"


# ── Helpers ───────────────────────────────────────────────────────────────────

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


def candidate_path(entry_id: str) -> Path:
    return CANDIDATES_DIR / f"{entry_id}.escriptorium.txt"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--entry-id")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-existing", action="store_true", default=True)
    args = ap.parse_args()

    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)

    entries = load_entries()

    if args.entry_id:
        to_process = [e for e in entries if e["entry_id"] == args.entry_id]
        if not to_process:
            sys.exit(f"Entry not found: {args.entry_id}")
    else:
        to_process = [e for e in entries if needs_transcription(e)]

    if args.skip_existing:
        to_process = [e for e in to_process
                      if not candidate_path(e["entry_id"]).exists()]

    if args.limit:
        to_process = to_process[:args.limit]

    print(f"Entries to transcribe (Kraken/eScriptorium): {len(to_process)}")

    if args.dry_run:
        for e in to_process:
            sp = find_scan_path(e)
            print(f"  {e['entry_id']}  scan={sp.name if sp else 'MISSING'}")
        return

    # Load models (downloads on first run)
    seg_path, rec_path = ensure_models()

    from kraken.lib import models as kmodels
    from kraken.lib.vgsl.model import TorchVGSLModel
    import warnings
    print("\nLoading models …")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        seg_model = TorchVGSLModel.load_model(str(seg_path))
    rec_model = kmodels.load_any(str(rec_path))
    print("Models loaded.\n")

    done = skipped = errors = 0

    for i, entry in enumerate(to_process, 1):
        eid  = entry["entry_id"]
        scan = find_scan_path(entry)

        if scan is None:
            print(f"[{i}/{len(to_process)}] {eid} — no scan, skip")
            skipped += 1
            continue

        size_kb = scan.stat().st_size // 1024
        print(f"[{i}/{len(to_process)}] {eid}  ({scan.name}, {size_kb} KB)")

        try:
            text = transcribe_image(scan, seg_model, rec_model)
            candidate_path(eid).write_text(text, encoding="utf-8")
            print(f"  → {len(text)} chars")
            done += 1
        except Exception as e:
            print(f"  [ERROR] {e}", file=sys.stderr)
            errors += 1

    print(f"\nDone: {done}  Skipped: {skipped}  Errors: {errors}")


if __name__ == "__main__":
    main()
