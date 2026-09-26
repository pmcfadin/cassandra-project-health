"""Loader for the Phase 2a Jev question set (DECISIONS.md D17, D18; issue #42).

Loads and validates `questions_v1.yaml` against the invariants required by
`docs/spec/COMMUNITY-HEALTH.md` §1.2/§1.3/§4 and `docs/spec/DECISIONS.md` D17, then
builds the `typesafe_sdk` `Noul`/`Score` question objects a classifier implementation
sends to Jev in one `system_one` call per message.

This module does not call TypeSafe. It is a pure, offline YAML-to-SDK-object loader,
by design (`tests/test_questions.py` runs with no network access and no API key).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import yaml

try:
    from typesafe_sdk import Noul, Score
except ImportError as exc:  # pragma: no cover - exercised only when the dep is missing
    raise ImportError(
        "typesafe-sdk is required to build question objects (see pyproject.toml "
        "dependencies). YAML loading/validation alone does not need it, but "
        "QuestionSet.build_questions() does."
    ) from exc

# The 12 message-level labels defined in COMMUNITY-HEALTH.md §1.2 (table rows 1-12).
# Every one of these must appear in questions_v1.yaml exactly once.
MESSAGE_LEVEL_LABELS: frozenset[str] = frozenset(
    {
        "technical_disagreement",
        "constructive_counterargument",
        "evidence_based_argument",
        "compromise_offer",
        "acknowledgment",
        "personal_attack",
        "hostility",
        "dismissiveness",
        "sarcasm",
        "gatekeeping",
        "status_authority_invocation",
        "resolution_marker",
    }
)

# The thread-level labels from COMMUNITY-HEALTH.md §1.2 (table rows 13-18). §1.3 requires
# these are never asked of the classifier directly -- they are derived in code (§2.3)
# from message-level answers plus the reply graph and timestamps. Listed here (by their
# §1.2 name, using underscores for the hyphenated ones) purely so the validator can give
# a precise error if one is ever added to the YAML by mistake.
THREAD_LEVEL_LABELS: frozenset[str] = frozenset(
    {
        "escalation",
        "de-escalation",
        "de_escalation",
        "pile-on",
        "pile_on",
        "resolution",
        "thread_abandonment",
        "newcomer_treatment",
    }
)

# label_group values must match one of COMMUNITY-HEALTH.md §6.4's gate groups.
VALID_LABEL_GROUPS: frozenset[str] = frozenset({"reputational_harm", "friction", "argument"})

EXPECTED_MODEL = "jev-1.13.0"
EXPECTED_VERSION = 1

DEFAULT_QUESTION_SET_PATH = Path(__file__).with_name("questions_v1.yaml")


class QuestionSetError(ValueError):
    """Raised when questions_v1.yaml fails validation."""


@dataclasses.dataclass(frozen=True)
class QuestionSet:
    """A validated, loaded question set, ready to build SDK question objects from."""

    version: int
    model: str
    labels: dict[str, dict[str, Any]]
    score: dict[str, Any]
    state_schema: dict[str, dict[str, Any]]
    raw: dict[str, Any]

    def build_questions(self) -> dict[str, Any]:
        """Build the ``{question_id: Noul | Score}`` mapping for a `system_one` call.

        One Noul per message-level label plus the one `tone_intensity` Score, matching
        the "ask independent questions over the same state together" guidance
        (concepts/how-to-build-with-system-one): all 13 questions are meant to be sent
        in a single request per message, not one request per question.
        """
        questions: dict[str, Any] = {}
        for label_id, spec in self.labels.items():
            criteria = spec["criteria"]
            questions[label_id] = Noul(
                instructions=spec["instructions"],
                criteria={"true": criteria["true"], "false": criteria["false"]},
            )

        score_spec = self.score
        ordered_levels = sorted(score_spec["levels"], key=lambda level: level["level"])
        questions[score_spec["id"]] = Score(
            instructions=score_spec["instructions"],
            criteria=[level["description"] for level in ordered_levels],
        )
        return questions


def load_question_set(path: Path | str | None = None) -> QuestionSet:
    """Load and validate `questions_v1.yaml` (or an override path for testing)."""
    resolved = Path(path) if path is not None else DEFAULT_QUESTION_SET_PATH
    with open(resolved, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise QuestionSetError(f"{resolved}: expected a YAML mapping at the top level")

    _validate(raw)

    labels = {item["id"]: item for item in raw["labels"]}
    return QuestionSet(
        version=raw["version"],
        model=raw["model"],
        labels=labels,
        score=raw["score"],
        state_schema=raw["state_schema"],
        raw=raw,
    )


def _validate(raw: dict[str, Any]) -> None:
    if raw.get("version") != EXPECTED_VERSION:
        raise QuestionSetError(f"expected version: {EXPECTED_VERSION}, got {raw.get('version')!r}")
    if raw.get("model") != EXPECTED_MODEL:
        raise QuestionSetError(
            f"expected pinned model {EXPECTED_MODEL!r}, got {raw.get('model')!r}"
        )

    state_schema = raw.get("state_schema")
    if not isinstance(state_schema, dict) or "message.text" not in state_schema:
        raise QuestionSetError("state_schema must be defined and include 'message.text'")

    labels = raw.get("labels")
    if not isinstance(labels, list) or not labels:
        raise QuestionSetError("questions_v1.yaml must define a non-empty 'labels' list")

    seen: set[str] = set()
    for item in labels:
        label_id = item.get("id")
        if not label_id:
            raise QuestionSetError(f"a label entry is missing 'id': {item!r}")
        if label_id in seen:
            raise QuestionSetError(f"label {label_id!r} is asked more than once")
        seen.add(label_id)

        if label_id in THREAD_LEVEL_LABELS:
            raise QuestionSetError(
                f"{label_id!r} is a thread-level label (COMMUNITY-HEALTH.md §1.2/§1.3) "
                "derived in code and must never be asked of the classifier directly"
            )
        if item.get("unit") != "message":
            raise QuestionSetError(f"label {label_id!r} must have unit: message")
        if item.get("primitive") != "noul":
            raise QuestionSetError(f"label {label_id!r} must use the noul primitive")
        if "threshold" not in item or item["threshold"] is not None:
            raise QuestionSetError(
                f"label {label_id!r} threshold must be null in v1 (calibrated later "
                "against the frozen benchmark, see issue #47)"
            )
        group = item.get("label_group")
        if group not in VALID_LABEL_GROUPS:
            raise QuestionSetError(
                f"label {label_id!r} has label_group {group!r}, expected one of "
                f"{sorted(VALID_LABEL_GROUPS)} (COMMUNITY-HEALTH.md §6.4 gate groups)"
            )
        if not item.get("instructions"):
            raise QuestionSetError(f"label {label_id!r} is missing instructions")
        criteria = item.get("criteria") or {}
        if "true" not in criteria or "false" not in criteria:
            raise QuestionSetError(f"label {label_id!r} criteria must define 'true' and 'false'")

    missing = MESSAGE_LEVEL_LABELS - seen
    if missing:
        raise QuestionSetError(
            f"missing required COMMUNITY-HEALTH.md §1.2 message-level label(s): {sorted(missing)}"
        )
    extra = seen - MESSAGE_LEVEL_LABELS
    if extra:
        raise QuestionSetError(
            f"label(s) not in COMMUNITY-HEALTH.md §1.2's message-level set: {sorted(extra)}"
        )

    score = raw.get("score")
    if not isinstance(score, dict):
        raise QuestionSetError("questions_v1.yaml must define a 'score' block for tone intensity")
    if score.get("id") != "tone_intensity":
        raise QuestionSetError("the score question must be named 'tone_intensity'")
    if not score.get("instructions"):
        raise QuestionSetError("tone_intensity is missing instructions")
    levels = score.get("levels")
    if not isinstance(levels, list) or not (2 <= len(levels) <= 10):
        raise QuestionSetError("tone_intensity must define between 2 and 10 levels")
    level_indices = sorted(level.get("level") for level in levels)
    if level_indices != list(range(len(levels))):
        raise QuestionSetError("tone_intensity levels must be numbered 0..N-1 with no gaps")
    for level in levels:
        if not level.get("description"):
            raise QuestionSetError(
                f"tone_intensity level {level.get('level')!r} needs a self-contained description"
            )
