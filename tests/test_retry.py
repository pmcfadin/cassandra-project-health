"""Tests for project_health.collectors.retry (issue #86).

The shared backoff-delay formula and body-error classification every
collector's own retry loop now imports rather than redefining locally.
"""

from __future__ import annotations

import json

import httpx
import pytest

from project_health.collectors.retry import (
    BACKOFF_CAP,
    exponential_backoff,
    is_transient_body_error,
)


class TestExponentialBackoff:
    def test_grows_with_attempt_number(self):
        # Jitter adds up to 25% on top of the base exponential value, so
        # compare against the next attempt's un-jittered floor to avoid a
        # flaky overlap.
        assert exponential_backoff(1, base=1.0, cap=100.0) < 2.0
        assert exponential_backoff(2, base=1.0, cap=100.0) >= 2.0
        assert exponential_backoff(3, base=1.0, cap=100.0) >= 4.0

    def test_capped_at_max_delay(self):
        for attempt in range(1, 20):
            assert exponential_backoff(attempt, base=0.5, cap=BACKOFF_CAP) <= BACKOFF_CAP * 1.25

    def test_never_negative(self):
        assert exponential_backoff(1) >= 0


class TestIsTransientBodyError:
    def test_json_decode_error_is_transient(self):
        try:
            json.loads("{not valid json")
        except json.JSONDecodeError as exc:
            assert is_transient_body_error(exc) is True
        else:
            pytest.fail("expected json.JSONDecodeError")

    def test_remote_protocol_error_is_transient(self):
        exc = httpx.RemoteProtocolError("incomplete chunked read")
        assert is_transient_body_error(exc) is True

    def test_unrelated_error_is_not_transient(self):
        assert is_transient_body_error(ValueError("unrelated")) is False
        assert is_transient_body_error(KeyError("data")) is False
