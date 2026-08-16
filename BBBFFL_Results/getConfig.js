function getConfig() {
  var scriptProperties = PropertiesService.getScriptProperties();
  return {
    SEASON_MONITORING_ENABLED: scriptProperties.getProperty("SEASON_MONITORING_ENABLED") === "true",
    seasonYear: scriptProperties.getProperty("SEASON_YEAR"),
    aflStatsSheetId: "1Y_GGnSQvhKW2nXSgx2krd9zwuAArT-KdbG_H8Cl9sKM",
    bbbfflStatsSheetId: "1kF6ipWdktnMTJrKUzlnenKc-2apzGibyzh2T0bqNWPo",
    bbbfflListsSheetId: "14_hVCX-rbfM7yxf9el9ZbHMVYbIpLcvKB_P_NbxvsSI",
    bbbfflWeeklyTeamsSheetId: "1PwT6oYZ6BDe6xkyVRMNHnRNoqjb-WljcxK09cnwkegI",
    bbbfflResultsSheetId: "1YRdlO8QmBx2PuftmlrjGYA9wn56jYAT1B1gKODjgMD4",
  };
}
