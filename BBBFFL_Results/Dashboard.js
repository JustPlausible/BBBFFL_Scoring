// Updated 29/03/2025
// Utility to record last run times for key scripts into the 'Dashboard' sheet

function updateResultsDashboard(scriptName, notes = "") {
  if (!scriptName) {
    scriptName = "updateResultsDashboard";
  }

  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName("Dashboard");

  if (!sheet) {
    sheet = ss.insertSheet("Dashboard");
    sheet.appendRow(["Script Name", "Last Run Time", "Notes"]);
  }

  const now = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), "yyyy-MM-dd HH:mm:ss");
  const context = typeof getTriggerContext === "function" ? getTriggerContext() : "manual";
  const fullNote = notes ? `${notes} (${context})` : `Executed via ${context}`;

  const data = sheet.getDataRange().getValues();

  // 🧹 Remove old entries with same script name
  for (let i = data.length - 1; i >= 1; i--) {
    if (data[i][0] && data[i][0].toString().toLowerCase().includes(scriptName.toLowerCase())) {
      sheet.deleteRow(i + 1);
    }
  }

  // ➕ Append updated row
  sheet.appendRow([scriptName, now, fullNote]);

  const range = sheet.getDataRange();
  const lastColumn = range.getLastColumn();
  const lastRow = sheet.getLastRow();

  // ✅ Format header: bold only header row
  const headerRange = sheet.getRange(1, 1, 1, lastColumn);
  headerRange.setFontWeight("bold");

  // 🧼 Remove all banding (alternating colours)
  // This *should* clear both script and manual banding, but Google sometimes retains "Alternating Colours" as a manual format.
  try {
    const bandings = sheet.getBandings();
    bandings.forEach(b => b.remove());
  } catch (e) {
    Logger.log("⚠️ Could not clear existing banding: " + e);
  }

  // 🎨 Only apply banding if there are >1 rows (header + at least one data row)
  if (lastRow > 1) {
    const bandRange = sheet.getRange(1, 1, lastRow, lastColumn);
    try {
      const banding = bandRange
        .applyRowBanding(SpreadsheetApp.BandingTheme.LIGHT_GREY)
        .setHeaderRowColor("#cccccc");
    } catch (e) {
      Logger.log("⚠️ Could not apply row banding: " + e);
    }

    // 🚫 Override bold in body rows (if any)
    if (lastRow > 2) { // Only body if more than header + 1 row
      const bodyRange = sheet.getRange(2, 1, lastRow - 1, lastColumn);
      bodyRange.setFontWeight("normal");
    }
  }

  // ↔️ Auto-fit columns
  for (let col = 1; col <= lastColumn; col++) {
    sheet.autoResizeColumn(col);
  }
}

function getTriggerContext() {
  try {
    const triggers = ScriptApp.getScriptTriggers();
    return triggers && triggers.length ? "Time Trigger" : "Manual Run";
  } catch (err) {
    return "Manual Run";
  }
}
