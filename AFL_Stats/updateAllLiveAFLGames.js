function updateAllLiveAFLGames() {
    var config = getConfig();
    var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("Game Schedule");

    if (!sheet) {
        Logger.log("⚠ No Game Schedule sheet found.");
        return;
    }

    var data = sheet.getDataRange().getValues();
    var liveStatuses = ["Q1", "Q2", "Q3", "Q4", "HT", "QT"]; // Live game statuses
    var liveGames = [];

    for (var i = 1; i < data.length; i++) { // Skip headers
        var gameId = data[i][0]; // Game ID
        var status = data[i][7]; // Status column

        if (liveStatuses.includes(status)) {
            liveGames.push(gameId);
        }
    }

    if (liveGames.length === 0) {
        Logger.log("⚠ No live games currently running.");
        return;
    }

    liveGames.forEach(function(gameId) {
        Logger.log("🔄 Updating live stats for Game ID: " + gameId);
        updateAFLGameStatus(gameId); // Calls your existing function
    });

    Logger.log("✅ All live games updated!");
}
