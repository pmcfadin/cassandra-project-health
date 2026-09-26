"""Tests for project_health.classify.classifier (issue #45).

Offline by default: every real TypeSafe call goes through an injected
`httpx2.MockTransport` (sync) or `httpx2.AsyncBaseTransport` handler (async) --
`tests/conftest.py`'s network block also catches any accidental real socket
connection regardless. One test (`TestLiveSmoke`) is marked `live_network` and
is skipped unless `RUN_LIVE_NETWORK_TESTS=1`; it uses three synthetic
messages, never real community text.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx2
import pydantic
import pytest
from typesafe_sdk import RetryPolicy

from project_health.classify.classifier import (
    DEFAULT_RETRYABLE_STATUSES,
    ClassificationCache,
    ClassificationRecord,
    CostCap,
    CostCapExceededError,
    JevClassifier,
    Label,
    NormalizedMessage,
    ParentContext,
    RunResult,
    ToneIntensity,
    Usage,
    compute_input_hash,
    load_jev_key_from_dotenv,
    load_pricing_config,
)
from project_health.classify.questions import (
    EXPECTED_MODEL,
    MESSAGE_LEVEL_LABELS,
    load_question_set,
)
from project_health.schema import get_schema

FIXED_NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


def _usage(input_tokens: int = 100, output_tokens: int = 20) -> dict:
    return {"input_tokens": input_tokens, "output_tokens": output_tokens}


def _full_answers(
    label_value: float = 0.5,
    tone_score: float = 1.0,
    tone_confidence: float = 0.8,
) -> dict:
    """A `system_one` response body's `answers`, covering every v1 question."""
    answers = {label: {"type": "noul", "noul": label_value} for label in MESSAGE_LEVEL_LABELS}
    answers["tone_intensity"] = {
        "type": "score",
        "score": tone_score,
        "confidence": tone_confidence,
        "legend": {0: "neutral", 1: "firm", 2: "sharp", 3: "heated", 4: "aggressive"},
        "probabilities": {0: 0.1, 1: 0.6, 2: 0.2, 3: 0.05, 4: 0.05},
    }
    return answers


def _mock_transport(
    responses: list[tuple[int, dict]] | None = None,
    call_log: list[dict] | None = None,
    fixed_response: dict | None = None,
) -> httpx2.MockTransport:
    """Serves canned `system_one` responses in order (or one fixed response
    forever), and optionally records each request body's `state`/`model`."""
    calls = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        if call_log is not None:
            call_log.append(json.loads(request.content))
        if fixed_response is not None:
            return httpx2.Response(200, json=fixed_response)
        status, body = responses[calls["n"]]
        calls["n"] += 1
        return httpx2.Response(status, json=body)

    return httpx2.MockTransport(handler)


def _classifier(
    tmp_path: Path,
    *,
    transport: httpx2.BaseTransport | None = None,
    async_transport: httpx2.AsyncBaseTransport | None = None,
    cost_cap: CostCap | None = None,
    concurrency: int = 4,
    cache_path: Path | None = None,
    retry: RetryPolicy | None = None,
) -> JevClassifier:
    cache = ClassificationCache(cache_path or (tmp_path / "cache.jsonl"))
    return JevClassifier(
        api_key="test-key",
        cache=cache,
        cost_cap=cost_cap,
        concurrency=concurrency,
        transport=transport,
        async_transport=async_transport,
        retry=retry if retry is not None else RetryPolicy(max_retries=0),
        clock=lambda: FIXED_NOW,
    )


# --- ClassificationRecord schema (COMMUNITY-HEALTH.md §4.3) -------------------------


