"""CapCut project pipeline: prep -> (editor sub-agent) -> review gate -> write.

This is the M4 orchestrator that turns a synced cam+screen recording into a
native, editable CapCut project (Main `longform-pip` timeline + 3-5
`short-switch` Shorts), gated by a human-readable `review.md`.

The pipeline is deliberately deterministic: it does the mechanical stages
(ffprobe, cached transcription, packing, linting, review render, writer
invocation) and NEVER makes editorial decisions. The editor sub-agent — a
separate LLM step driven by the brief in SKILL.md — owns every taste call and
produces the `cutplan.json` this pipeline consumes.

Subcommands:
    prep <videos_dir> [--cam F] [--screen F]
        ffprobe both sources, transcribe the cam (cached), pack transcripts,
        scan for Shorts-announcement cues, and emit:
          <videos_dir>/edit/analysis.md            (editor brief + facts)
          <videos_dir>/edit/cutplan.skeleton.json  (sources + canvas, empty cut)

    review <cutplan.json>
        Lint the cutplan (hard errors abort) and render the review gate to
        <edit>/review.md.

    write <cutplan.json> --project-name NAME
        Invoke the proven M2/M3 writer, registering the project with CapCut.

All outputs live under `<videos_dir>/edit/` (Hard Rule 12). Cached transcripts
are never regenerated (Hard Rule 9).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from capcut.cutplan import (  # noqa: E402
    lint_cutplan,
    load_cutplan,
    render_review_md,
)
from capcut.paths import detect_drafts_root, sanity_check_root  # noqa: E402
from capcut.probe import MediaInfo, probe_media  # noqa: E402
from capcut.validate import ValidationError  # noqa: E402
from capcut.writer import generate  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TEMPLATES = REPO_ROOT / "capcut_templates"

# Cues a creator says on camera when switching to recording Shorts / labelling a
# tail short or the call-to-action. Case-insensitive. These only *hint* at
# candidate lines; the editor makes the semantic call.
SHORTS_CUE_WORDS = [
    "short", "shorts", "reel", "reels", "vertical", "clip", "clips",
    "kurz", "kurzvideo", "hochkant",
    "tail short", "call to action", "call-to-action", "cta",
    "handlungsaufforderung", "aufruf",
]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _resolve_sources(
    videos_dir: Path, cam: Path | None, screen: Path | None
) -> tuple[Path, Path]:
    """Resolve cam + screen paths, auto-detecting from the directory if needed.

    Heuristic: any .mp4/.mov whose stem contains "screen" is the screen capture;
    the other is the cam. Ambiguity aborts with a clear message.
    """
    if cam and screen:
        return cam.resolve(), screen.resolve()

    vids = sorted(
        p for p in videos_dir.iterdir()
        if p.is_file() and p.suffix.lower() in (".mp4", ".mov", ".mkv")
    )
    if cam:
        cam = cam.resolve()
        others = [p for p in vids if p.resolve() != cam]
        screens = [p for p in others if "screen" in p.stem.lower()] or others
        if len(screens) != 1:
            sys.exit(f"could not auto-detect the screen source; pass --screen. Candidates: {screens}")
        return cam, screens[0].resolve()

    screens = [p for p in vids if "screen" in p.stem.lower()]
    cams = [p for p in vids if "screen" not in p.stem.lower()]
    if screen:
        screen = screen.resolve()
        cams = [p for p in vids if p.resolve() != screen]
    if len(cams) != 1 or (screen is None and len(screens) != 1):
        sys.exit(
            "could not auto-detect cam/screen sources; pass --cam and --screen explicitly.\n"
            f"  cam candidates:    {[p.name for p in cams]}\n"
            f"  screen candidates: {[p.name for p in screens]}"
        )
    return cams[0].resolve(), (screen or screens[0].resolve())


def _probe_safe(path: str | None) -> MediaInfo | None:
    if not path:
        return None
    try:
        return probe_media(Path(path).expanduser())
    except (FileNotFoundError, RuntimeError):
        return None


def _scan_shorts_cues(packed_md: Path) -> list[str]:
    """Return transcript lines that mention a Shorts-announcement cue word."""
    if not packed_md.exists():
        return []
    pattern = re.compile(r"\b(" + "|".join(re.escape(w) for w in SHORTS_CUE_WORDS) + r")\b", re.I)
    hits: list[str] = []
    for line in packed_md.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("[") and pattern.search(s):
            hits.append(s)
    return hits


_ANWEISUNG_RE = re.compile(r"\bAnweisung\b\s*[,:]?\s*", re.I)


def _scan_anweisung_markers(packed_md: Path) -> list[tuple[str, str]]:
    """Return (line, label) for every packed-transcript phrase containing the
    spoken "Anweisung" structure cue (e.g. "Anweisung, jetzt kommt das Intro",
    "Anweisung: Call to Action fuer Shorts"). When present, these are the
    AUTHORITATIVE section-boundary markers - far more reliable than inferring
    structure from content. `label` is the phrase with the "Anweisung" token
    (and its punctuation) stripped, i.e. just the section name the creator said.
    """
    if not packed_md.exists():
        return []
    hits: list[tuple[str, str]] = []
    for line in packed_md.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("[") and _ANWEISUNG_RE.search(s):
            label = _ANWEISUNG_RE.sub("", _phrase_text(s), count=1).strip()
            hits.append((s, label))
    return hits


def _phrase_text(line: str) -> str:
    """Strip the '[start-end] Sx ' prefix from a packed transcript line."""
    body = re.sub(r"^\[[0-9.]+-[0-9.]+\]", "", line).strip()
    body = re.sub(r"^S\S+\s+", "", body).strip()  # speaker tag
    return body


def _opening_key(text: str, n: int = 3) -> str:
    """Lowercased first n content words, for detecting repeated openings."""
    words = re.findall(r"[\wäöüßÄÖÜ']+", text.lower())
    return " ".join(words[:n])


def _scan_outtakes(packed_md: Path) -> tuple[list[str], list[list[str]]]:
    """Deterministic outtake candidates from the packed transcript.

    Returns (trailing_lines, repeated_opening_groups):
      - trailing_lines: phrases whose text trails off in '...' (broken-off takes).
      - repeated_opening_groups: runs of >=2 consecutive phrases that share the
        same first ~3 words (restart clusters; usually keep only the last).
    Hints only - the editor decides.
    """
    if not packed_md.exists():
        return [], []
    lines = [ln.strip() for ln in packed_md.read_text(encoding="utf-8").splitlines()
             if ln.strip().startswith("[")]
    trailing: list[str] = []
    groups: list[list[str]] = []
    run: list[str] = []
    run_key: str | None = None
    for ln in lines:
        text = _phrase_text(ln)
        if text.endswith("...") or text.endswith("--"):
            trailing.append(ln)
        key = _opening_key(text)
        if key and key == run_key:
            run.append(ln)
        else:
            if len(run) >= 2:
                groups.append(run)
            run = [ln] if key else []
            run_key = key
    if len(run) >= 2:
        groups.append(run)
    return trailing, groups


def _append_project_note(edit_dir: Path, note: str) -> None:
    project_md = edit_dir / "project.md"
    with project_md.open("a", encoding="utf-8") as f:
        f.write(note if note.endswith("\n") else note + "\n")


EDITOR_BRIEF = """\
You are the CapCut cutplan editor. Read `takes_packed.md` (cam transcript, the
voice track) and produce `edit/cutplan.json` — DO NOT touch the CapCut project
directly. Reason entirely in **cam time** (seconds into the cam file); the
writer derives the screen source via `sync_offset_ms`.

