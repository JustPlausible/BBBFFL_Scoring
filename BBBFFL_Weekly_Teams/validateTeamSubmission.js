// Updated 2/5/2025
// Validates a weekly team submission prior to storing in Master Weekly Teams

function validateTeamSubmission(row) {
  const rawSelections = row.slice(4, 13); // 9 player fields
  const seen = new Set();
  const duplicates = [];
  const playerInfo = [];

  // Check for duplicates
  rawSelections.forEach(input => {
    const { id, name } = extractIdAndName(input);
    if (!id) return;

    const player = lookupAFLPlayer(id);
    const aflTeam = player?.aflTeam || "Unknown";

    playerInfo.push({ id, name, aflTeam });

    if (seen.has(id)) {
      duplicates.push(name || `Player ${id}`);
    } else {
      seen.add(id);
    }
  });

  if (duplicates.length > 0) {
    return {
      isValid: false,
      reason: `⚠️ Duplicate player(s) detected: ${duplicates.join(", ")}`,
      playerInfo
    };
  }

  // Check if locked players (whose matches have started) were changed
  const prior = getPreviousSubmission(row[2], parseInt(row[3])); // team, round
  const positions = ["Forward1", "Forward2", "Forward3", "Midfield1", "Midfield2", "Midfield3", "Ruck", "Tackler", "Interchange"];

  for (let i = 0; i < positions.length; i++) {
    const position = positions[i];
    const currentInput = row[i + 4];       // e.g. "Nicholas Martin [745]"
    const previousInput = prior?.[i] || "";
    const { id, name } = extractIdAndName(currentInput);

    if (!id) continue;

    if (playerAlreadyStarted(id, row[3])) {
      logAction(`🔒 Checking locked player in ${position}:`, LOG_LEVELS.DEBUG);
      logAction(`➡️ Current Input: "${currentInput}" | Previous Input: "${previousInput}"`, LOG_LEVELS.DEBUG);

      const currentId = extractIdAndName(currentInput).id;
      const previousId = extractIdAndName(previousInput).id;

      if (currentId !== previousId) {
        logAction(`🚫 Submission rejected — player ID changed after game started: ${name} in ${position}`, LOG_LEVELS.WARN);
        holdLateSubmission(row, `❌ Player ${name} in ${position} has already played and was changed or removed.`);
        return {
          isValid: false,
          reason: `❌ Cannot change ${name} in ${position} — their game has started.`,
          playerInfo
        };
      }
    }
  }

  // Late submission check — only enforce if no prior entry exists
  const playerIds = playerInfo.map(p => p.id);
  if (isLateSubmission(row[2], row[3], playerIds)) {
    if (!prior) {
      holdLateSubmission(row, "Late submission (no prior entry)");
      return {
        isValid: false,
        reason: "⏳ Submission received after the first match, and no earlier entry exists.",
        playerInfo
      };
    }
    logAction(`⏩ Late submission allowed — all locked players are unchanged`, LOG_LEVELS.INFO);
  }

  // ✅ Passed all checks
  return {
    isValid: true,
    playerInfo
  };
}

function extractIdAndName(input) {
  const str = String(input || "");
  const match = str.match(/^(.+?)\s*\[(\d+)\]$/);

  if (match) {
    return { name: match[1].trim(), id: match[2] };
  }

  // Handle input that's *just* an ID, like "745"
  if (/^\d+$/.test(str)) {
    return { name: "", id: str };
  }

  return { name: "", id: "" };
}

// Functions to determine correct timing of submissions
function getFirstAFLGameForRound(roundNumber) {
  const config = getConfig();
  const scheduleSheet = SpreadsheetApp.openById(config.aflStatsSheetId).getSheetByName("Game Schedule");
  const data = scheduleSheet.getDataRange().getValues();
  const headers = data[0];

  const roundCol = headers.indexOf("Round");
  const timeCol = headers.indexOf("Local Time");
  const homeCol = headers.indexOf("Home Team");
  const awayCol = headers.indexOf("Away Team");
  const gameIdCol = headers.indexOf("Game ID");

  const matches = data.slice(1)
    .filter(row => parseInt(row[roundCol]) === roundNumber)
    .sort((a, b) => new Date(a[timeCol]) - new Date(b[timeCol]));

  if (matches.length === 0) return null;

  const firstMatch = matches[0];

  return {
    gameId: firstMatch[gameIdCol],
    localTime: new Date(firstMatch[timeCol]),
    homeTeam: firstMatch[homeCol],
    awayTeam: firstMatch[awayCol],
  };
}

