"""Preset builders. M2: longform-pip (the Main-timeline track stack).

Each builder takes the template draft_info (a real, rich timeline used as the
donor) plus the cutplan and probed media, and returns a rebuilt draft_info dict.
"""

from __future__ import annotations

import copy
from pathlib import Path

from .ids import new_uuid
from .probe import MediaInfo
from .segments import build_ref_index, clone_segment, snap_us

# Tail air left after the last spoken word so a timeline doesn't cut on the
# instant speech stops. Applied to the final segment (main + shorts), clamped to
# the footage actually available. Overridable per-cutplan via main.end_air_s.
END_AIR_S = 0.75

# ---- longform-pip reference constants (from the user's real Main timeline) --
INTRO_CAM_SCALE = 1.20            # centered talking-head zoom on the base track
SCREEN_SCALE = 1.18               # full-frame screen recording (v1 constant)
SCREEN_TRANSFORM = (0.0, 0.0)
CENTER_TRANSFORM = (0.0, 0.0)

# PiP corner framing — tuned by the user in CapCut for the 2560x1440 cam format
# (bottom-left, rounded-rectangle crop). Clip + mask are overridden onto the donor.
PIP_SCALE = 0.5319603844026544
PIP_TRANSFORM = (-0.693856339068474, -0.5878048780487806)
PIP_MASK_CONFIG = {
    "width": 0.46344048559415235, "height": 0.7170595364204463,
    "centerX": -0.02851083792258098, "centerY": 0.05120998399300932,
    "rotation": 0.0, "feather": 0.0, "expansion": 0.0,
    "roundCorner": 0.19, "invert": False, "aspectRatio": 1.0,
}

# ---- short-switch reference constants (from the user's real Shorts) ----------
SHORT_CANVAS = {"ratio": "9:16", "width": 1080, "height": 1920}
PUNCH_SCALE = 3.19               # cam (16:9) filling a 9:16 frame
PUNCH_TRANSFORM = (0.25, 0.0)    # slight right bias (talking head off-center)
SHORT_MUSIC_VOLUME = None        # keep donor "Dark Tech Vibe" bed volume
SHUTTER_LEAD_IN_US = 800_000     # no shutter click in the opening frames (< 0.8s)

# Material lists we fully rebuild; everything else in `materials` stays as the
# template supplies it (all empty for this timeline).
_CLEAR_LISTS = [
    "videos", "audios", "video_effects", "common_mask", "drafts",
    "speeds", "placeholder_infos", "canvases", "sound_channel_mappings",
    "material_colors", "vocal_separations", "audio_effects", "audio_fades",
    "beats", "loudnesses", "vocal_beautifys", "material_animations", "hsl",
    "effects", "time_marks",
]


def _classify_tracks(tracks: list[dict]) -> dict:
    """Map the template's Main-timeline tracks to roles by (type, flag, order)."""
    base = effect = voice = music = None
    overlays: list[dict] = []
    audios_f0: list[dict] = []
    for t in tracks:
        typ, flag = t.get("type"), t.get("flag", 0)
        if typ == "video" and flag == 0 and base is None:
            base = t
        elif typ == "effect" and flag == 0 and effect is None:
            effect = t
        elif typ == "video" and flag == 2:
            overlays.append(t)
        elif typ == "audio" and flag == 0:
            audios_f0.append(t)
    if audios_f0:
        voice = audios_f0[0]
    if len(audios_f0) > 1:
        music = audios_f0[1]
    screen = overlays[0] if len(overlays) > 0 else None
    pip = overlays[1] if len(overlays) > 1 else None
    broll = overlays[2] if len(overlays) > 2 else None
    missing = [n for n, v in [("base", base), ("effect", effect), ("screen", screen),
                              ("pip", pip), ("voice", voice), ("music", music)] if v is None]
    if missing:
        raise RuntimeError(f"longform-pip template missing tracks: {missing}")
    return {"base": base, "effect": effect, "screen": screen, "pip": pip,
            "broll": broll, "voice": voice, "music": music}


def _mat_by_id(materials: dict, mid: str) -> tuple[str, dict] | None:
    for ln, lst in materials.items():
        if isinstance(lst, list):
            for m in lst:
                if isinstance(m, dict) and m.get("id") == mid:
                    return ln, m
    return None


