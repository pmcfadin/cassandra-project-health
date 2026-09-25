"""Static site generator (ARCHITECTURE.md §7.5, §8; D8; task #8).

`project_health.site.generate.generate` is the package's single public
entry point: it reads a run's `metric_value` snapshot and manifest and
writes a fully static home page.
"""

from __future__ import annotations

from project_health.site.generate import generate

__all__ = ["generate"]
