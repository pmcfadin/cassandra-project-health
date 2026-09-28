"""Typed loader and validation for `governance-policy.yaml` (issue #36; v2, D24, issue #93).

`governance-policy.yaml` (repo root) is the binding, owner-approved compliance
policy (D14, D15, D24; `docs/spec/GOVERNANCE.md`). This module is the *only*
place that parses that file's raw YAML shape into typed objects — the rest of
the governance engine (`checks.py`, `engine.py`) works against `Policy`/`Rule`,
never the raw dict.

Per D14, "each commit is judged against the policy in force on its commit
date. A policy change is a dated version bump and never rescores history
silently." This module models that as two independent facts a caller checks
per (rule, commit):

- `Rule.in_force_on(commit_date)` — is `commit_date` on/after the rule's own
  `effective_from`? (`None` means "no dated source; always in force".)
- `Rule.applies_to_branch(branch)` — does the rule's `applies_to.branches`
  glob list (fnmatch patterns, e.g. `"cassandra-*"`) cover this branch at
  all? (`code-style-checkstyle` is the one rule that's version-anchored via
  an *exact* branch list rather than a date — see its own `effective_from:
  null` comment in the policy file.)

A rule that doesn't apply to a given branch produces no `commit_compliance`
row at all for that (rule, commit) — this is a different, stronger, more
literal exclusion than `not_in_force` (which is specifically about a dated
`effective_from`, per the policy's own `result_states.not_in_force`
definition) and is never confused with it.

## v2 additions (D24, issue #93)

- **Sourcing fields.** Every scored rule and exemption (and each
  `release-process` `sub_pattern`) now carries `source_type`, `source_url`,
  `source_quote` and `effective_from` in the YAML — `Rule`/`Exemption`/
  `SubPattern` expose them so `tests/test_governance_policy_sources.py`
  (enforcement) and `project-health verify-policy-sources` (live re-check)
  can read them without re-parsing YAML themselves. `Policy.source_types`
  is the file's own top-level whitelist (`ratified_governance`,
  `official_docs`, `official_tooling`) — nothing else qualifies (D24).
- **`detection: docs_only_change`.** v2 replaces the old ninja/
  release-housekeeping *pattern* exemptions with a `commit-then-review`
  exemption whose `detection` is `docs_only_change` rather than a regex: it
  matches when every path the commit touched is covered by the file's
  top-level `docs_only_paths` glob list. MEASUREMENT (ours, D24 preamble:
  "how evidence is found... are measurement choices this project makes"):
  `docs_only_paths` uses gitignore-style `**` (a leading `**/` means "at any
  depth, including the repo root"; a trailing `/**` means "anything under
  this directory"), not bare `fnmatch.fnmatchcase`, because plain `fnmatch`
  has no directory-boundary concept and a pattern like `"**/*.md"` would
  then require a literal `/` in the path — wrongly excluding a top-level
  file such as `CONTRIBUTING.md`. `_path_matches_glob` implements the three
  shapes this file's `docs_only_paths` actually uses: `<dir>/**` (path
  equals `<dir>` or starts with `<dir>/`), a leading `**/<glob>` (matched
  against the path's basename only, at any depth), and a bare glob with
  neither (also matched against the basename — e.g. `CHANGES.txt` matches
  that filename at any depth, not only at the repo root). A commit with
  `changed_paths=None` (not collected, e.g. a merge commit) can never be
  proven docs-only and never matches this exemption — see
  `_is_docs_only_change`.
- **`same_as`.** An exemption may point at another rule's exemption instead
  of repeating its pattern/detection/sub_patterns/source fields (e.g.
  `jira-ticket-referenced.exemptions[].same_as:
  reviewer-present.exemptions.release-process`) — `load_policy` resolves
  every `same_as` after all rules are parsed, copying the referenced
  exemption's matching behavior and any source field the pointing exemption
  didn't already set itself, so a resolved `Policy`'s `Exemption` objects
  are always immediately usable without a caller ever following `same_as`
  by hand.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any

import yaml

DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[3] / "governance-policy.yaml"

# The five result states `governance-policy.yaml`'s top-level `result_states`
# declares (D14, D15) — every check's result must be exactly one of these.
RESULT_STATES: tuple[str, ...] = ("pass", "fail", "unknown", "exempt", "not_in_force")


class PolicyValidationError(ValueError):
    """Raised when `governance-policy.yaml` doesn't match the shape this
    loader expects (a missing required key, an unparseable date, an
    unresolvable `same_as` pointer, etc.) — fails loudly rather than
    silently scoring against a malformed policy."""


@dataclass(frozen=True)
class SubPattern:
    id: str
    pattern: str
    pattern_scope: str  # 'full_commit_message' | 'first_line'
    source_type: str | None = None
    source_url: str | None = None
    source_quote: str | None = None
    effective_from: date | None = None
    source_context: str | None = None


@dataclass(frozen=True)
class Exemption:
    id: str
    result: str  # always 'exempt' in this policy, but read from the file rather than assumed
    pattern: str | None = None
    pattern_scope: str | None = None
    sub_patterns: tuple[SubPattern, ...] = ()
    # MEASUREMENT (ours, D24): the only detector besides a regex `pattern` —
    # see module docstring's "v2 additions" section.
    detection: str | None = None
    source_type: str | None = None
    source_url: str | None = None
    source_quote: str | None = None
    effective_from: date | None = None
    source_context: str | None = None
    # Raw `same_as` pointer as read from the YAML (e.g.
    # `"reviewer-present.exemptions.release-process"`) — always `None` on an
    # `Exemption` returned by `load_policy` (resolved away by `_resolve_same_as`
    # before the `Policy` is handed back); kept on the dataclass only so
    # `_resolve_same_as` itself has somewhere to read it from mid-parse.
    same_as: str | None = None

    def matches(
        self,
        message: str,
        changed_paths: tuple[str, ...] | None = None,
        docs_only_paths: tuple[str, ...] = (),
    ) -> bool:
        """True if this exemption applies to a commit with this `message`
        and (for `detection: docs_only_change`) `changed_paths`, per each
        pattern's own `pattern_scope` or `detection`'s own semantics."""
        if self.detection == "docs_only_change":
            return _is_docs_only_change(changed_paths, docs_only_paths)
        if self.sub_patterns:
            return any(
                _pattern_matches(sp.pattern, sp.pattern_scope, message) for sp in self.sub_patterns
            )
        if self.pattern is None:
            return False
        return _pattern_matches(self.pattern, self.pattern_scope or "full_commit_message", message)


@dataclass(frozen=True)
class ReviewWordingCheck:
    """`reviewer-present.review_wording_check` (D15's false-fail guard,
    docs/spec/GOVERNANCE.md §8) — never an exemption, just a guard that
    turns what would otherwise be a `fail` into an `unknown` when the
    message contains review wording the parser still couldn't resolve into
    a named reviewer."""

    pattern: str
    pattern_scope: str

    def matches(self, message: str) -> bool:
        return _pattern_matches(self.pattern, self.pattern_scope, message)


@dataclass(frozen=True)
class Rule:
    id: str
    description: str
    effective_from: date | None
    branches: tuple[str, ...]  # applies_to.branches, fnmatch-style globs
    change_types: tuple[str, ...]
    fail_allowed: bool
    scored: bool
    exemptions: tuple[Exemption, ...] = ()
    review_wording_check: ReviewWordingCheck | None = None
    source_type: str | None = None
    source_url: str | None = None
    source_quote: str | None = None
    # MEASUREMENT (ours, D24): denormalized from the policy's top-level
    # `docs_only_paths` at parse time so `Rule.matching_exemption` is
    # self-sufficient (never needs its owning `Policy` passed back in) —
    # every rule in the file shares the same single `docs_only_paths` list,
    # there is no per-rule override.
    docs_only_paths: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def in_force_on(self, commit_date: date) -> bool:
        """True unless `commit_date` is strictly before `effective_from`
        (D14). A rule with no dated source (`effective_from: null`) is
        always in force."""
        if self.effective_from is None:
            return True
        return commit_date >= self.effective_from

    def applies_to_branch(self, branch: str) -> bool:
        """True if `branch` matches any of `applies_to.branches`' fnmatch
        globs (e.g. `"cassandra-*"` matches `"cassandra-5.0"`)."""
        return any(fnmatch.fnmatchcase(branch, pattern) for pattern in self.branches)

    def matching_exemption(
        self, message: str, changed_paths: tuple[str, ...] | None = None
    ) -> Exemption | None:
        """The first exemption (in file order) whose pattern/detection
        matches this commit's `message` and `changed_paths`, or `None`."""
        for exemption in self.exemptions:
            if exemption.matches(message, changed_paths, self.docs_only_paths):
                return exemption
        return None

    def review_wording_present(self, message: str) -> bool:
        if self.review_wording_check is None:
            return False
        return self.review_wording_check.matches(message)


