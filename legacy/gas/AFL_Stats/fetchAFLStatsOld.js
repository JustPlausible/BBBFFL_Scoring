// Updated 1/5/2025
// Function to track only all matches for a given round and shift Live Stats sheet to final

function fetchAFLStatsOld(roundNumber, forceUpdate = false) {
  const config = getConfig();
  const apiKey = config.apiKey;
  const ss = SpreadsheetApp.getActiveSpreadsheet();

  const gameSheet = ss.getSheetByName('Game Schedule');
  const playerSheet = ss.getSheetByName('Player Names');
  const liveSheet = ss.getSheetByName('Live Stats');
  const roundSheetName = `Round ${roundNumber} old`;
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
const isHeaderMissing = existingHeaders.some((v, i) => v !== expectedHeaders[i]);

if (isHeaderMissing) {
  roundSheet.clear(); 
  roundSheet.getRange(1, 1, 1, expectedHeaders.length).setValues([expectedHeaders]);
}

  const now = new Date();
  const lastUpdated = Utilities.formatDate(now, Session.getScriptTimeZone(), "yyyy-MM-dd HH:mm:ss");

  // Build player lookup
  const playerLookup = {};
  const playerData = playerSheet.getDataRange().getValues();
  for (let i = 1; i < playerData.length; i++) {
    if (playerData[i][0]) playerLookup[playerData[i][0]] = playerData[i][1];
  }

  // Check existing Round data
  const roundExisting = roundSheet.getDataRange().getValues();
  const roundMap = new Set();
  for (let i = 1; i < roundExisting.length; i++) {
    roundMap.add(roundExisting[i][0] + "_" + roundExisting[i][1]);
  }

  // Only check Game Schedule for FT games in the round
  const gameData = gameSheet.getDataRange().getValues();
  const gameIDs = [];
  const gameStatuses = {};
  const ftTimestamps = {};

  for (let i = 1; i < gameData.length; i++) {
    const [gameId, , localTime, round, , , , status] = gameData[i];
    if (Number(round) !== roundNumber || status !== "FT") continue;

    const start = new Date(localTime);
    const ftEstimate = new Date(start.getTime() + 2.5 * 3600000); // +2.5h
    const timeSinceFT = (now - ftEstimate) / (1000 * 60);
    const alreadyStored = [...roundMap].some(k => k.startsWith(gameId + "_"));

    //if (forceUpdate || timeSinceFT <= 60 || !alreadyStored) {
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

  // Fetch and process FT stats only
  const roundBatch = [];
  let apiCalls = 0;

  gameIDs.forEach(gameId => {
    const url = `https://v1.afl.api-sports.io/games/statistics/players?id=${gameId}`;
    const options = { method: "GET", headers: { "x-apisports-key": apiKey } };

    try {
      const response = UrlFetchApp.fetch(url, options);
      if (!checkApiQuota(response)) return;
      apiCalls++;

      const stats = JSON.parse(response.getContentText());
      const teams = stats.response?.[0]?.teams;
      if (!teams) return;

      teams.forEach(team => {
        const teamId = team.team.id;
        const teamName = getTeamShortName(teamId);

        team.players.forEach(player => {
          const playerId = player.player.id;
          const playerName = playerLookup[playerId] || player.player.name || "Unknown";
          const key = `${gameId}_${playerId}`;

          //const row = [
          //  gameId, playerId, playerName, teamName, player.player.number || "",
          //  player.kicks || 0, player.handballs || 0, player.disposals || 0, player.marks || 0,
          //  player.hitouts || 0, player.tackles || 0, player.goals?.total || 0, player.behinds || 0,
          //  player.clearances || 0, player.free_kicks?.for || 0, player.free_kicks?.against || 0,
          //  "FT", ftTimestamps[gameId] || "", lastUpdated
          //];

          //roundBatch.push(row);

          if (!roundMap.has(key)) {
            roundBatch.push([
              gameId, playerId, playerName, teamName, player.player.number || "",
              player.kicks || 0, player.handballs || 0, player.disposals || 0, player.marks || 0,
              player.hitouts || 0, player.tackles || 0, player.goals?.total || 0, player.behinds || 0,
              player.clearances || 0, player.free_kicks?.for || 0, player.free_kicks?.against || 0,
              "FT", ftTimestamps[gameId] || "", lastUpdated
            ]);
          }
        });
      });

      Logger.log(`✅ Stats processed for Game ID: ${gameId}`);
    } catch (e) {
      Logger.log(`❌ Error on Game ID ${gameId}: ${e}`);
    }
  });

  // ✅ After writing FT stats, clean them from Live Stats if they are recent (<= 60 mins old)
  if (roundBatch.length > 0) {
    roundSheet.getRange(roundSheet.getLastRow() + 1, 1, roundBatch.length, roundBatch[0].length).setValues(roundBatch);
    Logger.log(`📥 ${roundBatch.length} FT stats written to ${roundSheetName}`);

    // 🔁 Clean up matching FT entries from Live Stats (only those we just handled)
    const liveData = liveSheet.getDataRange().getValues();
    const headers = liveData[0];
    const cleanedRows = [headers]; // Preserve header
    let removedCount = 0;

    for (let i = 1; i < liveData.length; i++) {
      const row = liveData[i];
      const gameId = row[0];
      const playerId = row[1];
      const key = `${gameId}_${playerId}`;

      // Only remove if we just wrote this FT game AND it's recent
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