function getSecondAFLGameForRound(roundNumber) {
  const config = getConfig();
  const sheet = SpreadsheetApp.openById(config.aflStatsSheetId).getSheetByName("Game Schedule");
  const data = sheet.getDataRange().getValues();
  const headers = data[0];

  const roundCol = headers.indexOf("Round");
  const timeCol = headers.indexOf("Local Time");
  const statusCol = headers.indexOf("Status");
  const homeCol = headers.indexOf("Home Team");
  const awayCol = headers.indexOf("Away Team");

  const matches = data.slice(1)
    .filter(row => parseInt(row[roundCol]) === roundNumber)
    .sort((a, b) => new Date(a[timeCol]) - new Date(b[timeCol]));

  if (matches.length < 2) return null;

  const secondMatch = matches[1];

  return {
    localTime: new Date(secondMatch[timeCol]),
    homeTeam: secondMatch[homeCol],
    awayTeam: secondMatch[awayCol],
    status: secondMatch[statusCol]
  };
}

function isLateSubmission(team, round, playerIds) {
  const config = getConfig();
  const scheduleSheet = SpreadsheetApp.openById(config.aflStatsSheetId).getSheetByName("Game Schedule");
  const scheduleData = scheduleSheet.getDataRange().getValues();

  const roundMatches = scheduleData.filter(row => row[3] === round && row[7] === "NS"); // Round & Not Started
  if (roundMatches.length === 0) return false; // No games, allow

  // Find first scheduled match (by Local Time)
  roundMatches.sort((a, b) => new Date(a[2]) - new Date(b[2]));
  const firstMatch = roundMatches[0];
  const firstMatchTime = new Date(firstMatch[2]); // Local Time column
  const earlyTeams = [firstMatch[4], firstMatch[5]]; // Home Team, Away Team

  // Get AFL team mapping
  const playerSheet = SpreadsheetApp.openById(config.aflStatsSheetId).getSheetByName("Player Names");
  const playerMap = {};
  const playerData = playerSheet.getDataRange().getValues();
  for (let i = 1; i < playerData.length; i++) {
    playerMap[playerData[i][0]] = playerData[i][2]; // PlayerID → AFL Team
  }

  // Find any submitted players in early teams
  const now = new Date();
  const late = playerIds.some(id => earlyTeams.includes(playerMap[id]));
  const isLate = late && now > firstMatchTime;

  return isLate;
}

function playerAlreadyStarted(playerId, roundNumber) {
  const config = getConfig();
  const playerSheet = SpreadsheetApp.openById(config.aflStatsSheetId).getSheetByName("Player Names");
  const scheduleSheet = SpreadsheetApp.openById(config.aflStatsSheetId).getSheetByName("Game Schedule");

  // Get player's AFL team
  const playerData = playerSheet.getDataRange().getValues();
  let aflTeam = null;
  for (let i = 1; i < playerData.length; i++) {
    if (String(playerData[i][0]) === String(playerId)) {
      aflTeam = playerData[i][4]; // AFL Team is in col E (index 4)
      break;
    }
  }
  if (!aflTeam) return false;

  // Search for any match involving that team in the current round
  const scheduleData = scheduleSheet.getDataRange().getValues();
  for (let i = 1; i < scheduleData.length; i++) {
    const round = scheduleData[i][3]; // Round
    const homeTeam = scheduleData[i][4];
    const awayTeam = scheduleData[i][5];
    const status = scheduleData[i][7]; // Status
    const localTimeStr = scheduleData[i][2]; // Local Time string

    if (parseInt(round) !== parseInt(roundNumber)) continue;
    if (homeTeam !== aflTeam && awayTeam !== aflTeam) continue;

    const matchStartTime = new Date(localTimeStr);
    const now = new Date();

    if (status !== "NS" || now > matchStartTime) {
      return true; // Match has started or is in progress
    }
  }

  return false;
}

function getPreviousSubmission(team, round) {
  const config = getConfig();
  const sheet = SpreadsheetApp.openById(config.bbbfflWeeklyTeamsSheetId).getSheetByName("Master Weekly Teams");
  const data = sheet.getDataRange().getValues();

  const headers = data[0];
  const teamCol = headers.indexOf("BBBFFL Team");
  const roundCol = headers.indexOf("Round");

  for (let i = 1; i < data.length; i++) {
    if (data[i][teamCol] === team && parseInt(data[i][roundCol]) === round) {
      return data[i].slice(4, 13); // player columns
    }
  }
  return null;
}

