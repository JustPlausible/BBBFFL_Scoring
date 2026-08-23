function onOpen() {
    var ui = SpreadsheetApp.getUi();
    ui.createMenu('Dashboard')
        .addItem('Update Game Schedule', 'fetchAFLGameSchedule')
        .addItem('Fetch Fantasy Stats', 'fetchBBBFFLStatsForRound')
        .addItem('Update BBBFFL Forms', 'updateAllBBBFFLForms')
        .addToUi();
}
