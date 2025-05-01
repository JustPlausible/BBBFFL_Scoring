function getConfig() {
  var scriptProperties = PropertiesService.getScriptProperties();
  return {
    apiKey: scriptProperties.getProperty("API_KEY"),
    seasonYear: scriptProperties.getProperty("SEASON_YEAR"),
    aflStatsSheetId: "1Y_GGnSQvhKW2nXSgx2krd9zwuAArT-KdbG_H8Cl9sKM",
    bbbfflStatsSheetId: "1kF6ipWdktnMTJrKUzlnenKc-2apzGibyzh2T0bqNWPo",
    bbbfflListsSheetId: "14_hVCX-rbfM7yxf9el9ZbHMVYbIpLcvKB_P_NbxvsSI",
    bbbfflWeeklyTeamsSheetId: "1PwT6oYZ6BDe6xkyVRMNHnRNoqjb-WljcxK09cnwkegI",
    bbbfflTeamNamingForms: {
        "Crabs": "1bU3LpMGw2X_JtFYTYC_TMx9pJc80P__i-bFdvD4rFnY",
        "JHAS": "1gAdwqRdVdcjJ-FlIYrQBlgk40WghDDEiGsAiTN-mI1A",
        "MotherRuckers": "1OpyXLlmaJsxmN9gVB9eWvvDYgU6tyXN5tYIaKvO2ccE",
        "Evils": "1P9FWS5BMSaM5frq2yPDCezNybZ0FLjOfd4Vs8IkNA1A",
        "Poms": "1heHYCaaiyO_mYm3XPsW3n16nRdEXED9impo2FwWFhZo",
        "1%ers": "1-0tK3mxhInhZuYvbriIzbQ-oUVdpPFNN9fLiYOUYva0",
        "Hots": "17im0iiDbpJiub2hecweSLsM_UaH3tbq-I0fhWhWQDEo",
        "Plague": "1FzwELCvdo8-pB5rO8gXtUwW7Ee08vI03sb2Q7iApYpY",
        "Bridesmaids": "17lg5B6T80z9u00P15vmEWBQt6zqll72sMj3i7PH0C00",
        "Wolverines": "1RaSuql3PNuvFLfN8678YgmGfcRQV6sVR5l76PtyAp4I"
    },
    bbbfflFormLinksOld: {
      "Crabs": "https://docs.google.com/forms/d/e/1FAIpQLSfbNPOqEJRvUDRRZaGj6KLYfEAZBEzPh3YwaPzLVH7cgeWebg/viewform",
      "JHAS": "https://docs.google.com/forms/d/e/1FAIpQLSec6Jq118RdUWuTrgg18YjhSEWWhsYywNjTPsf5HSROrTnjqg/viewform",
      "MotherRuckers": "https://docs.google.com/forms/d/e/1FAIpQLSdPK9Bnv_u-HKrTrtYKaYObmzLlY_Sk7azbeQnkYZuT0SWvWg/viewform",
      "Evils": "https://docs.google.com/forms/d/e/1FAIpQLSfULSzwfCda3J2ftjhYfh9F7YKODVl3nVT4xNpF2C3QERRY3A/viewform",
      "Poms": "https://docs.google.com/forms/d/e/1FAIpQLSccVFeIGwISHoghaakDuQlRLvurzmY-GtEmmgIBWCQ6Q17Azw/viewform",
      "1%ers": "https://docs.google.com/forms/d/e/1FAIpQLSdP6wd1S81rA7vGnK9CQ3ghGUFdh00hFhJc2137Oki7QdRhTg/viewform",
      "Hots": "https://docs.google.com/forms/d/e/1FAIpQLSfvuwrjdFzzKA8uKR19H8AEoviLWbTy9qxy7IliLzkDngbmJg/viewform",
      "Plague": "https://docs.google.com/forms/d/e/1FAIpQLScuI0P51g5uaH5vKTJ9DmDXro5CRjUuwClvvllPIgRiQvFFEQ/viewform",
      "Bridesmaids": "https://docs.google.com/forms/d/e/1FAIpQLSfhOyt7mMTPrKjMpj4WENjpDCygj3jVWeXA-QzWdJw0a7-JDA/viewform",
      "Wolverines": "https://docs.google.com/forms/d/e/1FAIpQLScJdWgKvXGPsVOWRN0MoZVtzAQbYfgHd-DbA0AwTwA3vVXQGg/viewform"
    },
    bbbfflFormLinks: {
      "Crabs": "1FAIpQLSfbNPOqEJRvUDRRZaGj6KLYfEAZBEzPh3YwaPzLVH7cgeWebg",
      "JHAS": "1FAIpQLSec6Jq118RdUWuTrgg18YjhSEWWhsYywNjTPsf5HSROrTnjqg",
      "MotherRuckers": "1FAIpQLSdPK9Bnv_u-HKrTrtYKaYObmzLlY_Sk7azbeQnkYZuT0SWvWg",
      "Evils": "1FAIpQLSfULSzwfCda3J2ftjhYfh9F7YKODVl3nVT4xNpF2C3QERRY3A",
      "Poms": "1FAIpQLSccVFeIGwISHoghaakDuQlRLvurzmY-GtEmmgIBWCQ6Q17Azw",
      "1%ers": "1FAIpQLSdP6wd1S81rA7vGnK9CQ3ghGUFdh00hFhJc2137Oki7QdRhTg",
      "Hots": "1FAIpQLSfvuwrjdFzzKA8uKR19H8AEoviLWbTy9qxy7IliLzkDngbmJg",
      "Plague": "1FAIpQLScuI0P51g5uaH5vKTJ9DmDXro5CRjUuwClvvllPIgRiQvFFEQ",
      "Bridesmaids": "1FAIpQLSfhOyt7mMTPrKjMpj4WENjpDCygj3jVWeXA-QzWdJw0a7-JDA",
      "Wolverines": "1FAIpQLScJdWgKvXGPsVOWRN0MoZVtzAQbYfgHd-DbA0AwTwA3vVXQGg"
    },
    bbbfflEntryMappings: {
      "Crabs": {
        teamName: "entry.1054594112",
        round: "entry.143347512",
        players: [
          "entry.1293288997", "entry.1675548604", "entry.618110812",
          "entry.1526618465", "entry.1037407442", "entry.1366594640",
          "entry.320356378", "entry.540361684", "entry.1334548148"
        ]
      },
      "JHAS": {
        teamName: "entry.2016212193",
        round: "entry.143347512",
        players: [
          "entry.1293288997", "entry.1675548604", "entry.618110812",
          "entry.1526618465", "entry.1037407442", "entry.1366594640",
          "entry.320356378", "entry.540361684", "entry.1334548148"
        ]
      },
      "MotherRuckers": {
        teamName: "entry.1819840682",
        round: "entry.143347512",
        players: [
          "entry.1293288997", "entry.1675548604", "entry.618110812",
          "entry.1526618465", "entry.1037407442", "entry.1366594640",
          "entry.320356378", "entry.540361684", "entry.1334548148"
        ]
      },
      "Evils": {
        teamName: "entry.191890383",
        round: "entry.143347512",
        players: [
          "entry.1293288997", "entry.1675548604", "entry.618110812",
          "entry.1526618465", "entry.1037407442", "entry.1366594640",
          "entry.320356378", "entry.540361684", "entry.1334548148"
        ]
      },
      "Poms": {
        teamName: "entry.830902413",
        round: "entry.143347512",
        players: [
          "entry.1293288997", "entry.1675548604", "entry.618110812",
          "entry.1526618465", "entry.1037407442", "entry.1366594640",
          "entry.320356378", "entry.540361684", "entry.1334548148"
        ]
      },
      "1%ers": {
        teamName: "entry.689050924",
        round: "entry.143347512",
        players: [
          "entry.1293288997", "entry.1675548604", "entry.618110812",
          "entry.1526618465", "entry.1037407442", "entry.1366594640",
          "entry.320356378", "entry.540361684", "entry.1334548148"
        ]
      },
      "Hots": {
        teamName: "entry.1232734393",
        round: "entry.143347512",
        players: [
          "entry.1293288997", "entry.1675548604", "entry.618110812",
          "entry.1526618465", "entry.1037407442", "entry.1366594640",
          "entry.320356378", "entry.540361684", "entry.1334548148"
        ]
      },
      "Plague": {
        teamName: "entry.872628076",
        round: "entry.143347512",
        players: [
          "entry.1293288997", "entry.1675548604", "entry.618110812",
          "entry.1526618465", "entry.1037407442", "entry.1366594640",
          "entry.320356378", "entry.540361684", "entry.1334548148"
        ]
      },
      "Bridesmaids": {
        teamName: "entry.1354918927",
        round: "entry.143347512",
        players: [
          "entry.1293288997", "entry.1675548604", "entry.618110812",
          "entry.1526618465", "entry.1037407442", "entry.1366594640",
          "entry.320356378", "entry.540361684", "entry.1334548148"
        ]
      },
      "Wolverines": {
        teamName: "entry.2143111154",
        round: "entry.143347512",
        players: [
          "entry.1293288997", "entry.1675548604", "entry.618110812",
          "entry.1526618465", "entry.1037407442", "entry.1366594640",
          "entry.320356378", "entry.540361684", "entry.1334548148"
        ]
      }
    },
    bbbfflFormSheets: {
      "1%ers": "Form Responses 3",
      "Crabs": "Form Responses 4",
      "Bridesmaids": "Form Responses 5",
      "Evils": "Form Responses 6",
      "Hots": "Form Responses 7",
      "JHAS": "Form Responses 8",
      "MotherRuckers": "Form Responses 9",
      "Plague": "Form Responses 10",
      "Poms": "Form Responses 11",
      "Wolverines": "Form Responses 12",
    }
  };
}