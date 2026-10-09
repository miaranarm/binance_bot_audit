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


## Post-merge validation — workflow #430 (2026-10-09)

- Run: https://github.com/miaranarm/binance_bot_audit/actions/runs/37961802717
- Tested branch/commit: `fix/exact-grid-profit-provenance` / `228ec1460458505573fcc192e2d17139ed588290`.
- The workflow completed successfully. The raw Grid Profit validator and final CSV validator both passed; the final CSV retained **29 columns** and **12,515 unique rows** with leverage <= 1.
- The validator reported: **exact Grid Profit = 0**, **reconstructed estimates = 11,213**, **unavailable = 1,302**, **unknown source = 0**, **ordering errors = 0**, **ratio errors = 0**, **floating-PnL errors = 0**, **source errors = 0**. Overall validator status: `PASS`.
- The 11,213 reconstructed rows are still estimates, not Binance-reported exact Grid Profit. The 1,302 unavailable rows remain unavailable; no values were promoted to exact.
- The scan collected 30,032 raw listing rows and retained 12,515 bots with leverage <= 1. Collection was explicitly **incomplete** for Marketplace types 12–20, so the run does not prove complete coverage of every bot family.
- The capital diagnostics sampled bots `3232564`, `6850680`, and `9161957`. Public listing payloads exposed `minInvestment` and null `initialMargin` for these Spot Grid examples; the diagnostic found no detail-response capital fields. These are candidate observations only, not proof that `minInvestment` equals total deployed capital.
- Conclusion after this run: the provenance guard and existing estimate pipeline passed automated checks, but the central research question remains unresolved. No exact public Grid Profit field or sufficient matched-fill-and-fee data was found in the tested responses. Do not change the CSV presentation or remove any estimate method.

## Next-source boundary: official Spot API fills are account-specific (2026-10-09)

The next investigation checked Binance's official Spot API documentation, because the Spot Grid formula requires filled buy/sell pairs and the actual fees.

- Binance's official Spot API documentation describes personal order history and trade history as signed, account-specific endpoints (USER_DATA); the trade response contains execution fields such as price, quantity, quote quantity, commission and commission asset. See the [official Spot API documentation repository](https://github.com/binance/binance-spot-api-docs) and [official trade-data guidance](https://www.binance.com/uk-UA/academy/articles/how-to-get-trading-data-via-the-binance-api).
- Those records describe the authenticated account's own trades. They are not a public endpoint for retrieving another marketplace bot operator's complete fills. Therefore they cannot be used by this public-marketplace scanner to reconstruct the exact Grid Profit of arbitrary third-party bots.
- Even if a user authenticated their own account, reconstructing Spot Grid Grid Profit from general account fills would still require reliably identifying the bot's fills, matching buy/sell legs according to Binance's grid rules, converting commissions paid in another asset at the correct price, and handling partial fills, rebates and terminated grids. A strategyId is not guaranteed on historical fills unless the order was created with that metadata.
- This is a boundary of the tested public-data approach, not proof that Binance has no internal/private endpoint or partner API. The audit will not attempt to bypass authentication or access another user's private data.

### Decision for the public bot audit

For third-party public Marketplace bots, proceed only with (a) a publicly accessible, successful Binance response that explicitly exposes Grid Profit and its unit/accounting basis, or (b) a documented Binance-supported public data source containing complete bot-specific fills and fees. Otherwise keep gridProfit exact blank, retain all current estimate methods/ranges, and preserve the current CSV presentation. Do not treat minInvestment, Marketplace pnl, roi, or matchedCount alone as sufficient to infer exact Grid Profit or the true capital deployed.


## Follow-up code-review finding: exact ratio vs scaled Grid Profit amount

Reviewing `grid_metrics()` after the source audit found a separate provenance issue to address before any exact values are trusted:

- When a detail endpoint supplies `Grid Profit` and `Total Profit`, the ratio can be calculated from those two same-source values.
- The current pipeline then may multiply that ratio by Marketplace `pnl` to create the CSV `gridProfit` amount, while labelling the amount `BINANCE_EXACT`.
- The Marketplace amount is expressed in USD, whereas Binance's Spot Grid FAQ says Grid Profit is displayed in the pair's quote asset. A ratio may be unit-invariant when both figures share the same valuation basis, but the scaled USD amount is not itself the raw exact Binance detail value unless the conversion basis and timestamp are verified.
- The same scaling issue can occur when detail `Total Profit` and `Floating Profit` are used to reconstruct detail `Grid Profit`, then the ratio is applied to Marketplace PNL.

**Required handling:** keep the same-source ratio and its provenance distinct from a USD amount derived by applying that ratio to Marketplace PNL. Do not label a scaled amount as the exact raw Grid Profit, and do not derive Floating Profit from a scaled amount as though it were exact. Existing estimates and CSV columns must remain intact.

This is a code-review finding, not a claim that current public scans actually contain qualifying detail values: the latest validation still reports 0 exact rows. No code was changed in this follow-up; a patch should be implemented with targeted regression tests before it can be considered fixed.
