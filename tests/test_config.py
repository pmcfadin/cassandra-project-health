"""Tests for project_health.config (ARCHITECTURE.md §2.1)."""

import json
from pathlib import Path

import pytest
import yaml

from project_health.config import (
    ReleasesConfig,
    load_project,
    load_project_index,
    resolve_index_config_path,
)


def test_load_cassandra_project_config():
    config = load_project("projects/cassandra.yaml")

    assert config.project.id == "cassandra"
    assert config.project.display_name == "Apache Cassandra"

    assert config.reviewer_extraction.jira_fields.reviewers_field == "customfield_12313420"
    assert config.reviewer_extraction.jira_fields.reviewer_field == "customfield_10022"
    assert config.reviewer_extraction.commit_trailer.exclude_merge_commits is True


def test_load_project_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        load_project("projects/does-not-exist.yaml")


def test_unknown_and_future_sections_load_without_error(tmp_path):
    """mailing_lists/roster/slack and any wholly unrecognized section must
    not cause a load failure — only the fields this package actually reads
    are strictly typed.
    """
    config_path = tmp_path / "future.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "project": {"id": "future", "display_name": "Future Project"},
                "reviewer_extraction": {
                    "commit_trailer": {
                        "type": "commit_message_regex",
                        "pattern": ".*",
                        "exclude_merge_commits": True,
                    },
                    "jira_fields": {"type": "jira_custom_field"},
                },
                "mailing_lists": {"type": "ponymail", "domain": "example.org", "lists": ["dev"]},
                "roster": {"type": "asf_whimsy", "extra_future_field": 42},
                "slack": {"enabled": False},
                "a_wholly_new_section_no_model_knows_about": {"foo": "bar"},
            }
        )
    )

    config = load_project(config_path)

    assert config.project.id == "future"
    assert config.mailing_lists.type == "ponymail"
    assert config.roster.type == "asf_whimsy"
    assert config.slack.enabled is False


def test_mailing_lists_is_typed_with_domain_and_lists():
    """issue #33: `mailing_lists` is a typed model, not a bare `FlexibleSection`."""
    config = load_project("projects/cassandra.yaml")

    assert config.mailing_lists.type == "ponymail"
    assert config.mailing_lists.domain == "cassandra.apache.org"
    assert config.mailing_lists.lists == ["dev", "user"]
    assert config.mailing_lists.metadata_only is True


def test_bot_patterns_and_baseline_window():
    config = load_project("projects/cassandra.yaml")

    assert config.baseline_window.trailing_months == 24
    assert config.baseline_window.min_completed_months == 12

    fields = {pattern.field for pattern in config.bot_patterns}
    assert "git_author_email" in fields
    assert "github_login" in fields
    assert "jira_username" in fields


def test_copilot_reviewer_bot_pattern():
    """Issue #145 fixup round 2 (orchestrator review of PR #147): added as
    a precaution -- GitHub's own automated Copilot PR-review account was
    empirically confirmed on four of the five peer projects (same
    cross-repo GitHub feature), not verified against Cassandra's own data
    (this task never pulled the production `data` branch locally)."""
    from project_health.collectors.github import _is_bot_login

    config = load_project("projects/cassandra.yaml")
    assert _is_bot_login("copilot-pull-request-reviewer", config.bot_patterns)
    assert _is_bot_login("copilot-pull-request-reviewer[bot]", config.bot_patterns)
    assert not _is_bot_login("a-real-committer", config.bot_patterns)


def test_truck_factor_excluded_path_globs(tmp_path):
    config = load_project("projects/cassandra.yaml")
    assert config.truck_factor.excluded_path_globs  # non-empty placeholder set
    assert all(isinstance(g, str) for g in config.truck_factor.excluded_path_globs)


def test_truck_factor_defaults_to_no_exclusions_when_absent(tmp_path):
    """A project config that never mentions `truck_factor:` must still load
    (it's a new, optional section) with an empty exclusion list, not fail."""
    import yaml

    config_path = tmp_path / "no_truck_factor.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "project": {"id": "bare", "display_name": "Bare Project"},
                "reviewer_extraction": {
                    "commit_trailer": {
                        "type": "commit_message_regex",
                        "pattern": ".*",
                        "exclude_merge_commits": True,
                    },
                    "jira_fields": {"type": "jira_custom_field"},
                },
            }
        )
    )

    config = load_project(config_path)
    assert config.truck_factor.excluded_path_globs == []


