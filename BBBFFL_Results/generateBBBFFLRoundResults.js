// Updated 29/3/2025
// Displays the match results based on data in Master Results and the fixtures

function generateBBBFFLRoundResults(roundNumber) {
  var config = getConfig(); // Fetch sheet config
  var ss = SpreadsheetApp.openById(config.bbbfflResultsSheetId);
  
  var sheetName = "Rnd " + roundNumber;
  var sheet = ss.getSheetByName(sheetName);
  if (!sheet) {
    sheet = ss.insertSheet(sheetName);
  } else {
    sheet.clear();
  }
  
  // Fetch fixture data
  var fixtureSheet = ss.getSheetByName("2025 Fixtures");
  if (!fixtureSheet) {
    Logger.log("❌ Error: '2025 Fixtures' sheet not found.");
    return;
  }

  var fixtureData = fixtureSheet.getDataRange().getValues();
  var matchups = fixtureData.filter(row => row[0] == roundNumber); // Find matches for this round

  if (matchups.length !== 5) {
    Logger.log("⚠️ Warning: Expected 5 matches for Round " + roundNumber + " but found " + matchups.length);
  }

  // Fetch player data from Master Results
  var resultsSheet = ss.getSheetByName("Master Results");
  if (!resultsSheet) {
    Logger.log("❌ Error: 'Master Results' sheet not found.");
    return;
  }

  var resultsData = resultsSheet.getDataRange().getValues();
  var playerStats = {}; // Store player data by team & position

  for (var i = 1; i < resultsData.length; i++) { // Skip headers
    var teamName = resultsData[i][1]; // Column B = BBBFFL Team
    var round = resultsData[i][0]; // Column A = Round

    if (round == roundNumber) { // Only get players for this round
      playerStats[teamName] = {
        "Forward1": { id: resultsData[i][2], score: resultsData[i][3], status: resultsData[i][4] },
        "Forward2": { id: resultsData[i][5], score: resultsData[i][6], status: resultsData[i][7] },
        "Forward3": { id: resultsData[i][8], score: resultsData[i][9], status: resultsData[i][10] },
        "Midfield1": { id: resultsData[i][11], score: resultsData[i][12], status: resultsData[i][13] },
        "Midfield2": { id: resultsData[i][14], score: resultsData[i][15], status: resultsData[i][16] },
        "Midfield3": { id: resultsData[i][17], score: resultsData[i][18], status: resultsData[i][19] },
        "Ruck":      { id: resultsData[i][20], score: resultsData[i][21], status: resultsData[i][22] },
        "Tackler":   { id: resultsData[i][23], score: resultsData[i][24], status: resultsData[i][25] },
        "Interchange": {
          id: resultsData[i][26],
          score: resultsData[i][27],     // This is "I Score" – sometimes blank
          status: resultsData[i][28]     // This is "I Status" – e.g., "Midfield3" if used
        }
      };
    }
  }

  // Fetch Local Overrides
  var overrideSheet = ss.getSheetByName("Local Overrides");
  var overrideLookup = {};

  if (overrideSheet) {
      var overrideData = overrideSheet.getDataRange().getValues();
      for (var i = 1; i < overrideData.length; i++) { // Skip headers
        var playerId = overrideData[i][2]; // Column C = Player ID
        var playerName = overrideData[i][3]; // Column D = Player Name
        overrideLookup[playerId] = playerName; // Store overrides by Player ID
      }
      Logger.log("✅ Loaded " + (overrideData.length - 1) + " overrides");
  } else {
      Logger.log("⚠️ No 'Local Overrides' sheet found.");
  }

  // Fetch player names using IDs from AFL Player Stats
  var aflStatsSS = SpreadsheetApp.openById(config.aflStatsSheetId);
  var playerSheet = aflStatsSS.getSheetByName("Player Names");
  if (!playerSheet) {
    Logger.log("❌ Error: 'Player Names' sheet not found in AFL Stats.");
    return;
  }

  var playerData = playerSheet.getDataRange().getValues();
  var playerLookup = {}; // Store player names by ID

  for (var i = 1; i < playerData.length; i++) { // Skip headers
      playerLookup[playerData[i][0]] = playerData[i][1]; // ID → Name mapping
  }

  // Define column structure
  var headers = ["", "", "Gls", "Bhs", "Total", "", "", "", "Gls", "Bhs", "Total", ""];
  var positions = ["Forward1", "Forward2", "Forward3", "Midfield1", "Midfield2", "Midfield3", "Ruck", "Tackler", "Interchange"];
  
  // Match template (one match)
  function createMatchTemplate(homeTeam, awayTeam) {
    const homeUsed = playerStats[homeTeam]?.Interchange?.status;
    const awayUsed = playerStats[awayTeam]?.Interchange?.status;

    //Logger.log(`🏠 ${homeTeam}: Interchange status: ${homeUsed}`);
    //Logger.log(`🛫 ${awayTeam}: Interchange status: ${awayUsed}`);

    let match = [];
    function shortPositionLabel(pos) {
      const map = {
        "Forward1": "For1",
        "Forward2": "For2",
        "Forward3": "For3",
        "Midfield1": "Mid1",
        "Midfield2": "Mid2",
        "Midfield3": "Mid3",
        "Ruck": "Ruck",
        "Tackler": "Tack"
      };
      return map[pos] || "";
    }

    let homeTotalGoals = 0, homeTotalBehinds = 0, homeTotalScore = 0;
    let awayTotalGoals = 0, awayTotalBehinds = 0, awayTotalScore = 0;
    let homeForwardSubtotal = 0, awayForwardSubtotal = 0;
    let homeMidfieldSubtotal = 0, awayMidfieldSubtotal = 0;

    match.push([`${homeTeam} v ${awayTeam}`, "", "", "", "", "", "", "", "", "", "", ""]);
    match.push(headers);
    
    positions.forEach((pos, index) => {
      let homePlayerID = playerStats[homeTeam] ? playerStats[homeTeam][pos].id : "";
      let awayPlayerID = playerStats[awayTeam] ? playerStats[awayTeam][pos].id : "";

      // **Use Local Override if available**
      //let homePlayer = overrideLookup[homePlayerID] || playerLookup[homePlayerID] || "{No Player}";
      //let awayPlayer = overrideLookup[awayPlayerID] || playerLookup[awayPlayerID] || "{No Player}";
      // Suppress "{No Player}" if Interchange covered this position
      let homeIRStatus = playerStats[homeTeam]?.Interchange?.status;
      let awayIRStatus = playerStats[awayTeam]?.Interchange?.status;

      let homePlayer =
        overrideLookup[homePlayerID] || playerLookup[homePlayerID] ||
        (homeIRStatus === pos ? "↪ Interchange" : "{No Player}");

      let awayPlayer =
        overrideLookup[awayPlayerID] || playerLookup[awayPlayerID] ||
        (awayIRStatus === pos ? "↪ Interchange" : "{No Player}");

      let homeScore = playerStats[homeTeam] ? playerStats[homeTeam][pos].score || 0 : 0;
      let awayScore = playerStats[awayTeam] ? playerStats[awayTeam][pos].score || 0 : 0;

      let homeGoals = Math.floor(homeScore / 6);
      let homeBehinds = homeScore % 6;
      let awayGoals = Math.floor(awayScore / 6);
      let awayBehinds = awayScore % 6;

      homeTotalGoals += homeGoals;
      homeTotalBehinds += homeBehinds;
      homeTotalScore += homeScore;

      awayTotalGoals += awayGoals;
      awayTotalBehinds += awayBehinds;
      awayTotalScore += awayScore;

      if (["Forward1", "Forward2", "Forward3"].includes(pos)) {
        homeForwardSubtotal += homeScore;
        awayForwardSubtotal += awayScore;
      }

      if (["Midfield1", "Midfield2", "Midfield3"].includes(pos)) {
        homeMidfieldSubtotal += homeScore;
        awayMidfieldSubtotal += awayScore;
      }

      //let homeSubtotal = (pos === "Forward3") ? homeForwardSubtotal : (pos === "Midfield3") ? homeMidfieldSubtotal : "";
      //let awaySubtotal = (pos === "Forward3") ? awayForwardSubtotal : (pos === "Midfield3") ? awayMidfieldSubtotal : "";
      let homeSubtotal = (index === 2) ? homeForwardSubtotal : (index === 5) ? homeMidfieldSubtotal : "";
      let awaySubtotal = (index === 2) ? awayForwardSubtotal : (index === 5) ? awayMidfieldSubtotal : "";

      if (pos === "Interchange") {
        // Display only name + position replaced (as per IR system)
        const homeIR = playerStats[homeTeam]?.Interchange || {};
        const awayIR = playerStats[awayTeam]?.Interchange || {};

        const homeIRName = overrideLookup[homeIR.id] || playerLookup[homeIR.id] || "";
        const awayIRName = overrideLookup[awayIR.id] || playerLookup[awayIR.id] || "";

        // Only show short status (e.g., "Mid2") if IR was used
        const homeIRStatus = homeIR.status ? shortPositionLabel(homeIR.status) : "";
        const awayIRStatus = awayIR.status ? shortPositionLabel(awayIR.status) : "";

        match.push([
            "Interchange:", homeIRName, "", "", "", homeIRStatus,
            "Interchange:", awayIRName, "", "", "", awayIRStatus
        ]);
      } else {
        //match.push([pos, homePlayer, homeGoals, homeBehinds, homeScore, homeSubtotal,
        //            pos, awayPlayer, awayGoals, awayBehinds, awayScore, awaySubtotal]);
        const homePosLabel =
          index === 0 ? "Forwards:" :
          index === 3 ? "Midfielders:" :
          index === 6 ? "Ruckman:" :
          index === 7 ? "Tackler:" :
          (index === 1 || index === 2 || index === 4 || index === 5) ? "" :
          pos;

        const awayPosLabel =
          index === 0 ? "Forwards:" :
          index === 3 ? "Midfielders:" :
          index === 6 ? "Ruckman:" :
          index === 7 ? "Tackler:" :
          (index === 1 || index === 2 || index === 4 || index === 5) ? "" :
          pos;

        match.push([
          homePosLabel, homePlayer, homeGoals, homeBehinds, homeScore, homeSubtotal,
          awayPosLabel, awayPlayer, awayGoals, awayBehinds, awayScore, awaySubtotal
        ]);
      }
    });

    match.push(["", "Total:", homeTotalGoals, homeTotalBehinds, homeTotalScore, "", "", "Total:", awayTotalGoals, awayTotalBehinds, awayTotalScore, ""]);

    let result = homeTotalScore > awayTotalScore ? "W" : homeTotalScore < awayTotalScore ? "L" : "D";
    let awayResult = result === "W" ? "L" : result === "L" ? "W" : "D";

    match.push(["", "", "", "", result, "", "", "", "", "", awayResult, ""]);
    match.push(["", "", "", "", "", "", "", "", "", "", "", ""]);
    return match;
  }

  let matchupRowMap = []; // [{ rowStart, homeTeam, awayTeam }]
  let currentRow = 1;

  matchups.forEach(match => {
    const [round, homeTeam, awayTeam] = match;
    const matchRows = createMatchTemplate(homeTeam, awayTeam); // returns array of rows

    matchupRowMap.push({ rowStart: currentRow, homeTeam, awayTeam });

    currentRow += matchRows.length;
  });

  let allMatches = matchups.flatMap(match => createMatchTemplate(match[1], match[2]));

  sheet.getRange(1, 1, allMatches.length, allMatches[0].length).setValues(allMatches);

  Logger.log("✅ Round " + roundNumber + " Results Generated with Overrides!");

  // Apply formatting after writing values
  let range = sheet.getRange(1, 1, allMatches.length, allMatches[0].length);
  range.setFontFamily("Calibri"); // Clean, readable font
  sheet.setColumnWidths(2, 1, 140); // Name columns
  sheet.setColumnWidths(3, 4, 50);  // Gls/Bhs/Total/Subtotals columns
  sheet.setColumnWidths(9, 4, 50);  // Gls/Bhs/Total/Subtotals on away side

  // Apply borders to the full range
  range.setBorder(true, true, true, true, true, true);

  for (let r = 0; r < allMatches.length; r++) {
    const rowIndex = r + 1;

    // 🆕 Highlight replaced players
    const matchup = matchupRowMap.find(m => rowIndex >= m.rowStart && rowIndex < m.rowStart + 14);

    if (matchup) {
      const positionRowMap = {
        "Forward1": 2, "Forward2": 3, "Forward3": 4,
        "Midfield1": 5, "Midfield2": 6, "Midfield3": 7,
        "Ruck": 8, "Tackler": 9
      };

      const homeIR = playerStats[matchup.homeTeam]?.Interchange || {};
      const awayIR = playerStats[matchup.awayTeam]?.Interchange || {};

      for (const [position, relRow] of Object.entries(positionRowMap)) {
        const absoluteRow = matchup.rowStart + relRow;
        if (rowIndex === absoluteRow) {
          if (homeIR.status === position) {
            sheet.getRange(rowIndex, 2).setFontStyle("italic").setFontColor("#888888"); // Home replaced player
          }
          if (awayIR.status === position) {
            sheet.getRange(rowIndex, 8).setFontStyle("italic").setFontColor("#888888"); // Away replaced player
          }

          // 🆕 DNP-specific highlighting (I don't think we're getting DNP sent through, so this doesn't currently work)
          //Logger.log(`[DEBUG] playerStats[${matchup.homeTeam}][${position}] → ${JSON.stringify(playerStats[matchup.homeTeam]?.[position])}`);

          const homeStatus = playerStats[matchup.homeTeam]?.[position]?.status;
          const awayStatus = playerStats[matchup.awayTeam]?.[position]?.status;

          if (homeStatus === "DNP") {
            sheet.getRange(rowIndex, 2).setFontStyle("italic").setFontColor("#888888");
          }
          if (awayStatus === "DNP") {
            sheet.getRange(rowIndex, 8).setFontStyle("italic").setFontColor("#888888");
          }            
        }
      }
    }

    if (r % 14 === 0) {
      // 🏉 Matchup title row – merge across all 12 columns
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
      // ➕ Totals row
      sheet.getRange(rowIndex, 1, 1, 12)
        .setFontWeight("bold")
        .setBackground("#f4f4f4");
        sheet.getRange(rowIndex, 5).setBorder(true, true, true, true, true, true, "black", SpreadsheetApp.BorderStyle.SOLID_MEDIUM); // Home total
        sheet.getRange(rowIndex, 11).setBorder(true, true, true, true, true, true, "black", SpreadsheetApp.BorderStyle.SOLID_MEDIUM); // Away total
    } else if (r % 14 === 12) {
      // ✅ Result row (W/L/D)
      const homeResult = sheet.getRange(rowIndex, 5).getValue();
      const awayResult = sheet.getRange(rowIndex, 11).getValue();

      // Home result colour
      const homeColor = homeResult === "W" ? "#a9dfbf" : homeResult === "L" ? "#f5b7b1" : "#d5dbdb";
      const awayColor = awayResult === "W" ? "#a9dfbf" : awayResult === "L" ? "#f5b7b1" : "#d5dbdb";

      sheet.getRange(rowIndex, 5).setBackground(homeColor).setFontWeight("bold").setHorizontalAlignment("center");
      sheet.getRange(rowIndex, 11).setBackground(awayColor).setFontWeight("bold").setHorizontalAlignment("center");
    } else if (allMatches[r][0] === "Interchange:") {
      // 🔁 Interchange row
      // Determine which matchup block this row belongs to
      let matchup = matchupRowMap.find(m => r + 1 >= m.rowStart && r + 1 < m.rowStart + 14);

      const homeTeamName = matchup?.homeTeam;
      const awayTeamName = matchup?.awayTeam;
      const validPositions = ["Forward1", "Forward2", "Forward3", "Midfield1", "Midfield2", "Midfield3", "Ruck", "Tackler"];

      const homeUsed = playerStats[homeTeamName]?.Interchange?.status;
      const awayUsed = playerStats[awayTeamName]?.Interchange?.status;

      if (validPositions.includes(homeUsed)) {
        sheet.getRange(rowIndex, 2).setFontColor("red");
      }
      if (validPositions.includes(awayUsed)) {
        sheet.getRange(rowIndex, 8).setFontColor("red");
      }
    }
    if ([4, 7].includes(r % 14)) {
      sheet.getRange(rowIndex, 6).setFontColor("red"); // Home subtotal
      sheet.getRange(rowIndex, 12).setFontColor("red"); // Away subtotal
    }
    // 🎯 DNP formatting (grey, italic)
    const dnpMatchup = matchupRowMap.find(m => rowIndex >= m.rowStart && rowIndex < m.rowStart + 14);
    if (dnpMatchup && allMatches[r][0] !== "Interchange:") {
      const homeTeam = dnpMatchup.homeTeam;
      const awayTeam = dnpMatchup.awayTeam;

      const homePlayer = allMatches[r][1];
      const homeStatus = allMatches[r][4];
      const awayPlayer = allMatches[r][7];
      const awayStatus = allMatches[r][10];
      //Logger.log(`🔍 Row ${r}: Home status = ${homeStatus}, Away status = ${awayStatus}`);


      if (homeStatus === "DNP" && homePlayer !== "{No Player}") {
        sheet.getRange(rowIndex, 2).setFontStyle("italic").setFontColor("#888888"); // Home DNP player
      }
      if (awayStatus === "DNP" && awayPlayer !== "{No Player}") {
        sheet.getRange(rowIndex, 8).setFontStyle("italic").setFontColor("#888888"); // Away DNP player
      }
    }
  }
  updateResultsDashboard("generateBBBFFLRoundResults", "Generated Final Round Results");
}