//Notification emails to coaches regarding invalid or late submissions
function notifyCoachOfInvalidSubmission(team, round, issue, link, playerInfo) {
  const config = getConfig();
  const emailSheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("Coach Emails");
  const emailData = emailSheet.getDataRange().getValues();
  const coachRow = emailData.find(row => row[0] === team);

  if (!coachRow) return;

  const email = coachRow[1];
  const coachName = coachRow[2];

  const subject = `⛔ BBBFFL Team Submission Error – ${team} Round ${round}`;
  const formattedTeam = formatSubmittedTeamHTML(playerInfo);

  const htmlBody = `
    <p>Hi ${coachName},</p>
    <p><strong>Your team submission for Round ${round} was not accepted</strong> due to the following issue:</p>
    <p style="color: red;">${issue}</p>
    <p>Please use the link below to resubmit your team:</p>
    <p><a href="${link}" target="_blank">📝 Resubmit Team for Round ${round}</a></p>
    <br/>
    <p>🧾 <strong>Your current submission:</strong></p>
    ${formattedTeam}
    <p>Regards,<br/>Auto BBBFFL Scorer</p>
  `;

  GmailApp.sendEmail(email, subject, "", { htmlBody });
}

function notifyCoachOfLateSubmission(team, round, gameInfo, submittedAt) {
  const config = getConfig();
  const coachSheet = SpreadsheetApp.openById(config.bbbfflWeeklyTeamsSheetId).getSheetByName("Coach Emails");
  const data = coachSheet.getDataRange().getValues();
  const coach = data.find(row => row[0] === team);
  if (!coach) return;

  const [_, email, coachName] = coach;

  const subject = `⏳ Submission Too Late for Round ${round}`;
  const body = `
    Hi ${coachName},<br><br>
    Your team submission for <b>${team}</b> in Round ${round} was received at <b>${submittedAt.toLocaleString("en-AU")}</b>,<br>
    but the first match of the round (between ${gameInfo.homeTeam} and ${gameInfo.awayTeam}) began at <b>${new Date(gameInfo.localTime).toLocaleString("en-AU")}</b>.<br><br>
    Your team has been held for manual review by the BBBFFL Scorer.<br><br>
    Regards,<br>
    Auto BBBFFL Scorer
  `;

  GmailApp.sendEmail(email, subject, "", { htmlBody: body });
}

function formatSubmittedTeamHTML(playerInfo) {
  const positions = ["Forward1", "Forward2", "Forward3", "Midfield1", "Midfield2", "Midfield3", "Ruck", "Tackler", "Interchange"];

  const rows = playerInfo.map((p, i) => {
    const position = positions[i] || `Pos ${i + 1}`;
    const player = p.name ? `${p.name} [${p.id}]` : "—";
    return `<tr><td><strong>${position}</strong></td><td>${player}</td></tr>`;
  });

  return `
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse: collapse; font-family: Arial, sans-serif;">
      <thead><tr style="background-color: #f1f1f1;"><th>Position</th><th>Player</th></tr></thead>
      <tbody>${rows.join("")}</tbody>
    </table>
  `;
}

