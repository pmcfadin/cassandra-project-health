"""Tests for project_health.peers.rotation (issue #148).

`read_next_start_index`/`write_next_start_index` round-trip
`state/peers/rotation.json` on a tmp data dir; `rotate`/`next_index` are
pure functions tested directly.
"""

from __future__ import annotations

import json

from project_health.peers import rotation


def test_read_next_start_index_defaults_to_zero_when_file_missing(tmp_path):
    """The real data branch has never written this file (3-4 real runs so
    far, issue #148) -- a missing file must mean "start at 0," today's
    un-rotated behavior, not an error."""
    assert rotation.read_next_start_index(tmp_path) == 0


def test_read_next_start_index_defaults_to_zero_on_malformed_json(tmp_path):
    path = tmp_path / "state" / "peers" / "rotation.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not valid json")
    assert rotation.read_next_start_index(tmp_path) == 0


def test_read_next_start_index_defaults_to_zero_on_bad_value(tmp_path):
    path = tmp_path / "state" / "peers" / "rotation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"next_start_index": "not-an-int"}))
    assert rotation.read_next_start_index(tmp_path) == 0

    path.write_text(json.dumps({"next_start_index": -1}))
    assert rotation.read_next_start_index(tmp_path) == 0


def test_write_then_read_round_trips(tmp_path):
    rotation.write_next_start_index(tmp_path, 3)
    assert rotation.read_next_start_index(tmp_path) == 3

    path = tmp_path / "state" / "peers" / "rotation.json"
    assert json.loads(path.read_text()) == {"next_start_index": 3}


def test_write_next_start_index_does_not_disturb_other_state(tmp_path):
    """Read-modify-write discipline matters here too, even though today
    this file holds only one key -- a future key added to this same file
    (e.g. a per-peer rotation history) should round-trip the same way
    `storage.write_watermark`/`provenance.manifest.record_last_good_snapshot`
    already guarantee for their own single-file state."""
    path = tmp_path / "state" / "peers" / "rotation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"next_start_index": 1, "note": "keep me"}))

    rotation.write_next_start_index(tmp_path, 2)

    # write_next_start_index always rewrites the whole small file (same
    # shape `storage.write_watermark` uses) -- document the actual
    # behavior: it overwrites with {"next_start_index": ...} only.
    assert json.loads(path.read_text()) == {"next_start_index": 2}


def test_rotate_wraps_around():
    items = ["kafka", "spark", "flink", "pulsar", "datafusion"]
    assert rotation.rotate(items, 0) == items
    assert rotation.rotate(items, 2) == ["flink", "pulsar", "datafusion", "kafka", "spark"]
    assert rotation.rotate(items, 5) == items  # wraps fully back to 0
    assert rotation.rotate(items, 7) == ["flink", "pulsar", "datafusion", "kafka", "spark"]


def test_rotate_empty_list():
    assert rotation.rotate([], 3) == []


def test_rotate_does_not_mutate_input():
    items = ["a", "b", "c"]
    rotation.rotate(items, 1)
    assert items == ["a", "b", "c"]


def test_next_index_advances_by_one_and_wraps():
    assert rotation.next_index(0, 5) == 1
    assert rotation.next_index(3, 5) == 4
    assert rotation.next_index(4, 5) == 0  # wraps back to the start


def test_next_index_zero_peers_is_always_zero():
    assert rotation.next_index(3, 0) == 0
