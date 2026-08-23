#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECTS=("AFL_Stats" "BBBFFL_Results" "BBBFFL_Weekly_Teams")

echo "🔁 Pushing all GAS subprojects..."
for project in "${PROJECTS[@]}"; do
  echo "🚀 Pushing: $project"
  (cd "$SCRIPT_DIR/$project" && clasp push)
done
echo "✅ All projects pushed!"
