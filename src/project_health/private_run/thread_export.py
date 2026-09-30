"""Public per-thread export for the private Cassandra communication run
(issue #122; DECISIONS.md D27, amending COMMUNITY-HEALTH.md §7.3 -- "no
deep links from aggregates to raw messages" -- for **threads only**).

Owner decision, 2026-09-29: "This is a public mailing list and we are out
there already." Every sampled, classified thread's public archive URL and
subject line publish -- dev@ threads link to their Pony Mail thread
permalink on `lists.apache.org` (the canonical ASF archive UI: "Just link
to pony mail" -- no markmail, no mail-archive.com, no other mirror, and no
in-site rendering of thread content), JIRA "threads" (one issue's full
comment stream) link to `issues.apache.org/jira/browse/<KEY>` (Pony Mail
does not host JIRA comment streams as threads, so this is the issue
tracker's own permalink, not an archive mirror). Every other D2.4/§7.3 rule
stays binding: no per-message scores, no per-person data or names, no
quotations, no verdict wording (D25) -- see this module's own hard-fail
allowlist counterpart, `publish.sanitize_threads`.

Writes `--out/threads.jsonl`: one JSON line per sampled thread that
produced at least one classified message at the headline (0.5 probability)
cutoff (`runner.py`'s own `THREAD_DERIVE_HEADLINE_CUTOFF`) -- venue, a
thread key, its public URL, its subject (public metadata fetched with the
thread, dev@ via Pony Mail's live digest API, JIRA via the already-
collected `jira/issue.summary` column -- never a message body), when it
started, its (venue, quarter) sample cell, message/participant counts,
COMMUNITY-HEALTH.md §2.3's derived outcome flags, its peak §2.2 intensity
tier, and per-label counts of messages whose classifier probability
cleared 0.5 -- never a per-message probability, a message id, or an author
key.

Rebuilt from scratch on every `private-run` invocation (including
`--no-classify`, since dev@/JIRA text/metadata is fetched every run
regardless -- `runner.py`'s own module docstring), never appended to --
same convention as `message_index.py`'s `write_message_index`, so a stale
row from a previous run's different `--quarters` scope never lingers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_THREADS_FILENAME = "threads.jsonl"


def build_thread_row(
    *,
    venue: str,
    thread_key: str,
    url: str,
    subject: str | None,
    started_at: str | None,
    quarter: str,
    n_messages: int,
    n_distinct_participants: int,
    escalation: bool,
    deescalation: bool,
    constructive_resolution: bool,
    abandonment_after_friction: bool,
    pile_on: bool,
    peak_intensity_tier: int,
    label_counts: dict[str, int],
) -> dict[str, Any]:
    """One `threads.jsonl` row -- every field named explicitly (never a
    passthrough of a caller's own dict), so a field this module doesn't
    know about can never ride along silently into the export (same
    discipline `publish.py`'s leaf-shape copiers already use)."""
    return {
        "venue": venue,
        "thread_key": thread_key,
        "url": url,
        "subject": subject,
        "started_at": started_at,
        "quarter": quarter,
        "n_messages": n_messages,
        "n_distinct_participants": n_distinct_participants,
        "outcome": {
            "escalation": escalation,
            "deescalation": deescalation,
            "constructive_resolution": constructive_resolution,
            "abandonment_after_friction": abandonment_after_friction,
            "pile_on": pile_on,
        },
        "peak_intensity_tier": peak_intensity_tier,
        "label_counts": dict(sorted(label_counts.items())),
    }


def write_threads_jsonl(
    out_dir: str | Path,
    rows: list[dict[str, Any]],
    filename: str = DEFAULT_THREADS_FILENAME,
) -> Path:
    """Overwrite `out_dir/filename` with exactly `rows` (sorted by
    `(venue, quarter, thread_key)` for a deterministic diff across
    re-runs) -- a full rebuild, not an append, per this module's
    docstring."""
    path = Path(out_dir) / filename
    ordered = sorted(rows, key=lambda r: (r["venue"], r["quarter"], r["thread_key"]))
    with open(path, "w", encoding="utf-8") as fh:
        for row in ordered:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    return path
