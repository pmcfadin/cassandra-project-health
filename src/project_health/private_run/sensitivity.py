"""Public-benchmark sensitivity thresholds for the private Cassandra
communication run (issue #110; DECISIONS.md D23; `docs/benchmark/
public-v1.md`).

D23 names exactly three labels with a *strong* public label mapping:
`personal_attack`, `hostility`, `sarcasm` ("Scope of public ground truth:
strong for personal_attack, hostility and sarcasm"). This module parses
`docs/benchmark/public-v1.md`'s per-dataset markdown tables (rendered by
`benchmark_public/report.py`, `project-health benchmark-public`) for rows
whose `Strength` column is `strong` *and* whose `Gate role` column is
`gating` (a `strong` mapping that D23 nonetheless treats as
informative-only -- e.g. Wikipedia Personal Attacks, whose own inter-rater
alpha is below this project's 0.667 usable floor -- is never picked here),
and reads off each such row's `Best-F1 threshold` and `F1` cells.

More than one public dataset can carry a strong, gating mapping for the
same label (as of `public-v1.md` v1, both the Ferreira LKML and Ferreira
GitHub-locked-issues datasets do, for all three of D23's strong labels).
This module's deterministic tie-break: **pick the threshold from whichever
qualifying row reports the higher F1** (ties broken by the lower
threshold) -- the best-performing public calibration wins. This is a
parsing choice, not a hardcoded number, so a future `public-v1.md` update
(a new dataset, a re-run with different sample draws) is picked up
automatically the next time `private-run` runs, with no code change.

Issue #110 fixup round 1: each picked threshold now also records **which
dataset it came from** (the `## <dataset name>` markdown heading the
qualifying row appeared under), so a reader of the private report can see
"this cutoff is calibrated against dataset X", not just a bare number --
and whether that cutoff is *permissive* (a low threshold flags more
messages as "present", i.e. counts more of the classifier's raw output as
a positive), so a low, dataset-calibrated cutoff (e.g. `personal_attack`'s
0.15, from the LKML Ferreira set) isn't mistaken for a strict one.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

# D23, verbatim: "strong for personal_attack, hostility and sarcasm."
STRONG_GATING_LABELS: frozenset[str] = frozenset({"personal_attack", "hostility", "sarcasm"})

# A cutoff below this is flagged "permissive" in the report (module
# docstring): an explicit, documented, if inherently somewhat arbitrary,
# line -- a threshold below 0.3 counts a message as "present" at less than
# 30% Jev-assessed probability, which reads as a low bar relative to the
# 0.5/0.7/0.9 fixed cutoffs every label is also reported at (aggregate.py).
PERMISSIVE_THRESHOLD_MAX = 0.3

# src/project_health/private_run/sensitivity.py -> private_run -> project_health
# -> src -> <repo root> -- same depth/convention as
# `classify/text_fetch.py`'s `_repo_root`.
DEFAULT_PUBLIC_BENCHMARK_PATH = (
    Path(__file__).resolve().parents[3] / "docs" / "benchmark" / "public-v1.md"
)

_LABEL_CELL_RE = re.compile(r"^`([a-z_]+)`$")
_LEADING_FLOAT_RE = re.compile(r"[-+]?\d+\.\d+|[-+]?\d+")
_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")


@dataclasses.dataclass(frozen=True)
class SensitivityThreshold:
    label_id: str
    threshold: float
    f1: float
    dataset_name: str

    @property
    def permissive(self) -> bool:
        return self.threshold < PERMISSIVE_THRESHOLD_MAX


def _parse_table_row(line: str) -> list[str] | None:
    """`[cell, ...]` for one markdown table row, or `None` if `line` isn't
    a table row at all (doesn't start and end with `|`)."""
    stripped = line.strip()
    if len(stripped) < 2 or not stripped.startswith("|") or not stripped.endswith("|"):
        return None
    return [cell.strip() for cell in stripped[1:-1].split("|")]


def parse_gating_thresholds(markdown: str) -> dict[str, SensitivityThreshold]:
    """`{label_id: SensitivityThreshold}` for every label in
    `STRONG_GATING_LABELS` that has at least one `strong` + `gating` row in
    `markdown`'s per-dataset tables (module docstring's tie-break rule). A
    label with no such row at all (e.g. `public-v1.md` hasn't been
    (re)generated, or a future benchmark drops a dataset) is simply absent
    from the result -- callers must treat a missing key as "no sensitivity
    threshold available for this label yet", never as `0.0`.

    `dataset_name` is read from the nearest preceding `## <heading>`
    markdown heading (`public-v1.md`'s own per-dataset section headings,
    e.g. "Ferreira, Cheng & Adams -- LKML incivility... (2021)") -- tracked
    by a single forward scan, so a row's dataset is whichever section it
    physically appears under in the file.
    """
    # label -> [(f1, threshold, dataset_name), ...] across every qualifying row.
    candidates: dict[str, list[tuple[float, float, str]]] = {}
    current_heading = ""
    for line in markdown.splitlines():
        heading_match = _HEADING_RE.match(line)
        if heading_match is not None:
            current_heading = heading_match.group(1)
            continue

        cells = _parse_table_row(line)
        if cells is None or len(cells) < 7:
            continue
        label_match = _LABEL_CELL_RE.match(cells[0])
        if label_match is None:
            continue
        label = label_match.group(1)
        if label not in STRONG_GATING_LABELS:
            continue
        if cells[1].lower() != "strong" or cells[2].lower() != "gating":
            continue
        threshold_match = _LEADING_FLOAT_RE.match(cells[3])
        # F1 is column index 6 ("Best-F1 threshold" is 3, "Precision" 4,
        # "Recall" 5, "F1" 6 -- see docs/benchmark/public-v1.md's own
        # header row, e.g. "| Our label | Strength | Gate role | Best-F1
        # threshold | Precision (95% CI) | Recall (95% CI) | F1 (95% CI) |
        # ...").
        f1_match = _LEADING_FLOAT_RE.match(cells[6])
        if threshold_match is None or f1_match is None:
            continue
        f1 = float(f1_match.group())
        threshold = float(threshold_match.group())
        candidates.setdefault(label, []).append((f1, threshold, current_heading))

    result: dict[str, SensitivityThreshold] = {}
    for label, entries in candidates.items():
        best_f1, best_threshold, best_dataset = max(entries, key=lambda e: (e[0], -e[1]))
        result[label] = SensitivityThreshold(
            label_id=label, threshold=best_threshold, f1=best_f1, dataset_name=best_dataset
        )
    return result


def load_gating_thresholds(path: str | Path | None = None) -> dict[str, SensitivityThreshold]:
    """`parse_gating_thresholds` over the file at `path` (default:
    `docs/benchmark/public-v1.md`). Returns `{}` (never raises) if the file
    doesn't exist yet -- a private run before `benchmark-public` has ever
    been generated simply has no sensitivity column, which the report
    renders as `n/a`."""
    resolved = Path(path) if path is not None else DEFAULT_PUBLIC_BENCHMARK_PATH
    if not resolved.is_file():
        return {}
    return parse_gating_thresholds(resolved.read_text(encoding="utf-8"))
