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
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq
import yaml

from project_health.schema import get_schema
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


def test_link_checker_every_relative_link_resolves(tmp_path):
    index_path, data_dir, out_dir = _build_two_project_fixture(tmp_path)
    generate_all_projects(index_path, data_dir, out_dir)

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
