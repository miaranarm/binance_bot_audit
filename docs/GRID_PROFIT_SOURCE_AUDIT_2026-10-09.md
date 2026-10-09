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
