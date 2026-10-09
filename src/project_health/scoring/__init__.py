"""Baseline status + versioned composite health score (issue #57, D20) --
DORMANT as of issue #136 / DECISIONS.md D29 (2026-10-09, reverses D20 and
the published parts of D4).

D29: there is no widely accepted standard for a composite OSS health score
(LFX Insights, OSS Compass and OpenSSF Scorecard each use their own
editorial weights; CHAOSS deliberately groups metrics into models without
scoring one). The site stops publishing the composite score, dimension
scores and improving/declining/stable status labels, and leads instead
with the published CHAOSS "Starter Project Health" metrics model
(`site/metrics_meta.CHAOSS_STARTER_METRICS`).

This package's code (this module, `baseline.py`, `composite.py`,
`config.py`, `dimension.py`, `engine.py`, `registry.py`) is kept in the
repo, unmodified, rather than deleted -- D29 explicitly asks for that,
since the underlying self-baseline math (median/MAD, modified z-score,
"worst key metric" dimension rule) is reusable, documented work, not a
mistake. What changed is that nothing calls it any more:
`pipeline.run_pipeline` no longer calls `compute_scoring` or writes
`snapshots/<run_id>/{metric_baseline_status,dimension_status,
composite_score}.parquet`, and `site/generate.py` no longer calls
`site/scoring_page.py` or renders a composite section on the home page.
`scoring.yaml` and `docs/spec/SCORING.md` stay in the repo for the same
reason, with `SCORING.md` marked superseded-for-published-output at its
top. This package's own unit tests (`tests/test_scoring_*.py`) keep
exercising the math directly, as regression coverage for code that is
dormant, not deleted.

See `scoring/engine.py` for the (no-longer-called) orchestration entry
point (`compute_scoring`), `scoring/config.py` for the `scoring.yaml`
loader, and `docs/spec/SCORING.md` §4-§5 and §12 for the underlying spec.
"""

from __future__ import annotations
