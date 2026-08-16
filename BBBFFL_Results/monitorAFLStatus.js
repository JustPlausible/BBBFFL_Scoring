// Updated 29/3/2025
// Monitors AFL sheets to determine whether to update the Live Results

function monitorAFLStatus() {
  const config = getConfig();

  if (!config.SEASON_MONITORING_ENABLED) {
    logAction("monitorAFLStatus: Season monitoring disabled in config, exiting.", LOG_LEVELS.INFO);
    return;
  }
  
  const aflStatsSS = SpreadsheetApp.openById(config.aflStatsSheetId);
  const aflDashboard = aflStatsSS.getSheetByName("Dashboard");
  const liveAFLSheet = aflStatsSS.getSheetByName("Live Stats");
  const aflScheduleSheet = aflStatsSS.getSheetByName("Game Schedule");

  const bbbfflResultsSS = SpreadsheetApp.openById(config.bbbfflResultsSheetId);
  const bbbDashboard = bbbfflResultsSS.getSheetByName("Dashboard");

  // --- Get last run timestamps ---
  const aflLastRuns = getScriptLastRunMap(aflDashboard);
  const bbbLastRuns = getScriptLastRunMap(bbbDashboard);

  // --- Determine current round from Live Stats Game ID ---
  const liveData = liveAFLSheet.getDataRange().getValues();
  const firstGameId = liveData[1]?.[0]; // Assume Game ID is in col 0
  const scheduleData = aflScheduleSheet.getDataRange().getValues();
  const roundFromLive = scheduleData.find(r => r[0] == firstGameId)?.[3]; // Round is in col 3

  // --- Determine round from last fetchAFLStats note ---
  const roundFromStats = getRoundFromAFLStatsDashboard(aflDashboard);

  // --- Use detected round, fallback to roundFromStats if needed ---
  const currentRound = roundFromLive || roundFromStats;
  if (!currentRound) {
    logAction("⚠️ Could not determine current round from Live Stats or Dashboard", LOG_LEVELS.WARN);
    return;
  }

  // --- Check last script run times ---
  const lastLiveRun = new Date(aflLastRuns["fetchLiveAFLPlayerStats"] || 0);
  const lastRoundRun = new Date(aflLastRuns[`fetchAFLStats`] || 0); // not round specific
  const lastBBBFFLLive = new Date(bbbLastRuns["generateLiveBBBFFLMatches"] || 0);
  const latestAFLRun = new Date(Math.max(lastLiveRun, lastRoundRun));

  // --- Trigger BBBFFL live results update if needed ---
  if (latestAFLRun > lastBBBFFLLive) {
    generateLiveBBBFFLMatches(currentRound);
    updateResultsDashboard("generateLiveBBBFFLMatches", `Triggered via monitorAFLStatus (Round ${currentRound})`);
  }

  updateResultsDashboard("monitorAFLStatus", `Compared AFL updates to BBBFFL Live Round ${currentRound}`);
}

// Helper Scripts

function getScriptLastRunMap(sheet) {
  const data = sheet.getDataRange().getValues();
  const map = {};
  for (let i = 1; i < data.length; i++) {
    const script = data[i][0];
    const time = data[i][1];
    if (script && time) {
      map[script] = new Date(time);
    }
  }
  return map;
}

function getRoundFromAFLStatsDashboard(sheet) {
  const data = sheet.getDataRange().getValues();
  for (let i = data.length - 1; i >= 1; i--) {
    const note = data[i][2];
    if (typeof note === "string" && note.includes("Updated Round")) {
      const match = note.match(/Updated Round (\d+)/);
      if (match) {
        return parseInt(match[1], 10);
      }
    }
  }
  return null;
}
