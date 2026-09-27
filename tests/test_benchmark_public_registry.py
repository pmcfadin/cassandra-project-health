"""Tests for project_health.benchmark_public.registry (issue #89).

Includes validation tests against small synthetic YAML fixtures, plus a
load of the real `datasets_v1.yaml` shipped with the package -- proving that
file parses and validates cleanly (no network access; loading the registry
never downloads anything).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from project_health.benchmark_public.registry import RegistryError, load_registry

VALID_SHA256 = "a" * 64


def _write_registry(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "datasets.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _working_dataset(**overrides) -> dict:
    base = {
        "id": "ds1",
        "name": "Dataset One",
        "citation": "Author, Title, Year.",
        "license": "CC0",
        "status": "working",
        "files": [
            {"url": "https://example.org/d.csv", "sha256": VALID_SHA256, "filename": "d.csv"}
        ],
        "loader": "load_ds1",
        "source_venue": "mailing_list",
        "source_venue_rationale": "it's a mailing list",
        "target_n": 100,
        "seed": 1,
    }
    base.update(overrides)
    return base


def test_load_real_registry_parses_and_validates() -> None:
    registry = load_registry()
    assert registry.version == 1
    assert len(registry.working()) >= 1
    for spec in registry.working():
        assert spec.source_venue in {"mailing_list", "jira_comment", "github_pr_comment"}
        assert spec.files
        for f in spec.files:
            assert len(f.sha256) == 64


def test_blocked_dataset_needs_no_files_or_loader(tmp_path: Path) -> None:
    path = _write_registry(
        tmp_path,
        {
            "version": 1,
            "datasets": [
                {
                    "id": "blocked1",
                    "name": "Blocked One",
                    "citation": "Author, Year.",
                    "license": "CC BY 4.0",
                    "status": "blocked",
                    "blocked_reason": "dead link, checked live",
                }
            ],
        },
    )
    registry = load_registry(path)
    spec = registry.datasets["blocked1"]
    assert spec.status == "blocked"
    assert spec.files == ()
    assert spec.loader is None
    assert registry.blocked() == [spec]
    assert registry.working() == []


def test_working_dataset_requires_files(tmp_path: Path) -> None:
    entry = _working_dataset()
    del entry["files"]
    path = _write_registry(tmp_path, {"version": 1, "datasets": [entry]})
    with pytest.raises(RegistryError, match="files"):
        load_registry(path)


def test_rejects_bad_sha256(tmp_path: Path) -> None:
    entry = _working_dataset()
    entry["files"][0]["sha256"] = "not-hex"
    path = _write_registry(tmp_path, {"version": 1, "datasets": [entry]})
    with pytest.raises(RegistryError, match="sha256"):
        load_registry(path)


def test_rejects_invalid_source_venue(tmp_path: Path) -> None:
    entry = _working_dataset(source_venue="slack")
    path = _write_registry(tmp_path, {"version": 1, "datasets": [entry]})
    with pytest.raises(RegistryError, match="source_venue"):
        load_registry(path)


def test_rejects_duplicate_ids(tmp_path: Path) -> None:
    path = _write_registry(
        tmp_path, {"version": 1, "datasets": [_working_dataset(), _working_dataset()]}
    )
    with pytest.raises(RegistryError, match="duplicate"):
        load_registry(path)


def test_rejects_unknown_status(tmp_path: Path) -> None:
    entry = _working_dataset(status="maybe")
    path = _write_registry(tmp_path, {"version": 1, "datasets": [entry]})
    with pytest.raises(RegistryError, match="status"):
        load_registry(path)
