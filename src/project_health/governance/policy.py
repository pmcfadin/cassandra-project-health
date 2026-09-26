"""Typed loader and validation for `governance-policy.yaml` (issue #36).

`governance-policy.yaml` (repo root) is the binding, owner-approved v1
compliance policy (D14, D15; `docs/spec/GOVERNANCE.md`). This module is the
*only* place that parses that file's raw YAML shape into typed objects — the
rest of the governance engine (`checks.py`, `engine.py`) works against
`Policy`/`Rule`, never the raw dict.

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
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
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
    loader expects (a missing required key, an unparseable date, etc.) —
    fails loudly rather than silently scoring against a malformed policy."""


@dataclass(frozen=True)
class SubPattern:
    id: str
    pattern: str
    pattern_scope: str  # 'full_commit_message' | 'first_line'


@dataclass(frozen=True)
class Exemption:
    id: str
    result: str  # always 'exempt' in v1, but read from the file rather than assumed
    pattern: str | None = None
    pattern_scope: str | None = None
    sub_patterns: tuple[SubPattern, ...] = ()

    def matches(self, message: str) -> bool:
        """True if this exemption's pattern (or any of its `sub_patterns`)
        matches `message`, per each pattern's own `pattern_scope`."""
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

    def matching_exemption(self, message: str) -> Exemption | None:
        """The first exemption (in file order) whose pattern matches
        `message`, or `None`."""
        for exemption in self.exemptions:
            if exemption.matches(message):
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
        SubPattern(id=item["id"], pattern=item["pattern"], pattern_scope=item["pattern_scope"])
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
            )
        )
    return tuple(exemptions)


def _parse_review_wording_check(raw: dict[str, Any] | None) -> ReviewWordingCheck | None:
    if raw is None:
        return None
    return ReviewWordingCheck(pattern=raw["pattern"], pattern_scope=raw["pattern_scope"])


def _parse_rule(raw: dict[str, Any]) -> Rule:
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
        raw=raw,
    )


def load_policy(path: str | Path = DEFAULT_POLICY_PATH) -> Policy:
    """Load and validate `governance-policy.yaml` into a typed `Policy`.

    Raises `FileNotFoundError` if `path` doesn't exist, and
    `PolicyValidationError` if a required top-level key or per-rule field is
    missing/malformed. Unknown/extra keys in the YAML (e.g. `not_scored`,
    `deferred_to_v2`, `branches_in_scope`, `merge_forward_policy`) are
    preserved on `Policy.raw` but not otherwise modeled here — this engine
    only needs the scoring-relevant shape.
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

    try:
        rules = {item["id"]: _parse_rule(item) for item in raw["rules"]}
    except (KeyError, TypeError) as exc:
        raise PolicyValidationError(f"{path}: malformed `rules` entry: {exc}") from exc

    result_states = tuple((raw.get("result_states") or {}).keys()) or RESULT_STATES

    return Policy(
        version=int(raw["version"]),
        policy_name=raw["policy_name"],
        approved_by=raw["approved_by"],
        approved_on=_parse_date(raw["approved_on"]),
        rules=rules,
        result_states=result_states,
        raw=raw,
    )