# --- issue #169: multi-project registry + config reorg -------------------


class TestExtendsDeepMerge:
    """`load_project`'s `extends:` support -- a project YAML's own sections
    deep-merge onto a shared base, winning any key both define."""

    def _write(self, path: Path, content: dict) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(content))
        return path

    def test_project_wins_on_a_shared_scalar_and_shared_list(self, tmp_path):
        self._write(
            tmp_path / "_shared.yaml",
            {
                "bot_patterns": [{"field": "github_login", "regex": "shared-pattern"}],
                "affiliations_file": "shared_affiliations.yaml",
            },
        )
        project_path = self._write(
            tmp_path / "child.yaml",
            {
                "extends": "_shared.yaml",
                "project": {"id": "child", "display_name": "Child"},
                "affiliations_file": "child_affiliations.yaml",
            },
        )

        config = load_project(project_path)

        # Only in the shared base -> inherited unchanged.
        assert config.bot_patterns[0].regex == "shared-pattern"
        # In both -> the project's own value wins.
        assert config.affiliations_file == "child_affiliations.yaml"

    def test_nested_dict_keys_merge_rather_than_replace_wholesale(self, tmp_path):
        """`reviewer_extraction.jira_fields` from the shared base and
        `reviewer_extraction.commit_trailer` from the project itself must
        both survive in the merged `reviewer_extraction` block (this is
        exactly cassandra.yaml's own split, issue #169)."""
        self._write(
            tmp_path / "_shared.yaml",
            {
                "reviewer_extraction": {
                    "jira_fields": {"type": "jira_custom_field", "reviewers_field": "cf_1"}
                }
            },
        )
        project_path = self._write(
            tmp_path / "child.yaml",
            {
                "extends": "_shared.yaml",
                "project": {"id": "child", "display_name": "Child"},
                "reviewer_extraction": {
                    "commit_trailer": {
                        "type": "commit_message_regex",
                        "pattern": ".*",
                        "exclude_merge_commits": True,
                    }
                },
            },
        )

        config = load_project(project_path)

        assert config.reviewer_extraction.jira_fields.reviewers_field == "cf_1"
        assert config.reviewer_extraction.commit_trailer.pattern == ".*"

    def test_extends_accepts_a_list_of_bases_merged_in_order(self, tmp_path):
        """Fixup round 1 (PR #177 review): `extends:` can be a list so a
        project layers more than one shared base -- e.g. cassandra.yaml's
        `extends: [_identity.yaml, _shared.yaml]`. Each base contributes
        its own keys; the project's own file still wins over either."""
        self._write(
            tmp_path / "_identity.yaml",
            {"bot_patterns": [{"field": "github_login", "regex": "identity-pattern"}]},
        )
        self._write(
            tmp_path / "_shared.yaml",
            {"affiliations_file": "shared_affiliations.yaml", "org_domains_file": "x.yaml"},
        )
        project_path = self._write(
            tmp_path / "child.yaml",
            {
                "extends": ["_identity.yaml", "_shared.yaml"],
                "project": {"id": "child", "display_name": "Child"},
                "org_domains_file": "child_org_domains.yaml",
            },
        )

        config = load_project(project_path)

        assert config.bot_patterns[0].regex == "identity-pattern"
        assert config.affiliations_file == "shared_affiliations.yaml"
        assert config.org_domains_file == "child_org_domains.yaml"  # project's own file wins

    def test_missing_extends_target_raises_file_not_found(self, tmp_path):
        project_path = self._write(
            tmp_path / "child.yaml",
            {
                "extends": "does_not_exist.yaml",
                "project": {"id": "child", "display_name": "Child"},
            },
        )

        with pytest.raises(FileNotFoundError):
            load_project(project_path)

    def test_extends_key_itself_does_not_leak_into_the_resolved_config(self, tmp_path):
        self._write(tmp_path / "_shared.yaml", {"affiliations_file": "shared.yaml"})
        project_path = self._write(
            tmp_path / "child.yaml",
            {
                "extends": "_shared.yaml",
                "project": {"id": "child", "display_name": "Child"},
            },
        )

        config = load_project(project_path)

        assert "extends" not in config.model_dump()
        assert not hasattr(config, "extends")


