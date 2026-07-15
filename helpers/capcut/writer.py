"""M1 template-clone writer.

Generates a minimal single-timeline CapCut project by deep-copying a real
project folder (the template) and rewriting only: canvas, ids, the base video
track segments, the shared video material, and the project/registry sidecars.

Everything else (the ~30 auxiliary draft_info keys, platform block, per-segment
~60 segment keys, aux-material shapes) is carried over verbatim from the
template so the output is schema-valid on a bleeding-edge CapCut version.
"""

from __future__ import annotations

import copy
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from .cutcheck import EdgeConfig, detect_silences, load_words, refine_ranges
from .ids import new_uuid
from .presets import build_longform_pip, build_raw, build_short_switch
from .probe import MediaInfo, probe_media
from .segments import build_ref_index, clone_segment, snap_us
from .validate import validate_project


# ---- cutplan ---------------------------------------------------------------


@dataclass
class BuildReport:
    project_name: str
    project_dir: Path
    timeline_id: str
    canvas: dict
    total_duration_us: int
    preset: str = "single"
    segments: list[dict] = field(default_factory=list)
    media_path: str = ""
    media_exists: bool = False
    media: dict[str, bool] = field(default_factory=dict)   # path -> exists (multi-media)
    track_counts: dict[str, int] = field(default_factory=dict)
    shorts: list[dict] = field(default_factory=list)       # [{name, total_us, counts}]
    raw: dict | None = None                                # {total_us} for the Raw timeline
    edges: dict | None = None                              # {refined, silence, pad} edge-air summary
    dry_run: bool = False
    warnings: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = []
        lines.append(f"{'DRY-RUN ' if self.dry_run else ''}CapCut project: {self.project_name}  [{self.preset}]")
        lines.append(f"  dir:      {self.project_dir}")
        lines.append(f"  timeline: {self.timeline_id}")
        lines.append(f"  canvas:   {self.canvas['width']}x{self.canvas['height']} @ {self.canvas['fps']}fps ({self.canvas.get('ratio','original')})")
        lines.append(f"  duration: {self.total_duration_us/1e6:.3f}s  ({self.total_duration_us} us)")
        if self.media:
            for path, ok in self.media.items():
                lines.append(f"  media:    {path}  [{'OK' if ok else 'MISSING'}]")
        else:
            lines.append(f"  media:    {self.media_path}  [{'OK' if self.media_exists else 'MISSING'}]")
        if self.track_counts:
            counts = ", ".join(f"{k}={v}" for k, v in self.track_counts.items())
            lines.append(f"  tracks:   {counts}")
        if self.shorts:
            lines.append(f"  shorts:   {len(self.shorts)}")
            for sh in self.shorts:
                c = ", ".join(f"{k}={v}" for k, v in sh["counts"].items())
                lines.append(f"    - {sh['name']}: {sh['total_us']/1e6:.1f}s  ({c})")
        if self.raw:
            lines.append(f"  raw:      Raw timeline ({self.raw['total_us']/1e6:.1f}s uncut cam+screen)")
        if self.edges:
            e = self.edges
            if e.get("applied"):
                lines.append(f"  edges:    silence-aware air on {e['refined']} edges "
                             f"({e['silence']} snapped to silence, {e['pad']} fixed pad)")
            else:
                lines.append(f"  edges:    edge refinement off ({e.get('reason','')})")
        if self.segments:
            lines.append(f"  segments: {len(self.segments)}")
            for i, s in enumerate(self.segments):
                src = s["source_timerange"]
                tgt = s["target_timerange"]
                lines.append(
                    f"    [{i}] src {src['start']/1e6:7.3f}..{(src['start']+src['duration'])/1e6:7.3f}"
                    f"  -> tgt {tgt['start']/1e6:7.3f}..{(tgt['start']+tgt['duration'])/1e6:7.3f}"
                    f"  ({tgt['duration']/1e6:.3f}s)"
                )
        for w in self.warnings:
            lines.append(f"  WARNING: {w}")
        return "\n".join(lines)


MIN_SEGMENT_S = 0.5


