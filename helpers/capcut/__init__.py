"""Thin, template-clone CapCut Desktop project writer.

M1 scope: generate a minimal single-timeline project (one base video track,
N segments of one source with gaps removed) by cloning a real CapCut project
folder and rewriting only what changes. Validated against CapCut mac 8.8/8.9,
draft schema version 360000. See FORMAT.md for the format spec.
"""

from .paths import detect_drafts_root, sanity_check_root
from .ids import new_uuid, IdMap
from .probe import probe_media

__all__ = [
    "detect_drafts_root",
    "sanity_check_root",
    "new_uuid",
    "IdMap",
    "probe_media",
]
