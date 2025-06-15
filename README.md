# BBBFFL_Scoring

An automated scoring system for the **Big Bad Bustling Fantasy Football League (BBBFFL)** using Google Apps Script (GAS) and Google Sheets, with support tools written in Node.js and Bash.

## 💡 Overview

This project automates the collection of AFL player stats and applies **BBBFFL custom scoring rules** to calculate fantasy results each round.

It is structured around three subprojects (managed via [`clasp`](https://github.com/google/clasp)):

- **`AFL_Stats/`** – Retrieves AFL match, player, and stat data via custom scraping scripts and stores them in Google Sheets.
- **`BBBFFL_Weekly_Teams/`** – Processes weekly team submissions (via Google Forms) to generate valid team sheets per coach.
- **`BBBFFL_Results/`** – Combines stats and team selections to automatically compute and validate weekly results.

## 🔄 Project Tools

Includes helper scripts:
- `pull-all.sh` – Pulls latest updates from Google Apps Script into local folders.
- `push-all.sh` – Pushes local script updates back to Google Apps Script.

## 🚧 Transition Note

This system **formerly used [api-football.com](https://www.api-football.com/)** (via their AFL endpoints) for stat ingestion, but is now being **fully migrated to a custom AFL data scraper** tailored for BBBFFL needs.

Once migration is complete, all `api-football` dependencies will be removed entirely.

## 📦 Requirements

- Node.js + `clasp` installed
- Google Apps Script linked projects (see `.clasp.json` in each subfolder)
- Git for version control

---

## 📝 Future Goals

- Migrate all stat endpoints to internal scraper API
- Enhance player matching and error handling
- Integrate live stats viewer and weekly fixture preview
