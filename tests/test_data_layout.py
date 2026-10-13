"""Tests for project_health.data_layout (issue #169, #176; epic #168)."""

import pytest

from pathlib import Path

from project_health.data_layout import (
    LegacyDataLayoutError,
    assert_cassandra_migrated,
    assert_peers_migrated,
    peers_data_dir,
    project_data_dir,
)


def test_cassandra_resolves_under_projects(tmp_path):
    """Issue #176: Cassandra now resolves to `projects/cassandra/`, the
    same `projects/<id>/` shape every other project already used."""
    assert project_data_dir(tmp_path, "cassandra") == tmp_path / "projects" / "cassandra"


def test_a_subproject_resolves_to_a_projects_subdirectory():
    root = Path("/data")
    assert project_data_dir(root, "java-driver") == root / "projects" / "java-driver"


def test_accepts_a_string_root():
    assert project_data_dir("/data", "gocql-driver") == Path("/data/projects/gocql-driver")


def test_distinct_subprojects_resolve_to_distinct_directories():
    root = Path("/data")
    assert project_data_dir(root, "ccm") != project_data_dir(root, "easy-stress")


def test_cassandra_and_other_subprojects_resolve_to_distinct_directories(tmp_path):
    assert project_data_dir(tmp_path, "cassandra") != project_data_dir(tmp_path, "java-driver")


def test_peers_data_dir_resolves_to_its_own_sibling_of_projects(tmp_path):
    assert peers_data_dir(tmp_path) == tmp_path / "peers"
    assert peers_data_dir(tmp_path) != project_data_dir(tmp_path, "cassandra")


# --- assert_cassandra_migrated / assert_peers_migrated (fail-fast guard) --


def test_assert_cassandra_migrated_fresh_root_is_fine(tmp_path):
    """Neither the legacy top-level dirs nor `projects/cassandra/` exist --
    a genuinely brand-new data branch, `nightly.yml`'s own first-run
    bootstrap, never a migration failure."""
    assert assert_cassandra_migrated(tmp_path) == tmp_path / "projects" / "cassandra"


def test_assert_cassandra_migrated_already_migrated_root_is_fine(tmp_path):
    (tmp_path / "projects" / "cassandra").mkdir(parents=True)
    assert assert_cassandra_migrated(tmp_path) == tmp_path / "projects" / "cassandra"


@pytest.mark.parametrize("legacy_dir", ["raw", "snapshots", "manifests", "state"])
def test_assert_cassandra_migrated_raises_on_unmigrated_legacy_data(tmp_path, legacy_dir):
    (tmp_path / legacy_dir).mkdir()
    with pytest.raises(LegacyDataLayoutError, match="projects/cassandra"):
        assert_cassandra_migrated(tmp_path)


def test_assert_cassandra_migrated_tolerates_legacy_data_once_migrated(tmp_path):
    """A migrated root may still have legacy dirs lying around in a
    pathological case (this project's own migration script doesn't leave
    any, but the guard itself only cares that `projects/cassandra/`
    exists) -- never a false failure once the target directory is there."""
    (tmp_path / "raw").mkdir()
    (tmp_path / "projects" / "cassandra").mkdir(parents=True)
    assert assert_cassandra_migrated(tmp_path) == tmp_path / "projects" / "cassandra"


def test_assert_peers_migrated_fresh_root_is_fine(tmp_path):
    assert assert_peers_migrated(tmp_path) == tmp_path / "peers"


def test_assert_peers_migrated_already_migrated_root_is_fine(tmp_path):
    (tmp_path / "peers").mkdir()
    assert assert_peers_migrated(tmp_path) == tmp_path / "peers"


@pytest.mark.parametrize(
    "legacy_path",
    ["raw/peers", "snapshots/peers", "state/peers"],
)
def test_assert_peers_migrated_raises_on_unmigrated_legacy_data(tmp_path, legacy_path):
    (tmp_path / legacy_path).mkdir(parents=True)
    with pytest.raises(LegacyDataLayoutError, match="peers/"):
        assert_peers_migrated(tmp_path)


def test_assert_peers_migrated_raises_on_legacy_manifest_file(tmp_path):
    (tmp_path / "manifests").mkdir()
    (tmp_path / "manifests" / "peers-peers-20260101T000000Z.json").write_text("{}")
    with pytest.raises(LegacyDataLayoutError):
        assert_peers_migrated(tmp_path)


def test_assert_peers_migrated_does_not_false_positive_on_cassandra_only_legacy_data(tmp_path):
    """A pre-#176 root whose peer-context collection never ran at all (no
    `raw/peers`, `snapshots/peers`, `state/peers`, or `manifests/peers-*.json`)
    has no peers-specific legacy marker -- `assert_peers_migrated` must not
    raise just because Cassandra's OWN legacy data (`raw/git/...`) is
    present; that's `assert_cassandra_migrated`'s concern, not this one's."""
    (tmp_path / "raw" / "git").mkdir(parents=True)
    assert assert_peers_migrated(tmp_path) == tmp_path / "peers"
