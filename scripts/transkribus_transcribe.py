#!/usr/bin/env python3
"""
transkribus_transcribe.py — Run Transkribus HTR on corpus scans.

Requires env vars:
    TRANSKRIBUS_USER      Transkribus account e-mail
    TRANSKRIBUS_PASSWORD  Transkribus account password
    TRANSKRIBUS_MODEL_ID  (optional) HTR model ID integer; default auto-discovers
                          the first public Hebrew model in the account.

Usage:
    python3 scripts/transkribus_transcribe.py [--limit N] [--entry-id ENTRY_ID]
                                              [--dry-run] [--delay SECS]

Outputs → data/transcripts/candidates/<entry_id>.transkribus.txt
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).parent
REPO = HERE.parent
DATA = REPO / "data"
ENTRIES_PATH = DATA / "index" / "entries.jsonl"
SCANS_DIR    = DATA / "scans"
CANDIDATES_DIR = DATA / "transcripts" / "candidates"

TRPSERVER = "https://transkribus.eu/TrpServer/rest"
COLLECTION_NAME = "hash-htr-pipeline"

# ── Scan resolution ───────────────────────────────────────────────────────────
# Keep this in sync with vision_transcribe.py
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

def find_scan_path(entry: dict) -> Path | None:
    eid  = entry["entry_id"]
    seq  = entry.get("sequence") or {}
    label = seq.get("label", "p1")

    # LoC
    if eid.startswith("loc__"):
        src = entry.get("source_id", "").replace("loc__", "")
        scan_dir = SCANS_DIR / f"loc__{src}"
        page_num = int(label[1:])
        for prefix in ("web", "thumb"):
            p = scan_dir / f"{prefix}_p{page_num:04d}.jpg"
            if p.exists():
                return p
        return None

    # NLI
    if eid.startswith("nli__"):
        parts = eid.split("__")
        archive_key = "__".join(parts[:-1])
        p = SCANS_DIR / archive_key / f"{eid}.jpg"
        return p if p.exists() else None

    # Commons
    if eid.startswith("commons__"):
        parts = eid.split("__")
        source = "__".join(parts[:-1])
        scan_dir = SCANS_DIR / source
        for ext in (".jpg", ".jpeg", ".png"):
            p = scan_dir / f"{eid}{ext}"
            if p.exists():
                return p
        thumb = scan_dir / f"{eid}_thumb.jpg"
        if thumb.exists():
            return thumb
        return None

    # OPenn
    if eid.startswith("openn__"):
        parts = eid.split("__")
        source = "__".join(parts[:-1])
        scan_dir = SCANS_DIR / source
        if scan_dir.exists():
            for f in sorted(scan_dir.iterdir()):
                if f.suffix in _IMAGE_EXTS:
                    return f
        return None

    return None


# ── Transkribus session ───────────────────────────────────────────────────────

class TranskribusSession:
    def __init__(self, user: str, password: str):
        self.sess = requests.Session()
        self.sess.headers["Accept"] = "application/json"
        self._login(user, password)

    def _login(self, user: str, password: str):
        r = self.sess.post(f"{TRPSERVER}/auth/login",
                           data={"user": user, "pw": password},
                           headers={"Content-Type": "application/x-www-form-urlencoded"})
        r.raise_for_status()
        data = r.json()
        self.user_id = data.get("userId")
        print(f"  Logged in as userId={self.user_id}")

    # ── Collections ───────────────────────────────────────────────────────────

    def get_or_create_collection(self, name: str) -> int:
        r = self.sess.get(f"{TRPSERVER}/collections/list")
        r.raise_for_status()
        for col in r.json():
            if col.get("colName") == name:
                cid = col["colId"]
                print(f"  Using existing collection '{name}' (id={cid})")
                return cid
        # Create
        r2 = self.sess.post(f"{TRPSERVER}/collections",
                            params={"collName": name})
        r2.raise_for_status()
        cid = r2.json()
        print(f"  Created collection '{name}' (id={cid})")
        return int(cid)

    # ── Model discovery ───────────────────────────────────────────────────────

    def find_hebrew_model(self) -> int | None:
        """Try to auto-discover a Hebrew HTR model from public models."""
        try:
            r = self.sess.get(f"{TRPSERVER}/recognition/htrModels",
                              params={"showPublic": True})
            r.raise_for_status()
            for m in r.json():
                name = (m.get("name") or "").lower()
                lang = (m.get("language") or "").lower()
                if "hebrew" in name or "hebrew" in lang or "heb" in lang:
                    mid = m["htrModelId"]
                    print(f"  Found Hebrew model: id={mid} '{m.get('name')}'")
                    return mid
        except Exception as e:
            print(f"  [model discovery failed: {e}]", file=sys.stderr)
        return None

    # ── Document upload ───────────────────────────────────────────────────────

    def upload_image_as_doc(self, col_id: int, entry_id: str,
                            image_path: Path) -> int:
        """Upload a single image as a one-page Transkribus document."""
        md = {
            "md": {
                "title": entry_id,
                "author": "",
                "description": f"HASH corpus entry {entry_id}",
                "nrOfPages": 1,
            }
        }
        with open(image_path, "rb") as fh:
            r = self.sess.post(
                f"{TRPSERVER}/collections/{col_id}/createDocFromImages",
                files={"img": (image_path.name, fh, "image/jpeg")},
                data={"md": json.dumps(md["md"])},
            )
        r.raise_for_status()
        doc_id = r.json()
        return int(doc_id)

    # ── HTR job ───────────────────────────────────────────────────────────────

    def submit_htr(self, col_id: int, doc_id: int, model_id: int) -> int:
        payload = {
            "htrId": model_id,
            "doWriteKwsIndex": False,
            "colId": col_id,
            "docId": doc_id,
            "pageNr": 1,
        }
        r = self.sess.post(f"{TRPSERVER}/jobs/htrCITlab",
                           json=payload)
        r.raise_for_status()
        job_id = r.json().get("jobId") or r.json()
        return int(job_id)

    def wait_for_job(self, job_id: int, timeout: int = 300, poll: float = 5.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self.sess.get(f"{TRPSERVER}/jobs/{job_id}")
            r.raise_for_status()
            data = r.json()
            state = data.get("state", "")
            if state == "FINISHED":
                return data
            if state in ("FAILED", "CANCELED"):
                raise RuntimeError(f"Job {job_id} ended with state={state}")
            time.sleep(poll)
        raise TimeoutError(f"Job {job_id} did not finish within {timeout}s")

    # ── Transcript retrieval ──────────────────────────────────────────────────

    def get_page_transcript(self, col_id: int, doc_id: int, page: int = 1) -> str:
        r = self.sess.get(
            f"{TRPSERVER}/collections/{col_id}/docs/{doc_id}/pages/{page}/transcripts"
        )
        r.raise_for_status()
        transcripts = r.json()
        if not transcripts:
            return ""
        # Most-recent transcript first
        latest = transcripts[0]
        ts_id  = latest.get("tsId")
        r2 = self.sess.get(
            f"{TRPSERVER}/collections/{col_id}/docs/{doc_id}/pages/{page}/transcripts/{ts_id}"
        )
        r2.raise_for_status()
        ts_data = r2.json()
        # Extract plain text from PAGE XML textLines
        return _extract_plain_text(ts_data)

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def delete_doc(self, col_id: int, doc_id: int):
        try:
            r = self.sess.delete(
                f"{TRPSERVER}/collections/{col_id}/docs/{doc_id}"
            )
            r.raise_for_status()
        except Exception as e:
            print(f"  [cleanup warning: {e}]", file=sys.stderr)

    def logout(self):
        try:
            self.sess.post(f"{TRPSERVER}/auth/logout")
        except Exception:
            pass


def _extract_plain_text(ts_data: dict) -> str:
    """Extract plain text lines from Transkribus transcript JSON."""
    lines = []
    for region in ts_data.get("regions", []):
        for tl in region.get("tLines", []):
            text = tl.get("tl_text", "") or ""
            if text.strip():
                lines.append(text.strip())
    if not lines:
        # Fallback: look for any 'text' field
        text = ts_data.get("tl_text") or ts_data.get("text") or ""
        if text.strip():
            lines.append(text.strip())
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def load_entries() -> list[dict]:
    rows = []
    with open(ENTRIES_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def needs_transcription(entry: dict) -> bool:
    """True if the entry has no approved transcript yet."""
    t = entry.get("transcription") or {}
    return t.get("status", "none") in ("none", "raw", "rejected")


def candidate_path(entry_id: str) -> Path:
    return CANDIDATES_DIR / f"{entry_id}.transkribus.txt"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--entry-id")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="Seconds between entries (default: 1.0)")
    ap.add_argument("--skip-existing", action="store_true", default=True,
                    help="Skip entries that already have a candidate file")
    args = ap.parse_args()

    user = os.environ.get("TRANSKRIBUS_USER")
    pw   = os.environ.get("TRANSKRIBUS_PASSWORD")
    if not user or not pw:
        sys.exit(
            "ERROR: Set TRANSKRIBUS_USER and TRANSKRIBUS_PASSWORD env vars.\n"
            "Sign up free at https://transkribus.eu and use your login credentials."
        )

    model_id_env = os.environ.get("TRANSKRIBUS_MODEL_ID")
    model_id = int(model_id_env) if model_id_env else None

    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)

    entries = load_entries()

    if args.entry_id:
        to_process = [e for e in entries if e["entry_id"] == args.entry_id]
    else:
        to_process = [e for e in entries if needs_transcription(e)]

    if args.skip_existing:
        to_process = [e for e in to_process
                      if not candidate_path(e["entry_id"]).exists()]

    if args.limit:
        to_process = to_process[:args.limit]

    print(f"Entries to transcribe (Transkribus): {len(to_process)}")
    if args.dry_run:
        for e in to_process:
            sp = find_scan_path(e)
            print(f"  {e['entry_id']}  scan={sp.name if sp else 'MISSING'}")
        return

    # Connect
    print("\nConnecting to Transkribus...")
    ts = TranskribusSession(user, pw)

    if model_id is None:
        model_id = ts.find_hebrew_model()
        if model_id is None:
            sys.exit(
                "ERROR: Could not auto-discover a Hebrew HTR model.\n"
                "Set TRANSKRIBUS_MODEL_ID to a model ID from your Transkribus account.\n"
                "In the Transkribus app: Tools → HTR → select a Hebrew model → note the ID."
            )

    col_id = ts.get_or_create_collection(COLLECTION_NAME)
    print(f"Model ID: {model_id}  |  Collection: {col_id}\n")

    done = skipped = errors = 0

    try:
        for i, entry in enumerate(to_process, 1):
            eid = entry["entry_id"]
            scan = find_scan_path(entry)
            if scan is None:
                print(f"[{i}/{len(to_process)}] {eid} — no scan, skip")
                skipped += 1
                continue

            print(f"[{i}/{len(to_process)}] {eid}  ({scan.name})")
            doc_id = None
            try:
                doc_id = ts.upload_image_as_doc(col_id, eid, scan)
                print(f"  uploaded doc_id={doc_id}")

                job_id = ts.submit_htr(col_id, doc_id, model_id)
                print(f"  job_id={job_id} … ", end="", flush=True)

                ts.wait_for_job(job_id)
                print("done")

                text = ts.get_page_transcript(col_id, doc_id)
                candidate_path(eid).write_text(text, encoding="utf-8")
                print(f"  → {len(text)} chars")
                done += 1

            except Exception as e:
                print(f"\n  [ERROR] {e}", file=sys.stderr)
                errors += 1

            finally:
                if doc_id:
                    ts.delete_doc(col_id, doc_id)

            if i < len(to_process):
                time.sleep(args.delay)

    finally:
        ts.logout()

    print(f"\nDone: {done}  Skipped: {skipped}  Errors: {errors}")


if __name__ == "__main__":
    main()