def _seg_aux(seg: dict, materials: dict) -> list[tuple[str, dict]]:
    """Resolve a segment's extra_material_refs to (list_name, material_dict)."""
    idx = {m["id"]: (ln, m) for ln, lst in materials.items() if isinstance(lst, list)
           for m in lst if isinstance(m, dict) and m.get("id")}
    return [idx[r] for r in seg.get("extra_material_refs") or [] if r in idx]


def _patch_pip_mask(seg: dict, materials: dict) -> None:
    """Override the PiP segment's rounded-rectangle mask with the tuned config."""
    for ln, m in _seg_aux(seg, materials):
        if ln == "common_mask" and isinstance(m.get("config"), dict):
            m["config"].update(PIP_MASK_CONFIG)


def _fixup_voice_aux(seg: dict, materials: dict) -> None:
    """Emit a voice segment's audio-processing aux in a clean, CapCut-native state.

    Normalization (`loudnesses`) and voice enhancement (`vocal_beautifys`) are
    NOT static settings: CapCut renders per-clip artifacts on disk when the user
    toggles them (a measured `loudness_param`/`file_id`, and an enhanced WAV under
    `Resources/audioAlg/<hash>_<start>_<dur>.wav`). Those come from CapCut's audio
    engine and cannot be fabricated from JSON — an enabled effect with missing
    artifacts is stripped/disabled by CapCut on load. So we ship these OFF in the
    exact shape CapCut writes for an un-processed clip, so the user can enable both
    with one action (select all voice clips -> Normalize loudness + Enhance voice).

    Voice Crisper (`audio_effects`) IS a static filter whose cache path exists, so
    it is kept (just range-bounded to the clip).
    """
    src = seg.get("source_timerange") or {"start": 0, "duration": 0}
    dur = int(src["duration"])
    drop: set[str] = set()
    for ln, m in _seg_aux(seg, materials):
        if ln == "loudnesses":
            m.update({"enable": False, "time_range": None, "file_id": "",
                      "target_loudness": 0.0, "loudness_param": None})
        elif ln == "vocal_beautifys":
            drop.add(m["id"])  # absent until CapCut renders it (matches native off-state)
        elif ln == "audio_effects" and isinstance(m.get("time_range"), dict):
            m["time_range"] = {"start": 0, "duration": dur}
    if drop:
        seg["extra_material_refs"] = [r for r in seg["extra_material_refs"] if r not in drop]
        for lst in materials.values():
            if isinstance(lst, list):
                lst[:] = [m for m in lst if not (isinstance(m, dict) and m.get("id") in drop)]


