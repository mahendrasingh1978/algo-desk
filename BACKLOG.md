# ALGO-DESK — Project Backlog

> Managed by Claude Code. Updated as tasks are added, started, or completed.
> Version history tracked via git commits.
> **Never delete completed items — they are the project history.**

---

## STATUS KEY
- `[ ]` To Do
- `[~]` In Progress
- `[x]` Done

---

## 🔴 HIGH PRIORITY

- [x] **H1 — Gemini API test endpoint** — `/api/ai/test` exists and looks correct. Verify with real key after deploy.
- [x] **H2 — precheck.py ternary quote check** — Added `)'` pattern detection to catch common JS string errors
- [ ] **H3 — Gap day strategy enhancement** — S8 prev-day move rules need more nuance. POSTPONED until rules defined.
- [ ] **H4 — Ghost engine prevention** — When an automation is restarted, the old `asyncio.Task` is orphaned (not cancelled). The old engine loop keeps running with its stale state, invisible to `active_engines`, and can place duplicate trades. Fix: (1) track tasks in `_engine_tasks` dict, cancel old task on restart; (2) add self-check inside `_run_engine` loop — if `active_engines[user_id][auto_id] is not state`, stop immediately. Confirmed cause of today's duplicate S4 trades.
- [ ] **H5 — Smart re-entry: replace blanket `traded_today` gate** — Currently one trade per automation per day, hard-blocked by `traded_today=True`. `max_trades_per_day: 3` config is dead code. Fix: allow re-entry only after profitable exits (PROFIT_TARGET, PROFIT_LOCK, EMA75_SL with positive P&L). Never re-enter after MAX_LOSS or VWAP_SL at a loss. S9 (expiry theta crush) should always run independently of morning trades — it fires in a different time window with a different mechanism. `max_trades_per_day` becomes the hard ceiling. **Discuss before implementing — capital risk implications.**
- [ ] **H6 — Live NSE holiday calendar sync** — `_market_open_now()` does not check NSE holidays — engine will try to trade on market holidays. Fix: (1) add `/api/market/holidays/sync` backend endpoint using `pandas_market_calendars` (NSE calendar) + Nager Date API for national holidays; (2) auto-sync on server start + weekly background job; (3) update `_market_open_now()` to check `trading_events` table for today's holiday; (4) add "Sync from NSE" button in Calendar page; (5) add election result dates as a new event category. **Prerequisite: `docker builder prune -f` to free 762MB disk first.**
- [ ] **H7 — Disk cleanup (immediate)** — Server is at 99% disk usage (121MB free). Run `docker builder prune -f` (frees 762MB build cache) and `docker image prune -f` (frees ~213MB old images). Must be done before any new features that write data.

---

## 🟡 MEDIUM PRIORITY

- [x] **M11 — Ratchet (Profit Lock) SL + dead zone fix**
- [x] **M12 — Live market data feeds: VIX, prev_close, gap guards now wired**
- [x] **M10 — VWAP SL buffer 2% → 5%**
- [x] **M9 — Hedge leg order sequence + 1000pt hedge width**
- [x] **M6 — S4 live ATM re-centering**
- [x] **M7 — Live ATM for S3, S5, S6, S8 post-9:30**
- [x] **M8 — S9 re-initialise strikes at 10:30**
- [x] **M1 — S2 real-world validation**
- [x] **M2 — Multi-symbol market data**
- [x] **M3 — Automation edit**
- [x] **M4 — Performance page**
- [x] **M5 — Git → deploy cycle test**

- [ ] **M13 — Backtest page enhancements** — Current page only shows basic stats on paper trades. Add: (1) **Drawdown analysis** — max drawdown ₹ and %, longest drawdown period, recovery days; (2) **Risk-adjusted returns** — Sharpe ratio, Sortino ratio (uses downside deviation only); (3) **Hour-of-day heatmap** — win rate and avg P&L by entry hour (9:15, 9:30, 10:00, etc.) — shows best entry windows per strategy; (4) **Max Adverse Excursion (MAE)** — how far against us trades went before exit — tells if SL is too tight; (5) **Strategy correlation** — are strategies all losing on same days? (6) **Parameter sensitivity** — slider to see how P&L changes with ±25pt wider/tighter SL. All computed from existing `shadow_trades` data — no new data source needed.

