// Updated 29/3/2025
// Generates pre-filled links to the Google Form of each team's weekly naming sheet

function generatePreFilledDataRows(roundNumber) {
  const config = getConfig();
  const formIDs = config.bbbfflFormLinks;
  const entryMappings = config.bbbfflEntryMappings;
  const weeklyTeamsSheet = SpreadsheetApp.openById(config.bbbfflWeeklyTeamsSheetId).getSheetByName("Master Weekly Teams");

  const data = weeklyTeamsSheet.getDataRange().getValues();
  const notes = weeklyTeamsSheet.getDataRange().getNotes();
  const teamCol = data[0].indexOf("BBBFFL Team");
  const roundCol = data[0].indexOf("Round");

  const rows = [];
  const includedTeams = new Set();

  for (let i = 1; i < data.length; i++) {
    const row = data[i];
    const rowNotes = notes[i];
    const team = row[teamCol];
    const round = parseInt(row[roundCol], 10);
    const timestamp = Utilities.formatDate(row[0], "Australia/Perth", "dd/MM/yyyy HH:mm:ss");

    if (round !== roundNumber || !formIDs[team]) continue;

    includedTeams.add(team);
    const playerIDs = row.slice(4, 13);
    const playerNotes = rowNotes.slice(4, 13);
    const fullLink = buildPreFilledBBBFFLLink(team, round, playerIDs, playerNotes);
    const formula = `=HYPERLINK("${fullLink}", "${team} Round ${roundNumber}")`;
    rows.push([team, round, formula, timestamp]);
  }

  Object.keys(formIDs).forEach(team => {
    if (!includedTeams.has(team)) {
      const fullLink = buildPreFilledBBBFFLLink(team, roundNumber, new Array(9).fill(""), new Array(9).fill(""));
      const formula = `=HYPERLINK("${fullLink}", "${team} Round ${roundNumber}")`;
      rows.push([team, roundNumber, formula, ""]);
    }
  });

  return rows;
}

function renderPreFilledLinksSheet(roundNumber) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName("Pre-Filled Links");
  if (!sheet) {
    sheet = ss.insertSheet("Pre-Filled Links");
  } else {
    sheet.clear();
  }

  const headers = ["BBBFFL Team", "Round", "Pre-Filled Link", "Generated At"];
  sheet.appendRow(headers);
  sheet.getRange("A1:D1").setFontWeight("bold").setBackground("#f1f3f4");

  const rows = generatePreFilledDataRows(roundNumber);
  if (rows.length > 0) {
    sheet.getRange(2, 1, rows.length, rows[0].length).setValues(rows);
  }

  // Basic formatting
  sheet.getDataRange().setFontFamily("Arial");
  sheet.autoResizeColumns(1, 4);
  sheet.setColumnWidth(2, 65);
  sheet.setFrozenRows(1);

  // Zebra striping
  for (let i = 2; i <= sheet.getLastRow(); i++) {
    const background = i % 2 === 0 ? "#ffffff" : "#f9f9f9";
    sheet.getRange(i, 1, 1, 4).setBackground(background);
  }

  // Timestamp
  const formattedTimestamp = Utilities.formatDate(new Date(), "Australia/Perth", "dd/MM/yyyy HH:mm:ss");
  sheet.getRange("C13").setValue("Generated At").setFontWeight("bold");
  sheet.getRange("D13").setValue(formattedTimestamp);

  logAction(`✅ Pre-filled links styled and generated for Round ${roundNumber}`, LOG_LEVELS.INFO);
}

function runGenerateLinksFromButton() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName("Pre-Filled Links");
  const roundCell = sheet.getRange("B13");
  const roundValue = parseInt(roundCell.getValue(), 10);

  if (!roundValue || isNaN(roundValue)) {
    SpreadsheetApp.getUi().alert("⚠ Please select a valid round number in cell B13.");
    return;
  }

  consolidateWeeklyTeams();
  renderPreFilledLinksSheet(roundValue);
  //emailPreFilledLinksToCoaches(roundValue, false); // Optional: can remove this if you want manual control
}

function suggestCurrentRoundScorerReview() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const resultsSheet = ss.getSheetByName("Master Results");
  const prefillSheet = ss.getSheetByName("Pre-Filled Links");
  const rounds = resultsSheet.getRange("A2:A").getValues().flat().filter(val => !isNaN(val));
  const uniqueRounds = [...new Set(rounds)].sort((a, b) => a - b);

  if (uniqueRounds.length === 0) return;

  const currentRound = uniqueRounds[uniqueRounds.length - 1];
  const roundCell = reviewSheet.getRange("O1");

  // Set dropdown validation
  const rule = SpreadsheetApp.newDataValidation()
    .requireValueInList(uniqueRounds.map(r => r.toString()), true)
    .setAllowInvalid(false)
    .setHelpText("Select the round to process")
    .build();
  roundCell.setDataValidation(rule);
  roundCell.setValue(currentRound); // Set default

  // 🟦 NEW: Apply dropdown style (CHIP) via new Sheet service
  const advancedSheet = SpreadsheetApp.getActive().getSheetByName("Scorer Review");
  const config = SpreadsheetApp.newDataValidation().copy().copy();

  const advancedValidation = SpreadsheetApp.newDataValidation()
    .requireValueInList(uniqueRounds.map(r => r.toString()), true)
    .setAllowInvalid(false)
    .setHelpText("Select round")
    .build();

  // This style change requires the Advanced Sheets Service (if not enabled, enable it via Apps Script > Services)
  const sheetId = ss.getId();
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("Scorer Review");
  const sheetName = sheet.getSheetName();
  const cellA1 = 'O1';

  const resource = {
    requests: [{
      updateCells: {
        range: {
          sheetId: sheet.getSheetId(),
          startRowIndex: 0,
          endRowIndex: 1,
          startColumnIndex: 14,
          endColumnIndex: 15,
        },
        rows: [{
          values: [{
            dataValidation: {
              condition: {
                type: 'ONE_OF_LIST',
                values: uniqueRounds.map(r => ({ userEnteredValue: r.toString() }))
              },
              strict: true,
              showCustomUi: true,
            }
          }]
        }],
        fields: "dataValidation"
      }
    }]
  };

  // Requires enabling the Advanced Sheets Service
  Sheets.Spreadsheets.batchUpdate(resource, ss.getId());
  //reviewSheet.getRange("N1").setValue("Select Round:");

  Logger.log(`🕓 Suggested current round: ${currentRound}`);
}

