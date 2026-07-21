#!/usr/bin/env bash
# Render a motion-graphics template to a TRANSPARENT (alpha) file.
#
# Usage:
#   ./render.sh <template> [mov|webm] [vars.json]
#
# Examples:
#   ./render.sh lower_third                            # MOV (ProRes 4444), template defaults
#   ./render.sh stat webm                              # transparent WebM, template defaults
#   ./render.sh callout mov templates/callout/preset.json   # MOV with your edited values
#
# If no vars file is given, the template's own preset.json is used when present.
#
# Templates: lower_third | stat | callout | feature_list | outro_abo | short_hook
# Output:    renders/<template>_<timestamp>.<mov|webm>
set -euo pipefail
cd "$(dirname "$0")"

TEMPLATE="${1:?template name, e.g. lower_third}"
FORMAT="${2:-mov}"
VARS="${3:-}"

DIR="templates/$TEMPLATE"
[[ -f "$DIR/index.html" ]] || { echo "No such template: $DIR/index.html" >&2; exit 1; }
[[ "$FORMAT" == "mov" || "$FORMAT" == "webm" ]] || { echo "Format must be mov or webm" >&2; exit 1; }

# Default to the template's own preset.json if the caller didn't pass one.
if [[ -z "$VARS" && -f "$DIR/preset.json" ]]; then VARS="$DIR/preset.json"; fi

STAMP="$(date +%Y%m%d_%H%M%S)"
OUT="renders/${TEMPLATE}_${STAMP}.${FORMAT}"
mkdir -p renders

ARGS=("$DIR" --format "$FORMAT" --output "$OUT" --quality high)
if [[ -n "$VARS" ]]; then
  [[ -f "$VARS" ]] || { echo "No such vars file: $VARS" >&2; exit 1; }
  ARGS+=(--variables-file "$VARS" --strict-variables)
fi

echo "Rendering $DIR -> $OUT (alpha ${FORMAT})..."
npx --yes hyperframes@0.7.58 render "${ARGS[@]}"

echo "OK -> $OUT"
command -v open >/dev/null 2>&1 && open -R "$OUT" || true
