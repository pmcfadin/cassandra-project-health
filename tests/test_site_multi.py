"""Tests for multi-project site generation (issue #171, epic #168).

Builds a two-project fixture directly against `project_health.site.
generate.generate_all_projects` -- Cassandra at the data root (the
pre-#171 layout, unchanged) plus one fake subproject under
`projects/<id>/` (`data_layout.project_data_dir`, issue #169) -- the same
minimal "empty metrics snapshot + manifest" shape `tests/test_site.py` /
`tests/test_site_releases.py` already use for a project with no real data
yet.

Covers the issue's own acceptance list:
- the two-project fixture yields `/index.html` and
  `/p/<id>/{,community/,conversations/,governance/,releases/}index.html`;
- a link-checker walks every generated page's relative href/src and
  asserts each resolves to a real file in the output tree;
- Cassandra's own output (`project=None` vs. a single-project
  `ProjectSiteContext`) is byte-for-byte identical, so wiring the new
  multi-project path never changes Cassandra's existing site;
- the header switcher is present (and links both ways) with two projects
  available, and entirely absent with only one.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from project_health.schema import get_schema, validate
from project_health.site.generate import generate, generate_all_projects
from project_health.site.project_site import (
    DiscoveredProject,
    ProjectSiteContext,
    resolve_latest_run_id,
)

CASSANDRA_RUN_ID = "2026-10-12T000000Z-abc123"
SUB_RUN_ID = "2026-10-12T000000Z-def456"
CODE_SHA = "abc123def4567890abc123def4567890abc1234"
BUILD_TIME = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)

_HREF_SRC_RE = re.compile(r'(?:href|src)="([^"]+)"')
# `governance_page.py::_commit_json_payload` stamps its own `generated_at`
# from the real wall clock (not this run's `now`/`build_time`), so two
# otherwise-identical renders a few milliseconds apart never match on that
# one field -- normalized out before any byte-for-byte comparison below.
_GENERATED_AT_RE = re.compile(rb'"generated_at":\s*"[^"]*"')


def _normalize_generated_at(data: bytes) -> bytes:
    return _GENERATED_AT_RE.sub(b'"generated_at":"NORMALIZED"', data)


def _write_empty_snapshot(data_dir: Path, run_id: str) -> None:
    snapshot_dir = data_dir / "snapshots" / run_id
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(get_schema("metric_value").empty_table(), snapshot_dir / "metrics.parquet")


def _write_manifest(data_dir: Path, run_id: str) -> None:
    manifest = {
        "run_id": run_id,
        "trigger": "schedule",
        "started_at": "2026-10-12T06:17:00Z",
        "completed_at": (BUILD_TIME - timedelta(hours=1)).isoformat(),
        "pipeline_code_sha": CODE_SHA,
        "sources": {},
    }
    manifests_dir = data_dir / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    (manifests_dir / f"{run_id}.json").write_text(json.dumps(manifest))


def _write_project_data(data_dir: Path, run_id: str) -> None:
    _write_empty_snapshot(data_dir, run_id)
    _write_manifest(data_dir, run_id)


def _write_two_project_index(tmp_path: Path) -> Path:
    """A `projects/index.yaml` + two project config files: `cassandra`
    (no `shared_sources`, same as the real `projects/cassandra.yaml`) and
    `demo-sub`, a fake subproject declaring the real `shared_sources:
    [asf_roster, ponymail, governance]` every real subproject config
    (`projects/java-driver.yaml` etc.) already carries."""
    (tmp_path / "cassandra.yaml").write_text(
        yaml.safe_dump({"project": {"id": "cassandra", "display_name": "Apache Cassandra"}})
    )
    (tmp_path / "demo-sub.yaml").write_text(
        yaml.safe_dump(
            {
                "project": {"id": "demo-sub", "display_name": "Demo Subproject"},
                "shared_sources": ["asf_roster", "ponymail", "governance"],
            }
        )
    )
    index_path = tmp_path / "index.yaml"
    index_path.write_text(
        yaml.safe_dump(
            [
                {"id": "cassandra", "config": "cassandra.yaml", "enabled": True},
                {"id": "demo-sub", "config": "demo-sub.yaml", "enabled": True},
            ]
        )
    )
    return index_path


def _build_two_project_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Returns (index_path, data_dir, out_dir)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    index_path = _write_two_project_index(tmp_path)
    data_dir = tmp_path / "data"
    _write_project_data(data_dir, CASSANDRA_RUN_ID)
    _write_project_data(data_dir / "projects" / "demo-sub", SUB_RUN_ID)
    out_dir = tmp_path / "out"
    return index_path, data_dir, out_dir


def _all_html_files(out_dir: Path) -> list[Path]:
    return sorted(out_dir.rglob("*.html"))


# --- Realistic subproject fixture (orchestrator review of PR #178, fixup
# round 1) -----------------------------------------------------------------
#
# The two-project fixture above reuses an *empty* metrics snapshot for its
# fake subproject -- fine for structural assertions (page existence, link-
# checking, byte-identity), but it can't catch a shared-source leak: with
# zero rows, `demo-sub`'s dev@ CHAOSS series would already read as
# "insufficient data" whether or not the exclusion logic below ever ran.
# This fixture instead writes *real* `metric_value` rows for exactly the
# git/GitHub/JIRA-sourced M0 metrics a real subproject's own pipeline run
# would compute (java-driver-shaped: has its own JIRA tracker, CASSJAVA) --
# and deliberately never a mailing-list (`ponymail`)/governance-sourced
# metric_id, since a real subproject's data_dir never collects those at
# all (PMC-wide, `shared_sources`). If the CHAOSS card venue-filtering or
# the Conversation patterns "shared" notice (generate.py, this fixup round)
# regressed, this is the fixture that would actually catch it: every
# `MetricSeries` dict entry always exists (`_build_series` registers one
# per `M0_METRICS` key regardless of row count, `generate.py`'s own
# docstring), so an unfiltered dev@ series here would render as a real
# "insufficient data" card with an empty chart, not simply vanish.
REALISTIC_SUB_RUN_ID = "2026-10-12T000000Z-aaa111"

# (metric_id, starting value, per-month step, n) -- six months of a
# plausible-looking trend, enough for a real "latest value" card + chart,
# never zero rows. Covers all four CHAOSS Starter cards' own non-shared
# series (`metrics_meta.CHAOSS_STARTER_METRICS`): GitHub PR review/closure,
# JIRA response/closure, contributor absence factor, release frequency.
_REALISTIC_METRIC_SERIES: tuple[tuple[str, float, float, int], ...] = (
    ("pr_time_to_first_review", 1.4, -0.05, 18),
    ("change_request_closure_ratio_pr", 0.52, 0.01, 18),
    ("time_to_first_response_jira", 0.9, -0.02, 12),
    ("change_request_closure_ratio_jira_patch", 0.47, 0.01, 12),
    ("contributor_absence_factor", 3, 0, 22),
    ("release_frequency", 2, 0, 22),
)


def _realistic_subproject_metric_rows(run_id: str) -> list[dict]:
    rows = []
    months = [date(2026, m, 1) for m in range(1, 7)]
    for metric_id, start_value, step, n in _REALISTIC_METRIC_SERIES:
        for i, month_start in enumerate(months):
            month_end = date(month_start.year, month_start.month, 28)
            rows.append(
                {
                    "metric_id": metric_id,
                    "definition_version": "1.0",
                    "window_start": month_start,
                    "window_end": month_end,
                    "value": float(start_value) + step * i,
                    "n": n,
                    "flag": "ok",
                    "run_id": run_id,
                    "computed_at": datetime(2026, 7, 1, tzinfo=UTC),
                    "details_json": None,
                }
            )
    return rows


def _realistic_metric_value_table(rows: list[dict]) -> pa.Table:
    columns = {name: [row[name] for row in rows] for name in rows[0]}
    table = pa.table(
        {
            "metric_id": pa.array(columns["metric_id"], type=pa.string()),
            "definition_version": pa.array(columns["definition_version"], type=pa.string()),
            "window_start": pa.array(columns["window_start"], type=pa.date32()),
            "window_end": pa.array(columns["window_end"], type=pa.date32()),
            "value": pa.array(columns["value"], type=pa.float64()),
            "n": pa.array(columns["n"], type=pa.int64()),
            "flag": pa.array(columns["flag"], type=pa.string()),
            "run_id": pa.array(columns["run_id"], type=pa.string()),
            "computed_at": pa.array(columns["computed_at"], type=pa.timestamp("us", tz="UTC")),
            "details_json": pa.array(columns["details_json"], type=pa.string()),
        }
    )
    return validate("metric_value", table)


def _write_realistic_subproject_data(data_dir: Path, run_id: str) -> None:
    snapshot_dir = data_dir / "snapshots" / run_id
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    table = _realistic_metric_value_table(_realistic_subproject_metric_rows(run_id))
    pq.write_table(table, snapshot_dir / "metrics.parquet")
    _write_manifest(data_dir, run_id)


def _write_realistic_index(tmp_path: Path) -> Path:
    """Like `_write_two_project_index`, but the subproject
    (`demo-driver`) carries a real `issue_tracker: {type: jira}` (modeling
    java-driver/gocql-driver/..., which all have their own JIRA project) so
    `DiscoveredProject.has_jira` is `True` -- exercising the "GitHub + JIRA,
    no dev@" CHAOSS copy branch, not the JIRA-less "GitHub only" one
    (covered instead by a direct `_chaoss_starter_cards` unit check)."""
    (tmp_path / "cassandra.yaml").write_text(
        yaml.safe_dump({"project": {"id": "cassandra", "display_name": "Apache Cassandra"}})
    )
    (tmp_path / "demo-driver.yaml").write_text(
        yaml.safe_dump(
            {
                "project": {"id": "demo-driver", "display_name": "Demo Driver"},
                "issue_tracker": {"type": "jira", "project_key": "DEMO"},
                "shared_sources": ["asf_roster", "ponymail", "governance"],
            }
        )
    )
    index_path = tmp_path / "index.yaml"
    index_path.write_text(
        yaml.safe_dump(
            [
                {"id": "cassandra", "config": "cassandra.yaml", "enabled": True},
                {"id": "demo-driver", "config": "demo-driver.yaml", "enabled": True},
            ]
        )
    )
    return index_path


def _build_realistic_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Returns (index_path, data_dir, out_dir). Cassandra keeps the plain
    empty-snapshot fixture (its own CHAOSS cards/community cards reading
    "insufficient data" is expected and unrelated to this fixup); the
    subproject gets the realistic git/GitHub/JIRA-only data above."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    index_path = _write_realistic_index(tmp_path)
    data_dir = tmp_path / "data"
    _write_project_data(data_dir, CASSANDRA_RUN_ID)
    _write_realistic_subproject_data(data_dir / "projects" / "demo-driver", REALISTIC_SUB_RUN_ID)
    out_dir = tmp_path / "out"
    return index_path, data_dir, out_dir


def test_two_project_fixture_yields_expected_pages(tmp_path):
    index_path, data_dir, out_dir = _build_two_project_fixture(tmp_path)

    rendered = generate_all_projects(index_path, data_dir, out_dir)

    assert set(rendered) == {"cassandra", "demo-sub"}

    # Cassandra: every project's five tabs, unconditionally emitted.
    assert (out_dir / "index.html").is_file()
    assert (out_dir / "community" / "index.html").is_file()
    assert (out_dir / "conversations" / "index.html").is_file()
    assert (out_dir / "governance" / "index.html").is_file()

    # The subproject fixture (issue #171's own acceptance list): Home,
    # Community, Conversations, Governance, Releases all present under
    # `p/<id>/`, including Releases -- forced even though this fixture has
    # no release-line data at all.
    sub_root = out_dir / "p" / "demo-sub"
    assert (sub_root / "index.html").is_file()
    assert (sub_root / "community" / "index.html").is_file()
    assert (sub_root / "conversations" / "index.html").is_file()
    assert (sub_root / "governance" / "index.html").is_file()
    assert (sub_root / "releases" / "index.html").is_file()

    # Cassandra-only features never appear under the subproject.
    assert not (sub_root / "peers").exists()
    assert not (sub_root / "conversations" / "threads").exists()

    # Static assets and the shared methodology/corrections pair are copied
    # exactly once, at the overall site root -- never duplicated per project.
    assert (out_dir / "static" / "style.css").is_file()
    assert (out_dir / "methodology" / "index.html").is_file()
    assert (out_dir / "corrections" / "index.html").is_file()
    assert not (sub_root / "static").exists()
    assert not (sub_root / "methodology").exists()
    assert not (sub_root / "corrections").exists()


def test_subproject_conversations_and_governance_show_shared_notice(tmp_path):
    index_path, data_dir, out_dir = _build_two_project_fixture(tmp_path)
    generate_all_projects(index_path, data_dir, out_dir)

    sub_root = out_dir / "p" / "demo-sub"
    conversations_html = (sub_root / "conversations" / "index.html").read_text()
    governance_html = (sub_root / "governance" / "index.html").read_text()

    assert "dev@ and user@ mailing lists cover all Apache Cassandra subprojects" in (
        conversations_html
    )
    assert "ASF PMC roster are maintained once" in governance_html
    # Both notices link back to Cassandra's own tab, at the site root.
    assert 'href="../../../conversations/"' in conversations_html
    assert 'href="../../../governance/"' in governance_html
    # No verdict language (D25), and no Cassandra-specific security-check
    # commentary leaking onto a page that never computed it.
    for forbidden in ("ready", "not ready", "Scorecard"):
        assert forbidden not in conversations_html
        assert forbidden not in governance_html


def _find_broken_links(out_dir: Path) -> list[str]:
    """Walk every generated page's relative href/src and return the ones
    that don't resolve to a real file under `out_dir` -- shared by every
    link-checker test in this module."""
    broken: list[str] = []
    for html_path in _all_html_files(out_dir):
        html = html_path.read_text()
        page_dir = html_path.parent
        for link in _HREF_SRC_RE.findall(html):
            if link.startswith(("http://", "https://", "mailto:", "#", "data:")):
                continue
            target = link.split("#", 1)[0]
            if not target:
                continue
            resolved = (page_dir / target).resolve()
            # A directory-style link ("community/") is this static site's
            # own convention for "that directory's index.html" (every nav/
            # footer/switcher link on the site is written this way) --
            # never a literal file path.
            if target.endswith("/"):
                resolved = resolved / "index.html"
            if not resolved.is_file():
                broken.append(f"{html_path.relative_to(out_dir)} -> {link}")
    return broken


def test_link_checker_every_relative_link_resolves(tmp_path):
    index_path, data_dir, out_dir = _build_two_project_fixture(tmp_path)
    generate_all_projects(index_path, data_dir, out_dir)

    broken = _find_broken_links(out_dir)
    assert not broken, "broken relative link(s):\n" + "\n".join(broken)


def test_cassandra_output_identical_single_vs_multi_project(tmp_path):
    """Rendering Cassandra through the new multi-project path (a Cassandra
    `ProjectSiteContext` with only itself available) must produce the
    exact same output tree, byte for byte, as the pre-#171 `project=None`
    call -- the issue's "Cassandra's root path set is identical to a
    single-project build" acceptance criterion."""
    data_dir = tmp_path / "data"
    _write_project_data(data_dir, CASSANDRA_RUN_ID)

    legacy_out = tmp_path / "legacy_out"
    generate(data_dir, CASSANDRA_RUN_ID, legacy_out, now=BUILD_TIME)

    cassandra_project = DiscoveredProject(
        id="cassandra",
        display_name="Apache Cassandra",
        root_path="",
        is_cassandra=True,
        conversations_shared=False,
        governance_shared=False,
        releases_available=False,
        has_jira=True,
        data_dir=data_dir,
        run_id=CASSANDRA_RUN_ID,
    )
    multi_out = tmp_path / "multi_out"
    generate(
        data_dir,
        CASSANDRA_RUN_ID,
        multi_out,
        now=BUILD_TIME,
        project=ProjectSiteContext(current=cassandra_project, available=(cassandra_project,)),
    )

    legacy_files = {
        p.relative_to(legacy_out): p for p in sorted(legacy_out.rglob("*")) if p.is_file()
    }
    multi_files = {
        p.relative_to(multi_out): p for p in sorted(multi_out.rglob("*")) if p.is_file()
    }

    assert set(legacy_files) == set(multi_files)
    for rel_path, legacy_path in legacy_files.items():
        legacy_bytes = _normalize_generated_at(legacy_path.read_bytes())
        multi_bytes = _normalize_generated_at(multi_files[rel_path].read_bytes())
        assert legacy_bytes == multi_bytes, rel_path


