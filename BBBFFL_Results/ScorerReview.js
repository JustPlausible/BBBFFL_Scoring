// Updated 29/3/2025
// Generate a list of recommendations for the scorer based on player stats

function generateScorerReview(roundNumber) {
  const config = getConfig();

  const aflStatsSS = SpreadsheetApp.openById(config.aflStatsSheetId);
  const bbbfflTeamsSS = SpreadsheetApp.openById(config.bbbfflWeeklyTeamsSheetId);
  const bbbfflResultsSS = SpreadsheetApp.openById(config.bbbfflResultsSheetId);

  const roundStatsSheetName = `Round ${roundNumber}`;
  const masterWeeklyTeamsSheet = bbbfflTeamsSS.getSheetByName("Master Weekly Teams");
  const playerNamesSheet = aflStatsSS.getSheetByName("Player Names");
  const byeReplaceSheet = bbbfflResultsSS.getSheetByName("Bye Replace");
  const localOverridesSheet = bbbfflResultsSS.getSheetByName("Local Overrides");

  // === Load Local Overrides ===
  const localOverridesData = localOverridesSheet.getDataRange().getValues();
  const localOverrideMap = {};          // playerId → { name, round }
  const overrideNameFallbackMap = {};   // playerId → name (any round)
  const overriddenPlayerIds = new Set(); // All players overridden this round
  const overriddenPositions = new Set(); // Positions that may be replaced

  for (let i = 1; i < localOverridesData.length; i++) {
    const row = localOverridesData[i];

    const round = parseInt(row[0]);           // Column A: Round
    const teamName = row[1];                  // Column B: BBBFFL Team
    const playerId = parseInt(row[2]);        // Column C: Player ID
    const name = row[3];                      // Column D: Player Name
    const goals = row[4];
    const behinds = row[5];
    const disposals = row[6];
    const marks = row[7];
    const hitouts = row[8];
    const tackles = row[9];
    const status = row[10];                   // Column K: Position being replaced (e.g. "Tackler")

    if (playerId) {
      overrideNameFallbackMap[playerId] = name;

      if (round === roundNumber) {
        localOverrideMap[playerId] = {
          name,
          round,
          goals: Number(goals),
          behinds: Number(behinds),
          disposals: Number(disposals),
          marks: Number(marks),
          hitouts: Number(hitouts),
          tackles: Number(tackles),
          total: [goals, behinds, disposals, marks, hitouts, tackles].map(Number).reduce((a, b) => a + b, 0),
          status
        };

        overriddenPlayerIds.add(playerId);

        // ✅ Track replaced position
        if (teamName && status) {
          overriddenPositions.add(`${teamName}_${status}`);
        }
      }
    }
  }

  // === Setup Output Sheet ===
  const reviewSheetName = "Scorer Review";
  let reviewSheet = bbbfflResultsSS.getSheetByName(reviewSheetName);
  if (!reviewSheet) reviewSheet = bbbfflResultsSS.insertSheet(reviewSheetName);
  else reviewSheet.clearContents();

  const output = [["Round", "BBBFFL Team", "Player ID", "Player Name", "Goals", "Behinds", "Disposals", "Tackles", "Hitouts", "Marks", "Status"]];

  // === Load Weekly Teams ===
  const headers = masterWeeklyTeamsSheet.getDataRange().getValues()[0];
  const colIndexes = headers.reduce((acc, h, idx) => (acc[h] = idx, acc), {});
  const teamData = masterWeeklyTeamsSheet.getDataRange().getValues().slice(1).filter(row => row[colIndexes["Round"]] == roundNumber);

  const posCols = ["Forward1", "Forward2", "Forward3", "Midfield1", "Midfield2", "Midfield3", "Ruck", "Tackler"];
  const interchangeCol = "Interchange";

  // === Load Player Info ===
  const playerNameMap = {};
  const playerTeamMap = {};
  const playerData = playerNamesSheet.getDataRange().getValues();
  for (let i = 1; i < playerData.length; i++) {
    const [id, fullName, , , team] = playerData[i];
    if (id) {
      playerNameMap[id] = fullName;
      playerTeamMap[id] = team;
    }
  }

  // === Load Bye Round Mappings ===
  const byeData = byeReplaceSheet.getDataRange().getValues();
  const byeMap = {};
  for (let i = 1; i < byeData.length; i++) {
    const [byeRound, aflTeam, insertRound] = byeData[i];
    if (insertRound == roundNumber) {
      logAction(`Bye mapped for ${aflTeam}`);
      byeMap[aflTeam] = byeRound;
    }
  }

  // === Load Round Stats ===
  const roundStatsSheet = aflStatsSS.getSheetByName(roundStatsSheetName);
  const roundStats = roundStatsSheet.getDataRange().getValues();
  const statsByPlayerId = {};
  for (let i = 1; i < roundStats.length; i++) {
    const row = roundStats[i];
    const playerId = row[1];
    const rawStats = row.slice(5, 15).map(Number);
    const statSum = rawStats.reduce((sum, val) => sum + val, 0);
    statsByPlayerId[playerId] = {
      name: row[2],
      goals: row[11],
      behinds: row[12],
      disposals: row[7],
      tackles: row[10],
      hitouts: row[9],
      marks: row[8],
      total: statSum
    };
  }

  // === Load Bye Stats ===
  const byeStatsLookup = {};
  const uniqueByeRounds = [...new Set(Object.values(byeMap))];

  uniqueByeRounds.forEach(byeRound => {
    const sheet = aflStatsSS.getSheetByName(`Round ${byeRound}`);
    if (!sheet) return;

    const data = sheet.getDataRange().getValues();

    // ✅ Get the AFL teams that had byes this round
    const byeTeamsForRound = Object.entries(byeMap)
      .filter(([team, mappedRound]) => mappedRound === byeRound)
      .map(([team]) => team);

    const byeTeamSet = new Set(byeTeamsForRound);

    for (let i = 1; i < data.length; i++) {
      const row = data[i];
      const playerId = row[1];
      const team = row[3]; // Assuming row[3] is AFL team code

      if (!byeTeamSet.has(team)) continue; // ✅ Only load from teams with a bye

      //logAction(`Bye stats available for ${team} (Player ${playerId})`);

      const rawStats = row.slice(5, 15).map(Number);
      const statSum = rawStats.reduce((sum, val) => sum + val, 0);

      byeStatsLookup[playerId] = {
        name: row[2],
        goals: row[11],
        behinds: row[12],
        disposals: row[7],
        tackles: row[10],
        hitouts: row[9],
        marks: row[8],
        total: statSum
      };
    }
  });

  // === Review Each Team ===
  for (const row of teamData) {
    const teamName = row[colIndexes["BBBFFL Team"]];
    const interchangeId = row[colIndexes[interchangeCol]];
    const interchangeStats = statsByPlayerId[interchangeId] || byeStatsLookup[interchangeId] || null;
    let interchangeUsed = false;

    for (const pos of posCols) {
      const pid = row[colIndexes[pos]];
      const playerId = pid ? parseInt(pid) : null;
      const contextLabel = `${teamName} (${pos})`;

      const overrideKey = `${teamName}_${pos}`;
      if (overriddenPositions.has(overrideKey)) {
        logAction(`🔁 ${contextLabel}: Position already filled by override — skipping`);
        continue;
      }

      const playerStats = statsByPlayerId[playerId];
      const playerTeam = playerTeamMap[playerId];
      const byeRound = byeMap[playerTeam];
      const byeStats = byeStatsLookup[playerId];

      if (shouldSkipDueToManualOverride(playerId, contextLabel, localOverrideMap)) continue;

      // If override exists but total = 0 → still check for interchange use
      const overrideData = localOverrideMap[playerId];
      if (overrideData && overrideData.total === 0) {
        logAction(`0️⃣ ${contextLabel}: Override with 0 stats — checking interchange`);
        if (handleZeroStats(null, overrideData, contextLabel, interchangeStats, interchangeUsed, interchangeId, overriddenPlayerIds, roundNumber, teamName, pos, playerNameMap, output)) {
          interchangeUsed = true;
          continue;
        }
      }

      if (handleNotInAPI(playerId, contextLabel, roundNumber, teamName, output, playerTeamMap, playerNameMap, overrideNameFallbackMap, overriddenPlayerIds)) continue;
      if (handleBlankSlot(playerId, contextLabel, interchangeStats, interchangeUsed, interchangeId, overriddenPlayerIds, roundNumber, teamName, pos, output)) {
        interchangeUsed = true;
        continue;
      }

      if (handleValidStats(playerStats, contextLabel)) continue;
      if (handleByeStats(byeStats, contextLabel, byeRound)) continue;
      if (handleZeroStats(playerStats, localOverrideMap[playerId], contextLabel, interchangeStats, interchangeUsed, interchangeId, overriddenPlayerIds, roundNumber, teamName, pos, playerNameMap, output)) {
        interchangeUsed = true;
        continue;
      }
      if (handleNoStats(playerStats, byeStats, contextLabel, interchangeStats, interchangeUsed, interchangeId, overriddenPlayerIds, roundNumber, teamName, pos, playerNameMap, output)) {
        interchangeUsed = true;
        continue;
      }

      const fallbackName = playerNameMap[playerId] || overrideNameFallbackMap[playerId] || "Unknown";
      const hasStatsButZero = (playerStats && playerStats.total === 0) || (localOverrideMap[playerId] && localOverrideMap[playerId].total === 0);

      if (!overriddenPlayerIds.has(playerId)) {
        const statusLabel = hasStatsButZero ? "0 Stats" : "Not in API";
        logAction(`🛑 ${contextLabel}: No stats or override — marking for review as "${statusLabel}"`);
        output.push([roundNumber, teamName, playerId, fallbackName, "", "", "", "", "", "", statusLabel]);
      }

    }
  }

  if (output.length === 0) {
    SpreadsheetApp.getUi().alert("✅ No review required — all teams scored correctly for this round.");
    reviewSheet.getRange("A2:L").clearContent();  // Optional: clear any old data
    return;
  }
  // Write the data to the Scorer Review sheet
  reviewSheet.getRange(1, 2, output.length, output[0].length).setValues(output);
  Logger.log(`✅ Scorer Review generated for Round ${roundNumber}`);

  // Add data validation dropdown to the "Status" column (column 11)
  const statusOptions = [
    ["OVERRIDE"],  // Manually overridden stats
    ["DNP"],       // Did Not Play
    ["Ignore"],     // Don't apply any logic
  ].concat(
    posCols.map(value => [value])     // Default positions available for interchange replacement
  );

  const statusValidation = SpreadsheetApp
    .newDataValidation()
    .requireValueInList(statusOptions, true)
    .setAllowInvalid(true)
    .build();

  if (output.length <= 1) {
    SpreadsheetApp.getUi().alert("✅ No further review required — all teams scored correctly for this round.");
    return;
  }

  reviewSheet.getRange(2, 12, output.length - 1, 1).setDataValidation(statusValidation);

  // Add checkbox for each completed row
  const checkboxRange = reviewSheet.getRange(2, 1, output.length - 1); // Skip header row
  checkboxRange.insertCheckboxes();
  reviewSheet.getRange("A1").setValue("Approved");

  updateResultsDashboard("generateScorerReview", "Generated Scorer Review rows");

}

