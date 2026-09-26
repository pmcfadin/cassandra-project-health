"""Phase 2a Jev pilot evaluation tooling (issue #47; DECISIONS.md D18, D22;
COMMUNITY-HEALTH.md §6).

D22 ("no LLM at runtime") applies throughout this package: every module here
is a plain, deterministic, offline-testable Python function. The *only*
model call anywhere under `project_health.pilot` is the pinned Jev
classifier invoked by `classify_runner.run_pilot_classify` (via the existing
`project_health.classify.classifier.JevClassifier`, issue #45) -- nothing in
`stats.py` or `evaluate.py` ever calls a model, acts as a rater, or breaks a
tie between raters. A tied/unsure human judgment is excluded from scoring,
never resolved by an LLM (D22: "No LLM acts as a rater, a tie-breaker or a
triage step").
"""

from __future__ import annotations
