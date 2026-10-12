"""Seed a subproject's GitHub PR/review/comment data from rows already
collected under another project's data dir (issue #172, epic #168).

Context: Cassandra's own `projects/cassandra.yaml` configures
`pull_requests.repos` with several *peer* repos too (`apache/
cassandra-java-driver`, `apache/cassandra-sidecar`, `apache/
cassandra-analytics`, `apache/cassandra-accord`) -- `collectors/github.py`
has therefore already fetched and written PR/review/comment rows for those
repos under the root data dir's `raw/github/{pr,pr_review,pr_comment}`
tables, with per-repo GraphQL cursors folded into the root `state/
watermarks.json`'s `"github"` key (`storage._watermark_key`; `pipeline.
_collect_github`'s per-repo cursor blob).

No `GH_PAT` is configured for this repo (issue #172) -- the nightly
`GITHUB_TOKEN` budget is shared across Cassandra's own run and every
enabled subproject's run, so a brand-new subproject re-fetching rows the
root project already has (and already paid the GraphQL cost for) would
waste that shared budget on day one. This module copies those rows --
filtered to the one subproject's own `pull_requests.repos` -- straight
into that subproject's own data dir, plus the matching per-repo cursor, so
its first real `project-health run` resumes from the copied cursor instead
of re-walking PR history from scratch (`collectors/github.py`'s "Watermark
strategy" module docstring: the collector reads `watermarks.get(repo_label)`
and passes it straight through as the GraphQL `after:` cursor).

Idempotent and safe to re-run: a destination that already has a `"github"`
watermark (either from a previous seed, or from a real collection run that
has since progressed) is left untouched -- seeding must never regress a
cursor a real run has already advanced past.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pyarrow.compute as pc

from project_health import storage
from project_health.config import ProjectConfig

# The three GitHub PR-family tables `pipeline._collect_github` writes
# (schema/tables.py's PR, PR_REVIEW, PR_COMMENT) -- all keyed by a `repo`
# column holding `"owner/name"`, so filtering by repo is a plain column
# equality mask on each, independently.
GITHUB_PR_TABLES = ("pr", "pr_review", "pr_comment")

# Fixed, deterministic partition coordinates for every seed write -- never
# wall-clock-derived -- so re-running this command against a destination
# that was *already* seeded hits the idempotency guard below rather than
# writing a second, duplicate partition under a fresh run_id.
_SEED_RUN_ID = "seed-from-root"
_SEED_PARTITION_DATE = "2000-01-01"


@dataclass(frozen=True)
class SeedRepoResult:
    repo: str
    rows_copied: dict[str, int]


@dataclass
class SeedReport:
    project_id: str
    # 'seeded' -- rows (if any existed for this project's repos) and the
    # matching cursor were copied; 'skipped_already_present' -- the
    # destination already had a `github` watermark (prior seed, or a real
    # run already in progress), nothing was touched.
    status: str
    repos: list[SeedRepoResult] = field(default_factory=list)

    def to_json_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "status": self.status,
            "repos": [
                {"repo": r.repo, "rows_copied": r.rows_copied} for r in self.repos
            ],
        }


def seed_subproject(
    root_data_dir: str | Path,
    project_config: ProjectConfig,
    dst_data_dir: str | Path,
) -> SeedReport:
    """Copy `project_config.pull_requests.repos`' PR/review/comment rows
    (and matching per-repo GraphQL cursor) from `root_data_dir` into
    `dst_data_dir`.

    `root_data_dir` is normally the data branch's own root (where Cassandra's
    `pull_requests.repos` already covers the peer repos this subproject
    cares about); `dst_data_dir` is this subproject's own data dir
    (`data_layout.project_data_dir`'s resolved per-project root).

    A no-op (`status='skipped_already_present'`) if `dst_data_dir` already
    has a `github` watermark -- see module docstring.
    """
    project_id = project_config.project.id
    dst_data_dir = Path(dst_data_dir)

    if storage.read_watermark(dst_data_dir, "github") is not None:
        return SeedReport(project_id=project_id, status="skipped_already_present")

    repos = list(getattr(project_config.pull_requests, "repos", None) or [])

    raw_root_watermark = storage.read_watermark(root_data_dir, "github")
    root_cursors: dict[str, str | None] = (
        json.loads(raw_root_watermark) if raw_root_watermark else {}
    )

    repo_results: list[SeedRepoResult] = []
    seeded_cursors: dict[str, str | None] = {}
    for repo in repos:
        rows_copied: dict[str, int] = {}
        for table in GITHUB_PR_TABLES:
            source_table = storage.read_table(root_data_dir, "github", table)
            filtered = source_table.filter(pc.equal(source_table.column("repo"), repo))
            rows_copied[table] = filtered.num_rows
            if filtered.num_rows:
                storage.write_partition(
                    dst_data_dir,
                    "github",
                    table,
                    _SEED_PARTITION_DATE,
                    _SEED_RUN_ID,
                    filtered,
                )
        if repo in root_cursors:
            seeded_cursors[repo] = root_cursors[repo]
        repo_results.append(SeedRepoResult(repo=repo, rows_copied=rows_copied))

    if seeded_cursors:
        storage.write_watermark(dst_data_dir, "github", json.dumps(seeded_cursors, sort_keys=True))

    return SeedReport(project_id=project_id, status="seeded", repos=repo_results)
