// Updated 29/3/2025
// Fetch the afl stats for each team and store them in the Master Results sheet

function fetchBBBFFLResults(roundNumber) {
  var config = getConfig();
  var weeklyTeamsSheet = SpreadsheetApp.openById(config.bbbfflWeeklyTeamsSheetId).getSheetByName("Master Weekly Teams");
  var aflStatsSheet = SpreadsheetApp.openById(config.aflStatsSheetId);
  var resultsSheet = SpreadsheetApp.openById(config.bbbfflResultsSheetId).getSheetByName("Master Results");
  var overridesSheet = SpreadsheetApp.openById(config.bbbfflResultsSheetId).getSheetByName("Local Overrides");
  var byeReplaceSheet = SpreadsheetApp.openById(config.bbbfflResultsSheetId).getSheetByName("Bye Replace");
  var playerSheet = aflStatsSheet.getSheetByName("Player Names");

  Logger.log("🔄 Fetching BBBFFL Results for Round: " + roundNumber);

  // **Load 'Bye Replace' Table**
  var byeTeamsForRound = {};
  if (byeReplaceSheet) {
    var byeData = byeReplaceSheet.getDataRange().getValues();
    for (var i = 1; i < byeData.length; i++) {
      var insertRound = byeData[i][2]; 
      var byeRound = byeData[i][0]; 
      var aflTeam = byeData[i][1]; 
      if (insertRound == roundNumber) {
          byeTeamsForRound[aflTeam] = byeRound;
      }
    }
  }
  Logger.log(`✅ Bye Round Mappings: ${JSON.stringify(byeTeamsForRound)}`);

  // **Load Local Overrides**
  var positionOverrides = {};  // { "BBBFFL Team" : { "Position": {playerId, stats, name, replacedPosition} } }
  var overridesData = overridesSheet ? overridesSheet.getDataRange().getValues() : [];
  var localOverrides = {};
  for (var i = 1; i < overridesData.length; i++) {
    var overrideRound = overridesData[i][0];
    if (overrideRound != roundNumber) continue;

    var bbbfflTeam = overridesData[i][1]; // Column B = BBBFFL Team
    var playerId = overridesData[i][2];
    var playerName = overridesData[i][3];
    var status = overridesData[i][10];

    localOverrides[playerId] = {
      name: playerName,
      goals: overridesData[i][4],
      behinds: overridesData[i][5],
      disposals: overridesData[i][6],
      tackles: overridesData[i][7],
      hitouts: overridesData[i][8],
      marks: overridesData[i][9]
    };
    // ✅ If this is an Interchange Replacement (e.g. "Midfield3"), store it team-specifically
    const cleanStatus = (status || "").trim();
    if (["Forward1", "Forward2", "Forward3", "Midfield1", "Midfield2", "Midfield3", "Ruck", "Tackler"].includes(cleanStatus)) {
      if (!positionOverrides[bbbfflTeam]) positionOverrides[bbbfflTeam] = {};
      positionOverrides[bbbfflTeam][cleanStatus] = {
        playerId: playerId,
        stats: localOverrides[playerId],
        name: playerName,
        replacedPosition: cleanStatus
      };
      Logger.log(`🔁 ${cleanStatus} → ${playerId} (${playerName}) replacing for team ${bbbfflTeam}`);
    }
  }
  Logger.log(`✅ Loaded ${Object.keys(localOverrides).length} Local Overrides for Round ${roundNumber}`);
  Logger.log("🧩 Position Overrides loaded:");
  //Object.entries(positionOverrides).forEach(([pos, override]) => {
  //  Logger.log(`🔁 ${pos} → ${override.playerId} (${override.name}) replacing ${override.replacedPosition}`);
  //});
  Object.entries(positionOverrides).forEach(([team, overrides]) => {
    Object.entries(overrides).forEach(([position, override]) => {
      Logger.log(`🔁 ${team} → ${override.playerId} (${override.name}) replacing ${override.replacedPosition}`);
    });
  });

  // **Load Player Names & AFL Teams**
  var playerData = playerSheet.getDataRange().getValues();
  var playerAFLTeamLookup = {};
  var playerNameLookup = {};
  for (var i = 1; i < playerData.length; i++) {
    var playerId = playerData[i][0]; 
    var aflTeam = playerData[i][4];
    var fullName = playerData[i][1]; // Assuming Full Name is in Column B 
    if (playerId) {
      playerAFLTeamLookup[playerId] = aflTeam;
      playerNameLookup[playerId] = fullName;
    }
  }

  // **Load AFL Player Stats**
  var roundStatsSheet = aflStatsSheet.getSheetByName("Round " + roundNumber);
  var roundStatsData = roundStatsSheet ? roundStatsSheet.getDataRange().getValues() : [];
  var statsLookup = buildStatsLookup(roundStatsData);

  // **Load Bye Round Stats**
  var byeStatsLookup = {};
  Object.values(byeTeamsForRound).forEach(byeRound => {
    var byeStatsSheet = aflStatsSheet.getSheetByName("Round " + byeRound);
    if (byeStatsSheet) {
      var byeStatsData = byeStatsSheet.getDataRange().getValues();
      byeStatsLookup[byeRound] = buildStatsLookup(byeStatsData);
    }
  });

  // **Get all weekly team selections**
  var teamData = weeklyTeamsSheet.getDataRange().getValues();
  var filteredTeams = teamData.filter(row => row[3] == roundNumber);

  var updatedRows = [];

  // **Process each team selection**
  filteredTeams.forEach(team => {
    var bbbfflTeam = team[2];
    var playerIds = team.slice(4, 13);
    var totalScore = 0;
    var statusFlags = [];
    var finalisedTimestamp = "";
    var row = [roundNumber, bbbfflTeam];

    let iScore = 0;
    let iStatus = "FT";
    let replacedPosition = null;

    playerIds.forEach((originalPlayerId, index) => {
      const position = getPositionFromIndex(index);
      let playerId = originalPlayerId;
      let playerScore = 0;
      let playerStatus = "NS";
      let isInterchangeUsed = false;

      const aflTeam = playerAFLTeamLookup[playerId] || "Unknown";

      // 1. 🧩 Interchange Replacement (only if slot is empty or has no stats)
      const teamOverrides = positionOverrides[bbbfflTeam] || {};
      const overrideExists = !!teamOverrides[position];
      const override = overrideExists ? teamOverrides[position] : null;

      // 🔁 Interchange should trigger if an override exists
      if (override) {
        const replacementId = override.playerId;
        const replacementStats = statsLookup[replacementId];
        const replacementName = override.name;

        const replacementAFLTeam = playerAFLTeamLookup[replacementId];
        let byeStats = null;

        // Check whether to use the bye replacement round for the current player
        if (replacementAFLTeam && byeTeamsForRound[replacementAFLTeam] !== undefined) {
          const byeRound = byeTeamsForRound[replacementAFLTeam];
          byeStats = (byeStatsLookup[byeRound] && byeStatsLookup[byeRound][replacementId]) || null;
        }
        // Use AFL stats if available
        if (replacementStats) {
          playerId = replacementId;
          playerScore = calculateFantasyPoints(override.replacedPosition || position, replacementStats);
          playerStatus = "IR";
          Logger.log(`🧠 Replacement stats source: AFL Stats`);
        // If note check for a current bye override
        } else if (byeStats) {
          playerScore = calculateFantasyPoints(override.replacedPosition || position, byeStats);
          playerStatus = "IRB";
          Logger.log(`🧠 Replacement stats source: Bye Replace Round`);
        // Otherwise fall back to manual override stats
        } else {
          playerId = replacementId;
          playerScore = calculateFantasyPoints(override.replacedPosition || position, override.stats);
          playerStatus = "IRO";
          Logger.log(`🧠 Replacement stats source: Manual Override`);
        }

        Logger.log(`🧠 Status applied: ${playerStatus}`);
        Logger.log(`🔄 Interchange Replacement triggered for ${bbbfflTeam} → ${position} replaced by ${replacementName} (${replacementId})`);

        isInterchangeUsed = true;

        // Track to update Interchange slot later
        iScore = playerScore;
        iStatus = position;
        replacedPosition = position;

        Logger.log(`✅ iStatus set to: ${iStatus}, iScore: ${iScore}, replacedPosition: ${replacedPosition}`);
        Logger.log(`🧠 Replacement stats source: ${statsLookup[playerId] ? "AFL Stats" : "Manual Override"}`);
        Logger.log(`🧠 Status applied: ${playerStatus}`);
      }

      // 2. 🟠 Manual Override (if not replaced already)
      else if (!isInterchangeUsed && localOverrides[playerId]) {
        const manualStats = localOverrides[playerId];

        if (overridesData.some(row =>
          row[0] == roundNumber &&
          row[2] == playerId &&
          (row[10] || "").trim() === "DNP"
        )) {
          playerScore = 0;
          playerStatus = "DNP";
          Logger.log(`⚠️ ${playerNameLookup[playerId] || manualStats.name} marked as DNP (Did Not Play).`);
        } else {
          playerScore = calculateFantasyPoints(position, manualStats);
          playerStatus = "O";
        }
      }

      // 3. ✅ Full-time stats
      else if (statsLookup[playerId]) {
        playerScore = calculateFantasyPoints(position, statsLookup[playerId]);
        playerStatus = "FT";
      }

      // 4. 📆 Bye Round stats
      else if (byeTeamsForRound[aflTeam] !== undefined) {
        const byeRound = byeTeamsForRound[aflTeam];
        if (byeStatsLookup[byeRound] && byeStatsLookup[byeRound][playerId]) {
          playerScore = calculateFantasyPoints(position, byeStatsLookup[byeRound][playerId]);
          playerStatus = "B";
        }
      }

      totalScore += playerScore;

      // Always push PlayerID, Score, Status — but override score/status if IR is used
      if (isInterchangeUsed) {
        // Show original slot as empty and "IR" gets patched later
        row.push(originalPlayerId, 0, "NS");
      } else {
        row.push(playerId, playerScore, playerStatus);
      }
      statusFlags.push(playerStatus);
    });

    // 🔁 If Interchange was used, replace both the I Score and the replaced Position Status
    if (replacedPosition) {
      const positionIndexes = {
        "Forward1": 2,
        "Forward2": 5,
        "Forward3": 8,
        "Midfield1": 11,
        "Midfield2": 14,
        "Midfield3": 17,
        "Ruck": 20,
        "Tackler": 23,
        "Interchange": 26
      };

      const replacedIndex = positionIndexes[replacedPosition];

      // 💥 Update replaced position with Interchange score and IR status
      row[replacedIndex + 1] = iScore;     // Score
      row[replacedIndex + 2] = "IR";       // Status

      // 💥 Keep Interchange ID and "replacedPosition" status, but blank the score
      row[26] = row[26] || "";             // ID (already correct)
      row[27] = "";                        // I Score blank
      row[28] = replacedPosition;          // I Status
    }

    const overallStatus = statusFlags.every(status => ["FT", "B", "O", "IR", "IRO", "IRB", "DNP"].includes(status)) ? "✔️ FT" : "🟡 Live";
    finalisedTimestamp = overallStatus === "✔️ FT" 
      ? Utilities.formatDate(new Date(), Session.getScriptTimeZone(), "yyyy-MM-dd HH:mm:ss") 
      : "";

    Logger.log(`📋 Pushing row for ${bbbfflTeam} - iScore: ${iScore}, iStatus: ${iStatus}, replacedPosition: ${replacedPosition}`);

    row.push(totalScore, overallStatus, finalisedTimestamp);

    Logger.log(`📄 Final row for ${bbbfflTeam}: ${JSON.stringify(row)}`);
    updatedRows.push(row);
  });

  // **Remove only previous results for this round**
  var existingResults = resultsSheet.getDataRange().getValues();
  var rowsToDelete = [];
  for (var i = existingResults.length - 1; i > 0; i--) { 
    if (existingResults[i][0] == roundNumber) { 
      rowsToDelete.push(i + 1);
    }
  }

  // **Batch delete previous rows for this round**
  rowsToDelete.forEach(rowIndex => resultsSheet.deleteRow(rowIndex));

  // **Append new data**
  if (updatedRows.length > 0) {
      resultsSheet.getRange(resultsSheet.getLastRow() + 1, 1, updatedRows.length, updatedRows[0].length).setValues(updatedRows);
  }

  // Add notes with player names to Player ID columns
  var resultStartRow = resultsSheet.getLastRow() - updatedRows.length + 1;
  var playerNoteCols = []; // Track player ID column indexes

  // Find Player ID columns: start from column 3, then every 3rd column (3, 6, 9, ..., up to 29)
  for (var i = 0; i < 9; i++) {
    playerNoteCols.push(3 + i * 3); // columns are 1-based
  }

  for (var r = 0; r < updatedRows.length; r++) {
    for (var c = 0; c < playerNoteCols.length; c++) {
      var col = playerNoteCols[c];
      var playerId = updatedRows[r][col - 1]; // 0-based index

      if (playerId) {
        let name = playerNameLookup[playerId] 
          || (localOverrides[playerId] && localOverrides[playerId].name)
          || null;

        if (name) {
          resultsSheet.getRange(resultStartRow + r, col).setNote(name);
        }
      }
    }
  }

  Logger.log("✅ BBBFFL Results updated for Round " + roundNumber);
  updateResultsDashboard("fetchBBBFFLResults", "Fetch results from AFL tables");
}

