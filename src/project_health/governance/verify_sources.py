"""`project-health verify-policy-sources` (D24, issue #93).

D24: "Enforcement: a test fails if any scored rule or exemption lacks a
source. `project-health verify-policy-sources` re-fetches each source and
confirms each quote still appears, so a change to the project's rules is
noticed."

`tests/test_governance_policy_sources.py` enforces that every scored rule,
exemption and sub_pattern *carries* a source (structural completeness, no
network access). This module is the complementary *live* check: it walks
those same sourced items, fetches each `source_url` fresh, and confirms
`source_quote` still appears there -- so if the Cassandra project ever edits
or removes the governance page, a contributor doc, or the release tooling
this policy cites, that drift is caught instead of silently going stale.

## Fetching per `source_type` (measurement choices, D24 preamble: "how
   evidence is found... are measurement choices this project makes")

- `cwiki.apache.org/confluence/display/<SPACE>/<Title>` (`ratified_governance`)
  is fetched through Confluence's REST content API
  (`.../confluence/rest/api/content?spaceKey=<SPACE>&title=<Title>&expand=
  body.storage`) rather than scraping the rendered `/display/` page, which
  can carry extra chrome (space sidebar, related-pages links) the storage
  body doesn't. The `body.storage.value` is Confluence storage format
  (XHTML-like) -- stripped of tags the same way plain HTML is.
- `github.com/<owner>/<repo>/blob/<ref>/<path>` (`official_tooling`) is
  rewritten to `raw.githubusercontent.com/<owner>/<repo>/<ref>/<path>` and
  fetched as plain text -- no HTML stripping needed, it's already source.
- Anything else (`official_docs`, e.g. `cassandra.apache.org/_/development/
  *.html`) is fetched as HTML and tag-stripped, then HTML-entity-unescaped.

## Quote matching

`source_quote` is meant to be a verbatim excerpt, but three things
legitimately differ between the YAML string and the live page without the
quote being wrong: curly vs. straight quote marks, incidental whitespace
(line wraps, repeated spaces), and a quote written with a literal `"..."`
marking an elided span. `normalize_quote_text` folds the first two;
`quote_fragments` splits on the third, and `quote_found` requires every
resulting fragment to appear (in order-independent fashion -- multi-fragment
quotes in this file are always contiguous prose with a short elision, never
reordered) in the normalized page text.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlencode, urlparse

import httpx
import yaml

from project_health.governance.policy import DEFAULT_POLICY_PATH

DEFAULT_TIMEOUT = 30.0
USER_AGENT = "cassandra-project-health/verify-policy-sources (+D24 governance-policy.yaml audit)"


class SourceFetchError(RuntimeError):
    """Raised when a `source_url` can't be fetched or parsed at all."""


@dataclass(frozen=True)
class SourcedItem:
    """One quote this module checks, at a dotted `path` into
    `governance-policy.yaml`'s `rules[]` (e.g. `"reviewer-present"`,
    `"pre-commit-ci-evidence.exemptions.not-code"`,
    `"reviewer-present.exemptions.release-process.sub_patterns.
    version-increment"`, or `"ci-artefacts-attached.additional_sources[0]"`)."""

    path: str
    source_type: str
    source_url: str
    source_quote: str


@dataclass(frozen=True)
class VerificationResult:
    item: SourcedItem
    ok: bool
    detail: str


def iter_sourced_items(raw_policy: dict) -> list[SourcedItem]:
    """Every `(source_url, source_quote)` pair this policy's *scored* rules
    cite -- a `same_as` exemption is skipped (its target is a different
    item, walked when its own owning rule is visited), and an exemption with
    `sub_patterns` is represented by its sub_patterns, not itself (matching
    `tests/test_governance_policy_sources.py`'s structural walk)."""
    items: list[SourcedItem] = []
    for rule in raw_policy.get("rules", []):
        if not rule.get("scored", True):
            continue
        rule_id = rule["id"]
        if rule.get("source_url") and rule.get("source_quote"):
            items.append(
                SourcedItem(rule_id, rule["source_type"], rule["source_url"], rule["source_quote"])
            )

        for exemption in rule.get("exemptions", []):
            if "same_as" in exemption:
                continue
            exemption_path = f"{rule_id}.exemptions.{exemption['id']}"
            if "sub_patterns" in exemption:
                for sub in exemption["sub_patterns"]:
                    if sub.get("source_url") and sub.get("source_quote"):
                        items.append(
                            SourcedItem(
                                f"{exemption_path}.sub_patterns.{sub['id']}",
                                sub["source_type"],
                                sub["source_url"],
                                sub["source_quote"],
                            )
                        )
                continue
            if exemption.get("source_url") and exemption.get("source_quote"):
                items.append(
                    SourcedItem(
                        exemption_path,
                        exemption["source_type"],
                        exemption["source_url"],
                        exemption["source_quote"],
                    )
                )

        for i, extra in enumerate(rule.get("additional_sources", []) or []):
            if extra.get("url") and extra.get("quote"):
                items.append(
                    SourcedItem(
                        f"{rule_id}.additional_sources[{i}]",
                        rule.get("source_type", ""),
                        extra["url"],
                        extra["quote"],
                    )
                )
    return items


