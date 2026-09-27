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

# --- Gap-focused label set (issue #90; DECISIONS.md D23) ---------------------
#
# D23 narrows the owner/rater pilot to "a Cassandra-domain sample... [that]
# covers the labels with no public ground truth, plus a quick check that the
# public-data results hold on Cassandra's own venues." These two constants
# are that split, applied to the 12 message-level (`unit: message`) labels
# above: GAP_LABEL_IDS has no public benchmark coverage at all (D23's own
# list, verbatim), QUICK_CHECK_LABEL_IDS is the six labels D23 says the
# public benchmark already covers well or partially. Together they are
# exactly the 12 ratable ids -- `tests/test_label_set.py` asserts this stays
# true rather than silently drifting if a label is ever added or renamed.
GAP_LABEL_IDS: tuple[str, ...] = (
    "evidence_based_argument",
    "compromise_offer",
    "acknowledgment",
    "resolution_marker",
    "gatekeeping",
    "status_authority_invocation",
)
QUICK_CHECK_LABEL_IDS: tuple[str, ...] = (
    "personal_attack",
    "hostility",
    "sarcasm",
    "dismissiveness",
    "technical_disagreement",
    "constructive_counterargument",
)


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

    @property
    def gap(self) -> tuple[LabelDef, ...]:
        """The 6 gap-focused labels (D23, `GAP_LABEL_IDS`), in that order."""
        by_id = {label.id: label for label in self.ratable}
        return tuple(by_id[label_id] for label_id in GAP_LABEL_IDS if label_id in by_id)

    @property
    def quick_check(self) -> tuple[LabelDef, ...]:
        """The 6 quick-check labels (D23, `QUICK_CHECK_LABEL_IDS`), in that order."""
        by_id = {label.id: label for label in self.ratable}
        return tuple(by_id[label_id] for label_id in QUICK_CHECK_LABEL_IDS if label_id in by_id)

    def presented(self, label_set_mode: str) -> tuple[LabelDef, ...]:
        """The labels a rater is asked to mark for `label_set_mode`
        ("full" or "gap") -- issue #90.

        Both modes present all 12 ratable labels: "gap" mode does not skip
        the six public-covered labels, it only reorganizes them into a
        compact "quick check" row (label/server.py's `build_state_payload`
        splits `self.gap` from `self.quick_check` for the UI) behind the six
        gap labels' full, verbatim-definition main form. `label_set_mode`
        drives which ids `label/server.py.validate_save_payload` requires --
        parameterized on this method's result rather than hard-coded, so a
        future label_set_mode that genuinely omits some labels (and the
        "labels not presented count as missing" rule `pilot/evaluate.py`
        applies for exactly that case) works without changing the
        validation code path itself.
        """
        if label_set_mode == "gap":
            return self.gap + self.quick_check
        if label_set_mode == "full":
            return self.ratable
        raise LabelSetError(f"unknown label_set mode: {label_set_mode!r}")

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