/**
 * Convert AFL stats into a lookup dictionary
 */
function buildStatsLookup(statsData) {
    var lookup = {};
    statsData.forEach(row => {
        var playerId = row[1];
        lookup[playerId] = row.slice(5, 17); // Extract relevant stats
    });
    return lookup;
}

/**
 * Determine player position
 */
function getPositionFromIndex(index) {
    const positions = [
        "Forward1", "Forward2", "Forward3",
        "Midfield1", "Midfield2", "Midfield3",
        "Ruck", "Tackler", "Interchange"
    ];
    return positions[index] || "Unknown";
}

/**
 * Calculate fantasy points
 */
function calculateFantasyPoints(position, stats) {
    // 🔁 Convert full position names to scoring type
    if (position.startsWith("Forward")) position = "For";
    else if (position.startsWith("Midfield")) position = "Mid";
    else if (position === "Tackler") position = "Tack";
    else if (position === "Interchange") {
        // Interchange player gets scored by their original position
        // So ideally, this function should be passed the replacedPosition
        Logger.log("⚠️ Interchange position passed to calculateFantasyPoints. Please pass the original position instead.");
        return 0;
    }
    if (typeof stats === "object" && !Array.isArray(stats)) {
        // **Convert override object into an array**
        stats = [
            stats.kicks || 0, stats.handballs || 0, stats.disposals || 0, stats.marks || 0,
            stats.hitouts || 0, stats.tackles || 0, stats.goals || 0, stats.behinds || 0,
            stats.clearances || 0, stats.freeFor || 0, stats.freeAgainst || 0
        ];
    }

    // **Ensure stats is an array before proceeding**
    if (!Array.isArray(stats)) {
        Logger.log("❌ Error: stats is not an array for position: " + position);
        return 0; // Return 0 to prevent breaking execution
    }

    var [kicks, handballs, disposals, marks, hitouts, tackles, goals, behinds, clearances, freeFor, freeAgainst] = stats.map(Number);

    switch (position) {
        case "For": return (6 * goals) + (1 * behinds);
        case "Mid": return disposals;
        case "Ruck": return hitouts + marks;
        case "Tack": return 6 * tackles;
        Logger.log(`🧮 [IR] Scoring ${replacement.name} (${playerId}) as ${replacement.replacedPosition} → ${playerScore} points`);
        default: return 0;
    }
}