# --- Fetching -----------------------------------------------------------


def _strip_html(markup: str) -> str:
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", markup)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return html.unescape(text)


def _cwiki_rest_url(display_url: str) -> str:
    """`.../confluence/display/<SPACE>/<Title>` -> `.../confluence/rest/api/
    content?spaceKey=<SPACE>&title=<Title>&expand=body.storage`."""
    parsed = urlparse(display_url)
    segments = [p for p in parsed.path.split("/") if p]
    try:
        idx = segments.index("display")
        space_key = segments[idx + 1]
        title = unquote(segments[idx + 2]).replace("+", " ")
    except (ValueError, IndexError) as exc:
        raise SourceFetchError(f"can't parse cwiki display URL: {display_url}") from exc
    query = urlencode({"spaceKey": space_key, "title": title, "expand": "body.storage"})
    return f"{parsed.scheme}://{parsed.netloc}/confluence/rest/api/content?{query}"


def _github_raw_url(blob_url: str) -> str:
    """`github.com/<owner>/<repo>/blob/<ref>/<path>` ->
    `raw.githubusercontent.com/<owner>/<repo>/<ref>/<path>`."""
    if "/blob/" not in blob_url:
        raise SourceFetchError(f"not a github blob URL: {blob_url}")
    return blob_url.replace("github.com", "raw.githubusercontent.com", 1).replace(
        "/blob/", "/", 1
    )


