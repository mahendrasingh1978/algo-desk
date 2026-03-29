# ALGO-DESK — Project Backlog

> Managed by Claude Code. Updated as tasks are added, started, or completed.
> Version history tracked via git commits.

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

---

## 🟡 MEDIUM PRIORITY

- [x] **M11 — Ratchet (Profit Lock) SL + dead zone fix** — Replace Layer 1 trailing SL with a stepped profit lock (ratchet) that guarantees profit once key decay milestones are reached. Steps: 15% decay → SL at entry (breakeven); 25% decay → SL at entry×0.90 (10% locked); 35% decay → SL at entry×0.75 (25% locked); 50% → profit target exit. Interaction with Layer 2 VWAP SL: profit lock suppresses VWAP SL when premium is below profit lock level (prevents VWAP exiting at a loss when profit is locked). Dead zone risk: if VWAP is suppressed but profit lock not yet hit, premium can grind up with nothing firing. Fix: rebound cap — if premium rebounds >15% from its lowest point AND VWAP SL is triggered, allow VWAP SL to fire regardless of profit lock. Gap risk: profit lock level is a target not a guaranteed fill — market can gap through it; Layer 3 Max Loss is the ultimate hard backstop and always fires unconditionally. Layer 3 is configurable per automation (20/25/30/40%) — wider max loss setting widens the dead zone, making the rebound cap critical. Impacts S1–S9. S10 BUY strategy has its own SL logic and is unaffected.
- [x] **M12 — Live market data feeds: VIX, prev_close, gap guards now wired** — Four engine guards/strategies were silently non-functional because their required market data was never fetched. Fixed: (1) India VIX fetched from Fyers `/data/quotes` on every market-hours tick until fetched for the day → feeds Guard 2 (VIX filter); (2) Previous day close fetched from Fyers daily history → feeds Guard 5 (gap skip), S8 (gap fade entry), S10 (gap directional buy), AI assessment gap% context; (3) `prev_day_move_pct` computed from yesterday's open→close → feeds Guard 5 and S8/S9 suppression; (4) VIX always shown in dashboard NIFTY KPI card with colour coding (green <15, amber 15–20, red ≥20) — shows "—" when not yet fetched. **Where to view live VIX: Dashboard → NIFTY KPI card → sub-line shows "ATM 23150 • VIX 14.2".** No time gate — fetches on any tick after server restart. Kelly sizing parameters (kelly_win_rate etc.) remain unfed — documented in L5 notes.

