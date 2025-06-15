#!/bin/bash
PROJECTS=("AFL_Stats" "BBBFFL_Results" "BBBFFL_Weekly_Teams")

echo "📥 Pulling all GAS subprojects..."
for project in "${PROJECTS[@]}"; do
  echo "🔄 Pulling: $project"
  (cd "$project" && clasp pull)
done
echo "✅ All projects pulled!"
