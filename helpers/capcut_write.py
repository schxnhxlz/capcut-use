"""Write a CapCut Desktop project from a cutplan (M1: single-timeline).

Clones a real CapCut project template and rewrites only the parts that change
(canvas, ids, base video track segments, shared media, project/registry
sidecars). Nothing is rendered — CapCut does the final render.

Usage:
    python helpers/capcut_write.py <cutplan.json> \
        --templates ./capcut_templates \
        --drafts-root auto \
        --project-name "YT 2026-07-07 agent-video" \
        [--dry-run] [--media-mode reference|copy] [--register]

See helpers/capcut/FORMAT.md for the format spec.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running as a script (python helpers/capcut_write.py ...) or as a module.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from capcut.paths import detect_drafts_root, sanity_check_root  # noqa: E402
from capcut.validate import ValidationError  # noqa: E402
from capcut.writer import generate  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Write a CapCut project from a cutplan (M1)")
    ap.add_argument("cutplan", type=Path, help="Path to cutplan.json")
    ap.add_argument(
        "--templates", type=Path, default=Path("capcut_templates"),
        help="Template store dir (expects single_timeline/ and longform_pip/ inside). Default: ./capcut_templates",
    )
    ap.add_argument(
        "--template-name", default=None,
        help="Template subfolder to clone. Default: derived from cutplan main.preset "
             "(longform-pip -> longform_pip, else single_timeline).",
    )
    ap.add_argument(
        "--drafts-root", default="auto",
        help="CapCut drafts root, or 'auto' to detect (standard vs sandboxed container).",
    )
    ap.add_argument("--project-name", required=True, help="Name of the new CapCut project folder")
    ap.add_argument("--media-mode", choices=["reference", "copy"], default="reference",
                    help="reference media in place (default) or copy into the project folder")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print a validation report without writing anything")
    ap.add_argument("--register", action="store_true",
                    help="Also add an entry to root_meta_info.json so CapCut lists the project")
    args = ap.parse_args()

    if not args.cutplan.exists():
        sys.exit(f"cutplan not found: {args.cutplan}")
    cutplan = json.loads(args.cutplan.read_text(encoding="utf-8"))

    preset = (cutplan.get("main", {}).get("preset") or "single").lower()
    if args.template_name:
        template_name = args.template_name
    elif preset in ("longform-pip", "longform_pip"):
        template_name = "longform_pip"
    else:
        template_name = "single_timeline"

    template_dir = (args.templates / template_name).resolve()
    if not (template_dir / "draft_info.json").exists():
        sys.exit(
            f"template '{template_name}' not found at {template_dir}\n"
            "See capcut_templates/README.md for how to ingest one."
        )

    # Short-switch donor is needed only when the cutplan declares shorts.
    short_template_dir = (args.templates / "short_switch").resolve()
    if cutplan.get("shorts") and not (short_template_dir / "draft_info.json").exists():
        sys.exit(
            f"cutplan declares shorts but the short-switch template is missing at "
            f"{short_template_dir}\nSee capcut_templates/README.md."
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
            register=args.register,
            short_template_dir=short_template_dir,
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


if __name__ == "__main__":
    main()
