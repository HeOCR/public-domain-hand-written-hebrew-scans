"""
HASH Review App — local review UI for pending and verified scan batches.

Run:
    cd /path/to/hash
    pip install flask
    python scripts/review_app/app.py

Then open http://localhost:5757
"""

import json
import os
from pathlib import Path
from urllib.parse import quote as url_quote, unquote as url_unquote
from flask import Flask, abort, jsonify, render_template, request, send_file

# ── Paths ────────────────────────────────────────────────────────────────────
HERE  = Path(__file__).parent
REPO  = HERE.parent.parent          # hash/ root
DATA  = REPO / "data"
REVIEW_DIR  = DATA / "review"
INDEX_DIR   = DATA / "index"
AUDIT_DECISIONS_PATH = REVIEW_DIR / "audit_decisions.json"

app = Flask(__name__, template_folder=str(HERE / "templates"),
            static_folder=str(HERE / "static"))


# ── File-level cache (mtime-invalidated) ────────────────────────────────────
# Values are (mtime: float, records: list[dict]).
# _enrich_entries() makes shallow copies before adding _* keys so cached
# records are never mutated by view code.

_file_cache: dict[str, tuple[float, list]] = {}


def _load_jsonl_cached(path: Path) -> list[dict]:
    """Return parsed JSONL from *path*, re-reading only when mtime changes."""
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return []
    cached = _file_cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    records: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    except FileNotFoundError:
        pass
    _file_cache[key] = (mtime, records)
    return records


# Keep the non-cached variant for callers that explicitly want a fresh read
# (e.g. large pending batches that are not re-read during normal browsing).
def load_jsonl(path: Path) -> list[dict]:
    records: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    except FileNotFoundError:
        pass
    return records


# ── Data helpers ─────────────────────────────────────────────────────────────

def load_sources() -> dict[str, dict]:
    return {s["source_id"]: s
            for s in _load_jsonl_cached(INDEX_DIR / "sources.jsonl")}


def load_entries() -> list[dict]:
    return _load_jsonl_cached(INDEX_DIR / "entries.jsonl")


