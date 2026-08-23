function onOpen() {
  const ui = SpreadsheetApp.getUi();
  ui.createMenu("Scorer Tools")
    .addItem("💡 Suggest Current Round", "suggestCurrentRoundScorerReview")
    .addItem("📋 Consolidate Weekly Teams", "consolidateWeeklyTeams")
    .addToUi();
}