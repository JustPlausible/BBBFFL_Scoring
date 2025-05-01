/**
 * Returns a pre-filled Google Form URL for a BBBFFL team submission.
 *
 * @param {string} team - The BBBFFL team name.
 * @param {number} round - The round number.
 * @param {string[]} playerIds - Array of 9 player IDs [F1, F2, F3, M1, M2, M3, R, T, I].
 * @param {string[]} playerNames - Optional array of player names (for display as "Name [ID]").
 * @returns {string} - Pre-filled form URL.
 */
function buildPreFilledBBBFFLLink(team, round, playerIds, playerNames = []) {
  const config = getConfig();
  const formIDs = config.bbbfflFormLinks;
  const entryMappings = config.bbbfflEntryMappings;

  const formId = formIDs[team];
  const mappings = entryMappings[team];
  if (!formId || !mappings) return "";

  const baseURL = `https://docs.google.com/forms/d/e/${formId}/viewform?usp=pp_url`;

  let params = `&${encodeURIComponent(mappings.teamName)}=${encodeURIComponent(team)}&${encodeURIComponent(mappings.round)}=${round}`;

  for (let i = 0; i < playerIds.length; i++) {
    const id = playerIds[i];
    const name = playerNames[i] || "Unknown";
    if (id) {
      const formatted = `${name} [${id}]`;
      params += `&${encodeURIComponent(mappings.players[i])}=${encodeURIComponent(formatted)}`;
    }
  }

  return baseURL + params;
}

// Lookup players from AFL Players sheets using an ID
function lookupAFLPlayer(playerId) {
  const config = getConfig();
  const playerSources = [
    { sheetName: "Player Names", source: "API" },
    { sheetName: "Player Names Manual", source: "Manual" }
  ];

  for (let source of playerSources) {
    const sheet = SpreadsheetApp.openById(config.aflStatsSheetId).getSheetByName(source.sheetName);
    if (!sheet) continue;

    const data = sheet.getDataRange().getValues();
    for (let i = 1; i < data.length; i++) {
      if (String(data[i][0]) === String(playerId)) {
        return {
          id: playerId,
          name: data[i][1],
          firstName: data[i][2],
          lastName: data[i][3],
          aflTeam: data[i][4],
          source: source.source
        };
      }
    }
  }

  return null; // Player not found
}
