"""Tests for project_health.peers.config (issue #145)."""

from __future__ import annotations

import pytest

from project_health.peers.config import (
    GithubReleasesVerification,
    JiraReleaseVerification,
    PypiReleaseVerification,
    load_peers,
)


@pytest.fixture
def peers_config():
    return load_peers("projects/peers.yaml")


def test_loads_five_peers(peers_config):
    ids = [peer.id for peer in peers_config.peers]
    assert ids == ["kafka", "spark", "flink", "pulsar", "datafusion"]


def test_peer_repo_owner_name(peers_config):
    kafka = peers_config.get("kafka")
    assert kafka.repo == "apache/kafka"
    assert kafka.owner == "apache"
    assert kafka.name == "kafka"


def test_jira_release_verification(peers_config):
    kafka = peers_config.get("kafka")
    assert isinstance(kafka.release_verification, JiraReleaseVerification)
    assert kafka.release_verification.project_key == "KAFKA"


def test_github_releases_verification(peers_config):
    pulsar = peers_config.get("pulsar")
    assert isinstance(pulsar.release_verification, GithubReleasesVerification)


def test_pypi_release_verification_for_datafusion(peers_config):
    """DataFusion publishes zero GitHub Releases (verified live, 2026-10-09,
    peers.yaml's own module comment) -- it uses a PyPI cross-check instead,
    not GitHub Releases as the issue's own text suggested."""
    datafusion = peers_config.get("datafusion")
    assert isinstance(datafusion.release_verification, PypiReleaseVerification)
    assert datafusion.release_verification.package == "datafusion"


def test_get_unknown_peer_raises(peers_config):
    with pytest.raises(KeyError):
        peers_config.get("nonexistent")


def test_collection_defaults(peers_config):
    assert peers_config.collection.commit_lookback_months == 48
    assert peers_config.collection.github_rate_limit_floor == 500
    assert len(peers_config.collection.bot_patterns) >= 1


def test_load_peers_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_peers(tmp_path / "nope.yaml")


def test_load_peers_rejects_unknown_verification_type(tmp_path):
    bad = tmp_path / "peers.yaml"
    bad.write_text(
        "peers:\n"
        "  - id: x\n"
        "    display_name: X\n"
        "    repo: a/b\n"
        "    default_branch: main\n"
        "    release_verification:\n"
        "      type: carrier_pigeon\n"
    )
    with pytest.raises(ValueError):
        load_peers(bad)
