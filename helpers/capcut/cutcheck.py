"""Cut-quality helpers: silence-aware edge air + post-cut transcript read-back.

Two concerns, one module, all deterministic (the semantic "does this read
cleanly" judgement stays an agent call):

1. Edge air (feedback point 1). Cuts authored by the editor land on Scribe word
   timestamps, which clip word-tails/breaths. `detect_silences` runs ffmpeg
   `silencedetect` on the cam audio; `refine_ranges` snaps each cut edge into a
   real nearby silence (leaving ~0.1s of air) and falls back to a small fixed
   pad where the editor made a deliberate mid-sentence cut.

2. Read-back (feedback points 2 + 3). `assemble_timeline` reconstructs the
   actual spoken text of a timeline from the transcript words inside each kept
   range; `scan_readback_tripwires` flags repeated/restated openings across a
   seam and ranges that end mid-thought. Hints only - surfaced in assembled.md.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
@dataclass
class EdgeConfig:
    window_s: float = 0.30       # search radius around an editor edge for silence
    residual_s: float = 0.10     # air kept on the cut when snapping into silence
    pad_s: float = 0.15          # fixed fallback pad when no silence is near
    silence_db: float = -30.0    # silencedetect noise floor
    min_silence_s: float = 0.08  # silencedetect minimum silence duration


# --------------------------------------------------------------------------- #
# silence detection (ffmpeg silencedetect), cached to edit/silences.json
# --------------------------------------------------------------------------- #
_SIL_START = re.compile(r"silence_start:\s*([0-9.]+)")
_SIL_END = re.compile(r"silence_end:\s*([0-9.]+)")


def detect_silences(
    cam_path: str | Path,
    edit_dir: str | Path | None = None,
    cfg: EdgeConfig | None = None,
) -> list[tuple[float, float]] | None:
    """Return silence intervals [(start_s, end_s), ...] for the cam audio.

    Cached to `<edit_dir>/silences.json` keyed by (path, mtime, db, d). Returns
    None if ffmpeg is unavailable or the file is missing (caller falls back to a
    fixed pad).
    """
    cfg = cfg or EdgeConfig()
    cam = Path(cam_path)
    if not cam.exists():
        return None
    try:
        mtime = cam.stat().st_mtime_ns
    except OSError:
        return None

    cache = Path(edit_dir) / "silences.json" if edit_dir else None
    key = {"cam": str(cam), "mtime": mtime, "db": cfg.silence_db, "d": cfg.min_silence_s}
    if cache and cache.exists():
        try:
            blob = json.loads(cache.read_text(encoding="utf-8"))
            if all(blob.get(k) == v for k, v in key.items()):
                return [(float(a), float(b)) for a, b in blob.get("intervals", [])]
        except (ValueError, OSError, TypeError):
            pass

    cmd = [
        "ffmpeg", "-nostdin", "-i", str(cam),
        "-af", f"silencedetect=noise={cfg.silence_db}dB:d={cfg.min_silence_s}",
        "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None
    log = (proc.stderr or "") + (proc.stdout or "")

    intervals: list[tuple[float, float]] = []
    pending_start: float | None = None
    for line in log.splitlines():
        ms = _SIL_START.search(line)
        if ms:
            pending_start = float(ms.group(1))
            continue
        me = _SIL_END.search(line)
        if me and pending_start is not None:
            end = float(me.group(1))
            if end > pending_start:
                intervals.append((pending_start, end))
            pending_start = None
    intervals.sort()

    if cache:
        try:
            cache.write_text(json.dumps({**key, "intervals": [[a, b] for a, b in intervals]},
                                        indent=2), encoding="utf-8")
        except OSError:
            pass
    return intervals


# --------------------------------------------------------------------------- #
# edge refinement
# --------------------------------------------------------------------------- #
def _silence_ending_near(silences: list[tuple[float, float]], t: float,
                         window: float) -> tuple[float, float] | None:
    """Silence whose END lands in [t-window, t+eps] (the gap before a word)."""
    best = None
    for a, b in silences:
        if t - window <= b <= t + 0.05:
            if best is None or abs(b - t) < abs(best[1] - t):
                best = (a, b)
    return best


def _silence_starting_near(silences: list[tuple[float, float]], t: float,
                           window: float) -> tuple[float, float] | None:
    """Silence whose START lands in [t-eps, t+window] (the gap after a word)."""
    best = None
    for a, b in silences:
        if t - 0.05 <= a <= t + window:
            if best is None or abs(a - t) < abs(best[0] - t):
                best = (a, b)
    return best


_GUARD_S = 0.02  # never pad closer than this to an adjacent word


def refine_ranges(
    ranges: list[dict],
    silences: list[tuple[float, float]] | None,
    cam_dur_s: float,
    cfg: EdgeConfig | None = None,
    *,
    words: list["Word"] | None = None,
    pad_last_end: bool = True,
) -> tuple[list[dict], list[dict]]:
    """Return (refined_ranges, notes). Each range gets a bit of air on its edges,
    snapped into a real silence when one is nearby, else a small fixed pad.

    Padding never crosses into an adjacent transcript word - kept OR dropped (so
    it lands in genuine inter-word silence and never grabs the onset of a dropped
    false start that sits in the gap). When `words` is None it only guards
    against the neighbouring kept range. The last range's END is left untouched
    when `pad_last_end` is False (the caller's end-air owns the final tail).
    """
    cfg = cfg or EdgeConfig()
    sil = silences or []
    ws = sorted(words or [], key=lambda w: w.start)
    out: list[dict] = []
    notes: list[dict] = []
    n = len(ranges)

    def prev_word_end(t: float) -> float | None:
        cands = [w.end for w in ws if w.end <= t + 0.01]
        return max(cands) if cands else None

    def next_word_start(t: float) -> float | None:
        cands = [w.start for w in ws if w.start >= t - 0.01]
        return min(cands) if cands else None

    for i, r in enumerate(ranges):
        start = float(r["start"])
        end = float(r["end"])
        prev_end = float(ranges[i - 1]["end"]) if i > 0 else None
        next_start = float(ranges[i + 1]["start"]) if i + 1 < n else None

        # ---- start edge: add air before the first word ----
        s_hit = _silence_ending_near(sil, start, cfg.window_s)
        if s_hit is not None:
            a, _b = s_hit
            new_start = max(a, start - cfg.residual_s)
            s_mode = "silence"
        else:
            new_start = start - cfg.pad_s
            s_mode = "pad"
        new_start = max(0.0, new_start)
        if prev_end is not None and prev_end <= start:
            new_start = max(new_start, prev_end)
        pw = prev_word_end(start)  # clamp off any word before this range's first word
        if pw is not None and pw < start:
            new_start = max(new_start, pw + _GUARD_S)

        # ---- end edge: add air after the last word ----
        do_end = pad_last_end or i != n - 1
        if do_end:
            e_hit = _silence_starting_near(sil, end, cfg.window_s)
            if e_hit is not None:
                _a, b = e_hit
                new_end = min(b, end + cfg.residual_s)
                e_mode = "silence"
            else:
                new_end = end + cfg.pad_s
                e_mode = "pad"
            new_end = min(cam_dur_s, new_end)
            if next_start is not None and next_start >= end:
                new_end = min(new_end, next_start)
            nw = next_word_start(end)  # clamp off the next word (incl. dropped false starts)
            if nw is not None and nw > end:
                new_end = min(new_end, nw - _GUARD_S)
        else:
            new_end, e_mode = end, "kept(end-air)"

        if new_end - new_start <= 0 or new_start > start or new_end < end:
            # degenerate or clamp pushed past the authored edge; keep original
            new_start = min(new_start, start)
            new_end = max(new_end, end)
            new_start = max(0.0, new_start)
            new_end = min(cam_dur_s, new_end)
            if new_end - new_start <= 0:
                new_start, new_end = start, end
                s_mode = e_mode = "skip"

        nr = dict(r)
        nr["start"] = round(new_start, 3)
        nr["end"] = round(new_end, 3)
        out.append(nr)
        notes.append({"i": i, "start": start, "end": end,
                      "new_start": nr["start"], "new_end": nr["end"],
                      "start_mode": s_mode, "end_mode": e_mode})
    return out, notes


# --------------------------------------------------------------------------- #
# transcript words + timeline assembly (read-back)
# --------------------------------------------------------------------------- #
@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class AssembledRange:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)


_SENT_COMPLETE = ".!?"     # a range that ends here reads as a finished thought
_BROKEN_SUFFIXES = ("...", "…", "--", "-")


def load_words(transcript_json: str | Path) -> list[Word]:
    """Load spoken words (type 'word') with timestamps from a Scribe transcript."""
    p = Path(transcript_json)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    words: list[Word] = []
    for w in data.get("words", []):
        if w.get("type", "word") != "word":
            continue
        s = w.get("start")
        if s is None:
            continue
        txt = (w.get("text") or "").strip()
        if not txt:
            continue
        words.append(Word(float(s), float(w.get("end", s)), txt))
    words.sort(key=lambda x: x.start)
    return words


def assemble_timeline(ranges: list[dict], words: list[Word]) -> list[AssembledRange]:
    """Reconstruct the actual spoken text per range, in output order."""
    out: list[AssembledRange] = []
    for r in ranges:
        s = float(r["start"]); e = float(r["end"])
        kept = [w for w in words if s <= w.start < e]
        text = " ".join(w.text for w in kept)
        text = re.sub(r"\s+([,.!?…])", r"\1", text)
        out.append(AssembledRange(s, e, text, kept))
    return out


def _tokens(text: str) -> list[str]:
    return re.findall(r"[\wäöüßÄÖÜ']+", text.lower())


def render_assembled_md(
    cutplan: dict,
    cam_path: str | Path,
    cam_dur_s: float,
    edit_dir: str | Path | None,
    cfg: EdgeConfig | None = None,
) -> tuple[str, int]:
    """Build the post-cut read-back doc: the actual assembled transcript of each
    timeline (Main + every Short incl. the appended CTA), reconstructed from the
    transcript words inside each refined range, plus deterministic tripwires.

    Returns (markdown, total_tripwire_count). Degrades to a note if no transcript.
    """
    cfg = cfg or EdgeConfig()
    words: list[Word] = []
    if edit_dir is not None:
        tj = Path(edit_dir) / "transcripts" / f"{Path(cam_path).stem}.json"
        words = load_words(tj)
    silences = detect_silences(cam_path, edit_dir, cfg)

    L: list[str] = []
    L.append("# Post-cut read-back — assembled transcript per timeline")
    L.append("")
    L.append("Read each timeline top-to-bottom: it must sound like clean continuous "
             "speech — no repeated or restated openings, no broken-off fragments, no "
             "sentence cut off mid-thought. Tripwires are deterministic hints only "
             "(paraphrased duplicates are NOT flagged — judge those by reading).")
    L.append("")
    if not words:
        L.append("_No cam transcript available; cannot reconstruct the read-back. "
                 "Run `prep` (transcription) first._")
        return "\n".join(L) + "\n", 0

    total_trips = 0

    def _section(title: str, ranges: list[dict], *, pad_last_end: bool) -> None:
        nonlocal total_trips
        refined, _ = refine_ranges(ranges, silences, cam_dur_s, cfg,
                                   words=words, pad_last_end=pad_last_end)
        asm = assemble_timeline(refined, words)
        trips = scan_readback_tripwires(asm, words)
        total_trips += len(trips)
        L.append(f"## {title}")
        L.append("")
        L.append("> " + " ".join(a.text for a in asm if a.text).replace("\n", " "))
        L.append("")
        for j, a in enumerate(asm):
            L.append(f"- `{a.start:.1f}-{a.end:.1f}` — {a.text or '_(no words)_'}")
        L.append("")
        L.append(f"**Read-back tripwires ({len(trips)}):**")
        if trips:
            for t in trips:
                L.append(f"- {t}")
        else:
            L.append("- none")
        L.append("")

    main = cutplan.get("main") or {}
    _section(main.get("name", "Main Video"), main.get("ranges") or [], pad_last_end=False)

    cta = cutplan.get("cta") or {}
    cta_ranges = cta.get("ranges") or []
    for si, sh in enumerate(cutplan.get("shorts") or []):
        name = sh.get("name") or f"Short {si+1}"
        # each short = its own ranges + the shared CTA appended (mirrors the writer)
        _section(name, list(sh.get("ranges") or []) + list(cta_ranges),
                 pad_last_end=False)

    return "\n".join(L) + "\n", total_trips


def scan_readback_tripwires(
    assembled: list[AssembledRange], words: list[Word], *, open_n: int = 4,
) -> list[str]:
    """Deterministic read-back warnings (hints only):
      - repeated/restated opening across a seam (point 2, literal/near-literal)
      - a range that ends mid-thought (point 3), with a suggested extension
    """
    warns: list[str] = []
    for i, ar in enumerate(assembled):
        toks = _tokens(ar.text)
        if not toks:
            continue
        # --- repeated opening vs the previous range ---
        if i > 0:
            prev = assembled[i - 1]
            prev_toks = _tokens(prev.text)
            open_ng = toks[:open_n]
            if len(open_ng) >= 3:
                prev_open = prev_toks[:open_n]
                overlap = sum(1 for a, b in zip(open_ng, prev_open) if a == b)
                joined_prev = " ".join(prev_toks)
                restated = " ".join(open_ng) in joined_prev
                if overlap >= 3 or restated:
                    warns.append(
                        f"range {i} (`{ar.start:.1f}-{ar.end:.1f}`) opens like the "
                        f"previous range - possible restated sentence: "
                        f"\"{' '.join(open_ng)}...\"")
        # --- broken-off fragment / mid-thought ending ---
        if ar.words:
            raw = ar.words[-1].text.strip()
            last = raw.rstrip('"\'')
            if any(last.endswith(sfx) for sfx in _BROKEN_SUFFIXES):
                warns.append(
                    f"range {i} (`{ar.start:.1f}-{ar.end:.1f}`) ends on a broken-off "
                    f"fragment \"{raw}\" - drop it or keep the clean take")
            elif last and last[-1] not in _SENT_COMPLETE:
                # Not mid-thought if the NEXT kept range is a direct source
                # continuation (a jump cut inside the same sentence): the thought
                # is finished later in the timeline, so it reads fine.
                nxt_asm = assembled[i + 1] if i + 1 < len(assembled) else None
                continues = nxt_asm is not None and 0 <= (nxt_asm.start - ar.end) <= 2.5
                if not continues:
                    finisher = next(
                        (w for w in words
                         if w.start >= ar.end and w.text.rstrip('"\'')[-1:] in _SENT_COMPLETE),
                        None)
                    sug = (f"; extend end to ~{finisher.end:.1f}s (\"...{finisher.text}\") "
                           f"or trim back to the previous full stop" if finisher else "")
                    warns.append(
                        f"range {i} (`{ar.start:.1f}-{ar.end:.1f}`) ends mid-thought "
                        f"on \"{raw}\"{sug}")
    return warns