function notifyCoachOfDeclinedSubmission(team, round, reason) {
  const config = getConfig();
  const ss = SpreadsheetApp.openById(config.bbbfflWeeklyTeamsSheetId);
  const emailSheet = ss.getSheetByName("Coach Emails");
  const masterSheet = ss.getSheetByName("Master Weekly Teams");

  const emailData = emailSheet.getDataRange().getValues();
  const header = emailData[0];
  const teamCol = header.indexOf("BBBFFL Team");
  const emailCol = header.indexOf("Email");
  const coachCol = header.indexOf("Coach Name");

  const emailRow = emailData.find(r => r[teamCol] === team);
  if (!emailRow) {
    logAction(`❌ Could not find coach email for ${team}`, LOG_LEVELS.ERROR);
    return;
  }

  const email = emailRow[emailCol];
  const coachName = emailRow[coachCol] || "Coach";

  // 🧠 Fetch previous round data
  const data = masterSheet.getDataRange().getValues();
  const notes = masterSheet.getDataRange().getNotes();
  const teamIndex = data[0].indexOf("BBBFFL Team");
  const roundIndex = data[0].indexOf("Round");

  const previousRound = round - 1;
  const teamRowIndex = data.findIndex(row => row[teamIndex] === team && parseInt(row[roundIndex]) === previousRound);

  let fallbackHTML = "<em>Not available</em>";
  if (teamRowIndex !== -1) {
    const playerNoteRow = notes[teamRowIndex];
    const playerNames = playerNoteRow.slice(4, 13).map(name => name || "-");

    const grouped = {
      Forwards: playerNames.slice(0, 3),
      Midfields: playerNames.slice(3, 6),
      Ruck: [playerNames[6]],
      Tackler: [playerNames[7]],
      Interchange: [playerNames[8]],
    };

    fallbackHTML = `<table border="1" cellpadding="6" cellspacing="0" style="border-collapse: collapse; font-family: Arial; font-size: 14px;">`;
    for (const [pos, names] of Object.entries(grouped)) {
      fallbackHTML += `<tr><td><strong>${pos}</strong></td><td>${names.join(", ")}</td></tr>`;
    }
    fallbackHTML += `</table>`;
  }

  // 📨 Email body
  const subject = `BBBFFL Submission Declined - ${team} Round ${round}`;
  const body = `
    <p>Hi ${coachName},</p>
    <p>Your submission for <strong>${team} - Round ${round}</strong> has been <strong>declined</strong>.</p>
    <p>📛 <strong>Reason:</strong> ${reason}</p>
    <p>⏪ As a result, your <strong>previous round's team</strong> will be used:</p>
    ${fallbackHTML}
    <p>Regards,<br>Auto BBBFFL Scorer</p>
  `;

  GmailApp.sendEmail(email, subject, "", { htmlBody: body });
  logAction(`📭 Decline notice sent to ${coachName} (${team}) for Round ${round} using fallback team`, LOG_LEVELS.INFO);
}

// Functions to manage additional processing of submissions
function saveToHoldingSheet(row, reason) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const holdingSheet = ss.getSheetByName("Holding Submissions") || ss.insertSheet("Holding Submissions");

  if (holdingSheet.getLastRow() === 0) {
    holdingSheet.appendRow(["Timestamp", "Entered by", "BBBFFL Team", "Round", "Forward1", "Forward2", "Forward3", "Midfield1", "Midfield2", "Midfield3", "Ruck", "Tackler", "Interchange", "Reason"]);
  }

  holdingSheet.appendRow([...row, reason]);
}

function holdLateSubmission(row, reason = "Late submission") {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName("Holding Sheet") || ss.insertSheet("Holding Sheet");

  const headers = [
    "Timestamp", "Entered by", "BBBFFL Team", "Round",
    "Forward1", "Forward2", "Forward3",
    "Midfield1", "Midfield2", "Midfield3",
    "Ruck", "Tackler", "Interchange",
    "Reason", "Held At", "Status", "Reviewed By", "Review Time"
  ];

  // Add headers if new sheet
  if (sheet.getLastRow() === 0) {
    sheet.appendRow(headers);
  }

  const heldAt = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), "yyyy-MM-dd HH:mm:ss");
  const newRow = [...row, reason, heldAt, "Pending", "", ""];
  sheet.appendRow(newRow);

  // ✅ Apply dropdown only to the new "Status" cell
  const statusCol = headers.indexOf("Status") + 1;
  const rule = SpreadsheetApp.newDataValidation()
    .requireValueInList(["Pending", "Approved", "Declined"], true)
    .setAllowInvalid(false)
    .setHelpText("Select approval status")
    .build();
  sheet.getRange(sheet.getLastRow(), statusCol).setDataValidation(rule);

  logAction(`📥 Held submission saved (${row[2]}, Round ${row[3]}): ${reason}`, LOG_LEVELS.INFO);
}

