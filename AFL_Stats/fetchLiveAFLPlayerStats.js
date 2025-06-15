function fetchLiveAFLPlayerStats(gameIds) {
  if (!Array.isArray(gameIds) || gameIds.length === 0) {
    Logger.log("⚠️ No live game IDs provided.");
    return;
  }

  const config = getConfig();
  const apiKey = config.afl_apiKey;
  const now = new Date();
  const lastUpdated = Utilities.formatDate(now, Session.getScriptTimeZone(), "yyyy-MM-dd HH:mm:ss");

  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const statsSheet = ss.getSheetByName("Live Stats") || ss.insertSheet("Live Stats");

  // Prepare headers
  const headers = [
    "Game ID", "Player ID", "Player Name", "AFL Team", "Jumper No.",
    "Kicks", "Handballs", "Disposals", "Marks", "Hitouts", "Tackles",
    "Goals", "Behinds", "Clearances", "Free Kicks For", "Free Kicks Against", "Status", "Last Updated"
  ];
  statsSheet.clear();
  statsSheet.appendRow(headers);

  let allRows = [];

  let totalGames = gameIds.length;
  let gamesUpdated = 0;
  let gamesWithNoData = 0;

  gameIds.forEach(gameId => {
    const url = "https://afl-api.thehardinghams.net/api/player-stats?match_id=" + gameId;
    const options = {
      method: "GET",
      headers: { "x-api-key": apiKey },
      muteHttpExceptions: true
    };

    try {
      const response = UrlFetchApp.fetch(url, options);
      const code = response.getResponseCode();
      if (code !== 200) {
        Logger.log(`❌ Failed to fetch stats for match ${gameId}: HTTP ${code}`);
        return;
      }

      const data = JSON.parse(response.getContentText());
      if (!Array.isArray(data) || data.length === 0) {
        Logger.log(`⚠️ No valid stats found for Game ID: ${gameId}`);
        gamesWithNoData++;
        return;
      }

      let rowsAddedForGame = 0;

      data.forEach(stat => {
        const row = [
          stat.match_id,
          stat.afl_id,
          stat.player_name,
          stat.team_code,
          stat.jumper_number || "",
          stat.kicks || 0,
          stat.handballs || 0,
          stat.disposals || 0,
          stat.marks || 0,
          stat.hitouts || 0,
          stat.tackles || 0,
          stat.goals || 0,
          stat.behinds || 0,
          stat.clearances || 0,
          "", "", // Free Kicks For/Against (not yet included)
          stat.status || "LIVE",
          lastUpdated
        ];

        allRows.push(row);
        rowsAddedForGame++;
      });

      if (rowsAddedForGame > 0) {
        gamesUpdated++;
        Logger.log(`✅ Stats pulled for Game ID: ${gameId}`);
      } else {
        gamesWithNoData++;
      }

    } catch (e) {
      Logger.log(`❌ Error fetching stats for Game ID ${gameId}: ${e.toString()}`);
    }
  });

  if (allRows.length > 0) {
    statsSheet.getRange(2, 1, allRows.length, headers.length).setValues(allRows);
    Logger.log(`✅ Wrote ${allRows.length} rows to Live Stats.`);
  } else {
    Logger.log("⚠️ No player rows written to Live Stats.");
  }

  Logger.log(`📊 Live Stats Fetch Summary:
  🏉 Total Games Attempted: ${totalGames}
  ✅ Games Successfully Updated: ${gamesUpdated}
  ⚠️ Games With No Valid Data: ${gamesWithNoData}`);

  updateDashboard("fetchLiveAFLPlayerStats", "Updated Live Matches");
}
