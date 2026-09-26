"""Provider-agnostic classifier interface + TypeSafe Jev implementation.

Implements `docs/spec/ARCHITECTURE.md` §6 and `docs/spec/COMMUNITY-HEALTH.md` §4 as
updated by `docs/spec/DECISIONS.md` D17 (provider) and D10 (cost cap), issue #45.

- `Classifier` is the provider-agnostic interface from COMMUNITY-HEALTH.md §4.2:
  ``classify(message, context) -> ClassificationRecord``. Nothing downstream of this
  module should import `typesafe_sdk` directly -- a future provider swap or a
  model-version comparison (§6.6) only needs a new class behind this Protocol.
- `ClassificationRecord` (and its nested `Usage`/`Label`/`ToneIntensity`/
  `SentimentPolarity` models) is the schema from COMMUNITY-HEALTH.md §4.3, field for
  field, with ``extra="forbid"`` matching that schema's ``additionalProperties: false``.
- `JevClassifier` is the one concrete implementation (§4.4): one `system_one` call per
  message with all of `questions_v1.yaml`'s questions together, pinned to
  `questions.EXPECTED_MODEL` (`jev-1.13.0`), never `-latest`.
- `ClassificationCache` is the input-hash cache (ARCHITECTURE.md §6: "Before submitting
  a message for classification, the pipeline checks `classification` for an existing
  row with the same `input_hash`; if found, it's skipped"). It stores only ids/hashes/
  numbers -- never message text -- so it carries no #43 privacy-guard risk regardless
  of where the caller points it; `ClassificationRecord`'s ``extra="forbid"`` config
  means a stray "text" field could never even be added to a record without every
  offline test in this module failing immediately.
- `CostCap` implements D10: input tokens are counted against a configurable monthly
  cap, priced from `pricing.yaml` (never hardcoded, so a price change is a config
  edit); hitting it pauses cleanly (`RunResult.status == "paused_cost_cap"`) and
  returns whatever was already classified -- it never raises.

Everything here that talks to TypeSafe is fully offline-testable: `JevClassifier`
accepts injectable `httpx2.BaseTransport`/`httpx2.AsyncBaseTransport` instances (see
`tests/test_classifier.py`), and `tests/conftest.py`'s network block still catches any
accidental real socket connection.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

try:
    import httpx2
    from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy, SystemOneResponse, TypeSafeClient
except ImportError as exc:  # pragma: no cover - exercised only when the dep is missing
    raise ImportError(
        "typesafe-sdk is required for project_health.classify.classifier (see "
        "pyproject.toml dependencies)."
    ) from exc

from project_health.classify.questions import (
    EXPECTED_MODEL,
    MESSAGE_LEVEL_LABELS,
    QuestionSet,
    load_question_set,
)
from project_health.classify.text_fetch import build_state

# --- Provider-agnostic domain types (COMMUNITY-HEALTH.md §4.1/§4.2) ----------------

# The three sources `questions_v1.yaml`'s `state_schema.message.source` enum allows.
# Slack (Phase 2b, D1) never reaches this classifier through the normal path: §4.6/
# ARCHITECTURE.md §6 has the Slack adapter aggregate in-process without a
# `ClassificationRecord`-shaped per-message row at all.
MessageSource = Literal["mailing_list", "jira_comment", "github_pr_comment"]

# COMMUNITY-HEALTH.md §4.3's `source` enum (one more member than the state-schema
# enum above: `slack` is a valid *record* source even though it never reaches this
# module's `classify()` -- kept here only so `ClassificationRecord.source` can be
# validated against the full schema enum).
RECORD_SOURCES: frozenset[str] = frozenset(
    {"mailing_list", "jira_comment", "github_pr_comment", "slack"}
)


@dataclass(frozen=True)
class NormalizedMessage:
    """One message to classify (COMMUNITY-HEALTH.md §4.1's `NormalizedMessage`).

    `text` is already preprocessed (`classify/preprocess.py`) -- this module never
    strips quoting/signatures itself, matching the pipeline stage split in §4.1.
    """

    message_id: str
    thread_id: str
    source: MessageSource
    text: str


@dataclass(frozen=True)
class ParentContext:
    """The immediate parent message's context, or `text=None` for a thread root
    (COMMUNITY-HEALTH.md §4.1's `ParentContext`)."""

    text: str | None = None


@runtime_checkable
class Classifier(Protocol):
    """The provider-agnostic classifier interface (COMMUNITY-HEALTH.md §4.2;
    ARCHITECTURE.md §6). Any implementation -- TypeSafe, a different vendor, or a
    future fine-tuned open-weight model -- plugs in behind this Protocol as long as
    it emits `ClassificationRecord`. The pipeline (and metric code) should depend on
    this Protocol, never on `typesafe_sdk` directly.
    """

    classifier_version: str
    question_set_version: str
    # The *pinned, requested* model id (e.g. "jev-1.13.0") -- not to be confused with
    # `ClassificationRecord.model_id`, which stores what the provider's response
    # actually reported for a given call (§4.2: "overwritten per-record with whatever
    # model id the provider's response actually reports").
    model_id: str

    def classify(
        self, message: NormalizedMessage, context: ParentContext
    ) -> "ClassificationRecord":
        ...


# --- ClassificationRecord schema (COMMUNITY-HEALTH.md §4.3, field for field) -------


class Label(BaseModel):
    """`$defs.label`: a single raw Noul probability, never a `{present, confidence}`
    pair (D17) -- thresholding happens in code, later, against the calibrated
    benchmark (issue #47), never baked in here."""

    model_config = ConfigDict(extra="forbid")

    probability: float = Field(ge=0.0, le=1.0)


class Usage(BaseModel):
    """Token accounting from the provider's response, tracked against D10's cap."""

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class ToneIntensity(BaseModel):
    """Descriptive-only Score output. Never a health/toxicity judgment and never
    feeds a published gate or metric on its own (§5.1's no-composite-score rule)."""

    model_config = ConfigDict(extra="forbid")

    score: float
    confidence: float = Field(ge=0.0, le=1.0)
    # Per-level probability distribution, keyed by level index *as a string*
    # (§4.3's own wording) -- the SDK's `ScoreAnswer.probabilities` is `dict[int,
    # float]`; `JevClassifier` converts the keys when building this model.
    probabilities: dict[str, float] = Field(default_factory=dict)


class SentimentPolarity(BaseModel):
    """Optional, observability-only. Never used in any gate or published metric.
    Not asked by `questions_v1.yaml` today, so `JevClassifier` never populates this;
    kept for schema completeness (§4.3) and a future provider/question addition."""

    model_config = ConfigDict(extra="forbid")

    value: Literal["negative", "neutral", "positive", "mixed"]
    confidence: float = Field(ge=0.0, le=1.0)


class ClassificationRecord(BaseModel):
    """COMMUNITY-HEALTH.md §4.3's `CommunityHealthClassificationRecord`, field for
    field. `extra="forbid"` matches the schema's `additionalProperties: false` --
    among other things, this means a record can never carry a stray "text"/"body"
    field by accident, which is the actual privacy backstop for this module (see
    the module docstring and `tests/test_classifier.py`'s
    `test_record_schema_has_no_text_carrying_field`).

    Records are immutable and additive (§4.3 "Notes"): a correction is a *new*
    record with `human_reviewed=True` and the original gets a `superseded_by`
    back-reference -- nothing in this module ever mutates or deletes a record.
    """

    model_config = ConfigDict(extra="forbid")

    record_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    message_id: str
    thread_id: str
    source: str
    classifier_version: str
    question_set_version: str
    # The provider's response-reported model (§4.2/§4.4), not necessarily identical
    # to the pinned value that was requested, though it always should be under D17's
    # pinned-model discipline -- divergence here is exactly what this field exists
    # to catch.
    model_id: str
    input_hash: str
    classified_at: datetime
    usage: Usage
    labels: dict[str, Label] = Field(default_factory=dict)
    tone_intensity: ToneIntensity | None = None
    sentiment_polarity: SentimentPolarity | None = None
    human_reviewed: bool = False
    human_label_id: str | None = None
    superseded_by: str | None = None

    @field_validator("source")
    @classmethod
    def _validate_source(cls, value: str) -> str:
        if value not in RECORD_SOURCES:
            raise ValueError(f"source must be one of {sorted(RECORD_SOURCES)}, got {value!r}")
        return value

    @field_validator("labels")
    @classmethod
    def _validate_label_keys(cls, value: dict[str, Label]) -> dict[str, Label]:
        extra = set(value) - MESSAGE_LEVEL_LABELS
        if extra:
            raise ValueError(
                f"labels contains key(s) not in COMMUNITY-HEALTH.md §1.2's "
                f"message-level set: {sorted(extra)}"
            )
        return value

    @field_validator("classified_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("classified_at must be timezone-aware (UTC)")
        return value


def compute_input_hash(state: Mapping[str, Any], question_set_version: str, model: str) -> str:
    """`sha256` of the canonical JSON classifier `state` + `question_set_version` +
    `model` (ARCHITECTURE.md §6: "input_hash = sha256(normalized_message_text +
    normalized_parent_text + question_set_version + model_id)"; COMMUNITY-HEALTH.md
    §4.3: "sha256 of the exact normalized text + context window sent to the model").

    `state` is hashed as canonical JSON (`sort_keys=True`, no incidental whitespace)
    rather than concatenating its fields directly, so the hash is stable regardless
    of dict key order and unambiguous about field boundaries (`"ab" + "c"` and
    `"a" + "bc"` would otherwise collide under naive string concatenation).

    `model` here is the *pinned, requested* model id, not the response-reported one
    -- the hash must be computable, and the cache check performed, *before* a call
    is ever made (that's the entire point of the cache: a message whose input hash
    already has a record is never sent at all).
    """
    canonical = json.dumps(state, sort_keys=True, separators=(",", ":"))
    digest_input = f"{canonical}|{question_set_version}|{model}".encode()
    return hashlib.sha256(digest_input).hexdigest()


# --- Input-hash cache (ARCHITECTURE.md §6) -----------------------------------------

CacheFormat = Literal["jsonl", "parquet"]


class ClassificationCache:
    """A local input-hash cache: a message whose input hash already has a row here
    is never re-sent (ARCHITECTURE.md §6). Backed by either a JSONL file (default;
    one `ClassificationRecord` per line) or a Parquet file (using
    `project_health.schema.tables.CLASSIFICATION`, so a Parquet cache is already
    shaped like the eventual `classification` table) -- the caller picks by path
    suffix or an explicit `format=`.

    Records here never contain message text (`ClassificationRecord` has no such
    field at all, enforced by its `extra="forbid"` config), so this cache carries
    no #43 privacy-guard risk no matter where the caller points `path` -- there is
    nothing here a hash could be reversed into.
    """

    def __init__(self, path: str | Path, format: CacheFormat | None = None) -> None:
        self.path = Path(path)
        if format is not None:
            self._format: CacheFormat = format
        elif self.path.suffix == ".parquet":
            self._format = "parquet"
        else:
            self._format = "jsonl"
        self._by_hash: dict[str, ClassificationRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        if self._format == "jsonl":
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    record = ClassificationRecord.model_validate(json.loads(line))
                    self._by_hash[record.input_hash] = record
        else:
            self._load_parquet()

    def _load_parquet(self) -> None:
        import pyarrow.parquet as pq

        table = pq.read_table(self.path)
        for row in table.to_pylist():
            record = _classification_record_from_arrow_row(row)
            self._by_hash[record.input_hash] = record

    def contains(self, input_hash: str) -> bool:
        return input_hash in self._by_hash

    def get(self, input_hash: str) -> ClassificationRecord | None:
        return self._by_hash.get(input_hash)

    def __len__(self) -> int:
        return len(self._by_hash)

    def append(self, record: ClassificationRecord) -> None:
        """Add `record` to the cache, both in memory and on disk. A record whose
        `input_hash` is already cached is a no-op (idempotent re-append)."""
        if record.input_hash in self._by_hash:
            return
        self._by_hash[record.input_hash] = record
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._format == "jsonl":
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record.model_dump(mode="json")) + "\n")
        else:
            self._rewrite_parquet()

    def _rewrite_parquet(self) -> None:
        # Parquet has no cheap single-row append, so the whole (small, pilot-scale)
        # cache is rewritten. Fine at this project's pilot scale (D18: 250 messages);
        # revisit if a much larger cache ever needs this format.
        import pyarrow as pa
        import pyarrow.parquet as pq

        from project_health.schema.tables import CLASSIFICATION

        rows = [_classification_record_to_arrow_row(r) for r in self._by_hash.values()]
        table = pa.Table.from_pylist(rows, schema=CLASSIFICATION)
        pq.write_table(table, self.path)


def _classification_record_to_arrow_row(record: ClassificationRecord) -> dict[str, Any]:
    dumped = record.model_dump(mode="python")
    labels = dumped.get("labels") or {}
    return {
        "record_id": dumped["record_id"],
        "message_id": dumped["message_id"],
        "thread_id": dumped["thread_id"],
        "source": dumped["source"],
        "classifier_version": dumped["classifier_version"],
        "question_set_version": dumped["question_set_version"],
        "model_id": dumped["model_id"],
        "input_hash": dumped["input_hash"],
        "classified_at": dumped["classified_at"],
        "usage": dumped["usage"],
        "labels": {name: labels[name] for name in MESSAGE_LEVEL_LABELS if name in labels} or None,
        "tone_intensity": dumped["tone_intensity"],
        "sentiment_polarity": dumped["sentiment_polarity"],
        "human_reviewed": dumped["human_reviewed"],
        "human_label_id": dumped["human_label_id"],
        "superseded_by": dumped["superseded_by"],
    }


def _classification_record_from_arrow_row(row: dict[str, Any]) -> ClassificationRecord:
    labels = row.get("labels") or {}
    tone_intensity = row.get("tone_intensity")
    if tone_intensity is not None:
        probabilities = tone_intensity.get("probabilities")
        if probabilities is not None:
            # pyarrow's map_() type round-trips through `to_pylist()` as a list
            # of (key, value) pairs, not a dict -- convert back.
            tone_intensity = {**tone_intensity, "probabilities": dict(probabilities)}
    return ClassificationRecord.model_validate(
        {
            **row,
            "labels": {name: value for name, value in labels.items() if value is not None},
            "tone_intensity": tone_intensity,
        }
    )


# --- D10 cost cap -------------------------------------------------------------------


def load_pricing_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load `pricing.yaml` (or an override path). Kept out of code per D10/issue #45
    ("Estimate cost from TypeSafe's published price... put it in config, not
    code")."""
    resolved = Path(path) if path is not None else Path(__file__).with_name("pricing.yaml")
    with open(resolved, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@dataclass
class CostCap:
    """D10's owner-funded monthly cost cap. `spent_usd` is the caller's own running
    total for the current month (e.g. reloaded from a run manifest) -- this class
    does not persist anything itself, so cap state survives across runs however the
    caller already persists run provenance (ARCHITECTURE.md §5).

    Never raises: `JevClassifier` checks `.exceeded` before each call and pauses
    cleanly (`RunResult.status == "paused_cost_cap"`) rather than treating the cap
    as an error condition (D10: "classification pauses... the rest of the pipeline
    keeps running").
    """

    monthly_cap_usd: float
    price_usd_per_million_input_tokens: float
    spent_usd: float = 0.0

    @classmethod
    def from_config(
        cls,
        monthly_cap_usd: float,
        spent_usd: float = 0.0,
        pricing_config_path: str | Path | None = None,
    ) -> "CostCap":
        price = load_pricing_config(pricing_config_path)["price_usd_per_million_input_tokens"]
        return cls(
            monthly_cap_usd=monthly_cap_usd,
            price_usd_per_million_input_tokens=price,
            spent_usd=spent_usd,
        )

    @property
    def exceeded(self) -> bool:
        return self.spent_usd >= self.monthly_cap_usd

    def estimate_cost_usd(self, input_tokens: int) -> float:
        return (input_tokens / 1_000_000) * self.price_usd_per_million_input_tokens

    def record(self, input_tokens: int) -> float:
        """Record `input_tokens` actually spent and return the new running total."""
        self.spent_usd += self.estimate_cost_usd(input_tokens)
        return self.spent_usd


# --- `.env` API key helper (pilot use) -----------------------------------------------


def load_jev_key_from_dotenv(dotenv_path: str | Path | None = None) -> bool:
    """Read `jev_key=` from a local, gitignored `.env` and set `TYPESAFE_API_KEY`
    from it in `os.environ`, without ever printing or logging the value (issue #45's
    "for pilot use" helper -- mirrors `scripts/text_fetch_real_check.py`'s
    `_load_jev_key_from_dotenv`, generalized here for reuse by tests/scripts).

    Looks in, in order: `dotenv_path` if given, `.env` in the current working
    directory, `.env` next to the repo root, and (for a git worktree, which doesn't
    share untracked files with the main checkout) `.env` next to the main
    checkout's root.

    Returns `True` if `TYPESAFE_API_KEY` was set from a `.env` file, `False` if no
    `jev_key` entry was found anywhere (leaving `os.environ` untouched -- this is
    not an error, since `TYPESAFE_API_KEY` may already be set directly, e.g. in CI).
    """
    candidates: list[Path] = []
    if dotenv_path is not None:
        candidates.append(Path(dotenv_path))
    candidates.append(Path.cwd() / ".env")
    candidates.append(Path(__file__).resolve().parents[3] / ".env")
    try:
        common_dir = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        candidates.append(Path(common_dir).resolve().parent / ".env")
    except Exception:
        pass

    for candidate in candidates:
        if not candidate.is_file():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "jev_key":
                os.environ["TYPESAFE_API_KEY"] = value.strip()
                return True
    return False


# --- Run result ----------------------------------------------------------------------

RunStatus = Literal["completed", "paused_cost_cap"]


@dataclass
class RunResult:
    """The outcome of a `JevClassifier.run`/`run_async` batch. `status ==
    "paused_cost_cap"` means D10's cap was hit partway through -- `records` holds
    whatever was classified (from calls and cache hits) before the pause, never an
    exception (D10: "the rest of the pipeline keeps running")."""

    status: RunStatus
    records: list[ClassificationRecord]
    calls_made: int
    cache_hits: int
    input_tokens_used: int
    output_tokens_used: int
    estimated_cost_usd: float


# --- JevClassifier -------------------------------------------------------------------

DEFAULT_CLASSIFIER_VERSION = "1.0.0"
DEFAULT_CONCURRENCY = 4

# 429 (rate limit) and 529 (overloaded) are both already covered by the SDK's
# default `RetryPolicy` (429 explicitly, 529 via the 500-599 range), but issue #45
# calls them out by name, so this project pins its own `RetryPolicy` naming them
# explicitly rather than relying on an unread default that could change upstream.
DEFAULT_RETRYABLE_STATUSES: frozenset[int] = frozenset({429, 529, *range(500, 600)})


def _default_retry_policy() -> "RetryPolicy":
    return RetryPolicy(http_statuses=set(DEFAULT_RETRYABLE_STATUSES))


class JevClassifier:
    """The TypeSafe Jev implementation of `Classifier` (COMMUNITY-HEALTH.md §4.4).

    `classify()` implements the `Classifier` Protocol exactly: one `system_one` call
    per message, synchronous, for a caller that wants the simple one-at-a-time
    interface. `run`/`run_async` is the throughput path ARCHITECTURE.md §6
    describes for nightly/backfill runs -- many concurrent `system_one` calls via
    `AsyncTypeSafeClient`, bounded by `concurrency`, with the input-hash cache and
    D10 cost cap both applied before any call is dispatched.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        question_set: QuestionSet | None = None,
        cache: ClassificationCache,
        cost_cap: CostCap | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
        classifier_version: str = DEFAULT_CLASSIFIER_VERSION,
        retry: "RetryPolicy | None" = None,
        transport: "httpx2.BaseTransport | None" = None,
        async_transport: "httpx2.AsyncBaseTransport | None" = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        self._question_set = question_set or load_question_set()
        if self._question_set.model != EXPECTED_MODEL:
            raise ValueError(
                f"question set is pinned to {self._question_set.model!r}, expected "
                f"{EXPECTED_MODEL!r} (D17: never jev-latest)"
            )
        self._questions = self._question_set.build_questions()
        self.classifier_version = classifier_version
        self.question_set_version = str(self._question_set.version)
        self.model_id = self._question_set.model  # the pinned, requested model
        self._api_key = api_key
        self._cache = cache
        self._cost_cap = cost_cap
        self._concurrency = concurrency
        self._retry = retry if retry is not None else _default_retry_policy()
        self._transport = transport
        self._async_transport = async_transport
        self._clock = clock

    # -- Protocol conformance: one message, synchronous -----------------------------

    def classify(self, message: NormalizedMessage, context: ParentContext) -> ClassificationRecord:
        """Classify one message. Does *not* consult the input-hash cache or the
        cost cap -- those are applied by `run`/`run_async`, which is the path every
        real caller (the pipeline, the pilot script) should use; this method exists
        to satisfy the `Classifier` Protocol for a caller that genuinely wants a
        single uncached call."""
        state = build_state(message.text, message.source, context.text)
        with TypeSafeClient(
            api_key=self._api_key, retry=self._retry, transport=self._transport
        ) as client:
            response = client.system_one(
                state=state, questions=self._questions, model=self.model_id
            )
        input_hash = compute_input_hash(state, self.question_set_version, self.model_id)
        return self._record_from_response(message, input_hash, response)

    # -- Batch path: cache + cost cap + bounded concurrency --------------------------

    def run(
        self, items: Sequence[tuple[NormalizedMessage, ParentContext]]
    ) -> RunResult:
        """Synchronous convenience wrapper around `run_async` (`asyncio.run`)."""
        return asyncio.run(self.run_async(items))

    async def run_async(
        self, items: Sequence[tuple[NormalizedMessage, ParentContext]]
    ) -> RunResult:
        records: list[ClassificationRecord | None] = [None] * len(items)
        calls_made = 0
        cache_hits = 0
        input_tokens_used = 0
        output_tokens_used = 0
        paused = False

        semaphore = asyncio.Semaphore(self._concurrency)

        async with AsyncTypeSafeClient(
            api_key=self._api_key, retry=self._retry, transport=self._async_transport
        ) as client:

            async def worker(
                index: int, message: NormalizedMessage, context: ParentContext
            ) -> None:
                nonlocal calls_made, cache_hits, input_tokens_used, output_tokens_used, paused
                async with semaphore:
                    if paused:
                        return
                    state = build_state(message.text, message.source, context.text)
                    input_hash = compute_input_hash(state, self.question_set_version, self.model_id)
                    cached = self._cache.get(input_hash)
                    if cached is not None:
                        cache_hits += 1
                        records[index] = cached
                        return
                    if self._cost_cap is not None and self._cost_cap.exceeded:
                        paused = True
                        return
                    response = await client.system_one(
                        state=state, questions=self._questions, model=self.model_id
                    )
                    record = self._record_from_response(message, input_hash, response)
                    self._cache.append(record)
                    calls_made += 1
                    input_tokens_used += record.usage.input_tokens
                    output_tokens_used += record.usage.output_tokens
                    if self._cost_cap is not None:
                        self._cost_cap.record(record.usage.input_tokens)
                    records[index] = record

            await asyncio.gather(
                *(worker(i, message, context) for i, (message, context) in enumerate(items))
            )

        estimated_cost_usd = 0.0
        if self._cost_cap is not None:
            estimated_cost_usd = self._cost_cap.estimate_cost_usd(input_tokens_used)

        return RunResult(
            status="paused_cost_cap" if paused else "completed",
            records=[r for r in records if r is not None],
            calls_made=calls_made,
            cache_hits=cache_hits,
            input_tokens_used=input_tokens_used,
            output_tokens_used=output_tokens_used,
            estimated_cost_usd=estimated_cost_usd,
        )

    # -- Response -> record ----------------------------------------------------------

    def _record_from_response(
        self, message: NormalizedMessage, input_hash: str, response: "SystemOneResponse"
    ) -> ClassificationRecord:
        labels: dict[str, Label] = {}
        for label_id, noul_answer in response.nouls.items():
            if label_id in MESSAGE_LEVEL_LABELS:
                labels[label_id] = Label(probability=noul_answer.noul)

        tone_intensity = None
        score_answer = response.scores.get("tone_intensity")
        if score_answer is not None:
            tone_intensity = ToneIntensity(
                score=score_answer.score,
                confidence=score_answer.confidence,
                probabilities={str(k): v for k, v in score_answer.probabilities.items()},
            )

        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        if input_tokens is None or output_tokens is None:
            raise ValueError(
                "TypeSafe response did not report token usage; cannot maintain D10's "
                "cost-cap accounting without it"
            )

        return ClassificationRecord(
            message_id=message.message_id,
            thread_id=message.thread_id,
            source=message.source,
            classifier_version=self.classifier_version,
            question_set_version=self.question_set_version,
            model_id=response.model,
            input_hash=input_hash,
            classified_at=self._clock(),
            usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
            labels=labels,
            tone_intensity=tone_intensity,
        )
