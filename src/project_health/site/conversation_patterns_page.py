"""`/conversations/` "Conversation patterns" section + Community page summary
card context builder (issue #118, DECISIONS.md D26).

Turns the latest `snapshots/conversation_patterns/<run date>.json` (written
by `project_health.private_run.publish.write_conversation_patterns_snapshot`,
itself an allowlisted, hard-fail-checked sanitization of the owner-only
private run's `aggregates.json` -- issue #110/#114) into everything
`templates/conversations.html`'s "Conversation patterns" section and
`templates/community.html`'s summary card need.

**Preliminary, by owner decision (D26).** This publishes ahead of
COMMUNITY-HEALTH.md §7.8's PMC preview/acknowledgment and issue #47's
production-threshold calibration -- every cutoff shown here is the fixed,
uncalibrated 0.5/0.7/0.9 headline/sensitivity cutoff the private run itself
used (`private_run/aggregate.py`'s own docstring), not a validated
per-label threshold. The page says so prominently; this module never
softens that into anything that reads as validated.

**No verdicts (D25).** Every string this module produces is a plain rate,
count, or "insufficient data" / "overlapping" / "not overlapping" label --
no pass/fail, no "healthy"/"unhealthy", no causal commentary on why any
year's numbers differ from another's (COMMUNITY-HEALTH.md §8).

**Tier `classified` on every metric** (issue #118's own acceptance
criterion) -- the template renders `badge--tier-classified` next to every
section this module contributes data to, matching every other tiered card
on this site (D2.1).

An honest no-data state (no `conversation_patterns` snapshot published yet)
returns `{"available": False}`, matching every other optional section on
this site (`review_responsiveness_page.py`'s own docstring) -- the site
generation reads the latest snapshot if present and renders nothing extra,
never an error, when it isn't.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from project_health.classify.questions import MESSAGE_LEVEL_LABELS

SORTED_LABELS: tuple[str, ...] = tuple(sorted(MESSAGE_LEVEL_LABELS))

# Issue #118 §2 item 4, verbatim: the two small-multiple chart groups.
CONSTRUCTIVE_LABELS: tuple[str, ...] = (
    "acknowledgment",
    "compromise_offer",
    "constructive_counterargument",
    "evidence_based_argument",
    "resolution_marker",
    "technical_disagreement",
)
NEGATIVE_LABELS: tuple[str, ...] = (
    "dismissiveness",
    "hostility",
    "personal_attack",
    "sarcasm",
    "gatekeeping",
    "status_authority_invocation",
)

VENUE_LABELS: dict[str, str] = {
    "mailing_list": "dev@ mailing list",
    "jira_comment": "JIRA comments",
}

# Issue #120's own two panels, in this fixed display order -- also the
# fixed x-axis label order for each panel's grouped-bar chart (never the
# alphabetical `SORTED_LABELS` order).
_YOY_LABEL_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("constructive", "Constructive / discussion", CONSTRUCTIVE_LABELS),
    ("negative", "Negative", NEGATIVE_LABELS),
)

# Issue #120 default range: "last 5 complete years" -- a year is complete
# once the calendar year after it has started.
_YOY_DEFAULT_RANGE_YEARS = 5

_EARLY_WINDOW = "2017_2019"
_RECENT_WINDOW = "2023_2025"
_WINDOW_LABELS = {_EARLY_WINDOW: "2017-2019", _RECENT_WINDOW: "2023-2025"}

REPO_URL = "https://github.com/pmcfadin/cassandra-project-health"
BENCHMARK_PUBLIC_URL = f"{REPO_URL}/blob/main/docs/benchmark/public-v1.md"
COMMUNITY_HEALTH_URL = f"{REPO_URL}/blob/main/docs/spec/COMMUNITY-HEALTH.md"
DECISIONS_URL = f"{REPO_URL}/blob/main/docs/spec/DECISIONS.md"
DECISIONS_D26_ANCHOR = (
    "d26-publish-conversation-patterns-page-preliminary-classified-aggregates"
)

_FEEDBACK_ISSUE_TEMPLATE = "conversation-patterns-feedback.yml"
_FEEDBACK_NEW_ISSUE_URL = f"{REPO_URL}/issues/new"

# Venues COMMUNITY-HEALTH.md's Phase 2a covers in principle (D7) but this
# private run's own `scope_note` says are out of scope for this snapshot --
# stated explicitly here (issue #118's "venues NOT covered") rather than
# left to a reader's inference from `venues` alone.
_ALL_PHASE2_VENUES = ("dev@ mailing list", "user@ mailing list", "JIRA comments",
                       "GitHub PR comments", "ASF Slack")


def conversation_patterns_feedback_url() -> str:
    """A pre-filled "Conversation patterns feedback" GitHub issue-form link
    (D26: "feedback link, existing corrections/issue link pattern" --
    mirrors `leaderboard_page.identity_correction_url`)."""
    params = {
        "template": _FEEDBACK_ISSUE_TEMPLATE,
        "labels": "conversation-patterns-feedback",
    }
    return f"{_FEEDBACK_NEW_ISSUE_URL}?{urlencode(params)}"


def _read_latest_snapshot(data_dir: str | Path) -> dict[str, Any] | None:
    snapshot_dir = Path(data_dir) / "snapshots" / "conversation_patterns"
    if not snapshot_dir.is_dir():
        return None
    # Issue #122 (D27) also writes `threads-<run date>.json` into this same
    # directory (`thread_explorer_page.py`'s own reader) -- excluded here
    # by its distinct `threads-` prefix so it's never mistaken for this
    # module's own `<run date>.json` aggregates snapshot (lexicographic
    # sort would otherwise put a "threads-..." filename after a plain date
    # and make it look like the latest run).
    files = sorted(f for f in snapshot_dir.glob("*.json") if not f.name.startswith("threads-"))
    if not files:
        return None
    # Filenames are `<run date>.json` (ISO 8601), so a lexicographic sort is
    # a chronological sort; the last file is the latest run.
    return json.loads(files[-1].read_text(encoding="utf-8"))


def _fmt_count_ci(entry: dict[str, Any] | None) -> str:
    if entry is None:
        return "insufficient data"
    lo, hi = entry["ci95"]
    return f"{entry['per_1000']:.2f} [{lo:.2f}, {hi:.2f}]"


def _fmt_overlap(value: bool | None) -> str:
    if value is None:
        return "insufficient data"
    return "overlapping" if value else "not overlapping"


def _fmt_rate_pct(entry: dict[str, Any] | None) -> str:
    if not entry or entry.get("insufficient_data") or entry.get("rate") is None:
        return "insufficient data"
    rate = entry["rate"]
    ci = entry.get("ci95")
    if ci:
        return f"{rate * 100:.1f}% [{ci[0] * 100:.1f}%, {ci[1] * 100:.1f}%]"
    return f"{rate * 100:.1f}%"


def _fmt_pile_on(entry: dict[str, Any] | None) -> str:
    if not entry or entry.get("insufficient_data") or entry.get("per_100_threads") is None:
        return "insufficient data"
    value = entry["per_100_threads"]
    ci = entry.get("ci95")
    if ci:
        return f"{value:.2f} [{ci[0]:.2f}, {ci[1]:.2f}] per 100 threads"
    return f"{value:.2f} per 100 threads"


def _fmt_newcomer_rate(entry: dict[str, Any], key: str) -> str:
    if entry.get("insufficient_data") or entry.get(key) is None:
        return "insufficient data"
    rate = entry[key]
    ci = entry.get(f"{key}_ci95")
    if ci:
        return f"{rate * 100:.1f}% [{ci[0] * 100:.1f}%, {ci[1] * 100:.1f}%]"
    return f"{rate * 100:.1f}%"


def _venue_label(venue: str) -> str:
    return VENUE_LABELS.get(venue, venue)


def _humanize_label(label: str) -> str:
    """`"compromise_offer"` -> `"Compromise offer"` -- issue #120's own
    example of a human-readable axis label (sentence case, not Title Case)."""
    return label.replace("_", " ").capitalize()


def _summary_by_venue(snapshot: dict[str, Any], venues: list[str]) -> list[dict[str, Any]]:
    out = []
    for venue in venues:
        trend = snapshot["trend_summary"].get(venue)
        if not trend:
            continue
        headline = trend["headline_cutoff"]
        early = trend["windows"][_EARLY_WINDOW]
        recent = trend["windows"][_RECENT_WINDOW]
        overlap = trend["ci_overlap_by_label"]
        rows = [
            {
                "label": label,
                "early": _fmt_count_ci(
                    early["cutoff_rates_per_1000_messages"][label].get(headline)
                ),
                "recent": _fmt_count_ci(
                    recent["cutoff_rates_per_1000_messages"][label].get(headline)
                ),
                "overlap": _fmt_overlap(overlap.get(label)),
            }
            for label in SORTED_LABELS
        ]
        out.append(
            {
                "venue": venue,
                "label": _venue_label(venue),
                "early_n": early["messages_classified"],
                "early_authors": early["distinct_authors"],
                "recent_n": recent["messages_classified"],
                "recent_authors": recent["distinct_authors"],
                "rows": rows,
            }
        )
    return out


def _newcomer_by_venue(snapshot: dict[str, Any], venues: list[str]) -> list[dict[str, Any]]:
    out = []
    for venue in venues:
        trend = snapshot["newcomer_trend_summary"].get(venue)
        if not trend:
            continue
        early = trend["windows"][_EARLY_WINDOW]
        recent = trend["windows"][_RECENT_WINDOW]
        out.append(
            {
                "venue": venue,
                "label": _venue_label(venue),
                "rows": [
                    {
                        "window": _WINDOW_LABELS[_EARLY_WINDOW],
                        "messages": early["messages_directed_at_newcomers"],
                        "distinct": early["distinct_newcomers"],
                        "constructive": _fmt_newcomer_rate(
                            early, "constructive_response_rate"
                        ),
                        "hostile": _fmt_newcomer_rate(
                            early, "dismissive_hostile_response_rate"
                        ),
                    },
                    {
                        "window": _WINDOW_LABELS[_RECENT_WINDOW],
                        "messages": recent["messages_directed_at_newcomers"],
                        "distinct": recent["distinct_newcomers"],
                        "constructive": _fmt_newcomer_rate(
                            recent, "constructive_response_rate"
                        ),
                        "hostile": _fmt_newcomer_rate(
                            recent, "dismissive_hostile_response_rate"
                        ),
                    },
                ],
            }
        )
    return out


def _disagreement_by_venue(snapshot: dict[str, Any], venues: list[str]) -> list[dict[str, Any]]:
    thread_headline = snapshot["thread_derive_headline_cutoff"]
    out = []
    for venue in venues:
        trend = snapshot["thread_trend_summary"].get(venue)
        if not trend:
            continue
        early = trend["windows"][_EARLY_WINDOW].get(thread_headline, {})
        recent = trend["windows"][_RECENT_WINDOW].get(thread_headline, {})
        out.append(
            {
                "venue": venue,
                "label": _venue_label(venue),
                "rows": [
                    {
                        "window": _WINDOW_LABELS[_EARLY_WINDOW],
                        "escalation": _fmt_rate_pct(early.get("escalation_rate")),
                        "resolution": _fmt_rate_pct(
                            early.get("constructive_resolution_rate")
                        ),
                        "abandonment": _fmt_rate_pct(
                            early.get("thread_abandonment_rate_post_friction")
                        ),
                        "pile_on": _fmt_pile_on(early.get("pile_on_rate")),
                    },
                    {
                        "window": _WINDOW_LABELS[_RECENT_WINDOW],
                        "escalation": _fmt_rate_pct(recent.get("escalation_rate")),
                        "resolution": _fmt_rate_pct(
                            recent.get("constructive_resolution_rate")
                        ),
                        "abandonment": _fmt_rate_pct(
                            recent.get("thread_abandonment_rate_post_friction")
                        ),
                        "pile_on": _fmt_pile_on(recent.get("pile_on_rate")),
                    },
                ],
            }
        )
    return out


def _small_multiples_spec(
    year_cells: dict[str, Any], years: list[str], labels: tuple[str, ...], headline_cutoff: str
) -> str | None:
    """A faceted (small-multiples, one panel per label) Vega-Lite spec:
    year on x, count per 1,000 messages on y, with a shaded 95% CI band
    layered under the point/line -- issue #118 §2 item 4's "line charts
    with CI bands ... as separate small-multiple groups"."""
    values: list[dict[str, Any]] = []
    for label in labels:
        for year in years:
            entry = year_cells[year]["cutoff_rates_per_1000_messages"][label].get(
                headline_cutoff
            )
            if entry is None:
                continue
            ci_lo, ci_hi = entry["ci95"]
            values.append(
                {
                    "label": label,
                    "year": year,
                    "per_1000": entry["per_1000"],
                    "ci_lo": ci_lo,
                    "ci_hi": ci_hi,
                }
            )
    if not values:
        return None

    spec = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "facet": {"field": "label", "type": "nominal", "columns": 2, "title": None},
        "spec": {
            "width": 220,
            "height": 130,
            "layer": [
                {
                    "mark": {"type": "area", "opacity": 0.18, "color": "#2b6cb0"},
                    "encoding": {
                        "x": {"field": "year", "type": "ordinal", "title": "Year"},
                        "y": {
                            "field": "ci_lo",
                            "type": "quantitative",
                            "title": "per 1,000 messages",
                        },
                        "y2": {"field": "ci_hi"},
                    },
                },
                {
                    "mark": {"type": "line", "point": True, "color": "#2b6cb0"},
                    "encoding": {
                        "x": {"field": "year", "type": "ordinal"},
                        "y": {"field": "per_1000", "type": "quantitative"},
                        "tooltip": [
                            {"field": "year", "type": "ordinal", "title": "Year"},
                            {
                                "field": "per_1000",
                                "type": "quantitative",
                                "title": "per 1,000",
                                "format": ".2f",
                            },
                            {
                                "field": "ci_lo",
                                "type": "quantitative",
                                "title": "CI low",
                                "format": ".2f",
                            },
                            {
                                "field": "ci_hi",
                                "type": "quantitative",
                                "title": "CI high",
                                "format": ".2f",
                            },
                        ],
                    },
                },
            ],
        },
        "data": {"values": values},
    }
    return json.dumps(spec)


