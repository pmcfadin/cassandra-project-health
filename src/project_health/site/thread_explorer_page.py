"""`/conversations/threads/` "Thread explorer" page (issue #122, DECISIONS.md
D27, amending COMMUNITY-HEALTH.md §7.3 -- "no deep links from aggregates to
raw messages" -- for **threads only**).

Turns the latest `snapshots/conversation_patterns/threads-<run date>.json`
(written by `project_health.private_run.publish.write_threads_snapshot`,
itself an allowlisted, hard-fail-checked sanitization of the owner-only
private run's `threads.jsonl` -- issue #122) into the filterable, sortable
table `templates/conversations_threads.html`/`static/thread_explorer.js`
render, reusing the governance commit-history table's own pattern (issue
#97): text filters, column sort, URL state, CSV/JSON export, full-width.

**Preliminary, by owner decision (D26/D27).** Every row here is classifier
output from the same fixed, uncalibrated 0.5 probability cutoff every other
conversation-patterns number uses -- never a validated score.

**No verdicts (D25), no "worst threads" view.** Default sort is date
descending; there is no severity-sorted default and no highlight styling
of negative rows (D27's own acceptance criterion) -- this module never
computes or exposes a ranking, only plain facts per thread. Column names
are neutral ("Outcome", "Peak intensity"), never "risk" or "severity".

**Public by design, for threads only (D27).** A thread's public archive
URL and subject line are intentionally published here -- "this is a public
mailing list and we are out there already" (owner). "Just link to pony
mail" (owner clarification): dev@ rows link only to their canonical Pony
Mail thread permalink on `lists.apache.org`; no other archive mirror, and
no in-site rendering of thread content. Every other §7 rule stays binding:
no per-message scores, no per-person data or names, no quotations.

An honest no-data state (no `threads-*.json` snapshot published yet)
returns `{"available": False}` and writes an empty rows file, matching
every other optional section on this site.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from project_health.classify.questions import MESSAGE_LEVEL_LABELS

REPO_URL = "https://github.com/pmcfadin/cassandra-project-health"
# `decisions_url` itself comes from `generate._common_page_context` (every
# page gets the same one) -- this module only needs its own section anchor.
DECISIONS_D27_ANCHOR = "d27-public-thread-level-conversation-explorer"

_DISAGREEMENT_ISSUE_TEMPLATE = "thread-score-disagreement.yml"
_NEW_ISSUE_URL = f"{REPO_URL}/issues/new"

VENUE_LABELS: dict[str, str] = {
    "mailing_list": "dev@ mailing list",
    "jira_comment": "JIRA comments",
}

# §2.2's fixed intensity-tier lookup table, verbatim (COMMUNITY-HEALTH.md).
_TIER_LABELS: dict[int, str] = {
    -2: "Closing / positive",
    -1: "Constructive / positive",
    0: "Neutral",
    1: "Substantive disagreement",
    2: "Non-substantive friction",
    3: "Hostile",
    4: "Attack",
}

# Short codes for the compact flagged-message-count cell (full label name
# in the header/tooltip, per issue #122's own acceptance criterion).
_LABEL_SHORT_CODES: dict[str, str] = {
    "acknowledgment": "Ack",
    "compromise_offer": "Comp",
    "constructive_counterargument": "CCA",
    "dismissiveness": "Dism",
    "evidence_based_argument": "EBA",
    "gatekeeping": "Gate",
    "hostility": "Host",
    "personal_attack": "PA",
    "resolution_marker": "Res",
    "sarcasm": "Sarc",
    "status_authority_invocation": "SAI",
    "technical_disagreement": "TechDis",
}

SORTED_LABELS: tuple[str, ...] = tuple(sorted(MESSAGE_LEVEL_LABELS))

# No "worst threads" default (D27): a plain, neutral chronological order.
DEFAULT_SORT_FIELD = "date"
DEFAULT_SORT_DIR = "desc"

DEFAULT_THREADS_ROWS_FILENAME = "conversation-threads.json"


def _venue_label(venue: str) -> str:
    return VENUE_LABELS.get(venue, venue)


def _humanize_label(label: str) -> str:
    return label.replace("_", " ").capitalize()


def _outcome_label(outcome: dict[str, Any]) -> str:
    """One of the five neutral outcome categories issue #122 names,
    verbatim: "resolved / escalated / de-escalated / abandoned after
    friction / none". A thread can carry more than one derived flag at
    once (e.g. it escalated *and* later de-escalated); this picks the
    single most informative one to show in one column, in a fixed
    priority order -- resolution and abandonment (§2.3's terminal
    outcomes) outrank the escalation/de-escalation *events* that happen
    along the way to either. `pile_on` isn't one of the five (it's a
    distinct §2.3 event, not a thread outcome) -- surfaced separately as
    its own boolean, never folded into this string."""
    if outcome.get("constructive_resolution"):
        return "resolved"
    if outcome.get("abandonment_after_friction"):
        return "abandoned after friction"
    if outcome.get("escalation"):
        return "escalated"
    if outcome.get("deescalation"):
        return "de-escalated"
    return "none"


def thread_disagreement_url(row: dict[str, Any]) -> str:
    """A pre-filled "Thread disagreement" GitHub issue-form link (issue
    #122: "'Disagree with this score?' link -> new GitHub issue template
    prefilled with the thread URL and its scores") -- mirrors
    `leaderboard_page.identity_correction_url`'s query-param prefill
    pattern, applied to `thread-score-disagreement.yml`'s `thread_url`/
    `scores` form fields."""
    label_summary = ", ".join(
        f"{label}={count}" for label, count in sorted(row["label_counts"].items())
    )
    outcome_line = f"Outcome: {_outcome_label(row['outcome'])}"
    if row["outcome"].get("pile_on"):
        outcome_line += " (pile-on)"
    tier = row["peak_intensity_tier"]
    scores_lines = [
        f"Venue: {_venue_label(row['venue'])}",
        f"Quarter: {row['quarter']}",
        outcome_line,
        f"Peak intensity tier: {tier} ({_TIER_LABELS.get(tier, 'unknown')})",
        f"Flagged-message counts (>=0.5): {label_summary or 'none'}",
    ]
    params = {
        "template": _DISAGREEMENT_ISSUE_TEMPLATE,
        "labels": "thread-score-disagreement",
        "thread_url": row["url"],
        "scores": "\n".join(scores_lines),
    }
    return f"{_NEW_ISSUE_URL}?{urlencode(params)}"


def _read_latest_threads_snapshot(data_dir: str | Path) -> dict[str, Any] | None:
    snapshot_dir = Path(data_dir) / "snapshots" / "conversation_patterns"
    if not snapshot_dir.is_dir():
        return None
    files = sorted(snapshot_dir.glob("threads-*.json"))
    if not files:
        return None
    # Filenames are `threads-<run date>.json` (ISO 8601), so a
    # lexicographic sort is a chronological sort; the last file is latest.
    return json.loads(files[-1].read_text(encoding="utf-8"))


def _row_context(row: dict[str, Any]) -> dict[str, Any]:
    started_at = row.get("started_at")
    date = started_at[:10] if started_at else ""
    year = date[:4] if date else ""
    label_counts = row.get("label_counts") or {}
    flagged = [
        {
            "label": label,
            "short": _LABEL_SHORT_CODES.get(label, label),
            "display": _humanize_label(label),
            "count": count,
        }
        for label, count in sorted(label_counts.items())
        if count
    ]
    return {
        "venue": row["venue"],
        "venue_label": _venue_label(row["venue"]),
        "date": date,
        "year": year,
        "started_at": started_at,
        "quarter": row["quarter"],
        "subject": row.get("subject") or "(no subject)",
        "url": row["url"],
        "thread_key": row["thread_key"],
        "n_messages": row["n_messages"],
        "n_distinct_participants": row["n_distinct_participants"],
        "outcome_label": _outcome_label(row["outcome"]),
        "pile_on": bool(row["outcome"].get("pile_on")),
        "peak_intensity_tier": row["peak_intensity_tier"],
        "peak_intensity_label": _TIER_LABELS.get(row["peak_intensity_tier"], "unknown"),
        "flagged": flagged,
        "label_counts": label_counts,
        "disagree_url": thread_disagreement_url(row),
    }


def _filter_options(rows: list[dict[str, Any]]) -> dict[str, Any]:
    venues = sorted({r["venue"] for r in rows})
    years = sorted({r["year"] for r in rows if r["year"]})
    outcomes = ["resolved", "escalated", "de-escalated", "abandoned after friction", "none"]
    return {
        "venues": [{"id": v, "label": _venue_label(v)} for v in venues],
        "years": years,
        "outcomes": outcomes,
        "labels": [
            {"id": label, "display": _humanize_label(label)} for label in SORTED_LABELS
        ],
    }


def _write_rows_json(path: Path, rows: list[dict[str, Any]]) -> None:
    payload = {"row_count": len(rows), "rows": rows}
    path.write_text(json.dumps(payload, separators=(",", ":")))


def build_thread_explorer_context(
    data_dir: str | Path,
    out_dir: str | Path,
    *,
    base_prefix: str,
    rows_filename: str = DEFAULT_THREADS_ROWS_FILENAME,
) -> dict[str, Any]:
    """Build `/conversations/threads/`'s context and write its
    downloadable `data/conversation-threads.json` rows file into
    `out_dir`. Honest empty state (no `threads-*.json` snapshot published
    yet) still returns a valid context so the page (and the links to it)
    render cleanly instead of crashing -- same convention as
    `conversation_patterns_page.build_conversation_patterns_context`.
    """
    snapshot = _read_latest_threads_snapshot(data_dir)
    data_out = Path(out_dir) / "data"
    data_out.mkdir(parents=True, exist_ok=True)
    rows_href = f"{base_prefix}data/{rows_filename}"

    # `decisions_url` is deliberately not included here -- every page
    # already gets it from `generate._common_page_context` (same DECISIONS.
    # md URL), and Jinja's `Template.render(**a, **b)` errors on a
    # duplicate keyword if this context repeated it.
    if snapshot is None:
        _write_rows_json(data_out / rows_filename, [])
        return {
            "available": False,
            "rows_href": rows_href,
            "row_count": 0,
            "filter_options": {"venues": [], "years": [], "outcomes": [], "labels": []},
            "default_sort_field": DEFAULT_SORT_FIELD,
            "default_sort_dir": DEFAULT_SORT_DIR,
            "decisions_d27_anchor": DECISIONS_D27_ANCHOR,
        }

    rows = [_row_context(r) for r in snapshot.get("threads", [])]
    _write_rows_json(data_out / rows_filename, rows)

    return {
        "available": True,
        "generated_at": snapshot.get("generated_at"),
        "rows_href": rows_href,
        "row_count": len(rows),
        "filter_options": _filter_options(rows),
        "default_sort_field": DEFAULT_SORT_FIELD,
        "default_sort_dir": DEFAULT_SORT_DIR,
        "decisions_d27_anchor": DECISIONS_D27_ANCHOR,
    }