def _validate_cutplan_ranges(ranges: list[dict], fps: float, warnings: list[str]) -> None:
    if not ranges:
        raise ValueError("cutplan main.ranges is empty")
    prev_end = None
    for i, r in enumerate(ranges):
        start = float(r["start"])
        end = float(r["end"])
        if end <= start:
            raise ValueError(f"range {i}: end ({end}) <= start ({start})")
        if (end - start) < MIN_SEGMENT_S:
            warnings.append(
                f"range {i} is {end-start:.3f}s (< {MIN_SEGMENT_S}s minimum); may render as confetti"
            )
        if prev_end is not None and start < prev_end - 1e-6:
            # Source ranges may legitimately be out of order (we pack by output
            # order), so this is a note, not an error. Overlap in *output* is
            # impossible because we always pack contiguously.
            pass
        prev_end = end


# ---- track / material helpers ----------------------------------------------


def _find_base_video_track(draft: dict) -> dict:
    for t in draft.get("tracks", []):
        if t.get("type") == "video" and t.get("flag", 0) == 0:
            return t
    raise RuntimeError("template has no base video track (type=video, flag=0)")


# ---- the build -------------------------------------------------------------


def build_draft(
    template_draft: dict,
    ranges: list[dict],
    canvas: dict,
    media: MediaInfo,
    media_path: str,
    timeline_id: str,
) -> tuple[dict, list[dict]]:
    """Return (new draft_info dict, list of new segment dicts).

    Rebuilds the base video track to one segment per kept range, packing them
    contiguously on the timeline. The video material is shared; all per-segment
    auxiliary materials are duplicated with fresh ids (matching CapCut).
    """
    draft = copy.deepcopy(template_draft)
    fps = float(canvas["fps"])

    # Canvas + timeline identity
    draft["id"] = timeline_id
    draft["fps"] = float(fps)
    draft["canvas_config"] = {
        "ratio": canvas.get("ratio", "original"),
        "width": int(canvas["width"]),
        "height": int(canvas["height"]),
        "background": None,
    }

    base_track = _find_base_video_track(draft)
    donor_seg = copy.deepcopy(base_track["segments"][0])
    materials = draft["materials"]

    # Donor's referenced materials: the shared video material + per-segment aux.
    donor_video_id = donor_seg["material_id"]
    donor_ref_ids = list(donor_seg.get("extra_material_refs") or [])
    ref_index = build_ref_index(materials, set(donor_ref_ids))

    donor_video = next((m for m in materials.get("videos", []) if m.get("id") == donor_video_id), None)
    if donor_video is None:
        raise RuntimeError("donor segment's video material not found in materials.videos")

    # Build the single shared video material pointing at the new source.
    new_video = copy.deepcopy(donor_video)
    new_video_id = new_uuid()
    new_video.update({
        "id": new_video_id, "path": media_path, "material_name": Path(media_path).name,
        "width": media.width, "height": media.height,
        "duration": media.duration_us, "has_audio": media.has_audio,
    })
    materials["videos"] = [new_video]

    # Reset the aux-material lists we repopulate per segment.
    for ln in {ln for (ln, _m) in ref_index.values()}:
        materials[ln] = []

    # Rebuild segments: one per kept range, packed contiguously.
    new_segments: list[dict] = []
    running = 0
    for r in ranges:
        s_start = snap_us(float(r["start"]), fps)
        dur = snap_us(float(r["end"]), fps) - s_start
        if dur <= 0:
            continue
        seg = clone_segment(
            donor_seg, material_id=new_video_id,
            source_us=(s_start, dur), target_us=(running, dur),
            ref_index=ref_index, materials=materials)
        new_segments.append(seg)
        running += dur

    base_track["segments"] = new_segments
    base_track["id"] = new_uuid()
    draft["duration"] = running
    return draft, new_segments


# ---- folder writing --------------------------------------------------------


def _now_us() -> int:
    return int(time.time() * 1_000_000)