@dataclass(frozen=True)
class Policy:
    version: int
    policy_name: str
    approved_by: str
    approved_on: date
    rules: dict[str, Rule]
    result_states: tuple[str, ...]
    # D24: the only source types anything in this file may cite. `()` for a
    # (hypothetical, pre-D24) policy file with no `source_types` block.
    source_types: tuple[str, ...] = ()
    docs_only_paths: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def rule(self, check_id: str) -> Rule:
        try:
            return self.rules[check_id]
        except KeyError as exc:
            raise KeyError(
                f"unknown governance check_id {check_id!r}; known: {sorted(self.rules)}"
            ) from exc


_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


def _compiled(pattern: str) -> re.Pattern[str]:
    compiled = _PATTERN_CACHE.get(pattern)
    if compiled is None:
        compiled = re.compile(pattern)
        _PATTERN_CACHE[pattern] = compiled
    return compiled


def _first_line(message: str) -> str:
    return message.splitlines()[0] if message else ""


def _pattern_matches(pattern: str, scope: str, message: str) -> bool:
    text = _first_line(message) if scope == "first_line" else message
    return _compiled(pattern).search(text) is not None


def _path_matches_glob(path: str, pattern: str) -> bool:
    """MEASUREMENT (ours, D24): gitignore-style `**` matching for
    `docs_only_paths` — see module docstring's "v2 additions" section for
    why plain `fnmatch.fnmatchcase` is wrong here (a leading `**/` would
    then require a literal `/`, excluding top-level files)."""
    if pattern.endswith("/**"):
        prefix = pattern[: -len("/**")]
        return path == prefix or path.startswith(prefix + "/")
    if pattern.startswith("**/"):
        pattern = pattern[len("**/") :]
    basename = path.rsplit("/", 1)[-1]
    return fnmatch.fnmatchcase(basename, pattern)


