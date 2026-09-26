"""Corpus reading and append-only label storage for the local labeling tool
(DECISIONS.md D18, D22; issue #46; corpus schema per issue #44).

Corpus JSONL (one record per line, private benchmark checkout, never
committed to the public repo -- D18): `id, stratum, source, archive_url,
text, parent_text, checksum`. `stratum` and the item's own `checksum` are
read here but never sent to the rater-facing HTTP layer (`server.py`'s
`blind_item_view`) -- the rating must be blind to which sampling stratum
(prevalence vs. rare-label enrichment) an item came from.

Label JSONL is append-only: one JSON object per save, `item_id, rater,
labels{}, tone, note, seconds, saved_at, corpus_checksum,
label_set_version`. Nothing is ever rewritten in place -- an edit is a new
record appended for the same `item_id`, and `read_label_records` folds a
file down to "the latest record per item_id" by taking each line in file
order and letting later lines win, so a restart resumes correctly and a
corrected rating is safe to append at any time.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any


class CorpusError(ValueError):
    """Raised when a corpus JSONL file is missing a required field or is malformed."""


_REQUIRED_CORPUS_FIELDS = ("id", "stratum", "source", "archive_url", "text", "parent_text")


@dataclasses.dataclass(frozen=True)
class CorpusItem:
    """One row of the pilot corpus (issue #44's schema)."""

    id: str
    stratum: str
    source: str
    archive_url: str
    text: str
    parent_text: str | None
    checksum: str | None


def checksum_file(path: Path | str) -> str:
    """sha256 hex digest of the corpus file's raw bytes -- recorded as
    `corpus_checksum` on every label record so labels are traceable to the
    exact frozen corpus version they were rated against (COMMUNITY-HEALTH.md
    §6.1)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_corpus(path: Path | str) -> list[CorpusItem]:
    """Load and validate the corpus JSONL, preserving file order (the order
    labeling proceeds in, and what `first_unlabeled_index` resumes against).

    Never raises with message text in the exception -- only line numbers and
    field names -- so a caller printing/logging this error can't leak a
    corpus message body (issue #46: "never log message text")."""
    items: list[CorpusItem] = []
    seen_ids: set[str] = set()
    with open(path, encoding="utf-8") as fh:
        for line_no, raw_line in enumerate(fh, start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise CorpusError(f"corpus line {line_no}: invalid JSON ({exc.msg})") from exc
            if not isinstance(record, dict):
                raise CorpusError(f"corpus line {line_no}: expected a JSON object")
            missing = [field for field in _REQUIRED_CORPUS_FIELDS if field not in record]
            if missing:
                raise CorpusError(f"corpus line {line_no}: missing field(s) {missing}")
            item_id = str(record["id"])
            if item_id in seen_ids:
                raise CorpusError(f"corpus line {line_no}: duplicate id {item_id!r}")
            seen_ids.add(item_id)
            items.append(
                CorpusItem(
                    id=item_id,
                    stratum=str(record["stratum"]),
                    source=str(record["source"]),
                    archive_url=str(record["archive_url"]),
                    text=str(record["text"]),
                    parent_text=(
                        None if record["parent_text"] is None else str(record["parent_text"])
                    ),
                    checksum=(
                        None if record.get("checksum") is None else str(record["checksum"])
                    ),
                )
            )
    if not items:
        raise CorpusError("corpus is empty")
    return items


def read_label_records(path: Path | str) -> dict[str, dict[str, Any]]:
    """Read an append-only label JSONL file and fold it down to the latest
    record per `item_id` (later lines in the file win). Returns `{}` if the
    file doesn't exist yet (first run for this rater)."""
    resolved = Path(path)
    if not resolved.is_file():
        return {}
    records: dict[str, dict[str, Any]] = {}
    with open(resolved, encoding="utf-8") as fh:
        for raw_line in fh:
            stripped = raw_line.strip()
            if not stripped:
                continue
            record = json.loads(stripped)
            records[record["item_id"]] = record
    return records


def append_label_record(path: Path | str, record: dict[str, Any]) -> None:
    """Append one JSON line to the label file, creating parent dirs as needed.
    Never rewrites or truncates existing content -- append-only by
    construction (opened with mode "a")."""
    resolved = Path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    with open(resolved, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False))
        fh.write("\n")


def first_unlabeled_index(
    items: list[CorpusItem], labeled_ids: set[str]
) -> int | None:
    """The index (in corpus order) of the first item not in `labeled_ids`, or
    `None` if every item already has a label record -- "resumes at the first
    unlabeled item" (issue #46)."""
    for index, item in enumerate(items):
        if item.id not in labeled_ids:
            return index
    return None