def _cleanup_caches(project_dir: Path) -> None:
    """Remove CapCut caches/backups that reference the old (template) state.

    CapCut regenerates these from draft_info.json on load. Leaving stale copies
    can cause it to show old ids/media.
    """
    patterns = ["*.tmp", "*.bak", "draft.extra"]
    for pat in patterns:
        for p in project_dir.rglob(pat):
            p.unlink(missing_ok=True)
    # per-timeline patch caches
    for patch in project_dir.rglob("attachment/patch"):
        if patch.is_dir():
            shutil.rmtree(patch, ignore_errors=True)
    # per-media analysis caches keyed to the template's OLD media (audio/loudness/
    # matting/etc.). CapCut recomputes these for the new source on demand; leaving
    # stale ones only wastes space and risks confusion. Empty the dirs, keep them.
    for cache_dir in ["loudness", "matting", "adjust_mask", "smart_crop",
                      "Resources/audioAlg", "Resources/videoAlg", "Resources/digitalHuman"]:
        d = project_dir / cache_dir
        if d.is_dir():
            for child in d.iterdir():
                if child.is_file():
                    child.unlink(missing_ok=True)
                elif child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _rewrite_meta_info(
    meta: dict,
    project_dir: Path,
    drafts_root: Path,
    project_name: str,
    media_entries: list[tuple[str, MediaInfo]],
    total_us: int,
) -> dict:
    """Rewrite draft_meta_info: project paths/name/duration + the media pool
    (one empty placeholder + one entry per source media file)."""
    meta = copy.deepcopy(meta)
    meta["draft_fold_path"] = str(project_dir)
    meta["draft_root_path"] = str(drafts_root)
    meta["draft_name"] = project_name
    meta["draft_id"] = new_uuid()
    meta["tm_duration"] = total_us
    now = _now_us()
    meta["tm_draft_create"] = now
    meta["tm_draft_modified"] = now
    meta["draft_cover"] = "draft_cover.jpg"

    placeholder_id = "cd484075-d92a-4bc9-b45c-d093d2f9e71b"
    placeholder = {
        "ai_group_type": "", "create_time": int(time.time()), "duration": 33333,
        "enter_from": 0, "extra_info": "", "file_Path": "", "height": 0,
        "id": placeholder_id, "import_time": int(time.time()),
        "import_time_ms": _now_us(), "item_source": 1, "md5": "", "metetype": "none",
        "roughcut_time_range": {"duration": 33333, "start": 0},
        "sub_time_range": {"duration": -1, "start": -1}, "type": 0, "width": 0,
    }
    pool = [placeholder]
    pool_ids = [placeholder_id]
    for media_path, media in media_entries:
        media_id = str(__import__("uuid").uuid4())  # lowercase, media-pool style
        pool.append({
            "ai_group_type": "", "create_time": int(time.time()),
            "duration": media.duration_us, "enter_from": 0,
            "extra_info": Path(media_path).name, "file_Path": media_path,
            "height": media.height, "id": media_id, "import_time": int(time.time()),
            "import_time_ms": _now_us(), "item_source": 1, "md5": "", "metetype": "video",
            "roughcut_time_range": {"duration": media.duration_us, "start": 0},
            "sub_time_range": {"duration": -1, "start": -1}, "type": 0,
            "width": media.width,
        })
        pool_ids.append(media_id)

    new_materials = []
    for block in meta.get("draft_materials", []):
        if block.get("type") == 0:
            new_materials.append({"type": 0, "value": pool})
        else:
            new_materials.append({"type": block.get("type"), "value": []})
    meta["draft_materials"] = new_materials
    meta["_media_pool_ids"] = pool_ids  # internal, stripped before write
    return meta


