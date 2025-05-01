//Updated 17/3/2025
function fetchAFLGameSchedule() {
    var config = getConfig();
    var apiKey = config.apiKey;
    var seasonYear = config.seasonYear;

    var url = 'https://v1.afl.api-sports.io/games?league=1&season=' + seasonYear;
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
            Logger.log("⚠️ No data received from API. Skipping update.");
            return;
        }

        var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName('Game Schedule');
        if (!sheet) {
            sheet = SpreadsheetApp.getActiveSpreadsheet().insertSheet('Game Schedule');
        }

        var headers = ["Game ID", "Date", "Local Time", "Round", "Home Team", "Away Team", "Venue", "Status", "Home Score", "Away Score", "Last Updated"];
        var existingData = sheet.getDataRange().getValues();
        var existingGameIDs = {};

        if (existingData.length > 1) {
            for (var i = 1; i < existingData.length; i++) {
                existingGameIDs[existingData[i][0]] = i + 1; // Store row index for fast lookup
            }
        }

        // ✅ Prepare batch update
        var batchUpdates = [];
        var batchInserts = [];
        var lastUpdated = formatDateTime(new Date());

        data.response.forEach(function(game) {
            var gameId = game.game.id;
            var gameDate = new Date(game.date).toISOString().split("T")[0];
            var gameTimeUTC = game.time;
            var gameRound = Number(game.week);
            var homeTeam = getTeamShortName(game.teams.home.id);
            var awayTeam = getTeamShortName(game.teams.away.id);
            var venue = game.venue;
            var status = game.status.short;
            var homeScore = game.scores.home ? game.scores.home.score || 0 : 0;
            var awayScore = game.scores.away ? game.scores.away.score || 0 : 0;
            var gameTimeLocal = convertUTCToLocal(gameDate + " " + gameTimeUTC);
            var newRow = [gameId, gameDate, gameTimeLocal, gameRound, homeTeam, awayTeam, venue, status, homeScore, awayScore, lastUpdated];

            if (existingGameIDs[gameId]) {
                var rowIndex = existingGameIDs[gameId];
                var existingRow = sheet.getRange(rowIndex, 1, 1, headers.length).getValues()[0];

                if (JSON.stringify(existingRow) !== JSON.stringify(newRow)) {
                    batchUpdates.push({ rowIndex: rowIndex, data: newRow });
                }
            } else {
                batchInserts.push(newRow);
            }
        });

        // ✅ Apply batch updates
        batchUpdates.forEach(update => {
            sheet.getRange(update.rowIndex, 1, 1, headers.length).setValues([update.data]);
        });

        // ✅ Apply batch inserts
        if (batchInserts.length > 0) {
            sheet.getRange(sheet.getLastRow() + 1, 1, batchInserts.length, headers.length).setValues(batchInserts);
        }

        sheet.getRange("D2:D").setNumberFormat("0"); // Ensure Round column is formatted as a number

        Logger.log(`✅ ${seasonYear} Game Schedule Updated: ${batchUpdates.length} updates, ${batchInserts.length} new entries!`);
    } catch (e) {
        Logger.log("❌ Error fetching AFL game schedule: " + e.message + "\nStack: " + e.stack);
    }
  updateDashboard("fetchAFLGameSchedule", "Updated Game Schedule");
}

/**
 * Converts a given UTC date-time string to local time.
 */
function convertUTCToLocal(utcDateTime) {
    var utcDate = new Date(utcDateTime + " UTC");
    return Utilities.formatDate(utcDate, Session.getScriptTimeZone(), "yyyy-MM-dd HH:mm");
}

/**
 * Formats a date-time value to `YYYY-MM-DD HH:MM:SS` format.
 */
function formatDateTime(date) {
    return Utilities.formatDate(date, Session.getScriptTimeZone(), "yyyy-MM-dd HH:mm:ss");
}
