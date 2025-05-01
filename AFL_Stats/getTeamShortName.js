function getTeamShortName(teamId) {
    var teamShortNames = {
        1: "ADE", 2: "BRI", 3: "CAR", 4: "COL", 5: "ESS",
        6: "FRE", 7: "GEE", 8: "HAW", 9: "MEL", 10: "NTH",
        11: "PTA", 12: "RIC", 13: "STK", 14: "SYD", 15: "WCE",
        16: "WBD", 17: "GCS", 18: "GWS"
    };

    var teamIdNum = parseInt(teamId, 10); // Convert to a number
    return teamShortNames[teamIdNum] || "UNK"; // Default to 'UNK' if not found
}
