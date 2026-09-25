"""Tests for project_health.config (ARCHITECTURE.md §2.1)."""

import pytest
import yaml

from project_health.config import load_project


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


def test_bot_patterns_and_baseline_window():
    config = load_project("projects/cassandra.yaml")

    assert config.baseline_window.trailing_months == 24
    assert config.baseline_window.min_completed_months == 12

    fields = {pattern.field for pattern in config.bot_patterns}
    assert "git_author_email" in fields
    assert "github_login" in fields
    assert "jira_username" in fields
