"""Project configuration loader.

Loads ``projects/<project_id>.yaml`` files into typed models, per
``docs/spec/ARCHITECTURE.md`` §2.1. Core code never reads a project YAML file
directly (§2.2) — everything goes through :func:`load_project`.

Sections whose full shape isn't pinned down yet (or that a future project's
config might add) are modeled permissively (``extra="allow"``) so that
unknown or future keys never cause a load failure — only the fields this
package actually depends on (repo list, reviewer extraction, bot patterns,
baseline window) are strictly typed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class ProjectInfo(BaseModel):
    """`project:` block."""

    model_config = ConfigDict(extra="allow")

    id: str
    display_name: str
    homepage: str | None = None


class Repo(BaseModel):
    """One entry in the `repos:` list."""

    model_config = ConfigDict(extra="allow")

    owner: str
    name: str
    default_branch: str


class FlexibleSection(BaseModel):
    """A section whose full shape is owned by an adapter, not core.

    Core only needs to know the section exists and pass it through; adapter
    code (or a later, more specific model) reads whatever fields it needs.
    Allows arbitrary extra keys so new adapters/config fields never break
    loading.
    """

    model_config = ConfigDict(extra="allow")

    type: str | None = None


class MailingListsConfig(BaseModel):
    """`mailing_lists:` block — selects `collectors/ponymail.py`
    (ARCHITECTURE.md §2.1 `MailingListAdapter`).

    Typed (not a bare `FlexibleSection`) because `collectors.ponymail.
    PonyMailCollector` reads `domain` and `lists` directly, the same way
    `JiraCollector` reads `issue_tracker.base_url`/`.project_key` off
    `FlexibleSection`'s duck-typed attributes today -- pinning the shape here
    catches a missing/misspelled key at config-load time instead of a
    collector-construction `AttributeError`.
    """

    model_config = ConfigDict(extra="allow")

    type: str
    domain: str
    lists: list[str]
    # Phase 1 (D1/D16): metadata only, no message body is ever collected.
    # Always true today; kept as a field (rather than hard-coded) so a
    # future phase 2 flip is a config change this model already accepts.
    metadata_only: bool = True
    # Per-list, per-run cap on how many months `collectors/ponymail.py`
    # fetches (issue #33 fixup: the full dev@ backfill took ~31 minutes at
    # the ≤2 req/s pacing cap; with `user@` likely similar-or-larger and the
    # nightly job's `timeout-minutes: 60` also covering git/JIRA/roster,
    # an uncapped first-run backfill would blow the nightly's time budget).
    # 36 months/list/run is ~5 minutes at the pacing cap. Used only when the
    # CLI's `--max-ponymail-months` isn't given; a nightly run backfills
    # oldest-first, a bounded number of months per run, until caught up --
    # see `pipeline._collect_ponymail` and `collectors.ponymail.
    # select_backfill_months`.
    max_months_per_run: int = 36


class BotPattern(BaseModel):
    """One entry in `bot_patterns:` — applied by identity resolution."""

    model_config = ConfigDict(extra="allow")

    field: str
    regex: str


class CommitTrailerExtraction(BaseModel):
    """`reviewer_extraction.commit_trailer` — see ARCHITECTURE.md §3.1."""

    model_config = ConfigDict(extra="allow")

    type: str
    pattern: str
    exclude_merge_commits: bool = False


class JiraFieldsExtraction(BaseModel):
    """`reviewer_extraction.jira_fields` — see ARCHITECTURE.md §3.1."""

    model_config = ConfigDict(extra="allow")

    type: str
    reviewers_field: str | None = None
    reviewer_field: str | None = None


class ReviewerExtraction(BaseModel):
    """`reviewer_extraction:` block — two independent, cross-checked sources."""

    model_config = ConfigDict(extra="allow")

    commit_trailer: CommitTrailerExtraction
    jira_fields: JiraFieldsExtraction
    reliable_from: str | None = None


class TruckFactorConfig(BaseModel):
    """`truck_factor:` block (issue #53) — `truck_factor` metric's file-level
    collection knobs, distinct from `bot_patterns` (which excludes *people*).

    `excluded_path_globs` excludes generated/vendored *paths* from
    `file_change_event` collection (METRICS.md `truck_factor` "Population &
    exclusions"): a mechanically regenerated or vendored file inflates
    whichever committer happened to run the generator/vendoring step into
    looking like a file "author," which is not the knowledge-concentration
    risk this metric is trying to measure. Patterns are matched with
    `fnmatch` (shell-glob-style, `*` matches across `/` too) against the
    file's repo-relative path as `git log --name-status` reports it.
    """

    model_config = ConfigDict(extra="allow")

    excluded_path_globs: list[str] = []


class BaselineWindow(BaseModel):
    """`baseline_window:` block — SCORING.md §4.1/§5.1."""

    model_config = ConfigDict(extra="allow")

    trailing_months: int
    min_completed_months: int


class SlackConfig(BaseModel):
    """`slack:` block — phase 2b only, absent/null/disabled until gated (D1)."""

    model_config = ConfigDict(extra="allow")

    enabled: bool = False
    workspace: str | None = None
    channels: list[str] = []


class ProjectConfig(BaseModel):
    """Root config for a project, loaded from `projects/<id>.yaml`.

    ``extra="allow"`` at every level means unknown or future top-level
    sections (and unknown keys within a modeled section) load without error,
    per the issue's requirement that mailing_lists/roster/slack/etc. never
    break a load even before/beyond what this model pins down.
    """

    model_config = ConfigDict(extra="allow")

    project: ProjectInfo
    repos: list[Repo] = []
    issue_tracker: FlexibleSection | None = None
    pull_requests: FlexibleSection | None = None
    mailing_lists: MailingListsConfig | None = None
    roster: FlexibleSection | None = None
    releases: FlexibleSection | None = None
    affiliations_file: str | None = None
    bot_patterns: list[BotPattern] = []
    reviewer_extraction: ReviewerExtraction
    truck_factor: TruckFactorConfig = Field(default_factory=TruckFactorConfig)
    baseline_window: BaselineWindow | None = None
    slack: SlackConfig | None = None


def load_project(path: str | Path) -> ProjectConfig:
    """Load and validate a `projects/<id>.yaml` file into a `ProjectConfig`.

    Raises ``FileNotFoundError`` if `path` doesn't exist, and
    ``pydantic.ValidationError`` if the required fields (project, reviewer
    extraction, etc.) are missing or malformed. Unknown/future sections are
    accepted, not rejected.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"project config not found: {path}")

    raw: Any = yaml.safe_load(path.read_text())
    if raw is None:
        raw = {}
    return ProjectConfig.model_validate(raw)