- [ ] **M14 — Server upgrade** — Current server: 1GB RAM, 8GB disk. Already using 325MB swap during normal operation. Needs upgrade to **2GB RAM minimum** before adding any new background jobs. Recommended: 4GB RAM, 50GB disk for future ML data collection. Providers: AWS t3.small ($17/month), Hetzner CX21 (€5/month), DigitalOcean 2GB ($12/month).

---

## 📱 UI / UX FIXES

- [x] **U1 — Mobile responsive: top content cut off**
- [x] **U2 — Symbol button not loading symbol data + ATM**
- [x] **U3 — Home page AI feature shows nothing**
- [x] **U4 — Guard rails: consolidate into one section + show which stopped automation**
- [x] **U5 — Trade details: richer exit information**
- [x] **U6 — Stuck automations cannot be deleted (recurring bug)**
- [x] **U8 — Paper history / backtest page: mobile layout + more useful data**
- [x] **U9 — Help page: full refresh with latest features + strategy exit conditions**
- [x] **U10 — Auto-refresh market data on app open**
- [x] **U11 — Mobile floating save bar (Cancel/Save) stuck on screen, hiding bottom tabs** — Fixed: `display:none` approach with double-RAF CSS transition. Buttons now work correctly.
- [x] **U12 — AI Assessment shows on mobile but not always on desktop** — Fixed: `_updateClaudeCards()` now called when navigating to dashboard with warm cache (was only called after API fetch).
- [x] **U13 — Per-automation Telegram account selection** — Multi-account selector in automation form. Each automation can target specific Telegram account(s). `_send_telegram_all()` updated with `account_ids` filter. All engine call sites updated.
- [x] **U14 — Hedge width UI showing ±2 instead of ±20** — All `||2` fallbacks in frontend changed to `||20`. STRATS descriptions updated to `±20 (auto, 1000pts)`.
- [x] **U15 — Desktop sidebar missing Calendar and Backtest pages** — Both pages existed but had no sidebar link. Added `sn-events` (Calendar) and `sn-backtest` to sidebar. Mobile nav labels standardised: Home→Dashboard, Live→Live Monitor. More drawer labels match sidebar exactly. Trades moved to main mobile tab bar (replacing Calendar).

- [ ] **U7 — Calendar: simplify UX + integrate with guard rails** — Calendar toggle button does not stop the automation — requires going into Edit to disable dates. Fix: (1) make calendar dates directly tappable to enable/disable skip on that date without opening edit form; (2) show a clear ON/OFF toggle per date in the calendar view; (3) when automation is stopped due to a skip date, show "Stopped: Skip Date [date]" in the guard rails section (U4) and on the automation card — same as other guard rail stops.

- [ ] **U16 — Broker reconnect UX improvements** — Added `↻ Refresh Token` quick reconnect button and `🔑 Test TOTP` persistent inline result box. TOTP test result now shows detailed error with guidance instead of disappearing toast. Consider: (1) show last successful token refresh timestamp; (2) show countdown to next 3 AM expiry; (3) green checkmark when TOTP test passes.

---

## 🟢 LOW PRIORITY / FUTURE

- [x] **L1 — Mobile PWA**
- [x] **L2 — Telegram bot commands**
- [x] **L5 — Position sizing**
- [x] **L6 — Admin dashboard**

- [ ] **L3 — Multiple broker support** — Zerodha/Upstox alongside Fyers. **Multi-week feature — needs full OAuth + API client**
- [ ] **L4 — True backtesting** — Run strategies against historical OHLCV + options data. Needs historical data infrastructure (see ML Phase 3). **Multi-week feature.**

---

## 🤖 ML / ALGO ROADMAP

> ⚠️ **READ BEFORE STARTING ANY ML WORK**
> - Hard gate: server must be upgraded to ≥4GB RAM, ≥50GB disk first (see M14)
> - Hard gate: minimum 50–100 completed shadow trades per strategy before Phase 4+
> - Never modify live engine behaviour based on ML until explicitly approved
> - Each phase must be explicitly approved before starting

---

### ML-P0 — Prerequisites (do before any ML phase)

- [ ] **ML-P0a — Server upgrade** — Upgrade to ≥4GB RAM, 50GB disk. Current 1GB server cannot run ML data collection alongside trading engine (see M14).
- [ ] **ML-P0b — Disk cleanup** — `docker builder prune -f` + `docker image prune -f` (see H7). Frees ~975MB.
- [ ] **ML-P0c — Dedicated ML server (optional but recommended)** — Separate server for data collection + model training. Trading server stays lean. ML server pulls trade data via API or DB replica.

