# Motion Graphics Studio

A **standalone** motion-graphics tool — independent of any single video project.
Pick a template, edit its text / numbers / colors, preview live, and render a
**transparent (alpha) video** you can drop onto any footage in CapCut, Premiere,
Resolve, After Effects, etc.

Built on [HyperFrames](https://hyperframes.heygen.com). No video needs to exist —
each template is its own self-contained mini-project under `templates/`.

## Templates

| Name           | Format            | What it is                                   |
| -------------- | ----------------- | -------------------------------------------- |
| `lower_third`  | 1920×1080         | Kicker + big headline + accent bar + subline |
| `stat`         | 1920×1080         | Eyebrow + big number/value + unit + caption  |
| `callout`      | 1920×1080         | Setup line + pink highlight block            |
| `feature_list` | 1920×1080         | Title + 3 staggered bullet rows              |
| `outro_abo`    | 1920×1080         | Subscribe / CTA end card                     |
| `short_hook`   | 1080×1920 (9:16)  | Vertical hook for Shorts / Reels             |

Each lives in `templates/<name>/index.html`, runs at 50 fps, and renders with a
transparent background. `templates/<name>/preset.json` holds its editable values.

## What you can edit

Every template exposes typed variables (declared on the `<html>` element via
`data-composition-variables`): the accent color, all text, and numbers. Two ways:

- **Visually in Studio** — live preview + side-panel controls (below).
- **Via `preset.json`** in each template folder — best for repeatable renders.

Timing (durations, stagger) lives on the timeline — adjust it in Studio, or edit
the `data-duration` / GSAP offsets directly in the template's `index.html`.

## 1. Edit + preview live (Studio)

```bash
cd motiongfx_studio
./preview.sh lower_third        # opens HyperFrames Studio for that template
```

Change variables in the side panel and scrub the timeline to preview. Studio
hot-reloads on file changes. (Under the hood: `npx hyperframes preview templates/<name>`.)

## 2. Render an alpha video

```bash
./render.sh lower_third                      # MOV (ProRes 4444), uses preset.json
./render.sh stat webm                        # transparent WebM
./render.sh callout mov templates/callout/preset.json   # explicit vars file
```

- **MOV** = ProRes 4444 with alpha — best editor compatibility (default).
- **WebM** = VP9 with alpha — smaller, good for web.

Output lands in `renders/<template>_<timestamp>.<ext>` and is revealed in Finder.

If no vars file is passed, the template's own `preset.json` is used automatically.
For one-off overrides without touching any file:

```bash
npx hyperframes render templates/lower_third --format mov \
  --variables '{"headline":"NEUES MODELL","accent":"#00e0a4"}' \
  --output renders/lower_third.mov
```

### Batch (many variants at once)

```bash
npx hyperframes render templates/stat --format mov \
  --batch rows.json --output "renders/{unit}.mov" --strict-variables
```

## 3. Editing values

Open `templates/<name>/preset.json`, change the values, and run `./render.sh <name>`.
Keys must match the template's variable ids — see them in the template's `index.html`
(`data-composition-variables`) or in Studio's variable panel.

## Checks

```bash
npx hyperframes check templates/<name>       # lint + runtime + layout + motion + contrast
```

Run this after editing a template's markup or animation.
