function populatePlayersSheetfromNewapi() {
  const config = getConfig();
  const apiKey = config.afl_apiKey;
  const endpoint = 'https://afl-api.thehardinghams.net/api/players';

  const options = {
    method: 'get',
    headers: {
      'x-api-key': apiKey,
      'User-Agent': 'Mozilla/5.0 (compatible; GoogleAppsScript/1.0)'
    },
    muteHttpExceptions: true
  };

  const response = UrlFetchApp.fetch(endpoint, options);
  if (response.getResponseCode() !== 200) {
    Logger.log("API error: " + response.getContentText());
    return;
  }

  const players = JSON.parse(response.getContentText());
  const sheetName = "New api Players";
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(sheetName);

  // Create or clear sheet
  if (!sheet) {
    sheet = ss.insertSheet(sheetName);
  } else {
    sheet.clearContents();
  }

  // Define header and rows
  const headers = [
    "AFL ID", "CD_id", "Full Name", "First Name", "Last Name", "Nickname", 
    "Club", "Guernsey", "Position", "AFL Profile", "Image URL"
  ];

  const rows = players.map(p => [
    p.afl_id,
    p.champion_data_id,
    p.full_name,
    p.first_name,
    p.last_name,
    p.nickname,
    p.club,
    p.guernsey,
    p.position,
    p.afl_url,
    p.image_url
  ]);

  // Write to sheet
  sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
  sheet.getRange(2, 1, rows.length, headers.length).setValues(rows);
}

function mapPlayersWithFallbacks() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const apiSheet = ss.getSheetByName("New api Players");
  const legacySheet = ss.getSheetByName("Player Names");
  const manualSheet = ss.getSheetByName("Player Names Manual");
  const outputSheetName = "Mapped AFL Players";

  const apiData = apiSheet.getDataRange().getValues();
  const legacyData = legacySheet.getDataRange().getValues();
  const manualData = manualSheet.getDataRange().getValues();

  const apiHeaders = apiData[0];
  const legacyHeaders = legacyData[0];
  const manualHeaders = manualData[0];

  const apiIndex = {
    fullName: apiHeaders.indexOf("Full Name"),
    firstName: apiHeaders.indexOf("First Name"),
    lastName: apiHeaders.indexOf("Last Name"),
    club: apiHeaders.indexOf("Club"),
    afl_id: apiHeaders.indexOf("AFL ID"),
    cd_id: apiHeaders.indexOf("Champion Data ID") !== -1 ? apiHeaders.indexOf("Champion Data ID") : apiHeaders.indexOf("CD_id")
  };

  const legacyIndex = {
    playerId: legacyHeaders.indexOf("Player ID"),
    fullName: legacyHeaders.indexOf("Full Name"),
    surname: legacyHeaders.indexOf("Surname"),
    team: legacyHeaders.indexOf("AFL Team")
  };

  const manualIndex = {
    playerId: manualHeaders.indexOf("Player ID"),
    fullName: manualHeaders.indexOf("Full Name"),
    surname: manualHeaders.indexOf("Surname"),
    team: manualHeaders.indexOf("AFL Team")
  };

  // 🔎 Build lookup maps
  const buildLookup = (data, idx) => {
    const map = new Map();
    const surnameMap = new Map();
    for (let i = 1; i < data.length; i++) {
      const row = data[i];
      const key = (row[idx.fullName] + "|" + row[idx.team]).toLowerCase();
      const surnameKey = (row[idx.surname] + "|" + row[idx.team]).toLowerCase();
      map.set(key, row[idx.playerId]);
      surnameMap.set(surnameKey, row[idx.playerId]);
    }
    return { map, surnameMap };
  };

  const legacyLookup = buildLookup(legacyData, legacyIndex);
  const manualLookup = buildLookup(manualData, manualIndex);

  // 🧾 Final output
  const outputRows = [["AFL ID", "CD_id", "Full Name", "First Name", "Last Name", "Club", "Legacy Player ID", "legacyName", "Match Source"]];

  function toTitleCase(str) {
    return str.replace(/\w\S*/g, txt =>
      txt.charAt(0).toUpperCase() + txt.substring(1).toLowerCase()
    );
  }

  const matchStats = {};

  for (let i = 1; i < apiData.length; i++) {
    const row = apiData[i];
    const fullName = toTitleCase(row[apiIndex.fullName]);
    const firstName = toTitleCase(row[apiIndex.firstName]);
    const lastName = toTitleCase(row[apiIndex.lastName]);
    const club = row[apiIndex.club];
    const fullKey = (fullName + "|" + club).toLowerCase();
    const surnameKey = (lastName + "|" + club).toLowerCase();

    let legacyId = "";
    let legacyName = "";
    let matchSource = "";

    if (legacyLookup.map.has(fullKey)) {
      legacyId = legacyLookup.map.get(fullKey);
      legacyName = legacyData.find(r => r[legacyIndex.playerId] === legacyId)?.[legacyIndex.fullName] || "";
      matchSource = "Legacy: Full Name + Club";
    } else if (manualLookup.map.has(fullKey)) {
      legacyId = manualLookup.map.get(fullKey);
      legacyName = manualData.find(r => r[manualIndex.playerId] === legacyId)?.[manualIndex.fullName] || "";
      matchSource = "Manual: Full Name + Club";
    } else if (legacyLookup.surnameMap.has(surnameKey)) {
      legacyId = legacyLookup.surnameMap.get(surnameKey);
      legacyName = legacyData.find(r => r[legacyIndex.playerId] === legacyId)?.[legacyIndex.fullName] || "";
      matchSource = "Legacy: Surname + Club";
    } else if (manualLookup.surnameMap.has(surnameKey)) {
      legacyId = manualLookup.surnameMap.get(surnameKey);
      legacyName = manualData.find(r => r[manualIndex.playerId] === legacyId)?.[manualIndex.fullName] || "";
      matchSource = "Manual: Surname + Club";
    } else {
      matchSource = "Unmatched";
    }

    // Tally count
    matchStats[matchSource] = (matchStats[matchSource] || 0) + 1;

    outputRows.push([
      row[apiIndex.afl_id],
      row[apiIndex.cd_id],
      fullName,
      firstName,
      lastName,
      club,
      legacyId,
      legacyName,
      matchSource
    ]);
  }

  // 📝 Output to new sheet
  let outputSheet = ss.getSheetByName(outputSheetName);
  if (!outputSheet) {
    outputSheet = ss.insertSheet(outputSheetName);
  } else {
    outputSheet.clearContents();
  }

  outputSheet.getRange(1, 1, outputRows.length, outputRows[0].length).setValues(outputRows);

  // Add summary starting in column L
  const summaryHeaders = ["Match Type", "Count"];
  const summaryRows = Object.entries(matchStats).sort((a, b) => b[1] - a[1]); // sort by count desc
  const allSummary = [summaryHeaders, ...summaryRows];

  const summaryStartCol = 12; // Column L
  outputSheet.getRange(1, summaryStartCol, allSummary.length, 2).setValues(allSummary);
}
