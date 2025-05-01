// Updated 29/3/2025
// logUtils.gs

const LOG_LEVELS = {
  DEBUG: 1,
  INFO: 2,
  WARN: 3,
  ERROR: 4,
  NONE: 99
};

// 👇 You can change this dynamically if needed.
const CURRENT_LOG_LEVEL = LOG_LEVELS.DEBUG;

function logAction(message, level = LOG_LEVELS.INFO) {
  if (level < CURRENT_LOG_LEVEL) return;

  const levelName = Object.keys(LOG_LEVELS).find(k => LOG_LEVELS[k] === level) || "INFO";
  const timestamp = Utilities.formatDate(new Date(), "Australia/Perth", "HH:mm:ss");

  const prefix = {
    [LOG_LEVELS.DEBUG]: "🔍",
    [LOG_LEVELS.INFO]: "ℹ️",
    [LOG_LEVELS.WARN]: "⚠️",
    [LOG_LEVELS.ERROR]: "❌"
  }[level] || "📝";

  console.log(`[${timestamp}] ${prefix} [${levelName}] ${message}`);
}