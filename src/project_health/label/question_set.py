"""Read-only view of the Jev question set's tone scale and message-level
label ids, for the local labeling tool (issue #46, orchestrator correction
2026-09-25): the rater's tone scale and label list must match
`classify/questions_v1.yaml` exactly -- loaded at runtime, never
hard-coded -- so a human rating and a Jev classification are answers to the
*same* question and comparable label-for-label.

Deliberately does **not** import `classify/questions.py` (which requires
`typesafe_sdk` to build SDK objects): this is a plain YAML read of the same
file, so the labeling tool keeps no dependency on the Jev SDK at all (D22:
no LLM/model dependency at runtime for labeling). `tests/test_label_server.
py` cross-checks that both modules agree on what the file contains.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import yaml

# classify/questions_v1.yaml, next to (not inside) this package.
DEFAULT_QUESTION_SET_PATH = (
    Path(__file__).resolve().parents[1] / "classify" / "questions_v1.yaml"
)


class QuestionSetReadError(ValueError):
    """Raised when questions_v1.yaml can't be read for its tone scale/labels."""


@dataclasses.dataclass(frozen=True)
class ToneLevel:
    level: int
    name: str
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level, "name": self.name, "description": self.description}


@dataclasses.dataclass(frozen=True)
class QuestionSetSummary:
    """The two facts about `questions_v1.yaml` the labeling tool needs: its
    version (recorded on every label record as `question_set_version`) and
    its tone scale (shown to the rater verbatim, replacing any hard-coded
    0-2 scale)."""

    version: int
    message_label_ids: tuple[str, ...]
    tone_levels: tuple[ToneLevel, ...]

    def tone_level_numbers(self) -> frozenset[int]:
        return frozenset(level.level for level in self.tone_levels)


def load_question_set_summary(path: Path | str | None = None) -> QuestionSetSummary:
    resolved = Path(path) if path is not None else DEFAULT_QUESTION_SET_PATH
    with open(resolved, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise QuestionSetReadError(f"{resolved}: expected a YAML mapping at the top level")

    version = raw.get("version")
    if not isinstance(version, int):
        raise QuestionSetReadError(f"{resolved}: 'version' must be an integer")

    labels = raw.get("labels")
    if not isinstance(labels, list) or not labels:
        raise QuestionSetReadError(f"{resolved}: 'labels' must be a non-empty list")
    label_ids = tuple(item["id"] for item in labels)

    score = raw.get("score")
    if not isinstance(score, dict) or score.get("id") != "tone_intensity":
        raise QuestionSetReadError(f"{resolved}: missing the 'tone_intensity' score block")
    levels_raw = score.get("levels")
    if not isinstance(levels_raw, list) or not levels_raw:
        raise QuestionSetReadError(f"{resolved}: 'score.levels' must be a non-empty list")
    levels = tuple(
        ToneLevel(
            level=item["level"],
            name=item.get("name", ""),
            description=item["description"],
        )
        for item in sorted(levels_raw, key=lambda item: item["level"])
    )

    return QuestionSetSummary(version=version, message_label_ids=label_ids, tone_levels=levels)
