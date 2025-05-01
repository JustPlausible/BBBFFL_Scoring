function checkApiQuota(response) {
    var headers = response.getAllHeaders();
    
    var dailyRemaining = parseInt(headers["x-ratelimit-requests-remaining"] || "0", 10);
    var minuteRemaining = parseInt(headers["X-RateLimit-Remaining"] || "-1", 10); // Default to -1 if missing

    Logger.log("🚀 API Requests Remaining: Daily = " + dailyRemaining + ", Per Minute = " + minuteRemaining);

    // ✅ Prevent excessive API calls (Optional Thresholds)
    if (dailyRemaining < 5) {
        Logger.log("⚠️ Daily API limit nearly reached. Skipping update.");
        return false;
    }

    if (minuteRemaining >= 0 && minuteRemaining < 2) {
        Logger.log("⚠️ Rate limit nearly reached for this minute. Waiting...");
        Utilities.sleep(2000); // Wait only 2 seconds instead of stopping execution
    }

    return true; // API calls allowed
}
