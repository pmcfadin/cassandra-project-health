"""Dataset registry loader/validator for `benchmark-public` (issue #89;
DECISIONS.md D23).

`datasets_v1.yaml` is the versioned, pinned list of public datasets this
command knows about: for each one, its download URL(s) + sha256, license,
citation, which loader function in `loaders.py` reads it, the seeded sample
size to draw (from `docs/plans/2026-09-27-public-benchmark-datasets.md`'s
shortlist), and the classifier `source` venue to tag its items with for
classification purposes (see the module docstring in `loaders.py` for why
that is a *closest-venue* choice, not a literal one).

A dataset with `status: blocked` carries no working `loader`/`files` -- it is
listed so the registry (and the public report) documents *why* it's blocked
(gated download, dead link, format mismatch) without a loader ever being
invoked for it. This mirrors D23/issue #89's "if a download fails, needs a
form, or its format differs from the report, say so -- don't fabricate a
loader."
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import yaml

from project_health.classify.classifier import MessageSource

DEFAULT_REGISTRY_PATH = Path(__file__).with_name("datasets_v1.yaml")

# The only `message.source` values `questions_v1.yaml`'s state schema accepts
# (classify/questions_v1.yaml's `state_schema.message.source.enum`) -- a
# dataset's `source_venue` must be one of these, since it is sent verbatim as
# `message.source` in the Jev call (`classify/text_fetch.py`'s `build_state`).
VALID_SOURCE_VENUES: frozenset[str] = frozenset(
    {"mailing_list", "jira_comment", "github_pr_comment"}
)

VALID_STATUSES: frozenset[str] = frozenset({"working", "blocked"})


class RegistryError(ValueError):
    """Raised when `datasets_v1.yaml` fails validation."""


@dataclasses.dataclass(frozen=True)
class DatasetFile:
    url: str
    sha256: str
    filename: str


@dataclasses.dataclass(frozen=True)
class DatasetSpec:
    id: str
    name: str
    citation: str
    license: str
    status: str  # "working" | "blocked"
    blocked_reason: str | None
    files: tuple[DatasetFile, ...]
    loader: str | None  # a function name in loaders.py, or None if blocked
    categorizer: str | None  # a function name in categorize.py, or None if not set/blocked
    source_venue: str | None  # MessageSource-compatible, or None if blocked
    source_venue_rationale: str | None
    target_n: int | None
    seed: int | None
    reported_iaa: str | None
    caveats: tuple[str, ...]

    @property
    def source_venue_typed(self) -> "MessageSource":
        if self.source_venue is None:
            raise RegistryError(f"dataset {self.id!r} is blocked and has no source_venue")
        return self.source_venue  # type: ignore[return-value]


@dataclasses.dataclass(frozen=True)
class Registry:
    version: int
    datasets: dict[str, DatasetSpec]

    def working(self) -> list[DatasetSpec]:
        return [d for d in self.datasets.values() if d.status == "working"]

    def blocked(self) -> list[DatasetSpec]:
        return [d for d in self.datasets.values() if d.status == "blocked"]


def load_registry(path: str | Path | None = None) -> Registry:
    resolved = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
    with open(resolved, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise RegistryError(f"{resolved}: expected a YAML mapping at the top level")

    version = raw.get("version")
    if not isinstance(version, int):
        raise RegistryError(f"{resolved}: 'version' must be an integer")

    entries = raw.get("datasets")
    if not isinstance(entries, list) or not entries:
        raise RegistryError(f"{resolved}: 'datasets' must be a non-empty list")

    datasets: dict[str, DatasetSpec] = {}
    for entry in entries:
        spec = _parse_dataset(entry, resolved)
        if spec.id in datasets:
            raise RegistryError(f"{resolved}: duplicate dataset id {spec.id!r}")
        datasets[spec.id] = spec

    return Registry(version=version, datasets=datasets)


def _parse_dataset(entry: dict[str, Any], source_path: Path) -> DatasetSpec:
    dataset_id = entry.get("id")
    if not dataset_id or not isinstance(dataset_id, str):
        raise RegistryError(f"{source_path}: a dataset entry is missing a string 'id'")

    def require(key: str) -> Any:
        if key not in entry or entry[key] in (None, ""):
            raise RegistryError(f"{source_path}: dataset {dataset_id!r} is missing {key!r}")
        return entry[key]

    status = require("status")
    if status not in VALID_STATUSES:
        raise RegistryError(
            f"{source_path}: dataset {dataset_id!r} has status {status!r}, "
            f"expected one of {sorted(VALID_STATUSES)}"
        )

    name = require("name")
    citation = require("citation")
    license_ = require("license")
    caveats = tuple(entry.get("caveats") or ())

    if status == "blocked":
        blocked_reason = require("blocked_reason")
        return DatasetSpec(
            id=dataset_id,
            name=name,
            citation=citation,
            license=license_,
            status=status,
            blocked_reason=blocked_reason,
            files=(),
            loader=None,
            categorizer=None,
            source_venue=None,
            source_venue_rationale=None,
            target_n=entry.get("target_n"),
            seed=entry.get("seed"),
            reported_iaa=entry.get("reported_iaa"),
            caveats=caveats,
        )

    files_raw = require("files")
    if not isinstance(files_raw, list) or not files_raw:
        raise RegistryError(
            f"{source_path}: working dataset {dataset_id!r} must list at least one file"
        )
    files = tuple(
        DatasetFile(
            url=f["url"],
            sha256=f["sha256"],
            filename=f["filename"],
        )
        for f in files_raw
    )
    for f in files:
        if len(f.sha256) != 64 or any(c not in "0123456789abcdef" for c in f.sha256.lower()):
            raise RegistryError(
                f"{source_path}: dataset {dataset_id!r} file {f.filename!r} has a "
                f"malformed sha256 {f.sha256!r} (expected 64 hex chars)"
            )

    loader = require("loader")
    categorizer = entry.get("categorizer")  # optional -- see categorize.py's module docstring
    source_venue = require("source_venue")
    if source_venue not in VALID_SOURCE_VENUES:
        raise RegistryError(
            f"{source_path}: dataset {dataset_id!r} has source_venue {source_venue!r}, "
            f"expected one of {sorted(VALID_SOURCE_VENUES)} "
            "(questions_v1.yaml's message.source enum)"
        )
    source_venue_rationale = require("source_venue_rationale")
    target_n = require("target_n")
    if not isinstance(target_n, int) or target_n <= 0:
        raise RegistryError(
            f"{source_path}: dataset {dataset_id!r} target_n must be a positive int"
        )
    seed = require("seed")
    if not isinstance(seed, int):
        raise RegistryError(f"{source_path}: dataset {dataset_id!r} seed must be an int")

    return DatasetSpec(
        id=dataset_id,
        name=name,
        citation=citation,
        license=license_,
        status=status,
        blocked_reason=None,
        files=files,
        loader=loader,
        categorizer=categorizer,
        source_venue=source_venue,
        source_venue_rationale=source_venue_rationale,
        target_n=target_n,
        seed=seed,
        reported_iaa=entry.get("reported_iaa"),
        caveats=caveats,
    )
