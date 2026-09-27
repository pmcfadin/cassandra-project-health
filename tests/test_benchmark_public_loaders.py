"""Tests for project_health.benchmark_public.loaders (issue #89).

Every test builds a tiny synthetic file in each dataset's *actual* on-disk
format (verified against the real downloaded files during implementation --
see loaders.py's per-loader docstrings for the exact column names/paths) --
never real dataset content, and no network access.
"""

from __future__ import annotations

import csv
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from project_health.benchmark_public.loaders import (
    load_ferreira_github,
    load_ferreira_lkml,
    load_talkdown,
    load_toxicr,
    load_wikipedia_attacks,
)

# --- Ferreira (shared zip layout for both lkml and gh) ------------------------------


def _write_csv(zf: zipfile.ZipFile, path: str, rows: list[dict]) -> None:
    if not rows:
        return
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    zf.writestr(path, buf.getvalue())


def _build_ferreira_zip(tmp_path: Path, venue_dir: str) -> Path:
    id_col = "email_id" if venue_dir == "lkml" else "comment_id"
    thread_col = "thread_id" if venue_dir == "lkml" else "issue_id"
    body_col = "email_body" if venue_dir == "lkml" else "comment_body"
    prev_body_col = "previous_email_body" if venue_dir == "lkml" else "previous_comment_body"
    prefix = "incivility_detection/data"

    zip_path = tmp_path / "replication_package.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        _write_csv(
            zf,
            f"{prefix}/{venue_dir}/uncivil.csv",
            [
                {thread_col: "t1", id_col: "e1", "quotation": "you clearly have no clue",
                 "quotation_tbdf": "name_calling"},
                {thread_col: "t1", id_col: "e1", "quotation": "this is a waste of time",
                 "quotation_tbdf": "bitter_frustration"},
                {thread_col: "t2", id_col: "e2", "quotation": "oh sure, great idea",
                 "quotation_tbdf": "mocking"},
            ],
        )
        _write_csv(
            zf,
            f"{prefix}/{venue_dir}/civil.csv",
            [
                {thread_col: "t3", id_col: "e3", "quotation": "good catch, thanks",
                 "quotation_tbdf": "humility"},
            ],
        )
        _write_csv(
            zf,
            f"{prefix}/{venue_dir}/technical.csv",
            [
                {
                    thread_col: "t4",
                    id_col: "e4",
                    "ahlaam_preprocessed_text": "the lock should be per-shard.",
                    "original_text": "On Mon, X wrote:  the lock should be per-shard.",
                    "email_classification": "technical",
                },
            ],
        )
        _write_csv(
            zf,
            f"{prefix}/{venue_dir}/non_technical.csv",
            [
                {
                    thread_col: "t5",
                    id_col: "e5",
                    "ahlaam_preprocessed_text": "thanks for the update",
                    "original_text": "On Tue, Y wrote:  thanks for the update",
                    "email_classification": "not_technical",
                },
            ],
        )
        _write_csv(
            zf,
            f"{prefix}/context_{venue_dir}/uncivil.csv",
            [
                {
                    thread_col: "t1",
                    id_col: "e1",
                    body_col: "you clearly have no clue. this is a waste of time",
                    prev_body_col: "what do you think about this approach?",
                },
                {
                    thread_col: "t2",
                    id_col: "e2",
                    body_col: "oh sure, great idea",
                    prev_body_col: "",
                },
            ],
        )
        _write_csv(
            zf,
            f"{prefix}/context_{venue_dir}/civil.csv",
            [
                {
                    thread_col: "t3",
                    id_col: "e3",
                    body_col: "good catch, thanks",
                    prev_body_col: "I found a bug in the flush path.",
                },
            ],
        )
    return zip_path


@pytest.mark.parametrize(
    "venue_dir,loader",
    [("lkml", load_ferreira_lkml), ("gh", load_ferreira_github)],
)
def test_ferreira_loader_builds_items_with_tbdf_rollup_and_context(
    tmp_path: Path, venue_dir: str, loader
) -> None:
    zip_path = _build_ferreira_zip(tmp_path, venue_dir)
    items = loader([zip_path])
    by_id = {item.item_id: item for item in items}

    assert set(by_id) == {"e1", "e2", "e3", "e4", "e5"}

    # e1 has two quotations, both TBDF-coded -- rolled up into one item with
    # both categories in raw_labels, full text/parent from the context file.
    assert by_id["e1"].raw_labels["tbdf_categories"] == frozenset(
        {"name_calling", "bitter_frustration"}
    )
    assert by_id["e1"].text == "you clearly have no clue. this is a waste of time"
    assert by_id["e1"].parent_text == "what do you think about this approach?"

    # e2's context row has an empty previous_email_body -- a thread root, parent=None.
    assert by_id["e2"].parent_text is None

    # e3 is civil-coded (humility), still gets its tbdf category recorded.
    assert by_id["e3"].raw_labels["tbdf_categories"] == frozenset({"humility"})

    # e4/e5 have no TBDF quotation at all -- empty category set, topic-classified,
    # text falls back to ahlaam_preprocessed_text (not original_text -- see
    # loaders.py's docstring on why), and no parent (technical.csv has none).
    assert by_id["e4"].raw_labels["tbdf_categories"] == frozenset()
    assert by_id["e4"].raw_labels["email_classification"] == "technical"
    assert by_id["e4"].text == "the lock should be per-shard."
    assert by_id["e4"].parent_text is None
    assert by_id["e5"].raw_labels["email_classification"] == "not_technical"

    # e1/e2/e3 (context-file items) have no topic classification at all.
    assert by_id["e1"].raw_labels["email_classification"] is None


