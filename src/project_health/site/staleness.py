"""Per-card staleness badges (issue #86, ARCHITECTURE.md §7.3).

The run manifest's header-level `source_badges` (`generate._common_page_context`)
already show every source's status once, site-wide. ARCHITECTURE.md §7.3
additionally specifies a *per-card* badge: "the site renders an explicit
staleness badge on every card/metric that depends on that source" — a
reader looking at one specific card should see, right there, which of
*that card's own* sources is stale and since when, rather than having to
cross-reference the header pill against `metrics_meta.MetricMeta.sources`
themselves.

Kept in its own module (rather than folded into `generate.py`, which is
where the per-metric-card and home-summary-card contexts actually call
this) because `site/leaderboard_page.py` needs the same badge logic for its
own, metric-id-less section (D19's contributor leaderboard) and importing
it from `generate.py` would be circular — `generate.py` is the one that
imports `leaderboard_page.build_leaderboard_page_context`, not the other
way around.
"""

from __future__ import annotations

import re
from typing import Any

from project_health.site.manifest import RunManifest
from project_health.site.metrics_meta import SOURCE_LABELS

_RUN_ID_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T\d{6}Z")


def run_id_date(run_id_like: str | None) -> str | None:
    """Best-effort `YYYY-MM-DD` parsed from the leading portion of a
    `pipeline.make_run_id`-shaped string (`%Y-%m-%dT%H%M%SZ-<sha7>`) -- used
    for both `last_good_snapshot` (itself a prior run's `run_id`) and this
    run's own `run_id`. Falls back to the raw string unparsed, rather than
    raising, if it's ever not in that shape -- a manifest field this site
    doesn't own the format of should degrade to "less pretty," never break
    the whole build."""
    if not run_id_like:
        return None
    match = _RUN_ID_DATE_RE.match(run_id_like)
    return match.group(1) if match else run_id_like


def source_staleness_badge(source: str, manifest: RunManifest) -> dict[str, Any] | None:
    """One staleness badge for `source`, or `None` if that source isn't in
    the manifest, or its status is `'ok'` or `'partial'`.

    `'partial'` is deliberately excluded (issue #86 acceptance criterion):
    that's budgeted backfill-in-progress (`generate._latest_point_is_backfill_pending`),
    which already has its own "backfill pending" badge -- this one is
    specifically for a source whose collection *failed* and fell back to
    reusing a stale snapshot (ARCHITECTURE.md §7.3).
    """
    status = manifest.sources.get(source)
    if status is None or status.status not in ("failed", "stale"):
        return None
    label = SOURCE_LABELS.get(source, source)
    last_refreshed = run_id_date(status.last_good_snapshot)
    failed_on = run_id_date(manifest.run_id) or (
        manifest.completed_at.date().isoformat() if manifest.completed_at else None
    )
    if last_refreshed and failed_on:
        text = f"{label} data last refreshed {last_refreshed}; collection failed on {failed_on}"
    elif failed_on:
        text = f"{label} collection failed on {failed_on}; no prior good snapshot available"
    else:
        text = f"{label} collection failed; no refresh date available"
    return {
        "source": source,
        "label": label,
        "status": status.status,
        "last_good_snapshot": status.last_good_snapshot,
        "text": text,
    }


def source_staleness_badges(
    sources: tuple[str, ...], manifest: RunManifest
) -> list[dict[str, Any]]:
    """A badge for every source in `sources` whose manifest status is
    `'failed'`/`'stale'`, in `sources`' own order and de-duplicated (a
    card/section naming the same source twice never renders two identical
    badges)."""
    badges = []
    seen: set[str] = set()
    for source in sources:
        if source in seen:
            continue
        seen.add(source)
        badge = source_staleness_badge(source, manifest)
        if badge is not None:
            badges.append(badge)
    return badges
