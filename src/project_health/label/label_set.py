"""Versioned label-set loader for the local labeling tool (DECISIONS.md D18,
D22; COMMUNITY-HEALTH.md §1.2; issue #46).

The 18 rows of `COMMUNITY-HEALTH.md` §1.2's label reference table are copied
**verbatim** into `label_set_v1.yaml` (id, unit, origin, definition) by
`parse_label_table`, the same parser `tests/test_label_set.py` uses to
independently re-parse the current spec doc and assert the packaged file
still matches it word for word -- so a future spec edit that isn't mirrored
into the packaged file fails the test loudly instead of drifting silently.

Only the twelve `unit: message` rows (table rows 1-12) are ever shown to a
rater as a yes/no/unsure question (`LabelSet.ratable`, COMMUNITY-HEALTH.md
§1.3: thread-level labels, rows 13-18, are derived in code and never asked
of a rater or a classifier directly). All 18 rows are still loaded and kept
so the versioned file is a complete, literal copy of the spec table, but the
non-`message` rows are exposed only via `LabelSet.labels` for completeness.

This module has no dependency on `classify/questions.py` or `typesafe_sdk`
by design: the labeling tool must work with no LLM/model dependency
whatsoever (D22).
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Any

import yaml

DEFAULT_LABEL_SET_PATH = Path(__file__).with_name("label_set_v1.yaml")

_TABLE_HEADING = "### 1.2 Label reference table"
_HEADER_CELLS = ("#", "Label", "Unit", "Origin", "Definition")


class LabelSetError(ValueError):
    """Raised when a label-set file fails to load or validate."""


@dataclasses.dataclass(frozen=True)
class LabelDef:
    """One row of COMMUNITY-HEALTH.md §1.2's label reference table."""

    number: int
    id: str
    unit: str
    origin: str
    definition: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "id": self.id,
            "unit": self.unit,
            "origin": self.origin,
            "definition": self.definition,
        }


@dataclasses.dataclass(frozen=True)
class LabelSet:
    """A validated, loaded label set."""

    version: int
    labels: tuple[LabelDef, ...]

    @property
    def ratable(self) -> tuple[LabelDef, ...]:
        """The message-level labels (§1.2 rows 1-12) a rater marks yes/no/unsure."""
        return tuple(label for label in self.labels if label.unit == "message")

    def by_id(self, label_id: str) -> LabelDef | None:
        return next((label for label in self.labels if label.id == label_id), None)


def parse_label_table_from_markdown(text: str) -> list[LabelDef]:
    """Parse the §1.2 "Label reference table" rows out of raw markdown text.

    Independent of `load_label_set` below -- this reads a COMMUNITY-HEALTH.md
    document (any version), not the packaged `label_set_v1.yaml`. Used both to
    generate the packaged file and, separately, by the test suite to prove the
    packaged file still matches the checked-out spec doc verbatim.
    """
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == _TABLE_HEADING)
    except StopIteration as exc:
        raise LabelSetError(f"{_TABLE_HEADING!r} not found in document") from exc

    # Find the table's header row (first "| # | Label | ..." line after the heading).
    header_idx = next(
        (i for i in range(start, len(lines)) if lines[i].strip().startswith("| #")),
        None,
    )
    if header_idx is None:
        raise LabelSetError("no table found under §1.2 heading")

    header_cells = _split_row(lines[header_idx])
    if tuple(header_cells) != _HEADER_CELLS:
        raise LabelSetError(f"unexpected table header: {header_cells!r}")

    # header_idx + 1 is the "|---|---|...|" separator row.
    rows: list[LabelDef] = []
    for line in lines[header_idx + 2 :]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            break  # end of the table
        cells = _split_row(line)
        if len(cells) != 5:
            raise LabelSetError(f"malformed table row (expected 5 cells): {line!r}")
        number_str, label_cell, unit, origin, definition = cells
        rows.append(
            LabelDef(
                number=int(number_str),
                id=label_cell.strip("`"),
                unit=unit,
                origin=origin,
                definition=definition,
            )
        )

    if not rows:
        raise LabelSetError("§1.2 table heading found but no data rows parsed")
    return rows


def _split_row(line: str) -> list[str]:
    stripped = line.strip()
    stripped = re.sub(r"^\|", "", stripped)
    stripped = re.sub(r"\|$", "", stripped)
    return [cell.strip() for cell in stripped.split("|")]


def parse_label_table(markdown_path: Path | str) -> list[LabelDef]:
    """`parse_label_table_from_markdown`, reading the doc from `markdown_path`."""
    text = Path(markdown_path).read_text(encoding="utf-8")
    return parse_label_table_from_markdown(text)


def load_label_set(path: Path | str | None = None) -> LabelSet:
    """Load and validate the packaged (or an override) label-set YAML file."""
    resolved = Path(path) if path is not None else DEFAULT_LABEL_SET_PATH
    with open(resolved, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise LabelSetError(f"{resolved}: expected a YAML mapping at the top level")

    version = raw.get("version")
    if not isinstance(version, int):
        raise LabelSetError(f"{resolved}: 'version' must be an integer")

    entries = raw.get("labels")
    if not isinstance(entries, list) or not entries:
        raise LabelSetError(f"{resolved}: 'labels' must be a non-empty list")

    labels: list[LabelDef] = []
    seen: set[str] = set()
    for entry in entries:
        try:
            label = LabelDef(
                number=entry["number"],
                id=entry["id"],
                unit=entry["unit"],
                origin=entry["origin"],
                definition=entry["definition"],
            )
        except (KeyError, TypeError) as exc:
            raise LabelSetError(f"{resolved}: malformed label entry {entry!r}") from exc
        if label.id in seen:
            raise LabelSetError(f"{resolved}: duplicate label id {label.id!r}")
        seen.add(label.id)
        labels.append(label)

    return LabelSet(version=version, labels=tuple(labels))
