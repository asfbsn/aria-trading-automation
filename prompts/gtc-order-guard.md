You are running a pre-open safety check: list every live order on the account so a
forgotten GTC order never fills unnoticed at the open again (this replaced a manual
mistake on 2026-08-25 where a stale GTC close order gapped-filled at 9:30 ET).

## What to do
1. Call `get_account_orders` (IBKR, read-only). This is the ONLY tool you may use.
2. List EVERY order returned — do not filter, do not judge which ones are "safe" to
   skip. The point of this check is that the human reviews all of them; a missed
   filter is exactly the failure mode this exists to prevent.
3. For each order, report on one line:
   `[Symbol] [primary_description] | [secondary_description] | status=[order_status] | order_id=[order_id] | age=[days since order_time, computed from order_time vs now]`
4. If `get_account_orders` returns zero orders, say so plainly: "No live orders." —
   do not treat an empty list as an error.
5. If any order's `order_status` is anything other than a clearly-live state (e.g.
   "REPLACED", "PENDING_CANCEL", or anything ambiguous), flag it explicitly as
   "STATUS UNCLEAR — verify manually in TWS/Client Portal" rather than guessing
   whether it's actually working.

## Output
Keep it short and scannable — this goes straight to Telegram, not a report file.
Format:

```
GTC/Order Guard — <date>
<N> live order(s) found.

[one line per order, as specified above]
```

Then, on its own final line, emit exactly:
ORDERS_CHECKED: <N>

Do not add commentary, recommendations, or risk assessment — this is a factual
listing only. The human decides what to do with each order.
