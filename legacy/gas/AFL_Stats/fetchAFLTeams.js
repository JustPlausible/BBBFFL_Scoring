function fetchAFLTeams() {
    var config = getConfig(); // Fetch config values
    var apiKey = config.apiKey;
    var seasonYear = config.seasonYear;

    var url = 'https://v1.afl.api-sports.io/teams?league=1&season=' + seasonYear;

    var options = {
        'method': 'GET',
        'headers': {
            'x-apisports-key': apiKey
        }
    };

    // Manually define AFL team short names
    var teamShortNames = {
        "Adelaide Crows": "ADE",
        "Brisbane Lions": "BRI",
        "Carlton Blues": "CAR",
        "Collingwood Magpies": "COL",
        "Essendon Bombers": "ESS",
        "Fremantle Dockers": "FRE",
        "Geelong Cats": "GEE",
        "Gold Coast Suns": "GCS",
        "Greater Western Sydney Giants": "GWS",
        "Hawthorn Hawks": "HAW",
        "Melbourne Demons": "MEL",
        "North Melbourne Kangaroos": "NTH",
        "Port Adelaide Power": "PTA",
        "Richmond Tigers": "RIC",
        "St Kilda Saints": "STK",
        "Sydney Swans": "SYD",
        "West Coast Eagles": "WCE",
        "Western Bulldogs": "WBD"
    };

    try {
        var response = UrlFetchApp.fetch(url, options);
        var data = JSON.parse(response.getContentText());

        var teamSheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("Teams");
        if (!teamSheet) {
            teamSheet = SpreadsheetApp.getActiveSpreadsheet().insertSheet("Teams");
        }
        teamSheet.clear();
        teamSheet.appendRow(["Team ID", "AFL Team", "Short Name"]);

        data.response.forEach(function(team) {
            var shortName = teamShortNames[team.name] || "N/A"; // Default to "N/A" if no match found

            teamSheet.appendRow([
                team.id,
                team.name,
                shortName
            ]);
        });

        Logger.log("AFL Teams Updated Successfully!");
    } catch (e) {
        Logger.log("Error fetching AFL teams: " + e.toString());
    }
    updateDashboard("fetchAFLTeams", "Pulled in new AFL team data");
}