def write_project(
    template_dir: Path,
    project_dir: Path,
    timelines: list[dict],
    meta: dict,
    project_name: str,
    main_timeline_id: str,
) -> None:
    """Materialize the project folder: clone template, then overwrite the
    files that carry state. `timelines` is an ordered list of
    {"id", "name", "draft"}; the first is the Main timeline (mirrored to the
    project-root draft_info.json). Extra timelines (Shorts) get their own
    Timelines/<id>/ subfolder. project.json lists them all."""
    if project_dir.exists():
        shutil.rmtree(project_dir)
    shutil.copytree(template_dir, project_dir)
    _cleanup_caches(project_dir)

    main = timelines[0]
    main_draft = main["draft"]

    timelines_dir = project_dir / "Timelines"
    timelines_dir.mkdir(exist_ok=True)
    # Drop the template's donor subfolder(s); we write fresh ones per timeline.
    for d in timelines_dir.iterdir():
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)

    # Root mirror = Main timeline.
    _write_json(project_dir / "draft_info.json", main_draft)
    _write_json(project_dir / "draft_info.json.bak", main_draft)

    # One subfolder per timeline.
    for tl in timelines:
        sub = timelines_dir / tl["id"]
        sub.mkdir(parents=True, exist_ok=True)
        _write_json(sub / "draft_info.json", tl["draft"])
        _write_json(sub / "draft_info.json.bak", tl["draft"])

    # Timelines/project.json — the multi-timeline manifest.
    proj_json_path = timelines_dir / "project.json"
    now = _now_us()
    pj = {}
    if proj_json_path.exists():
        with open(proj_json_path, encoding="utf-8") as f:
            pj = json.load(f)
    pj["id"] = new_uuid()
    pj["main_timeline_id"] = main_timeline_id
    pj["create_time"] = now
    pj["update_time"] = now
    pj["timelines"] = [{
        "create_time": now, "id": tl["id"], "is_marked_delete": False,
        "name": tl["name"], "update_time": now,
    } for tl in timelines]
    _write_json(proj_json_path, pj)

    # timeline_layout.json — dock layout references every timeline id + name.
    layout_path = project_dir / "timeline_layout.json"
    _write_json(layout_path, {
        "dockItems": [{
            "dockIndex": 0, "ratio": 1,
            "timelineIds": [tl["id"] for tl in timelines],
            "timelineNames": [tl["name"] for tl in timelines],
        }],
        "layoutOrientation": 1,
    })

    # draft_meta_info.json (strip internal helper key first).
    meta = copy.deepcopy(meta)
    meta.pop("_media_pool_ids", None)
    _write_json(project_dir / "draft_meta_info.json", meta)

    # draft_virtual_store.json — media-pool child listing.
    vs_path = project_dir / "draft_virtual_store.json"
    if vs_path.exists():
        pool_ids = []
        for block in meta.get("draft_materials", []):
            if block.get("type") == 0:
                pool_ids = [e["id"] for e in block.get("value", [])]
        vs = {
            "draft_materials": [],
            "draft_virtual_store": [
                {"type": 0, "value": [{
                    "creation_time": 0, "display_name": "", "filter_type": 0,
                    "id": "", "import_time": 0, "import_time_us": 0,
                    "sort_sub_type": 0, "sort_type": 0, "subdraft_filter_type": 0,
                }]},
                {"type": 1, "value": [{"child_id": cid, "parent_id": ""} for cid in pool_ids]},
                {"type": 2, "value": []},
            ],
        }
        _write_json(vs_path, vs)

    # draft_settings — INI timestamps.
    settings_path = project_dir / "draft_settings"
    if settings_path.exists():
        now_s = int(time.time())
        settings_path.write_text(
            f"[General]\ndraft_create_time={now_s}\n"
            f"draft_last_edit_time={now_s}\nreal_edit_keys=0\nreal_edit_seconds=0\n"
        )

    # key_value.json — drop any prior agent-session bookkeeping.
    kv_path = project_dir / "key_value.json"
    if kv_path.exists():
        _write_json(kv_path, {})


def register_in_root(drafts_root: Path, project_dir: Path, meta: dict) -> bool:
    """Append/refresh this project's entry in root_meta_info.json's all_draft_store.

    Returns True if the registry was updated, False if there is no registry
    file (CapCut auto-discovers) or on any non-fatal issue.
    """
    reg_path = drafts_root / "root_meta_info.json"
    if not reg_path.exists():
        return False
    try:
        with open(reg_path, encoding="utf-8") as f:
            reg = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False

    store = reg.get("all_draft_store")
    if not isinstance(store, list):
        return False

    fold = str(project_dir)
    entry = {
        "cloud_draft_cover": False, "cloud_draft_sync": False,
        "draft_cloud_last_action_download": False, "draft_cloud_purchase_info": "",
        "draft_cloud_template_id": "", "draft_cloud_tutorial_info": "",
        "draft_cloud_videocut_purchase_info": "",
        "draft_cover": str(project_dir / "draft_cover.jpg"),
        "draft_fold_path": fold, "draft_id": meta.get("draft_id", new_uuid()),
        "draft_is_ai_shorts": False, "draft_is_cloud_temp_draft": False,
        "draft_json_file": str(project_dir / "draft_info.json"),
        "draft_name": meta.get("draft_name", project_dir.name),
        "tm_draft_create": meta.get("tm_draft_create", _now_us()),
        "tm_draft_modified": meta.get("tm_draft_modified", _now_us()),
        "tm_duration": meta.get("tm_duration", 0),
    }
    store[:] = [e for e in store if e.get("draft_fold_path") != fold]
    store.insert(0, entry)
    _write_json(reg_path, reg)
    return True


# ---- top-level orchestration -----------------------------------------------


def _short_leak_path(short_donor: dict) -> str | None:
    """Find the light-leak video path in the short donor's materials."""
    for m in short_donor.get("materials", {}).get("videos", []):
        p = (m.get("path") or "")
        if "light leak" in p.lower() or "lightleak" in p.lower() or "light_leak" in p.lower():
            return p
    return None