class TestProjectIndex:
    """`projects/index.yaml` -- every entry must resolve to a config that
    loads, in index order; `cassandra` is first and enabled."""

    def test_index_entries_in_order_with_cassandra_first_and_enabled(self):
        entries = load_project_index("projects/index.yaml")

        assert [e.id for e in entries][0] == "cassandra"
        assert entries[0].enabled is True
        assert len(entries) == 11  # cassandra + the 10 subprojects (issue #169)

    def test_peers_yaml_is_not_an_index_entry(self):
        entries = load_project_index("projects/index.yaml")
        assert "peers" not in {e.id for e in entries}
        assert all(e.config != "peers.yaml" for e in entries)

    def test_every_index_entry_loads(self):
        """Every registered project's config file -- enabled or not -- must
        load into a valid `ProjectConfig` without error (issue #169's
        acceptance criterion)."""
        entries = load_project_index("projects/index.yaml")

        for entry in entries:
            config_path = resolve_index_config_path("projects/index.yaml", entry)
            config = load_project(config_path)
            assert config.project.id == entry.id

    # issue #181: accord/ccm have zero GA release tags at all (verified
    # live), so their configs omit `releases:` entirely -- `release`
    # becomes `not_applicable` (pipeline.applicable_sources) rather than a
    # real, permanently-empty collection that degrades every run. Every
    # OTHER subproject still configures `releases:` with an explicit
    # `tag_prefix`.
    _NO_RELEASES_SUBPROJECTS = frozenset({"accord", "ccm"})

    def test_disabled_subproject_entries_have_tag_prefix_or_github_issues(self):
        """Live-verified placeholder data (issue #169, #181): every
        subproject either resolves a non-default `releases.tag_prefix`, or
        has no `releases` section loaded at all because it genuinely has no
        GA release tags (`_NO_RELEASES_SUBPROJECTS`) -- i.e. none of the 10
        new entries are unconfigured boilerplate that happened to pass
        validation by accident."""
        entries = load_project_index("projects/index.yaml")
        subprojects = [e for e in entries if e.id != "cassandra"]
        assert len(subprojects) == 10

        for entry in subprojects:
            config_path = resolve_index_config_path("projects/index.yaml", entry)
            config = load_project(config_path)
            if entry.id in self._NO_RELEASES_SUBPROJECTS:
                assert config.releases is None, f"{entry.id}: expected no releases: section"
                continue
            assert config.releases is not None, f"{entry.id}: expected a releases: section"
            assert config.releases.tag_prefix is not None  # every subproject sets one explicitly


class TestSubprojectIdentityAndPullRequests:
    """Fixup round 1 (orchestrator review of PR #177): every project in the
    registry -- enabled or not -- must inherit PMC-wide identity/bot
    settings (projects/_identity.yaml, via `extends:`) so dependabot/CI-bot
    activity can't leak into its contributor metrics, and must declare its
    own repo under `pull_requests` so PR-based review metrics have
    something to collect from at all."""

    def test_every_entry_has_non_empty_bot_patterns(self):
        entries = load_project_index("projects/index.yaml")

        for entry in entries:
            config_path = resolve_index_config_path("projects/index.yaml", entry)
            config = load_project(config_path)
            assert config.bot_patterns, f"{entry.id}: bot_patterns is empty"

    def test_every_entry_declares_pull_requests_for_its_own_repo(self):
        entries = load_project_index("projects/index.yaml")

        for entry in entries:
            config_path = resolve_index_config_path("projects/index.yaml", entry)
            config = load_project(config_path)
            assert config.repos, f"{entry.id}: no repos configured"
            own_repo = f"{config.repos[0].owner}/{config.repos[0].name}"
            assert config.pull_requests is not None, f"{entry.id}: no pull_requests section"
            assert own_repo in config.pull_requests.repos, (
                f"{entry.id}: {own_repo!r} not in pull_requests.repos "
                f"({config.pull_requests.repos!r})"
            )