function reviewHeldSubmissions() {
  const config = getConfig();
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName("Holding Sheet");
  const data = sheet.getDataRange().getValues();
  const headers = data[0];

  const statusCol = headers.indexOf("Status");
  const teamCol = headers.indexOf("BBBFFL Team");
  const roundCol = headers.indexOf("Round");
  const reasonCol = headers.indexOf("Reason");
  const reviewedCol = headers.indexOf("Reviewed By");

  for (let i = 1; i < data.length; i++) {
    const row = data[i];
    const status = row[statusCol];
    const team = row[teamCol];
    const round = parseInt(row[roundCol]);
    const reason = row[reasonCol];
    const reviewed = row[reviewedCol];

    if (!status || status.toLowerCase() === "pending" || reviewed) continue;

    const reviewTime = new Date();
    const reviewer = Session.getActiveUser().getEmail();

    if (status.toLowerCase() === "approved") {
      logAction(`✅ Approved submission for ${team} (Round ${round})`, LOG_LEVELS.INFO);

      // Trigger your existing functions manually
      consolidateWeeklyTeams();
      sendPreFilledLinkToCoach(team, round);
      renderPreFilledLinksSheet(round);
      updateWeeklyDashboard("reviewHeldSubmissions", `Approved late submission for ${team} Round ${round}`);

      // Mark it as processed
      sheet.getRange(i + 1, headers.indexOf("Review Time") + 1).setValue(reviewTime);
      sheet.getRange(i + 1, headers.indexOf("Reviewed By") + 1).setValue(reviewer);
    }

    if (status.toLowerCase() === "declined") {
      logAction(`❌ Declined submission for ${team} (Round ${round}). Reason: ${reason}`, LOG_LEVELS.INFO);
      const inserted = insertPreviousRoundAsOverride(team, round);
        if (inserted) {
          notifyCoachOfDeclinedSubmission(team, round, reason);
        }
      sheet.getRange(i + 1, headers.indexOf("Review Time") + 1).setValue(reviewTime);
      sheet.getRange(i + 1, headers.indexOf("Reviewed By") + 1).setValue(reviewer);
    }
  }
}

function insertPreviousRoundAsOverride(team, currentRound) {
  const config = getConfig();
  const ss = SpreadsheetApp.openById(config.bbbfflWeeklyTeamsSheetId);
  const masterSheet = ss.getSheetByName("Master Weekly Teams");

  const data = masterSheet.getDataRange().getValues();
  const notes = masterSheet.getDataRange().getNotes();
  const headers = data[0];

  const roundCol = headers.indexOf("Round");
  const teamCol = headers.indexOf("BBBFFL Team");

  const firstDataRow = 2;
  const playerStartCol = 5; // Forward1
  const playerEndCol = 13; // Interchange

  // 🧹 Remove any existing entry for this team & round
  for (let i = data.length - 1; i >= 1; i--) {
    const rowRound = parseInt(data[i][roundCol], 10);
    const rowTeam = data[i][teamCol];
    if (rowRound === currentRound && rowTeam === team) {
      masterSheet.deleteRow(i + 1);
      logAction(`🧹 Removed existing entry for ${team} (Round ${currentRound})`, LOG_LEVELS.INFO);
    }
  }

  // 🔍 Find previous round row
  const previousRound = currentRound - 1;
  let prevRow = null;
  let prevNotes = null;

  for (let i = 1; i < data.length; i++) {
    const row = data[i];
    const noteRow = notes[i];
    if (parseInt(row[roundCol], 10) === previousRound && row[teamCol] === team) {
      prevRow = [...row];     // clone the row
      prevNotes = noteRow;    // matching notes
      break;
    }
  }

  if (!prevRow) {
    logAction(`❌ No previous round data found for ${team} (R${previousRound})`, LOG_LEVELS.WARN);
    return false;
  }

  // 📝 Prepare new row
  const now = new Date();
  const timestamp = Utilities.formatDate(now, "Australia/Perth", "yyyy-MM-dd HH:mm:ss");
  const newRow = [...prevRow];
  newRow[0] = timestamp;         // Timestamp
  newRow[1] = "(Scorer Override)"; // Entered by
  newRow[3] = currentRound;      // Round

  // ➕ Append new row
  masterSheet.appendRow(newRow);
  const newRowIndex = masterSheet.getLastRow();

  // ✏️ Add notes back (player names) to position columns
  if (prevNotes) {
    for (let col = playerStartCol - 1; col < playerEndCol; col++) {
      const note = prevNotes[col];
      if (note) {
        masterSheet.getRange(newRowIndex, col + 1).setNote(note);
      }
    }
  }

  logAction(`✅ Previous team for ${team} (R${previousRound}) copied to Round ${currentRound}`, LOG_LEVELS.INFO);
  return true;
}
