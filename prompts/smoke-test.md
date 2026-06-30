You are validating the ARIA daily-scan automation PLUMBING (smoke test — NOT the full scan).

Do this briefly, in order:
1. Call tv_health_check. ONLY if not connected, call tv_launch (kill_existing=true) and
   re-check. Do NOT relaunch if it is already connected.
2. Open the screener: ui_click by data-name "screener-dialog-button". Confirm the active
   preset is "Adi option swing 2.0" (ui_evaluate) and count its constituents.
3. Set the chart to AAPL on Daily (1D); read the "Premium Trading Dashboard" entry row
   (data_get_pine_tables) and report that row's text.
4. Print a 4-line OK summary: auth ok? bridge connected? screener constituent count?
   dashboard row readable?

Then, on its own final line, emit exactly (real tickers if you read them):
SCREENER_CONSTITUENTS: SYM1,SYM2,...

Keep under ~180 words. Do NOT run the full per-ticker scan.
