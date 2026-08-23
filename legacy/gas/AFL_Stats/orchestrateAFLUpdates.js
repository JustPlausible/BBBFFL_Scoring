// Updated 15/6/2025
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
  const gameScheduleData = gameScheduleSheet.getDataRange().getValues();
  const headers = gameScheduleData[0];
  const gameIdIdx = headers.indexOf("Game ID");
  const localTimeIdx = headers.indexOf("Local Time");
  const roundIdx = headers.indexOf("Round");
  const statusIdx = headers.indexOf("Status");
  const dateIdx = headers.indexOf("Date");

  const now = new Date();

  // Get time of last fetchAFLGameSchedule
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

  const isLiveStatus = ["Q1", "Q2", "Q3", "Q4", "QT", "HT", "LIVE"];

  const liveGames = [];
  const upcomingGames = [];
  let currentRound = null;

  for (let i = 1; i < gameScheduleData.length; i++) {
    const row = gameScheduleData[i];
    const status = row[statusIdx];
    const gameId = row[gameIdIdx];
    const round = Number(row[roundIdx]);
    const localTimeRaw = row[localTimeIdx];
    const gameDateRaw = row[dateIdx];

    // Filter: skip games with missing or TBC status/times
    if (!localTimeRaw || status === "TBC" || localTimeRaw === "") continue;

    const localTime = new Date(localTimeRaw);
    const gameDate = new Date(gameDateRaw);
    const timeToGame = (localTime - now) / 1000 / 60;

    // Detect current round by today's match
    if (!currentRound && gameDate.toDateString() === now.toDateString()) {
      currentRound = round;
    }

    if (isLiveStatus.includes(status)) {
      liveGames.push(gameId.toString());
    } else if (status === "NS" && timeToGame >= 0 && timeToGame <= 60) {
      upcomingGames.push(gameId.toString());
    }
  }

  Logger.log(`🟢 Found ${liveGames.length} live matches.`);
  Logger.log(`🕒 Upcoming Matches: ${JSON.stringify(upcomingGames)}`);
  Logger.log(`📆 Current Round: ${currentRound}`);

  if (minutesSinceLastFetch >= 360 || liveGames.length > 0 || upcomingGames.length > 0) {
    Logger.log("📅 Match-relevant time. Running fetchAFLGameSchedule...");
    fetchAFLGameSchedule();
    updateDashboard("fetchAFLGameSchedule", "Updated Game Schedule (via monitorMatchStatus)");
  } else {
    Logger.log(`⏳ Skipping fetchAFLGameSchedule. Last update ${Math.round(minutesSinceLastFetch)} mins ago.`);
  }

  if (upcomingGames.length > 0) {
    Logger.log("🔁 Running fetchAFLPlayerNames() for upcoming games...");
    fetchAFLPlayerNames();
  }

  if (liveGames.length > 0) {
    Logger.log("📡 Running fetchLiveAFLPlayerStats() for live games...");
    fetchLiveAFLPlayerStats(liveGames);
  }

  if (currentRound && liveGames.length === 0) {
    Logger.log(`📋 No live games. Running fetchAFLStats() for Round ${currentRound}...`);
    fetchAFLStats(currentRound, false);
  } else if (liveGames.length > 0) {
    Logger.log("⏳ Skipping fetchAFLStats() — live matches still running.");
  }

  updateDashboard("monitorMatchStatus", "Live games and upcoming matches checked");
}