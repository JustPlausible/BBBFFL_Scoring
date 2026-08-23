// Updated 4/4/2025
// Emails pre-filled links to the Google Form of each team's weekly naming sheet, to each coach

function emailPreFilledLinksToCoaches(roundNumber) {
  const config = getConfig();
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const prefilledSheet = ss.getSheetByName("Pre-Filled Links");
  const masterSheet = SpreadsheetApp.openById(config.bbbfflWeeklyTeamsSheetId).getSheetByName("Master Weekly Teams");
  const emailSheet = ss.getSheetByName("Coach Emails");

  const prefilledData = prefilledSheet.getRange(2, 1, prefilledSheet.getLastRow() - 1, 4).getValues();
  const prefilledFormulas = prefilledSheet.getRange(2, 3, prefilledSheet.getLastRow() - 1).getFormulas(); // Only column C

  const teamLinks = {};
  for (let i = 0; i < prefilledData.length; i++) {
    const [team, round, , timestamp] = prefilledData[i];
    const formula = prefilledFormulas[i][0];

    if (parseInt(round) !== roundNumber || !formula || !formula.includes("HYPERLINK")) continue;

    // Extract the actual link from the HYPERLINK formula
    const match = formula.match(/HYPERLINK\("([^"]+)"/);
    if (match && match[1]) {
      teamLinks[team] = match[1];
    }
  }

  const masterData = masterSheet.getDataRange().getValues();
  const masterHeaders = masterData[0];
  const masterRows = masterData.slice(1);

  const teamSubmissions = {};
  for (let row of masterRows) {
    const [ , , team, round, ...players ] = row;
    if (parseInt(round) === roundNumber) {
      teamSubmissions[team] = players;
    }
  }

  const emailData = emailSheet.getDataRange().getValues();
  for (let i = 1; i < emailData.length; i++) {
    const [team, email, coachName] = emailData[i];
    const link = teamLinks[team];
    const submission = teamSubmissions[team];

    if (!email || !link) continue;

    let htmlBody = `
      <p>Hi ${coachName},</p>
      <p>Use the link below to submit your <strong>Round ${roundNumber}</strong> team:</p>
      <p><a href="${link}" style="background:#4CAF50;color:#fff;padding:8px 12px;text-decoration:none;border-radius:4px;">Submit Team for Round ${roundNumber}</a></p>
    `;

    if (submission) {
      const grouped = {
        Forward: submission.slice(0, 3),
        Midfield: submission.slice(3, 6),
        Ruck: [submission[6]],
        Tackler: [submission[7]],
        Interchange: [submission[8]],
      };

      htmlBody += `<p><strong>Current submission:</strong></p>`;
      htmlBody += `<table border="1" cellpadding="6" style="border-collapse:collapse;font-family:Arial;font-size:13px;">`;

      for (let [label, group] of Object.entries(grouped)) {
        htmlBody += `<tr><td><strong>${label}</strong></td><td>${group.map(p => p || "").join(", ")}</td></tr>`;
      }

      htmlBody += `</table>`;
    } else {
      htmlBody += `<p><em>No submission found yet.</em></p>`;
    }

    htmlBody += `<br><p>Regards,<br><strong>BBBFFL Commissioner</strong></p>`;

    GmailApp.sendEmail(email, `BBBFFL: Round ${roundNumber} Team Link`, "", {
      htmlBody
    });

    logAction(`📧 Sent email to ${coachName} (${email}) for Round ${roundNumber}`, LOG_LEVELS.INFO);
  }
}

function sendPreFilledLinkToCoach(team, roundNumber) {
  const config = getConfig();

  const coachSheet = SpreadsheetApp.openById(config.bbbfflWeeklyTeamsSheetId).getSheetByName("Coach Emails");
  const masterSheet = SpreadsheetApp.openById(config.bbbfflWeeklyTeamsSheetId).getSheetByName("Master Weekly Teams");

  const coachData = coachSheet.getDataRange().getValues();
  const coachRow = coachData.find(row => row[0] === team);
  if (!coachRow) return;

  const email = coachRow[1];
  const coachName = coachRow[2];

  // Find matching row in Master Weekly Teams
  const data = masterSheet.getDataRange().getValues();
  const notes = masterSheet.getDataRange().getNotes();

  const headers = data[0];
  const teamCol = headers.indexOf("BBBFFL Team");
  const roundCol = headers.indexOf("Round");

  const rowIndex = data.findIndex(r => r[teamCol] === team && parseInt(r[roundCol]) === roundNumber);
  if (rowIndex === -1) return;

  const row = data[rowIndex];
  const noteRow = notes[rowIndex];

  const playerIDs = row.slice(4, 13);      // Player ID columns
  const playerNotes = noteRow.slice(4, 13); // Player names from notes

  const link = buildPreFilledBBBFFLLink(team, roundNumber, playerIDs, playerNotes);

  // Group players into position categories
  const groups = {
    Forwards: playerNotes.slice(0, 3).filter(Boolean),
    Midfielders: playerNotes.slice(3, 6).filter(Boolean),
    Ruck: playerNotes.slice(6, 7).filter(Boolean),
    Tackler: playerNotes.slice(7, 8).filter(Boolean),
    Interchange: playerNotes.slice(8, 9).filter(Boolean)
  };

  let submissionHtml = "";
  for (let role in groups) {
    submissionHtml += `<b>${role}:</b><br>${groups[role].join(", ") || ""}<br><br>`;
  }

  const subject = `✅ BBBFFL Round ${roundNumber} – Updated Team Submission`;
  const body = `
    Hi ${coachName},<br><br>
    Your latest team submission has been received.<br>
    If you'd like to make changes, use the updated link below:<br><br>
    <a href="${link}">📝 Submit or Update Team for Round ${roundNumber}</a><br><br>
    ✅ <b>Current Submission:</b><br>${submissionHtml}
    Regards,<br>
    Auto BBBFFL Scorer
  `;

  GmailApp.sendEmail(email, subject, "", { htmlBody: body });
  logAction(`📧 Sent updated pre-filled link to ${coachName} (${team})`, LOG_LEVELS.INFO);
}
