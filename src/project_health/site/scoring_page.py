"""Home-page composite score + dimension breakdown context builder (D20,
issue #57).

`build_scoring_page_context` turns one run's `snapshots/<run_id>/
{composite_score,dimension_status,metric_baseline_status}.parquet`
(`scoring/engine.py`'s three output tables) into everything
`templates/home.html`'s composite section needs: the 0-100 composite (never
shown alone, D20), the per-dimension score/status breakdown with its
disclosed weight renormalization, a declining-dimension flag, and a link to
the exact versioned `scoring.yaml` that produced it.

Kept out of `generate.py` for the same reason `leaderboard_page.py` and
`governance_page.py` are (their own module docstrings): this module and
`scoring/engine.py` are the only things that read/write scoring's own data,
so `generate.py`'s wiring stays a small, additive call plus one extra
template variable.

An honest no-data state (no scoring snapshot for this run, e.g. a run before
issue #57 shipped, or `compute_scoring` failed this run per its own
`manifest["scoring"]["status"] == "failed"`) still returns a valid context
with `has_data = False`, matching every other optional section on this site.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from project_health.schema import get_schema, validate

REPO_URL = "https://github.com/pmcfadin/cassandra-project-health"
SCORING_CONFIG_FILE_URL = f"{REPO_URL}/blob/main/scoring.yaml"
SCORING_SPEC_URL = f"{REPO_URL}/blob/main/docs/spec/SCORING.md"
SCORING_SPEC_COMPOSITE_ANCHOR = "12-composite-health-score-d20"


def _read_optional_table(data_dir: Path, run_id: str, filename: str, schema_name: str) -> pa.Table:
    path = Path(data_dir) / "snapshots" / run_id / filename
    if not path.is_file():
        return get_schema(schema_name).empty_table()
    return validate(schema_name, pq.read_table(path))


@dataclass(frozen=True)
class DimensionBreakdownContext:
    dimension: str
    label: str
    status: str
    score_display: str | None  # "72.4" or None (insufficient_data)
    weight_display: str  # "20%"
    renormalized_weight_display: str | None  # "25%" when re-normalized, else same as weight_display
    included: bool
    is_declining: bool
    driven_by: str | None
    key_metrics_scored: int
    key_metrics_total: int


@dataclass(frozen=True)
class ScoringPageContext:
    has_data: bool
    scoring_version: str | None
    composite_display: str | None  # "68.1" or None
    dimensions: list[DimensionBreakdownContext]
    dimensions_included: int
    dimensions_total: int
    has_declining_dimension: bool
    renormalized: bool  # True when dimensions_included < dimensions_total
    scoring_config_url: str
    scoring_spec_url: str
    scoring_spec_composite_anchor: str
    json_href: str
    csv_href: str


# Title-case labels for the home page (registry.py's dimension ids are
# lowercase, matching METRICS.md's own prose).
_DIMENSION_LABELS = {
    "contributor sustainability": "Contributor Sustainability",
    "reviewer capacity": "Reviewer Capacity",
    "responsiveness": "Responsiveness",
    "organizational diversity": "Organizational Diversity",
    "release cadence": "Release Cadence",
}


def _write_json(
    path: Path, composite_row: dict[str, Any] | None, dimension_rows: list[dict[str, Any]]
) -> None:
    payload = {
        "composite": composite_row,
        "dimensions": dimension_rows,
    }
    path.write_text(json.dumps(payload, default=str, indent=2) + "\n")


def _write_csv(path: Path, dimension_rows: list[dict[str, Any]]) -> None:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["dimension", "status", "score", "key_metrics_scored", "key_metrics_total", "driven_by"]
    )
    for row in dimension_rows:
        writer.writerow(
            [
                row["dimension"],
                row["status"],
                "" if row["score"] is None else row["score"],
                row["key_metrics_scored"],
                row["key_metrics_total"],
                row["driven_by"] or "",
            ]
        )
    path.write_text(buffer.getvalue())


def build_scoring_page_context(
    data_dir: str | Path,
    run_id: str,
    out_dir: Path,
    *,
    base_prefix: str,
) -> ScoringPageContext:
    """Build the home page's composite-score section context, and write its
    downloadable `data/scoring.json` / `.csv` files into `out_dir`."""
    data_dir = Path(data_dir)
    composite_table = _read_optional_table(
        data_dir, run_id, "composite_score.parquet", "composite_score"
    )
    dimension_table = _read_optional_table(
        data_dir, run_id, "dimension_status.parquet", "dimension_status"
    )

    composite_rows = composite_table.to_pylist()
    dimension_rows = {row["dimension"]: row for row in dimension_table.to_pylist()}

    data_out = Path(out_dir) / "data"
    data_out.mkdir(parents=True, exist_ok=True)
    json_href = f"{base_prefix}data/scoring.json"
    csv_href = f"{base_prefix}data/scoring.csv"

    composite_row = composite_rows[0] if composite_rows else None
    _write_json(data_out / "scoring.json", composite_row, list(dimension_rows.values()))
    _write_csv(data_out / "scoring.csv", list(dimension_rows.values()))

    if composite_row is None:
        return ScoringPageContext(
            has_data=False,
            scoring_version=None,
            composite_display=None,
            dimensions=[],
            dimensions_included=0,
            dimensions_total=0,
            has_declining_dimension=False,
            renormalized=False,
            scoring_config_url=SCORING_CONFIG_FILE_URL,
            scoring_spec_url=SCORING_SPEC_URL,
            scoring_spec_composite_anchor=SCORING_SPEC_COMPOSITE_ANCHOR,
            json_href=json_href,
            csv_href=csv_href,
        )

    dimension_entries = json.loads(composite_row["dimensions_json"])
    breakdown_by_dimension = {entry["dimension"]: entry for entry in dimension_entries}

    dimensions: list[DimensionBreakdownContext] = []
    for dimension, label in _DIMENSION_LABELS.items():
        entry = breakdown_by_dimension.get(dimension)
        status_row = dimension_rows.get(dimension)
        status = entry["status"] if entry else "insufficient_data"
        score = entry["score"] if entry else None
        weight = entry["weight"] if entry else 0.0
        renormalized_weight = entry.get("renormalized_weight") if entry else None
        dimensions.append(
            DimensionBreakdownContext(
                dimension=dimension,
                label=label,
                status=status,
                score_display=f"{score:.1f}" if score is not None else None,
                weight_display=f"{weight * 100:.0f}%",
                renormalized_weight_display=(
                    f"{renormalized_weight * 100:.0f}%" if renormalized_weight is not None else None
                ),
                included=bool(entry["included"]) if entry else False,
                is_declining=(status == "declining"),
                driven_by=status_row["driven_by"] if status_row else None,
                key_metrics_scored=entry["key_metrics_scored"] if entry else 0,
                key_metrics_total=entry["key_metrics_total"] if entry else 0,
            )
        )

    dimensions_included = composite_row["dimensions_included"]
    dimensions_total = composite_row["dimensions_total"]

    return ScoringPageContext(
        has_data=True,
        scoring_version=composite_row["scoring_version"],
        composite_display=(
            f"{composite_row['composite']:.1f}" if composite_row["composite"] is not None else None
        ),
        dimensions=dimensions,
        dimensions_included=dimensions_included,
        dimensions_total=dimensions_total,
        has_declining_dimension=bool(composite_row["has_declining_dimension"]),
        renormalized=dimensions_included < dimensions_total,
        scoring_config_url=SCORING_CONFIG_FILE_URL,
        scoring_spec_url=SCORING_SPEC_URL,
        scoring_spec_composite_anchor=SCORING_SPEC_COMPOSITE_ANCHOR,
        json_href=json_href,
        csv_href=csv_href,
    )
