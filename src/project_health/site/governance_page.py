"""Governance page context builder (issue #37, D13-D15).

`build_governance_page_context` turns one run's governance-engine snapshot
outputs (`snapshots/<run_id>/governance_commit_compliance.parquet`,
`governance_commit_fact.parquet`, `governance_metric_value.parquet`) plus
`governance-policy.yaml` and `governance_overrides.yaml` into everything
`templates/governance.html` needs: the policy header, the current FAIL list
with evidence and a correction link, the per-check compliance trend charts
plus the (descriptive, unscored) ninja-count trend, the honest backfill
state from the run manifest, and the per-commit table's downloadable JSON/CSV
data files (issue #36 built the engine; this module and `generate.py` are
the only things that read its output for the page — the engine itself is
untouched).

Kept out of `generate.py` (per the issue's "keep to site/ plus minimal
generate.py wiring") so #36's compliance-engine module boundary and #55's
`_security.html` partial both stay independent of this page section's own
churn.

D15's four transparency requirements are load-bearing design constraints
here, not decoration:
  - every check result shown carries its evidence text/link
    (`_merge_commit_rows` copies `evidence`/`evidence_url` onto every
    commit's `checks[check_id]`, and `_fail_rows` does the same for the
    fails view);
  - `unknown` is never rendered or counted as `fail` (`_fail_rows` filters
    on `result == "fail"` literally, reading the string the engine already
    produced rather than re-deriving one; the engine itself only ever sets
    `fail` via each rule's own narrow, documented condition);
  - every commit row and every fail row carries a `correction_url`
    pointing at the GitHub issue-form template plus
    `governance_overrides.yaml`;
  - every rule's `effective_from` is shown next to it (`_policy_context`),
    and a commit before it is `not_in_force`, never scored against a rule
    that didn't apply yet (enforced upstream by the engine; this module only
    displays what the engine already decided).
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pyarrow as pa
import pyarrow.parquet as pq

from project_health.governance.engine import SCORED_CHECK_IDS
from project_health.governance.metrics import metric_id_for_check
from project_health.governance.overrides import DEFAULT_OVERRIDES_PATH, load_overrides
from project_health.governance.policy import DEFAULT_POLICY_PATH, Policy, load_policy
from project_health.metrics.windows import add_months, month_start
from project_health.schema import get_schema, validate
from project_health.site import chart_spec
from project_health.site.manifest import GovernanceStatus, RunManifest

REPO_URL = "https://github.com/pmcfadin/cassandra-project-health"
GOVERNANCE_POLICY_FILE_URL = f"{REPO_URL}/blob/main/governance-policy.yaml"
GOVERNANCE_OVERRIDES_FILE_URL = f"{REPO_URL}/blob/main/governance_overrides.yaml"
CORRECTION_ISSUE_TEMPLATE = "governance-correction.yml"
CORRECTION_NEW_ISSUE_URL = f"{REPO_URL}/issues/new"

CASSANDRA_COMMIT_URL = "https://github.com/apache/cassandra/commit/{sha}"
JIRA_BROWSE_URL = "https://issues.apache.org/jira/browse/{key}"

# How many completed months of commit history the page's default ("recent")
# JSON data file covers -- the "1280+ commits" scale problem the issue
# names is solved by shipping a small default payload plus a "load all"
# button that fetches the full one, rather than ever embedding 32k+ rows in
# the HTML itself (issue #37: "paginate, or default to the last 12 months
# with a load-all").
RECENT_MONTHS = 12

# A FAIL row is, per D15/GOVERNANCE.md, meant to be rare (v1's own live
# measurement found 10 genuine reviewer-present fails on 2,151 trunk
# commits) -- this cap is a display safety valve, not an expected limit; if
# it's ever hit the page says so and points at the full CSV download.
MAX_FAILS_DISPLAYED = 1000

# Chart colors for the four result states a compliance-trend line can show
# (pass/fail/unknown/exempt -- `not_in_force` is a policy-timeline fact, not
# a rate, and is left out of the trend chart). Fixed hex values (not CSS
# custom properties) because the Vega-Lite spec is inert JSON evaluated by
# vega-embed, which can't read the page's `:root` variables; chosen to read
# clearly against both the light and dark `--bg`/`--bg-alt` in style.css.
_STATE_COLORS = {
    "pass": "#2f855a",
    "fail": "#c53030",
    "unknown": "#b7791f",
    "exempt": "#718096",
}
_STATE_ORDER = ("pass", "fail", "unknown", "exempt")

# issue #69: a governance headline card gets a "backfill pending" tag when
# the run manifest says this run's governance backfill is `partial` *and*
# this check's own latest-month `unknown` share is at least this fraction of
# its scored (pass+fail+unknown) denominator -- a small amount of ordinary
# `unknown` (an evidence source's own documented false-negative rate) never
# earns the tag; a headline that reads mostly `unknown` because backfill
# hasn't caught up yet does.
BACKFILL_PENDING_UNKNOWN_SHARE = 0.25


def correction_url(sha: str, check_id: str | None = None) -> str:
    """A pre-filled "request a correction" GitHub issue-form link (D15:
    "every row links to the corrections process"). `check_id` is included
    when the request is about one specific check (the fails view); the
    per-commit table's row-level link omits it and lets the reporter pick a
    check in the form."""
    params = {
        "template": CORRECTION_ISSUE_TEMPLATE,
        "labels": "governance-correction",
        "sha": sha,
        "title": f"Governance correction: {sha[:10]}" + (f" / {check_id}" if check_id else ""),
    }
    if check_id:
        params["check_id"] = check_id
    return f"{CORRECTION_NEW_ISSUE_URL}?{urlencode(params)}"


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {value!r}")


# --- Reading the governance snapshot (optional -- older/other runs may not
# have run the governance engine at all, per issue #36's own "governance
# metrics are deliberately not registered in metrics.registry.METRIC_IDS" --
# a run without them must still render an honest page, not crash). --------


def _read_optional_snapshot_table(
    data_dir: Path, run_id: str, filename: str, schema_name: str
) -> pa.Table:
    path = Path(data_dir) / "snapshots" / run_id / filename
    if not path.is_file():
        return get_schema(schema_name).empty_table()
    return validate(schema_name, pq.read_table(path))


def _load_policy_safe(policy_path: str | Path = DEFAULT_POLICY_PATH) -> Policy | None:
    try:
        return load_policy(policy_path)
    except FileNotFoundError:
        return None


def _load_overrides_count(overrides_path: str | Path = DEFAULT_OVERRIDES_PATH) -> int:
    return len(load_overrides(overrides_path))


# --- Policy header (D14) -----------------------------------------------------


@dataclass(frozen=True)
class PolicyRuleContext:
    id: str
    description: str
    source_url: str | None
    effective_from: str | None
    fail_allowed: bool
    scored: bool


@dataclass(frozen=True)
class PolicyContext:
    version: int
    approved_by: str
    approved_on: str
    policy_url: str
    overrides_url: str
    overrides_count: int
    rules: list[PolicyRuleContext]


def _policy_context(policy: Policy, overrides_count: int) -> PolicyContext:
    rules = [
        PolicyRuleContext(
            id=rule.id,
            description=rule.description.strip(),
            source_url=rule.raw.get("source_url"),
            effective_from=rule.effective_from.isoformat() if rule.effective_from else None,
            fail_allowed=rule.fail_allowed,
            scored=rule.scored,
        )
        for rule in policy.rules.values()
    ]
    return PolicyContext(
        version=policy.version,
        approved_by=policy.approved_by,
        approved_on=policy.approved_on.isoformat(),
        policy_url=GOVERNANCE_POLICY_FILE_URL,
        overrides_url=GOVERNANCE_OVERRIDES_FILE_URL,
        overrides_count=overrides_count,
        rules=rules,
    )


# --- Backfill / honesty banner (manifest["governance"]) ---------------------


@dataclass(frozen=True)
class BackfillContext:
    status: str | None
    commits_scored: int | None
    compliance_rows: int | None
    ci_pending: int | None
    check_run_pending: int | None

    @property
    def is_partial(self) -> bool:
        return self.status == "partial"

    @property
    def is_failed(self) -> bool:
        return self.status == "failed"

    @property
    def has_status(self) -> bool:
        return self.status is not None


def _backfill_context(governance_manifest: GovernanceStatus | None) -> BackfillContext:
    if governance_manifest is None:
        return BackfillContext(None, None, None, None, None)
    ci = governance_manifest.ci_evidence
    check_runs = governance_manifest.check_runs
    return BackfillContext(
        status=governance_manifest.status,
        commits_scored=governance_manifest.commits_scored,
        compliance_rows=governance_manifest.compliance_rows,
        ci_pending=ci.pending if ci else None,
        check_run_pending=check_runs.pending if check_runs else None,
    )


# --- Per-commit rows (D15: names, evidence, every row correction-linked) ----


def _merge_commit_rows(
    compliance_rows: list[dict[str, Any]], fact_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """One dict per commit, all of that commit's scored check results plus
    its display-only facts folded in -- the shape both the per-commit
    table's JSON data files and the fails view are built from."""
    facts_by_sha = {row["sha"]: row for row in fact_rows}
    by_sha: dict[str, dict[str, Any]] = {}

    for row in compliance_rows:
        sha = row["sha"]
        commit = by_sha.get(sha)
        if commit is None:
            commit = {
                "sha": sha,
                "short_sha": sha[:10],
                "commit_url": CASSANDRA_COMMIT_URL.format(sha=sha),
                "branch": row["branch"],
                "commit_date": row["commit_date"],
                "is_merge": row["is_merge"],
                "author": row["author"],
                "committer": row["committer"],
                "reviewers": list(row["reviewers"]),
                "jira_keys": list(row["jira_keys"]),
                "jira_urls": [JIRA_BROWSE_URL.format(key=k) for k in row["jira_keys"]],
                "policy_version": row["policy_version"],
                "checks": {},
                "correction_url": correction_url(sha),
            }
            by_sha[sha] = commit
        commit["checks"][row["check_id"]] = {
            "result": row["result"],
            "evidence": row["evidence"],
            "evidence_url": row["evidence_url"],
        }

    for sha, commit in by_sha.items():
        fact = facts_by_sha.get(sha)
        commit["facts"] = (
            {
                "changes_txt_touched": fact["changes_txt_touched"],
                "news_txt_touched": fact["news_txt_touched"],
                "test_touched": fact["test_touched"],
            }
            if fact is not None
            else None
        )

    return sorted(by_sha.values(), key=lambda c: c["commit_date"], reverse=True)