def load_pending_batches() -> list[dict]:
    """Return list of batch dicts for every data/review/*_pending.jsonl."""
    batches = []
    for p in sorted(REVIEW_DIR.glob("*_pending.jsonl")):
        batch_id = p.stem.replace("_pending", "")
        entries  = load_jsonl(p)                    # large; skip cache
        dec_path = REVIEW_DIR / f"{batch_id}_decisions.json"
        decisions: dict = {}
        if dec_path.exists():
            try:
                decisions = json.loads(dec_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        batches.append({
            "batch_id":      batch_id,
            "label":         batch_id.replace("_", " ").title(),
            "pending_path":  str(p),
            "decisions_path": str(dec_path),
            "entries":       entries,
            "decisions":     decisions,
            "total":         len(entries),
            "approved":  sum(1 for v in decisions.values() if v.get("status") == "approved"),
            "rejected":  sum(1 for v in decisions.values() if v.get("status") == "rejected"),
            "commented": sum(1 for v in decisions.values() if v.get("comment", "").strip()),
        })
    return batches


def load_audit_decisions(live_ids: set[str] | None = None) -> dict[str, dict]:
    """Load audit_decisions.json, optionally filtering to *live_ids*.

    Passing *live_ids* (the set of entry_ids currently in entries.jsonl)
    prevents stale decisions from removed entries from appearing in the UI.
    """
    try:
        data = json.loads(AUDIT_DECISIONS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if live_ids is not None:
        data = {k: v for k, v in data.items() if k in live_ids}
    return data


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

def _entry_image_url(entry: dict, role: str = "thumbnail") -> str | None:
    """Return the app URL for serving an entry's image file (images only, no PDFs)."""
    other = "original" if role == "thumbnail" else "thumbnail"
    primary, fallback = [], []
    for f in entry.get("files", []):
        lp = f.get("local_path")
        if not lp:
            continue
        # Skip non-image files (PDFs etc.) — they can't be displayed in <img>
        if Path(lp).suffix.lower() not in _IMAGE_EXTS:
            continue
        if f["role"] == role:
            primary.append(lp)
        elif f["role"] == other:
            fallback.append(lp)
    lp = (primary or fallback or [None])[0]
    return f"/scan/{lp}" if lp else None


def _transcript_info(entry: dict) -> dict:
    t   = entry.get("transcription") or {}
    r   = t.get("rights") or {}
    status = t.get("status", "none")
    return {
        "status":        status,
        "license":       r.get("license_expression"),
        "source_url":    t.get("source_url"),
        "has_transcript": status not in ("none", "unknown", None, ""),
    }


def rights_warning(entry: dict) -> str | None:
    """Return a warning string if rights are unclear, else None."""
    r = entry.get("rights") or {}
    if not r:
        return "No rights information recorded for this entry."
    if r.get("rights_basis", "unknown") == "unknown" or not r.get("license_expression"):
        return "License unknown — rights basis not established."
    if r.get("verification_status", "unverified") == "unverified":
        return "License unverified — evidence text not yet checked against source page."
    return None


def _enrich_entries(
    entries: list[dict],
    sources: dict[str, dict],
    decisions: dict[str, dict],
) -> list[dict]:
    """Return a new list of entry dicts with display fields added.

    Each entry is *shallow-copied* so that the cached raw records in
    _load_jsonl_cached() are never mutated by view code.
    """
    result = []
    for e in entries:
        e = {**e}                                   # shallow copy — don't mutate cache
        e["_thumb_url"]       = _entry_image_url(e, "thumbnail")
        e["_orig_url"]        = _entry_image_url(e, "original")
        e["_transcript"]      = _transcript_info(e)
        e["_rights_warning"]  = rights_warning(e)
        e["_decision"]        = decisions.get(e["entry_id"], {})
        src = sources.get(e["source_id"], {})
        e["_source_title"]    = (src.get("title")
                                 or e.get("holding_institution")
                                 or e["source_id"])
        e["_source_provider"] = src.get("provider") or e.get("holding_institution", "")
        result.append(e)
    return result


# ── Stats helpers ─────────────────────────────────────────────────────────────

_LICENSE_COLORS = {
    "PDM-1.0":                          "#3ecf8e",
    "CC0-1.0":                          "#3ecf8e",
    "CC-BY-4.0":                        "#6c8ef5",
    "CC-BY-SA-4.0":                     "#8b6cf5",
    "CC-BY-SA-3.0":                     "#8b6cf5",
    "CC-BY-SA-2.0":                     "#8b6cf5",
    "CC-BY-3.0":                        "#6c8ef5",
    "LicenseRef-Public-Domain-Israel":  "#3ecf8e",
    "LicenseRef-Public-Domain-Ukraine": "#3ecf8e",
}
_LICENSE_SHORT = {
    "LicenseRef-Public-Domain-Israel":  "PD (Israel)",
    "LicenseRef-Public-Domain-Ukraine": "PD (Ukraine)",
    "CC-BY-SA-3.0": "CC BY-SA 3.0",
    "CC-BY-SA-4.0": "CC BY-SA 4.0",
}


def compute_corpus_stats(entries: list[dict], sources: dict) -> dict:
    if not entries:
        return {}
    total = len(entries)

    writers: set[str] = set()
    for e in entries:
        for c in (e.get("creators") or []):
            if c.get("name"):
                writers.add(c["name"])

    lic_counts: dict[str, int] = {}
    for e in entries:
        lic = (e.get("rights") or {}).get("license_expression") or "unknown"
        lic_counts[lic] = lic_counts.get(lic, 0) + 1

    transcript_count = sum(
        1 for e in entries
        if (e.get("transcription") or {}).get("status")
        not in ("none", "unknown", None, "")
    )
    warned_count = sum(1 for e in entries if rights_warning(e))

    years: list[int] = []
    for e in entries:
        d = e.get("dates") or {}
        created = d.get("created", "") if isinstance(d, dict) else ""
        if created:
            try:
                years.append(int(str(created)[:4]))
            except (ValueError, TypeError):
                pass

    lics_sorted = sorted(lic_counts.items(), key=lambda x: -x[1])
    return {
        "total":            total,
        "source_count":     len({e["source_id"] for e in entries}),
        "writer_count":     len(writers),
        "transcript_count": transcript_count,
        "transcript_pct":   round(100 * transcript_count / total) if total else 0,
        "warned_count":     warned_count,
        "year_min":         min(years) if years else None,
        "year_max":         max(years) if years else None,
        "licenses": [
            {
                "expression": lic,
                "short":      _LICENSE_SHORT.get(lic, lic),
                "count":      cnt,
                "pct":        round(100 * cnt / total),
                "color":      _LICENSE_COLORS.get(lic, "#f5a623"),
            }
            for lic, cnt in lics_sorted
        ],
    }


# ── Group helpers ─────────────────────────────────────────────────────────────

def _writer_slug(name: str) -> str:
    return url_quote(name or "unknown", safe="")


def _group_by_writer(entries: list[dict]) -> list[dict]:
    by_w: dict[str, dict] = {}
    for e in entries:
        creators = e.get("creators") or []
        if not creators:
            key, name, death_year, authority_url = "__unknown__", "Unknown Author", None, None
        else:
            c = creators[0]
            name         = c.get("name") or "Unknown"
            key          = name
            death_year   = c.get("death_year")
            authority_url = c.get("authority_url")
        if key not in by_w:
            by_w[key] = {
                "slug":          _writer_slug(key),
                "name":          name,
                "death_year":    death_year,
                "authority_url": authority_url,
                "entries":       [],
            }
        by_w[key]["entries"].append(e)

    groups = []
    for g in by_w.values():
        elist = g["entries"]
        years: list[int] = []
        for e2 in elist:
            d = e2.get("dates") or {}
            created = d.get("created", "") if isinstance(d, dict) else ""
            if created:
                try:
                    years.append(int(str(created)[:4]))
                except (ValueError, TypeError):
                    pass
        g["entry_count"] = len(elist)
        g["date_range"]  = (f"{min(years)}–{max(years)}" if years else None)
        # Call _entry_image_url once per candidate (walrus avoids the double-call)
        g["sample_thumb"] = next(
            (url for e2 in elist if (url := _entry_image_url(e2, "thumbnail"))),
            None,
        )
        groups.append(g)
    return sorted(groups, key=lambda g: (-g["entry_count"], g["name"]))


def _group_by_source(entries: list[dict], sources: dict) -> list[dict]:
    by_s: dict[str, dict] = {}
    for e in entries:
        sid = e["source_id"]
        if sid not in by_s:
            src = sources.get(sid, {})
            by_s[sid] = {
                "source_id": sid,
                "title":     src.get("title") or sid,
                "provider":  src.get("provider") or "",
                "entries":   [],
            }
        by_s[sid]["entries"].append(e)

    groups = []
    for g in by_s.values():
        elist = g["entries"]
        lics  = sorted({(e.get("rights") or {}).get("license_expression")
                        for e in elist} - {None})
        g["entry_count"] = len(elist)
        g["licenses"]    = lics
        g["sample_thumb"] = next(
            (url for e in elist if (url := _entry_image_url(e, "thumbnail"))),
            None,
        )
        groups.append(g)
    return sorted(groups, key=lambda g: (-g["entry_count"], g["title"]))


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    sources       = load_sources()
    all_entries   = load_entries()
    live_ids      = {e["entry_id"] for e in all_entries}
    decisions     = load_audit_decisions(live_ids)
    enriched      = _enrich_entries(all_entries, sources, decisions)
    writer_groups = _group_by_writer(all_entries)
    source_groups = _group_by_source(all_entries, sources)
    stats         = compute_corpus_stats(all_entries, sources)
    return render_template(
        "index.html",
        pending_batches=load_pending_batches(),
        writer_groups=writer_groups,
        source_groups=source_groups,
        all_entries=enriched,
        total_entries=len(all_entries),
        stats=stats,
    )


@app.route("/source/<source_id>")
def source_detail(source_id: str):
    sources = load_sources()
    # 404 on unknown source, not on "source with no entries"
    if source_id not in sources:
        abort(404)
    all_entries = load_entries()
    entries     = [e for e in all_entries if e["source_id"] == source_id]
    live_ids    = {e["entry_id"] for e in entries}
    decisions   = load_audit_decisions(live_ids)
    enriched    = _enrich_entries(entries, sources, decisions)
    src         = sources[source_id]
    return render_template(
        "group.html",
        group_type="source",
        group_title=src.get("title") or source_id,
        group_subtitle=src.get("provider") or "",
        authority_url=None,
        back_param="source",
        entries=enriched,
    )


@app.route("/writer/<slug>")
def writer_detail(slug: str):
    name        = url_unquote(slug)
    all_entries = load_entries()

    if slug == _writer_slug("__unknown__"):
        entries      = [e for e in all_entries if not (e.get("creators") or [])]
        display_name = "Unknown Author"
        death_year   = None
        authority_url = None
    else:
        entries = [e for e in all_entries
                   if any(c.get("name") == name
                          for c in (e.get("creators") or []))]
        display_name  = name
        death_year    = None
        authority_url = None
        for e in entries:
            for c in (e.get("creators") or []):
                if c.get("name") == name:
                    death_year    = c.get("death_year")
                    authority_url = c.get("authority_url")
                    break
            if death_year is not None:
                break

    if not entries:
        abort(404)

    sources   = load_sources()
    live_ids  = {e["entry_id"] for e in entries}
    decisions = load_audit_decisions(live_ids)
    enriched  = _enrich_entries(entries, sources, decisions)

    return render_template(
        "group.html",
        group_type="writer",
        group_title=display_name,
        group_subtitle=f"d. {death_year}" if death_year else "",
        authority_url=authority_url,
        back_param="writer",
        entries=enriched,
    )


@app.route("/review/<batch_id>")
def review_batch(batch_id: str):
    batches = {b["batch_id"]: b for b in load_pending_batches()}
    if batch_id not in batches:
        abort(404)
    batch   = batches[batch_id]
    sources = load_sources()

    # Derive source from the batch's own entries instead of hardcoding
    source_ids = [e.get("source_id") for e in batch["entries"] if e.get("source_id")]
    primary_sid = max(set(source_ids), key=source_ids.count) if source_ids else None
    source = sources.get(primary_sid, {}) if primary_sid else {}

    for e in batch["entries"]:
        e["_thumb_url"] = _entry_image_url(e, "thumbnail")
        e["_orig_url"]  = _entry_image_url(e, "original")
        e["_decision"]  = batch["decisions"].get(e["entry_id"], {})

    return render_template("batch.html", batch=batch, source=source)


@app.route("/scan/<path:rel_path>")
def serve_scan(rel_path: str):
    """Serve a scan image by its repo-relative path."""
    full = (REPO / rel_path).resolve()
    # Guard against path traversal (e.g. ../../etc/passwd)
    try:
        full.relative_to(REPO.resolve())
    except ValueError:
        abort(403)
    if not full.exists():
        abort(404)
    if full.suffix.lower() not in (".jpg", ".jpeg", ".png", ".gif",
                                   ".tif", ".tiff", ".pdf"):
        abort(403)
    return send_file(str(full))


@app.route("/api/batch/<batch_id>/decide", methods=["POST"])
def save_decisions(batch_id: str):
    """Merge review decisions for a batch (does not clobber pre-existing ones)."""
    batches = {b["batch_id"]: b for b in load_pending_batches()}
    if batch_id not in batches:
        return jsonify({"error": "batch not found"}), 404

    incoming = request.get_json(force=True)
    dec_path = Path(batches[batch_id]["decisions_path"])

    # Merge with whatever's already on disk so a partial save doesn't clobber
    existing: dict = {}
    if dec_path.exists():
        try:
            existing = json.loads(dec_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    existing.update(incoming)

    dec_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    approved = sum(1 for v in existing.values() if v.get("status") == "approved")
    rejected = sum(1 for v in existing.values() if v.get("status") == "rejected")
    return jsonify({"ok": True, "approved": approved, "rejected": rejected})


@app.route("/api/batch/<batch_id>/status")
def batch_status(batch_id: str):
    batches = {b["batch_id"]: b for b in load_pending_batches()}
    if batch_id not in batches:
        return jsonify({"error": "not found"}), 404
    b = batches[batch_id]
    return jsonify({
        "batch_id": batch_id,
        "total":    b["total"],
        "rejected": b["rejected"],
        "approved": b["total"] - b["rejected"],
    })


@app.route("/audit")
def audit():
    sources     = load_sources()
    all_entries = load_entries()
    live_ids    = {e["entry_id"] for e in all_entries}
    decisions   = load_audit_decisions(live_ids)
    enriched    = _enrich_entries(all_entries, sources, decisions)
    rejected    = sum(1 for d in decisions.values() if d.get("status") == "rejected")
    commented   = sum(1 for d in decisions.values() if d.get("comment", "").strip())
    warned      = sum(1 for e in enriched if e["_rights_warning"])
    return render_template(
        "audit.html",
        entries=enriched,
        decisions=decisions,
        total=len(enriched),
        rejected=rejected,
        commented=commented,
        warned=warned,
    )


@app.route("/api/audit/decisions")
def get_audit_decisions():
    all_entries = load_entries()
    live_ids    = {e["entry_id"] for e in all_entries}
    return jsonify(load_audit_decisions(live_ids))


@app.route("/api/audit/decide", methods=["POST"])
def save_audit_decisions():
    data = request.get_json(force=True)
    AUDIT_DECISIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUDIT_DECISIONS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    rejected = sum(1 for v in data.values() if v.get("status") == "rejected")
    return jsonify({"ok": True, "rejected": rejected, "total": len(data)})


# ── Transcript review routes ──────────────────────────────────────────────────

TRANSCRIPT_STATUSES = {"none", "raw", "reviewed", "aligned", "rejected"}
TRANSCRIPTS_DIR = REPO / "data" / "transcripts"
CANDIDATES_DIR  = TRANSCRIPTS_DIR / "candidates"

CANDIDATE_SOURCES = ["transkribus", "escriptorium", "consolidated"]
CANDIDATE_LABELS  = {
    "transkribus":   "Transkribus",
    "escriptorium":  "eScriptorium",
    "consolidated":  "Consolidated",
}


def _list_candidates(entry_id: str) -> dict[str, str | None]:
    """Return {source: text} for all three candidates; None if file absent."""
    result = {}
    for src in CANDIDATE_SOURCES:
        p = CANDIDATES_DIR / f"{entry_id}.{src}.txt"
        result[src] = p.read_text(encoding="utf-8") if p.exists() else None
    return result


def _candidates_ready(entry_id: str) -> int:
    """Count how many candidate files exist for this entry."""
    return sum(
        1 for src in CANDIDATE_SOURCES
        if (CANDIDATES_DIR / f"{entry_id}.{src}.txt").exists()
    )


def _load_transcript_text(entry: dict) -> str | None:
    """Return text content of the transcript file, or None."""
    t = entry.get("transcription") or {}
    path_str = t.get("text_path")
    if not path_str:
        return None
    path = REPO / path_str
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _save_all_entries(entries: list[dict]):
    """Write entries list back to entries.jsonl."""
    path = INDEX_DIR / "entries.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    # Invalidate cache
    _file_cache.pop(str(path), None)


@app.route("/transcripts")
def transcript_list():
    """Transcript review list page."""
    all_entries   = load_entries()
    filter_status = request.args.get("status", "pending")
    # "pending" = has candidates but no approved transcript yet

    enriched = []
    for e in all_entries:
        t      = e.get("transcription") or {}
        status = t.get("status", "none")
        eid    = e["entry_id"]
        n_cand = _candidates_ready(eid)

        show = False
        if filter_status == "all":
            show = True
        elif filter_status == "pending":
            show = n_cand > 0 and status in ("none", "raw", "rejected")
        elif filter_status == "reviewed":
            show = status == "reviewed"
        elif filter_status == "aligned":
            show = status == "aligned"
        elif filter_status == "none":
            show = n_cand == 0 and status == "none"
        else:
            show = status == filter_status

        if not show:
            continue

        ee = dict(e)
        ee["_t_status"]    = status
        ee["_t_text"]      = _load_transcript_text(e)
        ee["_t_snippet"]   = (ee["_t_text"] or "")[:120].replace("\n", " ")
        ee["_thumb_url"]   = _entry_image_url(e, "thumbnail")
        ee["_n_candidates"] = n_cand
        # Per-source availability flags for the dot indicators
        ee["_cand_ready"] = {
            src: (CANDIDATES_DIR / f"{eid}.{src}.txt").exists()
            for src in CANDIDATE_SOURCES
        }
        enriched.append(ee)

    # Counts for filter tabs
    counts = {"none": 0, "reviewed": 0, "aligned": 0, "pending": 0, "all": 0}
    for e in all_entries:
        s   = (e.get("transcription") or {}).get("status", "none")
        nc  = _candidates_ready(e["entry_id"])
        counts["all"] += 1
        if nc > 0 and s in ("none", "raw", "rejected"):
            counts["pending"] += 1
        elif s == "reviewed":
            counts["reviewed"] += 1
        elif s == "aligned":
            counts["aligned"] += 1
        elif nc == 0 and s == "none":
            counts["none"] += 1

    return render_template(
        "transcript_list.html",
        entries=enriched,
        filter_status=filter_status,
        counts=counts,
        candidate_sources=CANDIDATE_SOURCES,
        candidate_labels=CANDIDATE_LABELS,
    )


@app.route("/transcript/<entry_id>")
def transcript_review(entry_id: str):
    """Single-entry transcript review page."""
    all_entries = load_entries()
    entry_map   = {e["entry_id"]: e for e in all_entries}

    if entry_id not in entry_map:
        abort(404)

    entry      = entry_map[entry_id]
    t          = entry.get("transcription") or {}
    status     = t.get("status", "none")
    text       = _load_transcript_text(entry)
    candidates = _list_candidates(entry_id)

    # Build ordered list for prev/next navigation
    filter_status = request.args.get("from", "pending")

    def _matches_filter(e: dict) -> bool:
        s  = (e.get("transcription") or {}).get("status", "none")
        nc = _candidates_ready(e["entry_id"])
        if filter_status == "all":
            return True
        if filter_status == "pending":
            return nc > 0 and s in ("none", "raw", "rejected")
        if filter_status == "reviewed":
            return s == "reviewed"
        return s == filter_status

    ordered = [e["entry_id"] for e in all_entries if _matches_filter(e)]
    try:
        pos     = ordered.index(entry_id)
        prev_id = ordered[pos - 1] if pos > 0     else None
        next_id = ordered[pos + 1] if pos < len(ordered) - 1 else None
    except ValueError:
        pos, prev_id, next_id = 0, None, None

    return render_template(
        "transcript_review.html",
        entry=entry,
        t_status=status,
        t_text=text or "",
        t_rights=t.get("rights") or {},
        candidates=candidates,
        candidate_sources=CANDIDATE_SOURCES,
        candidate_labels=CANDIDATE_LABELS,
        thumb_url=_entry_image_url(entry, "thumbnail"),
        orig_url=_entry_image_url(entry, "original"),
        prev_id=prev_id,
        next_id=next_id,
        pos=pos + 1,
        total=len(ordered),
        filter_status=filter_status,
    )


@app.route("/api/transcript/<entry_id>", methods=["GET"])
def get_transcript(entry_id: str):
    """Return transcript text, metadata, and all candidates for an entry."""
    all_entries = load_entries()
    entry_map   = {e["entry_id"]: e for e in all_entries}
    if entry_id not in entry_map:
        return jsonify({"error": "not found"}), 404

    entry      = entry_map[entry_id]
    t          = entry.get("transcription") or {}
    text       = _load_transcript_text(entry)
    candidates = _list_candidates(entry_id)
    return jsonify({
        "entry_id":   entry_id,
        "status":     t.get("status", "none"),
        "text":       text,
        "created_by": t.get("created_by"),
        "rights":     t.get("rights") or {},
        "candidates": candidates,
    })


@app.route("/api/candidate/<entry_id>/<source>", methods=["GET"])
def get_candidate(entry_id: str, source: str):
    """Return a single candidate transcript text."""
    if source not in CANDIDATE_SOURCES:
        return jsonify({"error": f"unknown source: {source}"}), 400
    p = CANDIDATES_DIR / f"{entry_id}.{source}.txt"
    if not p.exists():
        return jsonify({"entry_id": entry_id, "source": source, "text": None}), 404
    return jsonify({"entry_id": entry_id, "source": source,
                    "text": p.read_text(encoding="utf-8")})


@app.route("/api/transcript/<entry_id>", methods=["POST"])
def save_transcript(entry_id: str):
    """Save corrected transcript text and optionally promote status."""
    all_entries = load_entries()
    entry_index = {e["entry_id"]: i for i, e in enumerate(all_entries)}

    if entry_id not in entry_index:
        return jsonify({"error": "not found"}), 404

    payload    = request.get_json(force=True)
    new_text   = payload.get("text", "")
    new_status = payload.get("status", "reviewed")

    if new_status not in TRANSCRIPT_STATUSES:
        return jsonify({"error": f"invalid status: {new_status}"}), 400

    idx   = entry_index[entry_id]
    entry = dict(all_entries[idx])
    t     = dict(entry.get("transcription") or {})

    # Determine / create text_path
    rel_path = t.get("text_path") or f"data/transcripts/{entry_id}.txt"
    abs_path = REPO / rel_path

    # Write the transcript file
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(new_text, encoding="utf-8")

    # Promote verification_status when human reviews
    rights = dict(t.get("rights") or {})
    if new_status in ("reviewed", "aligned"):
        rights["verification_status"] = "primary_page_checked"
        import datetime
        rights["verified_at"] = datetime.date.today().isoformat()

    t["status"]    = new_status
    t["text_path"] = rel_path
    t["rights"]    = rights
    entry["transcription"] = t
    all_entries[idx] = entry

    _save_all_entries(all_entries)
    return jsonify({"ok": True, "entry_id": entry_id, "status": new_status,
                    "chars": len(new_text)})


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5757))
    print(f"\n  HASH Review  →  http://localhost:{port}\n")
    app.run(host="127.0.0.1", port=port, debug=False)
