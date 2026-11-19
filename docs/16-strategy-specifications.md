# Strategy specifications

| ID | Decision contract |
| --- | --- |
| DERIV-STRAT-01 | Closed M5 EMA20/50 pullback and RSI recovery, ADX and M15 trend confirmation |
| DERIV-STRAT-02 | Closed-bar Donchian breakout, ATR volatility and entry distance, cooldown |
| DERIV-STRAT-03 | Bollinger excursion and re-entry with RSI recovery under low ADX |
| KALSHI-STRAT-01 | Independently sourced probability and uncertainty versus executable price and costs |
| KALSHI-STRAT-02 | Inventory-aware post-only quote with spread, depth, staleness and adverse-selection gates |
| KALSHI-STRAT-03 | Verified compatible logical relation, executable depth, costs and non-atomic leg risk |

Decisions must include reason, version, provenance and NO_SIGNAL when inputs or thresholds fail. Do not infer a payout from OHLC alone. Never use forward observations for strategy decisions. Separate fixture calibration and actual probabilistic evidence.