---

### ML-P1 — Trade Feature Capture (safe to do on current server, tiny storage)

> Can start immediately after H7 disk cleanup. Adds <1KB per trade. No RAM impact.

- [ ] **ML-P1a — `trade_features` table** — New DB table capturing full market context at every trade entry and exit. Typed columns (not JSON). Columns: strategy_code, entry_ts, entry_spot, entry_atm, entry_dte, entry_time_bucket, entry_day_of_week, entry_week_of_month, market_regime, entry_vix, entry_iv_rank, entry_iv_skew, entry_vol_premium, entry_implied_move_pct, entry_gex, entry_pcr_oi, entry_pcr_volume, entry_max_pain, entry_max_pain_distance_pct, entry_ema9, entry_ema21, entry_ema9_vs_21_pct, entry_rsi, entry_vwap_distance_pct, entry_atr, prev_sp500_pct, prev_usdinr, prev_crude_pct, gift_nifty_premium_pct, fii_cash_3d_avg, is_expiry_week, is_monthly_expiry, pre_holiday, post_holiday, days_to_rbi, lots, entry_premium, sl_pct, target_pct. **Outcome columns filled at exit:** net_pnl, pnl_pct, won, exit_reason, holding_minutes, max_adverse_excursion, max_favorable_excursion, exit_spot, spot_move_pct, iv_change_at_exit.
- [ ] **ML-P1b — Signal log table** — `signal_log` table: every strategy signal evaluated (fired=True OR skipped=False) with skip_reason + full market context. Gives negative training examples — market conditions where NOT trading was correct. Columns: ts, user_id, strategy_code, fired, skip_reason, spot, vix, iv_rank, pcr_oi, market_regime, rsi, ema9_vs_21_pct, is_expiry_week, days_to_rbi.
- [ ] **ML-P1c — Auto-populate at trade entry/exit** — Wire into engine: at every `_open_position()` call, compute + insert `trade_features` row. At every `_close_position()` call, update the outcome columns. At every strategy signal check, insert `signal_log` row.
- [ ] **ML-P1d — ML export endpoint** — `GET /api/ml/export?format=csv` — downloads full `trade_features` table as CSV or Parquet. For training on laptop / Google Colab. No special tooling needed — standard SQL export.

---

### ML-P2 — Continuous Market Snapshot Collection (needs upgraded server)

> Requires ML-P0a (server upgrade). ~220MB/year storage, ~115MB extra RAM.

- [ ] **ML-P2a — `candles_1min` table** — 1-minute OHLCV for Nifty, BankNifty, FinNifty. Columns: ts, symbol, open, high, low, close, volume, ema9, ema21, ema50, ema200, rsi_14, vwap, atr_14, bb_upper, bb_lower. Background job stores every 1-min candle during market hours. ~375 rows/day/symbol = ~280K rows/year total.
- [ ] **ML-P2b — `options_snapshots` table** — 5-minute options chain snapshot for ATM ±10 strikes. Columns: ts, symbol, expiry, strike, option_type (CE/PE), ltp, bid, ask, volume, oi, oi_change, iv, delta, gamma, theta, vega. ~40 rows/snapshot × 75 snapshots/day = 3,000 rows/day = ~750K rows/year.
- [ ] **ML-P2c — `market_snapshots` table** — 5-minute market-level derived metrics. Columns: ts, symbol, spot, atm, india_vix, pcr_oi, pcr_volume, max_pain, atm_iv, iv_rank, iv_skew, iv_term_spread, implied_move_pct, gex (gamma exposure), top_ce_oi_strike, top_pe_oi_strike, oi_concentration, vol_premium. Derived from options_snapshots.
- [ ] **ML-P2d — `daily_market` table** — End-of-day + pre-market aggregate. Columns: date, symbol, open/high/low/close/volume, realized_vol_30d, fii_cash_net, fii_futures_net, fii_options_net, dii_cash_net, gift_nifty_8_45, gift_nifty_premium, sp500_pct, nasdaq_pct, nikkei_pct, hangseng_pct, usdinr, crude_oil_pct, dxy_pct, us_10y_yield, is_holiday, is_expiry_week, is_monthly_expiry, days_to_expiry, days_to_rbi. Sources: Fyers (Indian data), Yahoo Finance free API (global), NSE website (FII/DII).
- [ ] **ML-P2e — Background collection jobs** — Four scheduled background coroutines: (1) 1-min candle writer (market hours); (2) 5-min options snapshot writer (market hours); (3) pre-market job at 8:45 AM — fetches Gift Nifty, global markets; (4) post-market job at 4:00 PM — fetches FII/DII from NSE, computes daily aggregates.

