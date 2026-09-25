"""`contribution_event.event_type` values (METRICS.md §0.3, issue #24).

Single source of truth for the string(s) a `contribution_event` row's
`event_type` column may hold, so a collector that writes one spelling and an
engine that filters on another can never drift apart again (issue #24: the
git collector wrote `'code_commit'` while the metrics engine and the test
fixture builder both filtered/defaulted on `'commit'`, silently zeroing out
`active_contributors_monthly` and `new_contributors_monthly` on real data).

`CODE_COMMIT` is the only value any M0 collector emits today
(`collectors/git.py`). METRICS.md §0.3 also names `jira_issue_authored`,
`github_pr`, `mailing_list_post` and `release` as activity types this
project's Phase 1 scope eventually instruments as `contribution_event` rows;
they're omitted from `CONTRIBUTION_EVENT_TYPES` until a collector actually
produces them, so this tuple always reflects what the pipeline can really
emit, not the full long-term catalog (`code_review` and `jira_comment` are
not `contribution_event` types at all -- they live in `review_event` and
`issue` respectively).
"""

from __future__ import annotations

CODE_COMMIT = "code_commit"

CONTRIBUTION_EVENT_TYPES: tuple[str, ...] = (CODE_COMMIT,)
