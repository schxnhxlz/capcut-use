#!/usr/bin/env bash
# Open a template in HyperFrames Studio to edit its variables and preview live.
#
# Usage:
#   ./preview.sh <template> [--port 3002]
#
# Templates: lower_third | stat | callout | feature_list | outro_abo | short_hook
set -euo pipefail
cd "$(dirname "$0")"

TEMPLATE="${1:?template name, e.g. lower_third}"
DIR="templates/$TEMPLATE"
[[ -f "$DIR/index.html" ]] || { echo "No such template: $DIR/index.html" >&2; exit 1; }

exec npx --yes hyperframes@0.7.58 preview "$DIR" "${@:2}"
