// Updated for custom AFL API – 15/6/2025
// Fetches final stats only for completed (FT) matches in the given round

function fetchAFLStats(roundNumber, forceUpdate = false) {
  const config = getConfig();
  const apiKey = config.afl_apiKey;
  const ss = SpreadsheetApp.getActiveSpreadsheet();

  const gameSheet = ss.getSheetByName('Game Schedule');
  const playerSheet = ss.getSheetByName('Player Names');
  const liveSheet = ss.getSheetByName('Live Stats');
  const roundSheetName = "Round " + roundNumber;
  let roundSheet = ss.getSheetByName(roundSheetName);

  const expectedHeaders = [
    "Game ID", "Player ID", "Player Name", "AFL Team", "Jumper No.",
    "Kicks", "Handballs", "Disposals", "Marks", "Hitouts", "Tackles",
    "Goals", "Behinds", "Clearances", "Free Kicks For", "Free Kicks Against",
    "Status", "FT Timestamp", "Last Updated"
  ];

  if (!roundSheet) {
    roundSheet = ss.insertSheet(roundSheetName);
  }

  const existingHeaders = roundSheet.getRange(1, 1, 1, expectedHeaders.length).getValues()[0];
  const isHeaderMismatch = existingHeaders.some((v, i) => v !== expectedHeaders[i]);
  if (isHeaderMismatch) {
    roundSheet.clear();
    roundSheet.getRange(1, 1, 1, expectedHeaders.length).setValues([expectedHeaders]);
  }

  const now = new Date();
  const lastUpdated = Utilities.formatDate(now, Session.getScriptTimeZone(), "yyyy-MM-dd HH:mm:ss");

  const roundExisting = roundSheet.getDataRange().getValues();
  const roundMap = new Set();
  for (let i = 1; i < roundExisting.length; i++) {
    roundMap.add(roundExisting[i][0] + "_" + roundExisting[i][1]);
  }

  const gameData = gameSheet.getDataRange().getValues();
  const gameIDs = [];
  const gameStatuses = {};
  const ftTimestamps = {};

  for (let i = 1; i < gameData.length; i++) {
    const [gameId, , localTime, round, , , , status] = gameData[i];
    if (Number(round) !== roundNumber || status !== "FT") continue;

    const start = new Date(localTime);
    const ftEstimate = new Date(start.getTime() + 2.5 * 3600000); // FT assumed 2.5h after start
    const timeSinceFT = (now - ftEstimate) / (1000 * 60);
    const alreadyStored = [...roundMap].some(k => k.startsWith(gameId + "_"));

    if (status === "FT" && (forceUpdate || timeSinceFT <= 60)) {
      gameIDs.push(gameId);
      gameStatuses[gameId] = "FT";
      ftTimestamps[gameId] = Utilities.formatDate(ftEstimate, Session.getScriptTimeZone(), "yyyy-MM-dd HH:mm:ss");
    }
  }

  if (gameIDs.length === 0) {
    Logger.log("❌ No FT games to update.");
    return;
  }

  if (forceUpdate) clearRoundSheetFast(roundSheet);

  const roundBatch = [];
  let apiCalls = 0;

  gameIDs.forEach(gameId => {
    const url = `https://afl-api.thehardinghams.net/api/player-stats?match_id=${gameId}`;
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
      if (!Array.isArray(data)) {
        Logger.log(`⚠️ Unexpected format for Game ID ${gameId}`);
        return;
      }

      data.forEach(stat => {
        const key = `${stat.match_id}_${stat.afl_id}`;
        if (!roundMap.has(key)) {
          roundBatch.push([
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
            stat.free_kicks_for || 0,
            stat.free_kicks_against || 0,
            stat.status || "FT",
            ftTimestamps[gameId] || "",
            lastUpdated
          ]);
        }
      });

      Logger.log(`✅ Stats processed for Game ID: ${gameId}`);
      apiCalls++;

    } catch (e) {
      Logger.log(`❌ Error fetching stats for Game ID ${gameId}: ${e}`);
    }
  });

  if (roundBatch.length > 0) {
    roundSheet.getRange(roundSheet.getLastRow() + 1, 1, roundBatch.length, roundBatch[0].length).setValues(roundBatch);
    Logger.log(`📥 ${roundBatch.length} FT stats written to ${roundSheetName}`);

    // 🔁 Clean from Live Stats if recently handled
    const liveData = liveSheet.getDataRange().getValues();
    const headers = liveData[0];
    const cleanedRows = [headers];
    let removedCount = 0;

    for (let i = 1; i < liveData.length; i++) {
      const row = liveData[i];
      const gameId = row[0];
      const playerId = row[1];
      const key = `${gameId}_${playerId}`;
      const ftTimestamp = ftTimestamps[gameId];
      const isFTHandled = gameStatuses[gameId] === "FT";
      const isRecent = ftTimestamp && ((now - new Date(ftTimestamp)) / 1000 / 60 <= 60);

      if (isFTHandled && isRecent) {
        removedCount++;
      } else {
        cleanedRows.push(row);
      }
    }

    if (removedCount > 0) {
      liveSheet.clearContents();
      liveSheet.getRange(1, 1, cleanedRows.length, headers.length).setValues(cleanedRows);
      Logger.log(`🧹 Removed ${removedCount} FT rows from Live Stats.`);
    }
  }

  Logger.log(`📊 API Calls Made: ${apiCalls}`);
  updateDashboard("fetchAFLStats", "Updated Round " + roundNumber);
}

function clearRoundSheetFast(sheet) {
  const lastRow = sheet.getLastRow();
  if (lastRow > 1) {
    sheet.getRange(2, 1, lastRow - 1, sheet.getLastColumn()).clearContent();
    Logger.log(`🧹 Cleared existing FT data from ${sheet.getName()}`);
  }
}
