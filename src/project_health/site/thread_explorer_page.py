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
from project_health.private_run.sample import DEFAULT_K
from project_health.site.conversation_patterns_page import CONSTRUCTIVE_LABELS, NEGATIVE_LABELS

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


# Fixed narrative order for `_outcome_display` -- escalation and de-
# escalation are *events along the way*, resolution/abandonment are §2.3's
# terminal states, so this reads left-to-right as roughly chronological.
_OUTCOME_DISPLAY_ORDER: tuple[tuple[str, str], ...] = (
    ("escalation", "escalated"),
    ("deescalation", "de-escalated"),
    ("constructive_resolution", "resolved"),
    ("abandonment_after_friction", "abandoned after friction"),
)


def _outcome_display(outcome: dict[str, Any]) -> str:
    """Every derived §2.3 outcome flag that's true for this thread,
    joined into one neutral narrative string (e.g. `"escalated, then
    de-escalated"`), or `"none"` if none are. A thread can carry more
    than one flag at once (escalation and de-escalation both true is
    common -- a thread climbs, then comes back down); a fixup round of
    issue #122 replaced this function's original single-value, priority-
    ordered design (which picked one flag and silently dropped the
    others) after a real sample row (a JIRA thread that both escalated
    and later de-escalated) showed only "escalated" and never surfaced
    its own `deescalation: true`. `pile_on` isn't one of these (a
    distinct §2.3 event, not a thread outcome) -- kept as its own
    separate boolean everywhere this is used, never folded into this
    string."""
    parts = [label for key, label in _OUTCOME_DISPLAY_ORDER if outcome.get(key)]
    return ", then ".join(parts) if parts else "none"


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
    outcome_line = f"Outcome: {_outcome_display(row['outcome'])}"
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
        "outcome_display": _outcome_display(row["outcome"]),
        # Individual flags (issue #122 fixup) -- the table's Outcome filter
        # matches on these directly, not on `outcome_display`'s composed
        # string, so filtering for "escalated" also finds a thread whose
        # display reads "escalated, then de-escalated".
        "outcome_flags": {
            "resolved": bool(row["outcome"].get("constructive_resolution")),
            "escalated": bool(row["outcome"].get("escalation")),
            "de-escalated": bool(row["outcome"].get("deescalation")),
            "abandoned after friction": bool(row["outcome"].get("abandonment_after_friction")),
        },
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


# --- Charts above the table (issue #124) ----------------------------------
#
# Both charts are driven by the *same* filter state as the table (venue,
# outcome, label, year range): `thread_explorer.js` recomputes each chart's
# data from `state.filtered` -- the identical row list the table itself
# renders -- on every filter change, using a JS port of the two pure
# derivation functions below (`year_outcome_rows`, `label_year_share_rows`).
# Those two functions are the single source of truth for the aggregation
# logic; this module uses them to build the page's *initial* (unfiltered)
# specs at build time, and they're what `test_site_thread_explorer.py`
# exercises directly against synthetic rows so the math is tested
# independent of any browser.
#
# Unweighted counts only (issue #124 acceptance #3): every count and share
# here is of *sampled* threads, never population-weighted -- the chart
# footnote says so and points to Conversation patterns for the
# population-weighted numbers.

# Fixed stacking/legend order (issue #124's own list) -- never alphabetical,
# same reasoning as `_OUTCOME_DISPLAY_ORDER`.
CHART_OUTCOME_CATEGORIES: tuple[str, ...] = (
    "none",
    "resolved",
    "escalated",
    "escalated, then de-escalated",
    "abandoned after friction",
)

# A neutral qualitative palette (D25: no verdicts) -- deliberately not a
# red/green stoplight scheme, which would editorialize "escalated" as bad
# and "resolved" as good.
_OUTCOME_COLOR_SCHEME = "tableau10"


def _chart_outcome_category(outcome_flags: dict[str, Any]) -> str:
    """One mutually-exclusive chart category per thread (issue #124) --
    unlike `_outcome_display`'s narrative string, which can carry more
    than one true flag at once, a stacked bar needs exactly one category
    per thread. Escalation takes priority over resolution, same
    left-to-right precedence `_OUTCOME_DISPLAY_ORDER` already uses."""
    if outcome_flags.get("escalated") and outcome_flags.get("de-escalated"):
        return "escalated, then de-escalated"
    if outcome_flags.get("escalated"):
        return "escalated"
    if outcome_flags.get("resolved"):
        return "resolved"
    if outcome_flags.get("abandoned after friction"):
        return "abandoned after friction"
    return "none"