def _refine_cutplan_edges(
    cutplan: dict, cam_path: str, cam_media: MediaInfo, edit_dir: Path | None,
) -> dict:
    """Add silence-aware air to every cut edge (Main + shorts + CTA), in place on
    `cutplan`. Returns a summary for the BuildReport. Off when cutplan.edge_refine
    is False; degrades to a fixed pad when ffmpeg/silence data is unavailable."""
    main = cutplan.get("main") or {}
    if cutplan.get("edge_refine") is False:
        return {"applied": False, "reason": "edge_refine: false"}

    cfg = EdgeConfig()
    if main.get("edge_pad_s") is not None:
        cfg.pad_s = float(main["edge_pad_s"])

    cam_dur_s = cam_media.duration_us / 1e6
    silences = detect_silences(cam_path, edit_dir, cfg)
    words = []
    if edit_dir is not None:
        tj = edit_dir / "transcripts" / f"{Path(cam_path).stem}.json"
        words = load_words(tj)

    total_refined = sil_snapped = padded = 0

    def _apply(ranges: list[dict], *, pad_last_end: bool) -> list[dict]:
        nonlocal total_refined, sil_snapped, padded
        refined, notes = refine_ranges(
            ranges, silences, cam_dur_s, cfg, words=words, pad_last_end=pad_last_end)
        for nt in notes:
            for mode in (nt["start_mode"], nt["end_mode"]):
                if mode == "silence":
                    sil_snapped += 1; total_refined += 1
                elif mode == "pad":
                    padded += 1; total_refined += 1
        return refined

    if main.get("ranges"):
        main["ranges"] = _apply(main["ranges"], pad_last_end=False)
    for sh in cutplan.get("shorts") or []:
        if sh.get("ranges"):
            sh["ranges"] = _apply(sh["ranges"], pad_last_end=True)
    cta = cutplan.get("cta") or {}
    if cta.get("ranges"):
        cta["ranges"] = _apply(cta["ranges"], pad_last_end=False)

    return {"applied": True, "refined": total_refined,
            "silence": sil_snapped, "pad": padded,
            "silence_detected": silences is not None}