def _fail_rows(commit_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every currently-FAIL (commit, check) pair (D15's "currently failing"
    view), newest first -- `unknown` is never included here, by construction
    (the engine only ever writes `result == "fail"` via each rule's own
    narrow, documented condition; see governance-policy.yaml
    `result_semantics.fail`)."""
    fails = []
    for commit in commit_rows:
        for check_id in SCORED_CHECK_IDS:
            check = commit["checks"].get(check_id)
            if check is None or check["result"] != "fail":
                continue
            fails.append(
                {
                    "sha": commit["sha"],
                    "short_sha": commit["short_sha"],
                    "commit_url": commit["commit_url"],
                    "check_id": check_id,
                    "branch": commit["branch"],
                    "commit_date": commit["commit_date"],
                    "author": commit["author"],
                    "committer": commit["committer"],
                    "reviewers": commit["reviewers"],
                    "jira_keys": commit["jira_keys"],
                    "evidence": check["evidence"],
                    "evidence_url": check["evidence_url"],
                    "correction_url": correction_url(commit["sha"], check_id),
                }
            )
    return fails


# --- Filter option lists (month/branch/check/result) ------------------------


def _filter_options(commit_rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    branches: set[str] = set()
    months: set[str] = set()
    for commit in commit_rows:
        branches.add(commit["branch"])
        months.add(commit["commit_date"].strftime("%Y-%m"))
    return {
        "branches": sorted(branches),
        "months": sorted(months, reverse=True),
        "checks": list(SCORED_CHECK_IDS),
        "results": ["pass", "fail", "unknown", "exempt", "not_in_force"],
    }


# --- JSON / CSV data files ---------------------------------------------------


def _commit_json_payload(
    commit_rows: list[dict[str, Any]], policy_version: int | None
) -> dict[str, Any]:
    return {
        "policy_version": policy_version,
        "generated_at": datetime.now(UTC).isoformat(),
        "row_count": len(commit_rows),
        "rows": commit_rows,
    }


def _write_commits_json(
    path: Path, commit_rows: list[dict[str, Any]], policy_version: int | None
) -> None:
    payload = _commit_json_payload(commit_rows, policy_version)
    path.write_text(json.dumps(payload, default=_json_default, separators=(",", ":")))


def _write_commits_csv(path: Path, commit_rows: list[dict[str, Any]]) -> None:
    buffer = io.StringIO()
    header = [
        "sha",
        "commit_url",
        "branch",
        "commit_date",
        "is_merge",
        "author",
        "committer",
        "reviewers",
        "jira_keys",
        "policy_version",
        "changes_txt_touched",
        "news_txt_touched",
        "test_touched",
        "correction_url",
    ]
    for check_id in SCORED_CHECK_IDS:
        header.extend([f"{check_id}_result", f"{check_id}_evidence", f"{check_id}_evidence_url"])

    writer = csv.writer(buffer)
    writer.writerow(header)
    for commit in commit_rows:
        facts = commit["facts"] or {}
        row = [
            commit["sha"],
            commit["commit_url"],
            commit["branch"],
            commit["commit_date"].isoformat(),
            commit["is_merge"],
            commit["author"],
            commit["committer"],
            ";".join(commit["reviewers"]),
            ";".join(commit["jira_keys"]),
            commit["policy_version"],
            facts.get("changes_txt_touched", ""),
            facts.get("news_txt_touched", ""),
            facts.get("test_touched", ""),
            commit["correction_url"],
        ]
        for check_id in SCORED_CHECK_IDS:
            check = commit["checks"].get(check_id)
            if check is None:
                row.extend(["n/a", "", ""])
            else:
                row.extend([check["result"], check["evidence"], check["evidence_url"] or ""])
        writer.writerow(row)
    path.write_text(buffer.getvalue())


# --- Compliance trend charts (per check: monthly pass/fail/unknown/exempt) --
#
# Domain padding, the recent/full-history window, and the low-n
# de-emphasis threshold are shared with generate.py's single-series M0/
# conversations charts -- see `site/chart_spec.py` (issue #28).


def _trend_vega_spec(
    records: list[dict[str, Any]], *, value_field: str, value_format: str, value_title: str
) -> dict[str, Any]:
    """Build a (possibly multi-series) compliance-trend chart's Vega-Lite
    spec. Two layers (a full-opacity line, a point layer whose opacity is
    conditioned on `datum.low_n`) so a low-n/insufficient-data point
    (issue #28) renders de-emphasised without fading the trend line -- same
    approach `generate._vega_lite_spec` uses for the M0/conversations
    charts, kept independent here because this chart's `records` are
    per-(month, state) rows, not per-month rows, and its multi-series color
    encoding must keep working (`_STATE_COLORS`/`_STATE_ORDER`)."""
    dates = [date.fromisoformat(r["month"]) for r in records]
    window = chart_spec.chart_window(dates)

    x_encoding: dict[str, Any] = {
        "field": "month",
        "type": "temporal",
        "timeUnit": "yearmonth",
        "title": None,
        "axis": {"format": "%b %Y"},
    }
    if window is not None:
        # No explicit `tickCount` here (unlike generate.py's chart) --
        # Vega-Lite's automatic temporal ticking already reacts correctly
        # to `static/app.js` swapping this domain for the full one, so
        # there's no separate recent/full tick step to carry.
        x_encoding["scale"] = {"domain": window["domain"]["recent"], "nice": False}

    encoding: dict[str, Any] = {
        "x": x_encoding,
        "y": {
            "field": value_field,
            "type": "quantitative",
            "title": None,
            "axis": {"format": value_format},
        },
        "tooltip": [
            {"field": "month", "type": "temporal", "title": "Month", "format": "%b %Y"},
            {
                "field": value_field,
                "type": "quantitative",
                "title": value_title,
                "format": value_format,
            },
        ],
    }
    if any("state" in r for r in records):
        state_range = [_STATE_COLORS[s] for s in _STATE_ORDER]
        encoding["color"] = {
            "field": "state",
            "type": "nominal",
            "title": "Result",
            "scale": {"domain": list(_STATE_ORDER), "range": state_range},
        }
        encoding["tooltip"].insert(1, {"field": "state", "type": "nominal", "title": "Result"})
    if any("n" in r for r in records):
        encoding["tooltip"].append({"field": "n", "type": "quantitative", "title": "n"})
    if any("flag" in r for r in records):
        encoding["tooltip"].append({"field": "flag", "type": "nominal", "title": "Flag"})

    spec: dict[str, Any] = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "width": "container",
        "height": 160,
        "autosize": {"type": "fit-x", "contains": "padding"},
        "background": None,
        "data": {"values": records},
        # Shared across both layers (Vega-Lite merges a layered spec's
        # top-level `encoding` into each layer); only the point layer's
        # `opacity` differs.
        "encoding": encoding,
        "layer": [
            {"mark": {"type": "line", "clip": True}},
            {
                "mark": {"type": "point", "clip": True, "filled": True},
                "encoding": {"opacity": chart_spec.LOW_N_OPACITY_ENCODING},
            },
        ],
        "config": {"view": {"stroke": None}},
    }
    if window is not None:
        spec["usermeta"] = {"chartWindow": window}
    return spec


def _latest_scored_shares(rows: list[dict[str, Any]]) -> dict[str, float] | None:
    """The most recent `flag == 'ok'` month's pass/fail/unknown shares of
    the *scored* (pass+fail+unknown) denominator -- the identical
    denominator `governance/metrics.py`'s own `value` (the headline pass
    rate) is computed over, never re-derived from a different total (issue
    #69: show the unknown/fail shares beside the pass rate, without
    changing what the pass rate itself means). `None` when there's no
    `ok` month yet (mirrors `MetricSeries.latest`'s own "insufficient_data
    is never a stand-in value" rule in `generate.py`)."""
    ok_rows = [r for r in rows if r["flag"] == "ok"]
    if not ok_rows:
        return None
    latest = max(ok_rows, key=lambda r: r["window_end"])
    details = json.loads(latest["details_json"]) if latest["details_json"] else {}
    n_pass = details.get("pass", 0)
    n_fail = details.get("fail", 0)
    n_unknown = details.get("unknown", 0)
    scored = n_pass + n_fail + n_unknown
    if not scored:
        return None
    return {"pass": n_pass / scored, "fail": n_fail / scored, "unknown": n_unknown / scored}


def _compliance_trend_context(governance_metric_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One multi-line (pass/fail/unknown/exempt rate) chart per scored
    check, from `governance_metric_value.parquet`'s `details_json` counts
    (`governance/metrics.py`) -- `not_in_force` is shown nowhere on this
    chart (it isn't a compliance rate, it's a policy-timeline fact)."""
    rows_by_metric: dict[str, list[dict[str, Any]]] = {}
    for row in governance_metric_rows:
        rows_by_metric.setdefault(row["metric_id"], []).append(row)

    charts = []
    for check_id in SCORED_CHECK_IDS:
        metric_id = metric_id_for_check(check_id)
        rows = sorted(rows_by_metric.get(metric_id, []), key=lambda r: r["window_start"])
        records = []
        for row in rows:
            details = json.loads(row["details_json"]) if row["details_json"] else {}
            total = details.get("total_including_exempt_and_not_in_force") or 0
            window_end = row["window_end"]
            month = window_end.isoformat() if hasattr(window_end, "isoformat") else str(window_end)
            # `row["n"]`/`row["flag"]` are the same scored (pass+fail+
            # unknown) sample size and ok/insufficient_data flag
            # `governance/metrics.py` computed the month's pass rate from
            # (`_latest_scored_shares` above reads the identical fields) --
            # every state's record for this month carries them so the
            # tooltip and low-n de-emphasis (issue #28) read the real
            # sample size, not a re-derived one.
            n = row["n"]
            flag = row["flag"]
            # Every scored check's pass rate is a `value_kind="percent"`
            # metric in `metrics_meta.GOVERNANCE_METRICS` -- a rate over
            # `n` scored commits, not a plain headcount -- so it's eligible
            # for low-n de-emphasis the same as any other rate/ratio/
            # latency chart (`chart_spec.is_low_n`).
            low_n = chart_spec.is_low_n(n, flag, value_kind="percent")
            for state in _STATE_ORDER:
                count = details.get(state, 0)
                rate = (count / total) if total else None
                records.append(
                    {
                        "month": month,
                        "state": state,
                        "rate": rate,
                        "count": count,
                        "n": n,
                        "flag": flag,
                        "low_n": low_n,
                    }
                )
        spec = _trend_vega_spec(
            records, value_field="rate", value_format=".0%", value_title="Share of commits"
        )
        charts.append(
            {
                "check_id": check_id,
                "vega_spec_json": json.dumps(spec),
                "has_data": bool(rows),
                "latest_shares": _latest_scored_shares(rows),
            }
        )
    return charts


def _ninja_trend_context(compliance_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Descriptive-only ninja-exemption count per month (governance-policy.yaml
    `reviewer-present.descriptive_signal`) -- never scored, shown purely as a
    trend indicator next to the reviewer-present rule. Non-merge commits
    only, matching `governance/metrics.py`'s own "aggregate denominators
    exclude merges" convention."""
    counts: dict[str, int] = {}
    for row in compliance_rows:
        if row["check_id"] != "reviewer-present" or row["is_merge"]:
            continue
        if row["result"] != "exempt" or "ninja" not in row["evidence"]:
            continue
        month = date(row["commit_date"].year, row["commit_date"].month, 1).isoformat()
        counts[month] = counts.get(month, 0) + 1

    records = [{"month": month, "count": count} for month, count in sorted(counts.items())]
    spec = _trend_vega_spec(
        records, value_field="count", value_format=",.0f", value_title="Ninja-exempt commits"
    )
    return {
        "vega_spec_json": json.dumps(spec),
        "has_data": bool(records),
    }


# --- Top-level context --------------------------------------------------------


@dataclass(frozen=True)
class GovernanceContext:
    has_data: bool
    policy: PolicyContext | None
    backfill: BackfillContext
    fails: list[dict[str, Any]]
    fails_total: int
    fails_truncated: bool
    compliance_trends: list[dict[str, Any]]
    ninja_trend: dict[str, Any]
    filter_options: dict[str, list[str]]
    scored_check_ids: list[str]
    commits_recent_json_href: str
    commits_full_json_href: str
    commits_full_csv_href: str
    recent_months: int


def build_governance_page_context(
    data_dir: str | Path,
    run_id: str,
    out_dir: Path,
    manifest: RunManifest,
    *,
    base_prefix: str,
    policy_path: str | Path = DEFAULT_POLICY_PATH,
    overrides_path: str | Path = DEFAULT_OVERRIDES_PATH,
) -> GovernanceContext:
    """Build the Governance page's per-commit-detail context and write its
    downloadable `data/governance-commits-*.json`/`.csv` files into
    `out_dir` (issue #37). Honest no-data state (governance engine never
    ran, or produced zero rows for this run) still returns a valid context
    so the page renders a clear "not published yet" section instead of
    crashing.
    """
    data_dir = Path(data_dir)
    policy = _load_policy_safe(policy_path)
    overrides_count = _load_overrides_count(overrides_path)
    backfill = _backfill_context(manifest.governance)

    compliance_table = _read_optional_snapshot_table(
        data_dir, run_id, "governance_commit_compliance.parquet", "commit_compliance"
    )
    fact_table = _read_optional_snapshot_table(
        data_dir, run_id, "governance_commit_fact.parquet", "commit_fact"
    )
    governance_metrics_table = _read_optional_snapshot_table(
        data_dir, run_id, "governance_metric_value.parquet", "metric_value"
    )

    compliance_rows = compliance_table.to_pylist()
    fact_rows = fact_table.to_pylist()
    governance_metric_rows = governance_metrics_table.to_pylist()

    has_data = bool(compliance_rows)

    data_out = Path(out_dir) / "data"
    data_out.mkdir(parents=True, exist_ok=True)
    recent_href = f"{base_prefix}data/governance-commits-recent.json"
    full_json_href = f"{base_prefix}data/governance-commits-full.json"
    full_csv_href = f"{base_prefix}data/governance-commits-full.csv"

    policy_version = policy.version if policy else None
    if not has_data:
        _write_commits_json(data_out / "governance-commits-recent.json", [], policy_version)
        _write_commits_json(data_out / "governance-commits-full.json", [], policy_version)
        _write_commits_csv(data_out / "governance-commits-full.csv", [])
        return GovernanceContext(
            has_data=False,
            policy=_policy_context(policy, overrides_count) if policy else None,
            backfill=backfill,
            fails=[],
            fails_total=0,
            fails_truncated=False,
            compliance_trends=[],
            ninja_trend={"vega_spec_json": "null", "has_data": False},
            filter_options={
                "branches": [],
                "months": [],
                "checks": list(SCORED_CHECK_IDS),
                "results": [],
            },
            scored_check_ids=list(SCORED_CHECK_IDS),
            commits_recent_json_href=recent_href,
            commits_full_json_href=full_json_href,
            commits_full_csv_href=full_csv_href,
            recent_months=RECENT_MONTHS,
        )

    commit_rows = _merge_commit_rows(compliance_rows, fact_rows)
    latest_date = commit_rows[0]["commit_date"].date()
    recent_start = add_months(month_start(latest_date), -(RECENT_MONTHS - 1))
    recent_rows = [c for c in commit_rows if c["commit_date"].date() >= recent_start]

    _write_commits_json(data_out / "governance-commits-recent.json", recent_rows, policy_version)
    _write_commits_json(data_out / "governance-commits-full.json", commit_rows, policy_version)
    _write_commits_csv(data_out / "governance-commits-full.csv", commit_rows)

    all_fails = _fail_rows(commit_rows)
    fails_truncated = len(all_fails) > MAX_FAILS_DISPLAYED
    fails_displayed = all_fails[:MAX_FAILS_DISPLAYED]

    compliance_trends = _compliance_trend_context(governance_metric_rows)
    for chart in compliance_trends:
        shares = chart["latest_shares"]
        chart["backfill_pending"] = bool(
            backfill.is_partial
            and shares is not None
            and shares["unknown"] >= BACKFILL_PENDING_UNKNOWN_SHARE
        )

    return GovernanceContext(
        has_data=True,
        policy=_policy_context(policy, overrides_count) if policy else None,
        backfill=backfill,
        fails=fails_displayed,
        fails_total=len(all_fails),
        fails_truncated=fails_truncated,
        compliance_trends=compliance_trends,
        ninja_trend=_ninja_trend_context(compliance_rows),
        filter_options=_filter_options(commit_rows),
        scored_check_ids=list(SCORED_CHECK_IDS),
        commits_recent_json_href=recent_href,
        commits_full_json_href=full_json_href,
        commits_full_csv_href=full_csv_href,
        recent_months=RECENT_MONTHS,
    )
