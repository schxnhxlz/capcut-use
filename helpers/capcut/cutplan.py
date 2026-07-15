"""Cutplan schema helpers: load, lint, and render the human review gate.

A cutplan is the neutral JSON the editor sub-agent produces and the CapCut
writer consumes (see FORMAT.md for the full schema). This module is the
deterministic guardrail around the LLM's taste calls: it never *makes*
editorial decisions, it only validates them and renders them for human review.
"""

from __future__ import annotations

import json
from pathlib import Path

from .probe import MediaInfo

# Soft-guidance bounds (warnings only; the editor may override with reason).
SHORTS_MIN, SHORTS_MAX = 3, 5
SHORT_MIN_S, SHORT_MAX_S = 15.0, 30.0
SYNC_MISMATCH_WARN_S = 2.0
VALID_VISUALS = {"cam", "screen"}


def load_cutplan(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _mmss(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    m = int(seconds // 60)
    s = seconds - m * 60
    return f"{m:d}:{s:05.2f}"


def _pack_total_s(ranges: list[dict]) -> float:
    """Sum of range durations (ranges pack contiguously on output)."""
    total = 0.0
    for r in ranges:
        try:
            total += max(0.0, float(r["end"]) - float(r["start"]))
        except (KeyError, TypeError, ValueError):
            pass
    return total


def lint_cutplan(
    cutplan: dict, probes: dict[str, MediaInfo | None]
) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Errors must abort the write; warnings are advisory.

    `probes` maps source key ("cam"/"screen") to its MediaInfo, or None when the
    file was missing / un-probe-able.
    """
    errors: list[str] = []
    warnings: list[str] = []

    sources = cutplan.get("sources") or {}
    cam_path = sources.get("cam")
    screen_path = sources.get("screen")
    if not cam_path:
        errors.append("sources.cam is required")
    if not screen_path:
        errors.append("sources.screen is required for longform-pip")

    cam = probes.get("cam")
    screen = probes.get("screen")
    if cam_path and cam is None:
        errors.append(f"cam source not found or not probe-able: {cam_path}")
    if screen_path and screen is None:
        errors.append(f"screen source not found or not probe-able: {screen_path}")

    cam_dur_s = cam.duration_us / 1e6 if cam else None

    # sync offset
    off = cutplan.get("sync_offset_ms", 0)
    if not isinstance(off, (int, float)):
        errors.append(f"sync_offset_ms must be a number, got {off!r}")
    elif abs(off) > 60_000:
        warnings.append(f"sync_offset_ms={off} is > 60s; check cam/screen alignment")
    if cam and screen and abs(cam.duration_us - screen.duration_us) / 1e6 > SYNC_MISMATCH_WARN_S:
        warnings.append(
            f"cam and screen durations differ by "
            f"{abs(cam.duration_us - screen.duration_us) / 1e6:.1f}s (> {SYNC_MISMATCH_WARN_S}s); "
            "confirm they are the same session / set sync_offset_ms"
        )

    # main ranges
    main = cutplan.get("main") or {}
    ranges = main.get("ranges") or []
    if not ranges:
        errors.append("main.ranges is empty")
    for i, r in enumerate(ranges):
        _lint_range(f"main.ranges[{i}]", r, cam_dur_s, errors, require_visual=True)

    # shorts
    shorts = cutplan.get("shorts") or []
    if shorts and not (SHORTS_MIN <= len(shorts) <= SHORTS_MAX):
        warnings.append(
            f"{len(shorts)} shorts (guideline is {SHORTS_MIN}-{SHORTS_MAX})"
        )
    for si, sh in enumerate(shorts):
        label = sh.get("name") or f"shorts[{si}]"
        sh_ranges = sh.get("ranges") or []
        if not sh_ranges:
            errors.append(f"{label}: has no ranges")
        for j, r in enumerate(sh_ranges):
            _lint_range(f"{label}.ranges[{j}]", r, cam_dur_s, errors, require_visual=False)
        total = _pack_total_s(sh_ranges)
        if sh_ranges and not (SHORT_MIN_S <= total <= SHORT_MAX_S):
            warnings.append(
                f"{label}: total {total:.1f}s outside {SHORT_MIN_S:.0f}-{SHORT_MAX_S:.0f}s guideline"
            )

    # cta (optional; appended to every short, not a short itself)
    cta = cutplan.get("cta")
    if cta:
        cta_ranges = cta.get("ranges") or []
        if not cta_ranges:
            errors.append("cta.ranges is empty (omit the cta block if there is no CTA)")
        for j, r in enumerate(cta_ranges):
            _lint_range(f"cta.ranges[{j}]", r, cam_dur_s, errors, require_visual=False)

    return errors, warnings


def _lint_range(label, r, cam_dur_s, errors, *, require_visual) -> None:
    try:
        start = float(r["start"])
        end = float(r["end"])
    except (KeyError, TypeError, ValueError):
        errors.append(f"{label}: start/end missing or non-numeric")
        return
    if end <= start:
        errors.append(f"{label}: end ({end}) <= start ({start})")
    if start < 0:
        errors.append(f"{label}: start ({start}) < 0")
    if cam_dur_s is not None and end > cam_dur_s + 0.05:
        errors.append(f"{label}: end ({end:.2f}s) beyond cam duration ({cam_dur_s:.2f}s)")
    if require_visual:
        vis = r.get("visual")
        if vis not in VALID_VISUALS:
            errors.append(f"{label}: visual must be one of {sorted(VALID_VISUALS)}, got {vis!r}")


def render_review_md(cutplan: dict, probes: dict[str, MediaInfo | None]) -> str:
    """Render the human review gate as markdown."""
    sources = cutplan.get("sources") or {}
    cam = probes.get("cam")
    screen = probes.get("screen")
    main = cutplan.get("main") or {}
    ranges = main.get("ranges") or []
    shorts = cutplan.get("shorts") or []
    canvas = main.get("canvas") or {}

    L: list[str] = []
    L.append("# CapCut cutplan — review gate")
    L.append("")
    L.append("Review this cut, then approve to generate the CapCut project "
             "(`capcut_pipeline.py write`). Edit `cutplan.json` and re-run `review` to change it.")
    L.append("")

    # Sources
    L.append("## Sources")
    L.append("")
    for key in ("cam", "screen"):
        p = sources.get(key)
        info = probes.get(key)
        if not p:
            continue
        if info:
            L.append(f"- **{key}**: `{p}`  —  {info.width}x{info.height}, "
                     f"{info.duration_us/1e6:.1f}s @ {info.fps:.0f}fps")
        else:
            L.append(f"- **{key}**: `{p}`  —  MISSING")
    off = cutplan.get("sync_offset_ms", 0)
    L.append(f"- **sync_offset_ms**: {off}")
    if cam and screen:
        delta = abs(cam.duration_us - screen.duration_us) / 1e6
        L.append(f"- duration delta: {delta:.2f}s")
    L.append("")

    # Main cut
    total_s = _pack_total_s(ranges)
    cam_visual = sum(1 for r in ranges if r.get("visual") == "cam")
    screen_visual = sum(1 for r in ranges if r.get("visual") == "screen")
    L.append(f"## Main timeline — {_mmss(total_s)} "
             f"({len(ranges)} ranges: {cam_visual} cam, {screen_visual} screen)")
    L.append("")
    L.append("| # | beat | visual | source in | dur | quote |")
    L.append("|---|------|--------|-----------|-----|-------|")
    run = 0.0
    for i, r in enumerate(ranges):
        start = float(r.get("start", 0)); end = float(r.get("end", 0))
        dur = max(0.0, end - start)
        quote = (r.get("quote") or "").replace("|", "\\|")
        if len(quote) > 70:
            quote = quote[:67] + "..."
        L.append(f"| {i} | {r.get('beat','')} | {r.get('visual','')} | "
                 f"{_mmss(start)}-{_mmss(end)} | {dur:.1f}s | {quote} |")
        run += dur
    L.append("")
    L.append(f"**Estimated Main runtime: {_mmss(total_s)}** "
             f"(estimate in cutplan: {main.get('estimated_duration_s', '—')}s)")
    L.append("")

    # Dropped cuts (from reason fields)
    dropped = [r for r in ranges if r.get("reason")]
    if dropped:
        L.append("## Editorial notes (why cuts were made)")
        L.append("")
        for r in dropped:
            L.append(f"- `{_mmss(float(r.get('start',0)))}` {r.get('beat','')}: {r['reason']}")
        L.append("")

    # Shorts
    cta = cutplan.get("cta") or {}
    cta_ranges = cta.get("ranges") or []
    cta_total = _pack_total_s(cta_ranges)
    L.append(f"## Shorts — {len(shorts)}")
    L.append("")
    if cta_ranges:
        L.append(f"_Each short below also has the shared CTA ({cta_total:.1f}s) appended at the end._")
        L.append("")
    if not shorts:
        L.append("_No shorts proposed._")
        L.append("")
    for si, sh in enumerate(shorts):
        name = sh.get("name") or f"Short {si+1}"
        tail = " `[TAIL]`" if sh.get("tail") else ""
        sh_ranges = sh.get("ranges") or []
        total = _pack_total_s(sh_ranges)
        with_cta = f" (+CTA = {total + cta_total:.1f}s)" if cta_ranges else ""
        hook = sh.get("hook") or ""
        L.append(f"### {name}{tail} — {total:.1f}s{with_cta}")
        if hook:
            L.append(f"> {hook}")
        L.append("")
        for j, r in enumerate(sh_ranges):
            start = float(r.get("start", 0)); end = float(r.get("end", 0))
            q = (r.get("quote") or "").replace("|", "\\|")
            L.append(f"- cut {j}: `{_mmss(start)}-{_mmss(end)}` ({end-start:.1f}s){(' — ' + q) if q else ''}")
        L.append("")

    # Call-to-action (shared appendix)
    if cta_ranges:
        L.append(f"## Call-to-action (appended to every short) — {cta_total:.1f}s")
        L.append("")
        cq = cta.get("quote") or ""
        if cq:
            L.append(f"> {cq}")
            L.append("")
        for j, r in enumerate(cta_ranges):
            start = float(r.get("start", 0)); end = float(r.get("end", 0))
            q = (r.get("quote") or "").replace("|", "\\|")
            L.append(f"- cut {j}: `{_mmss(start)}-{_mmss(end)}` ({end-start:.1f}s){(' — ' + q) if q else ''}")
        L.append("")

    L.append("---")
    L.append("")
    L.append("**Approve to generate?** If yes, run:")
    L.append("")
    L.append("```")
    L.append("python helpers/capcut_pipeline.py write <edit>/cutplan.json --project-name \"<name>\"")
    L.append("```")
    L.append("")
    return "\n".join(L)