def build_longform_pip(
    template_draft: dict,
    main: dict,
    *,
    cam_path: str,
    cam_media: MediaInfo,
    screen_path: str,
    screen_media: MediaInfo,
    sync_offset_ms: int,
    timeline_id: str,
) -> tuple[dict, dict]:
    """Return (draft_info dict, stats). Rebuilds all seven tracks per the cutplan."""
    draft = copy.deepcopy(template_draft)
    canvas = main["canvas"]
    fps = float(canvas["fps"])

    draft["id"] = timeline_id
    draft["fps"] = fps
    draft["canvas_config"] = {
        "ratio": canvas.get("ratio", "original"),
        "width": int(canvas["width"]), "height": int(canvas["height"]),
        "background": None,
    }
    # No keyframes/relationships in this timeline; reset to clean shape.
    draft["keyframes"] = {"videos": [], "audios": [], "texts": [], "stickers": [],
                          "filters": [], "adjusts": []}
    draft["relationships"] = []

    tracks = draft["tracks"]
    roles = _classify_tracks(tracks)
    mats = draft["materials"]

    # ---- capture donor segments (before we clear anything) ----
    donor_screen_seg = copy.deepcopy(roles["screen"]["segments"][0])   # full-frame video donor
    donor_pip_seg = copy.deepcopy(roles["pip"]["segments"][0])         # corner PiP + mask
    donor_voice_seg = copy.deepcopy(roles["voice"]["segments"][0])
    donor_music_seg = copy.deepcopy(roles["music"]["segments"][0])
    donor_effect_seg = copy.deepcopy(roles["effect"]["segments"][0])

    # ---- capture donor materials ----
    cam_src = _mat_by_id(mats, donor_pip_seg["material_id"])           # cam video
    screen_src = _mat_by_id(mats, donor_screen_seg["material_id"])     # screen video
    voice_src = _mat_by_id(mats, donor_voice_seg["material_id"])       # video_original_sound
    music_src = _mat_by_id(mats, donor_music_seg["material_id"])       # music
    effect_src = _mat_by_id(mats, donor_effect_seg["material_id"])     # video_effect
    for name, v in [("cam", cam_src), ("screen", screen_src), ("voice", voice_src),
                    ("music", music_src), ("effect", effect_src)]:
        if v is None:
            raise RuntimeError(f"longform-pip: donor material for {name} not found")

    # ---- capture aux-material shapes referenced by any donor ----
    all_ref_ids: set[str] = set()
    for seg in (donor_screen_seg, donor_pip_seg, donor_voice_seg, donor_music_seg):
        all_ref_ids.update(seg.get("extra_material_refs") or [])
    ref_index = build_ref_index(mats, all_ref_ids)

    # ---- reset the material lists we rebuild ----
    for k in _CLEAR_LISTS:
        if k in mats:
            mats[k] = []

    # ---- shared materials ----
    cam_video = copy.deepcopy(cam_src[1])
    cam_video_id = new_uuid()
    cam_video.update({"id": cam_video_id, "path": cam_path,
                      "material_name": Path(cam_path).name,
                      "width": cam_media.width, "height": cam_media.height,
                      "duration": cam_media.duration_us, "has_audio": cam_media.has_audio})

    screen_video = copy.deepcopy(screen_src[1])
    screen_video_id = new_uuid()
    screen_video.update({"id": screen_video_id, "path": screen_path,
                         "material_name": Path(screen_path).name,
                         "width": screen_media.width, "height": screen_media.height,
                         "duration": screen_media.duration_us,
                         "has_audio": screen_media.has_audio})

    voice_audio = copy.deepcopy(voice_src[1])
    voice_audio_id = new_uuid()
    voice_audio.update({"id": voice_audio_id, "path": cam_path,
                        "name": Path(cam_path).stem, "duration": cam_media.duration_us,
                        "video_id": "", "local_material_id": ""})

    effect_mat = copy.deepcopy(effect_src[1])
    effect_mat_id = new_uuid()
    effect_mat["id"] = effect_mat_id

    music_mat = copy.deepcopy(music_src[1])
    music_mat_id = new_uuid()
    music_mat["id"] = music_mat_id
    music_len = int(music_mat.get("duration") or 0)

    mats["videos"] = [cam_video, screen_video]
    mats["audios"] = [voice_audio, music_mat]
    mats["video_effects"] = [effect_mat]

    # ---- output offsets from cumulative (snapped) range durations ----
    ranges = main["ranges"]
    offset_us = int(round(sync_offset_ms * 1000))
    plan = []  # (src_start_us, dur_us, out_off_us, visual)
    run = 0
    for r in ranges:
        s = snap_us(float(r["start"]), fps)
        e = snap_us(float(r["end"]), fps)
        d = e - s
        if d <= 0:
            continue
        plan.append((s, d, run, r.get("visual", "cam")))
        run += d

    # End-air: extend the final range so the video breathes after the last word.
    if plan:
        end_air_us = snap_us(float(main.get("end_air_s", END_AIR_S)), fps)
        s0, d0, off0, vis0 = plan[-1]
        avail = cam_media.duration_us - (s0 + d0)
        if vis0 == "screen":
            scr0 = max(0, s0 + offset_us)
            avail = min(avail, screen_media.duration_us - (scr0 + d0))
        add = max(0, min(end_air_us, avail))
        if add > 0:
            plan[-1] = (s0, d0 + add, off0, vis0)
            run += add
    total = run

    # ---- rebuild track segments ----
    base_segs, screen_segs, pip_segs = [], [], []
    voice_segs = []

    for src_start, dur, off, visual in plan:
        # Voice: every range, gapless, from cam audio.
        vseg = clone_segment(
            donor_voice_seg, material_id=voice_audio_id,
            source_us=(src_start, dur), target_us=(off, dur),
            ref_index=ref_index, materials=mats, volume=1.0)
        _fixup_voice_aux(vseg, mats)  # keep normalization + Voice Crisper valid
        voice_segs.append(vseg)

        if visual == "screen":
            scr_start = max(0, src_start + offset_us)
            screen_segs.append(clone_segment(
                donor_screen_seg, material_id=screen_video_id,
                source_us=(scr_start, dur), target_us=(off, dur),
                ref_index=ref_index, materials=mats,
                clip_scale=SCREEN_SCALE, clip_transform=SCREEN_TRANSFORM, volume=0.0))
            pseg = clone_segment(
                donor_pip_seg, material_id=cam_video_id,
                source_us=(src_start, dur), target_us=(off, dur),
                ref_index=ref_index, materials=mats,
                clip_scale=PIP_SCALE, clip_transform=PIP_TRANSFORM, volume=0.0)
            _patch_pip_mask(pseg, mats)  # tuned bottom-left rounded-rect crop
            pip_segs.append(pseg)
        else:  # cam
            base_segs.append(clone_segment(
                donor_screen_seg, material_id=cam_video_id,
                source_us=(src_start, dur), target_us=(off, dur),
                ref_index=ref_index, materials=mats,
                clip_scale=INTRO_CAM_SCALE, clip_transform=CENTER_TRANSFORM, volume=0.0))

    # Effect: single full-length segment (template-constant material).
    effect_seg = clone_segment(
        donor_effect_seg, material_id=effect_mat_id,
        source_us=None, target_us=(0, total),
        ref_index=ref_index, materials=mats)

    # Music: re-tile the loop to cover the timeline; last tile truncated.
    music_segs = []
    if music_len > 0:
        run2 = 0
        while run2 < total:
            d = min(music_len, total - run2)
            music_segs.append(clone_segment(
                donor_music_seg, material_id=music_mat_id,
                source_us=(0, d), target_us=(run2, d),
                ref_index=ref_index, materials=mats))  # keep donor (quiet) volume
            run2 += d

    # ---- assign segments + fresh track ids ----
    roles["base"]["segments"] = base_segs
    roles["effect"]["segments"] = [effect_seg]
    roles["screen"]["segments"] = screen_segs
    roles["pip"]["segments"] = pip_segs
    if roles["broll"] is not None:
        roles["broll"]["segments"] = []
    roles["voice"]["segments"] = voice_segs
    roles["music"]["segments"] = music_segs
    for role in ("base", "effect", "screen", "pip", "broll", "voice", "music"):
        if roles[role] is not None:
            roles[role]["id"] = new_uuid()

    draft["duration"] = total

    stats = {
        "total_us": total,
        "counts": {
            "base": len(base_segs), "screen": len(screen_segs), "pip": len(pip_segs),
            "voice": len(voice_segs), "music": len(music_segs), "effect": 1,
        },
        "media": {"cam": cam_path, "screen": screen_path},
        "media_exists": {"cam": Path(cam_path).exists(), "screen": Path(screen_path).exists()},
    }
    return draft, stats