function monitorWeeklyTeamUpdates() {
  const config = getConfig();

  const weeklyTeamsSS = SpreadsheetApp.openById(config.bbbfflWeeklyTeamsSheetId);
  const resultsSS = SpreadsheetApp.getActiveSpreadsheet();

  const weeklyDash = weeklyTeamsSS.getSheetByName("Dashboard");
  const resultsDash = resultsSS.getSheetByName("Dashboard");

  if (!weeklyDash || !resultsDash) {
    logAction("⚠️ Dashboard missing", LOG_LEVELS.WARN);
    return;
  }

  const weeklyRun = getScriptLastRunTime(weeklyDash, "consolidateWeeklyTeams");
  const resultsRun = getScriptLastRunTime(resultsDash, "fetchBBBFFLResults");

  if (weeklyRun && resultsRun && weeklyRun > resultsRun) {
    logAction("⚠️ Need to fetch new teams.", LOG_LEVELS.INFO);
    const masterSheet = weeklyTeamsSS.getSheetByName("Master Weekly Teams");
    const data = masterSheet.getDataRange().getValues();
    const latestRound = Math.max(...data.slice(1).map(row => parseInt(row[3], 10)).filter(r => !isNaN(r)));

    if (latestRound) {
      fetchBBBFFLResults(latestRound);
      updateResultsDashboard("fetchBBBFFLResults", `Triggered via monitorWeeklyTeamUpdates for Round ${latestRound}`);
    }
  }
}

function getScriptLastRunTime(sheet, scriptName) {
  const data = sheet.getDataRange().getValues();
  for (let i = data.length - 1; i >= 1; i--) {
    if (data[i][0] === scriptName && data[i][1]) {
      return new Date(data[i][1]);
    }
  }
  return null;
}