// Helper functions

function shouldSkipDueToManualOverride(playerId, label, localOverrideMap) {
  if (playerId && localOverrideMap[playerId]) {
    const override = localOverrideMap[playerId];
    const total = override.total || 0;
    if (total > 0) {
      logAction(`📋 ${label}: Manual override for ${playerId} — skipping (override stats = ${total})`);
      return true;
    } else {
      logAction(`📋 ${label}: Manual override for ${playerId}, but total = 0 — checking interchange`);
    }
  }
  return false;
}

function handleNotInAPI(playerId, label, round, teamName, output, playerTeamMap, playerNameMap, overrideNameFallbackMap, overriddenPlayerIds) {
  if (!playerId) return false; // Don't run this check if no playerId at all

  if (overriddenPlayerIds.has(playerId)) {
    logAction(`📋 ${label}: Already overridden — skipping Not in API check`);
    return true;
  }

  if (!playerTeamMap[playerId] && !playerNameMap[playerId]) {
    const fallbackName = overrideNameFallbackMap[playerId] || "Unknown";
    logAction(`❓ ${label}: ${playerId} not found in API — fallback to previous name`);
    output.push([round, teamName, playerId, fallbackName, "", "", "", "", "", "", "Not in API"]);
    return true;
  }

  return false;
}

