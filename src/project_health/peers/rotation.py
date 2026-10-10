"""Per-run peer-order rotation (issue #148).

Problem this fixes: `projects/peers.yaml`'s `peers:` list is always read in
the same fixed order (kafka, spark, flink, pulsar, datafusion), and
`peers.collect.collect_and_write_github` stops *every* remaining (peer,
pass) pair the moment any one pass hits the shared GraphQL rate-limit
floor (`budget_exhausted`, same "a shared budget means some work finishing
cleanly beats everything finishing half finished" discipline
`collectors/github.py`'s own module docstring documents). After 3-4 real
weekly runs, kafka/spark's own large, still-incomplete `created_desc`
backfills consumed that whole budget every single run, so flink/pulsar/
datafusion never got a single pass to run at all -- real-run finding,
2026-10-10: only 3/5 peers had any `raw/peers/<id>/github` data.

The fix: each run starts from a rotating offset into `peers_config.peers`
instead of always starting at index 0, persisted here as
`state/peers/rotation.json`'s `next_start_index` -- a small, standalone
JSON file under `state/`, same "its own dedicated `state/<name>.json`"
convention `provenance.manifest`'s `state/last_good_runs.json` already
establishes, rather than `project_health.storage`'s watermark API itself
(keyed by `source`/`table`, not by "the whole peers run"). Unlike that
file's per-source read-modify-write, this file holds exactly one value,
so `write_next_start_index` simply overwrites it each run.

A missing (or unreadable/malformed) rotation file means "start at 0" --
exactly the data branch's current real state (3-4 real runs so far, no
`state/peers/rotation.json` ever written) and exactly today's un-rotated
behavior, so this is a purely additive, backward-compatible change for any
data branch this ships against.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence, TypeVar

T = TypeVar("T")


def _rotation_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "state" / "peers" / "rotation.json"


def read_next_start_index(data_dir: str | Path) -> int:
    """The rotation offset to start *this* run at.

    `0` (today's un-rotated behavior) whenever the file is missing, not
    valid JSON, or its `next_start_index` isn't a non-negative int --
    never raises, same "a missing/corrupt state file degrades to the safe
    default, never a hard failure" discipline every other `state/*.json`
    reader in this project (`storage.read_watermark`, `provenance.manifest.
    read_last_good_snapshot`) already follows.
    """
    path = _rotation_path(data_dir)
    if not path.is_file():
        return 0
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return 0
    value = data.get("next_start_index", 0)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return 0
    return value


def write_next_start_index(data_dir: str | Path, next_start_index: int) -> None:
    """Persist the offset the *next* run should start at."""
    path = _rotation_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"next_start_index": next_start_index}, indent=2, sort_keys=True))


def rotate(items: Sequence[T], start_index: int) -> list[T]:
    """`items` reordered to start at `start_index % len(items)`, wrapping
    around -- e.g. `rotate([a, b, c, d, e], 2) == [c, d, e, a, b]`.

    `[]` for an empty `items` (never divides by zero); `start_index` may be
    any non-negative int, including one larger than `len(items)` (e.g. a
    rotation file left over from a run with more peers than today's
    config) -- `%` always brings it back in range.
    """
    items = list(items)
    if not items:
        return items
    offset = start_index % len(items)
    return items[offset:] + items[:offset]


def next_index(start_index: int, num_items: int) -> int:
    """The offset *next* run should start at, advancing by exactly one peer
    per run (module docstring) -- `0` whenever `num_items <= 0` (an empty
    peer list is never a rotation to resume)."""
    if num_items <= 0:
        return 0
    return (start_index + 1) % num_items