def _is_docs_only_change(
    changed_paths: tuple[str, ...] | None, docs_only_paths: tuple[str, ...]
) -> bool:
    """MEASUREMENT (ours, D24): see module docstring's "v2 additions"
    section for the glob-matching choice. `changed_paths=None` (not
    collected for this commit) or an empty `docs_only_paths` list can never
    be proven docs-only."""
    if not changed_paths or not docs_only_paths:
        return False
    return all(
        any(_path_matches_glob(path, pattern) for pattern in docs_only_paths)
        for path in changed_paths
    )


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _parse_sub_patterns(raw_list: list[dict[str, Any]] | None) -> tuple[SubPattern, ...]:
    if not raw_list:
        return ()
    return tuple(
        SubPattern(
            id=item["id"],
            pattern=item["pattern"],
            pattern_scope=item["pattern_scope"],
            source_type=item.get("source_type"),
            source_url=item.get("source_url"),
            source_quote=item.get("source_quote"),
            effective_from=_parse_date(item.get("effective_from")),
            source_context=item.get("source_context"),
        )
        for item in raw_list
    )


def _parse_exemptions(raw_list: list[dict[str, Any]] | None) -> tuple[Exemption, ...]:
    if not raw_list:
        return ()
    exemptions = []
    for item in raw_list:
        exemptions.append(
            Exemption(
                id=item["id"],
                result=item.get("result", "exempt"),
                pattern=item.get("pattern"),
                pattern_scope=item.get("pattern_scope"),
                sub_patterns=_parse_sub_patterns(item.get("sub_patterns")),
                detection=item.get("detection"),
                source_type=item.get("source_type"),
                source_url=item.get("source_url"),
                source_quote=item.get("source_quote"),
                effective_from=_parse_date(item.get("effective_from")),
                source_context=item.get("source_context"),
                same_as=item.get("same_as"),
            )
        )
    return tuple(exemptions)


def _parse_review_wording_check(raw: dict[str, Any] | None) -> ReviewWordingCheck | None:
    if raw is None:
        return None
    return ReviewWordingCheck(pattern=raw["pattern"], pattern_scope=raw["pattern_scope"])


def _parse_rule(raw: dict[str, Any], docs_only_paths: tuple[str, ...]) -> Rule:
    applies_to = raw.get("applies_to") or {}
    result_semantics = raw.get("result_semantics") or {}
    return Rule(
        id=raw["id"],
        description=raw.get("description", ""),
        effective_from=_parse_date(raw.get("effective_from")),
        branches=tuple(applies_to.get("branches") or ()),
        change_types=tuple(applies_to.get("change_types") or ()),
        fail_allowed=bool(result_semantics.get("fail_allowed", False)),
        scored=bool(raw.get("scored", True)),
        exemptions=_parse_exemptions(raw.get("exemptions")),
        review_wording_check=_parse_review_wording_check(raw.get("review_wording_check")),
        source_type=raw.get("source_type"),
        source_url=raw.get("source_url"),
        source_quote=raw.get("source_quote"),
        docs_only_paths=docs_only_paths,
        raw=raw,
    )