function handleBlankSlot(playerId, label, interchangeStats, interchangeUsed, interchangeId, overriddenIds, round, team, pos, output) {
  if (!playerId) {
    logAction(`⬜ ${label}: Slot blank – checking interchange`);
    if (interchangeStats && !interchangeUsed && !overriddenIds.has(interchangeId)) {
      logAction(`→ Interchange ${interchangeStats.name} used for ${pos}`);
      output.push([round, team, interchangeId, interchangeStats.name, interchangeStats.goals, interchangeStats.behinds, interchangeStats.disposals, interchangeStats.tackles, interchangeStats.hitouts, interchangeStats.marks, pos]);
      return true;
    } else {
      logAction(`→ No interchange available for blank ${pos} (either used or overridden)`);
    }
  }
  return false;
}

function handleValidStats(playerStats, label) {
  if (playerStats && playerStats.total > 0) {
    logAction(`✅ ${label}: Played and scored → skip`);
    return true;
  }
  return false;
}

function handleByeStats(byeStats, label, byeRound) {
  if (byeStats && byeStats.total > 0) {
    logAction(`📅 ${label}: Bye stats from Round ${byeRound} → skip`);
    return true;
  }
  return false;
}

function handleZeroStats(playerStats, override, label, interchangeStats, interchangeUsed, interchangeId, overriddenIds, round, team, pos, nameMap, output) {
  const total = (playerStats?.total != null) ? playerStats.total : (override?.total ?? null);
  if (total === 0) {
    logAction(`0️⃣ ${label}: Stats total = 0 — checking interchange`);
    if (interchangeStats && !interchangeUsed && !overriddenIds.has(interchangeId)) {
      logAction(`→ Interchange ${interchangeStats.name} used for ${pos}`);
      output.push([round, team, interchangeId, nameMap[interchangeId] || "", interchangeStats.goals, interchangeStats.behinds, interchangeStats.disposals, interchangeStats.tackles, interchangeStats.hitouts, interchangeStats.marks, pos]);
      return true;
    } else {
      logAction(`→ No interchange available or already used for ${pos}`);
    }
  }
  return false;
}

