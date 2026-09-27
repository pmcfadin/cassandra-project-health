"""Per-dataset source-category labeling for the public report's "separation
by source category" table (orchestrator review of issue #89).

A *categorizer* takes one `DatasetItem.raw_labels` dict and returns the list
of the dataset's own fine-grained category names that item belongs to (a
list, not a single value, because Ferreira's `tbdf_categories` is itself a
set -- one message can carry several TBDF categories at once, and the
separation table counts it once under each). An item matching no category at
all (e.g. a Ferreira message never TBDF-coded) falls under the sentinel
`"(none coded)"`.

This is deliberately independent of `mapping.py`'s label mappings: the
separation table's whole point (orchestrator review) is to show *how well
Jev's raw label probabilities separate the dataset's own fine-grained
categories*, not to re-derive our own positive/negative ground truth.
"""

from __future__ import annotations

from typing import Any, Callable

NONE_CODED = "(none coded)"

Categorizer = Callable[[dict[str, Any]], list[str]]

_CATEGORIZERS: dict[str, Categorizer] = {}


def register_categorizer(name: str) -> Callable[[Categorizer], Categorizer]:
    def decorator(fn: Categorizer) -> Categorizer:
        if name in _CATEGORIZERS:
            raise ValueError(f"categorizer {name!r} already registered")
        _CATEGORIZERS[name] = fn
        return fn

    return decorator


def get_categorizer(name: str) -> Categorizer:
    try:
        return _CATEGORIZERS[name]
    except KeyError:
        raise KeyError(
            f"no categorizer registered as {name!r}; known categorizers: {sorted(_CATEGORIZERS)}"
        ) from None


@register_categorizer("categorize_ferreira")
def categorize_ferreira(raw_labels: dict[str, Any]) -> list[str]:
    categories = raw_labels.get("tbdf_categories") or frozenset()
    return sorted(categories) if categories else [NONE_CODED]


@register_categorizer("categorize_toxicr")
def categorize_toxicr(raw_labels: dict[str, Any]) -> list[str]:
    return ["toxic"] if raw_labels.get("is_toxic") else ["not_toxic"]


@register_categorizer("categorize_talkdown")
def categorize_talkdown(raw_labels: dict[str, Any]) -> list[str]:
    return ["condescending"] if raw_labels.get("label") else ["not_condescending"]


@register_categorizer("categorize_wikipedia_attacks")
def categorize_wikipedia_attacks(raw_labels: dict[str, Any]) -> list[str]:
    return ["attack"] if raw_labels.get("attack") else ["no_attack"]