---

### ML-P3 — Feature Engineering (needs ML-P1 + ML-P2 data)

> Offline processing. Runs on laptop or Google Colab, not on server.

- [ ] **ML-P3a — Feature pipeline script** — Python script: reads `trade_features` + joins `daily_market` + joins nearest `market_snapshots` → produces flat feature matrix ready for ML. Export as Parquet.
- [ ] **ML-P3b — Derived features** — Compute: IV rank (percentile of current IV vs 252-day range), vol premium (IV − realized vol), GEX (gamma exposure = sum of gamma × OI × 100 per strike), implied daily move (ATM straddle / spot), OI build-up speed (OI change rate per 15 min), IV term structure spread (near expiry IV − far expiry IV).
- [ ] **ML-P3c — Regime classification** — Label each snapshot with market_regime: 'trending_up' (ema9 > ema21 > ema50, RSI > 55), 'trending_down' (reverse), 'ranging' (ema9 ≈ ema21, ATR low), 'volatile' (ATR > 1.5× 20-day avg OR VIX > 20). Used as a categorical feature in ML.

---

### ML-P4 — Phase 1 Model: Trade Filter (needs 50+ trades per strategy)

> Hard gate: minimum 50 completed shadow trades per strategy. No exceptions.
> Train on laptop/Colab. Deploy model file to server.

- [ ] **ML-P4a — Baseline model** — LightGBM binary classifier. Input: ~30 features from `trade_features`. Target: `won` (1/0). Walk-forward validation: train months 1–6, test month 7, etc. Never use future data. Metric: AUC-ROC + precision at P>0.60 threshold.
- [ ] **ML-P4b — SHAP interpretability** — SHAP values for every prediction. Must understand WHY the model skips a trade before deploying. Example: "Skipped because iv_rank=85 + pcr=0.6 = historically loses". Human review before deployment.
- [ ] **ML-P4c — Shadow mode deployment** — Model loaded by engine. At every trade signal, compute features + get prediction. Log to `signal_log.ml_score`. Do NOT act on it yet — observe for 2 weeks. Compare ML-would-skip vs ML-would-trade outcomes.
- [ ] **ML-P4d — Live gating (only after explicit approval)** — After shadow validation: if ML score < 0.45, skip trade. If ML score > 0.75, allow. Between 0.45–0.75, trade at 0.5x lots. **Requires explicit user sign-off on specific threshold values.**

---

### ML-P5 — Phase 2 Model: Position Sizing (needs 12+ months of data)

- [ ] **ML-P5a — Confidence-based lot sizing** — Same features as Phase 1 → predict expected P&L range (regression). High confidence (score > 0.70) → 1.5x lots. Low confidence (0.40–0.55) → 0.5x lots. Very low (< 0.40) → skip.
- [ ] **ML-P5b — Per-strategy models** — Train separate model per strategy (S1–S9). Each strategy has different optimal features — e.g. S1 (ORB) most sensitive to gap size, S9 (expiry theta) most sensitive to DTE and IV rank.

---

### ML-P6 — Phase 3 Model: Dynamic Exit (needs 18+ months, RL)

- [ ] **ML-P6a — Exit timing model** — At each 1-min candle during live trade: current features → should I exit now or hold? PPO/SAC reinforcement learning. Replaces fixed SL/target with learned exit policy. **Most complex phase — do not start before P4 + P5 are validated.**
- [ ] **ML-P6b — MAE/MFE-based SL calibration** — Use historical MAE (max adverse excursion) per strategy + regime to set dynamic SL levels. If historically S1 trades in trending_up markets have MAE < 8% before recovering, tighten SL to 8%.

---

### ML-P7 — Infrastructure and Automation