- [x] **M10 — VWAP SL buffer 2% → 5%** — Layer 2a VWAP SL currently exits when combined premium > VWAP × 1.02 (only ₹12 above VWAP on a ₹600 premium). Change default `vwap_buffer_pct` from 2 to 5, giving ₹30 tolerance (VWAP × 1.05). Impacts all selling strategies S1–S9 — shared SL layer. S10 BUY strategy unaffected (has its own SL). One-line change in `engine.py` + same change in `main.py` (two places where default is set). Trade-off: wider buffer gives more recovery room but larger loss if market keeps moving — validate in paper mode before going live.
- [x] **M9 — Hedge leg order sequence + 1000pt hedge width** — Two related fixes: (1) Order sequence: currently SELL legs placed before BUY hedge legs in `_open_position`. Must reverse — BUY hedges first, then SELL, to avoid momentary naked short, margin rejection, and SEBI compliance risk. (2) Hedge width: change from current tight hedges (±100–200pt) to ±1000pt OTM for all strategies except S9. Requires `strike_sides` config to increase from 3 → 20 so the engine tracks 41 strikes (±1000pt window) instead of 7. S9 (expiry day) to keep ±150pt hedge — 1000pt OTM options have zero bids on expiry day and won't fill.
- [x] **M6 — S4 live ATM re-centering** — S4 Iron Condor uses morning ATM (locked at 9:15) when building the condor. On gap days, spot can be 100+ pts away by 9:30, making the CE short immediately vulnerable (today: ATM=23100 but spot=23215 → CE sold at 23150, only 65pts OTM → VWAP SL hit in 14 mins). Fix: use `_current_atm(state)` (live spot) when spot has drifted >50pts from morning ATM. Affects S4 only — S1 intentionally uses morning ATM by design.
- [x] **M7 — Live ATM for S3, S5, S6, S8 post-9:30** — S3, S5, S6, S8 all use `state.atm` (morning ATM locked at 9:15) but should trade at wherever spot actually is at time of entry. Fix: replace `state.atm` with nearest tracked strike to current spot for these 4 strategies. S1 and S7 intentionally stay on morning ATM. Constraint: `state.strikes` tracks ±3 from 9:15 ATM (±150pt window) — live ATM must be within this range or fall back to nearest tracked strike.
- [x] **M8 — S9 re-initialise strikes at 10:30** — S9 fires at 11:00–12:00 (expiry Thursday only). By 11am, spot can be 200+ pts from 9:15 ATM — the morning strikes are deep OTM/ITM and have almost no theta to decay. Fix: at 10:30 AM, check if current spot has drifted >2 strikes from morning ATM; if so, rebuild `state.strikes` centred on current spot so S9 trades real ATM options. Also fixes S2 fallback issue (S2 already tries live ATM but falls back to morning ATM if live ATM not in tracked strikes).
- [x] **M1 — S2 real-world validation** — Added detailed emit() logging for each condition (candles, EMA, VWAP, spike, direction)
- [x] **M2 — Multi-symbol market data** — `user_symbol_cache` + fetches all symbols from user's automations. `/api/market/all-symbols` endpoint added.
- [x] **M3 — Automation edit** — PUT `/api/automations/{id}` endpoint + Edit button in UI + form pre-fill
- [x] **M4 — Performance page** — best_day/worst_day code confirmed correct — needs live data to validate
- [x] **M5 — Git → deploy cycle test** — Claude Code connected, verify full push → deploy workflow. ✅ Confirmed working — multiple deploys completed this session.

---

## 📱 UI / UX FIXES

- [x] **U1 — Mobile responsive: top content cut off** — Safe-area-inset-top already implemented in topbar CSS. viewport-fit=cover in meta tag. Verified correct. — Top of page is clipped on mobile browsers (Safari/Chrome). Likely missing `safe-area-inset-top` handling for notched phones or incorrect viewport meta/padding. Fix: add `padding-top: env(safe-area-inset-top)` to top nav, ensure `<meta name="viewport">` includes `viewport-fit=cover`. Test on iPhone Safari and Android Chrome.

- [x] **U2 — Symbol button not loading symbol data + ATM** — Clicking a symbol button in the Trader page does not populate the symbol's market data or ATM strike. Each symbol button should fetch and display that symbol's live spot price, ATM strike, and chain data. Fix: wire symbol button click to call the market data endpoint for that specific symbol and update the display.

- [x] **U3 — Home page AI feature shows nothing** — loadClaudeAssessment() now called on login and on dashboard navigation. — AI section on home page is empty. Add a short AI market summary (2–3 lines max) at the bottom of the home page — today's market outlook, active guard rails, and whether strategies are cleared to trade. Pulled from the morning assessment already stored in DB. Keep it concise, no clutter.

- [x] **U4 — Guard rails: consolidate into one section + show which stopped automation** — guard_status field in EngineState, shown on Automate cards and Live Monitor banner. — Currently AI guard rails and other guards (VIX, drift, gap, prev-day, skip-day) are shown in separate places. Consolidate all guard rails into one "Guard Rails" section per automation. When an automation stops due to a guard rail, clearly show which one triggered (e.g. "Stopped: VWAP SL — combined 617 > VWAP 603"). Both in the automation card and in the engine log panel.

- [x] **U5 — Trade details: richer exit information** — Exit panel shows: exit type with color, time held, premium decay %, SL layer, VWAP/EMA75/trail_low values at exit, AI insight. — Trade exit currently shows minimal info. Improve to show: exit reason label (VWAP SL / Profit Lock / Max Loss / Profit Target / Manual), exit premium vs entry premium, P&L in ₹ and %, time held, which SL layer fired and at what level. Keep layout clean and professional — use a structured card not a raw text dump. Entry details (why entered, signal conditions) already good — match that quality for exits.

