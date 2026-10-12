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


class IssueTrackerConfig(FlexibleSection):
    """`issue_tracker:` block. Adds `jql_filter` (issue #169, multi-project
    registry): a subproject that shares a JIRA project with another one
    (rather than having its own `project_key`) scopes to just its own
    issues with a JQL clause -- e.g. `projects/accord.yaml` and
    `projects/ccm.yaml` both set `project_key: CASSANDRA` with
    `jql_filter: component = Accord` / `component = "Tool/CCM"`
    respectively, verified live against the two components' issue counts
    in ASF JIRA. `None` (the default, Cassandra's own config) means no
    extra JQL clause -- `project = <project_key>` alone, today's behavior.
    Wiring `jql_filter` into the JIRA collector's actual query is left to
    whichever issue builds that collector call for a multi-tracker project;
    this issue only adds the field so it round-trips through config load.
    """

    jql_filter: str | None = None


class GitHubIssuesConfig(BaseModel):
    """`github_issues:` block (issue #169) -- a project's GitHub-native
    Issues tracker, independent of (and possibly alongside) `issue_tracker:`
    (JIRA). Some subprojects (e.g. easy-stress) use GitHub Issues as their
    ONLY tracker, with no `issue_tracker:` section at all; others (gocql,
    ccm -- both verified live with `has_issues: true`) have GitHub Issues
    enabled ALONGSIDE a JIRA `issue_tracker:`, so this is its own optional
    section rather than a variant of `issue_tracker.type`.
    """

    model_config = ConfigDict(extra="allow")

    repos: list[str] = []


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
    # issue #35: reviewed allow/deny list of automated dev@/user@ sender
    # addresses (JIRA-to-list bridge, GitHub notifications, CI, ASF INFRA
    # automation) -- Python `re` patterns matched against a message's
    # lowercased `sender_raw_value` (schema/tables.py `MESSAGE`).
    # `time_to_first_reply_devlist`/`unanswered_thread_rate_devlist` and
    # their issue #158 user@ companions, `time_to_first_reply_userlist`/
    # `unanswered_thread_rate_userlist` (METRICS.md §4), exclude any message
    # from a matching sender as a candidate "reply", and exclude a whole
    # thread from either metric's population when its root message's sender
    # matches (METRICS.md `unanswered_thread_rate_devlist` "Population and
    # exclusions": "excludes threads that are themselves auto-generated").
    # The issue #158 mailing-list *volume* metrics (`mailing_list_messages_
    # monthly_*`/`_new_threads_monthly_*`/`_distinct_senders_monthly_*`)
    # deliberately do NOT apply this exclusion -- they count raw activity
    # volume, not the reply-responsiveness population. Additive default
    # `[]` so a project config without this key still loads.
    automated_senders: list[str] = []


class BotPattern(BaseModel):
    """One entry in `bot_patterns:` — applied by identity resolution."""

    model_config = ConfigDict(extra="allow")

    field: str
    regex: str


class AutomatedSenderPattern(BaseModel):
    """One entry in `automated_senders:` (issue #43; shared with issue #35's
    dev@ automated-sender work — append new patterns here rather than
    starting a second list).

    Distinct from `bot_patterns` above: `bot_patterns` is applied by Phase 1
    identity resolution to *contributor identities* (git author email,
    GitHub login, JIRA username) so bot commits/reviews don't get counted as
    a person's activity. `automated_senders` is applied by
    `classify/preprocess.py` to the *sender string on a single message*
    (a mailing-list `From:` address or a JIRA comment's author username)
    before that message is fetched/classified at all (COMMUNITY-HEALTH.md
    §4.1: automated/notification traffic — JIRA/GitHub/CI bots, commit
    notification bots — is not a human communication to classify).
    """

    model_config = ConfigDict(extra="allow")

    regex: str
    note: str | None = None


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