# ---------------------------------------------------------------------------
# short-switch preset (9:16 punch-in jump-cut Short)
# ---------------------------------------------------------------------------


def _classify_short_tracks(tracks: list[dict], materials: dict) -> dict:
    """Map the Short donor's tracks to roles by (type, flag) + material type/path."""
    idx = {m["id"]: (ln, m) for ln, lst in materials.items() if isinstance(lst, list)
           for m in lst if isinstance(m, dict) and m.get("id")}

    def first_mat(track):
        seg = track["segments"][0]
        return idx.get(seg.get("material_id"), (None, {}))

    base = effect = leak = voice = music = sfx = broll = None
    for t in tracks:
        typ, flag = t.get("type"), t.get("flag", 0)
        if not t.get("segments"):
            continue
        ln, m = first_mat(t)
        if typ == "video" and flag == 0 and base is None:
            base = t
        elif typ == "effect" and flag == 0 and effect is None:
            effect = t
        elif typ == "video" and flag == 2:
            path = (m.get("path") or "").lower()
            if "light leak" in path or "lightleak" in path or "light_leak" in path:
                leak = leak or t
            else:
                broll = broll or t
        elif typ == "audio" and flag == 0:
            mtype = m.get("type")
            if mtype == "video_original_sound" and voice is None:
                voice = t
            elif mtype == "music" and music is None:
                music = t
            elif mtype == "sound" and sfx is None:
                sfx = t
    missing = [n for n, v in [("base", base), ("effect", effect), ("leak", leak),
                              ("voice", voice), ("music", music), ("sfx", sfx)] if v is None]
    if missing:
        raise RuntimeError(f"short-switch template missing tracks: {missing}")
    return {"base": base, "effect": effect, "leak": leak, "voice": voice,
            "music": music, "sfx": sfx, "broll": broll}