def test_resolve_latest_run_id_prefers_latest_run_json(tmp_path):
    data_dir = tmp_path / "data"
    _write_project_data(data_dir, CASSANDRA_RUN_ID)
    (data_dir / "state").mkdir(parents=True, exist_ok=True)
    (data_dir / "state" / "latest_run.json").write_text(
        json.dumps({"run_id": "2099-01-01T000000Z-override", "project": {"id": "cassandra"}})
    )

    assert resolve_latest_run_id(data_dir) == "2099-01-01T000000Z-override"


def test_resolve_latest_run_id_falls_back_to_manifests_and_ignores_other_run_families(
    tmp_path,
):
    """The real `data` branch (pre-#170) has no `state/latest_run.json`
    yet, and its `manifests/` directory also holds a second run family's
    own manifests on a totally different filename shape (`peers/
    collect.py`'s `peers-peers-<timestamp>.json`, issue #145) -- a plain
    lexicographic sort would pick one of those ("p" > "2") instead of the
    main pipeline's own latest run. Regression coverage for exactly that,
    found while building the real site for this issue's own screenshots."""
    data_dir = tmp_path / "data"
    _write_project_data(data_dir, CASSANDRA_RUN_ID)
    (data_dir / "manifests" / "peers-peers-20991231T235959Z.json").write_text("{}")

    assert resolve_latest_run_id(data_dir) == CASSANDRA_RUN_ID