class ReleasesConfig(FlexibleSection):
    """`releases:` block (issue #135's `FlexibleSection`, now typed for the
    multi-project registry, issue #169). Adds three optional fields on top
    of the existing `archive_url` (`collectors/release.py` already reads
    that one directly; typing it here changes nothing about who reads it):

    - `tag_prefix`: the literal prefix a repo's own GA git tags carry
      before the version number (Cassandra: `cassandra-5.0.0`). `None`
      (the default) means "use `effective_tag_prefix`'s fallback" --
      today's pipeline.py-hard-coded `f"{repo_cfg.name}-"` guess (~line
      3188, unchanged by this issue; wiring this field in is issue #170).
      An explicit `""` means the repo's tags carry NO prefix at all --
      verified live (2026-10-12) for apache/cassandra-java-driver
      (`4.19.4`), -python-driver (`3.30.1`), -cpp-driver (`2.17.1`), and
      apache/cassandra-accord/-ccm (neither has a systematic version-tag
      scheme at all).
    - `archive_url`: unchanged in meaning, just now an explicit field
      instead of falling out of `FlexibleSection`'s `extra="allow"`.
      Verified live (2026-10-12): 6 of the 10 subprojects DO publish their
      own `archive.apache.org/dist/cassandra/cassandra-<name>/` directory
      (java/gocql/python/nodejs-driver, sidecar, analytics) and get that
      URL here; cpp-driver/accord/easy-stress/ccm have no such directory at
      all (confirmed with a live `404` on each guessed path) and get
      `None`. `None` here SKIPS the archive cross-check entirely (issue
      #170 -- `ReleaseCollector.__init__` no longer falls back to
      `DEFAULT_ARCHIVE_URL` when this field is `None`; that fallback was
      Cassandra's own archive listing, which is wrong for a project with no
      archive directory of its own). This field only
      records the fact of which projects have their own archive directory
      today.
    - `ga_major_only`: a repo whose GA tags are bare major versions (e.g.
      apache/cassandra-easy-stress's `v9`/`v8`/`v7`, verified live) rather
      than full `major.minor.patch` semver.
    """

    tag_prefix: str | None = None
    archive_url: str | None = None
    ga_major_only: bool = False

    def effective_tag_prefix(self, repo_name: str) -> str:
        """Resolve the tag prefix to cross-check this repo's GA tags
        against. An explicit `tag_prefix` (including `""`, a deliberately
        empty prefix) always wins; otherwise reproduces pipeline.py's
        current hard-coded `f"{repo_cfg.name}-"` default, so wiring this
        resolver into pipeline.py (issue #170) is a behavior-preserving
        swap for Cassandra, not a behavior change.
        """
        if self.tag_prefix is not None:
            return self.tag_prefix
        return f"{repo_name}-"


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
    issue_tracker: IssueTrackerConfig | None = None
    # `github_issues:` (issue #169) — a project's GitHub-native Issues
    # tracker, independent of `issue_tracker:` (JIRA): some subprojects
    # (easy-stress) use it as their only tracker, others (gocql, ccm) have
    # it enabled alongside a JIRA `issue_tracker:`. See `GitHubIssuesConfig`.
    github_issues: GitHubIssuesConfig | None = None
    pull_requests: FlexibleSection | None = None
    mailing_lists: MailingListsConfig | None = None
    roster: FlexibleSection | None = None
    # `releases:` (issue #135, release cadence dimension; typed in issue
    # #169, see `ReleasesConfig`). GA tags come from the repo's own local
    # clone (`repos[0]`), not from this section.
    releases: ReleasesConfig | None = None
    # `shared_sources:` (issue #169) — names PMC-wide data sources (e.g.
    # `asf_roster`, `ponymail`, `governance`) this project's own metrics do
    # NOT independently collect/attribute; epic #168's site plan shows
    # these as a labelled "shared — see Apache Cassandra" notice on a
    # subproject's tabs instead of per-subproject numbers. Purely
    # descriptive metadata at this layer -- it doesn't change what this
    # project's own config sections above cause to be collected; wiring the
    # site notice is a later issue in the epic.
    shared_sources: list[str] = []
    # `security:` (issue #55, D21 item 3) — OpenSSF Scorecard + CVE/advisory
    # collector endpoints; a `FlexibleSection` like `roster`, since its full
    # shape is owned by `collectors/security.py`, not core.
    security: FlexibleSection | None = None
    # `release_lines:` (issue #166, Releases tab) — Butler/Jenkins CI
    # endpoints for the per-branch release-line model; a `FlexibleSection`
    # like `security`/`releases` above, since its shape is owned by
    # `collectors/release_lines.py`. JIRA versions/issues reuse
    # `issue_tracker.base_url`/`.project_key`, not a field here.
    release_lines: FlexibleSection | None = None
    affiliations_file: str | None = None
    # D6, issue #52: a reviewed email-domain -> organization map (see
    # normalize/affiliation.py). Optional -- a project with no reviewed
    # domain map yet just resolves every commit to "unknown" for the
    # organizational-diversity metrics rather than failing to load.
    org_domains_file: str | None = None
    # D6, issue #52 fixup cycle 1: a reviewed GitHub-profile `company`-field
    # alias map (see normalize/affiliation.py). Optional -- without one, the
    # github_company affiliation source never produces a row (every company
    # string is "unmatched", per D6's "never fuzzy-matched").
    org_aliases_file: str | None = None
    # D6, issue #52 fixup cycle 2: how many months before a GitHub profile's
    # `company` field was fetched that field's organization is trusted to
    # cover (see normalize/affiliation.py's
    # DEFAULT_GITHUB_COMPANY_LOOKBACK_MONTHS). Optional -- `None` uses that
    # default (24).
    github_company_lookback_months: int | None = None
    bot_patterns: list[BotPattern] = []
    # Phase 2a (issue #43, D18): automated dev@/JIRA senders to exclude before
    # fetching/classifying a message. See `AutomatedSenderPattern` docstring.
    automated_senders: list[AutomatedSenderPattern] = []
    # Issue #169: optional (was required) -- a brand-new subproject YAML
    # whose commit convention and/or JIRA custom-field IDs haven't been
    # verified yet can load (and run every other dimension) with this
    # section entirely absent, rather than being blocked from loading at
    # all until a reviewer-extraction source is confirmed. Cassandra's own
    # config always supplies it, so this widening doesn't change Cassandra's
    # resolved config (ARCHITECTURE.md §3.1 cross-check still applies
    # whenever the section is present).
    reviewer_extraction: ReviewerExtraction | None = None
    truck_factor: TruckFactorConfig = Field(default_factory=TruckFactorConfig)
    baseline_window: BaselineWindow | None = None
    slack: SlackConfig | None = None


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    """Load one YAML file's top-level content as a plain `dict`.

    Raises ``FileNotFoundError`` if `path` doesn't exist. An empty file
    loads as `{}` (a `_shared.yaml` with nothing in it yet is valid).
    """
    if not path.is_file():
        raise FileNotFoundError(f"project config not found: {path}")
    raw: Any = yaml.safe_load(path.read_text())
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"project config must be a YAML mapping, got {type(raw).__name__}: {path}")
    return raw


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge `override` onto `base`; `override` wins conflicts.

    Used by `load_project`'s `extends:` support (issue #169): a project's
    own YAML (`override`) is merged onto its shared base (`base`, e.g.
    `projects/_shared.yaml`). Dict values present in both are merged
    key-by-key, recursively -- this is what lets `cassandra.yaml` override
    just `reviewer_extraction.commit_trailer` while still inheriting
    `_shared.yaml`'s `reviewer_extraction.jira_fields`. Any other value
    type (list, scalar) in `override` replaces `base`'s value outright --
    lists are never concatenated or deduped; a project that wants to adjust
    a shared list (e.g. `bot_patterns`) repeats the whole list in its own
    YAML rather than appending to the shared one.
    """
    merged = dict(base)
    for key, override_value in override.items():
        base_value = merged.get(key)
        if isinstance(base_value, dict) and isinstance(override_value, dict):
            merged[key] = _deep_merge(base_value, override_value)
        else:
            merged[key] = override_value
    return merged


def load_project(path: str | Path) -> ProjectConfig:
    """Load and validate a `projects/<id>.yaml` file into a `ProjectConfig`.

    Supports an `extends:` top-level key (issue #169, multi-project
    registry; list form added in fixup round 1 so a project can layer more
    than one shared base, e.g. `projects/_identity.yaml` (bot/affiliation
    settings every project needs) plus `projects/_shared.yaml` (PMC-wide
    mailing_lists/roster/reviewer_extraction.jira_fields, meaningful only
    for the main `apache/cassandra` repo today)). Its value is either one
    filename or a list of filenames, each resolved relative to `path`'s own
    directory (e.g. `projects/cassandra.yaml`'s
    `extends: [_identity.yaml, _shared.yaml]` resolves to
    `projects/_identity.yaml` and `projects/_shared.yaml`). Each target is
    loaded as a plain mapping and the targets are deep-merged (`_deep_merge`)
    into each other in list order, then that combined base is deep-merged
    underneath this file's own content -- which always wins on any key any
    of them define. `extends:` itself is stripped before validation -- it
    is a load-time instruction, never a `ProjectConfig` field. Each target
    is loaded once, non-recursively (a target may not itself `extends:`
    another file).

    Raises ``FileNotFoundError`` if `path` (or any `extends:` target)
    doesn't exist, and ``pydantic.ValidationError`` if the required fields
    (project, etc.) are missing or malformed. Unknown/future sections are
    accepted, not rejected.
    """
    path = Path(path)
    raw = _load_yaml_mapping(path)

    extends = raw.pop("extends", None)
    if extends:
        targets = [extends] if isinstance(extends, str) else list(extends)
        merged_base: dict[str, Any] = {}
        for target in targets:
            base_path = path.parent / target
            try:
                base_raw = _load_yaml_mapping(base_path)
            except FileNotFoundError:
                raise FileNotFoundError(
                    f"extends target not found: {base_path} (from {path})"
                ) from None
            base_raw.pop("extends", None)  # a shared base may not itself chain to another extends
            merged_base = _deep_merge(merged_base, base_raw)
        raw = _deep_merge(merged_base, raw)

    return ProjectConfig.model_validate(raw)


class ProjectIndexEntry(BaseModel):
    """One entry in `projects/index.yaml` — issue #169's project registry.

    `config` is a filename resolved relative to the index file's own
    directory (mirroring `load_project`'s `extends:` resolution), not a
    full path -- so the registry stays portable if `projects/` itself ever
    moves. `enabled` gates whether a project appears in site-facing listings
    (e.g. `project-health list-projects`, and later the epic #168 header
    switcher); `load_project` itself doesn't consult it -- a disabled
    entry's config still loads validly, it's just not surfaced yet.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    config: str
    enabled: bool = True


def load_project_index(path: str | Path) -> list[ProjectIndexEntry]:
    """Load `projects/index.yaml`'s ordered list of project entries.

    Raises ``FileNotFoundError`` if `path` doesn't exist, and
    ``pydantic.ValidationError`` if an entry is missing `id`/`config`.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"project index not found: {path}")
    raw: Any = yaml.safe_load(path.read_text())
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ValueError(f"project index must be a YAML list: {path}")
    return [ProjectIndexEntry.model_validate(entry) for entry in raw]


def resolve_index_config_path(index_path: str | Path, entry: ProjectIndexEntry) -> Path:
    """Resolve one index entry's `config` filename to a full path.

    Relative to `index_path`'s own directory, matching `load_project`'s
    `extends:` resolution convention.
    """
    return Path(index_path).parent / entry.config