function handleNoStats(playerStats, byeStats, label, interchangeStats, interchangeUsed, interchangeId, overriddenIds, round, team, pos, nameMap, output) {
  if (!playerStats && !byeStats) {
    logAction(`🙈 ${label}: No stats found — checking interchange`);
    if (interchangeStats && !interchangeUsed && !overriddenIds.has(interchangeId)) {
      logAction(`→ Interchange ${interchangeStats.name} used for ${pos}`);
      output.push([round, team, interchangeId, nameMap[interchangeId] || "", interchangeStats.goals, interchangeStats.behinds, interchangeStats.disposals, interchangeStats.tackles, interchangeStats.hitouts, interchangeStats.marks, pos]);
      return true;
    }
  }
  return false;
}

function processApprovedOverrides() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const reviewSheet = ss.getSheetByName("Scorer Review");
  const overrideSheet = ss.getSheetByName("Local Overrides");

  const reviewData = reviewSheet.getDataRange().getValues(); // Includes header
  const approvedCol = 0;  // Column A = checkbox
  const headers = reviewData[0];
  const rows = reviewData.slice(1); // Exclude header

  const overrideDataToAdd = [];
  const rowsToDelete = [];

  rows.forEach((row, index) => {
    const isApproved = row[approvedCol];
    if (isApproved === true) {
      const [
        , // Skip checkbox
        round,
        team,
        playerId,
        name,
        goals,
        behinds,
        disposals,
        tackles,
        hitouts,
        marks,
        status
      ] = row;

      const timestamp = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), "yyyy-MM-dd HH:mm:ss");

      overrideDataToAdd.push([
        round,
        team,
        playerId,
        name,
        goals,
        behinds,
        disposals,
        tackles,
        hitouts,
        marks,
        status,
        timestamp
      ]);

      // Record row index to delete (add 2 to account for header and 0-indexing)
      rowsToDelete.unshift(index + 2);    }
  });

  // Append approved overrides to the next blank rows in the Local Overrides sheet
  if (overrideDataToAdd.length > 0) {
    overrideSheet.getRange(
      overrideSheet.getLastRow() + 1,
      1,
      overrideDataToAdd.length,
      overrideDataToAdd[0].length
    ).setValues(overrideDataToAdd);
  }

  // Delete processed rows from bottom to top to avoid index shifting
  rowsToDelete.forEach(rowIndex => {
    reviewSheet.deleteRow(rowIndex);
  });

  Logger.log(`✅ Processed and moved ${overrideDataToAdd.length} approved override(s).`);
  updateResultsDashboard("processApprovedOverrides", "Processed Overrides for round");
  suggestCurrentRoundScorerReview();
}

