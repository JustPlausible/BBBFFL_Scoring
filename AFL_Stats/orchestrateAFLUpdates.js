// Updated 23/3/2025
// Scheduling tool for further script execution

function monitorMatchStatus() {
  const config = getConfig();
  const ss = SpreadsheetApp.getActiveSpreadsheet();

  // 🔍 Search Dashboard for fetchAFLGameSchedule last run
  const dashboardSheet = ss.getSheetByName("Dashboard");
  const gameScheduleSheet = ss.getSheetByName("Game Schedule");

  if (!dashboardSheet || !gameScheduleSheet) {
    Logger.log("❌ Required sheets (Dashboard or Game Schedule) not found.");
    return;
  }

  const dashboardData = dashboardSheet.getDataRange().getValues();
  const gameScheduleData = gameScheduleSheet.getDataRange().getValues(); // ✅ Define this early
  const headers = gameScheduleData[0];
  const gameIdIdx = headers.indexOf("Game ID");
  const localTimeIdx = headers.indexOf("Local Time");
  const roundIdx = headers.indexOf("Round");
  const statusIdx = headers.indexOf("Status");
  const dateIdx = headers.indexOf("Date");

  const now = new Date();

  let lastFetch = null;
  for (let i = 1; i < dashboardData.length; i++) {
    const scriptName = dashboardData[i][0];
    if (scriptName === "fetchAFLGameSchedule") {
      lastFetch = new Date(dashboardData[i][1]);
      break;
    }
  }

  if (!lastFetch) {
    Logger.log("⚠️ No previous fetchAFLGameSchedule run time found. Proceeding with fetch.");
    lastFetch = new Date(0); // Fallback: Epoch start to force fetch
  }
  const minutesSinceLastFetch = (now - lastFetch) / 1000 / 60;

  // ✅ Use Game Schedule data to determine match relevance
  const isMatchDay = gameScheduleData.some((row, i) => {
    if (i === 0) return false; // Skip header
    const localTime = new Date(row[localTimeIdx]);
    return localTime.toDateString() === now.toDateString();
  });

  // Determine if a game is within 6 hours
  const isWithin6Hours = gameScheduleData.some((row, i) => {
    if (i === 0) return false; // Skip header
    const localTime = new Date(row[localTimeIdx]);
    const minutesToGame = (localTime - now) / 1000 / 60;
    return minutesToGame >= 0 && minutesToGame <= 360;
  });

  // Define frequency rules
  const needsUpdate = isMatchDay || isWithin6Hours || minutesSinceLastFetch >= 360; // 6 hours

  if (needsUpdate) {
    Logger.log("📅 Match-relevant time. Running fetchAFLGameSchedule...");
    fetchAFLGameSchedule();
    updateDashboard("fetchAFLGameSchedule", "Updated Game Schedule (via monitorMatchStatus)");
  } else {
    Logger.log(`⏳ Skipping fetchAFLGameSchedule. Last update ${Math.round(minutesSinceLastFetch)} mins ago.`);
  }

  const data = gameScheduleSheet.getDataRange().getValues();
  const headers2 = data[0];
  const statusIdx2 = headers2.indexOf("Status");
  const gameIdIdx2 = headers2.indexOf("Game ID");
  const localTimeIdx2 = headers2.indexOf("Local Time");
  const roundIdx2 = headers2.indexOf("Round");
  const dateIdx2 = headers2.indexOf("Date");

  const isLiveStatus = ["Q1", "Q2", "Q3", "Q4", "QT", "HT", "LIVE"];
  const liveGames = [];
  const upcomingGames = [];
  let currentRound = null;

  for (let i = 1; i < data.length; i++) {
    const gameId = data[i][gameIdIdx2];
    const localTime = new Date(data[i][localTimeIdx2]);
    const round = Number(data[i][roundIdx2]);
    const status = data[i][statusIdx2];

    const timeToStart = (localTime - now) / 1000 / 60; // in minutes

    // Update current round if game is today or in progress
    if (!currentRound && ["NS", ...isLiveStatus, "FT"].includes(status)) {
      const gameDay = new Date(data[i][dateIdx2]);
      if (gameDay.toDateString() === now.toDateString()) {
        currentRound = round;
      }
    }

    if (isLiveStatus.includes(status)) {
      liveGames.push(gameId.toString());
    } else if (status === "NS" && timeToStart >= 0 && timeToStart <= 60) {
      upcomingGames.push(gameId.toString());
    }
  }

  Logger.log(`🟢 Found ${liveGames.length} live matches.`);
  Logger.log(`🕒 Upcoming Matches: ${JSON.stringify(upcomingGames)}`);
  Logger.log(`📆 Current Round: ${currentRound}`);

  if (upcomingGames.length > 0) {
    Logger.log("🔁 Running fetchAFLPlayerNames() for upcoming games...");
    fetchAFLPlayerNames();
  }

  if (liveGames.length > 0) {
    Logger.log("📡 Running fetchLiveAFLPlayerStats() for live games...");
    fetchLiveAFLPlayerStats(liveGames);
  }

  if (currentRound) {
    Logger.log("📋 Running fetchAFLStats() for FT or LIVE updates...");
    fetchAFLStats(currentRound, false);
  } else if (liveGames.length === 0 && upcomingGames.length === 0) {
    Logger.log("🟢 No live or imminent matches found. No action taken.");
  }
  updateDashboard("monitorMatchStatus", "Live games and upcoming matches checked");
}