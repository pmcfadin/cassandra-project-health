"""Local labeling HTTP server (DECISIONS.md D18, D22; issue #46).

stdlib-only (`http.server`), bound to **127.0.0.1 only** -- never
`0.0.0.0` -- serving a static, vanilla-JS page (`static/`, package data,
no CDN) that reads the private corpus and appends to a per-rater label
JSONL file. There is no LLM/Jev call anywhere in this module (D22): the
rating is blind by construction because the corpus's `stratum` field (and
any Jev output, which does not exist in this tool's data model at all)
never leaves `build_state_payload`'s corpus item -> JSON translation.

Request handling is split into two layers on purpose:

- Pure functions (`build_state_payload`, `validate_save_payload`,
  `make_label_record`) contain all the actual logic and are exercised
  directly by `tests/test_label_server.py` with no sockets involved.
- `LabelRequestHandler` (a thin `BaseHTTPRequestHandler` subclass) wires
  those functions to real HTTP GET/POST -- verified by hand against a
  real running server (see the issue's acceptance criteria), not by the
  test suite's autouse network block (`tests/conftest.py`), which
  deliberately forbids real socket I/O in `pytest`.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from project_health.label.label_set import LabelSet, load_label_set
from project_health.label.question_set import QuestionSetSummary, load_question_set_summary
from project_health.label.store import (
    CorpusItem,
    append_label_record,
    checksum_file,
    load_corpus,
    read_label_records,
)

STATIC_DIR = Path(__file__).with_name("static")

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}

_VALID_MARKS = frozenset({"yes", "no", "unsure"})


class SavePayloadError(ValueError):
    """Raised by `validate_save_payload` for a malformed/invalid POST body."""


class LabelQuestionMismatchError(ValueError):
    """Raised when the packaged label set's ratable ids don't exactly match
    `classify/questions_v1.yaml`'s Noul label ids -- the two must stay in
    lockstep so a human rating and a Jev classification answer the same
    question (orchestrator correction, issue #46)."""


@dataclasses.dataclass
class AppState:
    """In-memory server state for one `project-health label` run.

    `records` is loaded once at startup (`read_label_records`, folding the
    append-only file down to "latest per item_id") and then updated in
    memory as saves come in, so a long run doesn't re-parse the whole label
    file on every request -- the file on disk is still the append-only
    source of truth, and a restart re-derives this same dict from it.
    """

    items: list[CorpusItem]
    label_set: LabelSet
    question_set: QuestionSetSummary
    labels_path: Path
    rater: str
    corpus_checksum: str
    records: dict[str, dict[str, Any]]


def build_state_payload(app: AppState) -> dict[str, Any]:
    """The full JSON payload served at `GET /api/state`.

    Blind by construction: the returned `item` dict has only `id`, `source`,
    `text`, `parent_text`, `archive_url` -- never `stratum`, and there is no
    Jev/classifier output anywhere in this tool's data model to leak.
    """
    item_ids = {item.id for item in app.items}
    labeled_ids = set(app.records) & item_ids
    total = len(app.items)
    next_item = next((item for item in app.items if item.id not in labeled_ids), None)

    payload: dict[str, Any] = {
        "rater": app.rater,
        "label_set_version": app.label_set.version,
        "question_set_version": app.question_set.version,
        "labels": [
            {"id": label.id, "number": label.number, "definition": label.definition}
            for label in app.label_set.ratable
        ],
        "tone_levels": [level.to_dict() for level in app.question_set.tone_levels],
        "progress": {"done": len(labeled_ids), "total": total},
        "complete": next_item is None,
        "item": None,
    }
    if next_item is not None:
        payload["item"] = {
            "id": next_item.id,
            "source": next_item.source,
            "text": next_item.text,
            "parent_text": next_item.parent_text,
            "archive_url": next_item.archive_url,
        }
    return payload


def validate_save_payload(
    body: dict[str, Any],
    label_set: LabelSet,
    known_item_ids: set[str],
    valid_tones: frozenset[int],
) -> dict[str, Any]:
    """Validate and normalize a `POST /api/save` body. Raises
    `SavePayloadError` (never including corpus/rater text beyond the item id
    and label ids -- issue #46: "never log message text") on anything
    malformed."""
    if not isinstance(body, dict):
        raise SavePayloadError("body must be a JSON object")

    item_id = body.get("item_id")
    if not isinstance(item_id, str) or item_id not in known_item_ids:
        raise SavePayloadError("item_id is missing or does not match the current item")

    ratable_ids = {label.id for label in label_set.ratable}
    labels = body.get("labels")
    if not isinstance(labels, dict):
        raise SavePayloadError("labels must be an object")
    unknown = set(labels) - ratable_ids
    if unknown:
        raise SavePayloadError(f"unknown label id(s): {sorted(unknown)}")
    missing = ratable_ids - set(labels)
    if missing:
        raise SavePayloadError(f"missing label id(s): {sorted(missing)}")
    for label_id, mark in labels.items():
        if mark not in _VALID_MARKS:
            raise SavePayloadError(f"label {label_id!r} must be one of {sorted(_VALID_MARKS)}")

    tone = body.get("tone")
    if not isinstance(tone, int) or isinstance(tone, bool) or tone not in valid_tones:
        raise SavePayloadError(f"tone must be one of {sorted(valid_tones)}")

    note = body.get("note")
    if note is not None and not isinstance(note, str):
        raise SavePayloadError("note must be a string or null")

    seconds = body.get("seconds")
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds < 0:
        raise SavePayloadError("seconds must be a non-negative number")

    return {
        "item_id": item_id,
        "labels": dict(labels),
        "tone": tone,
        "note": note,
        "seconds": float(seconds),
    }


def make_label_record(
    normalized: dict[str, Any],
    *,
    rater: str,
    corpus_checksum: str,
    label_set_version: int,
    question_set_version: int,
    saved_at: str | None = None,
) -> dict[str, Any]:
    """Build one append-only label JSONL record (issue #46's field list, plus
    `question_set_version` per the orchestrator's correction: the rater's
    tone scale and label list are read from `classify/questions_v1.yaml`, so
    every record names which version of that file was in effect)."""
    return {
        "item_id": normalized["item_id"],
        "rater": rater,
        "labels": normalized["labels"],
        "tone": normalized["tone"],
        "note": normalized["note"],
        "seconds": normalized["seconds"],
        "saved_at": saved_at or datetime.now(timezone.utc).isoformat(),
        "corpus_checksum": corpus_checksum,
        "label_set_version": label_set_version,
        "question_set_version": question_set_version,
    }


def _assert_labels_match_questions(label_set: LabelSet, question_set: QuestionSetSummary) -> None:
    """The message-level labels shown to the rater must be exactly the 12
    Noul labels in `questions_v1.yaml` -- not just the same count, the same
    ids (orchestrator correction, issue #46)."""
    ratable_ids = {label.id for label in label_set.ratable}
    question_ids = set(question_set.message_label_ids)
    if ratable_ids != question_ids:
        raise LabelQuestionMismatchError(
            "label_set_v1.yaml's ratable label ids do not match "
            f"classify/questions_v1.yaml's label ids: "
            f"only in label set: {sorted(ratable_ids - question_ids)}, "
            f"only in question set: {sorted(question_ids - ratable_ids)}"
        )


def build_app_state(
    *,
    corpus_path: Path | str,
    labels_path: Path | str,
    rater: str,
    label_set_path: Path | str | None = None,
    question_set_path: Path | str | None = None,
) -> AppState:
    label_set = load_label_set(label_set_path)
    question_set = load_question_set_summary(question_set_path)
    _assert_labels_match_questions(label_set, question_set)
    items = load_corpus(corpus_path)
    corpus_checksum = checksum_file(corpus_path)
    records = read_label_records(labels_path)
    return AppState(
        items=items,
        label_set=label_set,
        question_set=question_set,
        labels_path=Path(labels_path),
        rater=rater,
        corpus_checksum=corpus_checksum,
        records=records,
    )


class LabelRequestHandler(BaseHTTPRequestHandler):
    """Thin HTTP wiring around the pure functions above. `app` is bound per
    server instance by `make_server` (a per-class attribute set on a small
    subclass created there, since `BaseHTTPRequestHandler` takes no __init__
    args of its own from `HTTPServer`)."""

    app: AppState  # bound by make_server()
    server_version = "ProjectHealthLabel/1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        # Only ever logs the request line (method + path, e.g. "POST /api/save")
        # and status code -- never headers or bodies, which is where a rater's
        # free-text note or corpus message text could otherwise leak into logs
        # (issue #46: "never log message text").
        sys.stderr.write(
            "%s - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), format % args)
        )

    def _write_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_static(self, filename: str) -> None:
        safe_name = Path(filename).name  # no path traversal; basenames only
        file_path = STATIC_DIR / safe_name
        if not file_path.is_file():
            self.send_error(404)
            return
        data = file_path.read_bytes()
        content_type = _CONTENT_TYPES.get(file_path.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        if self.path in ("/", "/index.html"):
            self._write_static("index.html")
        elif self.path.startswith("/static/"):
            self._write_static(self.path[len("/static/") :])
        elif self.path == "/api/state":
            self._write_json(build_state_payload(self.app))
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802 - stdlib method name
        if self.path != "/api/save":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length > 0 else b""
            body = json.loads(raw_body) if raw_body else {}
            known_item_ids = {item.id for item in self.app.items}
            valid_tones = self.app.question_set.tone_level_numbers()
            normalized = validate_save_payload(
                body, self.app.label_set, known_item_ids, valid_tones
            )
        except (json.JSONDecodeError, SavePayloadError) as exc:
            self._write_json({"error": str(exc)}, status=400)
            return

        record = make_label_record(
            normalized,
            rater=self.app.rater,
            corpus_checksum=self.app.corpus_checksum,
            label_set_version=self.app.label_set.version,
            question_set_version=self.app.question_set.version,
        )
        append_label_record(self.app.labels_path, record)
        self.app.records[record["item_id"]] = record
        self._write_json(build_state_payload(self.app))


def make_server(
    *,
    corpus_path: Path | str,
    labels_path: Path | str,
    rater: str,
    port: int = 8765,
    label_set_path: Path | str | None = None,
    question_set_path: Path | str | None = None,
) -> ThreadingHTTPServer:
    """Build (but do not start) the labeling `HTTPServer`, bound to
    `127.0.0.1` only -- never `0.0.0.0` (issue #46)."""
    app_state = build_app_state(
        corpus_path=corpus_path,
        labels_path=labels_path,
        rater=rater,
        label_set_path=label_set_path,
        question_set_path=question_set_path,
    )
    handler_cls = type("BoundLabelRequestHandler", (LabelRequestHandler,), {"app": app_state})
    return ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
