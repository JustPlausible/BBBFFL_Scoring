// updateAllLiveAFLGames
// Update 1/6/2025

function updateAllLiveAFLGames() {
    var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("Game Schedule");
    if (!sheet) {
        Logger.log("⚠ No Game Schedule sheet found.");
        return;
    }

    var data = sheet.getDataRange().getValues();
    if (data.length < 2) {
        Logger.log("⚠ No data in Game Schedule.");
        return;
    }

    // Map headers to indexes for robust code
    var headers = data[0];
    var colIndex = {};
    headers.forEach(function(title, i) { colIndex[title.trim()] = i; });

    var liveStatuses = ["Q1", "Q2", "Q3", "3QT", "Q4", "HT", "QT"];
    var liveGames = [];

    for (var i = 1; i < data.length; i++) { // Skip headers
        var row = data[i];
        var status = row[colIndex["Status"]];
        var gameId = row[colIndex["Game ID"]];
        if (liveStatuses.includes(status)) {
            liveGames.push(gameId);
        }
    }

    if (liveGames.length === 0) {
        Logger.log("⚠ No live games currently running.");
        return;
    }

    Logger.log("🔄 Live games found: " + liveGames.join(", "));
    fetchLiveAFLPlayerStats(liveGames); // ← This is now your main action!
}
