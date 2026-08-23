// Updated 6/4/2025
// Manages Form Submission and links to updates

function onFormSubmit(e) {
  const row = e.values;
  const formatTime = (dt) => Utilities.formatDate(dt, "Australia/Perth", "EEE dd MMM yyyy, HH:mm:ss");

  const submissionTimestamp = new Date(row[0]);
  logAction(`🕒 Submission received at: ${formatTime(submissionTimestamp)}`, LOG_LEVELS.INFO);
  const team = row[2];
  const round = parseInt(row[3], 10);
  if (isNaN(round)) {
    logAction(`❌ Invalid round number from submission: ${row[3]}`, LOG_LEVELS.ERROR);
    return;
  }
  logAction(`📆 Submitted for Round: ${round}`, LOG_LEVELS.INFO);

  const playerNames = row.slice(4, 13);
  const playerIds = playerNames.map(extractPlayerID);

  // 🔍 Run primary validation (e.g. duplicates)
  const validation = validateTeamSubmission(row);

  if (!validation.isValid) {
    logAction(`🚫 Submission blocked for ${team} (Round ${round}): ${validation.reason}`, LOG_LEVELS.WARN);

    const cleanPlayerNames = playerNames.map((name) =>
      name ? name.replace(/\s*\[\d+\]$/, "").trim() : "Unknown"
    );

    const prefilledLink = buildPreFilledBBBFFLLink(team, round, playerIds, cleanPlayerNames);
    notifyCoachOfInvalidSubmission(team, round, validation.reason, prefilledLink, validation.playerInfo);
    return;
  }

  // ⏰ Step 2: Early game lockout check
  const earlyGameInfo = getFirstAFLGameForRound(round);
  if (!earlyGameInfo) {
    logAction(`❌ No AFL matches found for Round ${round}`, LOG_LEVELS.WARN);
    return;
  }

  const earlyGameTime = new Date(earlyGameInfo.localTime);
  const earlyTeams = [earlyGameInfo.homeTeam, earlyGameInfo.awayTeam];

  logAction(`🎯 First match: ${earlyTeams.join(" vs ")} at ${formatTime(earlyGameTime)}`, LOG_LEVELS.INFO);

  // 🧠 Match player IDs to AFL Teams
  const playerAFLTeams = validation.playerInfo.map(p => p.aflTeam);
  const hasEarlyGamePlayers = playerAFLTeams.some(team => earlyTeams.includes(team));

  logAction(`📋 Submitted AFL Teams: ${playerAFLTeams.join(", ")}`, LOG_LEVELS.DEBUG);
  logAction(`🛡 Early Match Teams: ${earlyTeams.join(", ")}`, LOG_LEVELS.DEBUG);
  logAction(`✅ hasEarlyGamePlayers: ${hasEarlyGamePlayers}`, LOG_LEVELS.INFO);
  logAction(`⏱ Match Start: ${formatTime(earlyGameTime)}`, LOG_LEVELS.DEBUG);
  logAction(`🕒 Submission Time: ${formatTime(submissionTimestamp)}`, LOG_LEVELS.DEBUG);

// ⏳ Already handled by validateTeamSubmission — no extra check needed here
if (hasEarlyGamePlayers && submissionTimestamp > earlyGameTime) {
  logAction(`⏩ Late submission detected — but validated as safe (locked players unchanged)`, LOG_LEVELS.INFO);
}

  // ⏰ Final timing check: late after second match start (even if player hasn't started yet)
  const secondGameInfo = getSecondAFLGameForRound(round);
  if (!secondGameInfo) {
    logAction(`ℹ️ No second match found for Round ${round} — skipping final timing check`, LOG_LEVELS.DEBUG);
  }

  if (secondGameInfo) {
    const secondGameTime = new Date(secondGameInfo.localTime);
  
    logAction(`🎯 Second match: ${secondGameInfo.homeTeam} vs ${secondGameInfo.awayTeam} at ${formatTime(secondGameTime)}`, LOG_LEVELS.INFO);
  
    if (submissionTimestamp > secondGameTime) {
      logAction(`⏳ Submission received after second match started — full lockout active.`, LOG_LEVELS.WARN);
      holdLateSubmission(row, "Submission received after full team lockout");
      notifyCoachOfLateSubmission(team, round, secondGameInfo, submissionTimestamp);
      return;
    }
  }
  
  // ✅ Valid submission
  try {
    consolidateWeeklyTeams();
    sendPreFilledLinkToCoach(team, round);
    renderPreFilledLinksSheet(round);
    updateWeeklyDashboard("onFormSubmit", `✅ ${team} Round ${round} submission saved`);
  } catch (err) {
    logAction(`❌ Uncaught error during submission processing: ${err.message}`, LOG_LEVELS.ERROR);
  }
}
