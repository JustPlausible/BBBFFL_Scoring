function getConfig() {
  var scriptProperties = PropertiesService.getScriptProperties();
  return {
    apiKey: scriptProperties.getProperty("API_KEY"),
    seasonYear: scriptProperties.getProperty("SEASON_YEAR"),
    aflStatsSheetId: "1Y_GGnSQvhKW2nXSgx2krd9zwuAArT-KdbG_H8Cl9sKM",
  };
}