# Capturing Adi's real screener output (ground truth for audit_tier1.py)

This step needs an interactive agent session driving TradingView Desktop
over CDP (tradingview-bridge MCP) -- it cannot be done by a standalone
script. Two attempts were tried 2026-09-10; neither fully succeeded, both
are documented here so the next attempt doesn't repeat them blind.

## What was tried and why it didn't finish

**Network capture (preferred, byte-exact).** Hooked `window.fetch` /
`XMLHttpRequest.prototype.send` for any request to `scanner.tradingview.com`,
then tried to force a fresh query by reopening the Filters panel and
clicking Ok with no changes -- no request fired (nothing had changed, so the
client didn't re-query). Also tried patching `window.WebSocket`, but the
screener's socket connection was already open before the hook was installed,
so patching the constructor after the fact catches nothing. Never got as far
as forcing an actual value change: the filter values (the "50" in `RSI, 20`,
the "0%-10%" bands) are rendered as styled `<span>`s from a custom
inline-edit widget, not `<input>` elements -- `ui_click`'s text/aria-label/
data-name/class-contains selectors didn't reliably hit the actual editable
target within reasonable effort.

**DOM scrape of the results table.** Grabbing ticker text directly from the
page picked up a mix of two problems: (a) an unrelated watchlist/index panel
(SPY, SPX, VIX, DXY, ...) got swept in because the selector was scoped to
the whole page, not just the "Adi option swing 2.0" screener widget, and
(b) TradingView virtualizes the results table -- only ~30-40 rows exist in
the DOM at any moment, not all 127 -- so even a correctly-scoped scrape
without scrolling returns a partial list.

## Recommended approach for next time

Combine both fixes:

1. Scope every query to the specific screener container, not `document`.
   Find it once via a stable anchor (e.g. the element containing the exact
   text "Adi option swing 2.0"), then `closest()` up to its panel root, and
   query only within that root.
2. Scroll-and-collect: repeatedly scroll the results table's internal
   scroll container (not the page) by its visible height, reading and
   accumulating ticker text after each scroll, until the accumulated count
   stops growing or reaches the badge's stated count (127 as of
   2026-09-10). `ui_evaluate` can drive both the scroll and the read in one
   call.
3. For the filter *values* (if byte-exact capture is still wanted): use
   `ui_find_element` with `strategy: "css"` to inspect the actual DOM
   structure around the "50" span first (parent classes, siblings) before
   attempting a click -- don't guess a selector blind. Or skip this
   entirely: the symbol list from step 2 is sufficient ground truth for
   `audit_tier1.py`'s diff; the exact filter wire-format is only needed if
   you also want to *reconstruct and replay* the query via `requests` or
   the `tradingview-screener` library, which is a nice-to-have, not a
   requirement for the audit to work.

## Fixture format audit_tier1.py expects

```json
{
  "captured_at": "2026-09-10T21:15:00+03:00",
  "method": "dom_scroll_scrape",
  "market_open": false,
  "symbols": ["SYY", "BBIO", "AXSM", "NASDAQ:AAPL", "NYSE:BRK.B", "..."]
}
```

Ticker format doesn't matter (`NASDAQ:AAPL`, `BRK.B`, `BRKB` all normalize
the same way) -- see `normalize_symbol()` in `audit_tier1.py`.

## Known-partial fixture on disk

`fixtures/adi_screener_provisional_2026-09-10.json` is the raw scrape from
tonight's first attempt -- explicitly marked `"method":
"dom_scrape_partial_UNVERIFIED"`. It mixes in index/ETF names from the
watchlist bleed-through described above and almost certainly isn't the full
127. Good enough to prove `audit_tier1.py`'s mechanics run end to end;
**do not treat its diff output as a real parity result** until re-captured
per the recommended approach above.