def _message_patterns_by_venue(
    snapshot: dict[str, Any], venues: list[str]
) -> list[dict[str, Any]]:
    headline_cutoff = snapshot["headline_cutoff"]
    out = []
    for venue in venues:
        year_cells = snapshot["cells_by_year"].get(venue, {})
        years = sorted(year_cells)
        table_rows = [
            {
                "year": year,
                "messages": year_cells[year]["messages_classified"],
                "authors": year_cells[year]["distinct_authors"],
                "values": {
                    label: _fmt_count_ci(
                        year_cells[year]["cutoff_rates_per_1000_messages"][label].get(
                            headline_cutoff
                        )
                    )
                    for label in SORTED_LABELS
                },
            }
            for year in years
        ]
        out.append(
            {
                "venue": venue,
                "label": _venue_label(venue),
                "constructive_labels": CONSTRUCTIVE_LABELS,
                "negative_labels": NEGATIVE_LABELS,
                "constructive_chart_spec": _small_multiples_spec(
                    year_cells, years, CONSTRUCTIVE_LABELS, headline_cutoff
                ),
                "negative_chart_spec": _small_multiples_spec(
                    year_cells, years, NEGATIVE_LABELS, headline_cutoff
                ),
                "table_rows": table_rows,
            }
        )
    return out


