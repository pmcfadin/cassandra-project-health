"""Baseline status + versioned composite health score (issue #57, D20).

See `scoring/engine.py` for the orchestration entry point
(`compute_scoring`), `scoring/config.py` for the `scoring.yaml` loader, and
`docs/spec/SCORING.md` §4-§5 and §12 for the underlying spec.
"""

from __future__ import annotations
