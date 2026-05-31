# HASH — Hebrew Archive of Scanned Handwriting

[![CI](https://github.com/HeOCR/hash/actions/workflows/ci.yml/badge.svg)](https://github.com/HeOCR/hash/actions/workflows/ci.yml)
[![Metadata license: CC0](https://img.shields.io/badge/metadata%20license-CC0%201.0-blue)](LICENSE)
[![Scans indexed](https://img.shields.io/badge/scans%20indexed-198-0f766e)](data/index/entries.jsonl)

A simple, agent-friendly dataset of scanned handwritten Hebrew-script documents
— notes, letters, notebook pages, drafts, forms, and similar material — from
the 18th century onward. The focus is everyday cursive Hebrew handwriting
(כתב יד, not דפוס); Yiddish documents written in the same Hebrew round script
are also in scope.

Created by [Shay Palachy Affek](http://www.shaypalachy.com/).

The target corpus is limited to scans that can be redistributed and transformed
for downstream uses, including substantial remixing and machine-learning
datasets. The index therefore keeps rights evidence at both source and scan
level instead of assuming that a collection-level label applies to every page.

## Sample Scans

![Representative HASH scan samples](docs/assets/hash-sample-grid.jpg)

These samples are drawn from scan records whose rights are recorded as public
domain, Public Domain Mark, or Israel public-domain terms in
[`data/index/entries.jsonl`](data/index/entries.jsonl). Always consult the
per-scan record before redistributing or transforming a specific image.

## At a Glance

| Field | Current value |
|---|---|
| Ingested scans | 198 |
| Verified sources | 48 |
| Candidate leads still under research | 15 |
| Provenance-only rejected source records | 46 |
| Corpus size on disk | ~283.00 MiB |
| Repository-authored metadata license | [CC0 1.0](LICENSE) |
| Per-scan rights policy | [`LICENSE.md`](LICENSE.md) |
| Canonical scan index | [`data/index/entries.jsonl`](data/index/entries.jsonl) |

## Dataset Layout

- `docs/sources/` contains raw research notes and source leads.
- `docs/dataset_structure.md` defines the repository layout and ingestion model.
- `data/index/sources.jsonl` is the source-level catalog, one JSON object per
  institution, collection, item, dataset, or source lead.
- `data/index/entries.jsonl` is the scan-level catalog, one JSON object
  per individual page, note, letter, or other scanned unit.
- `schemas/source.schema.json` and `schemas/entry.schema.json` define the
  machine-readable record contracts.
- `scripts/validate_indexes.py` validates JSONL records against the schemas and
  checks source/entry referential integrity.
- `LICENSE.md` documents the compound licensing policy for metadata and scans.

## Serialization Decision

The canonical editable indexes are newline-delimited JSON (`.jsonl`).

JSONL is deliberately used instead of CSV because these records need nested
rights evidence, multiple URLs, per-field provenance, quality measurements,
and acquisition state. The source of truth stays line-oriented, diffable,
streamable JSON.

## Flat Exports

For spreadsheet / pandas / data-engineering workflows the repo ships flat
derived views of the indexes under `exports/`:

- `exports/entries.csv` — one row per scan, with `files[role=="original"]`
  flattened into `file_*` columns and `creator_count` / `file_count`
  summary columns.
- `exports/sources.csv` — one row per source row.
- `exports/creators.csv` — one row per `(entry_id, creator)` pair (use this
  when you need creator names / death years / authority URLs without
  doing positional gymnastics inside a single cell).
- `dist/entries.parquet` — same shape as `entries.csv` with preserved
  types (nullable booleans, int64 file sizes). Produced under `dist/`
  (uncommitted build artefact).

Regenerate the exports and rebuild the release tarball with:

```bash
python3 -m pip install -r requirements-dev.txt
python3 scripts/validate_indexes.py
python3 -m pytest
make exports
make release
```

<!-- begin:status -->
## Current Status

The corpus currently contains 198 ingested scans drawn from 48 verified sources, totalling ~283.00 MiB on disk. The source-level index also tracks 15 candidate leads still being researched and 46 source records kept for provenance after being rejected as out of scope.

License breakdown across the 198 entries:

- 144 `LicenseRef-Public-Domain-Israel` (Public Domain (Israel; life + 70))
- 44 `PDM-1.0` (Public Domain Mark 1.0)
- 9 `CC-BY-SA-3.0` (Creative Commons Attribution-ShareAlike 3.0 Unported)
- 1 `LicenseRef-Public-Domain-Ukraine` (Public Domain (Ukraine; life + 70))
<!-- end:status -->

The repository uses a compound licensing model: repository-authored metadata
is dedicated to the public domain under CC0 1.0 (see [`LICENSE`](LICENSE)),
while per-scan rights are recorded individually in each entry. See
[`LICENSE.md`](LICENSE.md) for the full policy, including the CC BY-SA
ShareAlike caveat and the rules for remix-friendly release bundles.

## How to use this repo

- [`data/index/entries.jsonl`](data/index/entries.jsonl) is the source of
  truth for the scan-level corpus — one JSON object per scan, with rights
  evidence, file checksums, and provenance.
- [`data/index/sources.jsonl`](data/index/sources.jsonl) catalogs the
  upstream sources, including candidate leads and rejected records.
- [`schemas/entry.schema.json`](schemas/entry.schema.json) and
  [`schemas/source.schema.json`](schemas/source.schema.json) define the
  record contracts; [`scripts/validate_indexes.py`](scripts/validate_indexes.py)
  enforces them in CI.
- Contributors adding new scans should start with
  [`AGENTS.md`](AGENTS.md) for ingest rules, scope, and the pre-PR checklist.

## Credits

Created by [Shay Palachy Affek ](http://www.shaypalachy.com/) [[GitHub](https://github.com/shaypal5)]
