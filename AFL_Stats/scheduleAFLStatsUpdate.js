function scheduleAFLStatsUpdate() {
    var gameSheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName('Game Schedule');
    var spreadsheet = SpreadsheetApp.getActiveSpreadsheet();
    var data = gameSheet.getDataRange().getValues();
    var roundsToFetch = [];
    var roundsPending = [];

    // Find rounds that have finished but haven't been fetched yet
    for (var i = 1; i < data.length; i++) { // Skip header row
        var round = data[i][3]; // Column D: Round
        var status = data[i][7]; // Column H: Status
        var gameId = data[i][0]; // Column A: Game ID

        var roundSheetName = "Round " + round;
        var existingSheet = spreadsheet.getSheetByName(roundSheetName);

        if (status == "Finished" && !existingSheet && !roundsToFetch.includes(round)) {
            roundsToFetch.push(round);
        } else if (status != "Finished" && !roundsPending.includes(round)) {
            roundsPending.push(round);
        }
    }

    // Process up to 2 new rounds that haven't been fetched yet
    var roundsToProcess = roundsToFetch.slice(0, 2);

    if (roundsToProcess.length > 0) {
        roundsToProcess.forEach(function(roundNumber) {
            fetchAFL2025RoundStats(roundNumber);
        });
    } else {
        Logger.log("No new finished rounds to process.");
    }

    // If there are unfinished games, re-schedule the script
    if (roundsPending.length > 0) {
        ScriptApp.newTrigger("scheduleAFLStatsUpdate")
            .timeBased()
            .after(6 * 60 * 60 * 1000) // Run again in 6 hours
            .create();
        Logger.log("Re-scheduled next check in 6 hours.");
    } else {
        Logger.log("All rounds completed. Stopping scheduled runs.");
    }
}