class TestClassificationRecordSchema:
    def _valid_kwargs(self) -> dict:
        return dict(
            message_id="msg-1",
            thread_id="thread-1",
            source="mailing_list",
            classifier_version="1.0.0",
            question_set_version="1",
            model_id=EXPECTED_MODEL,
            input_hash="a" * 64,
            classified_at=FIXED_NOW,
            usage=Usage(input_tokens=10, output_tokens=5),
            labels={"technical_disagreement": Label(probability=0.9)},
            tone_intensity=ToneIntensity(
                score=1.2, confidence=0.7, probabilities={"0": 0.5, "1": 0.5}
            ),
        )

    def test_valid_record_round_trips_through_json(self):
        record = ClassificationRecord(**self._valid_kwargs())
        dumped = record.model_dump(mode="json")
        restored = ClassificationRecord.model_validate(dumped)
        assert restored == record

    def test_record_id_defaults_to_a_uuid(self):
        record = ClassificationRecord(**self._valid_kwargs())
        assert len(record.record_id) == 36

    def test_rejects_unknown_top_level_field(self):
        kwargs = self._valid_kwargs()
        kwargs["not_in_schema"] = "x"
        with pytest.raises(pydantic.ValidationError):
            ClassificationRecord(**kwargs)

    def test_rejects_a_text_field_specifically(self):
        """The actual privacy backstop (module docstring): §4.3 has no field to
        carry message text, so `extra='forbid'` means one can never be added
        by accident."""
        kwargs = self._valid_kwargs()
        kwargs["text"] = "some message body"
        with pytest.raises(pydantic.ValidationError):
            ClassificationRecord(**kwargs)

    def test_rejects_invalid_source(self):
        kwargs = self._valid_kwargs()
        kwargs["source"] = "carrier_pigeon"
        with pytest.raises(pydantic.ValidationError):
            ClassificationRecord(**kwargs)

    def test_rejects_thread_level_label_key(self):
        kwargs = self._valid_kwargs()
        kwargs["labels"] = {"escalation": Label(probability=0.5)}
        with pytest.raises(pydantic.ValidationError):
            ClassificationRecord(**kwargs)

    def test_rejects_naive_classified_at(self):
        kwargs = self._valid_kwargs()
        kwargs["classified_at"] = datetime(2026, 1, 1)
        with pytest.raises(pydantic.ValidationError):
            ClassificationRecord(**kwargs)

    def test_label_probability_must_be_in_unit_interval(self):
        with pytest.raises(pydantic.ValidationError):
            Label(probability=1.5)
        with pytest.raises(pydantic.ValidationError):
            Label(probability=-0.1)

    def test_human_reviewed_defaults_false(self):
        record = ClassificationRecord(**self._valid_kwargs())
        assert record.human_reviewed is False
        assert record.superseded_by is None


# --- input_hash ----------------------------------------------------------------------


class TestComputeInputHash:
    def test_deterministic_and_key_order_independent(self):
        state_a = {"message": {"text": "hello", "source": "mailing_list"}, "parent": None}
        state_b = {"parent": None, "message": {"source": "mailing_list", "text": "hello"}}
        assert compute_input_hash(state_a, "1", "jev-1.13.0") == compute_input_hash(
            state_b, "1", "jev-1.13.0"
        )

    def test_differs_on_question_set_version(self):
        state = {"message": {"text": "hello", "source": "mailing_list"}, "parent": None}
        assert compute_input_hash(state, "1", "jev-1.13.0") != compute_input_hash(
            state, "2", "jev-1.13.0"
        )

    def test_differs_on_model(self):
        state = {"message": {"text": "hello", "source": "mailing_list"}, "parent": None}
        assert compute_input_hash(state, "1", "jev-1.13.0") != compute_input_hash(
            state, "1", "jev-1.14.0"
        )

    def test_differs_on_text_content(self):
        s1 = {"message": {"text": "hello", "source": "mailing_list"}, "parent": None}
        s2 = {"message": {"text": "goodbye", "source": "mailing_list"}, "parent": None}
        assert compute_input_hash(s1, "1", "jev-1.13.0") != compute_input_hash(
            s2, "1", "jev-1.13.0"
        )

    def test_is_a_sha256_hex_digest(self):
        state = {"message": {"text": "hello", "source": "mailing_list"}, "parent": None}
        digest = compute_input_hash(state, "1", "jev-1.13.0")
        assert len(digest) == 64
        int(digest, 16)  # raises ValueError if not hex


# --- ClassificationCache -------------------------------------------------------------


def _sample_record(input_hash: str, message_id: str = "m1") -> ClassificationRecord:
    return ClassificationRecord(
        message_id=message_id,
        thread_id="t1",
        source="mailing_list",
        classifier_version="1.0.0",
        question_set_version="1",
        model_id=EXPECTED_MODEL,
        input_hash=input_hash,
        classified_at=FIXED_NOW,
        usage=Usage(input_tokens=10, output_tokens=5),
        labels={"technical_disagreement": Label(probability=0.4)},
        tone_intensity=ToneIntensity(score=1.0, confidence=0.5, probabilities={"0": 1.0}),
    )


