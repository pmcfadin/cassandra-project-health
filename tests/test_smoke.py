"""Smoke tests: verify package imports."""

import project_health


def test_import():
    """Verify project_health package can be imported."""
    assert project_health is not None
