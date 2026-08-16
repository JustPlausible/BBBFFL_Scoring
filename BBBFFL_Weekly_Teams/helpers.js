/**
 * Returns a pre-filled Google Form URL for a BBBFFL team submission.
 *
 * @param {string} team - The BBBFFL team name.
 * @param {number} round - The round number.
 * @param {string[]} playerIds - Array of 9 player IDs [F1, F2, F3, M1, M2, M3, R, T, I].
 * @param {string[]} playerNames - Optional array of player names (for display as "Name [ID]").
 * @returns {string} - Pre-filled form URL.
 */
function buildPreFilledBBBFFLLink(team, round, playerIds, playerNames = [], isSuperScore = false) {
  const config = getConfig();
  const formIDs = config.bbbfflFormLinks;
  const entryMappings = config.bbbfflEntryMappings;

  const formId = formIDs[team];
  const mappings = entryMappings[team];
  if (!formId || !mappings) return "";

  const baseURL = `https://docs.google.com/forms/d/e/${formId}/viewform?usp=pp_url`;

  // SuperScore round label (e.g. "SS1") or regular round number
  const roundLabel = isSuperScore ? `SS${round - 20}` : round;

  let params = `&${encodeURIComponent(mappings.teamName)}=${encodeURIComponent(team)}&${encodeURIComponent(mappings.round)}=${encodeURIComponent(roundLabel)}`;

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
const lookupAFLPlayer = (() => {
  let playerMap = null;

  return function(playerId) {
    if (!playerMap) {
      const config = getConfig();
      const sheet = SpreadsheetApp.openById(config.aflStatsSheetId).getSheetByName("Mapped AFL Players");

      if (!sheet) {
        Logger.log("❌ Mapped AFL Players sheet not found.");
        return null;
      }

      const data = sheet.getDataRange().getValues();
      const headers = data[0];

      const aflIdIndex = headers.indexOf("AFL ID");
      const cdIdIndex = headers.indexOf("CD_id");
      const fullNameIndex = headers.indexOf("Full Name");
      const firstNameIndex = headers.indexOf("First Name");
      const lastNameIndex = headers.indexOf("Last Name");
      const clubIndex = headers.indexOf("Club");

      playerMap = {};

      for (let i = 1; i < data.length; i++) {
        const row = data[i];
        const rowId = String(row[aflIdIndex]);
        playerMap[rowId] = {
          id: rowId,
          cdId: row[cdIdIndex],
          fullName: row[fullNameIndex],
          firstName: row[firstNameIndex],
          lastName: row[lastNameIndex],
          aflTeam: row[clubIndex],
          source: "Mapped AFL Players"
        };
      }
    }

    return playerMap[String(playerId)] || null;
  };
})();