class TestClassificationCacheJsonl:
    def test_append_then_contains_and_get(self, tmp_path: Path):
        cache = ClassificationCache(tmp_path / "cache.jsonl")
        record = _sample_record("hash-1")
        assert not cache.contains("hash-1")
        cache.append(record)
        assert cache.contains("hash-1")
        assert cache.get("hash-1") == record

    def test_persists_across_instances(self, tmp_path: Path):
        path = tmp_path / "cache.jsonl"
        ClassificationCache(path).append(_sample_record("hash-1"))
        reloaded = ClassificationCache(path)
        assert reloaded.contains("hash-1")
        assert len(reloaded) == 1

    def test_reappending_same_hash_is_a_no_op(self, tmp_path: Path):
        path = tmp_path / "cache.jsonl"
        cache = ClassificationCache(path)
        cache.append(_sample_record("hash-1", message_id="first"))
        cache.append(_sample_record("hash-1", message_id="second"))
        assert len(cache) == 1
        assert cache.get("hash-1").message_id == "first"
        # Only one line was ever written.
        assert len(path.read_text(encoding="utf-8").splitlines()) == 1

    def test_cached_records_never_contain_raw_text(self, tmp_path: Path):
        path = tmp_path / "cache.jsonl"
        ClassificationCache(path).append(_sample_record("hash-1"))
        raw = path.read_text(encoding="utf-8")
        row = json.loads(raw.splitlines()[0])
        assert "text" not in row
        assert set(row) <= set(ClassificationRecord.model_fields)


class TestClassificationCacheParquet:
    def test_append_reload_roundtrip(self, tmp_path: Path):
        path = tmp_path / "cache.parquet"
        cache = ClassificationCache(path)
        r1 = _sample_record("hash-1", "m1")
        r2 = _sample_record("hash-2", "m2")
        cache.append(r1)
        cache.append(r2)
        assert path.exists()

        reloaded = ClassificationCache(path)
        assert reloaded.contains("hash-1")
        assert reloaded.contains("hash-2")
        probability = reloaded.get("hash-1").labels["technical_disagreement"].probability
        assert probability == pytest.approx(0.4)

    def test_parquet_cache_matches_the_classification_table_schema(self, tmp_path: Path):
        import pyarrow.parquet as pq

        path = tmp_path / "cache.parquet"
        ClassificationCache(path).append(_sample_record("hash-1"))
        table = pq.read_table(path)
        # Validates column-by-column against schema/tables.py's CLASSIFICATION.
        from project_health.schema import validate

        validate("classification", table)


# --- CostCap (D10) ---------------------------------------------------------------------


class TestCostCap:
    def test_not_exceeded_below_cap(self):
        cap = CostCap(monthly_cap_usd=1.0, price_usd_per_million_input_tokens=0.042)
        assert not cap.exceeded

    def test_record_accumulates_spend_and_trips_exceeded(self):
        cap = CostCap(monthly_cap_usd=0.01, price_usd_per_million_input_tokens=1.0)
        assert not cap.exceeded
        cap.record(5_000)  # $0.005
        assert not cap.exceeded
        cap.record(6_000)  # + $0.006 = $0.011 > $0.01 cap
        assert cap.exceeded

    def test_estimate_cost_usd(self):
        cap = CostCap(monthly_cap_usd=100.0, price_usd_per_million_input_tokens=0.042)
        assert cap.estimate_cost_usd(1_000_000) == pytest.approx(0.042)

    def test_from_config_reads_price_from_pricing_yaml(self):
        cap = CostCap.from_config(monthly_cap_usd=5.0)
        assert cap.price_usd_per_million_input_tokens == pytest.approx(0.042)

    def test_load_pricing_config_default(self):
        config = load_pricing_config()
        assert config["price_usd_per_million_input_tokens"] == pytest.approx(0.042)

    def test_price_is_not_hardcoded_in_a_python_module(self):
        import project_health.classify.classifier as mod

        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert "0.042" not in source


# --- .env helper -----------------------------------------------------------------------


