# CapCut Desktop draft format — internal spec (M1)

Source of truth: the user's **real** project files on CapCut mac, app 8.8/8.9, draft
schema `version: 360000`. Verified against `03_GoogleOmniFlash-testProject` (multi-timeline)
and several single-timeline projects (`0704`, `0708`, ...). Do **not** trust OSS-library
assumptions over these files.

Everything here is what M1 needs. Multi-timeline / shorts / PiP / effects details are
deferred to later milestones.

## Drafts root

Two locations exist on this machine; both contain the same project list:

- Standard:  `~/Movies/CapCut/User Data/Projects/com.lveditor.draft/`
- Container:  `~/Library/Containers/com.lemon.lvoverseas/Data/Movies/CapCut/User Data/Projects/com.lveditor.draft/`

The real projects' `draft_meta_info.json` `draft_fold_path`/`draft_root_path` use the
**standard** `~/Movies/CapCut` variant, while the CapCut music/SFX cache paths reference
the **container** variant. The writer detects the root at runtime (prefers the one that
already contains projects) and can be overridden with `--drafts-root`.

Startup sanity check (Risk 2): read one existing `draft_info.json` in the chosen root and
confirm it parses as JSON. On this version drafts are **plaintext** (a `crypto_key_store.dat`
file exists in each project but does not encrypt the draft). If parsing fails, abort with a
clear message — CapCut may have flipped to encrypted drafts.

## Global project registry

`<root>/root_meta_info.json` holds `all_draft_store[]` — one entry per draft, mirroring each
project folder's `draft_meta_info.json` (has `draft_fold_path`, `draft_id`, `draft_cover`, ...).
Open M1 question resolved via the CapCut gate: whether CapCut auto-discovers a new folder or
requires an `all_draft_store[]` entry. Handled empirically in `capcut_write.py --register`.

## Single-timeline project layout (M1 target)

Minimal, still valid on this version (project `0708`). No `Timelines/` folder:

```
<project>/
  draft_info.json           # the timeline (tracks + materials). ~85 KB even when trivial.
  draft_info.json.bak       # byte-identical backup CapCut keeps; we write it too.
  draft_meta_info.json      # media pool + draft_fold_path/draft_root_path/draft_name/tm_duration
  draft_virtual_store.json  # media-pool child-id listing
  draft_biz_config.json
  draft_agency_config.json
  attachment_editing.json
  attachment_pc_common.json
  common_attachment/        # small json sidecars
  draft_settings            # INI: create/edit timestamps
  key_value.json
  draft_cover.jpg           # thumbnail (kept from template; cosmetic)
  Resources/ adjust_mask/ matting/ qr_upload/ smart_crop/ subdraft/   # (empty dirs)
```

Multi-timeline projects instead add `Timelines/project.json` + one UUID-named subfolder per
timeline, and keep a **byte-identical** copy of the main timeline's `draft_info.json` at the
project root (md5-confirmed). Deferred past M1.

## `draft_info.json` essentials

Top-level keys used by M1: `id`, `name`, `duration`, `fps` (25.0), `canvas_config`
(`{ratio, width, height, background}`), `platform` (copy verbatim from template), `tracks[]`,
`materials{}`, plus ~30 auxiliary keys supplied by the template (copied unchanged).

- **All times are integer microseconds.** `fps = 25` → `frame_us = 1e6/25 = 40000`.
  Snap every boundary to the frame grid: `round(t_us / frame_us) * frame_us`.
- `canvas_config` is set from the cutplan canvas (M1 longform = 1920x1080, `ratio: "original"`).

### Tracks

`tracks[]` order = z-order (first = bottom). Each: `{id, type, segments[], flag, attribute,
name, is_default_name}`. `flag: 0` = main track, `flag: 2` = overlay track. M1 uses one
`type: "video"`, `flag: 0` base track.

### Segments (the cut atoms)

Two fields implement the cut:
- `source_timerange {start, duration}` — slice of the source file (µs).
- `target_timerange {start, duration}` — placement on the timeline (µs).

To remove a gap: emit one segment per kept range; `target_timerange.start` = running
cumulative offset so kept ranges pack back-to-back. Pure metadata; nothing is rendered.

A donor video segment (`0708`) carries ~60 keys plus `clip` (scale/transform/rotation/flip/
alpha), `material_id` (→ `materials.videos`), and `extra_material_refs[]` (→ per-segment aux
materials). M1 clones this donor segment for each kept range and only rewrites: `id`,
`material_id` (shared cam material), the two timeranges, `render_index`, and the aux refs.

### Materials and per-segment aux duplication

`materials{}` is a dict of ~55 typed lists. In real projects the per-segment aux materials are
**duplicated per segment, not shared** (reference project: `speeds`/`placeholder_infos`/
`sound_channel_mappings`/`vocal_separations` = 48 = one per segment; `canvases`/`material_colors`
= 32 = one per video segment). M1 mirrors this.