- [x] **U6 — Stuck automations cannot be deleted (recurring bug)** — Automations that are not running sometimes get stuck and cannot be deleted from the UI. Root cause likely: delete endpoint checks `is_running` flag in DB which is stale (engine crashed or server restarted without clearing flag). Fix: (1) on server start, reset all `is_running=true` automations to `is_running=false`; (2) delete endpoint should allow deletion regardless of `is_running` state with a warning; (3) add a "Force Delete" option in UI for stuck automations.

- [ ] **U7 — Calendar: simplify UX + integrate with guard rails** — Calendar toggle button does not stop the automation — requires going into Edit to disable dates. Fix: (1) make calendar dates directly tappable to enable/disable skip on that date without opening edit form; (2) show a clear ON/OFF toggle per date in the calendar view; (3) when automation is stopped due to a skip date, show "Stopped: Skip Date [date]" in the guard rails section (U4) and on the automation card — same as other guard rail stops.

- [x] **U8 — Paper history / backtest page: mobile layout + more useful data** — Responsive kpi-grid.four (2-col on mobile), strategy table adds best/worst trade columns, wins/losses breakdown. — Boxes are cut off on mobile. Page currently shows only graphs. Make it fully responsive (horizontal scroll or stacked cards on mobile). Add useful data alongside graphs: per-strategy win rate, average profit/loss, best trade, worst trade, total P&L by strategy, trade count. Make it actionable — a trader should be able to see at a glance which strategies are working in paper mode.

- [x] **U9 — Help page: full refresh with latest features + strategy exit conditions** — S10 added, 4-layer SL with ratchet/VWAP 5%/max loss explained, 1000pt hedge, guard rails section added. — Help page is outdated — missing S10, skip day filters, guard rail changes, ratchet SL, 1000pt hedge. Rule going forward: every new feature added to the app must also be documented in the Help page in the same release. For each strategy section, add clear exit conditions: what triggers exit (VWAP SL, profit lock, profit target, hard time exit), in plain simple language a non-technical trader can understand. No jargon.

- [x] **U10 — Auto-refresh market data on app open (no manual Test click needed)** — Dashboard auto-fetches on load, ticker runs every 60s, updateTicker() called on dashboard navigation. — Currently the Trader page requires manually clicking "Test" to fetch market data before anything populates. Fix: on page load (and on returning to Trader tab), automatically fetch market data and refresh every 60 seconds during market hours (9:15–15:30). Outside market hours show last known data with a timestamp. Remove or repurpose the "Test" button — if kept, rename it to "Refresh Now". Also review the Strike Price display shown after clicking Test — clarify its purpose or remove if not useful for the trader.

---

## 🟢 LOW PRIORITY / FUTURE

- [x] **L1 — Mobile PWA** — manifest.json + sw.js service worker + Apple meta tags. Install: Share → Add to Home Screen
- [x] **L2 — Telegram bot commands** — `/api/telegram/webhook` + `/api/telegram/set-webhook`. Commands: /start /stop /status /engine /help
- [ ] **L3 — Multiple broker support** — Zerodha/Upstox alongside Fyers. **Multi-week feature — needs full OAuth + API client**

---

> ⚠️ **CONFIRM BEFORE STARTING — DO NOT DEVELOP UNTIL EXPLICITLY APPROVED**

