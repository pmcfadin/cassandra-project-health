"""Aggregate monthly compliance metrics per check (issue #36).

`compute_monthly_check_metrics` turns a `commit_compliance` table (`sha`,
`branch`, `commit_date`, ..., `check_id`, `result`, ...) into `metric_value`
rows (the same generic output-contract schema `metrics/engine.py` uses,
`schema/tables.py` `METRIC_VALUE`) — one row per `(check_id, month)`, with
`pass`/`fail`/`unknown`/`exempt`/`not_in_force` counts in `details_json` and
a `value` = pass rate among *scored* outcomes (`pass`+`fail`+`unknown`;
`exempt` and `not_in_force` are excluded from both the numerator and
denominator, since neither means "the rule was or wasn't met" — D14/D15).

Per docs/spec/GOVERNANCE.md §3 ("Reserve `--no-merges` for *aggregate*
rate/denominator calculations only"), every aggregate here is computed over
`is_merge = false` rows only — a merge commit's real, evidenced result still
exists as its own `commit_compliance` row (never suppressed there), it just
doesn't additionally count toward the monthly rate.

This deliberately does **not** go through `metrics/engine.py`'s
`compute_all`/`DEFINITION_VERSIONS`/`METRIC_IDS` machinery — governance
metrics are registered separately (`governance/registry.py`) and written to
their own `raw/governance/metric_value` partition
(`project_health.pipeline`'s governance step), so a governance-specific
metric can never trip `pipeline.py`'s "a *registered* M0 metric produced
zero rows" `metrics_missing` check (issue #24) for projects/runs where
governance isn't configured.
"""

from __future__ import annotations

import json
from datetime import date, datetime

import duckdb
import pyarrow as pa

from project_health.metrics.windows import is_completed_month, month_end
from project_health.schema import get_schema, validate

DEFINITION_VERSION = "1.0"

_RESULT_STATES = ("pass", "fail", "unknown", "exempt", "not_in_force")


def metric_id_for_check(check_id: str) -> str:
    """`governance-policy.yaml` check_id -> this rule's monthly pass-rate
    `metric_id` (e.g. `reviewer-present` -> `governance_reviewer_present_pass_rate`)."""
    return f"governance_{check_id.replace('-', '_')}_pass_rate"


def compute_monthly_check_metrics(
    commit_compliance: pa.Table,
    *,
    as_of: date,
    run_id: str,
    computed_at: datetime,
) -> pa.Table:
    """One `metric_value` row per `(check_id, completed month)` present in
    `commit_compliance`, restricted to `is_merge = false` rows.

    Returns an empty (but schema-valid) `metric_value` table if
    `commit_compliance` has no rows.
    """
    if commit_compliance.num_rows == 0:
        return get_schema("metric_value").empty_table()

    con = duckdb.connect(":memory:")
    try:
        con.execute("SET TimeZone='UTC'")
        con.register("commit_compliance", commit_compliance)

        check_ids = [r[0] for r in con.execute(
            "SELECT DISTINCT check_id FROM commit_compliance ORDER BY 1"
        ).fetchall()]

        rows: list[dict] = []
        for check_id in check_ids:
            monthly = con.execute(
                """
                SELECT
                    date_trunc('month', commit_date)::DATE AS month_start,
                    COUNT(*) FILTER (WHERE result = 'pass') AS n_pass,
                    COUNT(*) FILTER (WHERE result = 'fail') AS n_fail,
                    COUNT(*) FILTER (WHERE result = 'unknown') AS n_unknown,
                    COUNT(*) FILTER (WHERE result = 'exempt') AS n_exempt,
                    COUNT(*) FILTER (WHERE result = 'not_in_force') AS n_not_in_force,
                    COUNT(*) AS n_total
                FROM commit_compliance
                WHERE check_id = ? AND is_merge = false
                GROUP BY 1
                ORDER BY 1
                """,
                [check_id],
            ).fetchall()

            for (
                month_start,
                n_pass,
                n_fail,
                n_unknown,
                n_exempt,
                n_not_in_force,
                n_total,
            ) in monthly:
                if not is_completed_month(month_start, as_of):
                    continue
                scored_n = n_pass + n_fail + n_unknown
                value = (n_pass / scored_n) if scored_n else None
                rows.append(
                    {
                        "metric_id": metric_id_for_check(check_id),
                        "definition_version": DEFINITION_VERSION,
                        "window_start": month_start,
                        "window_end": month_end(month_start),
                        "value": value,
                        "n": scored_n,
                        "flag": "ok" if scored_n else "insufficient_data",
                        "run_id": run_id,
                        "computed_at": computed_at,
                        "details_json": json.dumps(
                            {
                                "check_id": check_id,
                                "pass": n_pass,
                                "fail": n_fail,
                                "unknown": n_unknown,
                                "exempt": n_exempt,
                                "not_in_force": n_not_in_force,
                                "total_including_exempt_and_not_in_force": n_total,
                            },
                            sort_keys=True,
                        ),
                    }
                )
    finally:
        con.close()

    schema = get_schema("metric_value")
    table = pa.Table.from_pylist(rows, schema=schema) if rows else schema.empty_table()
    return validate("metric_value", table)
