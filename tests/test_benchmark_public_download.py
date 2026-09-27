"""Tests for project_health.benchmark_public.download (issue #89).

Fully offline: every `httpx` call goes through an injected `httpx.MockTransport`
-- `tests/conftest.py`'s suite-wide network block would fail any real request
regardless.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from project_health.benchmark_public.download import (
    ChecksumMismatchError,
    DownloadError,
    PinnedFile,
    fetch_pinned,
    sha256_file,
)

CONTENT = b"hello, benchmark-public\n"
SHA256 = hashlib.sha256(CONTENT).hexdigest()


def _transport(status: int = 200, body: bytes = CONTENT) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body)

    return httpx.MockTransport(handler)


def test_sha256_file(tmp_path: Path) -> None:
    path = tmp_path / "f.bin"
    path.write_bytes(CONTENT)
    assert sha256_file(path) == SHA256


def test_fetch_pinned_downloads_and_verifies(tmp_path: Path) -> None:
    pinned = PinnedFile(url="https://example.org/data.csv", sha256=SHA256, filename="data.csv")
    result = fetch_pinned(pinned, tmp_path, transport=_transport())
    assert result == tmp_path / "data.csv"
    assert result.read_bytes() == CONTENT


def test_fetch_pinned_is_free_on_cache_hit(tmp_path: Path) -> None:
    pinned = PinnedFile(url="https://example.org/data.csv", sha256=SHA256, filename="data.csv")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=CONTENT)

    transport = httpx.MockTransport(handler)
    fetch_pinned(pinned, tmp_path, transport=transport)
    assert calls["n"] == 1
    fetch_pinned(pinned, tmp_path, transport=transport)
    assert calls["n"] == 1  # second call never hit the network


def test_fetch_pinned_rejects_checksum_mismatch(tmp_path: Path) -> None:
    pinned = PinnedFile(
        url="https://example.org/data.csv", sha256="0" * 64, filename="data.csv"
    )
    with pytest.raises(ChecksumMismatchError):
        fetch_pinned(pinned, tmp_path, transport=_transport())
    # A failed download must not leave a wrongly-named file behind to be
    # mistaken for a verified cache hit on a later run.
    assert not (tmp_path / "data.csv").is_file()


def test_fetch_pinned_rejects_mismatched_existing_cache(tmp_path: Path) -> None:
    dest = tmp_path / "data.csv"
    dest.write_bytes(b"something else entirely")
    pinned = PinnedFile(url="https://example.org/data.csv", sha256=SHA256, filename="data.csv")
    with pytest.raises(ChecksumMismatchError):
        fetch_pinned(pinned, tmp_path, transport=_transport())


def test_fetch_pinned_raises_download_error_after_retries(tmp_path: Path) -> None:
    pinned = PinnedFile(url="https://example.org/data.csv", sha256=SHA256, filename="data.csv")
    with pytest.raises(DownloadError):
        fetch_pinned(
            pinned,
            tmp_path,
            transport=_transport(status=500),
            max_retries=2,
            sleep_fn=lambda _seconds: None,
        )
