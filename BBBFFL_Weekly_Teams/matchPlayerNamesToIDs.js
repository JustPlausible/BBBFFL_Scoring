function matchPlayerNamesToIDs() {
    var config = getConfig();
    var draftSheet = SpreadsheetApp.openById(config.bbbfflListsSheetId).getSheetByName("2025 Draft List");
    var weeklyTeamsSheet = SpreadsheetApp.openById(config.bbbfflWeeklyTeamsSheetId).getSheetByName("Form Responses 12"); // Update if necessary

    var draftData = draftSheet.getDataRange().getValues();
    var weeklyData = weeklyTeamsSheet.getDataRange().getValues();

    // Create a mapping of Player Name → Player ID
    var playerMap = {};
    for (var i = 1; i < draftData.length; i++) { // Skip header row
        var playerName = draftData[i][3].trim().toLowerCase(); // Column D = Player Name
        var playerId = draftData[i][5]; // Column F = Player ID
        if (playerName) playerMap[playerName] = playerId;
    }

    // Get the headers and find player name column indexes
    var headers = weeklyData[0];
    var newHeaders = headers.slice(); // Copy headers for modification
    var positionColumns = {};
    
    var positions = ["Forward1", "Forward2", "Forward3", "Midfield1", "Midfield2", "Midfield3", "Ruck", "Tackler", "Interchange"];
    
    positions.forEach(pos => {
        var colIndex = headers.indexOf(pos);
        if (colIndex !== -1) {
            positionColumns[pos] = colIndex;
            var idColName = pos + " ID";
            if (!headers.includes(idColName)) {
                newHeaders.splice(colIndex + 1, 0, idColName); // Insert new ID column immediately after the name column
            }
        }
    });

    // Update headers if necessary
    if (newHeaders.length > headers.length) {
        weeklyTeamsSheet.getRange(1, 1, 1, newHeaders.length).setValues([newHeaders]);
    }

    // Process each row and insert Player IDs
    var updatedRows = [];
    for (var i = 1; i < weeklyData.length; i++) { // Skip header row
        var rowValues = weeklyData[i].slice(); // Copy row data
        var rowUpdated = false;

        Object.keys(positionColumns).forEach(pos => {
            var colIndex = positionColumns[pos];
            var playerName = rowValues[colIndex]?.trim().toLowerCase() || "";

            if (playerName && playerMap[playerName]) {
                rowValues.splice(colIndex + 1, 0, playerMap[playerName]); // Insert ID right after the name
                rowUpdated = true;
            } else {
                rowValues.splice(colIndex + 1, 0, ""); // Keep the ID column empty if no match
            }
        });

        if (rowUpdated) {
            weeklyTeamsSheet.getRange(i + 1, 1, 1, rowValues.length).setValues([rowValues]); // Update row
            updatedRows.push(i + 1);
        }
    }

    if (updatedRows.length > 0) {
        Logger.log("✅ Player IDs correctly placed for rows: " + updatedRows.join(", "));
    } else {
        Logger.log("⚠ No matching player names found to update.");
    }
}
