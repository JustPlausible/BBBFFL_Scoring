#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECTS=("AFL_Stats" "BBBFFL_Results" "BBBFFL_Weekly_Teams")

echo "📥 Pulling all GAS subprojects..."
for project in "${PROJECTS[@]}"; do
  echo "🔄 Pulling: $project"
  (cd "$SCRIPT_DIR/$project" && clasp pull)
done
echo "✅ All projects pulled!"