class TestReleasesEffectiveTagPrefix:
    """`ReleasesConfig.effective_tag_prefix` (issue #169) -- the resolver
    `pipeline.py`'s hard-coded `f"{repo_cfg.name}-"` (~line 3188) will be
    swapped to call in issue #170."""

    def test_default_reproduces_cassandra_prefix_when_tag_prefix_unset(self):
        releases = ReleasesConfig(type="git_tags_cross_checked_archive")
        assert releases.effective_tag_prefix("cassandra") == "cassandra-"

    def test_explicit_empty_string_prefix_is_not_treated_as_unset(self):
        """Issue #169: `tag_prefix: ""` (apache/cassandra-java-driver,
        -python-driver, -cpp-driver, -accord, -ccm -- verified live, none
        prefix their tags) must resolve to `""`, NOT fall back to the
        `<repo>-` default the way an actually-absent `tag_prefix` does."""
        releases = ReleasesConfig(type="git_tags_cross_checked_archive", tag_prefix="")
        assert releases.effective_tag_prefix("cassandra-java-driver") == ""

    def test_explicit_prefix_wins_over_default(self):
        releases = ReleasesConfig(type="git_tags_cross_checked_archive", tag_prefix="v")
        assert releases.effective_tag_prefix("cassandra-nodejs-driver") == "v"

    def test_cassandra_project_config_resolves_default_prefix(self):
        """End-to-end: Cassandra's own `releases:` block never sets
        `tag_prefix`, so it must resolve through the default and reproduce
        today's `cassandra-` prefix exactly."""
        config = load_project("projects/cassandra.yaml")
        assert config.releases.tag_prefix is None
        assert config.releases.effective_tag_prefix(config.repos[0].name) == "cassandra-"


class TestCassandraConfigBeforeAfterEquality:
    """The projects/_shared.yaml split + new optional fields (issue #169)
    must not change Cassandra's own resolved config at all. The fixture was
    captured by running `load_project("projects/cassandra.yaml").model_dump()`
    against this repo's state immediately before this issue's changes (see
    tests/fixtures/cassandra_project_config_dump.json)."""

    def test_resolved_cassandra_config_matches_pre_169_fixture(self):
        fixture_path = Path(__file__).parent / "fixtures" / "cassandra_project_config_dump.json"
        before = json.loads(fixture_path.read_text())

        config = load_project("projects/cassandra.yaml")
        # Exclude exactly the new, inert-by-default fields issue #169 added
        # to the schema -- every field that existed before this issue must
        # be present with an unchanged value.
        after = config.model_dump(
            exclude={
                "issue_tracker": {"jql_filter"},
                # `ga_require_patch` (issue #170 PR #179 review fixup) is
                # the same kind of new, inert-by-default `releases:` field
                # as `tag_prefix`/`ga_major_only` above.
                "releases": {"tag_prefix", "ga_major_only", "ga_require_patch"},
                "github_issues": True,
                "shared_sources": True,
                # `releases_tab` (issue #175) is the same kind of new,
                # inert-by-default field as the ones above --
                # `projects/cassandra.yaml` deliberately never sets it (see
                # `test_new_fields_are_inert_defaults_for_cassandra` below).
                "releases_tab": True,
            }
        )

        assert after == before

    def test_new_fields_are_inert_defaults_for_cassandra(self):
        """The fields excluded above are new and optional -- confirm they
        really are untouched/default for Cassandra, rather than the
        equality check above accidentally masking a real difference."""
        config = load_project("projects/cassandra.yaml")

        assert config.issue_tracker.jql_filter is None
        assert config.releases.tag_prefix is None
        assert config.releases.ga_major_only is False
        assert config.releases.ga_require_patch is False
        assert config.github_issues is None
        assert config.shared_sources == []
        assert config.releases_tab is None
