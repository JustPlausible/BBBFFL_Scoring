// Updated 29/3/2025
// Combines the latest version of each team's naming sheets into the Master Weekly Teams

function consolidateWeeklyTeams() {
  const config = getConfig();
  const ss = SpreadsheetApp.openById(config.bbbfflWeeklyTeamsSheetId);
  const masterSheet = ss.getSheetByName("Master Weekly Teams") || ss.insertSheet("Master Weekly Teams");

  // Set up headers
  const headers = [
    "Timestamp", "Entered by", "BBBFFL Team", "Round",
    "Forward1", "Forward2", "Forward3",
    "Midfield1", "Midfield2", "Midfield3",
    "Ruck", "Tackler", "Interchange"
  ];
  masterSheet.clear();
  masterSheet.appendRow(headers);

  const sheets = ss.getSheets();
  const latestEntries = {};
  const originalNotes = {};

  // Helper to extract ID and name
  function extractIdAndName(input) {
    const match = input?.match(/^(.+?)\s*\[(\d+)\]$/);
    if (match) return { name: match[1].trim(), id: match[2] };
    return { name: "", id: "" };
  }

  // Collect latest entries
  sheets.forEach(sheet => {
    const sheetName = sheet.getName();
    if (!sheetName.startsWith("Form Responses")) return;

    const data = sheet.getDataRange().getValues();
    if (data.length <= 1) return;

    const newData = data.slice(1);
    newData.forEach(row => {
      const timestamp = new Date(row[0]);
      const enteredBy = row[1];
      const team = row[2];
      const round = row[3];
      if (!team || !round) return;

      const key = `${team}-${round}`;

      // Check if this is the newest submission for this team-round combo
      if (!latestEntries[key] || timestamp > latestEntries[key][0]) {
        const extracted = row.slice(4, 13).map(extractIdAndName);
        const playerIDs = extracted.map(p => p.id);
        const playerNames = extracted.map(p => p.name);

        // ✅ Now check for duplicates (only on the latest entry)
        const duplicates = playerIDs.filter((id, idx, arr) => id && arr.indexOf(id) !== idx);
        if (duplicates.length > 0) {
          Logger.log(`⚠️ Duplicate player(s) detected for ${team} Round ${round}: ${duplicates.join(", ")}`);
          return; // Skip invalid latest entry
        }

        const entry = [formatTimestamp(timestamp), enteredBy, team, round, ...playerIDs];
        latestEntries[key] = [timestamp, entry];
        originalNotes[key] = playerNames;
      }
    });
  });

  // Convert to array and sort
  const consolidatedData = Object.values(latestEntries).map(e => e[1]);
  consolidatedData.sort((a, b) => {
    const roundA = parseInt(a[3], 10);
    const roundB = parseInt(b[3], 10);
    if (roundA !== roundB) return roundA - roundB;
    return a[2].localeCompare(b[2]); // BBBFFL Team
  });

  // Write data and reapply notes from original form names
  if (consolidatedData.length > 0) {
    const range = masterSheet.getRange(2, 1, consolidatedData.length, consolidatedData[0].length);
    range.setValues(consolidatedData);

    // Clear existing notes
    masterSheet.getRange(2, 5, masterSheet.getLastRow() - 1, 9).clearNote();

    // Reapply player name notes
    for (let i = 0; i < consolidatedData.length; i++) {
      const team = consolidatedData[i][2];
      const round = consolidatedData[i][3];
      const key = `${team}-${round}`;
      const notes = originalNotes[key] || [];
      for (let j = 0; j < notes.length; j++) {
        if (notes[j]) {
          masterSheet.getRange(i + 2, j + 5).setNote(notes[j]);
        }
      }
    }
  }

  logAction(`✅ Master Weekly Teams updated with latest entries (${consolidatedData.length} teams).`, LOG_LEVELS.INFO);
  updateWeeklyDashboard("consolidateWeeklyTeams", "Updated team data after form submission");
}

/**
 * Extracts Player ID from "Player Name [ID]"
 */
function extractPlayerID(input) {
    if (!input || typeof input !== "string") return "";
    var match = input.match(/\[(\d+)\]/);
    return match ? match[1] : ""; // Extracts the number inside brackets
}

/**
 * Formats timestamp to maintain original format
 */
function formatTimestamp(input) {
    if (!input) return "";
    var date = new Date(input);
    return Utilities.formatDate(date, Session.getScriptTimeZone(), "yyyy-MM-dd HH:mm:ss");
}