The `0708` base video segment references (verified) — clone each with a fresh UUID per new
segment and repoint `extra_material_refs`:

| material list          | type               | role                     |
|------------------------|--------------------|--------------------------|
| `videos`               | video              | the media clip (SHARED across both segments — one per source) |
| `speeds`               | speed              | per-segment speed (1.0)  |
| `placeholder_infos`    | placeholder_info   | per-segment              |
| `effects`              | lut                | per-segment              |
| `hsl`                  | hsl                | per-segment              |
| `canvases`             | canvas_color       | per-segment              |
| `material_animations`  | sticker_animation  | per-segment              |
| `sound_channel_mappings` |                  | per-segment              |
| `material_colors`      |                    | per-segment              |
| `loudnesses`           |                    | per-segment              |
| `vocal_beautifys`      | vocal_beautify     | per-segment              |
| `vocal_separations`    | vocal_separation   | per-segment              |

The **video material** is shared: both segments point at one `materials.videos[]` entry whose
`path` is the absolute media path, with real `width`/`height`/`duration`/`material_name`.

## `draft_meta_info.json`

The media pool + project registry entry. M1 rewrites:
- `draft_fold_path` = new project folder (absolute), `draft_root_path` = drafts root.
- `draft_name` = project name.
- `tm_duration` = timeline total (µs).
- `draft_materials` type-0 `value[]`: keep the empty placeholder entry (id `cd484075-...`
  style), rewrite the media entry to the cam file (`file_Path`, `extra_info` = basename,
  `width`, `height`, `duration`, `roughcut_time_range.duration`).
- `draft_id` = fresh UUID; `tm_draft_create`/`tm_draft_modified` = now (µs epoch).

Note: media-pool ids here (lowercase) are **independent** of `draft_info.materials.videos`
ids (uppercase); CapCut links them by path, not id.

## `draft_virtual_store.json`

`draft_virtual_store` type-1 `value[]` lists media-pool `child_id`s (placeholder + media).
Keep in sync with the meta media-pool ids. Cosmetic for M1 but cheap to keep correct.

## Media referencing

M1 uses **reference-in-place** (`--media-mode reference`): `materials.videos[].path` points at
the user's cam file where it already lives. `copy` (into the project folder) is a later flag.

---

# M2: the `longform-pip` preset (Main-timeline track stack)

M2 keeps the whole M1 engine (folder layout, id remapping, per-segment aux duplication,
registry) and adds a multi-track builder driven by a cam+screen cutplan whose ranges are
tagged `visual: "cam"` or `"screen"`. The builder (`presets.build_longform_pip`) never
synthesizes segments — it clones real donor segments from the user's Main timeline
(ingested as `capcut_templates/longform_pip/`) and rewrites ids + timeranges + clip/volume.

## Track model (z-order bottom → top)

| # | track (type, flag) | role | fill | clip override | volume |
|---|--------------------|------|------|---------------|--------|
| 0 | video, flag 0 | BASE cam | `cam` ranges only (has gaps) | scale 1.20, transform (0,0) | 0 |
| 1 | effect, flag 0 | Chromatic Quirk | one segment `{0, total}` | — (source_timerange null) | — |
| 2 | video, flag 2 | SCREEN | `screen` ranges only | scale 1.18, transform (0,0) | 0 |
| 3 | video, flag 2 | PiP cam | `screen` ranges only | **kept** (0.313, corner + `common_mask`) | 0 |
| 4 | video, flag 2 | B-ROLL | empty in v1 | — | — |
| 5 | audio, flag 0 | VOICE (cam) | **every** range, gapless | — | 1.0 |
| 6 | audio, flag 0 | MUSIC | re-tiled to cover `[0, total]` | — | kept (quiet bed) |

Per range: `visual=="cam"` → BASE + VOICE; `visual=="screen"` → SCREEN + PiP + VOICE.
Output offsets are cumulative frame-snapped range durations; VOICE is the master clock
(gapless), BASE/SCREEN/PiP sit at the same offsets only for their matching `visual`, so
they carry gaps (hidden by the full-frame SCREEN overlay during screen phases).

## Audio model (decision)

**All VIDEO segments are muted** (`volume: 0`); the dedicated VOICE audio track (cam's
`video_original_sound`, repointed at the cam path) carries all speech so it stays
continuous across cam/screen phases without doubling. MUSIC is the template's quiet bed,
tiled to length. This deviates from the reference's nonzero video-track volumes but is
correct for the plain-cam model.

### Audio effects: what the writer can and cannot bake

- **Voice Crisper** (`audio_effects`) is a *static filter* — a fixed cache path +
  `audio_adjust_params`. It carries over verbatim and applies with no analysis. Kept, range-bounded to the clip.
