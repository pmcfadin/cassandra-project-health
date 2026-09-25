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
from pydantic import BaseModel, ConfigDict


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
    mailing_lists: FlexibleSection | None = None
    roster: FlexibleSection | None = None
    releases: FlexibleSection | None = None
    affiliations_file: str | None = None
    bot_patterns: list[BotPattern] = []
    reviewer_extraction: ReviewerExtraction
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