def build_short_switch(
    template_draft: dict,
    short: dict,
    *,
    cam_path: str,
    cam_media: MediaInfo,
    fps: float,
    timeline_id: str,
    canvas: dict | None = None,
    cta_ranges: list[dict] | None = None,
    end_air_s: float = END_AIR_S,
) -> tuple[dict, dict]:
    """Build one 9:16 short: punch-in jump-cut base + light-leak/shutter at each
    switch, gapless voice, Chromatic Quirk, and the second (punchier) music bed.

    `cta_ranges`, if given, are appended (packed) after the short's own ranges so
    every short ends with the shared call-to-action."""
    draft = copy.deepcopy(template_draft)
    cv = dict(SHORT_CANVAS)
    if canvas:
        cv.update({k: canvas[k] for k in ("ratio", "width", "height") if k in canvas})

    draft["id"] = timeline_id
    draft["fps"] = float(fps)
    draft["canvas_config"] = {"ratio": cv["ratio"], "width": int(cv["width"]),
                              "height": int(cv["height"]), "background": None}
    draft["keyframes"] = {"videos": [], "audios": [], "texts": [], "stickers": [],
                          "filters": [], "adjusts": []}
    draft["relationships"] = []

    tracks = draft["tracks"]
    roles = _classify_short_tracks(tracks, draft["materials"])
    mats = draft["materials"]

    donor_base = copy.deepcopy(roles["base"]["segments"][0])
    donor_effect = copy.deepcopy(roles["effect"]["segments"][0])
    donor_leak = copy.deepcopy(roles["leak"]["segments"][0])
    donor_voice = copy.deepcopy(roles["voice"]["segments"][0])
    donor_music = copy.deepcopy(roles["music"]["segments"][0])
    donor_sfx = copy.deepcopy(roles["sfx"]["segments"][0])

    cam_src = _mat_by_id(mats, donor_base["material_id"])
    voice_src = _mat_by_id(mats, donor_voice["material_id"])
    leak_src = _mat_by_id(mats, donor_leak["material_id"])
    music_src = _mat_by_id(mats, donor_music["material_id"])
    sfx_src = _mat_by_id(mats, donor_sfx["material_id"])
    effect_src = _mat_by_id(mats, donor_effect["material_id"])
    for name, v in [("cam", cam_src), ("voice", voice_src), ("leak", leak_src),
                    ("music", music_src), ("sfx", sfx_src), ("effect", effect_src)]:
        if v is None:
            raise RuntimeError(f"short-switch: donor material for {name} not found")

    all_ref_ids: set[str] = set()
    for seg in (donor_base, donor_leak, donor_voice, donor_music, donor_sfx):
        all_ref_ids.update(seg.get("extra_material_refs") or [])
    ref_index = build_ref_index(mats, all_ref_ids)

    for k in _CLEAR_LISTS:
        if k in mats:
            mats[k] = []

    cam_video = copy.deepcopy(cam_src[1])
    cam_video_id = new_uuid()
    cam_video.update({"id": cam_video_id, "path": cam_path,
                      "material_name": Path(cam_path).name,
                      "width": cam_media.width, "height": cam_media.height,
                      "duration": cam_media.duration_us, "has_audio": cam_media.has_audio})

    voice_audio = copy.deepcopy(voice_src[1])
    voice_audio_id = new_uuid()
    voice_audio.update({"id": voice_audio_id, "path": cam_path,
                        "name": Path(cam_path).stem, "duration": cam_media.duration_us,
                        "video_id": "", "local_material_id": ""})

    leak_video = copy.deepcopy(leak_src[1]); leak_video_id = new_uuid()
    leak_video["id"] = leak_video_id           # keep path/dims/dur (asset on disk)
    music_mat = copy.deepcopy(music_src[1]); music_mat_id = new_uuid()
    music_mat["id"] = music_mat_id
    music_len = int(music_mat.get("duration") or 0)
    sfx_mat = copy.deepcopy(sfx_src[1]); sfx_mat_id = new_uuid()
    sfx_mat["id"] = sfx_mat_id
    effect_mat = copy.deepcopy(effect_src[1]); effect_mat_id = new_uuid()
    effect_mat["id"] = effect_mat_id

    mats["videos"] = [cam_video, leak_video]
    mats["audios"] = [voice_audio, music_mat, sfx_mat]
    mats["video_effects"] = [effect_mat]

    # ---- output plan: the short's own ranges, then the shared CTA appended ----
    combined = list(short["ranges"]) + list(cta_ranges or [])
    plan = []
    run = 0
    cta_count = 0
    for k, r in enumerate(combined):
        s = snap_us(float(r["start"]), fps)
        e = snap_us(float(r["end"]), fps)
        d = e - s
        if d <= 0:
            continue
        plan.append((s, d, run))
        run += d
        if k >= len(short["ranges"]):
            cta_count += 1

    # End-air on the final clip so the short breathes at the end.
    if plan:
        end_air_us = snap_us(float(end_air_s), fps)
        s0, d0, off0 = plan[-1]
        add = max(0, min(end_air_us, cam_media.duration_us - (s0 + d0)))
        if add > 0:
            plan[-1] = (s0, d0 + add, off0)
            run += add
    total = run

    base_segs, voice_segs, switches = [], [], []
    for i, (src_start, dur, off) in enumerate(plan):
        base_segs.append(clone_segment(
            donor_base, material_id=cam_video_id,
            source_us=(src_start, dur), target_us=(off, dur),
            ref_index=ref_index, materials=mats,
            clip_scale=PUNCH_SCALE, clip_transform=PUNCH_TRANSFORM, volume=0.0))
        vseg = clone_segment(
            donor_voice, material_id=voice_audio_id,
            source_us=(src_start, dur), target_us=(off, dur),
            ref_index=ref_index, materials=mats, volume=1.0)
        _fixup_voice_aux(vseg, mats)
        voice_segs.append(vseg)
        if i > 0:
            switches.append(off)  # boundary at the start of this clip

    effect_seg = clone_segment(
        donor_effect, material_id=effect_mat_id,
        source_us=None, target_us=(0, total), ref_index=ref_index, materials=mats)

    music_segs = []
    if music_len > 0:
        run2 = 0
        while run2 < total:
            d = min(music_len, total - run2)
            music_segs.append(clone_segment(
                donor_music, material_id=music_mat_id,
                source_us=(0, d), target_us=(run2, d),
                ref_index=ref_index, materials=mats))
            run2 += d

    # Light-leak: a single intro sweep at the very start of the Short.
    leak_src_tr = donor_leak.get("source_timerange") or {"start": 0, "duration": 720000}
    leak_dur = min(int(leak_src_tr["duration"]), total)
    leak_off = int(leak_src_tr["start"])
    leak_segs = []
    if leak_dur > 0:
        leak_segs.append(clone_segment(
            donor_leak, material_id=leak_video_id,
            source_us=(leak_off, leak_dur), target_us=(0, leak_dur),
            ref_index=ref_index, materials=mats))  # keep donor volume/clip

    # Shutter click at switches — never in the opening frames, one per cut.
    sfx_src_tr = donor_sfx.get("source_timerange") or {"start": 0, "duration": 200000}
    sfx_dur = int(sfx_src_tr["duration"])
    sfx_segs = []
    for sw in switches:
        if sw < SHUTTER_LEAD_IN_US:
            continue
        sfx_segs.append(clone_segment(
            donor_sfx, material_id=sfx_mat_id,
            source_us=(int(sfx_src_tr["start"]), sfx_dur), target_us=(sw, sfx_dur),
            ref_index=ref_index, materials=mats))

    roles["base"]["segments"] = base_segs
    roles["effect"]["segments"] = [effect_seg]
    roles["leak"]["segments"] = leak_segs
    roles["voice"]["segments"] = voice_segs
    roles["music"]["segments"] = music_segs
    roles["sfx"]["segments"] = sfx_segs
    if roles["broll"] is not None:
        roles["broll"]["segments"] = []
    for role in ("base", "effect", "leak", "voice", "music", "sfx", "broll"):
        if roles[role] is not None:
            roles[role]["id"] = new_uuid()

    draft["duration"] = total
    stats = {
        "total_us": total,
        "counts": {"base": len(base_segs), "voice": len(voice_segs),
                   "leak": len(leak_segs), "sfx": len(sfx_segs),
                   "music": len(music_segs), "effect": 1, "cta": cta_count},
        "canvas": f"{cv['width']}x{cv['height']}",
    }
    return draft, stats


