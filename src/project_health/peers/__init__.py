"""Peer context (issue #145, DECISIONS.md D30).

Same CHAOSS Starter-model-adjacent metrics computed for Cassandra (M0), run
again, unmodified, against a small, owner-chosen set of comparable ASF
projects -- apache/kafka, apache/spark, apache/flink, apache/pulsar,
apache/datafusion -- so the site's `/peers/` page can show Cassandra's own
line against each peer's, for context. No targets, no ranking (D30).

Submodules:

- `config.py` -- `projects/peers.yaml` loader (`PeersConfig`/`PeerProject`),
  deliberately not `project_health.config.ProjectConfig` (peers need none of
  that model's JIRA/mailing-list/roster shape).
- `git_clone.py` -- disk-safe peer git acquisition: a bare, blobless,
  shallow-since clone, never a full clone (issue #145's disk-budget
  constraint), plus best-effort cleanup.
- `github.py` -- a minimal duck-typed adapter that lets
  `collectors.github.GitHubCollector` (unmodified) collect every peer
  repo's PRs/reviews/comments in one shared-rate-limit-budget call.
- `release.py` -- GA-tag discovery reusing `collectors.release`'s own
  tag-pattern logic, plus each peer's independent-source release-count
  cross-check (JIRA versions or GitHub Releases/PyPI, per `peers.yaml`).
- `collect.py` -- per-peer raw-table collection, writing to the data
  branch's `raw/peers/<id>/...` namespace (Cassandra's own tables are never
  touched).
- `pipeline.py` -- top-level orchestration `.github/workflows/peers.yml`
  and the real-data run both call: collect every peer, compute the five
  metrics (`metrics.peer_metrics`), write snapshots.
"""

from __future__ import annotations
