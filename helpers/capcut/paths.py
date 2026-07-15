"""CapCut drafts-root detection and startup sanity check."""

from __future__ import annotations

import json
from pathlib import Path

# Standard install and sandboxed-container variants (mac).
_REL = "Movies/CapCut/User Data/Projects/com.lveditor.draft"
_CANDIDATE_ROOTS = [
    Path.home() / _REL,
    Path.home() / "Library/Containers/com.lemon.lvoverseas/Data" / _REL,
]


def _count_projects(root: Path) -> int:
    """Number of subfolders that look like CapCut projects (have draft_info.json)."""
    if not root.is_dir():
        return 0
    n = 0
    for child in root.iterdir():
        if child.is_dir() and (child / "draft_info.json").exists():
            n += 1
    return n


def detect_drafts_root(override: str | None = None) -> Path:
    """Return the CapCut drafts root.

    If `override` is given (and not "auto"), it is used directly. Otherwise
    pick, among the known candidates, the one that already contains the most
    projects (falling back to the first existing directory).
    """
    if override and override != "auto":
        p = Path(override).expanduser()
        if not p.is_dir():
            raise FileNotFoundError(f"--drafts-root does not exist: {p}")
        return p

    existing = [r for r in _CANDIDATE_ROOTS if r.is_dir()]
    if not existing:
        raise FileNotFoundError(
            "Could not find a CapCut drafts root. Looked in:\n  "
            + "\n  ".join(str(r) for r in _CANDIDATE_ROOTS)
            + "\nPass --drafts-root <path> explicitly."
        )
    existing.sort(key=_count_projects, reverse=True)
    return existing[0]


def sanity_check_root(root: Path) -> None:
    """Confirm drafts are plaintext JSON on this CapCut version (Risk 2).

    Reads one existing project's draft_info.json and asserts it parses as JSON.
    Raises RuntimeError with a clear message if the format looks encrypted or
    otherwise unreadable. No-op if the root has no projects yet.
    """
    sample = None
    for child in sorted(root.iterdir()):
        cand = child / "draft_info.json"
        if child.is_dir() and cand.exists():
            sample = cand
            break
    if sample is None:
        return  # empty root; nothing to verify

    try:
        with open(sample, "r", encoding="utf-8") as f:
            json.load(f)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise RuntimeError(
            f"Existing draft is not plaintext JSON ({sample}): {e}\n"
            "This CapCut version may have switched to encrypted drafts; the "
            "template-clone writer cannot safely proceed. Aborting."
        ) from e
