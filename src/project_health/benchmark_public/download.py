"""Pinned download + sha256 verification for `benchmark-public` (issue #89).

Every URL/checksum pair a caller passes here comes from `registry.py`'s
`datasets_v1.yaml` -- this module has no opinion about *which* datasets exist,
only about fetching one already-pinned file safely: download once into
`--cache-dir` (never the repo, D23: "downloads live in the cache dir"), verify
its sha256 against the pinned value before returning, and skip the network
entirely on a re-run if a file with the right name and hash is already
cached.

Uses `httpx` (already a project dependency) directly rather than
`classify/text_fetch.py`'s paced fetchers -- those are purpose-built for the
polite, paginated ASF Pony Mail/JIRA APIs; a one-shot dataset-archive download
from GitHub/figshare has no pagination or politeness-cap concern, just a
retryable GET.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_RETRIES = 3
_BACKOFF_BASE = 1.0


class DownloadError(RuntimeError):
    """Raised when a pinned download fails or its sha256 doesn't match."""


class ChecksumMismatchError(DownloadError):
    """Raised when a downloaded (or already-cached) file's sha256 doesn't
    match the pinned value in `datasets_v1.yaml`. Never silently accepted --
    a mismatch means either the registry's pin is stale or the upstream file
    changed, both of which need a human to look, not a re-download that
    might paper over a supply-chain problem."""


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class PinnedFile:
    """One file to fetch: `registry.py`'s `DatasetFile`, decoupled from that
    module so this one stays a generic "fetch a pinned URL" utility."""

    url: str
    sha256: str
    filename: str


def fetch_pinned(
    pinned: PinnedFile,
    cache_dir: str | Path,
    *,
    transport: httpx.BaseTransport | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    timeout: float = DEFAULT_TIMEOUT,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> Path:
    """Return the local path to `pinned`, downloading it into `cache_dir`
    first if not already cached with the right hash.

    Raises `ChecksumMismatchError` if a cached (or freshly downloaded) file's
    sha256 doesn't match `pinned.sha256`, and `DownloadError` if every retry
    of the download itself fails. Never overwrites a hash-mismatched cached
    file silently -- the caller must delete it (or fix the registry pin) and
    re-run, so a bad cache never gets "quietly repaired" into a different
    unpinned file.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / pinned.filename

    if dest.is_file():
        actual = sha256_file(dest)
        if actual != pinned.sha256:
            raise ChecksumMismatchError(
                f"{dest} already exists but sha256 {actual} != pinned {pinned.sha256} "
                f"for {pinned.url}; delete it or fix the registry pin before re-running"
            )
        return dest

    last_error: Exception | None = None
    with httpx.Client(transport=transport, timeout=timeout, follow_redirects=True) as client:
        for attempt in range(1, max_retries + 1):
            try:
                response = client.get(pinned.url)
                response.raise_for_status()
                tmp = dest.with_suffix(dest.suffix + ".partial")
                tmp.write_bytes(response.content)
                actual = sha256_file(tmp)
                if actual != pinned.sha256:
                    tmp.unlink(missing_ok=True)
                    raise ChecksumMismatchError(
                        f"downloaded {pinned.url} but sha256 {actual} != pinned "
                        f"{pinned.sha256}"
                    )
                tmp.rename(dest)
                return dest
            except ChecksumMismatchError:
                raise
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_error = exc
                if attempt >= max_retries:
                    break
                sleep_fn(_BACKOFF_BASE * (2 ** (attempt - 1)))

    raise DownloadError(
        f"failed to download {pinned.url} after {max_retries} attempt(s): {last_error}"
    )