def test_switcher_absent_with_one_project_present_with_two(tmp_path):
    # One project: no switcher markup at all.
    single_index = tmp_path / "single"
    single_index.mkdir()
    (single_index / "cassandra.yaml").write_text(
        yaml.safe_dump({"project": {"id": "cassandra", "display_name": "Apache Cassandra"}})
    )
    (single_index / "index.yaml").write_text(
        yaml.safe_dump([{"id": "cassandra", "config": "cassandra.yaml", "enabled": True}])
    )
    data_dir = single_index / "data"
    _write_project_data(data_dir, CASSANDRA_RUN_ID)
    single_out = single_index / "out"
    generate_all_projects(single_index / "index.yaml", data_dir, single_out)
    single_home = (single_out / "index.html").read_text()
    assert "project-switcher" not in single_home

    # Two projects: switcher present on both homes, with both projects
    # linked (one marked current via aria-current).
    index_path, data_dir2, out_dir = _build_two_project_fixture(tmp_path / "multi")
    generate_all_projects(index_path, data_dir2, out_dir)

    cassandra_home = (out_dir / "index.html").read_text()
    sub_home = (out_dir / "p" / "demo-sub" / "index.html").read_text()
    for home_html in (cassandra_home, sub_home):
        assert "project-switcher" in home_html
        assert "Apache Cassandra" in home_html
        assert "Demo Subproject" in home_html
    assert 'aria-current="page"' in cassandra_home
    assert 'aria-current="page"' in sub_home


