"""Test-suite-wide guardrail: no real network access (issue #36 fixup).

The orchestrator profiled the suite at 968s for 300 tests, with a single
test (`TestFileChangeEventBackfillGap::test_missing_file_change_event_
watermark_backfills_and_clears_metrics_missing`) accounting for 929s of
that -- because, at the time, it passed `run_pipeline` no `sources=`
override, so once issue #33 made `ponymail` part of the default
`ALL_SOURCES` with no stub factory given, the test crawled the real
lists.apache.org archives (36 months x 2 lists, twice: once per `run_
pipeline` call in that test). Every `run_pipeline` call in tests/test_
pipeline.py now passes an explicit `sources=` naming only the sources it
supplies a stub/mock factory for -- but that convention is easy to violate
again silently the next time a source is added to `ALL_SOURCES`, so this
module adds a hard backstop: an autouse fixture that makes any *real*
network attempt during the test suite fail loudly and immediately (a
`RuntimeError`) instead of quietly taking minutes.

Blocked at two levels:

- `httpx.HTTPTransport.handle_request` / `httpx.AsyncHTTPTransport.
  handle_async_request` -- the code path any `httpx.Client(...)` constructed
  *without* an explicit `transport=` uses. Every collector in this project
  accepts a `transport` (or a full collector-factory override) precisely so
  tests can inject an `httpx.MockTransport` instead; `MockTransport.handle_
  request` is a distinct method on a distinct class, so it is never touched
  by this patch, and every existing offline test keeps working unchanged.
- `socket.socket.connect` -- a backstop for anything that reaches the
  network without going through httpx at all.

A test that deliberately needs the real network (an opt-in live smoke test)
marks itself `@pytest.mark.live_network`, which both exempts it from the
block above and, absent `RUN_LIVE_NETWORK_TESTS=1` in the environment,
skips it outright -- so `pytest` stays fast and offline by default, and a
live check is still one env var away from running for real.
"""

from __future__ import annotations

import os
import socket

import httpx
import pytest

_BLOCK_MESSAGE = "network access in tests is forbidden; pass a mock transport/factory"


def _blocked_handle_request(self, request):  # noqa: ARG001 - matches real signature
    raise RuntimeError(_BLOCK_MESSAGE)


def _blocked_handle_async_request(self, request):  # noqa: ARG001
    raise RuntimeError(_BLOCK_MESSAGE)


def _blocked_connect(self, address):  # noqa: ARG001
    raise RuntimeError(_BLOCK_MESSAGE)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "live_network: deliberately hits the real network; skipped unless "
        "RUN_LIVE_NETWORK_TESTS=1 is set, and never subject to the autouse "
        "network block in tests/conftest.py.",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if os.environ.get("RUN_LIVE_NETWORK_TESTS") == "1":
        return
    skip_live = pytest.mark.skip(
        reason="live network test; set RUN_LIVE_NETWORK_TESTS=1 to run"
    )
    for item in items:
        if item.get_closest_marker("live_network") is not None:
            item.add_marker(skip_live)


@pytest.fixture(autouse=True)
def _block_real_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("live_network") is not None:
        return
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked_handle_request)
    monkeypatch.setattr(
        httpx.AsyncHTTPTransport, "handle_async_request", _blocked_handle_async_request
    )
    monkeypatch.setattr(socket.socket, "connect", _blocked_connect)