- [ ] **ML-P7a — Automated retraining pipeline** — Monthly: export latest `trade_features`, retrain Phase 1 model, validate walk-forward, if AUC improves → deploy new model file. Alert via Telegram if model degrades.
- [ ] **ML-P7b — Model versioning** — Each deployed model tagged with version, training date, training data range, AUC score. Previous version kept as fallback.
- [ ] **ML-P7c — Data quality monitoring** — Daily check: are snapshots being collected? Feature drift detection — alert if market conditions shift outside training distribution (model becomes unreliable).
- [ ] **ML-P7d — Full dataset export** — `GET /api/ml/export/full` — exports all tables (trade_features, signal_log, daily_market, candles_1min) as a ZIP of Parquet files. Works in any environment: local, Colab, SageMaker, Azure ML.

---

## ✅ COMPLETED (v6 original)

- [x] Gemini AI replaces Anthropic/Claude entirely (google-genai SDK)
- [x] Morning assessment at 9:15, post-trade AI insight stored per trade
- [x] AI ask panel (slide-in chat), News & Risk Gate, per-user API key
- [x] All /api/ai/* endpoints (72 total, no duplicates)
- [x] S1–S9 strategies implemented and validated
- [x] 3-layer SL on combined premium only
- [x] Engine state fields renamed ai_* (not claude_*)
- [x] Event Calendar in bottom nav
- [x] Day picker (M/T/W/Th/F) visible in automation form
- [x] Skip dates visible in automation form
- [x] Automation naming field
- [x] Help page: Calendar, Day picker, AI News Gate sections
- [x] Gemini model dropdown (correct model names)
- [x] 116/116 tests passing across 24 sections
- [x] Auto-migrations on startup (additive only)
- [x] Token refresh 24/7 (every 5 min)
- [x] Claude Code connected to server + GitHub

---

## VERSION HISTORY

| Version | Date | Summary |
|---------|------|---------|
| v6.4 | 2026-04-04 | TOTP daily re-auth fixed (app_id extraction from client_id suffix, appType string), stale refresh_token() calls removed, mobile floating save bar fixed, AI assessment desktop race condition fixed, per-automation Telegram selection, hedge width UI ±20, desktop sidebar + mobile nav consistency (Calendar + Backtest added to sidebar, Trades to mobile tab bar, labels standardised) |
| v6.3 | 2026-03-26 | VWAP/EMA75 SL buffer configurable per automation, AI config consolidated to Guard Rails, trade view shows all entry+exit params, disk cleanup, ML backlog L7 added with hard gate |
| v6.2 | 2026-03-25 | Ratchet profit lock SL (M11), VWAP 5% buffer (M10), 1000pt hedges + BUY-first order (M9), live ATM for S3-S9 (M6-M8), guard rail status display (U4), S10 in help (U9), AI dashboard fix (U3), trade exit details (U5), backtest responsive (U8), symbol tab fix (U2) |
| v6.1 | 2026-03-21 | Automation edit, multi-symbol market data, PWA, Telegram commands, Kelly position sizing, admin revenue KPIs, S2 validation logging, precheck ternary fix |
| v6 | 2026-03-21 | Gemini AI, all 9 strategy fixes, Calendar in nav, day picker, help page updated, Claude Code connected |
| v5 | — | Previous Claude/Anthropic AI version |

---

## SETUP NOTES

### Telegram Bot Commands (L2)
1. Profile → Telegram → add bot token + chat ID
2. `POST /api/telegram/set-webhook` with `{"webhook_url": "https://35.91.127.14/api/telegram/webhook"}`
3. Commands: `/start` `/stop` `/status` `/engine <name>` `/help`

### PWA Install (L1)
- Android Chrome: Menu → Add to Home Screen
- iPhone Safari: Share → Add to Home Screen

### Position Sizing (L5)
- **Fixed** (default, safe): always uses configured lot count
- **Kelly**: auto-sizes using historical win rate. Enable only after 20+ trades.
- Requires `kelly_win_rate`, `kelly_avg_win`, `kelly_avg_loss` in automation config (future: auto-populate from performance page)

### ML Model Training (future)
- Training runs on laptop or Google Colab — NOT on the trading server
- Download training data: `GET /api/ml/export?format=csv`
- Deploy trained model: upload `.pkl` file to server, engine loads it at startup
- Framework: LightGBM (pip install lightgbm) + SHAP (pip install shap)
