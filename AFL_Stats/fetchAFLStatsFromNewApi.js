function fetchAFLStats(roundLabel, forceUpdate = false) {
  const config = getConfig();
  const apiKey = config.afl_apiKey;

  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const scheduleSheet = ss.getSheetByName("Game Schedule");
  const liveSheet = ss.getSheetByName("Live Stats");

  const headers = [
    "Game ID", "Player ID", "Player Name", "AFL Team", "Jumper No.",
    "Kicks", "Handballs", "Disposals", "Marks", "Hitouts", "Tackles",
    "Goals", "Behinds", "Clearances", "Free Kicks For", "Free Kicks Against",
    "Status", "FT Timestamp", "Last Updated"
  ];

  // 📌 Step 1: Resolve Round Label → Round ID
  const scheduleData = scheduleSheet.getDataRange().getValues();
  let roundId = null;
  const ftTimestamps = {};
  const gameStatusMap = {};

  for (let i = 1; i < scheduleData.length; i++) {
    const row = scheduleData[i];
    const label = String(row[3]);
    const rId = row[4];
    const gameId = row[0];
    const localTime = new Date(row[2]);
    const status = row[7];

    if (label === String(roundLabel)) {
      roundId = rId;
      if (status === "FT" || status === "COMPLETED") {
        const ftEstimate = new Date(localTime.getTime() + 2.5 * 3600000);
        ftTimestamps[gameId] = Utilities.formatDate(ftEstimate, Session.getScriptTimeZone(), "yyyy-MM-dd HH:mm:ss");
        gameStatusMap[gameId] = "FT";
      }
    }
  }

  if (!roundId) {
    Logger.log(`❌ No Round ID found for Round Label: ${roundLabel}`);
    return;
  }

  const sheetName = `Round ${roundLabel}`;
  let sheet = ss.getSheetByName(sheetName);
  if (!sheet) sheet = ss.insertSheet(sheetName);

  const now = new Date();
  const lastUpdated = Utilities.formatDate(now, Session.getScriptTimeZone(), "yyyy-MM-dd HH:mm:ss");

  // 📌 Step 2: Prepare existing stat keys
  const existingData = sheet.getDataRange().getValues();
  const seenKeys = new Set();
  for (let i = 1; i < existingData.length; i++) {
    seenKeys.add(`${existingData[i][0]}_${existingData[i][1]}`);
  }

  // 📌 Step 3: Clear sheet if forceUpdate
  if (forceUpdate) {
    sheet.clear();
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
    seenKeys.clear(); // All will be new
  }

  const url = `https://afl-api.thehardinghams.net/api/player-stats?round_id=${roundId}`;
  const options = {
    method: "GET",
    headers: { "x-api-key": apiKey },
    muteHttpExceptions: true
  };

  try {
    const response = UrlFetchApp.fetch(url, options);
    const code = response.getResponseCode();
    if (code !== 200) {
      Logger.log(`❌ Failed to fetch player stats: ${code}`);
      return;
    }

    const data = JSON.parse(response.getContentText());

    const rows = [];

    data.forEach(stat => {
      const key = `${stat.match_id}_${stat.afl_id}`;
      if (seenKeys.has(key) && !forceUpdate) return;

      rows.push([
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
        "", "", // Free Kicks For/Against
        (stat.status === "COMPLETED" ? "FT" : stat.status || "LIVE"),
        ftTimestamps[stat.match_id] || "",
        lastUpdated
      ]);
    });

    if (rows.length > 0) {
      // Sort before writing
      rows.sort((a, b) => {
        if (a[0] !== b[0]) return a[0] - b[0];
        if (a[3] !== b[3]) return a[3].localeCompare(b[3]);
        return a[1] - b[1];
      });

      sheet.getRange(sheet.getLastRow() + 1, 1, rows.length, headers.length).setValues(rows);
      Logger.log(`✅ Loaded ${rows.length} new stats to '${sheetName}'`);
    } else {
      Logger.log("⚠️ No new stats written (all previously stored?)");
    }

    // 🔁 Clean up matching FT entries from Live Stats (only recent)
    const liveData = liveSheet.getDataRange().getValues();
    const liveHeaders = liveData[0];
    const cleanedLive = [liveHeaders];
    let removedCount = 0;

    for (let i = 1; i < liveData.length; i++) {
      const row = liveData[i];
      const gameId = row[0];
      const playerId = row[1];
      const key = `${gameId}_${playerId}`;
      const ftTime = ftTimestamps[gameId];
      const isFT = gameStatusMap[gameId] === "FT";
      const isRecent = ftTime && ((now - new Date(ftTime)) / 1000 / 60 <= 60);

      if (isFT && isRecent && seenKeys.has(key)) {
        removedCount++;
      } else {
        cleanedLive.push(row);
      }
    }

    if (removedCount > 0) {
      liveSheet.clearContents();
      liveSheet.getRange(1, 1, cleanedLive.length, liveHeaders.length).setValues(cleanedLive);
      Logger.log(`🧹 Removed ${removedCount} FT rows from Live Stats.`);
    }

  } catch (err) {
    Logger.log(`❌ Exception during fetch: ${err}`);
  }

  updateDashboard("fetchAFLStats", `Updated Round ${roundLabel}`);
}