def _dev_at_occurrences_outside_shared_note(html: str) -> list[str]:
    """Every substring surrounding a "dev@" occurrence in `html` that is
    NOT part of the shared-mailing-list note -- should always be empty for
    a subproject whose dev@ is shared (orchestrator review of PR #178)."""
    bad = []
    for m in re.finditer("dev@", html):
        start, end = m.start(), m.end()
        snippet = html[max(0, start - 100) : end + 100]
        if "shared across all Apache Cassandra subprojects" not in snippet:
            bad.append(snippet)
    return bad


def test_realistic_subproject_home_chaoss_cards_reflect_actual_venues(tmp_path):
    """A subproject with its own JIRA tracker but shared (PMC-wide) dev@
    (java-driver/gocql-driver-shaped): the Home CHAOSS cards must cite only
    the venues this project actually has data for -- GitHub PR + JIRA, no
    dev@ venue claimed, and the dev@ series collapses to one shared-note
    line instead of an empty "insufficient data" card+chart."""
    index_path, data_dir, out_dir = _build_realistic_fixture(tmp_path)
    rendered = generate_all_projects(index_path, data_dir, out_dir)
    assert set(rendered) == {"cassandra", "demo-driver"}

    home_html = (out_dir / "p" / "demo-driver" / "index.html").read_text()

    # Venue-aware mapping_note copy (reviewer's own example wording).
    assert (
        "Computed for Demo Driver from two venues, shown separately: "
        "GitHub PR time to first review and JIRA time to first response."
        in home_html
    )
    # change_request_closure_ratio keeps its JIRA-patch venue since this
    # project DOES have JIRA -- only the "Patch Available (X's ...)" phrase
    # substitutes the project's own name, never leaves "Cassandra" behind.
    # (Jinja autoescapes the apostrophe to `&#39;`.)
    assert "Patch Available (Demo Driver&#39;s patch-submission equivalent" in home_html
    assert "Patch Available (Cassandra&#39;s patch-submission equivalent" not in home_html

    # No dev@ text anywhere except the one shared-note line, and that line
    # links back to Cassandra's own home page.
    leaks = _dev_at_occurrences_outside_shared_note(home_html)
    assert not leaks, f"unexpected dev@ text outside the shared note: {leaks}"
    assert "dev@ is shared across all Apache Cassandra subprojects." in home_html
    # Home's own `base_prefix` is "./" (not "../"), so its `site_root_prefix`
    # is "./" + the project's own "../../" depth, not a clean "../../../".
    assert 'href="./../../index.html"' in home_html

    # No empty chart container for the excluded dev@ series -- its
    # chart_id never appears at all (the series was dropped, not rendered
    # with zero data points).
    assert "time_to_first_reply_devlist" not in home_html

    # The venues this project DOES have render with real values, not
    # "insufficient data" placeholders.
    assert "chaoss-time_to_first_response-pr_time_to_first_review" in home_html
    assert "chaoss-time_to_first_response-time_to_first_response_jira" in home_html