class TestLoadJevKeyFromDotenv:
    def test_sets_typesafe_api_key_from_explicit_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        dotenv = tmp_path / ".env"
        dotenv.write_text("jev_key=super-secret-pilot-key\nother_var=ignored\n", encoding="utf-8")
        found = load_jev_key_from_dotenv(dotenv)
        assert found is True
        assert os.environ["TYPESAFE_API_KEY"] == "super-secret-pilot-key"

    def test_returns_false_when_no_jev_key_anywhere(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        monkeypatch.chdir(tmp_path)

        def _raise(*args, **kwargs):
            raise FileNotFoundError("no git here")

        monkeypatch.setattr(
            "project_health.classify.classifier.subprocess.run", _raise
        )
        found = load_jev_key_from_dotenv(tmp_path / "does-not-exist.env")
        assert found is False
        assert "TYPESAFE_API_KEY" not in os.environ

    def test_never_appears_in_module_source_as_a_print_call_on_the_value(self):
        import project_health.classify.classifier as mod

        source = Path(mod.__file__).read_text(encoding="utf-8")
        # Defence-in-depth documentation check: the loader function body must
        # not print/log the parsed value.
        start = source.index("def load_jev_key_from_dotenv")
        end = source.index("\ndef ", start + 1)
        body = source[start:end]
        assert "print(" not in body


# --- JevClassifier: Classifier protocol conformance, single message -----------------


class TestJevClassifierClassify:
    def test_classify_returns_a_full_classification_record(self, tmp_path: Path):
        answers = {"model": EXPECTED_MODEL, "usage": _usage(), "answers": _full_answers(0.7)}
        transport = _mock_transport(fixed_response=answers)
        classifier = _classifier(tmp_path, transport=transport)
        message = NormalizedMessage("m1", "t1", "mailing_list", "I disagree with this approach.")
        record = classifier.classify(message, ParentContext(text=None))

        assert record.message_id == "m1"
        assert record.thread_id == "t1"
        assert record.source == "mailing_list"
        assert record.model_id == EXPECTED_MODEL
        assert record.question_set_version == "1"
        assert set(record.labels) == MESSAGE_LEVEL_LABELS
        assert record.labels["technical_disagreement"].probability == pytest.approx(0.7)
        assert record.tone_intensity.score == pytest.approx(1.0)
        expected_probabilities = {"0": 0.1, "1": 0.6, "2": 0.2, "3": 0.05, "4": 0.05}
        assert record.tone_intensity.probabilities == expected_probabilities
        assert record.usage.input_tokens == 100
        assert record.usage.output_tokens == 20
        assert record.classified_at == FIXED_NOW

    def test_classify_records_provider_reported_model_even_if_different(self, tmp_path: Path):
        """D17/§4.4: `model_id` on the record is whatever the response reports,
        which is auditable evidence even under a pinned request."""
        transport = _mock_transport(
            fixed_response={"model": "jev-1.13.1", "usage": _usage(), "answers": _full_answers()}
        )
        classifier = _classifier(tmp_path, transport=transport)
        record = classifier.classify(
            NormalizedMessage("m1", "t1", "mailing_list", "text"), ParentContext()
        )
        assert record.model_id == "jev-1.13.1"
        # But the *pinned* attribute on the classifier itself never changes.
        assert classifier.model_id == EXPECTED_MODEL

    def test_classify_sends_the_pinned_model_in_the_request(self, tmp_path: Path):
        call_log: list[dict] = []
        transport = _mock_transport(
            fixed_response={"model": EXPECTED_MODEL, "usage": _usage(), "answers": _full_answers()},
            call_log=call_log,
        )
        classifier = _classifier(tmp_path, transport=transport)
        classifier.classify(NormalizedMessage("m1", "t1", "mailing_list", "text"), ParentContext())
        assert call_log[0]["model"] == EXPECTED_MODEL

    def test_classify_sends_state_shape_with_parent(self, tmp_path: Path):
        call_log: list[dict] = []
        transport = _mock_transport(
            fixed_response={"model": EXPECTED_MODEL, "usage": _usage(), "answers": _full_answers()},
            call_log=call_log,
        )
        classifier = _classifier(tmp_path, transport=transport)
        classifier.classify(
            NormalizedMessage("m1", "t1", "jira_comment", "child text"),
            ParentContext(text="parent text"),
        )
        assert call_log[0]["state"] == {
            "message": {"text": "child text", "source": "jira_comment"},
            "parent": {"text": "parent text"},
        }

    def test_classify_raises_on_missing_usage(self, tmp_path: Path):
        transport = _mock_transport(
            fixed_response={
                "model": EXPECTED_MODEL,
                "usage": {"input_tokens": None, "output_tokens": None},
                "answers": _full_answers(),
            }
        )
        classifier = _classifier(tmp_path, transport=transport)
        message = NormalizedMessage("m1", "t1", "mailing_list", "text")
        with pytest.raises(ValueError, match="usage"):
            classifier.classify(message, ParentContext())

    def test_rejects_a_question_set_not_pinned_to_expected_model(self, tmp_path: Path):
        real_qs = load_question_set()
        fake_qs = type(real_qs)(
            version=real_qs.version,
            model="jev-latest",
            labels=real_qs.labels,
            score=real_qs.score,
            state_schema=real_qs.state_schema,
            raw=real_qs.raw,
        )
        with pytest.raises(ValueError, match="jev-latest"):
            JevClassifier(
                api_key="test-key",
                question_set=fake_qs,
                cache=ClassificationCache(tmp_path / "cache.jsonl"),
            )

    def test_rejects_non_positive_concurrency(self, tmp_path: Path):
        with pytest.raises(ValueError):
            JevClassifier(
                api_key="test-key",
                cache=ClassificationCache(tmp_path / "cache.jsonl"),
                concurrency=0,
            )

    def test_second_classify_call_is_a_cache_hit_with_zero_transport_calls(
        self, tmp_path: Path
    ):
        """D22: a repeated `classify()` call for the same message must never
        re-send it -- `classify()` is not exempt from the input-hash cache
        just because it's the single-message path."""
        call_log: list[dict] = []
        answers = {"model": EXPECTED_MODEL, "usage": _usage(), "answers": _full_answers(0.6)}
        transport = _mock_transport(fixed_response=answers, call_log=call_log)
        classifier = _classifier(tmp_path, transport=transport)
        message = NormalizedMessage("m1", "t1", "mailing_list", "same text every time")

        first = classifier.classify(message, ParentContext())
        assert len(call_log) == 1

        second = classifier.classify(message, ParentContext())
        assert len(call_log) == 1  # no new transport call
        assert second == first
        assert second.labels["technical_disagreement"].probability == pytest.approx(0.6)

    def test_classify_rejects_invalid_source_with_zero_transport_calls(self, tmp_path: Path):
        call_log: list[dict] = []
        answers = {"model": EXPECTED_MODEL, "usage": _usage(), "answers": _full_answers()}
        transport = _mock_transport(fixed_response=answers, call_log=call_log)
        classifier = _classifier(tmp_path, transport=transport)
        # `NormalizedMessage.source`'s `Literal` type hint isn't runtime-checked
        # by a plain dataclass, so an invalid value can reach `classify()`.
        bad_message = NormalizedMessage("m1", "t1", "carrier_pigeon", "text")

        with pytest.raises(ValueError, match="carrier_pigeon"):
            classifier.classify(bad_message, ParentContext())

        assert call_log == []  # never paid for the call

    def test_classify_raises_cost_cap_exceeded_when_uncached_and_cap_hit(self, tmp_path: Path):
        transport = _mock_transport(
            fixed_response={"model": EXPECTED_MODEL, "usage": _usage(), "answers": _full_answers()}
        )
        cost_cap = CostCap(monthly_cap_usd=0.0, price_usd_per_million_input_tokens=0.042)
        classifier = _classifier(tmp_path, transport=transport, cost_cap=cost_cap)
        message = NormalizedMessage("m1", "t1", "mailing_list", "text")

        with pytest.raises(CostCapExceededError):
            classifier.classify(message, ParentContext())

    def test_classify_use_cache_false_bypasses_cache_and_cost_cap(self, tmp_path: Path):
        call_log: list[dict] = []
        answers = {"model": EXPECTED_MODEL, "usage": _usage(), "answers": _full_answers()}
        transport = _mock_transport(fixed_response=answers, call_log=call_log)
        # A cap that's already exceeded would normally block an uncached call,
        # but use_cache=False is an explicit, deliberate bypass of both.
        cost_cap = CostCap(monthly_cap_usd=0.0, price_usd_per_million_input_tokens=0.042)
        classifier = _classifier(tmp_path, transport=transport, cost_cap=cost_cap)
        message = NormalizedMessage("m1", "t1", "mailing_list", "text")

        record = classifier.classify(message, ParentContext(), use_cache=False)

        assert len(call_log) == 1
        assert len(classifier._cache) == 0  # never written to the cache
        assert cost_cap.spent_usd == 0.0  # never recorded against the cap
        assert record.message_id == "m1"


# --- JevClassifier.run: cache + cost cap + concurrency -------------------------------


class TestJevClassifierRun:
    def test_run_classifies_all_messages_and_populates_cache(self, tmp_path: Path):
        call_log: list[dict] = []
        answers = {"model": EXPECTED_MODEL, "usage": _usage(50, 10), "answers": _full_answers()}
        transport = _mock_transport(fixed_response=answers, call_log=call_log)
        cache_path = tmp_path / "cache.jsonl"
        classifier = _classifier(tmp_path, async_transport=transport, cache_path=cache_path)
        items = [
            (NormalizedMessage("m1", "t1", "mailing_list", "first message"), ParentContext()),
            (NormalizedMessage("m2", "t1", "mailing_list", "second message"), ParentContext()),
            (NormalizedMessage("m3", "t2", "jira_comment", "third message"), ParentContext()),
        ]
        result = classifier.run(items)

        assert isinstance(result, RunResult)
        assert result.status == "completed"
        assert result.calls_made == 3
        assert result.cache_hits == 0
        assert len(result.records) == 3
        assert {r.message_id for r in result.records} == {"m1", "m2", "m3"}
        assert result.input_tokens_used == 150
        assert result.output_tokens_used == 30
        assert len(call_log) == 3
        assert len(ClassificationCache(cache_path)) == 3

    def test_run_never_resends_a_message_whose_input_hash_is_already_cached(self, tmp_path: Path):
        call_log: list[dict] = []
        transport = _mock_transport(
            fixed_response={"model": EXPECTED_MODEL, "usage": _usage(), "answers": _full_answers()},
            call_log=call_log,
        )
        cache_path = tmp_path / "cache.jsonl"
        items = [
            (NormalizedMessage("m1", "t1", "mailing_list", "same text"), ParentContext()),
        ]

        first = _classifier(tmp_path, async_transport=transport, cache_path=cache_path)
        result1 = first.run(items)
        assert result1.calls_made == 1
        assert len(call_log) == 1

        # A brand-new classifier + cache instance pointed at the same path --
        # proves persistence, not just in-memory reuse.
        second = _classifier(tmp_path, async_transport=transport, cache_path=cache_path)
        result2 = second.run(items)
        assert result2.calls_made == 0
        assert result2.cache_hits == 1
        assert len(result2.records) == 1
        assert len(call_log) == 1  # no new network call

    def test_run_deduplicates_identical_messages_within_a_single_run(self, tmp_path: Path):
        call_log: list[dict] = []
        transport = _mock_transport(
            fixed_response={"model": EXPECTED_MODEL, "usage": _usage(), "answers": _full_answers()},
            call_log=call_log,
        )
        classifier = _classifier(tmp_path, async_transport=transport, concurrency=1)
        same_message = NormalizedMessage("m1", "t1", "mailing_list", "identical text")
        items = [(same_message, ParentContext()), (same_message, ParentContext())]
        result = classifier.run(items)
        assert result.calls_made == 1
        assert result.cache_hits == 1
        assert len(result.records) == 2

    def test_run_pauses_cleanly_when_cost_cap_is_hit_and_returns_partial_results(
        self, tmp_path: Path
    ):
        transport = _mock_transport(
            fixed_response={
                "model": EXPECTED_MODEL,
                "usage": _usage(input_tokens=1_000_000, output_tokens=10),
                "answers": _full_answers(),
            }
        )
        # The very first message still goes through (cap checked *before* each
        # call, and nothing has been spent yet); its 1M input tokens cost
        # $0.042 at this price, which alone already exceeds this $0.01 cap, so
        # the second message is never sent.
        cost_cap = CostCap(monthly_cap_usd=0.01, price_usd_per_million_input_tokens=0.042)
        classifier = _classifier(
            tmp_path, async_transport=transport, cost_cap=cost_cap, concurrency=1
        )
        items = [
            (NormalizedMessage(f"m{i}", "t1", "mailing_list", f"message {i}"), ParentContext())
            for i in range(5)
        ]

        result = classifier.run(items)

        assert result.status == "paused_cost_cap"
        assert result.calls_made == 1
        assert len(result.records) == 1
        assert result.estimated_cost_usd == pytest.approx(0.042)
        assert cost_cap.exceeded

    def test_run_never_raises_when_cost_cap_is_hit(self, tmp_path: Path):
        transport = _mock_transport(
            fixed_response={
                "model": EXPECTED_MODEL,
                "usage": _usage(input_tokens=1_000_000, output_tokens=10),
                "answers": _full_answers(),
            }
        )
        cost_cap = CostCap(monthly_cap_usd=0.0, price_usd_per_million_input_tokens=0.042)
        classifier = _classifier(tmp_path, async_transport=transport, cost_cap=cost_cap)
        items = [
            (NormalizedMessage("m1", "t1", "mailing_list", "text"), ParentContext()),
        ]
        # Cap already exceeded (0.0 cap) before the first call -- must pause
        # with zero calls made, not raise.
        result = classifier.run(items)
        assert result.status == "paused_cost_cap"
        assert result.calls_made == 0
        assert result.records == []

    def test_run_respects_concurrency_limit(self, tmp_path: Path):
        in_flight = {"current": 0, "max_seen": 0}

        def handler(request: httpx2.Request) -> httpx2.Response:
            in_flight["current"] += 1
            in_flight["max_seen"] = max(in_flight["max_seen"], in_flight["current"])
            in_flight["current"] -= 1
            return httpx2.Response(
                200,
                json={"model": EXPECTED_MODEL, "usage": _usage(), "answers": _full_answers()},
            )

        transport = httpx2.MockTransport(handler)
        classifier = _classifier(tmp_path, async_transport=transport, concurrency=2)
        items = [
            (NormalizedMessage(f"m{i}", "t1", "mailing_list", f"text {i}"), ParentContext())
            for i in range(6)
        ]
        result = classifier.run(items)
        assert result.calls_made == 6
        assert in_flight["max_seen"] <= 2


# --- Retries (429/529) ----------------------------------------------------------------


class TestRetries:
    def test_default_retryable_statuses_include_429_and_529(self):
        assert 429 in DEFAULT_RETRYABLE_STATUSES
        assert 529 in DEFAULT_RETRYABLE_STATUSES

    def test_classify_retries_on_429_then_succeeds(self, tmp_path: Path):
        responses = [
            (429, {"error": "rate limited"}),
            (200, {"model": EXPECTED_MODEL, "usage": _usage(), "answers": _full_answers(0.9)}),
        ]
        transport = _mock_transport(responses=responses)
        classifier = _classifier(
            tmp_path,
            transport=transport,
            retry=RetryPolicy(max_retries=2, backoff_initial=0, backoff_max=0, backoff_jitter=0),
        )
        record = classifier.classify(
            NormalizedMessage("m1", "t1", "mailing_list", "text"), ParentContext()
        )
        assert record.labels["technical_disagreement"].probability == pytest.approx(0.9)

    def test_classify_retries_on_529_then_succeeds(self, tmp_path: Path):
        responses = [
            (529, {"error": "overloaded"}),
            (200, {"model": EXPECTED_MODEL, "usage": _usage(), "answers": _full_answers(0.2)}),
        ]
        transport = _mock_transport(responses=responses)
        classifier = _classifier(
            tmp_path,
            transport=transport,
            retry=RetryPolicy(max_retries=2, backoff_initial=0, backoff_max=0, backoff_jitter=0),
        )
        record = classifier.classify(
            NormalizedMessage("m1", "t1", "mailing_list", "text"), ParentContext()
        )
        assert record.labels["technical_disagreement"].probability == pytest.approx(0.2)


# --- schema/tables.py registration -----------------------------------------------------


def test_classification_table_is_registered_in_schema_registry():
    schema = get_schema("classification")
    assert "record_id" in schema.names
    assert "input_hash" in schema.names
    assert "labels" in schema.names


# --- Live smoke test (opt-in) -----------------------------------------------------------


class TestLiveSmoke:
    """Real check for issue #45: 3 SYNTHETIC messages through the real Jev API,
    then a second run against the same (fresh, on-disk) cache proving 0 new
    calls. Skipped unless RUN_LIVE_NETWORK_TESTS=1 (tests/conftest.py)."""

    @pytest.mark.live_network
    def test_three_synthetic_messages_then_cache_hit_on_second_run(self, tmp_path: Path):
        found = load_jev_key_from_dotenv()
        assert found or os.environ.get("TYPESAFE_API_KEY"), (
            "no TYPESAFE_API_KEY available (neither .env's jev_key nor the env var)"
        )

        cache_path = tmp_path / "live_smoke_cache.jsonl"
        cost_cap = CostCap.from_config(monthly_cap_usd=1.0)

        synthetic_items = [
            (
                NormalizedMessage(
                    "synthetic-msg-1",
                    "synthetic-thread-1",
                    "mailing_list",
                    "This is a synthetic pilot message for issue #45's live smoke test. "
                    "I don't think the proposed change is correct: it removes the retry "
                    "loop entirely instead of just shortening its backoff.",
                ),
                ParentContext(text=None),
            ),
            (
                NormalizedMessage(
                    "synthetic-msg-2",
                    "synthetic-thread-1",
                    "mailing_list",
                    "Good catch, you're right that removing it entirely goes too far. "
                    "Let's shorten the backoff instead and keep the retry loop.",
                ),
                ParentContext(
                    text="I don't think the proposed change is correct: it removes the "
                    "retry loop entirely instead of just shortening its backoff."
                ),
            ),
            (
                NormalizedMessage(
                    "synthetic-msg-3",
                    "synthetic-thread-2",
                    "jira_comment",
                    "LGTM, +1, committing this synthetic patch for the pilot smoke test.",
                ),
                ParentContext(text=None),
            ),
        ]

        classifier_run_1 = JevClassifier(
            cache=ClassificationCache(cache_path),
            cost_cap=cost_cap,
            concurrency=2,
        )
        result_1 = classifier_run_1.run(synthetic_items)

        print(f"\n[live smoke, run 1] status={result_1.status}")
        print(
            f"[live smoke, run 1] calls_made={result_1.calls_made} "
            f"cache_hits={result_1.cache_hits}"
        )
        print(
            f"[live smoke, run 1] input_tokens={result_1.input_tokens_used} "
            f"output_tokens={result_1.output_tokens_used}"
        )
        print(f"[live smoke, run 1] estimated_cost_usd={result_1.estimated_cost_usd:.6f}")
        for record in result_1.records:
            print(f"[live smoke, run 1] {record.message_id} model_id={record.model_id}")
            for label_id in sorted(record.labels):
                print(f"    {label_id}: {record.labels[label_id].probability:.3f}")
            if record.tone_intensity is not None:
                print(
                    f"    tone_intensity: score={record.tone_intensity.score:.3f} "
                    f"confidence={record.tone_intensity.confidence:.3f}"
                )

        assert result_1.status == "completed"
        assert result_1.calls_made == 3
        assert result_1.cache_hits == 0
        assert len(result_1.records) == 3
        for record in result_1.records:
            assert set(record.labels) == MESSAGE_LEVEL_LABELS
            assert record.model_id  # provider-reported model, non-empty
            assert record.tone_intensity is not None

        # Second run: a brand-new classifier + cache instance reading the same
        # on-disk cache file -- must make zero new calls.
        classifier_run_2 = JevClassifier(
            cache=ClassificationCache(cache_path),
            cost_cap=CostCap.from_config(monthly_cap_usd=1.0),
            concurrency=2,
        )
        result_2 = classifier_run_2.run(synthetic_items)

        print(f"[live smoke, run 2] status={result_2.status}")
        print(
            f"[live smoke, run 2] calls_made={result_2.calls_made} "
            f"cache_hits={result_2.cache_hits}"
        )

        assert result_2.calls_made == 0
        assert result_2.cache_hits == 3
        assert len(result_2.records) == 3
