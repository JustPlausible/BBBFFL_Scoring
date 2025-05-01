function onOpen() {
  const ui = SpreadsheetApp.getUi();
  ui.createMenu("Scorer Tools")
    .addItem("💡 Suggest Current Round", "suggestCurrentRoundScorerReview")
    .addItem("📺 Update Live Round", "runLiveBBBFFLForCurrentRound")
    .addItem("📋 Run Review for Suggested Round", "runReviewForSuggestedRound")
    .addItem("✅ Process Approved Overrides", "processApprovedOverrides")
    .addToUi();
  
  // Automatically suggest round when Scorer Review opens
  suggestCurrentRoundScorerReview();
}