def test_realistic_subproject_community_shared_sections_use_notice_not_empty_chart(tmp_path):
    index_path, data_dir, out_dir = _build_realistic_fixture(tmp_path)
    generate_all_projects(index_path, data_dir, out_dir)

    community_html = (out_dir / "p" / "demo-driver" / "community" / "index.html").read_text()

    conv_section_match = re.search(
        r'<section class="conversation-patterns-summary".*?</section>', community_html, re.DOTALL
    )
    assert conv_section_match, "conversation-patterns-summary section missing"
    conv_section = conv_section_match.group(0)

    # Shared/classified content (Tone over time, conversation patterns)
    # shows the shared notice, never a chart container or "not published
    # yet" (which would wrongly imply it's just late, not categorically
    # PMC-wide) -- and links back to Cassandra's own Conversations tab.
    assert "data-vega-spec" not in conv_section
    assert "Tone over time" not in conv_section
    assert "not published yet" not in conv_section
    assert re.search(r"shared across all\s+Apache Cassandra subprojects", conv_section)
    assert 'href="../../../conversations/"' in conv_section

    leaks = _dev_at_occurrences_outside_shared_note(community_html)
    # Community's own notice text doesn't literally say "dev@ is shared
    # across all Apache Cassandra subprojects" (it says "classified from
    # the dev@ mailing list, shared across..."), so check the actual
    # wording used there too rather than reusing the Home helper verbatim.
    for snippet in leaks:
        assert "classified from the dev@ mailing list, shared across" in snippet, snippet

    # Peers/thread-explorer stay Cassandra-only (unchanged from issue #171).
    assert "peer-context-summary" in community_html  # section always present
    assert 'href="../peers/"' not in community_html


