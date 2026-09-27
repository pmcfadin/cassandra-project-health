"""Per-dataset loaders for `benchmark-public` (issue #89; DECISIONS.md D23).

Each loader takes the local, already-downloaded-and-verified file path(s)
for one dataset (`download.fetch_pinned`, driven by `registry.py`) and
returns a list of `DatasetItem` -- this project's own normalized shape,
independent of any one dataset's on-disk format. Nothing here does network
I/O; `runner.py` is the only caller, and it downloads first, then loads.

A loader is intentionally **not written** for a dataset the registry marks
`status: blocked` (issue #89: "If a download fails, needs a form, or its
format differs from the report, say so. Don't fabricate a loader."). See
`datasets_v1.yaml`'s `blocked_reason` for each one and
`docs/benchmark/public-v1.md`'s "Datasets not run" section for the public
writeup.

`DatasetItem.item_id` is the dataset's own row/example identifier (a figshare
row index, a GitHub repo's line number, a Wikipedia `rev_id`, ...) --
**internal only**. It is used as part of the classifier cache key
(`runner.py`) and the private sampling manifest, but per COMMUNITY-HEALTH.md
§7 / D23 ("no item text or ids") it must never reach the public report
(`report.py`'s leak test, `tests/test_benchmark_public_report.py`, checks
this by construction).
"""

from __future__ import annotations

import csv
import json
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

LoaderFn = Callable[[Sequence[Path]], "list[DatasetItem]"]

_LOADERS: dict[str, LoaderFn] = {}


def register_loader(name: str) -> Callable[[LoaderFn], LoaderFn]:
    def decorator(fn: LoaderFn) -> LoaderFn:
        if name in _LOADERS:
            raise ValueError(f"loader {name!r} already registered")
        _LOADERS[name] = fn
        return fn

    return decorator


def get_loader(name: str) -> LoaderFn:
    try:
        return _LOADERS[name]
    except KeyError:
        raise KeyError(
            f"no loader registered as {name!r}; known loaders: {sorted(_LOADERS)}"
        ) from None


@dataclass(frozen=True)
class DatasetItem:
    """One normalized example from a public dataset.

    `raw_labels` keeps the dataset's own label field(s) verbatim (e.g.
    `{"tbdf_category": "name_calling"}`, `{"is_toxic": 1}`) -- `mapping.py`'s
    `is_positive` reads directly from this dict, so a loader's only
    obligation is to expose whatever raw field(s) `label_mapping_v1.yaml`
    names for that dataset, under exactly those names.
    """

    item_id: str
    text: str
    parent_text: str | None
    raw_labels: dict[str, Any] = field(default_factory=dict)


# --- Small shared helpers (CSV/JSON/zip reading) ------------------------------------


def read_csv_rows(path: str | Path, *, encoding: str = "utf-8") -> list[dict[str, str]]:
    with open(path, encoding=encoding, newline="") as fh:
        return list(csv.DictReader(fh))