def year_outcome_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Long-format `{year, outcome, count}` rows for the "threads per year
    by outcome" stacked-bar chart (issue #124) -- one row per (year,
    category) pair actually present in `rows`. `rows` is whatever subset
    the caller passes in: `thread_explorer.js`'s JS port of this function
    passes the table's currently *filtered* rows on every filter change;
    this Python function is called once, over every row, to build the
    page's initial unfiltered chart."""
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        year = row.get("year")
        if not year:
            continue
        category = _chart_outcome_category(row.get("outcome_flags") or {})
        key = (year, category)
        counts[key] = counts.get(key, 0) + 1
    return [
        {"year": year, "outcome": outcome, "count": count}
        for (year, outcome), count in sorted(
            counts.items(),
            key=lambda kv: (kv[0][0], CHART_OUTCOME_CATEGORIES.index(kv[0][1])),
        )
    ]


def label_year_share_rows(
    rows: list[dict[str, Any]], labels: tuple[str, ...]
) -> list[dict[str, Any]]:
    """Long-format `{label, label_display, year, count, total, share}` rows
    for the "threads with >=1 flagged message, by label, year over year"
    small-multiples chart (issue #124, #133). `share` is this year's (within `rows`)
    fraction of threads with at least one message classified >=0.5 for
    `label` -- `count / total`, `total` being every row in `rows` for that
    year regardless of label. `rows` is whatever the caller passes in --
    the same "filter first with the table's own filter predicate, then
    aggregate" contract as `year_outcome_rows` above, so a test can verify
    "share with filter applied" just by filtering the input list before
    calling this function."""
    totals: dict[str, int] = {}
    for row in rows:
        year = row.get("year")
        if not year:
            continue
        totals[year] = totals.get(year, 0) + 1

    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        year = row.get("year")
        if not year:
            continue
        label_counts = row.get("label_counts") or {}
        for label in labels:
            if label_counts.get(label):
                key = (label, year)
                counts[key] = counts.get(key, 0) + 1

    years = sorted(totals)
    out = []
    for label in labels:
        for year in years:
            total = totals[year]
            count = counts.get((label, year), 0)
            out.append(
                {
                    "label": label,
                    "label_display": _humanize_label(label),
                    "year": year,
                    "count": count,
                    "total": total,
                    "share": (count / total) if total else 0.0,
                }
            )
    return out


def _year_outcome_chart_spec(rows: list[dict[str, Any]]) -> str | None:
    """Stacked-bar Vega-Lite spec for the "threads per year by outcome"
    chart -- one `mark: bar` layer, `stack: zero`, toggled client-side
    (`thread_explorer.js`) to `stack: normalize` for the counts <-> share
    of year toggle (issue #124). No CI band/rule layer: unlike the
    Conversation patterns section's rate-based charts, this is a raw
    count of sampled threads, not a population estimate with a confidence
    interval.

    Issue #133 fixup: this chart spans every year in the data (15+ once
    the project's history is long enough) on one un-faceted x-axis, so the
    small-multiples fix above doesn't apply here -- but the reviewer's
    "doesn't wrap / can't see the right side" complaint turned out to
    apply to this chart too, for a different mechanical reason. A plain
    numeric `width` (what `thread_explorer.js`'s `embedChart` sets from
    the container's resolved pixel width, same as before) only sizes the
    *plot body*; Vega-Lite's default `autosize: "pad"` then adds the
    color legend and axis chrome on top of that, so the rendered chart
    was always wider than its container once the legend's "escalated,
    then de-escalated"/"abandoned after friction" labels were counted
    (measured ~211px of overflow, constant across container widths --
    confirming it was exactly this, not the bars themselves). Moving the
    legend below the plot and setting `autosize: {"type": "fit-x", ...}`
    (which makes Vega-Lite shrink the plot body, not just pad outward, so
    legend + axis + plot together fit the given width exactly) measured
    zero overflow at every width from a narrow phone up, including with
    all 15+ years of bars visible at once -- no horizontal scroll or
    year-range default needed."""
    values = year_outcome_rows(rows)
    if not values:
        return None
    spec = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "width": "container",
        "height": 260,
        "autosize": {"type": "fit-x", "contains": "padding"},
        "data": {"values": values},
        "mark": {"type": "bar"},
        "encoding": {
            "x": {"field": "year", "type": "ordinal", "title": "Year"},
            "y": {
                "field": "count",
                "type": "quantitative",
                "title": "Threads",
                "stack": "zero",
            },
            "color": {
                "field": "outcome",
                "type": "nominal",
                "title": "Outcome",
                "scale": {
                    "domain": list(CHART_OUTCOME_CATEGORIES),
                    "scheme": _OUTCOME_COLOR_SCHEME,
                },
                "legend": {"orient": "bottom", "direction": "horizontal", "columns": 2},
            },
            "tooltip": [
                {"field": "year", "type": "ordinal", "title": "Year"},
                {"field": "outcome", "type": "nominal", "title": "Outcome"},
                {"field": "count", "type": "quantitative", "title": "Threads"},
            ],
        },
    }
    return json.dumps(spec)


