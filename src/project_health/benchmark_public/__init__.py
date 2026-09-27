"""D23 public-dataset benchmark (`project-health benchmark-public`, issue #89).

DECISIONS.md D22, D23; COMMUNITY-HEALTH.md §1, §6. A fully scripted benchmark
(no LLM in the loop except the one pinned Jev classifier call, D22) that:

1. Downloads a pinned shortlist of public, human-labeled datasets
   (`registry.py`/`datasets_v1.yaml`), verifying each file's sha256 before use.
2. Draws a seeded, stratified sample from each dataset (`sampling.py`), sized
   per `docs/plans/2026-09-27-public-benchmark-datasets.md`'s shortlist item
   counts, oversampling positives so precision/recall are measurable.
3. Maps each dataset's own labels onto this project's 12 message-level labels
   through a versioned mapping file (`mapping.py`/`label_mapping_v1.yaml`),
   documenting each mapping's strength (`strong`/`partial`) and whether it
   counts toward the §6.4 gate (`gating`) or is informative-only.
4. Builds classifier state per item and runs the pinned Jev model
   (`runner.py`), cached by input hash exactly like the pilot (D22: "outputs
   are cached by input hash... re-rendering never calls the model again").
5. Computes per dataset x label threshold sweeps, precision/recall/F1 with
   bootstrap CIs, prevalence, and reliability bins (`evaluate.py`, reusing
   `project_health.pilot.stats`).
6. Renders an aggregate-only public report (`report.py`) -- no item text or
   ids, per COMMUNITY-HEALTH.md §7's "quotations: default none" and this
   project's existing pilot-report leak-test convention.

Dataset text is fetched into `--cache-dir` (never the repo, never the `data`
branch) at run time and is never redistributed; only aggregate results are
published, and each dataset is cited under its own license.
"""