def read_tsv_rows(path: str | Path, *, encoding: str = "utf-8") -> list[dict[str, str]]:
    with open(path, encoding=encoding, newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def read_json(path: str | Path, *, encoding: str = "utf-8") -> Any:
    with open(path, encoding=encoding) as fh:
        return json.load(fh)


def read_jsonl(path: str | Path, *, encoding: str = "utf-8") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with open(path, encoding=encoding) as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def extract_zip_member(zip_path: str | Path, member: str, dest_dir: str | Path) -> Path:
    """Extract one `member` from a zip archive into `dest_dir`, returning its
    path. Used by loaders whose pinned download is a zip of several files."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extract(member, dest_dir)
    return dest_dir / member


def by_filename(paths: Sequence[Path]) -> dict[str, Path]:
    return {p.name: p for p in paths}


# --- Ferreira et al. incivility trilogy (figshare 24603237; issue #89) -------------
#
# One pinned zip (`replication_package.zip`, CC BY 4.0 per the figshare article's own
# `license.name` -- verified live, 2026-09-27) holds both the LKML and GitHub datasets
# under `incivility_detection/data/{lkml,gh}/` (sentence-level TBDF quotations,
# `{civil,uncivil}.csv`, plus a message-level topic classification,
# `{technical,non_technical}.csv`) and `incivility_detection/data/context_{lkml,gh}/`
# (full message + immediate-parent text, joined back to the quotation files by
# `email_id`/`comment_id` -- the join is intentionally not 1:1: not every quotation-
# level row has a matching context row, and vice versa, exactly as
# `docs/plans/2026-09-27-public-benchmark-datasets.md` §2.1's "no parent/context text"
# caveat for the *sentence-level* files predicts).
#
# `_load_ferreira` builds one `DatasetItem` per unique message (email/comment), not
# per quotation -- Jev classifies the whole message, not an isolated sentence -- with
# `raw_labels`:
#   - `tbdf_categories`: the `frozenset` of every TBDF category (from *both*
#     civil.csv and uncivil.csv) any quotation of this message was coded with, or an
#     empty frozenset if the message was never TBDF-coded at all (`mapping.is_positive`
#     treats a `frozenset` raw value as "positive if it intersects `positive_values`",
#     see `mapping.py`).
#   - `email_classification`: `"technical"`/`"not_technical"` from the topic
#     classification files, or `None` if this message isn't in either.
# Full text/parent come from the context files when available (the TBDF-coded
# majority); the topic-classification files' `original_text` is used as a fallback
# for messages only in `technical.csv`/`non_technical.csv` (which carry no parent
# field at all -- `parent_text=None` for those, a valid thread-root state).


def _read_zip_csv(zip_path: Path, member: str, extract_dir: Path) -> list[dict[str, Any]]:
    dest = extract_dir / member
    if not dest.is_file():
        extract_zip_member(zip_path, member, extract_dir)
    return read_csv_rows(dest)


def _load_ferreira(paths: Sequence[Path], venue_dir: str) -> list[DatasetItem]:
    """`venue_dir` is `"lkml"` or `"gh"` -- the subdirectory name both
    `data/<venue_dir>/` and `data/context_<venue_dir>/` share."""
    zip_path = paths[0]
    extract_dir = zip_path.parent / "extracted"
    prefix = "incivility_detection/data"

    id_col = "email_id" if venue_dir == "lkml" else "comment_id"
    body_col = "email_body" if venue_dir == "lkml" else "comment_body"
    prev_body_col = "previous_email_body" if venue_dir == "lkml" else "previous_comment_body"

    tbdf_by_id: dict[str, set[str]] = {}
    for tbdf_file in ("uncivil.csv", "civil.csv"):
        for row in _read_zip_csv(zip_path, f"{prefix}/{venue_dir}/{tbdf_file}", extract_dir):
            tbdf_by_id.setdefault(row[id_col], set()).add(row["quotation_tbdf"])

    context_by_id: dict[str, tuple[str, str | None]] = {}
    for context_file in ("uncivil.csv", "civil.csv"):
        path = f"{prefix}/context_{venue_dir}/{context_file}"
        for row in _read_zip_csv(zip_path, path, extract_dir):
            parent = row.get(prev_body_col) or None
            context_by_id[row[id_col]] = (row[body_col], parent)

    classification_by_id: dict[str, tuple[str, str]] = {}
    for topic_file in ("technical.csv", "non_technical.csv"):
        for row in _read_zip_csv(zip_path, f"{prefix}/{venue_dir}/{topic_file}", extract_dir):
            # `ahlaam_preprocessed_text`, not `original_text` -- `original_text` keeps
            # a dangling "On <date>, <name> wrote:" attribution line (Ferreira's own
            # dataset construction artifact) with the real reply glued onto the same
            # line, which this project's own `preprocess_text` (designed for raw,
            # freshly-fetched Pony Mail bodies) then treats as "everything from here
            # on is quoted" and strips entirely -- verified against this exact zip:
            # using `original_text` empties out 926/1495 lkml items and 154/2000 gh
            # items after `preprocess_text`, vs. 0 using this field.
            classification_by_id[row[id_col]] = (
                row["email_classification"],
                row["ahlaam_preprocessed_text"],
            )

    all_ids = set(context_by_id) | set(classification_by_id)
    items: list[DatasetItem] = []
    for item_id in sorted(all_ids):
        classification = classification_by_id.get(item_id)
        if item_id in context_by_id:
            text, parent_text = context_by_id[item_id]
        else:
            text, parent_text = classification[1], None  # type: ignore[index]
        items.append(
            DatasetItem(
                item_id=item_id,
                text=text,
                parent_text=parent_text,
                raw_labels={
                    "tbdf_categories": frozenset(tbdf_by_id.get(item_id, ())),
                    "email_classification": classification[0] if classification else None,
                },
            )
        )
    return items


@register_loader("load_ferreira_lkml")
def load_ferreira_lkml(paths: Sequence[Path]) -> list[DatasetItem]:
    return _load_ferreira(paths, "lkml")


@register_loader("load_ferreira_github")
def load_ferreira_github(paths: Sequence[Path]) -> list[DatasetItem]:
    return _load_ferreira(paths, "gh")


# --- ToxiCR (WSU-SEAL/ToxiCR; issue #89) --------------------------------------------
#
# `models/code-review-dataset-full.xlsx`, GPL-3.0 (repo `LICENSE`, confirmed via the
# GitHub API's `license.spdx_id`, 2026-09-27) -- one sheet, columns `message`,
# `is_toxic` (0/1). No parent/context column at all
# (`docs/plans/2026-09-27-public-benchmark-datasets.md` §2.1's caveat, confirmed).


@register_loader("load_toxicr")
def load_toxicr(paths: Sequence[Path]) -> list[DatasetItem]:
    import openpyxl

    workbook = openpyxl.load_workbook(paths[0], read_only=True, data_only=True)
    sheet = workbook[workbook.sheetnames[0]]
    rows = sheet.iter_rows(values_only=True)
    header = next(rows)
    col = {name: i for i, name in enumerate(header)}
    items: list[DatasetItem] = []
    for i, row in enumerate(rows):
        message = row[col["message"]]
        if message is None:
            continue
        is_toxic = row[col["is_toxic"]]
        items.append(
            DatasetItem(
                item_id=f"row_{i}",
                text=str(message),
                parent_text=None,
                raw_labels={"is_toxic": int(is_toxic) if is_toxic is not None else 0},
            )
        )
    workbook.close()
    return items


# --- TalkDown (zijwang/talkdown; issue #89) -----------------------------------------
#
# `talkdown.tar.gz` -> `annotated.jsonl`, AGPL-3.0 (repo-level `LICENSE`; no separate
# data license). 4,991 Reddit comment/reply pairs, `reply` (the message to judge),
# `comment` (its immediate parent -- exactly this project's message+parent design),
# `label` (bool, condescending/not). `reddit_reply_id` is TalkDown's own stable id.


@register_loader("load_talkdown")
def load_talkdown(paths: Sequence[Path]) -> list[DatasetItem]:
    extract_dir = paths[0].parent / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)
    member_path = extract_dir / "annotated.jsonl"
    if not member_path.is_file():
        with tarfile.open(paths[0], "r:gz") as tf:
            tf.extract("annotated.jsonl", extract_dir, filter="data")

    items: list[DatasetItem] = []
    for row in read_jsonl(member_path):
        items.append(
            DatasetItem(
                item_id=str(row["reddit_reply_id"]),
                text=row["reply"],
                parent_text=row.get("comment") or None,
                raw_labels={"label": bool(row["label"])},
            )
        )
    return items


# --- Wikipedia Personal Attacks (Wulczyn, Thain & Dixon 2017; issue #89) ------------
#
# Two figshare files (article 4054689, CC0 per the figshare API's `license.name`,
# confirmed live 2026-09-27): `attack_annotated_comments.tsv` (one row per comment,
# `rev_id`, `comment` -- `NEWLINE_TOKEN` in place of real newlines) and
# `attack_annotations.tsv` (one row per (comment, crowd worker), ~10 workers/comment,
# `attack` in {0.0, 1.0}). Ground truth follows the paper's own convention: a comment
# is a positive `attack` example if `mean(attack across its workers) > 0.5`. No
# parent text -- these are independent talk-page comments, not a reply chain.


@register_loader("load_wikipedia_attacks")
def load_wikipedia_attacks(paths: Sequence[Path]) -> list[DatasetItem]:
    comments_path, annotations_path = None, None
    for path in paths:
        if "annotations" in path.name:
            annotations_path = path
        else:
            comments_path = path
    assert comments_path is not None and annotations_path is not None

    attack_sums: dict[str, float] = {}
    attack_counts: dict[str, int] = {}
    for row in read_tsv_rows(annotations_path):
        rev_id = row["rev_id"]
        attack_sums[rev_id] = attack_sums.get(rev_id, 0.0) + float(row["attack"])
        attack_counts[rev_id] = attack_counts.get(rev_id, 0) + 1

    items: list[DatasetItem] = []
    for row in read_tsv_rows(comments_path):
        rev_id = row["rev_id"]
        text = row["comment"].replace("NEWLINE_TOKEN", "\n").replace("TAB_TOKEN", "\t")
        n = attack_counts.get(rev_id, 0)
        mean_attack = (attack_sums.get(rev_id, 0.0) / n) if n else 0.0
        items.append(
            DatasetItem(
                item_id=rev_id,
                text=text,
                parent_text=None,
                raw_labels={"attack": mean_attack > 0.5},
            )
        )
    return items