def _resolve_same_as(rules: dict[str, Rule], path: str | Path) -> dict[str, Rule]:
    """Resolve every exemption's `same_as` pointer (v2, D24) against the
    *unresolved* `rules` mapping (a `same_as` target is never itself a
    `same_as` in this policy, so a single pass over the original mapping is
    enough — resolving against `rules` rather than a partially-resolved
    dict avoids depending on dict iteration order).

    A resolved exemption inherits the target's matching behavior
    (`pattern`/`pattern_scope`/`sub_patterns`/`detection`) unconditionally
    (that *is* what `same_as` means: score commits exactly like that other
    exemption does) and inherits each source field only where the pointing
    exemption didn't already set its own.
    """

    def resolve(exemption: Exemption, path_hint: str) -> Exemption:
        if exemption.same_as is None:
            return exemption
        rule_id, sep, exemption_id = exemption.same_as.partition(".exemptions.")
        if not sep:
            raise PolicyValidationError(
                f"{path}: malformed same_as {exemption.same_as!r} on {path_hint} "
                "(expected '<rule_id>.exemptions.<exemption_id>')"
            )
        target_rule = rules.get(rule_id)
        if target_rule is None:
            raise PolicyValidationError(
                f"{path}: same_as {exemption.same_as!r} on {path_hint} references "
                f"unknown rule {rule_id!r}"
            )
        target = next((e for e in target_rule.exemptions if e.id == exemption_id), None)
        if target is None:
            raise PolicyValidationError(
                f"{path}: same_as {exemption.same_as!r} on {path_hint} references "
                f"unknown exemption {exemption_id!r} on rule {rule_id!r}"
            )
        return replace(
            exemption,
            pattern=target.pattern,
            pattern_scope=target.pattern_scope,
            sub_patterns=target.sub_patterns,
            detection=target.detection,
            source_type=exemption.source_type or target.source_type,
            source_url=exemption.source_url or target.source_url,
            source_quote=exemption.source_quote or target.source_quote,
            effective_from=exemption.effective_from or target.effective_from,
            same_as=None,
        )

    resolved: dict[str, Rule] = {}
    for rule_id, rule in rules.items():
        new_exemptions = tuple(
            resolve(ex, f"{rule_id}.exemptions.{ex.id}") for ex in rule.exemptions
        )
        resolved[rule_id] = replace(rule, exemptions=new_exemptions)
    return resolved


def load_policy(path: str | Path = DEFAULT_POLICY_PATH) -> Policy:
    """Load and validate `governance-policy.yaml` into a typed `Policy`.

    Raises `FileNotFoundError` if `path` doesn't exist, and
    `PolicyValidationError` if a required top-level key or per-rule field is
    missing/malformed, or an exemption's `same_as` pointer doesn't resolve.
    Unknown/extra keys in the YAML (e.g. `not_scored`, `deferred_to_v2`,
    `branches_in_scope`, `merge_forward_policy`, `removed_in_v2`) are
    preserved on `Policy.raw` (and each rule's `Rule.raw`) but not otherwise
    modeled here — this engine only needs the scoring-relevant shape.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"governance policy not found: {path}")

    raw: Any = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise PolicyValidationError(f"{path}: expected a YAML mapping at the top level")

    required = ("version", "policy_name", "approved_by", "approved_on", "rules")
    missing = [key for key in required if key not in raw]
    if missing:
        raise PolicyValidationError(f"{path}: missing required top-level key(s) {missing!r}")

    docs_only_paths = tuple(raw.get("docs_only_paths") or ())

    try:
        rules = {item["id"]: _parse_rule(item, docs_only_paths) for item in raw["rules"]}
    except (KeyError, TypeError) as exc:
        raise PolicyValidationError(f"{path}: malformed `rules` entry: {exc}") from exc

    rules = _resolve_same_as(rules, path)

    result_states = tuple((raw.get("result_states") or {}).keys()) or RESULT_STATES
    source_types = tuple((raw.get("source_types") or {}).keys())

    return Policy(
        version=int(raw["version"]),
        policy_name=raw["policy_name"],
        approved_by=raw["approved_by"],
        approved_on=_parse_date(raw["approved_on"]),
        rules=rules,
        result_states=result_states,
        source_types=source_types,
        docs_only_paths=docs_only_paths,
        raw=raw,
    )