# --- ToxiCR ---------------------------------------------------------------------------


def _build_toxicr_xlsx(tmp_path: Path) -> Path:
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["message", "is_toxic"])
    ws.append(["nit: rename this variable", 0])
    ws.append(["you are an idiot and should quit", 1])
    ws.append([None, None])  # a blank trailing row, as real exports sometimes have
    path = tmp_path / "code-review-dataset-full.xlsx"
    wb.save(path)
    return path


def test_toxicr_loader(tmp_path: Path) -> None:
    path = _build_toxicr_xlsx(tmp_path)
    items = load_toxicr([path])
    assert len(items) == 2  # the blank row is skipped
    assert items[0].text == "nit: rename this variable"
    assert items[0].raw_labels == {"is_toxic": 0}
    assert items[0].parent_text is None
    assert items[1].raw_labels == {"is_toxic": 1}


# --- TalkDown ---------------------------------------------------------------------------


def _build_talkdown_targz(tmp_path: Path) -> Path:
    rows = [
        {
            "reddit_comment_id": "c1",
            "reddit_reply_id": "r1",
            "comment": "I think tabs are better than spaces.",
            "reply": "Oh, you must be very smart to know that.",
            "label": True,
        },
        {
            "reddit_comment_id": "c2",
            "reddit_reply_id": "r2",
            "comment": "I prefer spaces.",
            "reply": "Sure, that makes sense to me too.",
            "label": False,
        },
    ]
    jsonl_path = tmp_path / "annotated.jsonl"
    with open(jsonl_path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")

    targz_path = tmp_path / "talkdown.tar.gz"
    with tarfile.open(targz_path, "w:gz") as tf:
        tf.add(jsonl_path, arcname="annotated.jsonl")
    return targz_path


def test_talkdown_loader(tmp_path: Path) -> None:
    path = _build_talkdown_targz(tmp_path)
    items = load_talkdown([path])
    by_id = {item.item_id: item for item in items}
    assert by_id["r1"].text == "Oh, you must be very smart to know that."
    assert by_id["r1"].parent_text == "I think tabs are better than spaces."
    assert by_id["r1"].raw_labels == {"label": True}
    assert by_id["r2"].raw_labels == {"label": False}


# --- Wikipedia attacks ------------------------------------------------------------------


def _build_wikipedia_tsvs(tmp_path: Path) -> tuple[Path, Path]:
    comments_path = tmp_path / "attack_annotated_comments.tsv"
    with open(comments_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t")
        writer.writerow(["rev_id", "comment", "year", "logged_in", "ns", "sample", "split"])
        writer.writerow(
            ["1", "This is a fine edit.NEWLINE_TOKENThanks.", "2010", "True", "article",
             "random", "train"]
        )
        writer.writerow(
            ["2", "You are a complete idiot.", "2011", "False", "user", "blocked", "test"]
        )

    annotations_path = tmp_path / "attack_annotations.tsv"
    with open(annotations_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t")
        writer.writerow(
            ["rev_id", "worker_id", "quoting_attack", "recipient_attack", "third_party_attack",
             "other_attack", "attack"]
        )
        # rev_id 1: 3 workers, majority say no attack.
        writer.writerow(["1", "w1", "0.0", "0.0", "0.0", "0.0", "0.0"])
        writer.writerow(["1", "w2", "0.0", "0.0", "0.0", "0.0", "0.0"])
        writer.writerow(["1", "w3", "0.0", "0.0", "0.0", "0.0", "1.0"])
        # rev_id 2: 2 workers, majority say attack.
        writer.writerow(["2", "w1", "0.0", "1.0", "0.0", "0.0", "1.0"])
        writer.writerow(["2", "w2", "0.0", "0.0", "0.0", "0.0", "1.0"])
    return comments_path, annotations_path


def test_wikipedia_attacks_loader(tmp_path: Path) -> None:
    comments_path, annotations_path = _build_wikipedia_tsvs(tmp_path)
    items = load_wikipedia_attacks([comments_path, annotations_path])
    by_id = {item.item_id: item for item in items}

    assert by_id["1"].text == "This is a fine edit.\nThanks."  # NEWLINE_TOKEN decoded
    assert by_id["1"].raw_labels == {"attack": False}  # 1/3 mean = 0.33, not > 0.5
    assert by_id["1"].parent_text is None

    assert by_id["2"].raw_labels == {"attack": True}  # 2/2 mean = 1.0