def _label_year_group_spec(rows: list[dict[str, Any]], labels: tuple[str, ...]) -> str | None:
    """Faceted small-multiples Vega-Lite spec for one panel (constructive
    or negative) of the "threads with >=1 flagged message, by label, year
    over year" chart -- same visual language as `conversation_patterns_page.
    _yoy_group_spec` (issue #133, amending #120/#124's shared original
    grouped-bar-dodged-by-year design, after the same reviewer feedback
    applied to both: "graphs don't wrap", "colour should run along the
    x-axis, not the dodge/color group"): one mini-chart per label, x =
    year, y = share of that year's threads. One flat color per panel, not
    color-by-year -- time is already the x-axis. `thread_explorer.js`'s
    `applyFacetColumns` computes `facet.columns`/`spec.width` from the
    container's resolved pixel width on every embed/resize/filter-change,
    so panels wrap instead of ever scrolling horizontally.

    Independent y-scale per label (`resolve.scale.y: "independent"`), same
    reasoning as `_yoy_group_spec`'s own docstring: labels' shares differ
    enough in magnitude that a shared scale would flatten the rarer ones.

    No CI band (see `_year_outcome_chart_spec`'s docstring -- same
    reasoning: a raw sample share, not a population rate)."""
    values = label_year_share_rows(rows, labels)
    if not values:
        return None
    label_order = [_humanize_label(label) for label in labels]
    spec = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "facet": {
            "field": "label_display",
            "type": "nominal",
            "title": None,
            "sort": label_order,
        },
        "columns": 2,
        "resolve": {"scale": {"y": "independent"}},
        "spec": {
            "width": 220,
            "height": 140,
            "mark": {"type": "bar", "color": "#2b6cb0"},
            "encoding": {
                "x": {"field": "year", "type": "ordinal", "title": "Year"},
                "y": {
                    "field": "share",
                    "type": "quantitative",
                    "title": "Share of year's threads",
                    "axis": {"format": "%"},
                },
                "tooltip": [
                    {"field": "label_display", "type": "nominal", "title": "Label"},
                    {"field": "year", "type": "ordinal", "title": "Year"},
                    {"field": "share", "type": "quantitative", "title": "Share", "format": ".1%"},
                    {"field": "count", "type": "quantitative", "title": "Threads flagged"},
                    {"field": "total", "type": "quantitative", "title": "Threads (year total)"},
                ],
            },
        },
        "data": {"values": values},
    }
    return json.dumps(spec)


def _threads_chart_context(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Both charts' initial (unfiltered) specs, plus the sample-size note
    the template renders under them (issue #124: "Counts are of sampled
    threads (K=<n> per venue-quarter); for population-weighted rates see
    Conversation patterns")."""
    outcome_spec = _year_outcome_chart_spec(rows)
    constructive_spec = _label_year_group_spec(rows, CONSTRUCTIVE_LABELS)
    negative_spec = _label_year_group_spec(rows, NEGATIVE_LABELS)
    return {
        "available": bool(outcome_spec or constructive_spec or negative_spec),
        "outcome_spec_json": outcome_spec,
        "constructive_spec_json": constructive_spec,
        "negative_spec_json": negative_spec,
        "sample_k": DEFAULT_K,
    }


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
            "charts": {"available": False},
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
        "charts": _threads_chart_context(rows),
    }
