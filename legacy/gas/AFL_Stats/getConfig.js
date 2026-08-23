function getConfig() {
  var scriptProperties = PropertiesService.getScriptProperties();
  return {
    apiKey: scriptProperties.getProperty("API_KEY"),
    afl_apiKey: scriptProperties.getProperty("AFL_API_KEY"),
    seasonYear: scriptProperties.getProperty("SEASON_YEAR"),
    season_id: 73, //See afl.com.au for updates each season
    aflStatsSheetId: "1Y_GGnSQvhKW2nXSgx2krd9zwuAArT-KdbG_H8Cl9sKM",
    liveStatuses: ["Q1", "QT", "Q2", "HT", "Q3", "3QT", "Q4"],
  };
}