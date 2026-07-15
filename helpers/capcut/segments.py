"""Shared segment/material cloning primitives used by every preset.

The core trick (validated in M1): never synthesize a segment. Clone a real
donor segment, duplicate its per-segment auxiliary materials with fresh UUIDs,
and rewrite only ids + timeranges (+ optional clip/volume). This carries the
~60 opaque segment keys and the aux-material shapes over verbatim.
"""

from __future__ import annotations

import copy

from .ids import new_uuid


def frame_us(fps: float) -> int:
    return int(round(1_000_000.0 / fps))


def snap_us(seconds: float, fps: float) -> int:
    """Seconds -> microseconds snapped to the frame grid."""
    fu = frame_us(fps)
    return int(round(seconds * 1_000_000.0 / fu)) * fu


def build_ref_index(materials: dict, ids: set[str]) -> dict[str, tuple[str, dict]]:
    """Map each id in `ids` to (list_name, deepcopy(material_dict)).

    Deep-copies so callers may clear `materials` lists afterwards without
    invalidating the captured donor aux-material shapes.
    """
    out: dict[str, tuple[str, dict]] = {}
    for list_name, lst in materials.items():
        if not isinstance(lst, list):
            continue
        for m in lst:
            if isinstance(m, dict) and m.get("id") in ids:
                out[m["id"]] = (list_name, copy.deepcopy(m))
    return out


def dup_aux(donor_ref_ids: list[str], ref_index: dict[str, tuple[str, dict]],
            materials: dict) -> list[str]:
    """Duplicate each referenced aux material with a fresh id, append it to its
    list in `materials`, and return the new ref-id list (same order)."""
    new_refs: list[str] = []
    for rid in donor_ref_ids:
        if rid not in ref_index:
            continue  # ref not resolvable in template; drop it
        list_name, mat = ref_index[rid]
        clone = copy.deepcopy(mat)
        clone["id"] = new_uuid()
        materials.setdefault(list_name, []).append(clone)
        new_refs.append(clone["id"])
    return new_refs


def clone_segment(
    donor: dict,
    *,
    material_id: str,
    target_us: tuple[int, int],
    ref_index: dict[str, tuple[str, dict]],
    materials: dict,
    source_us: tuple[int, int] | None = None,
    clip_scale: float | None = None,
    clip_transform: tuple[float, float] | None = None,
    volume: float | None = None,
    render_index: int | None = None,
) -> dict:
    """Clone `donor` into a new segment.

    - `source_us`/`target_us` are (start, duration) in microseconds; source may
      be None (effect segments carry `source_timerange: null`).
    - `clip_scale`/`clip_transform` override the donor's clip (only if the donor
      has a clip block).
    - aux materials are duplicated per segment via `dup_aux`.
    """
    seg = copy.deepcopy(donor)
    seg["id"] = new_uuid()
    seg["material_id"] = material_id
    seg["group_id"] = ""
    seg["source_timerange"] = None if source_us is None else {
        "start": int(source_us[0]), "duration": int(source_us[1])}
    seg["target_timerange"] = {"start": int(target_us[0]), "duration": int(target_us[1])}

    if render_index is not None:
        seg["render_index"] = render_index
    if volume is not None:
        seg["volume"] = float(volume)
        if volume != 0:
            seg["last_nonzero_volume"] = float(volume)

    if seg.get("clip"):
        if clip_scale is not None:
            seg["clip"]["scale"] = {"x": float(clip_scale), "y": float(clip_scale)}
        if clip_transform is not None:
            seg["clip"]["transform"] = {"x": float(clip_transform[0]),
                                         "y": float(clip_transform[1])}

    seg["extra_material_refs"] = dup_aux(
        list(donor.get("extra_material_refs") or []), ref_index, materials)
    return seg
