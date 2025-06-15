// Updated 29/3/2025
// Generates Live BBBFFL Match Sheets

function generateLiveBBBFFLMatches(roundNumber) {
  const config = getConfig();
  const ss = SpreadsheetApp.openById(config.bbbfflResultsSheetId);

  const aflStatsSS = SpreadsheetApp.openById(config.aflStatsSheetId);
  const liveSheet = aflStatsSS.getSheetByName("Live Stats");
  const roundSheetName = "Round " + roundNumber;
  const roundSheet = aflStatsSS.getSheetByName(roundSheetName);
  const byeReplaceSheet = ss.getSheetByName("Bye Replace");

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
  Logger.log(`✅ Bye Round Mappings: ${JSON.stringify(byeTeamsForRound)}`, LOG_LEVELS.INFO);

  if (!liveSheet) throw new Error("❌ Sheet 'Live Stats' not found.");
  if (!roundSheet) throw new Error(`❌ Sheet '${roundSheetName}' not found.`);

  const weeklySS = SpreadsheetApp.openById(config.bbbfflWeeklyTeamsSheetId);
  const weeklyTeamsSheet = weeklySS.getSheetByName("Master Weekly Teams");
  if (!weeklyTeamsSheet) throw new Error("❌ Sheet 'Master Weekly Teams' not found.");

  const fixtureSheet = ss.getSheetByName("2025 Fixtures");
  if (!fixtureSheet) throw new Error("❌ Sheet '2025 Fixtures' not found.");

  const playerSheet = aflStatsSS.getSheetByName("Player Names");
  if (!playerSheet) throw new Error("❌ Sheet 'Player Names' not found.");

  const overrideSheet = ss.getSheetByName("Local Overrides");

  const sheetName = "Live Round " + roundNumber;
  let sheet = ss.getSheetByName(sheetName);
  if (!sheet) sheet = ss.insertSheet(sheetName); else sheet.clear();

  const fixtureData = fixtureSheet.getDataRange().getValues().filter(r => r[0] == roundNumber);
  const weeklyData = weeklyTeamsSheet.getDataRange().getValues();
  const liveData = liveSheet.getDataRange().getValues();
  const roundData = roundSheet.getDataRange().getValues();
  const playerData = playerSheet.getDataRange().getValues();
  const overrideData = overrideSheet ? overrideSheet.getDataRange().getValues() : [];

  const playerLookup = {};
  for (let i = 1; i < playerData.length; i++) {
    playerLookup[String(playerData[i][0])] = playerData[i][1];
  }

  const overrideNames = {};
  for (let i = 1; i < overrideData.length; i++) {
    overrideNames[String(overrideData[i][2])] = overrideData[i][3];
  }

  const aflTeamMap = {};
  const byeStatsLookup = {};
  for (const byeRound of Object.values(byeTeamsForRound)) {
    const sheet = aflStatsSS.getSheetByName("Round " + byeRound);
    if (sheet) {
      const data = sheet.getDataRange().getValues();
      for (let i = 1; i < data.length; i++) {
        const playerId = String(data[i][1]);
        if (!byeStatsLookup[playerId]) {
          byeStatsLookup[playerId] = {
            goals: data[i][11] || 0,
            behinds: data[i][12] || 0,
            disposals: data[i][7] || 0,
            hitouts: data[i][9] || 0,
            marks: data[i][8] || 0,
            tackles: data[i][10] || 0
          };
        }
        const teamAbbrev = data[i][3];
        if (!aflTeamMap[playerId] && teamAbbrev) {
          aflTeamMap[playerId] = teamAbbrev;
        }
      }
    }
  }
  logAction(`✅ Bye Stats loaded for ${Object.keys(byeStatsLookup).length} players`, LOG_LEVELS.INFO);
  logAction(`🧪 byeStatsLookup[893] = ${JSON.stringify(byeStatsLookup["893"])}`, LOG_LEVELS.DEBUG);

  const combinedStats = {};
  const interchangeMap = {}; // team → { position → { playerId, name } }
  const positions = ["Forward1", "Forward2", "Forward3", "Midfield1", "Midfield2", "Midfield3", "Ruck", "Tackler", "Interchange"];

  function processOverrides(overrideData, roundNumber) {
    let count = 0;

    for (let i = 1; i < overrideData.length; i++) {
      const row = overrideData[i];
      const rowRound = row[0];
      if (rowRound != roundNumber) continue;

      const team = row[1];
      const playerId = String(row[2]);
      const playerName = row[3];
      const status = row[10]; // Either "OVERRIDE" or a position like "Tackler"

      const goals = parseInt(row[4]) || 0;
      const behinds = parseInt(row[5]) || 0;
      const disposals = parseInt(row[6]) || 0;
      const tackles = parseInt(row[7]) || 0;
      const hitouts = parseInt(row[8]) || 0;
      const marks = parseInt(row[9]) || 0;

      const stats = {
        goals,
        behinds,
        disposals,
        marks,
        hitouts,
        tackles,
        _source: "override"
      };

      if (status === "OVERRIDE") {
        // Standard stat override
        combinedStats[playerId] = stats;
        count++;
      } else if (positions.includes(status)) {
        // Interchange replacement for a position (e.g. "Tackler")
        if (!interchangeMap[team]) interchangeMap[team] = {};
        interchangeMap[team][status] = {
          playerId,
          stats,
          name: playerName
        };
        combinedStats[playerId] = stats; // Also include in stats map for scoring
        count++;
      }
    }

    logAction(`📋 Loaded override stats for ${count} player(s)`, LOG_LEVELS.INFO);
  }

  function getHeaderIndexes(headerRow) {
    return {
      playerIdIdx: headerRow.indexOf("Player ID"),
      statusIdx: headerRow.indexOf("Status"),
      kicksIdx: headerRow.indexOf("Kicks"),
      handballsIdx: headerRow.indexOf("Handballs"),
      disposalsIdx: headerRow.indexOf("Disposals"),
      marksIdx: headerRow.indexOf("Marks"),
      hitoutsIdx: headerRow.indexOf("Hitouts"),
      tacklesIdx: headerRow.indexOf("Tackles"),
      goalsIdx: headerRow.indexOf("Goals"),
      behindsIdx: headerRow.indexOf("Behinds"),
      // ...add any others you need
    };
  }

  function processStats(data, label, sourceKey) {
    let added = 0;
    const headerIndexes = getHeaderIndexes(data[0]);
    for (let i = 1; i < data.length; i++) {
      const row = data[i];
      const playerId = String(row[headerIndexes.playerIdIdx]);
      const playerStatus = row[headerIndexes.statusIdx];

      // Only use live stats for players with "LIVE" status
      if (sourceKey === "live" && playerStatus !== "LIVE") continue;
      // Only use round stats for players with "FT" status (or "NS" for not started)
      if (sourceKey === "round" && playerStatus !== "FT") continue;

      // ...rest of your code as before
      const stats = {
        disposals: row[headerIndexes.disposalsIdx] || 0,
        marks: row[headerIndexes.marksIdx] || 0,
        hitouts: row[headerIndexes.hitoutsIdx] || 0,
        tackles: row[headerIndexes.tacklesIdx] || 0,
        goals: row[headerIndexes.goalsIdx] || 0,
        behinds: row[headerIndexes.behindsIdx] || 0,
        _source: sourceKey
      };

      combinedStats[playerId] = stats;
      aflTeamMap[playerId] = row[3]; // Or use mapped index
      added++;
    }
    logAction(`🧮 ${label} stats processed: ${added} players`, LOG_LEVELS.INFO);
  }

  function processByeStats(byeTeamsForRound) {
    for (const [aflTeam, byeRound] of Object.entries(byeTeamsForRound)) {
      const sheet = aflStatsSS.getSheetByName("Round " + byeRound);
      if (!sheet) continue;

      const data = sheet.getDataRange().getValues();
      for (let i = 1; i < data.length; i++) {
        const playerId = String(data[i][1]);
        const playerTeam = data[i][3];

        // Only process if this player's team is a bye team for the current round
        if (playerTeam !== aflTeam) continue;

        // Only add if the player doesn't already have stats
        if (!combinedStats[playerId]) {
          combinedStats[playerId] = {
            disposals: data[i][7] || 0,
            marks: data[i][8] || 0,
            hitouts: data[i][9] || 0,
            tackles: data[i][10] || 0,
            goals: data[i][11] || 0,
            behinds: data[i][12] || 0,
            _source: "bye"
          };
          aflTeamMap[playerId] = playerTeam;

          logAction(`💡 [BYE] Stats added for ${playerId} (${playerLookup[playerId] || "Unknown"}) from team ${aflTeam}`, LOG_LEVELS.DEBUG);
        }
      }
    }

    logAction(`✅ Bye stats added for ${Object.keys(byeTeamsForRound).length} team(s)`, LOG_LEVELS.INFO);
  }

  processStats(liveData, "Live", "live");
  processStats(roundData, `Round ${roundNumber}`, "round");
  processOverrides(overrideData, roundNumber);
  processByeStats(byeTeamsForRound);
  logAction(`📦 CombinedStats: ${Object.keys(combinedStats).length} players`, LOG_LEVELS.INFO);
  logAction(`🔍 AFL Team for Player 893: ${aflTeamMap["893"]}`, LOG_LEVELS.DEBUG);

  //Object.entries(combinedStats).slice(0, 5).forEach(([id, stat]) => {
  //  logAction(`➡️ ${id} → ${JSON.stringify(stat)}`, LOG_LEVELS.DEBUG);
  //});

  const posCols = ["Forward1", "Forward2", "Forward3", "Midfield1", "Midfield2", "Midfield3", "Ruck", "Tackler", "Interchange"];
  const headers = ["", "", "Gls", "Bhs", "Total", "", "", "", "Gls", "Bhs", "Total", ""];

  function renderMatchupRow(pos, label, playerName, stats) {
    return [
      label,
      playerName,
      stats.goals || 0,
      stats.behinds || 0,
      stats.total || 0,
      ""
    ];
  }

  function generateMatchSummary(homeTeam, awayTeam, homeScore, awayScore, homeRemaining, awayRemaining) {
    if (homeScore > awayScore) {
      return `🟢 ${homeTeam} leads by ${homeScore - awayScore} — Players remaining: ${homeTeam} (${homeRemaining}) vs ${awayTeam} (${awayRemaining})`;
    } else if (awayScore > homeScore) {
      return `🟢 ${awayTeam} leads by ${awayScore - homeScore} — Players remaining: ${homeTeam} (${homeRemaining}) vs ${awayTeam} (${awayRemaining})`;
    } else {
      return `⚖️ Match tied — Players remaining: ${homeTeam} (${homeRemaining}) vs ${awayTeam} (${awayRemaining})`;
    }
  }

  function formatMatch(home, away, teamMap) {
    const rows = [];
    const sourceMap = {};
    const nameMap = {};
    let homeScore = 0;
    let awayScore = 0;

    rows.push([`${home} v ${away}`, "", "", "", "", "", "", "", "", "", "", ""]);
    rows.push(headers);

    for (let i = 0; i < posCols.length; i++) {
      const pos = posCols[i];

      // ⛳ Get original player IDs
      let homeId = String(teamMap[home]?.[pos] || "");
      let awayId = String(teamMap[away]?.[pos] || "");

      // 🔁 Check if there's a confirmed interchange override
      const homeOverride = interchangeMap[home]?.[pos];
      const awayOverride = interchangeMap[away]?.[pos];

      if (homeOverride) {
        logAction(`🔁 ${home} using interchange ${homeOverride.name} (${homeOverride.playerId}) at ${pos}`, LOG_LEVELS.DEBUG);
        homeId = homeOverride.playerId;
      }

      if (awayOverride) {
        logAction(`🔁 ${away} using interchange ${awayOverride.name} (${awayOverride.playerId}) at ${pos}`, LOG_LEVELS.DEBUG);
        awayId = awayOverride.playerId;
      }

      const homeStats = combinedStats[homeId] || {};
      const awayStats = combinedStats[awayId] || {};

      sourceMap[homeId] = homeStats._source || "none";
      sourceMap[awayId] = awayStats._source || "none";

      // 💬 Use overrideName if available, fallback to playerLookup or replacement name
      const homeName = homeOverride?.name || overrideNames[homeId] || playerLookup[homeId] || "{Unknown}";
      const awayName = awayOverride?.name || overrideNames[awayId] || playerLookup[awayId] || "{Unknown}";

      nameMap[homeId] = homeName;
      nameMap[awayId] = awayName;

      const hStats = getPositionScore(homeStats, pos);
      const aStats = getPositionScore(awayStats, pos);

      homeScore += hStats.total;
      awayScore += aStats.total;

      const label =
        i === 0 ? "Forwards:" :
        i === 3 ? "Midfielders:" :
        i === 6 ? "Ruckman:" :
        i === 7 ? "Tackler:" : "";

      logAction(`📋 ${homeName} (${homeId}) @${pos} → ${JSON.stringify(homeStats)}`, LOG_LEVELS.DEBUG);
      logAction(`📋 ${awayName} (${awayId}) @${pos} → ${JSON.stringify(awayStats)}`, LOG_LEVELS.DEBUG);
      logAction(`🏃 ${homeName} → ${hStats.total} | ${awayName} → ${aStats.total}`, LOG_LEVELS.DEBUG);

      const homeLabel = homeName;
      const awayLabel = awayName;

      if (pos === "Interchange") {
        rows.push([
          label, homeLabel, "", "", "", "",
          label, awayLabel, "", "", "", ""
        ]);
      } else {
        rows.push([
          ...renderMatchupRow(pos, label, homeLabel, hStats),
          ...renderMatchupRow(pos, label, awayLabel, aStats)
        ]);
      }
    }

    const homeRemaining = getRemainingPlayers(teamMap, combinedStats, home, interchangeMap);
    const awayRemaining = getRemainingPlayers(teamMap, combinedStats, away, interchangeMap);

    const summary = generateMatchSummary(home, away, homeScore, awayScore, homeRemaining, awayRemaining);

    rows.push(["", "Total:", "", "", homeScore, "", "", "Total:", "", "", awayScore, ""]);
    rows.push([summary, "", "", "", "", "", "", "", "", "", "", ""]);
    rows.push(["", "", "", "", "", "", "", "", "", "", "", ""]);

    return { rows, sourceMap, nameMap };
  }

  const headersRow = weeklyData[0];
  const teamCol = headersRow.indexOf("BBBFFL Team");
  const roundCol = headersRow.indexOf("Round");

  let liveResults = [];
  let allSourceMap = {};
  let allNameMap = {};

  const teamMap = {};
  for (let i = 1; i < weeklyData.length; i++) {
    const row = weeklyData[i];
    const team = row[teamCol];
    const round = row[roundCol];
    if (round !== roundNumber) continue;

    const map = {};
    for (let j = 0; j < posCols.length; j++) {
      const id = row[4 + j];
      if (id) map[posCols[j]] = String(id);
    }
    teamMap[team] = map;
  }

  for (const [_, home, away] of fixtureData) {
    const matchResult = formatMatch(home, away, teamMap);

    if (!matchResult || !matchResult.rows) continue; // Safety check

    const { rows, sourceMap = {}, nameMap = {} } = matchResult;

    liveResults = liveResults.concat(rows);
    Object.assign(allSourceMap, sourceMap);
    Object.assign(allNameMap, nameMap);
  }

  sheet.getRange(1, 1, liveResults.length, liveResults[0].length).setValues(liveResults);
  logAction(`✅ Live BBBFFL results for Round ${roundNumber} written to '${sheetName}'`, LOG_LEVELS.INFO);

  // 🧼 Apply visual formatting to the live match sheet
  let range = sheet.getRange(1, 1, liveResults.length, liveResults[0].length);
  range.setFontFamily("Calibri");

  // Set column widths
  sheet.setColumnWidths(2, 1, 140); // Player name columns
  sheet.setColumnWidths(3, 4, 50);  // Home Gls/Bhs/Total columns
  sheet.setColumnWidths(9, 4, 50);  // Away Gls/Bhs/Total columns

  // Apply borders to the full range
  range.setBorder(true, true, true, true, true, true);

  // Apply row-specific formatting
  const totalCols = liveResults[0].length;

  for (let r = 0; r < liveResults.length; r++) {
    const rowIndex = r + 1;
    const row = liveResults[r];

    const homeNameCell = row[1]; // Column B (index 1)
    const awayNameCell = row[7]; // Column H (index 7)

    // Match IDs from nameMap (reverse lookup)
    const homeId = Object.keys(allNameMap).find(id => homeNameCell.includes(allNameMap[id]));
    const awayId = Object.keys(allNameMap).find(id => awayNameCell.includes(allNameMap[id]));

    const homeSource = allSourceMap[homeId];
    const awaySource = allSourceMap[awayId];

    if (homeSource === "override") {
      sheet.getRange(rowIndex, 2).setFontColor("red").setFontWeight("bold");
    } else if (homeSource === "live") {
      sheet.getRange(rowIndex, 2).setFontStyle("italic");
    } else if (["round", "bye"].includes(homeSource)) {
      sheet.getRange(rowIndex, 2).setFontWeight("bold");
    }
    logAction(`🎨 Formatting ${allNameMap[homeId]} [${homeSource}]`, LOG_LEVELS.DEBUG);

    if (awaySource === "override") {
      sheet.getRange(rowIndex, 8).setFontColor("red").setFontWeight("bold");
    } else if (awaySource === "live") {
      sheet.getRange(rowIndex, 8).setFontStyle("italic");
    } else if (["round", "bye"].includes(awaySource)) {
      sheet.getRange(rowIndex, 8).setFontWeight("bold");
    }
    logAction(`🎨 Formatting ${allNameMap[awayId]} [${awaySource}]`, LOG_LEVELS.DEBUG);

    if (r % 14 === 0) {
      // 🏉 Matchup title row
      sheet.getRange(rowIndex, 1, 1, 12).merge();
      sheet.getRange(rowIndex, 1)
        .setFontWeight("bold")
        .setHorizontalAlignment("center")
        .setFontSize(12)
        .setBackground("#dfe6e9");

    } else if (r % 14 === 1) {
      // 📊 Header row
      sheet.getRange(rowIndex, 1, 1, 12)
        .setFontWeight("bold")
        .setBackground("#b2bec3");

    } else if (r % 14 === 11) {
      // ➕ Total row
      sheet.getRange(rowIndex, 1, 1, 12)
        .setFontWeight("bold")
        .setBackground("#f4f4f4");

      sheet.getRange(rowIndex, 5).setBorder(true, true, true, true, true, true, "black", SpreadsheetApp.BorderStyle.SOLID_MEDIUM); // Home total
      sheet.getRange(rowIndex, 11).setBorder(true, true, true, true, true, true, "black", SpreadsheetApp.BorderStyle.SOLID_MEDIUM); // Away total
    }
  }
  updateResultsDashboard("generateLiveBBBFFLMatches", "Generated Live Round view");
}

