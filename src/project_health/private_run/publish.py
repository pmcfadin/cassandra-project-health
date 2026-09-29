"""Public publish path for the private Cassandra communication run's
aggregate output (issue #118, DECISIONS.md D26).

`project-health publish-conversation-aggregates` is the one bridge between
the owner-only private run (`private_run/runner.py`, issue #110/#114 --
whose own `aggregates.json`/`report.md` are never committed anywhere public,
per that module's own docstrings) and the public `data` branch: it reads
that private `aggregates.json`, sanitizes it through an explicit
**allowlist** of keys, and writes the sanitized result to
`snapshots/conversation_patterns/<run date>.json` on a `data`-branch
checkout.

D26 (owner, 2026-09-29): this ships the site's first *classified* content
ahead of the full §7.8 PMC acknowledgment and the #47 production-threshold
calibration, explicitly marked **preliminary** everywhere it renders
(`site/conversation_patterns_page.py`). Every other §7 rule stays binding
-- this module is where they are mechanically enforced for this one publish
path:

- **Allowlist, not passthrough.** Only the keys named below (and each one's
  own known sub-shape, reconstructed field by field) are copied out of the
  private aggregates dict; nothing else gets a free pass just because it
  looks harmless. The cost ledger's own operational detail (calls made,
  cache hits, token counts, per-call latency, run status) is deliberately
  left off the list -- issue #118's "drop cost ledger internals except
  total USD" -- keeping only the classifier provenance fields
  (`classifier_version`/`question_set_version`/`model_id_pinned`) and two
  total-USD figures (latest run, cumulative).
- **Hard-fail, not best-effort.** `sanitize_aggregates` scans its own
  sanitized output -- everything about to be published, field by field --
  for anything that looks like a mailing-list Message-ID, an email
  address, a JIRA comment reference, or a salted `author_key`-shaped hex
  hash (`identity.hash_author` always emits a 32-hex-char digest, and a raw
  sha256 input hash is 64) and raises `SanitizeError` rather than silently
  publishing it, instead of writing anything. It also asserts the
  §5.1/§5.2 floor invariant every `insufficient_data` cell already carries
  in the private aggregates (`aggregate.aggregate_cell`,
  `thread_aggregate.py`): whenever a cell/entry is below its floor, every
  one of its rate/count-derived fields must be `None` -- never a leaked
  small-population number that slipped past `insufficient_data: True`.

Nothing here has access to message text, thread ids, message ids, or any
per-person identifier in the first place (mirrors `report.py`'s own
docstring) -- the private aggregates dict this module reads is itself
already aggregate-only by construction. The hard-fail scan and the floor
check are defense in depth, not the primary guarantee.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

# Keys `sanitize_aggregates` requires on the input dict -- if any is
# missing, the private aggregates.json is either from an incompatible
# runner.py version or truncated, and this refuses to publish a partial
# snapshot rather than guessing.
_REQUIRED_TOP_KEYS = (
    "generated_at",
    "seed",
    "k",
    "quarters",
    "venues",
    "frame_definition",
    "scope_note",
    "cutoffs",
    "headline_cutoff",
    "floors",
    "cost_summary",
    "venue_totals",
    "trend_summary",
    "cells_by_quarter",
    "cells_by_year",
    "thread_derive_cutoffs",
    "thread_derive_headline_cutoff",
    "newcomer_n",
    "thread_metrics_by_year",
    "newcomer_by_year",
    "thread_trend_summary",
    "newcomer_trend_summary",
)

# --- Hard-fail leak patterns (defense in depth; COMMUNITY-HEALTH.md §7.3) -

_MESSAGE_ID_RE = re.compile(r"<[^<>@\s]+@[^<>\s]+>")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}")
# `identity.hash_author` always emits a 32-hex-char `author_key`; a raw
# sha256 input hash (classifier cache key) is 64. Either way, any long hex
# run in a published string is suspect -- nothing legitimately published
# here is a hex id (label ids, venue ids, dataset names and frame-
# definition prose are all plain words).
_HEX_ID_RE = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_JIRA_COMMENT_RE = re.compile(r"\bCASSANDRA-\d+\b[^\n]{0,20}\bcomment\b", re.IGNORECASE)


class SanitizeError(RuntimeError):
    """Raised when the private aggregates dict is missing a required key,
    contains a value that looks like it could identify a message or a
    person, or publishes a number below its §5.1/§5.2 floor."""


def _leak_reason(value: str) -> str | None:
    if _MESSAGE_ID_RE.search(value):
        return "looks like a mailing-list Message-ID"
    if _EMAIL_RE.search(value):
        return "looks like an email address"
    if _HEX_ID_RE.search(value):
        return "looks like a hash / author_key / input hash (long hex string)"
    if _JIRA_COMMENT_RE.search(value):
        return "looks like a JIRA comment reference"
    return None


def _hard_fail_scan(node: Any, path: str) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            _hard_fail_scan(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _hard_fail_scan(value, f"{path}[{index}]")
    elif isinstance(node, str):
        reason = _leak_reason(node)
        if reason:
            raise SanitizeError(f"{path}: refusing to publish {node!r} -- {reason}")


# --- Leaf-shape copiers (each field named explicitly -- never **kwargs / --
# passthrough of a whole sub-dict, so a future field added to runner.py's
# private aggregates never rides along silently) -------------------------


def _copy_rate_ci_entry(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if entry is None:
        return None
    return {"per_1000": entry["per_1000"], "ci95": list(entry["ci95"])}


def _copy_sensitivity_value_entry(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if entry is None:
        return None
    return {
        "per_1000": entry["per_1000"],
        "threshold": entry["threshold"],
        "dataset_name": entry["dataset_name"],
        "permissive": entry["permissive"],
    }


def _copy_cell(cell: dict[str, Any]) -> dict[str, Any]:
    return {
        "messages_classified": cell["messages_classified"],
        "distinct_authors": cell["distinct_authors"],
        "threads_sampled": cell["threads_sampled"],
        "threads_population": cell["threads_population"],
        "insufficient_data": cell["insufficient_data"],
        "cutoff_rates_per_1000_messages": {
            label: {
                cutoff_key: _copy_rate_ci_entry(entry) for cutoff_key, entry in by_cutoff.items()
            }
            for label, by_cutoff in cell["cutoff_rates_per_1000_messages"].items()
        },
        "probability_index_per_1000_messages": {
            label: _copy_rate_ci_entry(entry)
            for label, entry in cell["probability_index_per_1000_messages"].items()
        },
        "sensitivity_per_1000_messages": {
            label: _copy_sensitivity_value_entry(entry)
            for label, entry in cell["sensitivity_per_1000_messages"].items()
        },
    }


def _copy_cells_by_period(cells_by_period: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        venue: {period: _copy_cell(cell) for period, cell in periods.items()}
        for venue, periods in cells_by_period.items()
    }


def _copy_thread_rate_entry(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if entry is None:
        return None
    return {
        "qualifying_threads": entry["qualifying_threads"],
        "distinct_participants": entry["distinct_participants"],
        "insufficient_data": entry["insufficient_data"],
        "rate": entry["rate"],
        "ci95": list(entry["ci95"]) if entry.get("ci95") is not None else None,
    }


def _copy_pile_on_entry(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if entry is None:
        return None
    return {
        "threads_total": entry["threads_total"],
        "distinct_targets": entry["distinct_targets"],
        "insufficient_data": entry["insufficient_data"],
        "per_100_threads": entry["per_100_threads"],
        "ci95": list(entry["ci95"]) if entry.get("ci95") is not None else None,
    }


def _copy_thread_metrics_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "threads_total": entry["threads_total"],
        "escalation_rate": _copy_thread_rate_entry(entry["escalation_rate"]),
        "constructive_resolution_rate": _copy_thread_rate_entry(
            entry["constructive_resolution_rate"]
        ),
        "thread_abandonment_rate_post_friction": _copy_thread_rate_entry(
            entry["thread_abandonment_rate_post_friction"]
        ),
        "pile_on_rate": _copy_pile_on_entry(entry["pile_on_rate"]),
    }


def _copy_thread_metrics_by_period(thread_metrics_by_period: dict[str, Any]) -> dict[str, Any]:
    return {
        venue: {
            period: {
                cutoff_key: _copy_thread_metrics_entry(entry)
                for cutoff_key, entry in by_cutoff.items()
            }
            for period, by_cutoff in periods.items()
        }
        for venue, periods in thread_metrics_by_period.items()
    }


def _copy_newcomer_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "messages_directed_at_newcomers": entry["messages_directed_at_newcomers"],
        "distinct_newcomers": entry["distinct_newcomers"],
        "insufficient_data": entry["insufficient_data"],
        "constructive_response_rate": entry["constructive_response_rate"],
        "constructive_response_rate_ci95": (
            list(entry["constructive_response_rate_ci95"])
            if entry.get("constructive_response_rate_ci95") is not None
            else None
        ),
        "dismissive_hostile_response_rate": entry["dismissive_hostile_response_rate"],
        "dismissive_hostile_response_rate_ci95": (
            list(entry["dismissive_hostile_response_rate_ci95"])
            if entry.get("dismissive_hostile_response_rate_ci95") is not None
            else None
        ),
    }


def _copy_newcomer_by_period(newcomer_by_period: dict[str, Any]) -> dict[str, Any]:
    return {
        venue: {period: _copy_newcomer_entry(entry) for period, entry in periods.items()}
        for venue, periods in newcomer_by_period.items()
    }


def _copy_trend_summary(trend_summary: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for venue, window_info in trend_summary.items():
        out[venue] = {
            "windows": {
                name: _copy_cell(cell) for name, cell in window_info["windows"].items()
            },
            "headline_cutoff": window_info["headline_cutoff"],
            "ci_overlap_by_label": dict(window_info["ci_overlap_by_label"]),
        }
    return out


def _copy_thread_trend_summary(thread_trend_summary: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for venue, window_info in thread_trend_summary.items():
        out[venue] = {
            "windows": {
                name: {
                    cutoff_key: _copy_thread_metrics_entry(entry)
                    for cutoff_key, entry in by_cutoff.items()
                }
                for name, by_cutoff in window_info["windows"].items()
            }
        }
    return out


def _copy_newcomer_trend_summary(newcomer_trend_summary: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for venue, window_info in newcomer_trend_summary.items():
        out[venue] = {
            "windows": {
                name: _copy_newcomer_entry(entry)
                for name, entry in window_info["windows"].items()
            }
        }
    return out


def _copy_partial_run(partial_run: dict[str, Any] | None) -> dict[str, Any] | None:
    if not partial_run:
        return None
    return {
        "status": partial_run["status"],
        "messages_sampled": partial_run["messages_sampled"],
        "messages_classified": partial_run["messages_classified"],
        "messages_truncated": partial_run.get("messages_truncated", 0),
        "messages_skipped": partial_run.get("messages_skipped", 0),
        "skip_reasons": dict(partial_run.get("skip_reasons") or {}),
        "coverage_by_stratum": [
            {
                "venue": row["venue"],
                "quarter": row["quarter"],
                "messages_sampled": row["messages_sampled"],
                "messages_classified": row["messages_classified"],
                "coverage": row["coverage"],
            }
            for row in partial_run.get("coverage_by_stratum", [])
        ],
    }


def _copy_venue_totals(venue_totals: dict[str, Any]) -> dict[str, Any]:
    return {
        venue: {
            "messages_classified": totals["messages_classified"],
            "threads_sampled": totals["threads_sampled"],
            "threads_population": totals["threads_population"],
        }
        for venue, totals in venue_totals.items()
    }


def _copy_sensitivity_thresholds(sensitivity_thresholds: dict[str, Any] | None) -> dict[str, Any]:
    return {
        label: {
            "threshold": info["threshold"],
            "f1": info["f1"],
            "dataset_name": info["dataset_name"],
            "permissive": info["permissive"],
        }
        for label, info in (sensitivity_thresholds or {}).items()
    }


def _cost_total_usd(aggregates: dict[str, Any]) -> dict[str, float | None]:
    """Issue #118: "drop cost ledger internals except total USD" -- the
    only cost figures published are the latest run's own total and the
    lifetime cumulative total from the cost ledger (`runner.py`'s
    `cost_ledger.cumulative_from_ledger_runs`); calls made, cache hits,
    token counts, per-call latency and run status never leave this
    function."""
    cost_summary = aggregates.get("cost_summary") or {}
    cost_ledger = aggregates.get("cost_ledger") or {}
    ledger_runs = cost_ledger.get("cumulative_from_ledger_runs") or {}
    return {
        "latest_run_usd": cost_summary.get("estimated_cost_usd"),
        "cumulative_usd": ledger_runs.get("estimated_cost_usd"),
    }


# --- §5.1/§5.2 floor invariant (defense in depth) -------------------------


def _assert_cell_floor(cell: dict[str, Any], path: str) -> None:
    if not cell["insufficient_data"]:
        return
    for label, by_cutoff in cell["cutoff_rates_per_1000_messages"].items():
        for cutoff_key, entry in by_cutoff.items():
            if entry is not None:
                raise SanitizeError(
                    f"{path}: {label}@{cutoff_key} has a value despite insufficient_data "
                    "(§5.1 floor violation)"
                )
    for label, entry in cell["probability_index_per_1000_messages"].items():
        if entry is not None:
            raise SanitizeError(
                f"{path}: probability index for {label} has a value despite "
                "insufficient_data (§5.1 floor violation)"
            )
    for label, entry in cell["sensitivity_per_1000_messages"].items():
        if entry is not None:
            raise SanitizeError(
                f"{path}: sensitivity value for {label} has a value despite "
                "insufficient_data (§5.1 floor violation)"
            )


def _assert_thread_rate_floor(entry: dict[str, Any] | None, path: str) -> None:
    if entry is None:
        return
    if entry["insufficient_data"] and (
        entry.get("rate") is not None or entry.get("ci95") is not None
    ):
        raise SanitizeError(f"{path}: rate/ci95 set despite insufficient_data (§5.2 floor)")


def _assert_pile_on_floor(entry: dict[str, Any] | None, path: str) -> None:
    if entry is None:
        return
    if entry["insufficient_data"] and (
        entry.get("per_100_threads") is not None or entry.get("ci95") is not None
    ):
        raise SanitizeError(
            f"{path}: per_100_threads/ci95 set despite insufficient_data (§5.2 floor)"
        )


def _assert_newcomer_floor(entry: dict[str, Any], path: str) -> None:
    if entry["insufficient_data"] and any(
        entry.get(key) is not None
        for key in (
            "constructive_response_rate",
            "constructive_response_rate_ci95",
            "dismissive_hostile_response_rate",
            "dismissive_hostile_response_rate_ci95",
        )
    ):
        raise SanitizeError(f"{path}: a rate is set despite insufficient_data (§5.2 floor)")


def _assert_thread_metrics_entry_floors(entry: dict[str, Any], path: str) -> None:
    _assert_thread_rate_floor(entry["escalation_rate"], f"{path}.escalation_rate")
    _assert_thread_rate_floor(
        entry["constructive_resolution_rate"], f"{path}.constructive_resolution_rate"
    )
    _assert_thread_rate_floor(
        entry["thread_abandonment_rate_post_friction"],
        f"{path}.thread_abandonment_rate_post_friction",
    )
    _assert_pile_on_floor(entry["pile_on_rate"], f"{path}.pile_on_rate")


def _assert_floor_invariants(sanitized: dict[str, Any]) -> None:
    for venue, periods in sanitized["cells_by_year"].items():
        for period, cell in periods.items():
            _assert_cell_floor(cell, f"cells_by_year.{venue}.{period}")
    for venue, periods in sanitized["cells_by_quarter"].items():
        for period, cell in periods.items():
            _assert_cell_floor(cell, f"cells_by_quarter.{venue}.{period}")
    for venue, window_info in sanitized["trend_summary"].items():
        for window_name, cell in window_info["windows"].items():
            _assert_cell_floor(cell, f"trend_summary.{venue}.{window_name}")

    for venue, periods in sanitized["thread_metrics_by_year"].items():
        for period, by_cutoff in periods.items():
            for cutoff_key, entry in by_cutoff.items():
                _assert_thread_metrics_entry_floors(
                    entry, f"thread_metrics_by_year.{venue}.{period}.{cutoff_key}"
                )
    for venue, window_info in sanitized["thread_trend_summary"].items():
        for window_name, by_cutoff in window_info["windows"].items():
            for cutoff_key, entry in by_cutoff.items():
                _assert_thread_metrics_entry_floors(
                    entry, f"thread_trend_summary.{venue}.{window_name}.{cutoff_key}"
                )

    for venue, periods in sanitized["newcomer_by_year"].items():
        for period, entry in periods.items():
            _assert_newcomer_floor(entry, f"newcomer_by_year.{venue}.{period}")
    for venue, window_info in sanitized["newcomer_trend_summary"].items():
        for window_name, entry in window_info["windows"].items():
            _assert_newcomer_floor(entry, f"newcomer_trend_summary.{venue}.{window_name}")


def sanitize_aggregates(aggregates: dict[str, Any]) -> dict[str, Any]:
    """Sanitize one private run's `aggregates.json` dict through the D26
    allowlist, returning a new dict safe to publish to the public `data`
    branch. Raises `SanitizeError` (never publishes a partial/best-effort
    result) if a required key is missing, a value looks like it could
    identify a message or a person, or a number is published despite its
    own `insufficient_data` flag."""
    missing = [key for key in _REQUIRED_TOP_KEYS if key not in aggregates]
    if missing:
        raise SanitizeError(
            f"private aggregates.json is missing required key(s): {missing}"
        )

    cost_summary = aggregates["cost_summary"]
    sanitized: dict[str, Any] = {
        "generated_at": aggregates["generated_at"],
        "seed": aggregates["seed"],
        "k": aggregates["k"],
        "quarters": list(aggregates["quarters"]),
        "venues": list(aggregates["venues"]),
        "frame_definition": dict(aggregates["frame_definition"]),
        "scope_note": aggregates["scope_note"],
        "floors": dict(aggregates["floors"]),
        "cutoffs": list(aggregates["cutoffs"]),
        "headline_cutoff": aggregates["headline_cutoff"],
        "thread_derive_cutoffs": list(aggregates["thread_derive_cutoffs"]),
        "thread_derive_headline_cutoff": aggregates["thread_derive_headline_cutoff"],
        "newcomer_n": aggregates["newcomer_n"],
        "sensitivity_thresholds": _copy_sensitivity_thresholds(
            aggregates.get("sensitivity_thresholds")
        ),
        "probability_index_floor_per_1000_messages": dict(
            aggregates.get("probability_index_floor_per_1000_messages") or {}
        ),
        "classifier_version": cost_summary.get("classifier_version"),
        "question_set_version": cost_summary.get("question_set_version"),
        "model_id_pinned": cost_summary.get("model_id_pinned"),
        "cost_total_usd": _cost_total_usd(aggregates),
        "venue_totals": _copy_venue_totals(aggregates["venue_totals"]),
        "truncated_thread_counts": {
            venue: dict(by_quarter)
            for venue, by_quarter in (aggregates.get("truncated_thread_counts") or {}).items()
        },
        "partial_run": _copy_partial_run(aggregates.get("partial_run")),
        "trend_summary": _copy_trend_summary(aggregates["trend_summary"]),
        "cells_by_year": _copy_cells_by_period(aggregates["cells_by_year"]),
        "cells_by_quarter": _copy_cells_by_period(aggregates["cells_by_quarter"]),
        "thread_metrics_by_year": _copy_thread_metrics_by_period(
            aggregates["thread_metrics_by_year"]
        ),
        "newcomer_by_year": _copy_newcomer_by_period(aggregates["newcomer_by_year"]),
        "thread_trend_summary": _copy_thread_trend_summary(aggregates["thread_trend_summary"]),
        "newcomer_trend_summary": _copy_newcomer_trend_summary(
            aggregates["newcomer_trend_summary"]
        ),
    }

    _assert_floor_invariants(sanitized)
    _hard_fail_scan(sanitized, "<output>")
    return sanitized


def _parse_run_date(generated_at: str) -> date:
    return datetime.fromisoformat(generated_at).date()


def write_conversation_patterns_snapshot(
    aggregates: dict[str, Any],
    data_dir: str | Path,
    *,
    run_date: date | None = None,
) -> Path:
    """Sanitize `aggregates` (a private run's `aggregates.json`, already
    loaded) and write it to `<data_dir>/snapshots/conversation_patterns/
    <run date>.json`. `run_date` defaults to the date portion of the
    aggregates' own `generated_at`. Raises `SanitizeError` and writes
    nothing if sanitization fails."""
    sanitized = sanitize_aggregates(aggregates)
    if run_date is None:
        run_date = _parse_run_date(aggregates["generated_at"])
    out_dir = Path(data_dir) / "snapshots" / "conversation_patterns"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{run_date.isoformat()}.json"
    out_path.write_text(json.dumps(sanitized, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out_path