def fetch_source_text(url: str, client: httpx.Client) -> str:
    """Fetch `url` and return plain text suitable for `quote_found` -- see
    module docstring's "Fetching per source_type" section for the three
    cases."""
    if "cwiki.apache.org" in url and "/display/" in url:
        rest_url = _cwiki_rest_url(url)
        response = client.get(rest_url, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results") or []
        if not results:
            raise SourceFetchError(f"cwiki REST API returned no page for {url} ({rest_url})")
        storage_html = results[0]["body"]["storage"]["value"]
        return _strip_html(storage_html)

    if "github.com" in url and "/blob/" in url:
        raw_url = _github_raw_url(url)
        response = client.get(raw_url, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        return response.text

    response = client.get(url, timeout=DEFAULT_TIMEOUT, follow_redirects=True)
    response.raise_for_status()
    return _strip_html(response.text)


# --- Quote matching -------------------------------------------------------

_CURLY_TO_STRAIGHT = {
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
    "′": "'",
    "″": '"',
}


def normalize_quote_text(text: str) -> str:
    """Curly -> straight quotes, collapse whitespace, drop spaces before
    punctuation -- the normalizations D24/issue #93 name explicitly. Case is
    left alone: `source_quote` is meant to be verbatim, and folding case
    would let a materially different sentence pass.

    Quote marks additionally get whitespace stripped on *both* sides (not
    just before, like other punctuation): stripping inline emphasis tags
    (`<strong>`, `<em>`) around a quoted phrase (`_strip_html` replaces each
    tag with a space so words never fuse) leaves a stray space just inside
    the quote marks that was never part of the rendered sentence (e.g. a
    cwiki `"<strong>Commit Then Review</strong>"` renders as `" Commit Then
    Review "`, not `"Commit Then Review"`). Applying the same rule to both
    the live page text and the policy's own `source_quote` before comparing
    means this never depends on guessing which side is "correct".
    """
    for curly, straight in _CURLY_TO_STRAIGHT.items():
        text = text.replace(curly, straight)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([.,;:!?\"'])", r"\1", text)
    text = re.sub(r"([\"'])\s+", r"\1", text)
    return text.strip()


_WRAPPING_OPEN_QUOTES = "\"'“‘"
_WRAPPING_CLOSE_QUOTES = "\"'”’"


def _strip_wrapping_quotes(fragment: str) -> str:
    """Drop a fragment's own outer quote marks, when the *whole* fragment is
    wrapped in one pair (e.g. this file's `source_quote` values are written
    as a quoted excerpt, `"Code modifications..."`, even where the source
    page's own text has no literal quote marks around that sentence -- it's
    a documentation convention, not part of the quoted text). This only
    fires when the wrapping quotes bracket the *entire* fragment; a fragment
    whose quote marks sit in the middle (e.g. `...operate a "Commit Then
    Review" policy`, where the source page really does contain those inner
    quote marks) is left untouched."""
    if (
        len(fragment) >= 2
        and fragment[0] in _WRAPPING_OPEN_QUOTES
        and fragment[-1] in _WRAPPING_CLOSE_QUOTES
    ):
        return fragment[1:-1].strip()
    return fragment


_STRAIGHT_QUOTE_SPAN_RE = re.compile(r'"([^"]*)"')


def quote_fragments(quote: str) -> list[str]:
    """Split `quote` into the independent fragments that must all appear
    (issue #93: "splitting on '...' into fragments that must all appear").

    Two shapes occur in this policy file: (1) one sentence wrapped in a
    single pair of straight quotes, possibly itself containing a `"..."`
    elision inside that sentence (e.g. `code-style-checkstyle`'s two
    sentences are instead handled by case 2 below; a single-sentence
    elision looks like `"...text before... text after..."`); and (2)
    *multiple*, independently double-quoted excerpts concatenated in one
    `source_quote` value with no connecting prose (e.g.
    `code-style-checkstyle`: `"Checkstyle is part of..." "The checkstyle
    target is..."` — two separate quoted sentences, not one sentence with
    an elision). Case (2) is detected by finding 2+ top-level
    straight-quote-delimited spans; each becomes its own fragment (further
    split on `...`, in case one of them also elides). Case (1) is the
    original single-fragment path, with its own outer wrapping quotes (if
    the whole thing is wrapped) stripped by `_strip_wrapping_quotes`.
    """
    spans = [span.strip() for span in _STRAIGHT_QUOTE_SPAN_RE.findall(quote) if span.strip()]
    if len(spans) >= 2:
        fragments: list[str] = []
        for span in spans:
            fragments.extend(part.strip() for part in span.split("...") if part.strip())
        return fragments

    # A single wrapped quote (0 or 1 top-level straight-quote pair): strip
    # the wrapping quotes from the *whole* string first, THEN split on
    # "..." -- splitting first would leave the opening quote glued to the
    # first fragment and the closing quote glued to the last one, since
    # each is only at one end of the un-split string, not both ends of its
    # own post-split fragment.
    unwrapped = _strip_wrapping_quotes(quote.strip())
    return [part.strip() for part in unwrapped.split("...") if part.strip()]


def quote_found(page_text: str, quote: str) -> bool:
    """A `source_quote` (or, after `quote_fragments`, each of its pieces)
    counts as found if it appears in `page_text` after normalization, OR
    (fallback) it appears with its own trailing sentence punctuation
    (`.,;:!?`) dropped. The fallback exists because several of this
    policy's quotes are single bullet-list items from cwiki, closed with a
    period for normal English quoting style even though the rendered list
    item itself has no trailing punctuation (the next bullet just starts on
    a new line) -- stripped-HTML text then runs the two together with no
    punctuation at all at that boundary. This only ever relaxes the very
    last character of a fragment, never its substance."""
    haystack = normalize_quote_text(page_text)
    fragments = quote_fragments(quote)
    if not fragments:
        return True
    for fragment in fragments:
        normalized = normalize_quote_text(fragment)
        if normalized in haystack:
            continue
        relaxed = normalized.rstrip(".,;:!?")
        if relaxed and relaxed in haystack:
            continue
        return False
    return True


# --- Top-level verification ------------------------------------------------


def verify_policy_sources(
    policy_path: str | Path = DEFAULT_POLICY_PATH,
    *,
    client: httpx.Client | None = None,
) -> list[VerificationResult]:
    """Fetch every scored source_url in `policy_path` fresh and confirm its
    source_quote still appears there. Never raises for an individual item's
    fetch failure (network error, 404, unparseable page) -- that item is
    just recorded as a failed `VerificationResult` so one dead link doesn't
    prevent checking the rest; `main`/the CLI turns any failure into a
    non-zero exit."""
    raw_policy = yaml.safe_load(Path(policy_path).read_text())
    items = iter_sourced_items(raw_policy)

    owns_client = client is None
    client = client or httpx.Client(headers={"User-Agent": USER_AGENT})
    results: list[VerificationResult] = []
    try:
        for item in items:
            try:
                text = fetch_source_text(item.source_url, client)
            except Exception as exc:  # noqa: BLE001 - one bad source must not abort the run
                results.append(VerificationResult(item, False, f"fetch failed: {exc}"))
                continue
            if quote_found(text, item.source_quote):
                results.append(VerificationResult(item, True, "quote found"))
            else:
                results.append(
                    VerificationResult(item, False, "quote NOT found on the live page")
                )
    finally:
        if owns_client:
            client.close()
    return results
