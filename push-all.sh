#!/bin/bash
PROJECTS=("AFL_Stats" "BBBFFL_Results" "BBBFFL_Weekly_Teams")

echo "🔁 Pushing all GAS subprojects..."
for project in "${PROJECTS[@]}"; do
  echo "🚀 Pushing: $project"
  (cd "$project" && clasp push)
done
echo "✅ All projects pushed!"