def _yoy_rows(
    snapshot: dict[str, Any], venues: list[str]
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Long-format rows for the year-over-year grouped-bar chart (issue
    #120): one row per (venue, cutoff, label, year) cell that clears the
    §5.1 floor, across every venue and every cutoff the snapshot carries --
    filtering down to a single venue/cutoff/year-range is left to the
    client (`app.js`'s `applyYoyFilter`), same "ship the whole small
    dataset, filter in JS" pattern as the chart-window toggle (issue #28).

    Also returns each venue's own list of years omitted for
    `insufficient_data` (a whole cell, never a single label within it --
    `publish.py`'s own §5.1 floor invariant), for the template's note
    under the chart."""
    cutoffs = list(snapshot["cutoffs"])
    rows: list[dict[str, Any]] = []
    insufficient_years_by_venue: dict[str, list[str]] = {}
    for venue in venues:
        year_cells = snapshot["cells_by_year"].get(venue, {})
        insufficient_years: list[str] = []
        for year in sorted(year_cells):
            cell = year_cells[year]
            if cell["insufficient_data"]:
                insufficient_years.append(year)
                continue
            for group_id, _group_title, labels in _YOY_LABEL_GROUPS:
                for label in labels:
                    for cutoff in cutoffs:
                        entry = cell["cutoff_rates_per_1000_messages"][label].get(cutoff)
                        if entry is None:
                            continue
                        ci_lo, ci_hi = entry["ci95"]
                        rows.append(
                            {
                                "venue": venue,
                                "venue_label": _venue_label(venue),
                                "year": year,
                                "cutoff": cutoff,
                                "group": group_id,
                                "label": label,
                                "label_display": _humanize_label(label),
                                "per_1000": entry["per_1000"],
                                "ci_lo": ci_lo,
                                "ci_hi": ci_hi,
                                "messages": cell["messages_classified"],
                                "authors": cell["distinct_authors"],
                            }
                        )
        insufficient_years_by_venue[venue] = insufficient_years
    return rows, insufficient_years_by_venue


def _yoy_default_range(years: list[str], now: datetime) -> tuple[str, str]:
    """Issue #120: "default: last 5 complete years, i.e. 2021-2025" (given
    `now` in 2026) -- a year is "complete" once the following calendar
    year has started, i.e. up to `now.year - 1`, clamped to the years
    actually present in the data."""
    if not years:
        return ("", "")
    numeric_years = sorted(int(y) for y in years)
    default_to = min(now.year - 1, numeric_years[-1])
    default_to = max(default_to, numeric_years[0])
    default_from = max(default_to - (_YOY_DEFAULT_RANGE_YEARS - 1), numeric_years[0])
    return (str(default_from), str(default_to))


def _yoy_year_options(years: list[str], now: datetime) -> list[dict[str, str]]:
    """Every year selectable in the From/To year controls -- "2026
    selectable but labeled partial" (issue #120): any year whose calendar
    year has not yet fully elapsed as of `now` gets a "(partial)" suffix,
    distinct from (and independent of) a year being `insufficient_data`."""
    options = []
    for year in years:
        display = year
        if int(year) >= now.year:
            display = f"{year} (partial)"
        options.append({"value": year, "display": display})
    return options


def _yoy_group_spec(
    rows: list[dict[str, Any]], group_id: str, labels: tuple[str, ...]
) -> str | None:
    """A grouped-bar-chart-with-error-bars Vega-Lite spec for one panel
    (constructive or negative): x = human-readable label, dodged by year,
    color = year on a sequential single-hue ramp (older lighter, newer
    darker -- issue #120), with a `rule` layer for each bar's 95% CI.

    `data.values` carries every venue/cutoff/year row for this group (not
    pre-filtered to a default range/venue/cutoff) -- `app.js`'s
    `applyYoyFilter` narrows it to the controls' current selection on
    every render, first paint included, so this Python-side default and
    the client-side one never have to be kept in sync by hand."""
    group_rows = [row for row in rows if row["group"] == group_id]
    if not group_rows:
        return None

    label_order = [_humanize_label(label) for label in labels]
    tooltip = [
        {"field": "label_display", "type": "nominal", "title": "Label"},
        {"field": "year", "type": "ordinal", "title": "Year"},
        {"field": "per_1000", "type": "quantitative", "title": "per 1,000", "format": ".2f"},
        {"field": "ci_lo", "type": "quantitative", "title": "CI low", "format": ".2f"},
        {"field": "ci_hi", "type": "quantitative", "title": "CI high", "format": ".2f"},
        {"field": "messages", "type": "quantitative", "title": "Messages"},
        {"field": "authors", "type": "quantitative", "title": "Authors"},
    ]
    spec = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "width": "container",
        "height": 280,
        "data": {"values": group_rows},
        "encoding": {
            "x": {
                "field": "label_display",
                "type": "nominal",
                "title": None,
                "sort": label_order,
                "axis": {"labelAngle": -40, "labelLimit": 130, "labelPadding": 4},
            },
            "xOffset": {"field": "year", "type": "ordinal"},
            "y": {"field": "per_1000", "type": "quantitative", "title": "per 1,000 messages"},
            "color": {
                "field": "year",
                "type": "ordinal",
                "title": "Year",
                "scale": {"scheme": "blues"},
            },
            "tooltip": tooltip,
        },
        "layer": [
            {"mark": {"type": "bar"}},
            {
                "mark": {"type": "rule"},
                "encoding": {
                    "y": {"field": "ci_lo", "type": "quantitative"},
                    "y2": {"field": "ci_hi", "type": "quantitative"},
                    "color": {"value": "rgba(0, 0, 0, 0.55)"},
                },
            },
        ],
    }
    return json.dumps(spec)


def _yoy_context(
    snapshot: dict[str, Any], venues: list[str], venue_meta: list[dict[str, str]], now: datetime
) -> dict[str, Any] | None:
    """Issue #120: the year-over-year grouped-bar chart context -- both
    panels' specs, the shared From/To year + venue + (optional) cutoff
    controls, and each venue's insufficient-data-years note.

    `None` (never an error/empty chart) when no venue/cutoff/year/label
    cell clears the floor at all -- same "honest no-data" convention as
    `build_conversation_patterns_context` itself."""
    rows, insufficient_years_by_venue = _yoy_rows(snapshot, venues)
    if not rows:
        return None

    all_years = sorted({row["year"] for row in rows})
    default_from, default_to = _yoy_default_range(all_years, now)
    cutoffs = list(snapshot["cutoffs"])
    headline_cutoff = snapshot["headline_cutoff"]
    default_cutoff = headline_cutoff if headline_cutoff in cutoffs else cutoffs[0]

    groups = []
    for group_id, group_title, labels in _YOY_LABEL_GROUPS:
        spec_json = _yoy_group_spec(rows, group_id, labels)
        if spec_json is None:
            continue
        groups.append({"id": group_id, "title": group_title, "spec_json": spec_json})
    if not groups:
        return None

    insufficient_notes = [
        {"venue_label": _venue_label(venue), "years": years}
        for venue, years in insufficient_years_by_venue.items()
        if years
    ]

    return {
        "groups": groups,
        "venues": venue_meta,
        "default_venue": venues[0],
        "years": _yoy_year_options(all_years, now),
        "default_from": default_from,
        "default_to": default_to,
        "cutoffs": cutoffs,
        "default_cutoff": default_cutoff,
        "insufficient_notes": insufficient_notes,
    }


def _method_context(snapshot: dict[str, Any]) -> dict[str, Any]:
    headline_cutoff = snapshot["headline_cutoff"]
    covered = {_venue_label(v) for v in snapshot["venues"]}
    not_covered = [label for label in _ALL_PHASE2_VENUES if label not in covered]

    benchmark_thresholds = sorted(
        (
            {"label": label, **info}
            for label, info in (snapshot.get("sensitivity_thresholds") or {}).items()
        ),
        key=lambda row: row["label"],
    )

    partial_run = snapshot.get("partial_run")

    return {
        "seed": snapshot["seed"],
        "k": snapshot["k"],
        "quarters_start": snapshot["quarters"][0],
        "quarters_end": snapshot["quarters"][-1],
        "quarters_count": len(snapshot["quarters"]),
        "frame_definition": [
            {"venue": _venue_label(venue), "text": text}
            for venue, text in snapshot["frame_definition"].items()
        ],
        "headline_cutoff": headline_cutoff,
        "sensitivity_cutoffs": [c for c in snapshot["cutoffs"] if c != headline_cutoff],
        "thread_derive_headline_cutoff": snapshot["thread_derive_headline_cutoff"],
        "thread_derive_sensitivity_cutoffs": [
            c
            for c in snapshot["thread_derive_cutoffs"]
            if c != snapshot["thread_derive_headline_cutoff"]
        ],
        "newcomer_n": snapshot["newcomer_n"],
        "floors": snapshot["floors"],
        "probability_index_floor": sorted(
            (
                {"label": label, "value": value}
                for label, value in (
                    snapshot.get("probability_index_floor_per_1000_messages") or {}
                ).items()
            ),
            key=lambda row: row["label"],
        ),
        "benchmark_thresholds": benchmark_thresholds,
        "scope_note": snapshot["scope_note"],
        "venues_not_covered": not_covered,
        "truncated_thread_counts": snapshot.get("truncated_thread_counts") or {},
        "partial_run": partial_run,
        "cost_total_usd": snapshot.get("cost_total_usd") or {},
    }


def _community_card_metrics(snapshot: dict[str, Any], venues: list[str]) -> list[dict[str, str]]:
    return [
        {"name": "Venues covered", "value": ", ".join(_venue_label(v) for v in venues)},
        {"name": "Classifier", "value": str(snapshot.get("classifier_version"))},
        {
            "name": "Quarters covered",
            "value": f"{snapshot['quarters'][0]}–{snapshot['quarters'][-1]}",
        },
    ]


def build_conversation_patterns_context(
    data_dir: str | Path, *, now: datetime | None = None
) -> dict[str, Any]:
    """`now` is the build-time reference for the year-over-year chart's
    default range and "(partial)" year labeling (issue #120); it defaults
    to the current UTC time -- tests pass an explicit value for
    determinism, same convention as `site.generate.generate`'s own `now`."""
    snapshot = _read_latest_snapshot(data_dir)
    if snapshot is None:
        return {"available": False}
    build_time = now if now is not None else datetime.now(UTC)

    venues = list(snapshot["venues"])
    venue_meta = [{"id": v, "label": _venue_label(v)} for v in venues]

    return {
        "available": True,
        "generated_at": snapshot["generated_at"],
        "classifier_version": snapshot.get("classifier_version"),
        "question_set_version": snapshot.get("question_set_version"),
        "model_id_pinned": snapshot.get("model_id_pinned"),
        "venues": venue_meta,
        "summary_by_venue": _summary_by_venue(snapshot, venues),
        "yoy": _yoy_context(snapshot, venues, venue_meta, build_time),
        "newcomer_by_venue": _newcomer_by_venue(snapshot, venues),
        "disagreement_by_venue": _disagreement_by_venue(snapshot, venues),
        "message_patterns_by_venue": _message_patterns_by_venue(snapshot, venues),
        "method": _method_context(snapshot),
        "community_card_metrics": _community_card_metrics(snapshot, venues),
        "feedback_url": conversation_patterns_feedback_url(),
        "benchmark_public_url": BENCHMARK_PUBLIC_URL,
        "community_health_url": COMMUNITY_HEALTH_URL,
        "decisions_url": DECISIONS_URL,
        "decisions_d26_anchor": DECISIONS_D26_ANCHOR,
    }
