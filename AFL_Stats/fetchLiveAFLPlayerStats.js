// Updated 23/3/2025
// Function to track only live matches and update to Live Stats sheet

function fetchLiveAFLPlayerStats(gameIds) {
  if (!Array.isArray(gameIds) || gameIds.length === 0) {
    Logger.log("⚠️ No live game IDs provided.");
    return;
  }

  const config = getConfig();
  const apiKey = config.apiKey;
  const now = new Date();
  const lastUpdated = Utilities.formatDate(now, Session.getScriptTimeZone(), "yyyy-MM-dd HH:mm:ss");

  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const statsSheet = ss.getSheetByName("Live Stats") || ss.insertSheet("Live Stats");
  const teamSheet = ss.getSheetByName("Teams");
  const playerSheet = ss.getSheetByName("Player Names");

  // Load Team Names
  const teamData = teamSheet.getDataRange().getValues();
  const teamMap = {};
  for (let i = 1; i < teamData.length; i++) {
    teamMap[teamData[i][0].toString()] = teamData[i][2]; // Team ID → Short Name
  }

  // Load Player Names
  const playerData = playerSheet.getDataRange().getValues();
  const playerMap = {};
  for (let i = 1; i < playerData.length; i++) {
    playerMap[playerData[i][0].toString()] = playerData[i][1]; // Player ID → Name
  }

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
  let gamesSkippedDueToQuota = 0;
  let gamesWithNoData = 0;

  gameIds.forEach(gameId => {
    const url = "https://v1.afl.api-sports.io/games/statistics/players?id=" + gameId;
    const options = {
      method: "GET",
      headers: { "x-apisports-key": apiKey }
    };

    try {
      const response = UrlFetchApp.fetch(url, options);

      // ✅ Check API quota before proceeding
      if (!checkApiQuota(response)) return;

      if (!checkApiQuota(response)) {
        gamesSkippedDueToQuota++;
        return;
      }

      const data = JSON.parse(response.getContentText());

      if (!data.response || !data.response.length) {
        Logger.log(`⚠️ No valid stats found for Game ID: ${gameId}`);
        gamesWithNoData++;
        return;
      }

      let rowsAddedForGame = 0;

      data.response.forEach(gameData => {
        if (!gameData?.teams?.length) return;

        gameData.teams.forEach(teamData => {
          const teamId = teamData.team?.id?.toString();
          const teamName = teamMap[teamId] || "Unknown";

          teamData.players.forEach(player => {
            const playerId = player.player?.id?.toString();
            const playerName = playerMap[playerId] || "Unknown";

            const row = [
              gameId,
              playerId,
              playerName,
              teamName,
              player.player.number || "",
              player.kicks || 0,
              player.handballs || 0,
              player.disposals || 0,
              player.marks || 0,
              player.hitouts || 0,
              player.tackles || 0,
              player.goals?.total || 0,
              player.behinds || 0,
              player.clearances || 0,
              player.free_kicks?.for || 0,
              player.free_kicks?.against || 0,
              "LIVE",
              lastUpdated
            ];

            if (row.length !== headers.length) {
              Logger.log(`❌ Row length mismatch for Player ${playerName}: expected ${headers.length}, got ${row.length}`);
            }

            allRows.push(row);
            rowsAddedForGame++;
          });
        });
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
  ⚠️ Games Skipped Due to Quota: ${gamesSkippedDueToQuota}
  ⚠️ Games With No Valid Data: ${gamesWithNoData}`);

  updateDashboard("fetchLiveAFLPlayerStats", "Updated Live Matches");
}