def generate(
    cutplan: dict,
    template_dir: Path,
    drafts_root: Path,
    project_name: str,
    media_mode: str = "reference",
    dry_run: bool = False,
    register: bool = False,
    short_template_dir: Path | None = None,
    edit_dir: Path | None = None,
) -> BuildReport:
    cutplan = copy.deepcopy(cutplan)  # never mutate the caller's cutplan (we refine edges)
    main = cutplan["main"]
    canvas = dict(main["canvas"])
    canvas.setdefault("ratio", "original")
    fps = float(canvas["fps"])
    ranges = main["ranges"]
    preset = (main.get("preset") or "single").lower()

    warnings: list[str] = []
    _validate_cutplan_ranges(ranges, fps, warnings)

    if media_mode == "copy":
        warnings.append("--media-mode copy is not implemented yet; referencing media in place")

    sources = cutplan.get("sources", {})
    cam_path = sources.get("cam")
    if not cam_path:
        raise ValueError("cutplan.sources.cam is required")
    cam_path = str(Path(cam_path).expanduser())
    cam_media = probe_media(cam_path)

    # Silence-aware cut-edge air (Main + shorts + CTA), before any timeline builds.
    edge_report = _refine_cutplan_edges(cutplan, cam_path, cam_media, edit_dir)
    ranges = main["ranges"]

    project_dir = drafts_root / project_name

    # Load template draft_info + meta.
    with open(template_dir / "draft_info.json", encoding="utf-8") as f:
        template_draft = json.load(f)
    with open(template_dir / "draft_meta_info.json", encoding="utf-8") as f:
        template_meta = json.load(f)

    timeline_id = new_uuid()
    # (main timeline, [(id, name, preset, draft), ...]) accumulates all timelines.
    timelines: list[dict] = []
    short_infos: list[dict] = []

    if preset in ("longform-pip", "longform_pip"):
        screen_path = sources.get("screen")
        if not screen_path:
            raise ValueError("cutplan.sources.screen is required for longform-pip")
        screen_path = str(Path(screen_path).expanduser())
        screen_media = probe_media(screen_path)
        if abs(cam_media.duration_us - screen_media.duration_us) > 2_000_000:
            warnings.append(
                f"cam and screen durations differ by "
                f"{abs(cam_media.duration_us - screen_media.duration_us)/1e6:.1f}s "
                "(>2s); check they are the same session / --offset-ms"
            )
        draft, stats = build_longform_pip(
            template_draft, main, cam_path=cam_path, cam_media=cam_media,
            screen_path=screen_path, screen_media=screen_media,
            sync_offset_ms=int(cutplan.get("sync_offset_ms", 0)), timeline_id=timeline_id,
        )
        media_entries = [(cam_path, cam_media), (screen_path, screen_media)]
        total_us = draft["duration"]
        timelines.append({"id": timeline_id, "name": main.get("name", "Main Video"),
                          "preset": "longform-pip", "draft": draft})

        # ---- Shorts (M3): each becomes its own 9:16 short-switch timeline ----
        shorts = cutplan.get("shorts") or []
        if shorts:
            if short_template_dir is None or not (Path(short_template_dir) / "draft_info.json").exists():
                raise ValueError(
                    "cutplan has shorts but the short-switch template is missing; "
                    "expected capcut_templates/short_switch/draft_info.json"
                )
            with open(Path(short_template_dir) / "draft_info.json", encoding="utf-8") as f:
                short_donor = json.load(f)
            leak_path = _short_leak_path(short_donor)
            if leak_path and Path(leak_path).exists():
                media_entries.append((leak_path, probe_media(leak_path)))
            elif leak_path:
                warnings.append(f"light-leak asset missing on disk: {leak_path}")
            # Shared call-to-action appended to the end of every short.
            cta = cutplan.get("cta") or {}
            cta_ranges = cta.get("ranges") or None
            for i, sh in enumerate(shorts):
                sh_id = new_uuid()
                sh_name = sh.get("name") or f"Short {i + 1}"
                sh_draft, sh_stats = build_short_switch(
                    short_donor, sh, cam_path=cam_path, cam_media=cam_media,
                    fps=fps, timeline_id=sh_id, canvas=sh.get("canvas"),
                    cta_ranges=cta_ranges,
                )
                timelines.append({"id": sh_id, "name": sh_name,
                                  "preset": "short-switch", "draft": sh_draft})
                short_infos.append({"name": sh_name, "total_us": sh_stats["total_us"],
                                    "counts": sh_stats["counts"]})

        # ---- Raw archive timeline (uncut originals; default-on) ----
        raw_info = None
        if cutplan.get("raw", True):
            raw_id = new_uuid()
            raw_draft, raw_stats = build_raw(
                template_draft, canvas=canvas,
                cam_path=cam_path, cam_media=cam_media,
                screen_path=screen_path, screen_media=screen_media,
                timeline_id=raw_id,
            )
            timelines.append({"id": raw_id, "name": "Raw",
                              "preset": "raw", "draft": raw_draft})
            raw_info = {"total_us": raw_stats["total_us"]}

        report = BuildReport(
            project_name=project_name, project_dir=project_dir, timeline_id=timeline_id,
            canvas=canvas, total_duration_us=total_us, preset="longform-pip",
            media={p: Path(p).exists() for p, _ in media_entries},
            track_counts=stats["counts"], shorts=short_infos, raw=raw_info,
            edges=edge_report, dry_run=dry_run, warnings=warnings,
        )
    else:
        draft, segments = build_draft(
            template_draft, ranges, canvas, cam_media, cam_path, timeline_id
        )
        media_entries = [(cam_path, cam_media)]
        total_us = draft["duration"]
        timelines.append({"id": timeline_id, "name": main.get("name", project_name),
                          "preset": "single", "draft": draft})
        report = BuildReport(
            project_name=project_name, project_dir=project_dir, timeline_id=timeline_id,
            canvas=canvas, total_duration_us=total_us, preset="single", segments=segments,
            media_path=cam_path, media_exists=Path(cam_path).exists(),
            edges=edge_report, dry_run=dry_run, warnings=warnings,
        )

    meta = _rewrite_meta_info(
        template_meta, project_dir, drafts_root, project_name, media_entries, total_us,
    )

    if dry_run:
        return report

    write_project(template_dir, project_dir, timelines, meta, project_name, timeline_id)

    # Post-write validation (per timeline, preset-aware).
    validate_project(project_dir, timeline_id, preset=preset,
                     timelines=[(tl["id"], tl["preset"]) for tl in timelines])

    if register:
        registered = register_in_root(drafts_root, project_dir, meta)
        if not registered:
            report.warnings.append(
                "root_meta_info.json not updated (missing/unwritable); CapCut may need to auto-discover the folder"
            )

    return report
