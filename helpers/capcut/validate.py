"""Post-write validation of a generated CapCut project.

Cheap, high-value integrity checks. Raises ValidationError (non-zero exit at
the CLI) with a human-readable message on any violation.
"""

from __future__ import annotations

import json
from pathlib import Path


class ValidationError(Exception):
    pass


def _load(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _all_material_ids(materials: dict) -> set[str]:
    ids: set[str] = set()
    for lst in materials.values():
        if isinstance(lst, list):
            for m in lst:
                if isinstance(m, dict) and m.get("id"):
                    ids.add(m["id"])
    return ids


def _monotonic_nonoverlapping(segs: list[dict], label: str, errors: list[str]) -> None:
    """Segments (in track order) must not overlap and durations must be positive."""
    prev_end = 0
    for si, seg in enumerate(segs):
        tgt = seg["target_timerange"]
        if tgt["duration"] <= 0:
            errors.append(f"{label} seg {si}: non-positive duration {tgt['duration']}")
        if tgt["start"] < prev_end:
            errors.append(
                f"{label} seg {si}: target start {tgt['start']} < previous end {prev_end} (overlap)")
        prev_end = tgt["start"] + tgt["duration"]


def _gapless(segs: list[dict], label: str, errors: list[str]) -> None:
    """Segments must pack contiguously from 0 (no gaps, no overlaps)."""
    expected = 0
    for si, seg in enumerate(segs):
        tgt = seg["target_timerange"]
        if tgt["duration"] <= 0:
            errors.append(f"{label} seg {si}: non-positive duration {tgt['duration']}")
        if tgt["start"] != expected:
            errors.append(
                f"{label} seg {si}: target start {tgt['start']} != expected {expected} (gap/overlap)")
        expected = tgt["start"] + tgt["duration"]


def validate_draft(draft: dict, errors: list[str], preset: str = "single") -> None:
    materials = draft.get("materials", {})
    mat_ids = _all_material_ids(materials)
    tracks = draft.get("tracks", [])

    # All material references resolve; no track overlaps.
    for ti, track in enumerate(tracks):
        segs = track.get("segments", [])
        for si, seg in enumerate(segs):
            mid = seg.get("material_id")
            if mid and mid not in mat_ids:
                errors.append(f"track {ti} seg {si}: material_id {mid} does not resolve")
            for ref in seg.get("extra_material_refs") or []:
                if ref not in mat_ids:
                    errors.append(f"track {ti} seg {si}: extra_material_ref {ref} does not resolve")
        _monotonic_nonoverlapping(segs, f"track {ti} ({track.get('type')},flag{track.get('flag',0)})", errors)

    # Timeline duration == max target end across all tracks.
    max_end = 0
    for track in tracks:
        for seg in track.get("segments", []):
            tgt = seg.get("target_timerange") or {}
            end = int(tgt.get("start", 0)) + int(tgt.get("duration", 0))
            max_end = max(max_end, end)
    total = draft.get("duration")
    if total != max_end:
        errors.append(f"timeline duration {total} != max target end {max_end}")

    if preset in ("longform-pip", "longform_pip"):
        audio_f0 = [t for t in tracks if t.get("type") == "audio" and t.get("flag", 0) == 0]
        effect_f0 = [t for t in tracks if t.get("type") == "effect" and t.get("flag", 0) == 0]
        if audio_f0:
            # First audio flag0 track is VOICE — must be gapless (continuous speech).
            _gapless(audio_f0[0]["segments"], "voice track", errors)
        if len(audio_f0) > 1:
            # MUSIC track must cover the full timeline contiguously from 0.
            music = audio_f0[1]["segments"]
            _gapless(music, "music track", errors)
            if music:
                last = music[-1]["target_timerange"]
                cov = last["start"] + last["duration"]
                if cov != total:
                    errors.append(f"music coverage {cov} != timeline total {total}")
        if effect_f0:
            eff = effect_f0[0]["segments"]
            if len(eff) != 1:
                errors.append(f"effect track expected 1 segment, got {len(eff)}")
            elif eff[0]["target_timerange"]["duration"] != total:
                errors.append(
                    f"effect duration {eff[0]['target_timerange']['duration']} != timeline total {total}")
    elif preset in ("short-switch", "short_switch"):
        # BASE (video flag0) is a gapless jump-cut sequence; effect spans total.
        base = [t for t in tracks if t.get("type") == "video" and t.get("flag", 0) == 0]
        if base:
            _gapless(base[0]["segments"], "short base track", errors)
        effect_f0 = [t for t in tracks if t.get("type") == "effect" and t.get("flag", 0) == 0]
        if effect_f0:
            eff = effect_f0[0]["segments"]
            if len(eff) == 1 and eff[0]["target_timerange"]["duration"] != total:
                errors.append(
                    f"short effect duration {eff[0]['target_timerange']['duration']} != total {total}")
    elif preset == "raw":
        # Uncut archive: base video flag0 is a single segment from 0. Overlays
        # (full screen) may have their own length; no music/effect requirements.
        base = [t for t in tracks if t.get("type") == "video" and t.get("flag", 0) == 0]
        if base:
            _gapless(base[0]["segments"], "raw base track", errors)
    else:
        # single preset: the base video flag0 track must be gapless.
        base = [t for t in tracks if t.get("type") == "video" and t.get("flag", 0) == 0]
        if base:
            _gapless(base[0]["segments"], "base track", errors)

    # Referenced video media exists on disk (audio may reference CapCut caches).
    for m in materials.get("videos", []):
        path = m.get("path")
        if path and not Path(path).exists():
            errors.append(f"video material path missing on disk: {path}")


def validate_project(project_dir: Path, timeline_id: str, preset: str = "single",
                     timelines: list[tuple[str, str]] | None = None) -> None:
    """Validate a written project. `timelines` is an optional ordered list of
    (timeline_id, preset); when given, every timeline subfolder is validated with
    its own preset (Main + Shorts). The first entry must be the Main timeline."""
    errors: list[str] = []
    all_timelines = timelines if timelines else [(timeline_id, preset)]

    root_info_path = project_dir / "draft_info.json"
    if not root_info_path.exists():
        raise ValidationError(f"missing {root_info_path}")
    root_draft = _load(root_info_path)
    validate_draft(root_draft, errors, preset=preset)

    if root_draft.get("id") != timeline_id:
        errors.append(f"root draft_info id {root_draft.get('id')} != timeline_id {timeline_id}")

    timelines_dir = project_dir / "Timelines"
    if timelines_dir.is_dir():
        # Every declared timeline must have a subfolder with a valid draft_info.
        for tid, tpreset in all_timelines:
            sub_info = timelines_dir / tid / "draft_info.json"
            if not sub_info.exists():
                errors.append(f"missing Timelines/{tid}/draft_info.json")
                continue
            if tid == timeline_id:
                if sub_info.read_text(encoding="utf-8") != root_info_path.read_text(encoding="utf-8"):
                    errors.append("root draft_info.json and Main timeline subfolder differ")
            else:
                validate_draft(_load(sub_info), errors, preset=tpreset)

        proj_json = timelines_dir / "project.json"
        if proj_json.exists():
            pj = _load(proj_json)
            if pj.get("main_timeline_id") != timeline_id:
                errors.append(
                    f"project.json main_timeline_id {pj.get('main_timeline_id')} != {timeline_id}"
                )
            tl_ids = [t.get("id") for t in pj.get("timelines", [])]
            for tid, _ in all_timelines:
                if tid not in tl_ids:
                    errors.append(f"project.json timelines missing {tid} (has {tl_ids})")

    if errors:
        raise ValidationError(
            "post-write validation failed:\n  - " + "\n  - ".join(errors)
        )