- [ ] **L7 — Machine Learning: adaptive parameter calibration**

  **Hard gate: minimum 50–100 completed shadow trades per strategy must be logged before any ML development begins. No exceptions.**

  Once the trade history is sufficient, ML can enhance the engine in 5 areas:

  1. **Dynamic entry threshold calibration** — use historical signal accuracy (e.g. ORB range hit rate for S1, squeeze detection accuracy for S2) to auto-tune signal thresholds per strategy instead of fixed constants.
  2. **Adaptive VWAP / EMA75 SL buffer** — replace fixed `vwap_buffer_pct` (5%) and `ema_buffer_pct` (1%) with buffers that self-adjust based on recent volatility regime (ATR or rolling premium std-dev). Tighter buffer in low-vol, wider in high-vol.
  3. **Exit timing prediction** — train a classifier on historical trade data (entry signal, strategy, time-of-day, VIX, premium decay curve) to predict optimal exit window and suppress premature SL triggers.
  4. **Per-day strategy selection** — use daily features (gap size, prev-day move, VIX, day-of-week) to score each strategy's probability of success that day. Suppress low-probability strategies automatically instead of running all enabled strategies every day.
  5. **Premium behaviour anomaly detection** — flag abnormal intraday premium spikes (fat-finger fills, circuit breakers, data errors) to avoid SL triggers caused by bad data rather than real market moves.

  **Implementation approach when approved:**
  - Shadow-only mode first (ML predictions logged but not acted on) for at least 2 weeks
  - A/B comparison: ML-gated vs non-gated trades on same days
  - Only graduate to live gating after statistically significant improvement confirmed
  - No changes to core engine logic until user explicitly approves each ML gate individually

  **Dependencies:** 50–100 trades × 9 strategies = ~450–900 paper trades minimum. At current paper-mode pace, estimate 3–6 months of data collection first.

  > ⚠️ **TO START: user must explicitly say "start L7 ML development" — do not begin this work based on any other instruction.**

---
- [ ] **L4 — Backtesting page** — Run strategies against historical data. **Multi-week feature — needs historical data infra**
- [x] **L5 — Position sizing** — Kelly criterion (`kelly_lots`, `get_position_size`) in engine.py + dropdown in automation form
- [x] **L6 — Admin dashboard** — Revenue KPIs (MRR, ARR), usage stats (engines running, trades today), plan breakdown

---

## ✅ COMPLETED (v6 original)

- [x] Gemini AI replaces Anthropic/Claude entirely (google-genai SDK)
- [x] Morning assessment at 9:15, post-trade AI insight stored per trade
- [x] AI ask panel (slide-in chat), News & Risk Gate, per-user API key
- [x] All /api/ai/* endpoints (72 total, no duplicates)
- [x] S1: fires 9:22, morning ATM locked at 9:15, 50pt drift gate, ORB min 0.3%
- [x] S2: fires 9:35, min 20 candles, real squeeze pattern
- [x] S3: fires 9:35, min 20 candles
- [x] S6: IV gate correct (combined > ORB×1.05)
- [x] S7: fires 9:30, all strikes must break ORB
- [x] S8: prev-day filter (skip if yesterday >2% move)
- [x] S9: widens hedge to ±3 on big-move days
- [x] S5: disabled by default
- [x] 3-layer SL on combined premium only
- [x] Engine state fields renamed ai_* (not claude_*)
- [x] Event Calendar in bottom nav (replaced Results)
- [x] Results moved to More drawer
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

---

## VERSION HISTORY

| Version | Date | Summary |
|---------|------|---------|
| v6.3 | 2026-03-26 | VWAP/EMA75 SL buffer configurable per automation, AI config consolidated to Guard Rails (removed Profile duplicate), trade view shows all entry+exit params, disk cleanup, ML backlog L7 added with hard gate |
| v6.2 | 2026-03-25 | Ratchet profit lock SL (M11), VWAP 5% buffer (M10), 1000pt hedges + BUY-first order (M9), live ATM for S3-S9 (M6-M8), guard rail status display (U4), S10 in help (U9), AI dashboard fix (U3), trade exit details (U5), backtest responsive (U8), symbol tab fix (U2) |
| v6.1 | 2026-03-21 | Automation edit, multi-symbol market data, PWA, Telegram commands, Kelly position sizing, admin revenue KPIs, S2 validation logging, precheck ternary fix |
| v6 | 2026-03-21 | Gemini AI, all 9 strategy fixes, Calendar in nav, day picker, help page updated, Claude Code connected |
| v5 | — | Previous Claude/Anthropic AI version |
