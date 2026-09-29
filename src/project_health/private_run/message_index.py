"""Per-message private index for the private Cassandra communication run
(issue #114, build item 1).

One JSON line per fetched/classified message, written to
`--out/message_index.jsonl`: venue, thread key, position in thread,
timestamp, parent (dev@: `In-Reply-To` resolved within the thread's
survivors; JIRA: the previous surviving comment in the stream -- see
`thread_derive`'s module docstring, ambiguity resolution #2), an
**author key hashed with this `--out` directory's per-run salt**
(`identity.py` -- never a raw sender string), and the `input_hash` that
joins a row to its `ClassificationRecord` in the Jev cache
(`jev_cache.jsonl`, same directory).

This file is **never read by `report.md`/`aggregates.json`** (those stay
aggregate-only, COMMUNITY-HEALTH.md §7.3/§7.4) -- it exists as the audit
trail issue #114 asks for: enough to reconstruct which messages fed which
thread-level derivation, without ever writing a raw message id, raw author
string, or any message text to disk. It is rebuilt from scratch on every
`private-run` invocation (including `--no-classify`, since dev@/JIRA text
is fetched every run regardless -- `runner.py`'s own docstring), not
appended to -- a stale row from a previous run's different `--quarters`
scope would otherwise linger forever.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from project_health.private_run.identity import hash_author

DEFAULT_MESSAGE_INDEX_FILENAME = "message_index.jsonl"


def build_entry(
    *,
    call_id: str,
    venue: str,
    quarter: str,
    thread_key: str,
    position: int,
    posted_at: str,
    parent_call_id: str | None,
    author_raw: str,
    salt: str,
    input_hash: str | None,
) -> dict[str, Any]:
    return {
        "call_id": call_id,
        "venue": venue,
        "quarter": quarter,
        "thread_key": thread_key,
        "position": position,
        "posted_at": posted_at,
        "parent_call_id": parent_call_id,
        "author_key": hash_author(author_raw, salt),
        "input_hash": input_hash,
    }


def write_message_index(
    out_dir: str | Path,
    entries: list[dict[str, Any]],
    filename: str = DEFAULT_MESSAGE_INDEX_FILENAME,
) -> Path:
    """Overwrite `out_dir/filename` with exactly `entries` (sorted by
    `call_id` for a deterministic diff across re-runs) -- a full rebuild,
    not an append, per this module's docstring."""
    path = Path(out_dir) / filename
    ordered = sorted(entries, key=lambda e: e["call_id"])
    with open(path, "w", encoding="utf-8") as fh:
        for entry in ordered:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    return path
