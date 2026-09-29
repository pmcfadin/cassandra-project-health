"""Community page "Review responsiveness" section context builder (issue #102).

Turns one run's `snapshots/<run_id>/review_responsiveness_metric_value.parquet`
(`metrics/review_responsiveness.py`) into everything `templates/community.html`'s
review-responsiveness section needs: the yearly table per tier, a
trailing-12-month line chart of the 30-day response share by tier, first-patch
volume, no-visible-response share, and a source-of-first-response breakdown.

Kept out of `generate.py` (mirrors `leaderboard_page.py`/`governance_page.py`'s
own reasoning): this module and `metrics/review_responsiveness.py` are the
only things that read/write this section's data, so `generate.py`'s wiring
stays a small, additive call plus one extra template variable.

D25 (neutral, informational site): every string here avoids verdict/pass-fail/
threshold/compliance wording -- rates, medians and counts only, framed as
"what the record shows," matching this project's Community/Governance pages.
The methodology note spells out the definitions (submission unit, author-tier
reconstruction, blended first-response, right-censoring) so a reader can
audit the numbers themselves (D2 rule 3).

An honest no-data / not-yet-run state (no snapshot for this run, or the
computation produced zero rows -- e.g. before the Patch-Available backfill
has reached any issues) still returns a valid context with `available=False`,
matching every other optional section on this site.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from project_health import storage
from project_health.metrics.review_responsiveness import (
    TIER_2_5,
    TIER_6PLUS,
    TIER_FIRST,
    WINDOW_TRAILING12M,
    WINDOW_YEARLY,
    metric_id,
)
from project_health.schema import get_schema, validate

TIER_LABELS: dict[str, str] = {
    TIER_FIRST: "First-time submitters",
    TIER_2_5: "2nd-5th submission",
    TIER_6PLUS: "6th+ submission",
}
TIER_ORDER: tuple[str, ...] = (TIER_FIRST, TIER_2_5, TIER_6PLUS)

SOURCE_LABELS: dict[str, str] = {
    "jira_comment": "JIRA comment",
    "jira_status": "JIRA status change",
    "gh_review": "GitHub review",
    "gh_comment": "GitHub PR comment",
}

METRICS_SPEC_URL = (
    "https://github.com/pmcfadin/cassandra-project-health/blob/main/docs/spec/METRICS.md"
    "#10-review-responsiveness-descriptive-only-not-in-the-summary-table"
)


def _read_optional_snapshot_table(data_dir: Path, run_id: str) -> pa.Table:
    path = Path(data_dir) / "snapshots" / run_id / "review_responsiveness_metric_value.parquet"
    if not path.is_file():
        return get_schema("metric_value").empty_table()
    return validate("metric_value", pq.read_table(path))


def _details(row: dict) -> dict:
    return json.loads(row["details_json"]) if row.get("details_json") else {}


def _fmt_pct(value: float | None) -> str | None:
    return f"{round(value * 100)}%" if value is not None else None


def _fmt_days(value: float | None) -> str | None:
    return f"{value:.1f}" if value is not None else None


@dataclass(frozen=True)
class _YearRow:
    year: int
    n_submitted: int
    median_days: str | None
    within_7d_share: str | None
    within_30d_share: str | None
    no_visible_response_share: str | None
    committed_within_365d_share: str | None


def _year_rows_for_tier(rows_by_metric: dict[str, dict[date, dict]], tier: str) -> list[_YearRow]:
    median_by_year = rows_by_metric.get(
        metric_id("review_first_response_median_days", WINDOW_YEARLY, tier), {}
    )
    within_7d_by_year = rows_by_metric.get(
        metric_id("review_response_within_7d_share", WINDOW_YEARLY, tier), {}
    )
    within_30d_by_year = rows_by_metric.get(
        metric_id("review_response_within_30d_share", WINDOW_YEARLY, tier), {}
    )
    no_visible_by_year = rows_by_metric.get(
        metric_id("review_no_visible_response_share", WINDOW_YEARLY, tier), {}
    )
    committed_by_year = rows_by_metric.get(
        metric_id("patch_committed_within_365d_share", WINDOW_YEARLY, tier), {}
    )

    years = sorted(median_by_year)
    out = []
    for window_start in years:
        median_row = median_by_year[window_start]
        out.append(
            _YearRow(
                year=window_start.year,
                n_submitted=_details(median_row).get("n_submitted", 0),
                median_days=_fmt_days(median_row["value"]),
                within_7d_share=_fmt_pct(
                    (within_7d_by_year.get(window_start) or {}).get("value")
                ),
                within_30d_share=_fmt_pct(
                    (within_30d_by_year.get(window_start) or {}).get("value")
                ),
                no_visible_response_share=_fmt_pct(
                    (no_visible_by_year.get(window_start) or {}).get("value")
                ),
                committed_within_365d_share=_fmt_pct(
                    (committed_by_year.get(window_start) or {}).get("value")
                ),
            )
        )
    return out


def _trailing12m_chart_spec(rows_by_metric: dict[str, dict[date, dict]]) -> str | None:
    """A multi-series Vega-Lite line chart of the trailing-12-month
    ≤30-day response share, one line per tier (color-encoded, with a
    legend) -- the one chart the issue explicitly asks for. No existing
    chart in this project encodes more than one series, so this is a small,
    self-contained spec rather than a reuse of `chart_spec.py`'s
    single-series monthly helpers (which are tuned for 17 years of monthly
    M0 data; this series is far shorter)."""
    values: list[dict[str, Any]] = []
    for tier in TIER_ORDER:
        series = rows_by_metric.get(
            metric_id("review_response_within_30d_share", WINDOW_TRAILING12M, tier), {}
        )
        for window_end, row in sorted(series.items()):
            if row["value"] is None:
                continue
            values.append(
                {
                    "window_end": window_end.isoformat(),
                    "tier": TIER_LABELS[tier],
                    "value": row["value"],
                    "n": row["n"],
                }
            )
    if not values:
        return None

    spec = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "width": "container",
        "height": 240,
        "data": {"values": values},
        "mark": {"type": "line", "point": True},
        "encoding": {
            "x": {"field": "window_end", "type": "temporal", "title": "Trailing 12 months ending"},
            "y": {
                "field": "value",
                "type": "quantitative",
                "title": "Response within 30 days",
                "axis": {"format": "%"},
                "scale": {"domain": [0, 1]},
            },
            "color": {"field": "tier", "type": "nominal", "title": "Submitter tier"},
            "tooltip": [
                {"field": "window_end", "type": "temporal", "title": "Window ending"},
                {"field": "tier", "type": "nominal", "title": "Tier"},
                {"field": "value", "type": "quantitative", "title": "Share", "format": ".0%"},
                {"field": "n", "type": "quantitative", "title": "n"},
            ],
        },
    }
    return json.dumps(spec)


def _source_breakdown_for_latest_year(rows_by_metric: dict[str, dict[date, dict]]) -> list[dict]:
    """Aggregate `details_json.source_breakdown` across all three tiers for
    the most recent completed year -- "what actually answers a patch
    first" (JIRA comment, JIRA status change, GitHub review, GitHub PR
    comment), a plain, neutral fact (D25)."""
    latest_year: date | None = None
    for tier in TIER_ORDER:
        series = rows_by_metric.get(
            metric_id("review_first_response_median_days", WINDOW_YEARLY, tier), {}
        )
        if series:
            candidate = max(series)
            if latest_year is None or candidate > latest_year:
                latest_year = candidate
    if latest_year is None:
        return []

    totals: dict[str, int] = {}
    for tier in TIER_ORDER:
        series = rows_by_metric.get(
            metric_id("review_first_response_median_days", WINDOW_YEARLY, tier), {}
        )
        row = series.get(latest_year)
        if not row:
            continue
        for source, count in _details(row).get("source_breakdown", {}).items():
            totals[source] = totals.get(source, 0) + count

    total_count = sum(totals.values())
    if total_count == 0:
        return []
    return [
        {
            "source": SOURCE_LABELS.get(source, source),
            "count": count,
            "share": _fmt_pct(count / total_count),
        }
        for source, count in sorted(totals.items(), key=lambda item: -item[1])
    ]


def _coverage_note(data_dir: Path) -> str | None:
    """"History still loading (x of y issues)" note (issue #102's own
    wording) while the Patch-Available changelog backfill
    (`pipeline._collect_jira_patch_available_backfill`) hasn't yet reached
    every issue -- an operational completeness note, not a verdict (same
    framing `governance_page.py`'s own backfill banner uses), so tiers stay
    trustworthy-labeled rather than silently wrong until coverage is
    complete."""
    total_raw = storage.read_watermark(data_dir, "jira", table="patch_available_backfill_total")
    covered_raw = storage.read_watermark(data_dir, "jira", table="patch_available_backfill_covered")
    if not total_raw:
        return (
            "History still loading: the Patch-Available changelog backfill hasn't started yet, "
            "so author tiers and shares below are not yet trustworthy."
        )
    total = int(total_raw)
    covered = int(covered_raw) if covered_raw else 0
    if covered >= total:
        return None
    return (
        f"History still loading ({covered:,} of {total:,} issues) -- author tiers and the "
        "figures below will keep shifting until every Patch-Available issue's history has "
        "been walked."
    )


def build_review_responsiveness_context(data_dir: str | Path, run_id: str) -> dict[str, Any]:
    data_dir = Path(data_dir)
    table = _read_optional_snapshot_table(data_dir, run_id)
    rows = table.to_pylist()

    if not rows:
        return {
            "available": False,
            "coverage_note": _coverage_note(data_dir),
            "metrics_spec_url": METRICS_SPEC_URL,
        }

    rows_by_metric: dict[str, dict[date, dict]] = {}
    for row in rows:
        rows_by_metric.setdefault(row["metric_id"], {})[row["window_start"]] = row

    tiers = [
        {
            "tier": tier,
            "label": TIER_LABELS[tier],
            "years": _year_rows_for_tier(rows_by_metric, tier),
        }
        for tier in TIER_ORDER
    ]

    return {
        "available": any(t["years"] for t in tiers),
        "coverage_note": _coverage_note(data_dir),
        "tiers": tiers,
        "trailing12m_chart_spec": _trailing12m_chart_spec(rows_by_metric),
        "source_breakdown": _source_breakdown_for_latest_year(rows_by_metric),
        "metrics_spec_url": METRICS_SPEC_URL,
    }