Output shape (see helpers/capcut/FORMAT.md for the full schema):

  {
    "version": 1,
    "sources": { "cam": "...", "screen": "..." },   // copy from the skeleton
    "sync_offset_ms": 0,
    "main": {
      "preset": "longform-pip",
      "canvas": {"width": 1920, "height": 1080, "fps": 25, "ratio": "original"},
      "ranges": [
        {"start": 10.0, "end": 20.0, "visual": "cam",
         "beat": "HOOK", "quote": "...", "reason": "..."}
      ],
      "estimated_duration_s": 0
    },
    "shorts": [
      {"name": "Short 1", "hook": "...", "tail": false,
       "ranges": [{"start": 12.0, "end": 15.5, "quote": "..."}]}
    ],
    "cta": {"quote": "...", "ranges": [{"start": 1369.0, "end": 1376.8}]}
  }

ABORTED / INCOMPLETE TAKES (drop these):
  - A take that ends mid-sentence or trails into "..." with no resolution is an
    OUTTAKE - drop it entirely, do not include the broken-off fragment.
  - When the same sentence is started several times, keep ONLY the last clean,
    COMPLETE delivery; drop every earlier false start.
  - Repeated opening words across consecutive phrases are the signature of
    restarts. The analysis.md "Outtake / false-start candidates" section lists
    concrete candidates (trailing "..." and repeated openings) - but you make
    the final call by reading the surrounding lines.

