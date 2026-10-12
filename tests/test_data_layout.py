"""Tests for project_health.data_layout (issue #169, epic #168)."""

from pathlib import Path

from project_health.data_layout import project_data_dir


def test_cassandra_resolves_to_the_root_unchanged():
    """Cassandra is the default project and keeps today's production `data`
    branch layout -- rooted directly at `root`, no subdirectory."""
    root = Path("/data")
    assert project_data_dir(root, "cassandra") == root


def test_a_subproject_resolves_to_a_projects_subdirectory():
    root = Path("/data")
    assert project_data_dir(root, "java-driver") == root / "projects" / "java-driver"


def test_accepts_a_string_root():
    assert project_data_dir("/data", "gocql-driver") == Path("/data/projects/gocql-driver")


def test_distinct_subprojects_resolve_to_distinct_directories():
    root = Path("/data")
    assert project_data_dir(root, "ccm") != project_data_dir(root, "easy-stress")
