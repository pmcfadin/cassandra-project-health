"""Per-run author salt for the private Cassandra communication run (issue
#114; COMMUNITY-HEALTH.md §7.2/§7.3).

Every internal in-memory computation in this package (thread derivation,
newcomer determination, pile-on target counting) works on raw sender/author
strings -- exactly like `runner.py`'s pre-existing `_PendingMessage.
author_raw`, which the module docstring there already documents as "used
only to build an in-memory *distinct-author count*" and discarded once the
run finishes. Nothing in this module changes that: raw author strings never
reach `aggregates.json`/`report.md`.

The **one** place a raw author string must not appear even in a private,
non-published file is `--out/message_index.jsonl` (issue #114's per-message
private index) -- a durable, on-disk artifact, unlike the run's in-memory
state. This module provides the salted hash used there: a per-`--out`-
directory salt, generated once and persisted (`author_salt.txt`), so the
same contributor hashes to the same `author_key` across every run against
one `--out` directory (needed for the index to stay internally consistent
run over run), while remaining infeasible to reverse without the salt file
itself.
"""

from __future__ import annotations

import hashlib
import secrets
from pathlib import Path

DEFAULT_SALT_FILENAME = "author_salt.txt"


def load_or_create_salt(out_dir: str | Path, filename: str = DEFAULT_SALT_FILENAME) -> str:
    """Read the salt from `out_dir/filename`, creating it (32 random bytes,
    hex-encoded) on first use. Stable across runs against the same `--out`
    directory.
    """
    path = Path(out_dir) / filename
    if path.is_file():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    salt = secrets.token_hex(32)
    path.write_text(salt + "\n", encoding="utf-8")
    return salt


def hash_author(raw_author: str | None, salt: str) -> str:
    """A salted, non-reversible `author_key` for `raw_author`. Case/
    whitespace-normalized before hashing so the same contributor's address
    hashes identically regardless of incidental formatting differences
    (e.g. Pony Mail's partially-obfuscated display-name capitalization)."""
    normalized = (raw_author or "").strip().lower()
    digest = hashlib.sha256(f"{salt}:{normalized}".encode("utf-8")).hexdigest()
    return digest[:32]
