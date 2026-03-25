# Lumibot PoC Evaluation Report

**Date:** 2026-03-24
**Strategy:** TrendPullback v1.0.0 (BTC, H1, Long-only)
**Lumibot Version:** 4.4.56
**Status:** PASS

---

## 1. Signal Concordance

| Metric | Value |
|--------|-------|
| Reference signals | 8 |
| Lumibot signals | 8 |
| Exact match | 8 |
| **Concordance rate** | **100.00%** |
| Missing in Lumibot | 0 |
| Extra in Lumibot | 0 |

**Threshold:** >= 95%. **Result: PASS**

The Lumibot implementation produces bit-identical signals to the reference strategy
when given the same pre-computed features and data. This validates that the signal
detection logic can be faithfully ported to Lumibot's event-driven framework.

---

## 2. Development Time Estimate

| Task | Estimate |
|------|----------|
| Lumibot API exploration & data format wiring | ~2h |
| Strategy class implementation | ~1h |
| Signal comparison framework | ~1h |
| DST / timezone edge case debugging | ~1h |
| Tests & validation | ~1h |
| **Total** | **~6h** |

---

## 3. Maintainability Assessment

### 3.1 Dependencies

| Dependency | Version | Size | Risk |
|-----------|---------|------|------|
| lumibot | 4.4.56 | Heavy (~80+ transitive deps) | Medium-High |
| pandas | 2.x | Already in stack | Low |
| numpy | 1.x | Already in stack | Low |

**Concern:** `pip install lumibot` pulls in ~80 packages including: yfinance, ccxt, alpaca-py,
schwab-py, polygon-api-client, plotly, quantstats, openai, flask, sqlalchemy, ibapi,
and many others. Most are broker-specific integrations that this project does not need.

### 3.2 Testability

| Aspect | Rating | Notes |
|--------|--------|-------|
| Unit testability | Good | Strategy logic can be tested by calling `on_trading_iteration()` directly |
| Integration testability | Fair | Full backtest requires `PandasDataBacktesting` + `Data` entity wiring |
| Signal extraction in tests | Good | Direct instantiation via `__new__` + `initialize()` works |
| Deterministic tests | Good | Synthetic data with fixed seed ensures reproducibility |

### 3.3 Extensibility

| Aspect | Rating | Notes |
|--------|--------|-------|
| Multi-timeframe | Fair | Lumibot's `sleeptime` controls bar iteration; multi-TF requires manual feature injection |
| Live trading | Good | Lumibot has Alpaca/IBKR/ccxt adapters out-of-box |
| Custom indicators | Fair | Must pre-compute externally or use `get_historical_prices()` API |
| Signal logging | Good | Custom signals can be appended in `on_trading_iteration()` |
| InfluxDB integration | Manual | No built-in InfluxDB writer; would need custom adapter |

### 3.4 Architecture Fit

| Dimension | Existing Stack (Nautilus Lab) | Lumibot | Verdict |
|-----------|-------------------------------|---------|---------|
| Backtest engine | Custom iterative loop | Event-driven (Strategy class) | Different paradigm |
| Data pipeline | Polars/Pandas + custom ETL | PandasDataBacktesting / Yahoo / broker APIs | Lumibot more opinionated |
| Signal format | Custom schema (BacktestOutput) | No built-in signal schema | Must bridge |
| Monitoring | InfluxDB + Grafana | quantstats tearsheets | Different approach |
| Execution | Shioaji / Binance adapters | Alpaca / IBKR / ccxt | Different broker focus |

---

## 4. Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| **Heavy dependency tree** | High | Pin version; consider vendoring only needed modules |
| **DST timezone handling** | Medium | Lumibot defaults to US-Eastern; crypto (24/7) requires explicit UTC config |
| **Event loop opacity** | Medium | Backtest timing may not match bar-exact expectations; need validation per strategy |
| **Breaking changes** | Medium | Lumibot API changes frequently (4.x series); pin version strictly |
| **No InfluxDB integration** | Low | Build custom writer (similar to existing influx_writer.py) |
| **Community size** | Low | Smaller community than zipline/backtrader; fewer battle-tested edge cases |

---

## 5. Test Results

```
14 passed, 3 warnings in 10.74s

tests/test_lumibot_poc.py::TestSampleData::test_deterministic PASSED
tests/test_lumibot_poc.py::TestSampleData::test_generate_btc_1m_shape PASSED
tests/test_lumibot_poc.py::TestSampleData::test_ohlc_consistency PASSED
tests/test_lumibot_poc.py::TestSampleData::test_resample_reduces_rows PASSED
tests/test_lumibot_poc.py::TestReferenceSignals::test_daily_gate_columns PASSED
tests/test_lumibot_poc.py::TestReferenceSignals::test_extract_signals_returns_list PASSED
tests/test_lumibot_poc.py::TestReferenceSignals::test_features_columns PASSED
tests/test_lumibot_poc.py::TestReferenceSignals::test_signal_format PASSED
tests/test_lumibot_poc.py::TestReferenceSignals::test_signals_to_df PASSED
tests/test_lumibot_poc.py::TestSignalConcordance::test_concordance_at_least_95_percent PASSED
tests/test_lumibot_poc.py::TestSignalConcordance::test_empty_signals_concordance PASSED
tests/test_lumibot_poc.py::TestSignalConcordance::test_exact_match PASSED
tests/test_lumibot_poc.py::TestLumibotStrategyClass::test_strategy_has_required_parameters PASSED
tests/test_lumibot_poc.py::TestLumibotStrategyClass::test_strategy_inherits_from_lumibot PASSED
```

---

## 6. Reproducible Commands

```bash
# Install
pip install lumibot

# Run signal comparison
python -m poc.lumibot.compare_signals

# Run full Lumibot backtest engine
python -m poc.lumibot.run_lumibot_backtest

# Run tests
python -m pytest tests/test_lumibot_poc.py -v
```

---

## 7. Recommendation

**Verdict: PASS — Lumibot adopted as unified strategy framework (backtest + live).**

PoC validates that signal logic ports faithfully (100% concordance) and the
pre-computed feature injection pattern is compatible with the existing ETL pipeline.
Trade-offs (heavy deps, no built-in InfluxDB, DST handling) are accepted and documented
in the Risks table above (Section 4).

Full decision rationale, architecture design, and re-evaluation triggers:
see `docs/framework_selection_report.md` and `docs/trading_framework_blueprint.md`.
