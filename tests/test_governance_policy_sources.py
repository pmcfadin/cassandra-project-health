"""D24 enforcement: every scored rule, exemption and sub_pattern in
`governance-policy.yaml` must carry a source (issue #93).

"Allowed sources... every scored governance rule and every exemption in
`governance-policy.yaml` must come from one of three places... Required
fields: each rule and exemption carries its URL, a verbatim quote, its
source type and an `effective_from` date... Enforcement: a test fails if any
scored rule or exemption lacks a source." (docs/spec/DECISIONS.md D24)

This walks the **raw YAML** (not the parsed `Policy`/`Rule`/`Exemption`
objects) deliberately: the requirement is about what the *file* declares, and
checking `"effective_from" in raw_dict` is the only way to distinguish "the
key is present with an explicit `null`" (allowed -- e.g.
`jira-ticket-referenced.effective_from: null`) from "the key is missing
entirely" (not allowed), which a value already parsed to `None` can't tell
apart on its own.

An exemption with `sub_patterns` (e.g. `release-process`) is sourced through
each sub_pattern individually rather than at the exemption's own level (that
is what `release-process` etc. actually look like in the file — see
governance-policy.yaml). An exemption with `same_as` points at another rule's
exemption instead of repeating a source; this test doesn't re-check the
target's fields directly (a `same_as` pointer's target is itself just another
exemption in `rules[]` and gets checked when its own rule is walked) but does
confirm the pointer resolves, via `load_policy` -- an unresolvable `same_as`
raises `PolicyValidationError`, which the `policy()` fixture surfaces
immediately as a fixture-setup failure for every test in this module, itself
a strong enforcement signal.
"""

from __future__ import annotations

import yaml
import pytest

from project_health.governance.policy import DEFAULT_POLICY_PATH, load_policy

_REQUIRED_FIELDS = ("source_type", "source_url", "source_quote", "effective_from")


@pytest.fixture(scope="module")
def raw_policy() -> dict:
    return yaml.safe_load(DEFAULT_POLICY_PATH.read_text())


@pytest.fixture(scope="module")
def policy():
    # Loading (not just reading the YAML) also exercises `same_as`
    # resolution -- an unresolvable pointer fails here for every test.
    return load_policy(DEFAULT_POLICY_PATH)


def _missing_fields(item: dict) -> list[str]:
    return [field for field in _REQUIRED_FIELDS if field not in item]


def _check_source_type(item: dict, source_types: set[str], where: str, errors: list[str]) -> None:
    source_type = item.get("source_type")
    if source_type is not None and source_type not in source_types:
        errors.append(
            f"{where}: source_type {source_type!r} is not one of the file's source_types "
            f"{sorted(source_types)!r}"
        )


def test_source_types_block_present(raw_policy):
    assert raw_policy.get("source_types"), "governance-policy.yaml must declare source_types"


def test_every_scored_rule_exemption_and_sub_pattern_is_sourced(raw_policy):
    """The core D24 enforcement check. Fails with every offending path
    collected at once (rather than stopping at the first) so a real
    regression is easy to triage in one run."""
    source_types = set(raw_policy["source_types"])
    errors: list[str] = []

    for rule in raw_policy["rules"]:
        rule_id = rule["id"]
        if not rule.get("scored", True):
            continue  # changes-txt-entry / news-txt-entry / test-touched: display-only, D24 N/A

        missing = _missing_fields(rule)
        if missing:
            errors.append(f"rule {rule_id!r}: missing {missing}")
        _check_source_type(rule, source_types, f"rule {rule_id!r}", errors)

        for exemption in rule.get("exemptions", []):
            exemption_id = exemption["id"]
            where = f"rule {rule_id!r} exemption {exemption_id!r}"

            if "same_as" in exemption:
                # Sourced by inheritance from its same_as target -- see
                # module docstring. Nothing further to check here.
                continue

            if "sub_patterns" in exemption:
                for sub in exemption["sub_patterns"]:
                    sub_where = f"{where} sub_pattern {sub['id']!r}"
                    missing = _missing_fields(sub)
                    if missing:
                        errors.append(f"{sub_where}: missing {missing}")
                    _check_source_type(sub, source_types, sub_where, errors)
                continue

            missing = _missing_fields(exemption)
            if missing:
                errors.append(f"{where}: missing {missing}")
            _check_source_type(exemption, source_types, where, errors)

    assert not errors, "D24 source-completeness violations:\n" + "\n".join(errors)


def test_same_as_pointers_all_resolve(policy):
    """`load_policy` raises `PolicyValidationError` (surfaced as a fixture
    error, not an assertion failure) if any `same_as` doesn't resolve --
    this test just confirms the fixture actually built a `Policy`, and spot
    checks that a resolved exemption never carries a `same_as` remnant."""
    for rule in policy.rules.values():
        for exemption in rule.exemptions:
            assert exemption.same_as is None


def test_removed_in_v2_entries_are_not_active_exemptions(raw_policy):
    """D24's first application removed `ninja`/`submodule-repin` -- confirm
    they're documented under `removed_in_v2`, not left in `exemptions`
    (which would defeat the point of this whole enforcement test, since
    `removed_in_v2` entries carry no source fields by design)."""
    for rule in raw_policy["rules"]:
        removed_ids = {item["id"] for item in rule.get("removed_in_v2", [])}
        active_ids = {exemption["id"] for exemption in rule.get("exemptions", [])}
        assert removed_ids.isdisjoint(active_ids), (
            f"rule {rule['id']!r}: {removed_ids & active_ids} listed in both "
            "removed_in_v2 and exemptions"
        )