function suggestCurrentRoundScorerReview() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const resultsSheet = ss.getSheetByName("Master Results");
  const reviewSheet = ss.getSheetByName("Scorer Review");
  const rounds = resultsSheet.getRange("A2:A").getValues().flat().filter(val => !isNaN(val));
  const uniqueRounds = [...new Set(rounds)].sort((a, b) => a - b);

  if (uniqueRounds.length === 0) return;

  // Clear sheet
  const lastRow = reviewSheet.getLastRow();
  const maxCols = reviewSheet.getMaxColumns();

  if (lastRow > 1) {
    const range = reviewSheet.getRange(2, 1, lastRow - 1, maxCols);
    range.clearContent();           // Clear values
    range.clearNote();              // Clear notes
    range.clearDataValidations();   // Remove checkboxes or dropdowns
  }

  const currentRound = uniqueRounds[uniqueRounds.length - 1];
  const roundCell = reviewSheet.getRange("O1");

  // Set dropdown validation
  const rule = SpreadsheetApp.newDataValidation()
    .requireValueInList(uniqueRounds.map(r => r.toString()), true)
    .setAllowInvalid(false)
    .setHelpText("Select the round to process")
    .build();
  roundCell.setDataValidation(rule);
  roundCell.setValue(currentRound); // Set default

  // 🟦 NEW: Apply dropdown style (CHIP) via new Sheet service
  const advancedSheet = SpreadsheetApp.getActive().getSheetByName("Scorer Review");
  const config = SpreadsheetApp.newDataValidation().copy().copy();

  const advancedValidation = SpreadsheetApp.newDataValidation()
    .requireValueInList(uniqueRounds.map(r => r.toString()), true)
    .setAllowInvalid(false)
    .setHelpText("Select round")
    .build();

  // This style change requires the Advanced Sheets Service (if not enabled, enable it via Apps Script > Services)
  const sheetId = ss.getId();
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("Scorer Review");
  const sheetName = sheet.getSheetName();
  const cellA1 = 'O1';

  const resource = {
    requests: [{
      updateCells: {
        range: {
          sheetId: sheet.getSheetId(),
          startRowIndex: 0,
          endRowIndex: 1,
          startColumnIndex: 14,
          endColumnIndex: 15,
        },
        rows: [{
          values: [{
            dataValidation: {
              condition: {
                type: 'ONE_OF_LIST',
                values: uniqueRounds.map(r => ({ userEnteredValue: r.toString() }))
              },
              strict: true,
              showCustomUi: true,
            }
          }]
        }],
        fields: "dataValidation"
      }
    }]
  };

  // Requires enabling the Advanced Sheets Service
  Sheets.Spreadsheets.batchUpdate(resource, ss.getId());
  reviewSheet.getRange("N1").setValue("Select Round:");

  Logger.log(`🕓 Suggested current round: ${currentRound}`);
}

function protectRoundCell() {
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("Scorer Review");
  const cell = sheet.getRange("O1");

  const protection = cell.protect().setDescription("Protect Round Selector");
  protection.setWarningOnly(true); // 🟡 Just warns on edit — scorer can still choose

  // Optional: Fully restrict edits to only specific users
  // protection.removeEditors(protection.getEditors());
  // protection.addEditor("your.email@domain.com");
}

function runReviewForSuggestedRound() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const reviewSheet = ss.getSheetByName("Scorer Review");
  const round = reviewSheet.getRange("O1").getValue(); // Suggested round in O1

  if (!round || isNaN(round)) {
    SpreadsheetApp.getUi().alert("Please ensure a valid round number is set in cell O1.");
    return;
  }

  generateScorerReview(round);
}
