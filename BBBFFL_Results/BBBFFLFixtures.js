// Updated 29/3/2025
// Generates a full season (1-24) of fixtures based on a provided structure
function generateBBBFFLFixtures() {
  var config = getConfig(); // Fetch sheet IDs from config
  var ss = SpreadsheetApp.openById(config.bbbfflResultsSheetId);
  
  var drawSheet = ss.getSheetByName("Fixture Draw"); // Random draw stored here
  var fixtureSheet = ss.getSheetByName("2025 Fixtures");

  if (!fixtureSheet) {
    fixtureSheet = ss.insertSheet("2025 Fixtures");
  } else {
    fixtureSheet.clear();
  }

  fixtureSheet.appendRow(["Round", "Home Team", "Away Team"]);

  // Fetch teams in random draw order
  var drawData = drawSheet.getRange("B1:B10").getValues().flat();
  if (drawData.length !== 10) {
    Logger.log("⚠️ Error: Expected 10 teams in the draw.");
    return;
  }

  var totalRounds = 20; // Can be increased if needed

  // **Define the fixed fixture placements from 2024 structure**
  var baseFixtures = [
    [1, [1, 2], [3, 4], [5, 6], [7, 8], [9, 10]],
    [2, [1, 3], [2, 8], [9, 6], [7, 4], [10, 5]],
    [3, [1, 4], [2, 6], [3, 5], [8, 10], [7, 9]],
    [4, [1, 6], [2, 4], [5, 7], [3, 10], [8, 9]],
    [5, [1, 5], [2, 7], [4, 10], [3, 9], [6, 8]],
    [6, [1, 7], [2, 9], [4, 5], [6, 10], [3, 8]],
    [7, [1, 8], [2, 10], [4, 6], [3, 7], [5, 9]],
    [8, [1, 9], [2, 5], [3, 8], [4, 6], [7, 10]],
    [9, [1, 10], [2, 3], [4, 9], [5, 8], [6, 7]]
  ];

  // **Generate Rounds 1-9 using the base structure**
  var firstHalfFixtures = [];
  
  baseFixtures.forEach(round => {
    var roundNumber = round[0];
    round.slice(1).forEach(match => {
      var homeTeam = drawData[match[0] - 1];
      var awayTeam = drawData[match[1] - 1];
      fixtureSheet.appendRow([roundNumber, homeTeam, awayTeam]);
      firstHalfFixtures.push([roundNumber, homeTeam, awayTeam]);
    });
  });

  // **Generate Rounds 10-18 by mirroring Rounds 1-9**
  var secondHalfFixtures = [];
  firstHalfFixtures.forEach((match, index) => {
    var roundNumber = match[0] + 9;
    var homeTeam = match[2]; // Swap home/away
    var awayTeam = match[1];
    fixtureSheet.appendRow([roundNumber, homeTeam, awayTeam]);
    secondHalfFixtures.push([roundNumber, homeTeam, awayTeam]);
  });

  // **Generate Rounds 19+ by cycling through earlier rounds**
  var totalGeneratedRounds = 18;
  while (totalGeneratedRounds < totalRounds) {
    var roundNumber = totalGeneratedRounds + 1;
    var matchIndex = (roundNumber - 19) % baseFixtures.length; // Loop through base rounds
    baseFixtures[matchIndex].slice(1).forEach(match => {
      var homeTeam = drawData[match[0] - 1]; // Same matchups
      var awayTeam = drawData[match[1] - 1];
      fixtureSheet.appendRow([roundNumber, homeTeam, awayTeam]);
    });
    totalGeneratedRounds++;
  }

  Logger.log("✅ 2025 BBBFFL Fixtures Generated Successfully! " + totalRounds + " Rounds Completed.");
  updateResultsDashboard("generateBBBFFLFixtures", "Generated a full season of fixtures");
}
