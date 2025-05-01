function fetchAFLPlayerNames() {
    var config = getConfig(); 
    var apiKey = config.apiKey;
    var seasonYear = config.seasonYear;

    var playerSheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("Player Names");
    var teamSheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("Teams");

    if (!playerSheet) {
        playerSheet = SpreadsheetApp.getActiveSpreadsheet().insertSheet("Player Names");
    }

    // ✅ Preserve Headers
    playerSheet.clear();
    playerSheet.appendRow(["Player ID", "Full Name", "First Name", "Surname", "AFL Team"]);

    // ✅ Load all teams into a lookup table
    var teams = teamSheet.getDataRange().getValues();
    var teamShortNames = {};

    for (var i = 1; i < teams.length; i++) { // Skip header row
        var teamId = teams[i][0];   // Column A (Team ID)
        var shortName = teams[i][2]; // Column C (Short Name)
        if (teamId) {
            teamShortNames[teamId] = shortName || "N/A"; // Default to "N/A" if missing
        }
    }

    // ✅ Store batch data
    var playerDataBatch = [];

    for (var teamId in teamShortNames) {
        var url = `https://v1.afl.api-sports.io/players?team=${teamId}&season=${seasonYear}`;
        var options = {
            'method': 'GET',
            'headers': {
                'x-apisports-key': apiKey
            }
        };

        try {
            var response = UrlFetchApp.fetch(url, options);

            // ✅ Check API quota before proceeding
            if (!checkApiQuota(response)) return;

            var data = JSON.parse(response.getContentText());
            if (!data.response || data.response.length === 0) {
                Logger.log(`⚠️ No players found for Team ID ${teamId}.`);
                continue;
            }

            data.response.forEach(function(player) {
                var fullName = player.name;
                var nameParts = splitName(fullName); // Call function to split name

                playerDataBatch.push([
                    player.id,
                    fullName,
                    nameParts.firstName,
                    nameParts.surname,
                    teamShortNames[teamId] // Use short team name
                ]);
            });

            Logger.log(`✅ Fetched ${data.response.length} players for Team ID ${teamId}.`);
        } catch (e) {
            Logger.log(`❌ Error fetching players for Team ID ${teamId}: ${e.toString()}`);
        }
    }

    // ✅ Batch write all player data at once
    if (playerDataBatch.length > 0) {
        playerSheet.getRange(2, 1, playerDataBatch.length, playerDataBatch[0].length).setValues(playerDataBatch);
        Logger.log(`✅ Successfully updated ${playerDataBatch.length} players.`);
    } else {
        Logger.log("⚠️ No player data was updated.");
    }
  updateDashboard("fetchAFLPlayerNames", "Updated Player Names");
}

/**
 * Splits a full name into first name and surname.
 * Handles cases like "Jacob van Rooyen" and "Bailey J. Williams".
 */
function splitName(fullName) {
    var parts = fullName.split(" ");
    
    if (parts.length === 2) {
        return { firstName: parts[0], surname: parts[1] }; // Simple case (First Last)
    } else if (parts.length > 2) {
        // If there's a middle initial, keep it with the first name
        if (parts[1].length === 2 && parts[1].endsWith(".")) {
            return { firstName: parts[0] + " " + parts[1], surname: parts.slice(2).join(" ") };
        } else {
            return { firstName: parts[0], surname: parts.slice(1).join(" ") }; // Assume rest is surname
        }
    }
    return { firstName: fullName, surname: "" }; // Default case (if only one name found)
}
