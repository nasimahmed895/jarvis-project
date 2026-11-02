# Crypto Market Research — JARVIS Add-on

Spot-market research and decision-support module for the existing Mark LV
JARVIS application. **Research only.** No live orders. No live trading.

## What JARVIS now does

Say "Jarvis, research Bitcoin" (or Ethereum, etc.) and JARVIS will:

1. Pull public spot OHLCV from Binance for the configured timeframes.
2. Validate the data: OHLC sanity, duplicates, gaps, future-dated candles,
   timeframe-aware staleness.
3. Run multi-timeframe analysis + the baseline strategy + risk-aware
   trade-setup generation.
4. Produce a **final decision** — one of **BUY BIAS**, **SHORT BIAS**, or
   **NO TRADE** — that policy-combines the strategy call with the
   multi-timeframe bias.
5. Speak a concise Bengali summary with: final decision, what to do now
   (spot-only guidance), entry, stop-loss, target, risk/reward, reason,
   and the next condition to wait for.
6. Save detailed Markdown + JSON reports to your `~/Downloads/JARVIS Reports`
   folder (or `~/Documents/JARVIS Reports` / project `reports/` /
   tempfile — see §Report locations).
7. Record every actionable forecast immutably so later outcomes can be
   evaluated.

## What JARVIS deliberately does NOT do

- Does not place real orders.
- Does not read, log, modify, or rotate the Gemini API key.
- Does not fabricate news, prices, levels, or probabilities.
- Does not instruct the user to short-sell on `SHORT BIAS` (this is a
  spot-only project — a short bias becomes "wait, avoid buying").
- Does not promise profits.

## Final-decision policy

Documented in `market/pipeline.py::compute_final_decision`:

- `strategy=NO TRADE` → final is **NO TRADE**.
- `multi_tf=NO TRADE` → final is **NO TRADE**.
- `strategy direction` disagrees with `multi_tf direction` → **NO TRADE**.
- Both agree on a direction → that direction (**BUY BIAS** or **SHORT BIAS**).

The spoken response, the on-disk Markdown, the JSON `final_decision` and
`conclusion` keys, and the Markdown headline all show the same label. A
regression test suite (`tests/unit/test_report_integrity.py`,
`tests/integration/test_pipeline_advisory.py`) guards this.

## Voice response contract (Bengali)

Every response contains, in order:

1. **Final decision** (BUY BIAS / SHORT BIAS / NO TRADE).
2. **What to do now** — plain-language action.
   - BUY BIAS: conditions to meet before considering a spot buy.
   - SHORT BIAS: never a short-sell instruction. Explains why to wait or
     avoid buying.
   - NO TRADE: explains why no entry is valid yet.
3. **Entry** — price or condition, or `কোনো valid entry এখনো নেই`.
4. **Stop-loss** — price when valid, otherwise `প্রযোজ্য নয়`.
5. **Target** — same policy as stop-loss.
6. **Risk/Reward** — numeric when derivable from a valid entry/stop/target.
7. **Reason (Bengali)** — key indicators + timeframe conflicts.
8. **Next step** — what signal or price condition to wait for.

The timeframe is embedded in the opening sentence
(`স্যার, Bitcoin-এর 1d analysis শেষ...`).

## Report locations

Reports are written to the first writable candidate from:

1. `~/Downloads/JARVIS Reports/` (preferred — the real OS Downloads folder).
2. `~/Documents/JARVIS Reports/`.
3. The project-local `reports/` directory configured in `market_config.report_dir`.
4. A system temp folder (`<tempdir>/JARVIS Reports/`).

The pipeline announces the resolved path only after confirming the files
exist. If a fallback is used, the Research warning list says so.

Each report has a unique ID: `analysis-<SYMBOL>-<UTC-ISO>-<10char>.{md,json}`.

## Reliability

Handled with explicit tests:

