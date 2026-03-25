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

---

## 🟡 MEDIUM PRIORITY

- [ ] **M10 — VWAP SL buffer 2% → 5%** — Layer 2a VWAP SL currently exits when combined premium > VWAP × 1.02 (only ₹12 above VWAP on a ₹600 premium). Change default `vwap_buffer_pct` from 2 to 5, giving ₹30 tolerance (VWAP × 1.05). Impacts all selling strategies S1–S9 — shared SL layer. S10 BUY strategy unaffected (has its own SL). One-line change in `engine.py` + same change in `main.py` (two places where default is set). Trade-off: wider buffer gives more recovery room but larger loss if market keeps moving — validate in paper mode before going live.
- [ ] **M9 — Hedge leg order sequence + 1000pt hedge width** — Two related fixes: (1) Order sequence: currently SELL legs placed before BUY hedge legs in `_open_position`. Must reverse — BUY hedges first, then SELL, to avoid momentary naked short, margin rejection, and SEBI compliance risk. (2) Hedge width: change from current tight hedges (±100–200pt) to ±1000pt OTM for all strategies except S9. Requires `strike_sides` config to increase from 3 → 20 so the engine tracks 41 strikes (±1000pt window) instead of 7. S9 (expiry day) to keep ±150pt hedge — 1000pt OTM options have zero bids on expiry day and won't fill.
- [ ] **M6 — S4 live ATM re-centering** — S4 Iron Condor uses morning ATM (locked at 9:15) when building the condor. On gap days, spot can be 100+ pts away by 9:30, making the CE short immediately vulnerable (today: ATM=23100 but spot=23215 → CE sold at 23150, only 65pts OTM → VWAP SL hit in 14 mins). Fix: use `_current_atm(state)` (live spot) when spot has drifted >50pts from morning ATM. Affects S4 only — S1 intentionally uses morning ATM by design.
- [ ] **M7 — Live ATM for S3, S5, S6, S8 post-9:30** — S3, S5, S6, S8 all use `state.atm` (morning ATM locked at 9:15) but should trade at wherever spot actually is at time of entry. Fix: replace `state.atm` with nearest tracked strike to current spot for these 4 strategies. S1 and S7 intentionally stay on morning ATM. Constraint: `state.strikes` tracks ±3 from 9:15 ATM (±150pt window) — live ATM must be within this range or fall back to nearest tracked strike.
- [ ] **M8 — S9 re-initialise strikes at 10:30** — S9 fires at 11:00–12:00 (expiry Thursday only). By 11am, spot can be 200+ pts from 9:15 ATM — the morning strikes are deep OTM/ITM and have almost no theta to decay. Fix: at 10:30 AM, check if current spot has drifted >2 strikes from morning ATM; if so, rebuild `state.strikes` centred on current spot so S9 trades real ATM options. Also fixes S2 fallback issue (S2 already tries live ATM but falls back to morning ATM if live ATM not in tracked strikes).
- [x] **M1 — S2 real-world validation** — Added detailed emit() logging for each condition (candles, EMA, VWAP, spike, direction)
- [x] **M2 — Multi-symbol market data** — `user_symbol_cache` + fetches all symbols from user's automations. `/api/market/all-symbols` endpoint added.
- [x] **M3 — Automation edit** — PUT `/api/automations/{id}` endpoint + Edit button in UI + form pre-fill
- [x] **M4 — Performance page** — best_day/worst_day code confirmed correct — needs live data to validate
- [ ] **M5 — Git → deploy cycle test** — Claude Code connected, verify full push → deploy workflow

---

## 🟢 LOW PRIORITY / FUTURE

- [x] **L1 — Mobile PWA** — manifest.json + sw.js service worker + Apple meta tags. Install: Share → Add to Home Screen
- [x] **L2 — Telegram bot commands** — `/api/telegram/webhook` + `/api/telegram/set-webhook`. Commands: /start /stop /status /engine /help
- [ ] **L3 — Multiple broker support** — Zerodha/Upstox alongside Fyers. **Multi-week feature — needs full OAuth + API client**
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
| v6.1 | 2026-03-21 | Automation edit, multi-symbol market data, PWA, Telegram commands, Kelly position sizing, admin revenue KPIs, S2 validation logging, precheck ternary fix |
| v6 | 2026-03-21 | Gemini AI, all 9 strategy fixes, Calendar in nav, day picker, help page updated, Claude Code connected |
| v5 | — | Previous Claude/Anthropic AI version |
