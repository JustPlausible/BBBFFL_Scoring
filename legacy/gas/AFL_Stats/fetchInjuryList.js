// Updated 2/4/2025
// Function to fetch AFL player injury details from Footywire

function fetchInjuryList() {
  const url = "https://www.footywire.com/afl/footy/injury_list";
  const response = UrlFetchApp.fetch(url);
  const html = response.getContentText();
  // Proceed to parse the HTML content
}

function parseInjuryList(html) {
  const injuryData = [];
  const teamSections = html.split('<h3>');

  teamSections.forEach(section => {
    const teamMatch = section.match(/(.+?)<\/h3>/);
    if (teamMatch) {
      const teamName = teamMatch[1].trim();
      const playerRows = section.match(/<tr>(.*?)<\/tr>/gs);

      if (playerRows) {
        playerRows.forEach(row => {
          const cols = row.match(/<td.*?>(.*?)<\/td>/gs);
          if (cols && cols.length >= 3) {
            const player = cols[0].replace(/<.*?>/g, '').trim();
            const injury = cols[1].replace(/<.*?>/g, '').trim();
            const expected = cols[2].replace(/<.*?>/g, '').trim();
            injuryData.push([teamName, player, injury, expected]);

            logAction(`➕ Added: ${player} (${teamName}) - ${injury}, ${expected}`, LOG_LEVELS.DEBUG);
          }
        });
      } else {
        logAction(`ℹ️ No player rows found for ${teamName}`, LOG_LEVELS.DEBUG);
      }
    }
  });

  return injuryData;
}

function updateInjurySheet() {
  const url = "https://www.footywire.com/afl/footy/injury_list";
  logAction(`📡 Fetching injury data from: ${url}`, LOG_LEVELS.INFO);

  let html;
  try {
    const options = {
      headers: {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
      },
      muteHttpExceptions: true
    };

    const response = UrlFetchApp.fetch(url, options);
    const code = response.getResponseCode();

    if (code !== 200) {
      logAction(`❌ Failed to fetch page: Status Code ${code}`, LOG_LEVELS.ERROR);
      return;
    }

    html = response.getContentText();
    logAction("✅ HTML content fetched successfully.", LOG_LEVELS.DEBUG);

  } catch (err) {
    logAction(`❌ Request failed: ${err.message}`, LOG_LEVELS.ERROR);
    return;
  }

  const injuryData = parseInjuryList(html);
  logAction(`📊 Parsed ${injuryData.length} injury entries.`, LOG_LEVELS.INFO);

  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName("Injury List");

  if (!sheet) {
    sheet = ss.insertSheet("Injury List");
    logAction("🆕 Created 'Injury List' sheet.", LOG_LEVELS.INFO);
  } else {
    sheet.clearContents();
    logAction("🧹 Cleared existing content in 'Injury List' sheet.", LOG_LEVELS.DEBUG);
  }

  const headers = ["Team", "Player", "Injury", "Expected Return"];
  sheet.appendRow(headers);

  if (injuryData.length > 0) {
    sheet.getRange(2, 1, injuryData.length, headers.length).setValues(injuryData);
    logAction("✅ Injury data written to sheet successfully.", LOG_LEVELS.INFO);
  } else {
    logAction("⚠️ No injury data found to write.", LOG_LEVELS.WARN);
  }

  sheet.autoResizeColumns(1, headers.length);
}