| Scenario | Behaviour |
|---|---|
| Binance API error | pipeline records the failure in `warnings`, drops the failing timeframe, continues with the rest. If every timeframe fails, final decision is `NO TRADE` with reason `no timeframe produced usable data`. |
| Future-dated candle (clock skew > 60 s) | dropped by `validate_frame`; count surfaced in `data_quality.warnings`. |
| Candle not yet closed | excluded from analysis by `closed_candles_only` before any indicator runs. |
| Timeframe-aware staleness | `max(configured_floor, 1.5 × timeframe_seconds)`. 1d candles allow ~36 h since close; 1 m candles use the 120 s floor. |
| Missing directory | `resolve_report_dir` creates it; falls through to the next candidate on OSError. |
| File-write error | caught at the pipeline boundary; `ResearchResult` returns with blank paths and a warning. The Bengali spoken response is still produced. |
| Application restart | paper account state persists in SQLite and reloads untouched. |
| Secrets in logs | logger output never includes API tokens or user config values. |

## Configuration

Optional. Add a `market_config` block to `config/api_keys.json`. Example
with all defaults expanded:

```json
{
  "market_config": {
    "provider": "binance_public",
    "quote_currency": "USDT",
    "assets": ["BTC", "ETH"],
    "intraday_timeframes": ["1m", "5m", "15m", "1h"],
    "swing_timeframes": ["4h", "1d"],
    "lookback_candles": 1000,
    "data_staleness_seconds": 120,
    "report_dir": "reports",
    "db_path": "memory/market.db",
    "log_level": "INFO",
    "spot_only": true,
    "paper_trading": {
      "initial_balance_usd": 10000,
      "risk_per_trade": 0.01,
      "max_concurrent_positions": 3,
      "max_daily_loss": 0.03,
      "max_drawdown": 0.15,
      "fee_rate": 0.001,
      "slippage_bps": 5
    }
  }
}
```

Illustrative values. `spot_only` cannot be set to `false`. The
`report_dir` entry is a project-local fallback — Downloads/Documents are
preferred for the saved files themselves.

## Running JARVIS

```bash
python main.py
```

- No startup changes compared with the vanilla JARVIS launch.
- The `market_research` action is auto-discovered at startup; a success
  line "Action loaded: market_research (market_research.py)" appears in
  the launch log.
- Say any of:
  - "Jarvis, research Bitcoin."
  - "Analyze Ethereum on swing."
  - "Market check on BTC intraday."

## Running tests

```bash
python3 -m pytest tests/ -q
```

Latest run (verified in this delivery): **185 passed, 0 failed, 0 skipped,
exit 0**, 8.08 s on Python 3.14.

## File layout

```
market/
  config.py / exceptions.py / interfaces.py / types.py
  logging_setup.py / pipeline.py / advisory.py
  providers/binance_public.py
  data/validator.py  data/cache.py
  indicators/{trend,momentum,volatility,volume}.py
  analysis/{structure,regime,multi_tf,context}.py
  strategies/{base,registry,ma_trend_v1}.py
  risk/{sizer,gates}.py
  backtest/{engine,metrics}.py
  paper/{store,account}.py
  forecast/tracker.py
  reports/{paths,writer,analysis_report,location}.py
  storage/db.py

actions/market_research.py                      auto-discovered voice command

tests/
  unit/            19 test files, deterministic
  integration/     test_pipeline.py, test_pipeline_advisory.py, test_pipeline_reliability.py
  regression/      test_imports_smoke.py
  fixtures/synth.py
```

## Known limitations (verified)

- The baseline `ma_trend_momentum` strategy is intentionally conservative.
  On flat / early-trend data it often returns NO TRADE. This is by design.
- No news / sentiment integration in v1 — reported honestly as unavailable.
- No order-book, funding, or open-interest data in v1.
- Live-trading paths do not exist in the code and are not reachable.
- The Binance public provider is covered by mocked tests only. Live network
  behaviour has not been exercised in CI — set up a `@pytest.mark.network`
  opt-in test if you need that coverage.
- Confidence scores are heuristic, not calibrated probabilities.

## Safety boundaries (enforced by tests)

- `spot_only=false` → `ConfigError`.
- Paper-trading short selling is OFF by default.
- Paper + pipeline modules scanned for forbidden tokens
  (`requests.post`, `requests.put`, `place_order`, `live_order`,
  `/api/v3/order`) — tests fail if introduced.
- Gemini key is never read, logged, or referenced by any `market/*`
  module. Verified by scanning the source for non-docstring occurrences.
- Report / database paths resolved through `resolve_report_dir` only
  allow Downloads, Documents, the project-root subtree, or tempfile.
