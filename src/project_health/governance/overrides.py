"""Typed loader for `governance_overrides.yaml` (issue #36, D15).

D15: "every row links to the corrections process, and a correction is a
PR-reviewed override with the reason recorded." `governance_overrides.yaml`
(repo root) is that correction file — a small, human-curated, PR-reviewed
list of `(sha, check_id) -> result` overrides, each carrying a `reason` and
the `reviewer` who approved the correcting PR, the same shape/spirit as
`identity_overrides.yaml` (`normalize/identity.py`) but for governance
results instead of identity links.

Overrides are applied *last*, after every other scoring step
(`governance/engine.py`), and always leave a trace: the overridden row's
`evidence` field is rewritten to record the original result, the override's
reason, and its reviewer, so a reader never sees a silently-changed number.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from project_health.governance.policy import RESULT_STATES

DEFAULT_OVERRIDES_PATH = Path(__file__).resolve().parents[3] / "governance_overrides.yaml"


class OverrideValidationError(ValueError):
    """Raised when `governance_overrides.yaml` doesn't match the expected shape."""


@dataclass(frozen=True)
class Override:
    sha: str
    check_id: str
    result: str
    reason: str
    reviewer: str


def _parse_override(item: dict[str, Any]) -> Override:
    required = ("sha", "check_id", "result", "reason", "reviewer")
    missing = [key for key in required if not item.get(key)]
    if missing:
        raise OverrideValidationError(
            f"override entry missing required field(s) {missing!r}: {item!r}"
        )
    result = item["result"]
    if result not in RESULT_STATES:
        raise OverrideValidationError(
            f"override for {item['sha']}/{item['check_id']}: result {result!r} is not one of "
            f"{RESULT_STATES!r}"
        )
    return Override(
        sha=item["sha"],
        check_id=item["check_id"],
        result=result,
        reason=item["reason"],
        reviewer=item["reviewer"],
    )


def load_overrides(path: str | Path = DEFAULT_OVERRIDES_PATH) -> list[Override]:
    """Load `governance_overrides.yaml`'s `overrides:` list.

    Returns `[]` if the file doesn't exist (overrides are optional — most
    runs have none) or if its `overrides:` list is empty/absent. Raises
    `OverrideValidationError` on a malformed entry (missing field, or a
    `result` that isn't one of the policy's five result states) — a
    corrections file is human-edited and PR-reviewed, so a typo should fail
    the run loudly rather than silently doing nothing.
    """
    path = Path(path)
    if not path.is_file():
        return []

    raw: Any = yaml.safe_load(path.read_text())
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise OverrideValidationError(f"{path}: expected a YAML mapping at the top level")

    entries = raw.get("overrides") or []
    return [_parse_override(item) for item in entries]


def override_key(sha: str, check_id: str) -> tuple[str, str]:
    return (sha, check_id)


def index_overrides(overrides: list[Override]) -> dict[tuple[str, str], Override]:
    """`(sha, check_id) -> Override`, for O(1) lookup while scoring rows.

    If more than one override targets the same `(sha, check_id)`, the last
    one in file order wins (matches `dict`'s natural last-write-wins
    behavior) — the file is short and PR-reviewed, so this is a reasonable
    tie-break rather than an error.
    """
    return {override_key(o.sha, o.check_id): o for o in overrides}
