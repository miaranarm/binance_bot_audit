# TSTUSDT Grid Sweep V2

Data: 2026-01-01 → 2026-10-05. 26,628 candles 15m.

V2 comparative simulator: geometric Binance-style levels, OHLC intrabar sequencing, explicit pending orders, fees=0.10%. It is still an approximation, not Binance's internal engine.

Binance defines geometric grid levels by an equal price ratio and calculates grid profit after fees.

## Best safety-first result
**medium / 8 grids** — validation ROI 32.38%, MDD -15.14%; holdout ROI 15.31%, MDD -23.33%, matched orders 169.

Configurations with validation MDD <=5%: 0 / 24.

| range    |   grids |   lower |   upper |   train_roi |   train_mdd |   train_trades |   train_matched |   validation_roi |   validation_mdd |   validation_trades |   validation_matched |   holdout_roi |   holdout_mdd |   holdout_trades |   holdout_matched |   validation_score | safe5   |
|:---------|--------:|--------:|--------:|------------:|------------:|---------------:|----------------:|-----------------:|-----------------:|--------------------:|---------------------:|--------------:|--------------:|-----------------:|------------------:|-------------------:|:--------|
| medium   |       8 | 0.0145  | 0.0185  |    16.0411  |    -37.5589 |            378 |             187 |          32.3817 |         -15.1383 |                 104 |                   56 |       15.3074 |      -23.3304 |              338 |               169 |            2.13905 | False   |
| wide     |       8 | 0.014   | 0.019   |    18.265   |    -33.1558 |            303 |             150 |          31.6592 |         -15.1383 |                  78 |                   43 |       12.8276 |      -23.7192 |              236 |               118 |            2.09133 | False   |
| wide     |      12 | 0.014   | 0.019   |    13.3972  |    -36.769  |            628 |             311 |          31.1669 |         -15.1383 |                 174 |                   93 |       12.7888 |      -23.7208 |              548 |               274 |            2.05881 | False   |
| narrow   |       8 | 0.01525 | 0.01775 |    10.8355  |    -34.1747 |            513 |             255 |          30.9721 |         -15.1383 |                 154 |                   81 |       16.4834 |      -22.6035 |              596 |               298 |            2.04594 | False   |
| baseline |       8 | 0.015   | 0.018   |    15.7654  |    -30.2899 |            470 |             234 |          30.7175 |         -15.1383 |                 124 |                   66 |       18.7152 |      -22.7712 |              536 |               268 |            2.02912 | False   |
| medium   |      12 | 0.0145  | 0.0185  |    13.1379  |    -32.5862 |            750 |             373 |          30.14   |         -15.1383 |                 200 |                  106 |       14.1582 |      -23.7959 |              750 |               375 |            1.99097 | False   |
| wide     |      16 | 0.014   | 0.019   |    10.4893  |    -36.8634 |           1040 |             516 |          29.7199 |         -15.1383 |                 251 |                  133 |       12.2341 |      -24.0281 |              972 |               486 |            1.96322 | False   |
| baseline |      12 | 0.015   | 0.018   |     9.68504 |    -32.8575 |            924 |             460 |          29.1082 |         -15.1383 |                 250 |                  131 |       14.6093 |      -23.4234 |             1058 |               529 |            1.92281 | False   |
| medium   |      16 | 0.0145  | 0.0185  |     9.09726 |    -35.5895 |           1249 |             621 |          28.9421 |         -15.1383 |                 287 |                  151 |       13.2824 |      -23.9813 |             1324 |               662 |            1.91184 | False   |
| wide     |      20 | 0.014   | 0.019   |     7.94276 |    -35.657  |           1479 |             735 |          28.541  |         -15.1383 |                 363 |                  191 |       11.6894 |      -23.9997 |             1518 |               759 |            1.88534 | False   |