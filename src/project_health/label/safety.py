"""Path safety guard for the local labeling tool (DECISIONS.md D18, D22;
issue #46): "refuse to start if `--labels` or `--corpus` resolve inside the
public repo's git toplevel."

Mirrors the spirit of `classify/text_fetch.py`'s `guard_write_path` (issue
#43), but locates the public repo root via the actual git checkout (`git
rev-parse --show-toplevel`) rather than a fixed relative-parents count, since
this guard runs from an *installed* console script, which may not sit at a
fixed depth under the repo root the way `text_fetch.py`'s module file does.

If the tool isn't running from inside any git checkout at all (e.g.
installed from a wheel with no `.git` in sight), there is no public repo to
guard against, and every path is treated as safe -- callers that want a
guaranteed check regardless should pass an explicit `repo_root`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class UnsafePathError(RuntimeError):
    """Raised when `--corpus` or `--labels` resolves inside the public repo."""


def find_public_repo_root(start: Path | None = None) -> Path | None:
    """Best-effort `git rev-parse --show-toplevel` from `start` (default: this
    file's own directory). Returns `None` if not inside a git checkout, or if
    git itself isn't available -- callers must treat `None` as "unknown,
    cannot verify" rather than "safe"."""
    cwd = start if start is not None else Path(__file__).resolve().parent
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    toplevel = result.stdout.strip()
    if not toplevel:
        return None
    return Path(toplevel).resolve()


def assert_outside_repo(path: Path | str, repo_root: Path | None, *, label: str) -> Path:
    """Raise `UnsafePathError` if `path` resolves to `repo_root` or a path
    beneath it; otherwise return the resolved path. A `repo_root` of `None`
    (no git checkout found) skips the check -- there is nothing to guard
    against."""
    resolved = Path(path).expanduser().resolve()
    if repo_root is not None and (resolved == repo_root or repo_root in resolved.parents):
        raise UnsafePathError(
            f"refusing to start: --{label} ({resolved}) resolves inside the public repo "
            f"checkout ({repo_root}). Corpus and label files must live outside the public "
            "repo -- e.g. in a checkout of the private benchmark repo (DECISIONS.md D18)."
        )
    return resolved