- **Normalization** (`loudnesses`) and **Voice enhancer** (`vocal_beautifys`) are **not**
  static: CapCut renders per-clip artifacts on disk when the user enables them — a measured
  `loudness_param`/`file_id` for loudness, and an enhanced WAV at
  `Resources/audioAlg/<contenthash>_<start>_<dur>.wav` for the enhancer (plus vocal/background
  separation stems). These come from CapCut's proprietary audio engine and **cannot be
  fabricated from JSON**; an *enabled* effect with missing artifacts is stripped/disabled by
  CapCut on load. The writer therefore ships voice clips with `loudnesses` present-but-disabled
  and no `vocal_beautifys` (CapCut's native "off" shape), so the user enables both with a single
  action (select all voice clips → Normalize loudness + Enhance voice); CapCut then renders the
  artifacts for every selected clip at once.

## Donor map (from the real Main timeline)

- **cam video** material = the PiP donor's material (BASE + PiP share it); **screen video**
  material = the SCREEN donor's material (retina 5120×3414 — always probed, never assumed).
- **BASE** clones the SCREEN donor *structure* (full-frame video segment, no mask) but points
  at the cam material and overrides clip to centered 1.20. **SCREEN** clones the SCREEN donor,
  clip 1.18. **PiP** clones the PiP donor verbatim (keeps 0.313 corner clip + rounded-rect
  `common_mask`), points at cam.
- **VOICE** clones the `audios:video_original_sound` donor; path/name/duration set to cam,
  `video_id`/`local_material_id` blanked. **MUSIC** clones the `audios:music` donor (kept path
  = CapCut music cache, kept duration for tiling). **EFFECT** clones the `video_effects`
  "Chromatic Quirk" donor (kept `adjust_params`), `source_timerange: null`, `target {0,total}`.
- Shared materials: one cam video, one screen video, one cam audio, one effect, one music.
  Per-segment aux materials (speeds, canvases, sound_channel_mappings, common_mask,
  audio_effects, audio_fades, beats, loudnesses, vocal_beautifys, vocal_separations, …) are
  duplicated per segment with fresh UUIDs by `segments.clone_segment`.

## Sync

`cutplan.sources` has both `cam` and `screen`; `sync_offset_ms` shifts the screen
`source_timerange` relative to cam (`screen_src = cam_src + offset`). Both files are assumed
to share one clock (same recording session).

## `draft_meta_info.json` media pool (M2)

The media pool now holds the empty placeholder **plus one entry per source file** (cam and
screen). `draft_virtual_store.json` lists all pool `child_id`s.

## Validation (`validate.py`, preset-aware)

- refs resolve; no track has overlapping targets; timeline `duration` == max target end.
- longform-pip: VOICE gapless from 0; MUSIC gapless and coverage == total; EFFECT exactly one
  segment of duration == total.
- short-switch: BASE (video flag0) gapless jump-cut sequence; EFFECT == total.
- single: BASE (video flag0) gapless (M1 rule).
- referenced **video** media exists on disk (audio may reference CapCut caches, so it is not
  hard-checked — a missing music cache just triggers CapCut's relink prompt).

---

# M3: the `short-switch` preset + multi-timeline projects

M3 adds Shorts. When a cutplan's `shorts[]` is non-empty, the writer emits a
**multi-timeline** project: the Main `longform-pip` timeline plus one `short-switch`
timeline per Short, all in a single CapCut project.

## Multi-timeline layout

The project is written from the `longform_pip/` folder shell, but `Timelines/` now holds
**one `<timeline-id>/draft_info.json` subfolder per timeline** and `Timelines/project.json`
lists them all with the Main timeline as `main_timeline_id`. The project-root
`draft_info.json` mirrors the Main timeline. `timeline_layout.json` docks every timeline.
The `draft_meta_info.json` media pool is the union of all sources (cam + screen + light-leak).

## short-switch track model (9:16, 1080×1920)

| # | track (type, flag) | role | fill |
|---|--------------------|------|------|
| 0 | video, flag 0 | BASE punch-in | cam jump-cuts, scale 3.19, transform (0.25,0), muted, gapless |
| — | video, flag 2 | b-roll | empty in v1 |
| 1 | effect, flag 0 | Chromatic Quirk | one segment `{0, total}` |
| 2 | video, flag 2 | light-leak | one intro sweep at t=0 (donor volume/clip kept) |
| 3 | audio, flag 0 | VOICE | cam audio, gapless (same aux fixup as Main) |
| 4 | audio, flag 0 | MUSIC (2nd bed) | "Dark Tech Vibe", tiled to total |
| 5 | audio, flag 0 | SFX | shutter click at switches, never in the opening frames |

A "switch" is the boundary between consecutive `ranges`. The light-leak plays **once at
the very start** of each Short. Shutter clicks land on switches but are suppressed within
the opening `SHUTTER_LEAD_IN_US` (0.8s) so nothing pops in the first frames. Punch-in scale
3.19 works for any 16:9 cam (1920×1080 or 2560×1440).
Light-leak / shutter / second-music materials are cloned verbatim from the donor (their
CapCut cache / download paths resolve), so a Short only needs the cam source.

## Cutplan shorts schema

```
"shorts": [
  {"name": "Short 1", "hook": "...", "tail": false,
   "ranges": [{"start": s, "end": e, "quote": "..."}, ...]},
  ...
]
```

Ranges are cam-relative seconds (frame-snapped, packed contiguously into the Short).
Donor tracks are located by type + material type/path, so the ingested Short donor must
keep the stack shape above.

`tail`, `hook`, and range `quote` are **review-only metadata** (surfaced in `review.md`);
the writer ignores them. `tail: true` marks a Short recorded after the main take (detected
from the creator's spoken cue — see the M4 pipeline below).

## Cutplan CTA schema (shared appendix)

```
"cta": {"quote": "...", "ranges": [{"start": s, "end": e}, ...]}
```

Optional top-level block. `build_short_switch` appends the CTA ranges (packed) to the end
of **every** Short (regular and tail) — one shared call-to-action, specified once, never a
Short of its own and never appended to the Main timeline. Ranges are cam-relative seconds.

## End-air

Every built timeline (Main + Shorts) extends its **final** clip by `END_AIR_S` (0.75s,
overridable via `main.end_air_s`) so it does not cut on the instant speech stops. The pad is
clamped to the footage actually available after the last kept word (and, for a screen-final
Main range, to the screen source too), so it never runs past the source.

## Cut-edge air (silence-aware)

The writer widens every cut edge (Main + shorts + CTA) so it lands on a natural gap instead
of clipping a word-tail or breath. Implemented in [cutcheck.py](cutcheck.py):

- `detect_silences` runs `ffmpeg silencedetect` on the cam audio (cached to
  `edit/silences.json`, keyed by cam path + mtime + threshold).
- `refine_ranges` snaps each edge into a nearby real silence, leaving `EDGE_RESIDUAL_S`
  (0.10s) of air; when no silence is within `EDGE_WINDOW_S` (0.30s) it applies a fixed
  `EDGE_PAD_S` (0.15s) pad. Padding never crosses an adjacent transcript word (kept OR
  dropped), so it can't pull in the onset of a dropped false start.
- The final Main range's END is left to `END_AIR_S` (no double-pad). Toggle with cutplan
  `edge_refine: false`; override the fallback with `main.edge_pad_s`. Degrades to fixed pad
  if ffmpeg is unavailable. Applied on a deep copy - the editor's `cutplan.json` is untouched.

## Post-cut read-back (assembled.md)

`review` also emits `edit/assembled.md`: the ACTUAL assembled transcript of every timeline
(Main + each Short incl. the appended CTA), reconstructed from the transcript words inside
each refined range, in output order. `scan_readback_tripwires` adds deterministic hints -
restated openings across a seam, broken-off fragments, and ranges ending mid-thought (unless
the next kept range is a direct source continuation). Paraphrased duplicates are NOT flagged;
the editor must read each timeline top-to-bottom and confirm clean continuous speech before
the project is written.

---

# M4: the agent pipeline (`capcut_pipeline.py`)

M4 wires footage → cutplan → review gate → writer. It is deterministic glue; the
editorial `cutplan.json` is produced by an LLM editor sub-agent (brief in `SKILL.md`).

- `prep <videos_dir>` — ffprobe cam+screen, cached cam transcription + pack, Shorts-cue scan,
  and an "Anweisung" structure-marker scan (`_scan_anweisung_markers`: creators can say the
  trigger word "Anweisung" right before each section - Intro, Hauptteil, Tail Short N,
  Call-to-Action, etc. - as an authoritative, deterministic section boundary; falls back to
  content-based inference when absent) → `edit/analysis.md` + `edit/cutplan.skeleton.json`.
- `review <cutplan.json>` — `cutplan.lint_cutplan` (hard errors abort) + `render_review_md`
  → `edit/review.md` (the human gate).
- `write <cutplan.json> --project-name NAME` — re-lints, then calls `writer.generate(...,
  register=True)` with the same template selection as `capcut_write.py`.

`main.ranges[].visual` (`cam`|`screen`) and the optional `main.estimated_duration_s` are the
M4-facing Main fields. `lint_cutplan` checks: sources exist/probe-able, every range
`0 ≤ start < end ≤ cam_duration`, Main `visual ∈ {cam, screen}`, numeric `sync_offset_ms`;
it warns on Shorts count outside 3–5, per-Short total outside 15–30s, and cam/screen
duration mismatch > 2s. Warnings never block; hard errors abort the write.