// Helper functions

function getPositionScore(stats, pos) {
  if (!stats || typeof stats !== 'object') {
    logAction(`🔴 No stats object for ${pos}`, LOG_LEVELS.DEBUG);
    return { goals: 0, behinds: 0, total: 0 };
  }

  let total = 0;

  switch (pos) {
    case "Forward1":
    case "Forward2":
    case "Forward3":
      total = (stats.goals || 0) * 6 + (stats.behinds || 0);
      break;
    case "Midfield1":
    case "Midfield2":
    case "Midfield3":
      total = stats.disposals || 0;
      break;
    case "Ruck":
      total = (stats.marks || 0) + (stats.hitouts || 0);
      break;
    case "Tackler":
      total = (stats.tackles || 0) * 6;
      break;
    default:
      logAction(`⚠️ Unknown position '${pos}'`, "WARN");
      total = 0;
  }

  const goals = Math.floor(total / 6);
  const behinds = total % 6;

  logAction(`📊 Score for ${pos}: goals=${goals}, behinds=${behinds}, total=${total}`, LOG_LEVELS.DEBUG);

  return { goals, behinds, total };
}

function getRemainingPlayers(teamMap, statMap, teamName, interchangeMap) {
  const positions = ["Forward1", "Forward2", "Forward3", "Midfield1", "Midfield2", "Midfield3", "Ruck", "Tackler"];
  const teamPlayers = teamMap[teamName] || {};
  let remaining = 0;

  for (const pos of positions) {
    let playerId = teamPlayers[pos];

    // 👀 Check for confirmed interchange replacement
    const override = interchangeMap[teamName]?.[pos];
    if (override) {
      playerId = override.playerId;
    }

    if (playerId && !statMap[playerId]) {
      remaining++;
      logAction(`🔴 ${teamName} → ${pos} (${playerId}) → No stats found`, LOG_LEVELS.DEBUG);
    } else {
      logAction(`🟢 ${teamName} → ${pos} (${playerId}) → Stats found`, LOG_LEVELS.DEBUG);
    }
  }

  logAction(`➡️ ${teamName} has ${remaining} player(s) remaining`, LOG_LEVELS.DEBUG);
  return remaining;
}

function runLiveBBBFFLForCurrentRound() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const reviewSheet = ss.getSheetByName("Scorer Review");
  const roundCell = reviewSheet.getRange("O1").getValue();
  const roundNumber = parseInt(roundCell);

  if (!roundNumber || isNaN(roundNumber)) {
    SpreadsheetApp.getUi().alert("❌ Unable to determine current round from cell O1.");
    return;
  }

  generateLiveBBBFFLMatches(roundNumber);
  SpreadsheetApp.getUi().alert(`✅ Live BBBFFL sheet generated for Round ${roundNumber}`);
}