STRUCTURE MARKERS ("Anweisung" cues, when present):
  - The creator may mark each section verbally right before it, always prefixed
    with the trigger word "Anweisung": "Anweisung, jetzt kommt das Intro",
    "Anweisung: Hauptteil", "Anweisung, erste Tail Short", "Anweisung: Call-to-
    Action fuer Shorts", etc. When present, analysis.md lists every hit under
    "Structure markers" - treat these as AUTHORITATIVE section boundaries:
    everything from one marker up to the next belongs to that section.
  - Drop the marker phrase itself ("Anweisung, ...") from the cut entirely -
    it's a spoken directive to the editor, not audience-facing content.
  - Recordings without any "Anweisung" cues have no markers to lean on - fall
    back to inferring structure from content and the Shorts-cue-word hints
    below (this was the only option before this convention existed).

MAIN timeline (the long-form rough cut):
  - Assemble the talk chronologically; drop dead air, flubs, false starts.
  - Tag each range `visual`: "cam" when the creator talking to camera IS the
    subject (hook, asides, outro); "screen" when the screen is the subject
    (demos, walkthroughs). The writer composites screen + a camera PiP live.
  - Pick each edge on a word boundary. The writer AUTOMATICALLY adds ~0.1-0.3s
    of silence-aware air at every cut (it snaps to the real silence floor), so
    do NOT hand-pad - land start on the first word and end on the last word.
  - Put a one-line `reason` on any non-obvious cut; it surfaces in review.md.

POST-CUT READ-BACK (required before you approve - feedback points 2 + 3):
  - After you have a cutplan, run `review`; it writes `edit/assembled.md`, the
    ACTUAL assembled transcript of the Main video and every Short (incl. the
    appended CTA), reconstructed from the transcript words in your kept ranges.
  - Read each timeline top-to-bottom. It MUST sound like clean continuous
    speech: no sentence said twice in a row (even paraphrased - the tripwires
    only catch literal repeats, YOU catch the rest), no broken-off fragments,
    and no range that ends mid-thought when more was clearly coming.
  - Fix `cutplan.json` (tighten to the clean take; extend a truncated range to
    its natural sentence end) and re-run `review` until every timeline reads
    cleanly. The assembled.md tripwire list is a hint, not the whole check.

SHORTS (3-5 standalone vertical clips, 15-30s each):
  - Pull the strongest self-contained hooks from the FULL transcript, not just
    what made the Main cut. Each Short is a hook + main part, as a list of cam
    ranges (jump cuts ok). Give each a punchy `name` + `hook` line.
  - Do NOT make the call-to-action its own short (see CTA below).

