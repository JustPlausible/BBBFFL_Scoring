function scheduleMonitorIfLive() {
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("Game Schedule");
  const data = sheet.getDataRange().getValues();
  const isLiveStatus = ["Q1", "Q2", "Q3", "Q4", "QT", "HT", "LIVE"];
  let liveGamesExist = false;

  for (let i = 1; i < data.length; i++) {
    const status = data[i][7];
    if (isLiveStatus.includes(status)) {
      liveGamesExist = true;
      break;
    }
  }

  if (liveGamesExist) {
    Logger.log("📡 Live matches found. Running monitorMatchStatus() and rescheduling...");

    // Run your main function
    monitorMatchStatus();

    // Remove old self-triggers
    ScriptApp.getProjectTriggers().forEach(trigger => {
      if (trigger.getHandlerFunction() === "scheduleMonitorIfLive") {
        ScriptApp.deleteTrigger(trigger);
      }
    });

    // Schedule it again in 5 minutes
    ScriptApp.newTrigger("scheduleMonitorIfLive")
      .timeBased()
      .after(5 * 60 * 1000) // 5 minutes
      .create();
  } else {
    Logger.log("🛑 No live matches. No further triggers scheduled.");
  }
}
