// Updated 29/3/2025
// Generates the BBBFFL Ladder

function generateBBBFFLLadder() {
  var config = getConfig(); // Fetch sheet config
  var ss = SpreadsheetApp.openById(config.bbbfflResultsSheetId);

  // Fetch fixture and results sheets
  var fixtureSheet = ss.getSheetByName("2025 Fixtures");
  var resultsSheet = ss.getSheetByName("Master Results");
  var ladderSheet = ss.getSheetByName("Ladder");

  if (!fixtureSheet || !resultsSheet || !ladderSheet) {
      Logger.log("❌ Error: Missing necessary sheets.");
      return;
  }

  Logger.log("🔄 Generating BBBFFL Ladder & Round Wins Table...");

  // Load fixture and results data
  var fixtureData = fixtureSheet.getDataRange().getValues();
  var resultsData = resultsSheet.getDataRange().getValues();
  const maxCompletedRound = getMaxCompletedRound(resultsData);
  Logger.log(`📆 Max Completed Round: ${maxCompletedRound}`);

  // Track team performance
  var teamStats = {};
  var roundWins = {}; // Track wins per round

  // Process each fixture to determine match results
  for (var i = 1; i < fixtureData.length; i++) {
    var round = fixtureData[i][0]; // Column A = Round

    if (round > maxCompletedRound) {
      Logger.log(`⏩ Skipping future round ${round}`);
      continue;
    }

    var homeTeam = fixtureData[i][1]; // Column B = Home Team
    var awayTeam = fixtureData[i][2]; // Column C = Away Team

    // Retrieve scores from Master Results
    var homeScore = null;
    var awayScore = null;
    var homeStatus = null;
    var awayStatus = null;

    for (var j = 1; j < resultsData.length; j++) {
      if (resultsData[j][0] == round) {
        if (resultsData[j][1] == homeTeam) {
          homeScore = resultsData[j][29]; // Column 30 = Total Score
          homeStatus = resultsData[j][30]; // Column 31 = Team Status (FT)
        }
        if (resultsData[j][1] == awayTeam) {
          awayScore = resultsData[j][29]; // Column 30 = Total Score
          awayStatus = resultsData[j][30]; // Column 31 = Team Status (FT)
        }
      }
    }

    // Skip if any match is incomplete
    if (homeStatus !== "✔️ FT" || awayStatus !== "✔️ FT") {
      Logger.log(`🔎 Debug: homeStatus=${homeStatus}, awayStatus=${awayStatus}, round=${round}`);
      Logger.log(`⚠️ Round ${round}: Match between ${homeTeam} and ${awayTeam} is incomplete. Skipping.`);
      continue;
    }

    // Initialize teams in objects
    if (!teamStats[homeTeam]) teamStats[homeTeam] = { P: 0, W: 0, L: 0, D: 0, PF: 0, PA: 0, PTS: 0 };
    if (!teamStats[awayTeam]) teamStats[awayTeam] = { P: 0, W: 0, L: 0, D: 0, PF: 0, PA: 0, PTS: 0 };
    if (!roundWins[homeTeam]) roundWins[homeTeam] = {};
    if (!roundWins[awayTeam]) roundWins[awayTeam] = {};

    // Update stats
    teamStats[homeTeam].P++;
    teamStats[awayTeam].P++;
    teamStats[homeTeam].PF += homeScore;
    teamStats[homeTeam].PA += awayScore;
    teamStats[awayTeam].PF += awayScore;
    teamStats[awayTeam].PA += homeScore;

    if (homeScore > awayScore) {
      teamStats[homeTeam].W++;
      teamStats[awayTeam].L++;
      teamStats[homeTeam].PTS += 4;
      roundWins[homeTeam][round] = "W"; // Mark round win
    } else if (homeScore < awayScore) {
      teamStats[awayTeam].W++;
      teamStats[homeTeam].L++;
      teamStats[awayTeam].PTS += 4;
      roundWins[awayTeam][round] = "W"; // Mark round win
    } else {
      teamStats[homeTeam].D++;
      teamStats[awayTeam].D++;
      teamStats[homeTeam].PTS += 2;
      teamStats[awayTeam].PTS += 2;
    }
  }

  // Convert stats object into array
  var ladderArray = [];
  for (var team in teamStats) {
    var stats = teamStats[team];
    var ppgAvg = (stats.PF / stats.P).toFixed(1); // One decimal place
    var percentage = stats.PA > 0 ? ((stats.PF / stats.PA) * 100).toFixed(2) : "0.00"; // Two decimal places
    
    ladderArray.push([
      team, stats.P, stats.W, stats.L, stats.D, stats.PF, stats.PA, 
      ppgAvg, percentage, stats.PTS
    ]);
  }

  // Sort ladder by PTS, then %, then PF
  ladderArray.sort((a, b) => {
    if (b[9] !== a[9]) return b[9] - a[9]; // Sort by PTS
    if (b[8] !== a[8]) return b[8] - a[8]; // Sort by %
    return b[5] - a[5]; // Sort by PF
  });

  // Add position numbers
  ladderArray = ladderArray.map((row, index) => [index + 1, ...row]);

  // Clear ladder data (excluding headers)
  var startRow = 4;
  var startCol = 2;
  var numRows = ladderSheet.getLastRow() - startRow + 1;
  var numCols = 10;

  if (numRows > 0) {
    ladderSheet.getRange(startRow, startCol, numRows, numCols).clearContent();
  }

  // Insert ladder data into C4
  ladderSheet.getRange(startRow, startCol, ladderArray.length, ladderArray[0].length).setValues(ladderArray);

  Logger.log("✅ BBBFFL Ladder Updated!");

  // 🔹 **CREATE ROUND WIN TABLE (O3)**
  var numRounds = 20; // Adjust if necessary
  var roundHeaders = ["Team"].concat([...Array(numRounds).keys()].map(r => r + 1));
  var roundWinsArray = [roundHeaders];

  // **Create win matrix in same order as ladder**
  ladderArray.forEach(row => {
    var team = row[1]; // Team name from ladder
    var winRow = [team];

    for (var r = 1; r <= numRounds; r++) {
      winRow.push(roundWins[team] && roundWins[team][r] ? "W" : ""); // Mark 'W' if team won that round
    }

    roundWinsArray.push(winRow);
  });

  // **Clear previous win table before inserting**
  var winStartRow = 3;
  var winStartCol = 14; // Column 'O'
  var winNumRows = ladderSheet.getLastRow() - winStartRow + 1;
  var winNumCols = numRounds + 1;

  if (winNumRows > 0) {
    ladderSheet.getRange(winStartRow, winStartCol, winNumRows, winNumCols).clearContent();
  }

  // **Insert new win table at O3**
  ladderSheet.getRange(winStartRow, winStartCol, roundWinsArray.length, roundWinsArray[0].length).setValues(roundWinsArray);

  Logger.log("✅ Round Wins Table Updated at O3!");
  updateResultsDashboard("generateBBBFFLLadder", "Generated BBBFFL Ladder");
}

function getMaxCompletedRound(resultsData) {
  const roundStatusMap = {};

  for (let i = 1; i < resultsData.length; i++) {
    const round = resultsData[i][0];
    const status = resultsData[i][30]; // Column 31 = ✔️ FT

    if (!roundStatusMap[round]) roundStatusMap[round] = { completeCount: 0 };
    if (status === "✔️ FT") roundStatusMap[round].completeCount++;
  }

  // Identify max round with 10 complete team results
  let maxRound = 0;
  for (const round in roundStatusMap) {
    if (roundStatusMap[round].completeCount === 10) {
      maxRound = Math.max(maxRound, parseInt(round));
    }
  }

  return maxRound;
}