# ---------------------------------------------------------------------------
# raw preset (uncut archive: full cam + screen + audio, no cutting)
# ---------------------------------------------------------------------------


def build_raw(
    template_draft: dict,
    *,
    canvas: dict,
    cam_path: str,
    cam_media: MediaInfo,
    screen_path: str,
    screen_media: MediaInfo,
    timeline_id: str,
) -> tuple[dict, dict]:
    """A fallback timeline holding the untouched originals: the full cam clip
    (audible) on the base track and the full screen clip (muted) as an overlay,
    dropped in without any cutting. Reuses the longform donor's track stack."""
    draft = copy.deepcopy(template_draft)
    fps = float(canvas["fps"])
    draft["id"] = timeline_id
    draft["fps"] = fps
    draft["canvas_config"] = {
        "ratio": canvas.get("ratio", "original"),
        "width": int(canvas["width"]), "height": int(canvas["height"]),
        "background": None,
    }
    draft["keyframes"] = {"videos": [], "audios": [], "texts": [], "stickers": [],
                          "filters": [], "adjusts": []}
    draft["relationships"] = []

    tracks = draft["tracks"]
    roles = _classify_tracks(tracks)
    mats = draft["materials"]

    donor_fullframe = copy.deepcopy(roles["screen"]["segments"][0])  # full-frame, no mask
    donor_pip_seg = copy.deepcopy(roles["pip"]["segments"][0])
    cam_src = _mat_by_id(mats, donor_pip_seg["material_id"])          # cam video material
    screen_src = _mat_by_id(mats, donor_fullframe["material_id"])     # screen video material
    for name, v in [("cam", cam_src), ("screen", screen_src)]:
        if v is None:
            raise RuntimeError(f"raw: donor material for {name} not found")

    ref_index = build_ref_index(mats, set(donor_fullframe.get("extra_material_refs") or []))
    for k in _CLEAR_LISTS:
        if k in mats:
            mats[k] = []

    cam_video = copy.deepcopy(cam_src[1]); cam_video_id = new_uuid()
    cam_video.update({"id": cam_video_id, "path": cam_path,
                      "material_name": Path(cam_path).name,
                      "width": cam_media.width, "height": cam_media.height,
                      "duration": cam_media.duration_us, "has_audio": cam_media.has_audio})
    screen_video = copy.deepcopy(screen_src[1]); screen_video_id = new_uuid()
    screen_video.update({"id": screen_video_id, "path": screen_path,
                         "material_name": Path(screen_path).name,
                         "width": screen_media.width, "height": screen_media.height,
                         "duration": screen_media.duration_us,
                         "has_audio": screen_media.has_audio})
    mats["videos"] = [cam_video, screen_video]

    cam_dur = int(cam_media.duration_us)
    screen_dur = int(screen_media.duration_us)

    base_seg = clone_segment(
        donor_fullframe, material_id=cam_video_id,
        source_us=(0, cam_dur), target_us=(0, cam_dur),
        ref_index=ref_index, materials=mats,
        clip_scale=1.0, clip_transform=(0.0, 0.0), volume=1.0)   # audible cam
    screen_seg = clone_segment(
        donor_fullframe, material_id=screen_video_id,
        source_us=(0, screen_dur), target_us=(0, screen_dur),
        ref_index=ref_index, materials=mats,
        clip_scale=1.0, clip_transform=(0.0, 0.0), volume=0.0)   # muted screen

    roles["base"]["segments"] = [base_seg]
    roles["screen"]["segments"] = [screen_seg]
    for role in ("effect", "pip", "broll", "voice", "music"):
        if roles[role] is not None:
            roles[role]["segments"] = []
    for role in ("base", "effect", "screen", "pip", "broll", "voice", "music"):
        if roles[role] is not None:
            roles[role]["id"] = new_uuid()

    # Keep only the two populated lanes (cam base + screen overlay).
    draft["tracks"] = [t for t in tracks if t.get("segments")]

    total = max(cam_dur, screen_dur)
    draft["duration"] = total
    stats = {"total_us": total, "counts": {"cam": 1, "screen": 1}}
    return draft, stats
