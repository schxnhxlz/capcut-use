# CapCut quick-start (talking-head → editable CapCut project)

This is the short "what do I do next time" guide for turning a synced
**cam + screen** recording into a native, editable **CapCut project** (one
long-form rough cut + 3–5 Shorts + a Raw reference timeline). Everything is
driven by chatting with the agent; the commands below are just what runs under
the hood.

---

## 1. Record

Record **two files** into one folder (one folder per video):

| File               | What it is                                                   |
| ------------------ | ------------------------------------------------------------ |
| cam **with audio** | you talking to camera — this is the **voice** track          |
| screen capture     | the screen you're demoing — put `**screen**` in the filename |

Auto-detection rule: the file whose name contains `screen` is the screen
capture; the other one is the cam. If both are ambiguous, you'll be asked to
name them explicitly.

**Say "Anweisung" out loud right before each section** so the agent can place
things with certainty instead of guessing from content alone:

- _"Anweisung, jetzt kommt das Intro"_
- _"Anweisung, Hauptteil"_
- _"Anweisung, erste Tail Short"_ ... _"Anweisung, zweite Tail Short"_
- _"Anweisung, Call-to-Action für Shorts"_ — this gets appended to the end of
  **every** Short automatically (it is not its own Short).

The word **"Anweisung"** itself is the trigger — say it, then name whatever
section is coming next in your own words. The agent scans the transcript for
every "Anweisung" hit and lists them in `edit/analysis.md`; everything from one
marker up to the next becomes that section, and the marker phrase itself is cut
out (it's a directive to the editor, not audience-facing content). No markers?
The agent falls back to inferring structure from content, as before.

Leave a short pause between separate takes.

---

## 2. Drop the folder and talk to the agent

```bash
cd "/path/to/your/video folder"   # the folder with the two .mp4s
claude                            # or codex, cursor, etc.
```

Then just say:

> **edit this into a CapCut project**

The agent reads `[SKILL.md](./SKILL.md)`, inventories the two sources, and runs
the pipeline. You confirm once at the review gate, then it writes the project.

---

## 3. What happens under the hood (3 steps)

All outputs land in `<your folder>/edit/`. The agent runs these for you — you
normally don't type them yourself:

```bash
# 1. PREP — probe + transcribe the cam + pack transcripts + first analysis
python3 helpers/capcut_pipeline.py prep "/path/to/your/video folder"

#    → the agent (cutplan editor) reads edit/takes_packed.md and writes
#      edit/cutplan.json (the cut decisions: Main ranges + Shorts + CTA)

# 2. REVIEW — lint + render the human gate + the post-cut read-back
python3 helpers/capcut_pipeline.py review "/path/to/your/video folder/edit/cutplan.json"
#    → writes edit/review.md      (cut table you approve)
#    → writes edit/assembled.md   (the ACTUAL spoken text of every timeline,
#                                   so you can read it back top-to-bottom)

# 3. WRITE — build the native CapCut project (only after you approve)
python3 helpers/capcut_pipeline.py write \
  "/path/to/your/video folder/edit/cutplan.json" \
  --project-name "My Video V1"
```

### The review gate is where you look

- `**edit/review.md**` — the proposed cuts: Main video table, the Shorts, and
  editorial notes. This is your approve/reject point.
- `**edit/assembled.md**` — the reconstructed transcript of the Main video and
  each Short (with the CTA appended). Read each one top-to-bottom: it should
  sound like clean, continuous speech — no sentence said twice, no broken-off
  fragments, no half-finished thoughts. Deterministic "tripwires" flag the
  obvious ones; you (and the agent) catch the rest by reading.

If something reads wrong, tell the agent, it fixes `cutplan.json` and re-runs
`review`. Repeat until it reads clean, **then** approve → `write`.

---

## 4. Open it in CapCut

The project is written into your CapCut drafts folder
(`~/Movies/CapCut/User Data/Projects/com.lveditor.draft/<project name>`).
**Restart CapCut** (or reopen the project list) and it shows up.

You get **6 timelines**:

1. **Main Video** — the full long-form rough cut (cam + screen PiP + voice +
   effect + music bed).
2. **Short 01–0N** — 9:16 vertical Shorts, each ending with the CTA.
3. **Raw** — the untouched cam + screen dropped in with audio, no cuts, so you
   can always grab original material if a cut removed something you wanted.

### Two things CapCut can't set from the file (toggle by hand once)

- **Audio normalization** and **Voice enhancement** on the voice clips (the
  Voice Changer / "Voice Crisper" effect _is_ applied automatically).

---

## Automatic quality helpers (already on)

- **Silence-aware cut edges** — every cut gets ~0.1–0.3s of air, snapped to the
  real silence around the words (no clipped word-tails or breaths). Cached in
  `edit/silences.json`.
- **End-of-video air** — ~0.75s of breathing room after the last spoken word.
- **Outtake / restart detection** — false starts and broken-off takes are
  dropped; the read-back (`assembled.md`) is the safety net.

### Handy knobs (optional, in `cutplan.json`)

- `"edge_refine": false` — turn off automatic edge air.
- `"main": { "edge_pad_s": 0.2 }` — change the fixed edge pad.
- `"main": { "end_air_s": 1.0 }` — change the end-of-video air.

---

## Requirements

`ffmpeg` (edge detection + probing) and an ElevenLabs API key in `.env`
(`ELEVENLABS_API_KEY=...`) for transcription. See `[install.md](./install.md)`.