def test_link_checker_passes_on_realistic_subproject_fixture(tmp_path):
    index_path, data_dir, out_dir = _build_realistic_fixture(tmp_path)
    generate_all_projects(index_path, data_dir, out_dir)

    broken = _find_broken_links(out_dir)
    assert not broken, "broken relative link(s):\n" + "\n".join(broken)


def test_chaoss_cards_github_only_when_no_jira(tmp_path):
    """Direct unit coverage of the other venue-text branch (reviewer's
    "GitHub-only when no JIRA" case, e.g. easy-stress) without a second
    full fixture: `generate._chaoss_starter_cards` with `has_jira=False`
    must drop the JIRA series from both multi-venue cards and never say
    "Patch Available"."""
    from project_health.site.generate import _build_series, _chaoss_starter_cards
    from project_health.site.metrics_meta import CHAOSS_STARTER_METRICS, M0_METRICS
    from project_health.site.manifest import RunManifest

    rows = _realistic_subproject_metric_rows(REALISTIC_SUB_RUN_ID)
    table = _realistic_metric_value_table(rows)
    series_by_id = _build_series(table, M0_METRICS)
    manifest = RunManifest(
        run_id=REALISTIC_SUB_RUN_ID, pipeline_code_sha="a" * 40, sources={}
    )

    cards = _chaoss_starter_cards(
        series_by_id,
        date(2026, 6, 1),
        manifest,
        project_display_name="Easy Stress",
        dev_mailing_list_shared=True,
        has_jira=False,
        cassandra_home_href="../../index.html",
    )
    by_key = {c["key"]: c for c in cards}

    assert by_key["time_to_first_response"]["mapping_note"] == (
        "Computed for Easy Stress from one venue: GitHub PR time to first review."
    )
    assert {s["label"] for s in by_key["time_to_first_response"]["series"]} == {"GitHub PR"}
    assert by_key["time_to_first_response"]["shared_notes"][0]["text"] == (
        "dev@ is shared across all Apache Cassandra subprojects."
    )

    assert (
        by_key["change_request_closure_ratio"]["mapping_note"]
        == "Computed for Easy Stress from GitHub pull requests."
    )
    assert {s["label"] for s in by_key["change_request_closure_ratio"]["series"]} == {
        "GitHub PR"
    }
    assert "Patch Available" not in by_key["change_request_closure_ratio"]["mapping_note"]
    assert CHAOSS_STARTER_METRICS  # sanity: the catalogue itself still loads
