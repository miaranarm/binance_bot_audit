# Binance Grid Profit source audit — 2026-10-09

## Objective

Find a Binance-origin field/API that provides **Grid Profit** separately from **Total Profit** and **Floating Profit**, without changing the existing CSV/table presentation or silently replacing the existing estimates.

## Evidence from workflow #423

- Workflow: https://github.com/miaranarm/binance_bot_audit/actions/runs/37957948595
- Commit under test: `f9c502b4e6205367db3caf1bbc72eec9abb70079`
- Run completed successfully. This confirms the diagnostic workflow ran; it does not mean an exact Grid Profit source was found.
- Captured 576 BAPI requests and 365 BAPI responses.
- 13 sampled Spot Grid detail pages were captured. All 13 showed the same public-page limitations: `Pending Trigger`, placeholder duration/creation time, a login prompt, and placeholder PNL fields.
- The visible values were `Total Profit = 0.00000000`, `Grid Profit = 0.00000000`, and `Floating Profit = --`. Because the pages were pending-trigger/login-limited placeholders, these are **not valid measurements** of bot performance.
- The authentication check returned HTTP 200 but an application-level error: code `100001005`, `success=false`, message `Please log in first.` This occurred 41 times in the capture. HTTP 200 alone therefore must not be interpreted as a successful authenticated response.
- The captured response schemas contained **zero occurrences** of `gridProfit`, `totalProfit`, `floatingProfit`, `profitPerGrid`, or `matchedProfit`.
- Public `queryTopStrategy` exposes fields such as `roi`, `pnl`, `runningTime`, `strategyParams`, `matchedCount`, and `minInvestment`; no separate Grid Profit field was present in its captured schema.
- Public `queryRoiChart` provides ROI/PnL time-series data, not the separate Grid Profit / Total Profit / Floating Profit breakdown.

## Official Binance accounting definitions

Binance's [Spot Grid Trading Parameters](https://www.binance.com/en/support/faq/detail/688ff6ff08734848915de76a07b953dd), updated 2026-01-07, defines:

- **Grid Profit** as the sum of profit from completed buy/sell matched pairs, in the quote asset.
- Each matched-pair profit uses the filled sell value minus the filled buy value and the applicable trading fees (including conversion of base-asset fees at the relevant last price).
- **Total Profit = Grid Profit + Unrealized PnL** for Spot Grid.
- **Profit/Grid** can be calculated from the grid bounds, grid count, grid mode and fee rate.

This confirms the accounting meaning and supports the existing calculated **Profit/Grid** field. It does **not** provide enough information to calculate a bot's cumulative exact Grid Profit from public Marketplace ROI, PNL and matched-trade count alone. To reproduce cumulative Grid Profit exactly, the audit needs the actual filled buy/sell amounts and applicable fees for matched pairs, or a Binance response that supplies the aggregate metric directly.

For Futures Grid, Binance documents a different breakdown: total profit includes net realized profit, unrealized PnL and funding fees. Do not apply the Spot Grid formula to Futures Grid rows.

## Conclusion

**No exact public Grid Profit source has been identified in this capture.** The page labels are not enough: placeholder zeros on pending-trigger pages must not be accepted as exact values, and total PNL/ROI cannot safely be relabelled as Grid Profit.

This is a finding about the endpoints and pages observed in this run, not proof that no such field exists anywhere in Binance.

## Rules for the audit pipeline

1. Preserve the existing table presentation and columns.
2. Preserve existing Grid Profit estimates; label them as estimates and keep their provenance explicit.
3. Never promote a placeholder zero, `--`, absent field, or login-limited response to an exact value.
4. Treat an API response as failed when its application envelope says `success=false` or carries a non-success code, even if HTTP status is 200.
5. Only label a Grid Profit value **exact** when a Binance response explicitly supplies the metric or an independently verified official formula and sufficient source inputs reproduce it.
6. Keep estimated Grid Profit and any derived ratio separate from Binance-reported Total PNL. Do not infer exact Grid Profit by subtracting an unverified floating-profit estimate.

## Next investigation

Continue looking for an endpoint or authenticated/public response that explicitly returns matched-grid profit and the corresponding total/floating PNL breakdown for an actual running Spot Grid bot. If authentication is required, document the access boundary and do not attempt to bypass it. Until that evidence is found, retain the current estimates rather than presenting them as Binance-reported facts.

## Additional official-source cross-check (2026-10-09)

Binance's [Bot Marketplace landing-page FAQ](https://www.binance.com/en/support/faq/detail/f0c2bd5bc16c40b9998d22549e91cd1c) clarifies an important distinction:

- For **Spot Grid**, Marketplace PNL is defined as **Current Value − Total Investment**. It is a total-value measure, not the Grid Profit component.
- For **Futures Grid**, Marketplace PNL may be **Matched PNL + Funding Fee**, and Binance notes that unmatched PNL is not included in that Marketplace PNL definition. This differs from the detail-page accounting breakdown, where Total Profit includes net realized profit, Unrealized PnL and Funding Fees.
- Consequently, the same field label `pnl` cannot be treated as one uniform accounting basis across Spot and Futures strategies. Any exact ratio or reconciliation must be strategy-family-specific and source-specific.

Binance's [Futures Grid FAQ](https://www.binance.com/en/support/faq/detail/f4c453bab89648beb722aa26634120c3) further distinguishes matched profit, unmatched PnL, realized profit, unrealized PnL and funding fees. For Futures Grid, the audit must not substitute Spot Grid's `Total Profit = Grid Profit + Unrealized PnL` identity without accounting for funding and Binance's definition of matched profit.

### Consequence for the next endpoint investigation

The current public Marketplace fields are not sufficient to reconstruct exact Spot Grid Grid Profit by themselves. The next useful evidence would be either:

1. a successful response for an actual running bot that explicitly returns Grid Profit and its accounting asset; or
2. complete matched fill pairs and fee amounts, with a verified matching rule and conversion prices, sufficient to reproduce Binance's aggregate.

A response that returns HTTP 200 but has an application-level failure, login prompt, pending-trigger placeholder or empty metric is not qualifying evidence. A field name alone is also insufficient: its endpoint, bot ID, asset/unit, accounting basis and relationship to the displayed Total Profit must be verified.

## Calculation-preservation requirement

**No existing calculation method is to be removed or replaced during this investigation.** The current estimated Grid Profit reconstruction, its low/mid/high range, the estimated Grid-Profit/Total-Profit ratio range, calculated Profit/Grid, and all existing provenance/status fields remain in place. This PR narrows what may be labelled as an exact Binance accounting value; it does not retire the estimate path. A future official source may replace an existing estimate only after cross-checking multiple actual bots and documenting the source, unit, accounting basis and reconciliation results. The existing CSV/table presentation must remain unchanged.

