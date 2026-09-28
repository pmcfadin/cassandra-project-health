"""Governance page context builder (issue #37, D13-D15; rebuilt as a purely
informational commit-history table by issue #97, per the owner's 2026-09-28
decision — docs/plans/2026-09-28-commit-history-table-design.md, as amended
by the owner's same-day "informational stance" instruction).

**This module never renders a compliance verdict.** The governance engine
(`governance/checks.py`, `governance/engine.py`) still scores every commit
against `governance-policy.yaml` — that scoring keeps running unchanged, and
still backs this project's own understanding of what evidence exists — but
nothing derived from a scored `result`/`state` (pass/fail/unknown/exempt/
not_in_force) is ever shown to a site visitor. Instead, this module turns
each commit's structured evidence (`checks.CheckResult`'s `evidence_kind`/
`evidence_label`/`evidence_at`/`lead_time_seconds`/`reason`, carried on every
`commit_compliance` row, `schema/tables.py`) into a **fact**: what the
public record shows for one commit on one column (reviewed by, CI evidence,
CI artefacts on JIRA, checkstyle), described in the check's own vocabulary
(a reviewer's name and where it came from; a CI artefact's filename and how
long before or after the commit it was attached; a GitHub check-run's own
"success"/"failure" conclusion) rather than this project's pass/fail
judgment of it.

The owner's own words for this (2026-09-28): "This page reports what the
public record shows for each commit. It does not judge compliance; the
Apache Cassandra project and its maintainers set and interpret their own
rules."
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from project_health.governance.checks import (
    CODE_STYLE_CHECKSTYLE,
)
from project_health.governance.fact_metrics import FACT_METRIC_IDS
from project_health.governance.overrides import DEFAULT_OVERRIDES_PATH, load_overrides
from project_health.governance.policy import DEFAULT_POLICY_PATH, Policy
from project_health.schema import get_schema, validate
from project_health.site import chart_spec
from project_health.site.manifest import GovernanceStatus, RunManifest

REPO_URL = "https://github.com/pmcfadin/cassandra-project-health"
GOVERNANCE_POLICY_FILE_URL = f"{REPO_URL}/blob/main/governance-policy.yaml"
GOVERNANCE_OVERRIDES_FILE_URL = f"{REPO_URL}/blob/main/governance_overrides.yaml"

CASSANDRA_COMMIT_URL = "https://github.com/apache/cassandra/commit/{sha}"
JIRA_BROWSE_URL = "https://issues.apache.org/jira/browse/{key}"

# design doc: "the default range starts at 2020-06-25, when the governance
# rules were ratified" -- same date `governance-policy.yaml`'s
# `reviewer-present`/`pre-commit-ci-evidence` `effective_from` uses (the
# ratified cwiki governance page). Commits on/after this date load by
# default; older history is a separate, lazily-loaded file.
DEFAULT_RANGE_START = date(2020, 6, 25)

# The project's own published guidance this page's facts are measured
# against -- shown as plain reference links (References section), never
# quoted as rules this project enforces (the owner's 2026-09-28 "let
# maintainers judge" decision retires the old per-rule policy-quote header).
REFERENCES: tuple[dict[str, str], ...] = (
    {
        "label": "Cassandra Project Governance (cwiki, ratified 2020-06-25)",
        "url": "https://cwiki.apache.org/confluence/display/CASSANDRA/Cassandra+Project+Governance",
    },
    {
        "label": "CI Process (cwiki, ratified 2022-01-13)",
        "url": "https://cwiki.apache.org/confluence/display/CASSANDRA/CI+Process",
    },
    {
        "label": "CI Systems (cwiki)",
        "url": "https://cwiki.apache.org/confluence/display/CASSANDRA/CI+Systems",
    },
    {
        "label": "How to Commit",
        "url": "https://cassandra.apache.org/_/development/how_to_commit.html",
    },
    {
        "label": "Patches",
        "url": "https://cassandra.apache.org/_/development/patches.html",
    },
    {
        "label": "Continuous Integration",
        "url": "https://cassandra.apache.org/_/development/ci.html",
    },
)

PAGE_INTRO = (
    "This page reports what the public record shows for each commit. It does not judge "
    "compliance; the Apache Cassandra project and its maintainers set and interpret their own "
    "rules."
)


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {value!r}")


# --- Reading the governance snapshot (optional -- older/other runs may not
# have run the governance engine at all) -------------------------------------


def _read_optional_snapshot_table(
    data_dir: Path, run_id: str, filename: str, schema_name: str
) -> pa.Table:
    path = Path(data_dir) / "snapshots" / run_id / filename
    if not path.is_file():
        return get_schema(schema_name).empty_table()
    return validate(schema_name, pq.read_table(path))


def _load_overrides_count(overrides_path: str | Path = DEFAULT_OVERRIDES_PATH) -> int:
    return len(load_overrides(overrides_path))


# --- Backfill / honesty banner (manifest["governance"]) ---------------------
#
# Kept: this is about how much of the public record has been *collected* so
# far, not a judgment about any commit -- an operational completeness note,
# not a verdict (D2 rule 6, "nothing changes silently").


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


# --- Removed-exemptions note (kept for its own test/back-compat; no longer
# rendered on the page -- see module docstring) ------------------------------


@dataclass(frozen=True)
class RemovedExemptionContext:
    rule_ids: list[str]
    exemption_id: str
    reason: str


def _removed_exemptions(policy: Policy) -> list[RemovedExemptionContext]:
    """One entry per removed exemption id, listing every rule it was removed
    from. Rules may repeat an exemption's removal with a short cross-reference
    reason ("see reviewer-present..."), so the longest reason is kept.

    Not rendered on the page any more (issue #97: the page shows facts, not
    exemption bookkeeping) -- kept for `tests/test_governance_page_removed.py`
    and any future internal use."""
    by_id: dict[str, RemovedExemptionContext] = {}
    for rule in policy.rules.values():
        for item in rule.raw.get("removed_in_v2") or []:
            exemption_id = item["id"]
            reason = " ".join((item.get("reason") or "").split())
            existing = by_id.get(exemption_id)
            if existing is None:
                by_id[exemption_id] = RemovedExemptionContext(
                    rule_ids=[rule.id], exemption_id=exemption_id, reason=reason
                )
            else:
                existing.rule_ids.append(rule.id)
                if len(reason) > len(existing.reason):
                    by_id[exemption_id] = RemovedExemptionContext(
                        rule_ids=existing.rule_ids, exemption_id=exemption_id, reason=reason
                    )
    return list(by_id.values())


# --- Structured evidence -> facts (issue #97) --------------------------------
#
# `_ci_evidence_fact`/`_ci_artefacts_fact` read a `commit_evidence` row
# (`governance/commit_evidence.py`, computed directly from raw JIRA
# evidence -- never from a scored `commit_compliance` row: orchestrator
# review of 127bd5a found the previous version reading the scored row here,
# which leaked policy vocabulary -- "not applicable (before 2026-08-19)" --
# and hid real evidence for a pre-2026-08-19 commit whose ticket had, in
# fact, been checked). `_checkstyle_fact` and `_reviewer_fact` still read
# `commit_compliance`, which is safe for them: `code-style-checkstyle` has
# no exemption and no dated `effective_from`, and `reviewer_detail` is a
# plain per-commit fact computed independently of any check's result.


def _reviewer_fact(reviewer_detail: list[dict[str, str]]) -> dict[str, Any]:
    """design doc: "Reviewed by: 'S. Tunnicliffe (trailer)' or 'none named'."
    Independent of any check's scored result -- purely whether a reviewer
    name was found, by either evidence source, on this commit."""
    if not reviewer_detail:
        return {"named": False, "names": [], "text": "none named"}
    text = ", ".join(f"{r['name']} ({r['source']})" for r in reviewer_detail)
    return {"named": True, "names": reviewer_detail, "text": text}


_NO_TICKET_CI_EVIDENCE = {
    "bucket": "no_ticket",
    "text": "no ticket referenced",
    "url": None,
    "evidence_at": None,
    "lead_time_seconds": None,
}
_NO_TICKET_CI_ARTEFACTS = {
    "bucket": "no_ticket",
    "text": "no ticket referenced",
    "evidence_at": None,
}


def _ci_evidence_fact(evidence_row: dict[str, Any] | None) -> dict[str, Any]:
    """The "CI evidence" column, straight from `commit_evidence` (issue #97,
    orchestrator review of 127bd5a) -- `evidence_row is None` only for a run
    whose governance snapshot predates this table, an honest gap rather than
    a fabricated "no ticket"."""
    if evidence_row is None:
        return _NO_TICKET_CI_EVIDENCE
    evidence_at = evidence_row["ci_evidence_at"]
    return {
        "bucket": evidence_row["ci_evidence_bucket"],
        "text": evidence_row["ci_evidence_text"],
        "url": evidence_row["ci_evidence_url"],
        "evidence_at": evidence_at.isoformat() if evidence_at else None,
        "lead_time_seconds": evidence_row["ci_evidence_lead_time_seconds"],
    }


def _ci_artefacts_fact(evidence_row: dict[str, Any] | None) -> dict[str, Any]:
    """The "CI artefacts on JIRA" column, straight from `commit_evidence`
    (issue #97, orchestrator review of 127bd5a)."""
    if evidence_row is None:
        return _NO_TICKET_CI_ARTEFACTS
    evidence_at = evidence_row["ci_artefacts_at"]
    return {
        "bucket": evidence_row["ci_artefacts_bucket"],
        "text": evidence_row["ci_artefacts_text"],
        "evidence_at": evidence_at.isoformat() if evidence_at else None,
    }


def _checkstyle_fact(check_row: dict[str, Any] | None) -> dict[str, Any]:
    """`code-style-checkstyle` -> the "Checkstyle" column. Buckets: `success`
    | `failure` | `none` (design doc: "'ant-check-jdk11 success/failure' or
    'no check-run recorded'"). `check_row is None` means the check didn't
    even apply to this commit's branch (pre-4.1) -- same plain "no
    check-run recorded" text as any other reason there's nothing to show,
    since a branch not being covered is itself just a fact, not a policy
    exemption."""
    if check_row is None:
        return {"bucket": "none", "text": "no check-run recorded", "url": None}
    result = check_row["result"]
    if result == "pass":
        return {"bucket": "success", "text": check_row.get("evidence_label"),
                 "url": check_row.get("evidence_url")}
    if result == "fail":
        return {"bucket": "failure", "text": check_row.get("evidence_label"),
                 "url": check_row.get("evidence_url")}
    return {"bucket": "none", "text": check_row.get("reason") or "no check-run recorded",
             "url": None}


def _tags(checks_by_id: dict[str, dict[str, Any]], ninja_declared: bool | None) -> dict[str, bool]:
    """Descriptive tags (design pivot: "Descriptive tags, not exemptions...
    neutral, and never used to excuse or condemn"). `docs_only`/
    `release_process` come from whichever exemption the (unchanged) policy
    engine matched; `ninja` is the independent, always-computed
    `commit_fact.ninja_declared` ("declares ninja")."""
    docs_only = False
    release_process = False
    for row in checks_by_id.values():
        kind = row.get("evidence_kind") or ""
        if kind.startswith("policy_exempt:commit-then-review"):
            docs_only = True
        elif kind.startswith("policy_exempt:release-process"):
            release_process = True
    return {
        "docs_only": docs_only,
        "release_process": release_process,
        "ninja": bool(ninja_declared),
    }


def _merge_commit_rows(
    compliance_rows: list[dict[str, Any]],
    fact_rows: list[dict[str, Any]],
    evidence_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """One fact dict per commit -- the shape the per-commit table's JSON data
    files and row expansion are built from. Never carries a `result`/`state`
    field (issue #97: facts only). `evidence_rows` (issue #97, orchestrator
    review of 127bd5a) is `commit_evidence`'s raw-evidence-derived CI facts,
    keyed by sha -- the CI evidence/CI artefacts columns come from there,
    never from `compliance_rows`."""
    facts_by_sha = {row["sha"]: row for row in fact_rows}
    evidence_by_sha = {row["sha"]: row for row in evidence_rows}
    by_sha: dict[str, dict[str, Any]] = {}
    checks_by_sha: dict[str, dict[str, dict[str, Any]]] = {}

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
                "subject": row["subject"],
                "jira_keys": list(row["jira_keys"]),
                "jira_urls": [JIRA_BROWSE_URL.format(key=k) for k in row["jira_keys"]],
                "reviewer": _reviewer_fact(list(row.get("reviewer_detail") or [])),
            }
            by_sha[sha] = commit
            checks_by_sha[sha] = {}
        checks_by_sha[sha][row["check_id"]] = row

    for sha, commit in by_sha.items():
        checks = checks_by_sha[sha]
        evidence_row = evidence_by_sha.get(sha)
        commit["ci_evidence"] = _ci_evidence_fact(evidence_row)
        commit["ci_artefacts"] = _ci_artefacts_fact(evidence_row)
        commit["checkstyle"] = _checkstyle_fact(checks.get(CODE_STYLE_CHECKSTYLE))
        fact = facts_by_sha.get(sha)
        commit["changes_txt_touched"] = bool(fact["changes_txt_touched"]) if fact else False
        commit["news_txt_touched"] = bool(fact["news_txt_touched"]) if fact else False
        commit["tags"] = _tags(checks, fact["ninja_declared"] if fact is not None else None)

    return sorted(by_sha.values(), key=lambda c: c["commit_date"], reverse=True)


# --- Filter option lists (branch) --------------------------------------------


def _filter_options(commit_rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    branches: set[str] = set()
    for commit in commit_rows:
        branches.add(commit["branch"])
    return {"branches": sorted(branches)}


# --- JSON data files ----------------------------------------------------------


def _commit_json_payload(commit_rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "row_count": len(commit_rows),
        "rows": commit_rows,
    }


def _write_commits_json(path: Path, commit_rows: list[dict[str, Any]]) -> None:
    payload = _commit_json_payload(commit_rows)
    path.write_text(json.dumps(payload, default=_json_default, separators=(",", ":")))


# --- Trend cards: plain, neutral rates (issue #97 amends issue #36/#69) -----
#
# "Trend cards become plain rates with neutral labels ... No thresholds, no
# good/bad colors, no 'declining' judgments." Each chart is a single-series
# monthly share (no color-by-result-state breakdown, no red/green/yellow) --
# `governance/metrics.py`'s already-computed `value` (pass / scored) is
# reused directly as that share; this module never recomputes or relabels it
# as a "pass rate" anywhere a reader sees it.


def _trend_vega_spec(records: list[dict[str, Any]]) -> dict[str, Any]:
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
        x_encoding["scale"] = {"domain": window["domain"]["recent"], "nice": False}

    encoding: dict[str, Any] = {
        "x": x_encoding,
        "y": {
            "field": "share",
            "type": "quantitative",
            "title": None,
            "axis": {"format": ".0%"},
        },
        "tooltip": [
            {"field": "month", "type": "temporal", "title": "Month", "format": "%b %Y"},
            {"field": "share", "type": "quantitative", "title": "Share", "format": ".0%"},
            {"field": "n", "type": "quantitative", "title": "n"},
            {"field": "flag", "type": "nominal", "title": "Flag"},
        ],
    }

    spec: dict[str, Any] = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "width": "container",
        "height": 160,
        "autosize": {"type": "fit-x", "contains": "padding"},
        "background": None,
        "data": {"values": records},
        "encoding": encoding,
        "layer": [
            {"mark": {"type": "line", "clip": True, "color": "#718096"}},
            {
                "mark": {"type": "point", "clip": True, "filled": True, "color": "#718096"},
                "encoding": {"opacity": chart_spec.LOW_N_OPACITY_ENCODING},
            },
        ],
        "config": {"view": {"stroke": None}},
    }
    if window is not None:
        spec["usermeta"] = {"chartWindow": window}
    return spec


def _compliance_trend_context(governance_metric_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One neutral, single-series monthly-share chart per fact-based trend
    metric (`fact_metrics.FACT_METRIC_IDS`) -- issue #97, D25 amendment.
    `governance_metric_rows` is the *entire* `governance_metric_value.parquet`
    snapshot, which also still carries the internal-only policy pass-rate
    rows (`governance/metrics.py`); this function only ever reads the fact
    metric_ids, so those internal rows are silently ignored here, never
    rendered."""
    rows_by_metric: dict[str, list[dict[str, Any]]] = {}
    for row in governance_metric_rows:
        rows_by_metric.setdefault(row["metric_id"], []).append(row)

    charts = []
    for metric_id in FACT_METRIC_IDS:
        rows = sorted(rows_by_metric.get(metric_id, []), key=lambda r: r["window_start"])
        records = []
        for row in rows:
            window_end = row["window_end"]
            month = window_end.isoformat() if hasattr(window_end, "isoformat") else str(window_end)
            n = row["n"]
            flag = row["flag"]
            low_n = chart_spec.is_low_n(n, flag, value_kind="percent")
            records.append(
                {"month": month, "share": row["value"], "n": n, "flag": flag, "low_n": low_n}
            )
        spec = _trend_vega_spec(records)
        charts.append(
            {
                "metric_id": metric_id,
                "vega_spec_json": json.dumps(spec),
                "has_data": bool(rows),
            }
        )
    return charts


def _ninja_trend_context(commit_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Descriptive-only ninja-declared count per month -- never scored,
    independent of any check (D24: no official source for "ninja")."""
    counts: dict[str, int] = {}
    for commit in commit_rows:
        if commit["is_merge"] or not commit["tags"]["ninja"]:
            continue
        commit_date = commit["commit_date"]
        month = date(commit_date.year, commit_date.month, 1).isoformat()
        counts[month] = counts.get(month, 0) + 1

    records = [{"month": month, "count": count} for month, count in sorted(counts.items())]
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
        x_encoding["scale"] = {"domain": window["domain"]["recent"], "nice": False}
    spec: dict[str, Any] = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "width": "container",
        "height": 160,
        "autosize": {"type": "fit-x", "contains": "padding"},
        "background": None,
        "data": {"values": records},
        "encoding": {
            "x": x_encoding,
            "y": {
                "field": "count",
                "type": "quantitative",
                "title": None,
                "axis": {"format": ",.0f"},
            },
            "tooltip": [
                {"field": "month", "type": "temporal", "title": "Month", "format": "%b %Y"},
                {"field": "count", "type": "quantitative", "title": "Commits"},
            ],
        },
        "layer": [
            {"mark": {"type": "line", "clip": True, "color": "#718096"}},
            {"mark": {"type": "point", "clip": True, "filled": True, "color": "#718096"}},
        ],
        "config": {"view": {"stroke": None}},
    }
    if window is not None:
        spec["usermeta"] = {"chartWindow": window}
    return {"vega_spec_json": json.dumps(spec), "has_data": bool(records)}


# --- Top-level context --------------------------------------------------------


@dataclass(frozen=True)
class GovernanceContext:
    has_data: bool
    overrides_url: str
    overrides_count: int
    backfill: BackfillContext
    compliance_trends: list[dict[str, Any]]
    ninja_trend: dict[str, Any]
    filter_options: dict[str, list[str]]
    references: tuple[dict[str, str], ...]
    page_intro: str
    commits_default_json_href: str
    commits_older_json_href: str
    default_range_start: str


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
    """Build the Governance page's commit-history context and write its
    downloadable `data/governance-commits-*.json` files into `out_dir`
    (issue #37; rebuilt fact-only by issue #97). Honest empty state (the
    governance engine never ran, or produced zero rows for this run) still
    returns a valid context so the page renders cleanly instead of crashing.
    """
    data_dir = Path(data_dir)
    # `policy_path` is accepted for API stability / future use (e.g. a
    # future internal-only diagnostics view) but this page no longer reads
    # or renders anything from the policy itself (issue #97: facts, not a
    # policy header) -- `_load_policy_safe`/`Policy` stay imported only for
    # `_removed_exemptions`, kept for `tests/test_governance_page_removed.py`.
    overrides_count = _load_overrides_count(overrides_path)
    backfill = _backfill_context(manifest.governance)

    compliance_table = _read_optional_snapshot_table(
        data_dir, run_id, "governance_commit_compliance.parquet", "commit_compliance"
    )
    fact_table = _read_optional_snapshot_table(
        data_dir, run_id, "governance_commit_fact.parquet", "commit_fact"
    )
    # issue #97 (orchestrator review of 127bd5a): the CI evidence/CI
    # artefacts columns come from this raw-evidence-derived table, never
    # from `compliance_table` -- see `governance/commit_evidence.py`.
    evidence_table = _read_optional_snapshot_table(
        data_dir, run_id, "governance_commit_evidence.parquet", "commit_evidence"
    )
    governance_metrics_table = _read_optional_snapshot_table(
        data_dir, run_id, "governance_metric_value.parquet", "metric_value"
    )

    compliance_rows = compliance_table.to_pylist()
    fact_rows = fact_table.to_pylist()
    evidence_rows = evidence_table.to_pylist()
    governance_metric_rows = governance_metrics_table.to_pylist()

    has_data = bool(compliance_rows)

    data_out = Path(out_dir) / "data"
    data_out.mkdir(parents=True, exist_ok=True)
    default_href = f"{base_prefix}data/governance-commits-default.json"
    older_href = f"{base_prefix}data/governance-commits-older.json"

    if not has_data:
        _write_commits_json(data_out / "governance-commits-default.json", [])
        _write_commits_json(data_out / "governance-commits-older.json", [])
        return GovernanceContext(
            has_data=False,
            overrides_url=GOVERNANCE_OVERRIDES_FILE_URL,
            overrides_count=overrides_count,
            backfill=backfill,
            compliance_trends=[],
            ninja_trend={"vega_spec_json": "null", "has_data": False},
            filter_options={"branches": []},
            references=REFERENCES,
            page_intro=PAGE_INTRO,
            commits_default_json_href=default_href,
            commits_older_json_href=older_href,
            default_range_start=DEFAULT_RANGE_START.isoformat(),
        )

    commit_rows = _merge_commit_rows(compliance_rows, fact_rows, evidence_rows)
    default_rows = [c for c in commit_rows if c["commit_date"].date() >= DEFAULT_RANGE_START]
    older_rows = [c for c in commit_rows if c["commit_date"].date() < DEFAULT_RANGE_START]

    _write_commits_json(data_out / "governance-commits-default.json", default_rows)
    _write_commits_json(data_out / "governance-commits-older.json", older_rows)

    compliance_trends = _compliance_trend_context(governance_metric_rows)

    return GovernanceContext(
        has_data=True,
        overrides_url=GOVERNANCE_OVERRIDES_FILE_URL,
        overrides_count=overrides_count,
        backfill=backfill,
        compliance_trends=compliance_trends,
        ninja_trend=_ninja_trend_context(commit_rows),
        filter_options=_filter_options(commit_rows),
        references=REFERENCES,
        page_intro=PAGE_INTRO,
        commits_default_json_href=default_href,
        commits_older_json_href=older_href,
        default_range_start=DEFAULT_RANGE_START.isoformat(),
    )
