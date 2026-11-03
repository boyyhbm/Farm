# Weekly reward periods and evidence

This application does not derive CS2 reward weeks from the server's calendar
week and does not infer Weekly Care Package receipt from an inventory change.
Inventory changes and confirmed reward records are separate data sets.

A period is an explicit `[starts_at, ends_at)` interval in UTC with a required
`source`. An administrator records the actual reset boundary after checking the
current official client/server behavior or a current reliable published source.
Keep a dated citation and timezone conversion in `source`; preserve an internal
screenshot or observation reference when it is the evidence. Once drops reference
a period, retain that period as historical evidence rather than changing its dates.
No weeks are seeded automatically.

## Verification status, 2026-11-03

The implementation session could retrieve Valve's public news and Steam Help
pages, but did not obtain a current authoritative statement of the precise UTC
weekly reset instant. Several third-party search results/articles were unavailable
(403/404) or did not provide usable evidence. Accordingly, the software contains
**no asserted default reset weekday/time**, no unverified conversion of Pacific
time to UTC, and no claim that a Monday-to-Sunday calendar week is the CS2 cycle.

This is an explicit external-verification limitation. Configure verified periods
before recording rewards. The UI/API permit source-backed manual intervals so the
ledger remains usable while keeping the missing verification visible. An article's
publication date alone does not establish that its reset rule remains current;
check the current client or current Valve statement before automating future periods.

## Confirmation rules

- `NOT_RECORDED`: no reward information has been entered for that period.
- `PENDING_REVIEW`: a human or an import has reported a possible reward.
- `CONFIRMED`: a human confirms the reward with its evidence source.
- `NOT_RECEIVED`: explicitly recorded absence, rather than an inference from a
  private/unavailable inventory.

New inventory assets produce `added` changes only. They never automatically
create a confirmed drop. Item listing quotes are estimates before fees, and an
unknown quote remains unknown. Neither a drop valuation nor Steam Wallet balance
is recorded as realized cash revenue. Actual cash sales are entered separately
with their fees.