TAIL SHORTS + CTA (spoken labels; "Anweisung" cues preferred, else semantic):
  - Preferred: the creator prefixes the label with "Anweisung" (see STRUCTURE
    MARKERS above), e.g. "Anweisung, erste Tail Short" / "Anweisung, zweite
    Tail Short" / "Anweisung: Call-to-Action" - segment directly by those markers.
  - Fallback (no "Anweisung" cues in this recording): the creator announces on
    camera, right before each, which is the first tail short, which is the
    second, and which is the call-to-action (e.g. "das ist die erste Tail
    Short", "... die zweite", "... jetzt die Call-to-Action"). Segment the
    tail by those spoken labels the same way.
  - Either way: everything from one label up to the next is that short (keep
    its clean hook+main take; drop restarts). Set "tail": true on tail shorts.
  - LENGTH: tail shorts are recorded separately and self-contained, so they MAY
    run longer than the 15-30s guideline. Keep the full clean take - do NOT trim
    a tail short just to hit a target length (only cut restarts/flubs). The
    review length warning is suppressed for tail shorts on the upper bound.
  - The clip labelled call-to-action goes in the top-level `cta` block, NOT in
    `shorts`. The writer appends the CTA to the end of EVERY short (regular and
    tail), so you only specify it once.
  - The analysis.md cue-hint lines are candidates only; you make the call by
    reading the transcript.
"""


# --------------------------------------------------------------------------- #
# prep
# --------------------------------------------------------------------------- #
def cmd_prep(args: argparse.Namespace) -> None:
    videos_dir = args.videos_dir.resolve()
    if not videos_dir.is_dir():
        sys.exit(f"videos_dir not found: {videos_dir}")

    cam, screen = _resolve_sources(videos_dir, args.cam, args.screen)
    edit_dir = (videos_dir / "edit").resolve()
    edit_dir.mkdir(parents=True, exist_ok=True)

    print(f"cam:    {cam}")
    print(f"screen: {screen}")

    cam_info = probe_media(cam)
    screen_info = probe_media(screen)

    # Transcribe the cam (voice source), cached. Optional: skip cleanly if no key.
    packed_md = edit_dir / "takes_packed.md"
    transcribe_status = "skipped"
    if not args.no_transcribe:
        try:
            from transcribe import load_api_key, transcribe_one

            api_key = load_api_key()
            transcribe_one(video=cam, edit_dir=edit_dir, api_key=api_key,
                           language=args.language, num_speakers=args.num_speakers)
            from pack_transcripts import pack_one_file, render_markdown

            tdir = edit_dir / "transcripts"
            jsons = sorted(tdir.glob("*.json"))
            if jsons:
                entries = [pack_one_file(p, 0.5) for p in jsons]
                packed_md.write_text(render_markdown(entries, 0.5), encoding="utf-8")
            transcribe_status = "ok"
        except SystemExit as e:
            # load_api_key exits when the key is missing — degrade gracefully.
            transcribe_status = f"skipped ({e})"
        except Exception as e:  # noqa: BLE001 - prep must not hard-fail on ASR
            transcribe_status = f"failed ({e})"

    cue_hits = _scan_shorts_cues(packed_md)
    trailing_outtakes, repeated_openings = _scan_outtakes(packed_md)
    anweisung_hits = _scan_anweisung_markers(packed_md)

    # cutplan skeleton
    fps = round(cam_info.fps) or 25
    skeleton = {
        "version": 1,
        "sources": {"cam": str(cam), "screen": str(screen)},
        "sync_offset_ms": 0,
        "main": {
            "preset": "longform-pip",
            "canvas": {"width": 1920, "height": 1080, "fps": fps, "ratio": "original"},
            "ranges": [],
            "estimated_duration_s": 0,
        },
        "shorts": [],
    }
    skeleton_path = edit_dir / "cutplan.skeleton.json"
    skeleton_path.write_text(json.dumps(skeleton, indent=2), encoding="utf-8")

    # analysis.md
    delta = abs(cam_info.duration_us - screen_info.duration_us) / 1e6
    A: list[str] = []
    A.append("# CapCut prep — analysis")
    A.append("")
    A.append("## Sources")
    A.append("")
    A.append(f"- **cam**: `{cam}`  — {cam_info.width}x{cam_info.height}, "
             f"{cam_info.duration_us/1e6:.1f}s @ {cam_info.fps:.2f}fps, "
             f"audio={'yes' if cam_info.has_audio else 'NO'}")
    A.append(f"- **screen**: `{screen}`  — {screen_info.width}x{screen_info.height}, "
             f"{screen_info.duration_us/1e6:.1f}s @ {screen_info.fps:.2f}fps, "
             f"audio={'yes' if screen_info.has_audio else 'no'}")
    A.append(f"- duration delta: {delta:.2f}s "
             + ("(within 2s — assume synced at offset 0)" if delta <= 2
                else "(>2s — set sync_offset_ms or confirm same session)"))
    A.append(f"- transcription: {transcribe_status}")
    if not cam_info.has_audio:
        A.append("- **WARNING**: cam has no audio track; longform-pip takes voice from the cam.")
    A.append("")
    A.append("## Primary reading view")
    A.append("")
    if packed_md.exists():
        A.append(f"- `{packed_md.relative_to(videos_dir)}` — the phrase-level cam transcript. "
                 "Pick cuts from here.")
    else:
        A.append("- _no transcript packed_ (run `transcribe.py` on the cam, then `pack_transcripts.py`).")
    A.append("")
    A.append("## Structure markers (\"Anweisung\" cues)")
    A.append("")
    if anweisung_hits:
        A.append(f"The creator marked {len(anweisung_hits)} section(s) with the spoken "
                 "\"Anweisung\" cue - treat these as AUTHORITATIVE section boundaries "
                 "(everything from one marker up to the next belongs to that section). "
                 "Drop the marker phrase itself from the cut.")
        A.append("")
        for line, label in anweisung_hits:
            A.append(f"- {line}" + (f"  → **{label}**" if label else ""))
    else:
        A.append("_No \"Anweisung\" markers found in this recording — fall back to "
                 "inferring structure from content and the Shorts-cue-word hints below._")
    A.append("")
    A.append("## Outtake / false-start candidates")
    A.append("")
    A.append("Deterministic hints only - the editor decides. Drop broken-off takes; "
             "in a restart cluster keep only the last clean, complete delivery.")
    A.append("")
    A.append(f"### Trailing / broken-off takes ({len(trailing_outtakes)})")
    if trailing_outtakes:
        for h in trailing_outtakes:
            A.append(f"- {h}")
    else:
        A.append("_none detected_")
    A.append("")
    A.append(f"### Repeated-opening clusters ({len(repeated_openings)})")
    if repeated_openings:
        for grp in repeated_openings:
            A.append(f"- cluster of {len(grp)} (usually keep the last):")
            for h in grp:
                A.append(f"    - {h}")
    else:
        A.append("_none detected_")
    A.append("")
    A.append("## Shorts-announcement cue hints")
    A.append("")
    if cue_hits:
        A.append("Lines mentioning a Shorts cue word (candidates only — the editor decides "
                 "which, if any, is the real tail-Short announcement):")
        A.append("")
        for h in cue_hits:
            A.append(f"- {h}")
    else:
        A.append("_No cue words found. There may still be a spoken tail-Short announcement — "
                 "the editor should read the transcript tail directly._")
    A.append("")
    A.append("## Editor brief")
    A.append("")
    A.append("```")
    A.append(EDITOR_BRIEF)
    A.append("```")
    A.append("")
    A.append("## Next")
    A.append("")
    A.append(f"1. Editor produces `{(edit_dir / 'cutplan.json').relative_to(videos_dir)}` "
             f"(start from `{skeleton_path.relative_to(videos_dir)}`).")
    A.append("2. `python helpers/capcut_pipeline.py review <edit>/cutplan.json`  → review.md gate.")
    A.append("3. On approval: `python helpers/capcut_pipeline.py write <edit>/cutplan.json "
             "--project-name \"<name>\"`.")
    A.append("")
    analysis_path = edit_dir / "analysis.md"
    analysis_path.write_text("\n".join(A), encoding="utf-8")

    print(f"\nwrote {analysis_path}")
    print(f"wrote {skeleton_path}")
    print(f"transcription: {transcribe_status}")
    print(f"shorts-cue hint lines: {len(cue_hits)}")
    print(f"outtake hints: {len(trailing_outtakes)} trailing, {len(repeated_openings)} restart clusters")

    _append_project_note(
        edit_dir,
        f"\n## CapCut prep — {videos_dir.name}\n\n"
        f"Prepped cam (`{cam.name}`, {cam_info.duration_us/1e6:.0f}s) + screen "
        f"(`{screen.name}`), delta {delta:.1f}s, transcription {transcribe_status}. "
        f"Emitted analysis.md + cutplan.skeleton.json; {len(cue_hits)} Shorts-cue hint lines. "
        "Awaiting editor cutplan.json.\n",
    )


# --------------------------------------------------------------------------- #
# review
# --------------------------------------------------------------------------- #
def cmd_review(args: argparse.Namespace) -> None:
    cutplan_path = args.cutplan.resolve()
    if not cutplan_path.exists():
        sys.exit(f"cutplan not found: {cutplan_path}")
    cutplan = load_cutplan(cutplan_path)

    edit_dir = args.edit_dir.resolve() if args.edit_dir else cutplan_path.parent
    edit_dir.mkdir(parents=True, exist_ok=True)

    sources = cutplan.get("sources") or {}
    probes = {
        "cam": _probe_safe(sources.get("cam")),
        "screen": _probe_safe(sources.get("screen")),
    }

    errors, warnings = lint_cutplan(cutplan, probes)

    review_md = render_review_md(cutplan, probes)
    review_path = edit_dir / "review.md"
    review_path.write_text(review_md, encoding="utf-8")

    # Post-cut read-back: assembled transcript per timeline + tripwires.
    assembled_trips = 0
    cam_media = probes.get("cam")
    cam_src = (cutplan.get("sources") or {}).get("cam")
    if cam_media is not None and cam_src:
        from capcut.cutcheck import render_assembled_md

        assembled_md, assembled_trips = render_assembled_md(
            cutplan, str(Path(cam_src).expanduser()),
            cam_media.duration_us / 1e6, edit_dir)
        (edit_dir / "assembled.md").write_text(assembled_md, encoding="utf-8")
        print(f"wrote {edit_dir / 'assembled.md'}")

    print(f"wrote {review_path}")
    if warnings:
        print(f"\n{len(warnings)} warning(s):")
        for w in warnings:
            print(f"  - {w}")
    if errors:
        print(f"\n{len(errors)} ERROR(s) — write is blocked until fixed:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)

    main = cutplan.get("main") or {}
    print(f"\nOK: {len(main.get('ranges') or [])} main ranges, "
          f"{len(cutplan.get('shorts') or [])} shorts.")
    if assembled_trips:
        print(f"\nREAD-BACK: {assembled_trips} tripwire(s) in assembled.md — read it "
              "top-to-bottom per timeline and confirm each reads as clean continuous "
              "speech (no restated/broken/mid-thought passages) before writing.")
    else:
        print("\nREAD-BACK: assembled.md written — read it top-to-bottom per timeline "
              "to confirm clean continuous speech before writing.")
    print(f"Review {review_path} + {edit_dir / 'assembled.md'}, then approve with `write`.")


# --------------------------------------------------------------------------- #
# write
# --------------------------------------------------------------------------- #
def _select_template(templates: Path, cutplan: dict, template_name: str | None) -> tuple[Path, Path]:
    preset = (cutplan.get("main", {}).get("preset") or "single").lower()
    if template_name:
        name = template_name
    elif preset in ("longform-pip", "longform_pip"):
        name = "longform_pip"
    else:
        name = "single_timeline"
    template_dir = (templates / name).resolve()
    if not (template_dir / "draft_info.json").exists():
        sys.exit(f"template '{name}' not found at {template_dir} "
                 "(see capcut_templates/README.md)")
    short_template_dir = (templates / "short_switch").resolve()
    if cutplan.get("shorts") and not (short_template_dir / "draft_info.json").exists():
        sys.exit(f"cutplan declares shorts but short-switch template is missing at "
                 f"{short_template_dir}")
    return template_dir, short_template_dir


def cmd_write(args: argparse.Namespace) -> None:
    cutplan_path = args.cutplan.resolve()
    if not cutplan_path.exists():
        sys.exit(f"cutplan not found: {cutplan_path}")
    cutplan = load_cutplan(cutplan_path)

    edit_dir = args.edit_dir.resolve() if args.edit_dir else cutplan_path.parent

    # Re-lint as a final guardrail: never write a cutplan with hard errors.
    sources = cutplan.get("sources") or {}
    probes = {
        "cam": _probe_safe(sources.get("cam")),
        "screen": _probe_safe(sources.get("screen")),
    }
    errors, warnings = lint_cutplan(cutplan, probes)
    for w in warnings:
        print(f"warning: {w}")
    if errors:
        print("cutplan has hard errors — aborting write. Run `review` and fix:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)

    template_dir, short_template_dir = _select_template(
        args.templates, cutplan, args.template_name
    )

    try:
        drafts_root = detect_drafts_root(args.drafts_root)
    except FileNotFoundError as e:
        sys.exit(str(e))
    if not args.dry_run:
        try:
            sanity_check_root(drafts_root)
        except RuntimeError as e:
            sys.exit(str(e))

    print(f"drafts root: {drafts_root}")
    print(f"template:    {template_dir}")

    try:
        report = generate(
            cutplan=cutplan,
            template_dir=template_dir,
            drafts_root=drafts_root,
            project_name=args.project_name,
            media_mode=args.media_mode,
            dry_run=args.dry_run,
            register=not args.no_register,
            short_template_dir=short_template_dir,
            edit_dir=edit_dir,
        )
    except (ValueError, RuntimeError, FileNotFoundError) as e:
        sys.exit(f"error: {e}")
    except ValidationError as e:
        sys.exit(f"{e}")

    print()
    print(report.render())
    if not args.dry_run:
        print(f"\nWrote project to: {report.project_dir}")
        print("Open (or restart) CapCut and check the project list.")
        _append_project_note(
            edit_dir,
            f"\n## CapCut write — {args.project_name}\n\n"
            f"Generated CapCut project `{args.project_name}` from "
            f"`{cutplan_path.name}`: {report.preset} main + "
            f"{len(cutplan.get('shorts') or [])} shorts. Registered={not args.no_register}.\n",
        )


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="CapCut project pipeline (prep/review/write)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prep", help="ffprobe + transcribe + pack + skeleton + analysis.md")
    p.add_argument("videos_dir", type=Path, help="Directory holding the cam+screen recording")
    p.add_argument("--cam", type=Path, default=None, help="Cam (voice) source; auto-detected if omitted")
    p.add_argument("--screen", type=Path, default=None, help="Screen source; auto-detected if omitted")
    p.add_argument("--language", default=None, help="Optional ISO language code for Scribe")
    p.add_argument("--num-speakers", type=int, default=None, help="Optional speaker count hint")
    p.add_argument("--no-transcribe", action="store_true",
                   help="Skip transcription/packing (cheap prep; reuse cached packed transcript)")
    p.set_defaults(func=cmd_prep)

    r = sub.add_parser("review", help="lint cutplan + render review.md gate")
    r.add_argument("cutplan", type=Path, help="Path to cutplan.json")
    r.add_argument("--edit-dir", type=Path, default=None,
                   help="Where to write review.md (default: cutplan's directory)")
    r.set_defaults(func=cmd_review)

    w = sub.add_parser("write", help="invoke the CapCut writer (registers the project)")
    w.add_argument("cutplan", type=Path, help="Path to cutplan.json")
    w.add_argument("--project-name", required=True, help="Name of the new CapCut project folder")
    w.add_argument("--templates", type=Path, default=DEFAULT_TEMPLATES,
                   help=f"Template store dir (default: {DEFAULT_TEMPLATES})")
    w.add_argument("--template-name", default=None, help="Force a template subfolder")
    w.add_argument("--drafts-root", default="auto", help="CapCut drafts root or 'auto'")
    w.add_argument("--media-mode", choices=["reference", "copy"], default="reference")
    w.add_argument("--edit-dir", type=Path, default=None,
                   help="Where to append project.md (default: cutplan's directory)")
    w.add_argument("--dry-run", action="store_true", help="Validate without writing")
    w.add_argument("--no-register", action="store_true",
                   help="Do not add an entry to root_meta_info.json")
    w.set_defaults(func=cmd_write)

    return ap


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
