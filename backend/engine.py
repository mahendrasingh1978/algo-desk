"""
ALGO-DESK Trading Engine v3 — Professional Edition
====================================================
Based on industry research from Zerodha Varsity, AlgoTest,
StraddlePro, Share.Market, PL Capital India.

KEY PRINCIPLES (research-backed):
1. Iron Fly / Iron Condor preferred over naked straddle/strangle
   - Defined risk, lower margin (~50-60% less)
   - SEBI 2024: naked short options require ₹1.25L+ per lot
   - With hedge wings: ~₹50K per lot
2. NIFTY lot size: 75 (post Nov 2024 SEBI mandate)
   - Always configurable in frontend — never hardcoded
3. SL based entirely on combined premium — never spot price
4. Take profit at 50% of max (industry standard)
5. Never hold through major events (budget, RBI policy)
6. Deploy in chunks for better avg entry (pro approach)
"""

import logging
from datetime import datetime, time as dtime
from typing import Optional, List
from dataclasses import dataclass, field

log = logging.getLogger("engine")


# ── Strike state ──────────────────────────────────────────────────

@dataclass
class StrikeState:
    strike:           int
    offset:           int
    is_atm:           bool  = False
    ce_symbol:        str   = ""
    pe_symbol:        str   = ""
    combined_history: List[float] = field(default_factory=list)
    orb_high:         float = 0.0
    orb_low:          float = 0.0
    fired:            bool  = False
    # VWAP of combined premium
    _vwap_pv:  float = 0.0
    _vwap_v:   float = 0.0
    vwap_val:  float = 0.0
    # EMA75 of combined premium
    ema75:     float = 0.0
    _ema_count: int  = 0

    # Individual option LTPs (for S10 directional buy SL tracking)
    ce_ltp:    float = 0.0
    pe_ltp:    float = 0.0

    @property
    def current(self):
        return self.combined_history[-1] if self.combined_history else 0.0

    def update(self, combined: float, volume: float = 1.0,
               ce_ltp: float = 0.0, pe_ltp: float = 0.0):
        self.ce_ltp = ce_ltp if ce_ltp else combined / 2
        self.pe_ltp = pe_ltp if pe_ltp else combined / 2
        self.combined_history.append(combined)
        self._vwap_pv += combined * volume
        self._vwap_v  += volume
        self.vwap_val  = self._vwap_pv / self._vwap_v if self._vwap_v else combined
        k = 2 / 76
        self._ema_count += 1
        self.ema75 = combined * k + self.ema75 * (1-k) if self.ema75 else combined


# ── SL State — three layer, combined premium only ─────────────────

class SLState:
    """
    Industry standard three-layer SL for short straddle/strangle.
    ALL checks on combined premium (CE + PE) only.
    No fixed amounts. Everything percentage-relative to entry.

    Layer 1: Trailing SL — locks in gains
    Layer 2: VWAP SL — dynamic market condition exit
    Layer 3: Max loss backstop — defined percentage of entry
    Profit target: exit at 50% premium decay (industry standard)
    """
    def __init__(self):
        self.reset()

    def reset(self):
        self.entry_combined = 0.0
        self.trailing_low   = 0.0
        self.trailing_sl    = 0.0
        self.candles        = 0
        self.sl_type        = "NONE"

    def activate(self, entry_combined: float, config: dict):
        self.entry_combined = entry_combined
        self.trailing_low   = entry_combined
        max_loss_pct        = config.get("max_loss_pct", 30) / 100
        self.trailing_sl    = entry_combined * (1 + max_loss_pct)
        self.candles        = 0
        self.sl_type        = "VWAP"

    def update(self, current: float, vwap: float, ema75: float,
               ema_count: int, config: dict):
        self.candles += 1
        max_loss_pct     = config.get("max_loss_pct",     30) / 100
        trail_pct        = config.get("trail_pct",        20) / 100
        min_profit_pct   = config.get("min_profit_pct",   15) / 100
        vwap_buf         = config.get("vwap_buffer_pct",   2) / 100
        ema_buf          = config.get("ema_buffer_pct",    1) / 100
        target_pct       = config.get("profit_target_pct",50) / 100  # industry: 50% decay

        # Update trailing low
        if current < self.trailing_low:
            self.trailing_low = current
            self.trailing_sl  = self.trailing_low * (1 + trail_pct)

        # ── Layer 1: Trailing SL ──────────────────────────────────
        if (self.trailing_low <= self.entry_combined * (1 - min_profit_pct)
                and current >= self.trailing_sl):
            return True, (f"TRAILING_SL entry={self.entry_combined:.1f} "
                         f"low={self.trailing_low:.1f} sl={self.trailing_sl:.1f} now={current:.1f}")

        # ── Layer 2a: VWAP SL ─────────────────────────────────────
        if self.candles >= 3 and vwap > 0:
            if current > vwap * (1 + vwap_buf):
                self.sl_type = "VWAP"
                return True, f"VWAP_SL combined={current:.1f} vwap={vwap:.1f}"

        # ── Layer 2b: EMA75 SL (tighter when active & < VWAP) ────
        if ema_count >= 75 and ema75 > 0 and vwap > 0 and ema75 < vwap:
            if current > ema75 * (1 + ema_buf):
                self.sl_type = "EMA75"
                return True, f"EMA75_SL combined={current:.1f} ema75={ema75:.1f}"

        # ── Layer 3: Max loss backstop ────────────────────────────
        if current > self.entry_combined * (1 + max_loss_pct):
            self.sl_type = "MAX_LOSS"
            return True, (f"MAX_LOSS combined={current:.1f} "
                         f"entry={self.entry_combined:.1f} limit={max_loss_pct*100:.0f}%")

        # ── Profit target ─────────────────────────────────────────
        # Industry standard: exit at 50% premium decay (captured 50%)
        if current <= self.entry_combined * (1 - target_pct):
            return True, (f"PROFIT_TARGET entry={self.entry_combined:.1f} "
                         f"now={current:.1f} captured={target_pct*100:.0f}%")

        return False, ""


# ── Engine state ──────────────────────────────────────────────────

class EngineState:
    def __init__(self, config: dict):
        self.config        = config
        self.is_running    = False
        self.spot_history: List[float] = []
        self.atm_strike:   Optional[int] = None
        self.spot_locked:  Optional[float] = None
        self.event_checked:   bool = False    # event calendar checked today
        self.events_suspended: bool = False   # trading suspended by event
        self.ai_checked:    bool = False   # AI assessment checked today
        self.ai_suspended:  bool = False   # trading suspended by AI
        self.ai_avoid:      list = []      # strategies to skip per AI assessment
        self.strikes:      List[StrikeState] = []
        self.orb_complete: bool = False
        self.position:     Optional[dict] = None
        self.day_pnl:      float = 0.0
        self.sl_state      = SLState()
        self.log:          List[dict] = []
        # One trade per automation per day gate
        self.traded_today: bool = False
        self.trade_count:  int  = 0

    @property
    def atm(self) -> Optional[StrikeState]:
        return next((s for s in self.strikes if s.is_atm), None)

    def emit(self, msg: str, kind: str = "INFO"):
        entry = {"ts": datetime.now().strftime("%H:%M:%S"), "msg": msg, "kind": kind}
        self.log.append(entry)
        if len(self.log) > 200:
            self.log = self.log[-200:]
        log.info(f"[engine][{kind}] {msg}")


# ── Strategy leg builder ──────────────────────────────────────────

def nearest_strike(spot: float, gap: int = 50) -> int:
    return round(spot / gap) * gap

# Maximum spot drift (in strike widths) before suspending signals.
# 3 strikes = 150 pts on NIFTY (50pt gap × 3).
# Beyond this, the morning ATM is too far from current price to be relevant.
DRIFT_MAX_STRIKES = 3

def _current_atm(state: "EngineState") -> int:
    """ATM based on the most recent spot price, not the morning lock."""
    if state.spot_history:
        return nearest_strike(state.spot_history[-1])
    return state.atm_strike or 0

def _drift_strikes(state: "EngineState") -> int:
    """How many strikes has spot drifted from morning ATM?"""
    if not state.atm_strike or not state.spot_history:
        return 0
    gap = state.config.get("strike_round", 50)
    return abs(state.spot_history[-1] - state.atm_strike) / gap

def _drift_ok(state: "EngineState") -> bool:
    """True if spot is still within DRIFT_MAX_STRIKES of morning ATM."""
    return _drift_strikes(state) <= DRIFT_MAX_STRIKES

def _prev_day_move_pct(state: "EngineState") -> float:
    """Yesterday move as absolute %. Stored in config by market data service."""
    return abs(state.config.get("prev_day_move_pct", 0))

def _spot_rising_fast(state: "EngineState", window: int = 5, threshold: float = 0.3) -> bool:
    """True if spot moved >threshold% in last window readings — market not directionless."""
    if len(state.spot_history) < window:
        return False
    recent = state.spot_history[-window:]
    pct = abs(recent[-1] - recent[0]) / recent[0] * 100
    return pct > threshold

def _orb_range_valid(state: "EngineState", min_pct: float = 0.3) -> bool:
    """True if ORB range is meaningful (>min_pct of combined). Filters noise ORBs."""
    atm = state.atm
    if not atm or not atm.orb_high or not atm.orb_low or not atm.current:
        return False
    rng_pct = (atm.orb_high - atm.orb_low) / atm.current * 100
    return rng_pct >= min_pct


def _sb(state: EngineState) -> dict:
    return {s.offset: s for s in state.strikes}


def _build_legs(strategy: str, sell_strike: StrikeState,
                sb: dict, hedge_width: int = 2) -> dict:
    """
    Build order legs for a strategy.
    Always includes hedge wings to:
    - Define maximum risk
    - Reduce margin requirement (~50% reduction)
    - Comply with SEBI requirements
    
    Industry standard hedge widths:
    - Tight hedge (±100pt / 2 strikes): Iron Fly — max protection, lower premium
    - Medium hedge (±200pt / 4 strikes): Iron Condor — balance
    - Wide hedge (±300pt / 6 strikes): Wide condor — more premium, more risk
    """
    offset = sell_strike.offset
    hedge_ce = sb.get(offset + hedge_width) or sb.get(offset + hedge_width - 1) or sell_strike
    hedge_pe = sb.get(offset - hedge_width) or sb.get(offset - hedge_width + 1) or sell_strike
    return {
        "sell_ce":    sell_strike.ce_symbol,
        "sell_pe":    sell_strike.pe_symbol,
        "buy_ce":     hedge_ce.ce_symbol,
        "buy_pe":     hedge_pe.pe_symbol,
        "sell_strike": sell_strike.strike,
        "buy_ce_strike": hedge_ce.strike,
        "buy_pe_strike": hedge_pe.strike,
        "hedge_width":  hedge_width,
    }


# ── Strategy checks ───────────────────────────────────────────────

def check_all_strategies(state: EngineState, now: datetime) -> Optional[dict]:
    """
    Priority order: S7 > S1 > S8 > S2 > S3 > S4 > S6 > S9 > S5

    Four pre-signal guards (all configurable per automation):

    Guard 1 — One trade per day gate (always on)
    Guard 2 — VIX filter: if India VIX >= vix_max (default 17), skip all
              signals for the day. High VIX = elevated IV = options expensive
              to buy back = adverse SL hits likely.
    Guard 3 — Drift suspension: if spot has moved more than drift_max_pct
              (default 1.5%) from the morning open, suspend signals.
    Guard 4 — Previous day move: if yesterday moved >2%, restrict strategies.
              Only S9 allowed on post-big-move days (via individual strategy checks).

    FIRE WINDOW RULES (per strategy):
    S1: 9:22 — ORB-only, no VWAP/EMA needed. Fire as soon as ORB complete.
    S7: 9:30 — needs all strikes settled.
    S2/S3: 9:35 — needs 20+ candles for meaningful VWAP.
    S4/S8: 9:30 — needs price discovery to settle.
    S6: 9:45 — IV confirmation needs more time.
    S9: 11:00 — expiry theta crush window.
    """
    # Gate: one trade per automation per day
    if not state.orb_complete or state.position or state.traded_today:
        return None

    t = now.time()

    # Guard 2 — VIX filter
    vix_open = state.config.get("vix_open", 0)       # Set from Fyers at open if available
    vix_max  = state.config.get("vix_max",  17.0)    # User-configurable, default 17
    if vix_open and vix_open >= vix_max:
        state.emit(
            f"All signals suspended — India VIX {vix_open:.1f} >= threshold {vix_max:.1f}. "            f"High IV day: risk of adverse premium expansion too high.", "INFO")
        return None

    # Guard 3 — Drift suspension
    # If spot has moved more than drift_max_pct from morning open, suspend signals.
    # This catches days like today (NIFTY -3.26%) where IV expansion makes
    # all short-premium strategies unfavourable regardless of ORB levels.
    drift_max_pct = state.config.get("drift_max_pct", 1.5)  # default 1.5%
    if state.spot_locked and state.spot_history:
        current_spot  = state.spot_history[-1]
        drift_pct     = abs(current_spot - state.spot_locked) / state.spot_locked * 100
        if drift_pct >= drift_max_pct:
            state.emit(
                f"All signals suspended — spot drifted {drift_pct:.2f}% from open "                f"{state.spot_locked:.0f} → {current_spot:.0f} "                f"(threshold {drift_max_pct}%). "                f"High-drift days favour IV expansion, not theta decay.", "INFO")
            return None

    enabled = set(state.config.get("strategies", ["S1", "S8"]))
    # AI avoid list — strategies flagged by morning assessment
    ai_avoid = set(getattr(state, 'ai_avoid', []))
    if ai_avoid:
        enabled = enabled - ai_avoid

    # ── Guard 5 — Skip day filter ──────────────────────────────────
    # Suspends all SELLING strategies on gap/volatile days.
    # S10 (directional buy) is exempt and runs regardless.
    _selling_suspended = False
    _skip_reasons      = []
    _gap_skip_pct      = state.config.get("gap_skip_pct", 0)
    _prev_skip_pct     = state.config.get("prev_day_skip_pct", 0)
    _prev_c            = state.config.get("prev_close", 0)

    if _gap_skip_pct and _gap_skip_pct > 0 and _prev_c and state.spot_locked:
        _gap_abs = abs(state.spot_locked - _prev_c) / _prev_c * 100
        if _gap_abs >= _gap_skip_pct:
            _skip_reasons.append(f"gap {_gap_abs:.1f}% ≥ {_gap_skip_pct:.1f}%")
            _selling_suspended = True

    if _prev_skip_pct and _prev_skip_pct > 0:
        _prev_move = _prev_day_move_pct(state)
        if _prev_move >= _prev_skip_pct:
            _skip_reasons.append(f"prev day moved {_prev_move:.1f}% ≥ {_prev_skip_pct:.1f}%")
            _selling_suspended = True

    if _selling_suspended:
        state.emit(
            f"Selling signals suspended — {', '.join(_skip_reasons)}. "
            f"S10 directional buy still active if enabled.", "INFO")

    for code, fn in [
        ("S7",_s7),("S1",_s1),("S8",_s8),
        ("S2",_s2),("S3",_s3),("S4",_s4),
        ("S6",_s6),("S9",_s9),("S5",_s5),
        ("S10",_s10),
    ]:
        if code not in enabled: continue
        if _selling_suspended and code != "S10": continue  # selling strategies skip on gap/volatile days
        sig = fn(state, t, now)
        if sig: return sig
    return None


def check_sl(state: EngineState) -> Optional[str]:
    if not state.position: return None
    atm = state.atm
    if not atm: return None
    config = {
        "max_loss_pct":      state.config.get("max_loss_pct",      30),
        "trail_pct":         state.config.get("trail_pct",         20),
        "min_profit_pct":    state.config.get("min_profit_pct",    15),
        "vwap_buffer_pct":   state.config.get("vwap_buffer_pct",    2),
        "ema_buffer_pct":    state.config.get("ema_buffer_pct",     1),
        "profit_target_pct": state.config.get("profit_target_pct", 50),
    }
    exit_, reason = state.sl_state.update(
        atm.current, atm.vwap_val, atm.ema75, atm._ema_count, config)
    if exit_:
        state.emit(f"SL triggered: {reason}", "SL")
        return reason
    if state.sl_state.candles % 5 == 0:
        state.emit(
            f"Position: combined={atm.current:.1f} vwap={atm.vwap_val:.1f} "
            f"entry={state.sl_state.entry_combined:.1f} "
            f"trail_sl={state.sl_state.trailing_sl:.1f} "
            f"sl_type={state.sl_state.sl_type}", "INFO")
    return None


# ═══════════════════════════════════════════════════════════════════
# STRATEGY DEFINITIONS
# Industry-standard order structures with proper hedging
# All use Iron Fly or Iron Condor structure for defined risk
# ═══════════════════════════════════════════════════════════════════

def _s7(state, t, now):
    """
    S7 — All-Strike Iron Butterfly
    ================================
    Fires when ALL 7 strikes break ORB low simultaneously.
    Highest conviction — rare but powerful.
    Structure: Iron Fly at ATM (sell ATM CE+PE, buy OTM CE+PE)
    Hedge: ±2 strikes (tight wings, lower margin, higher protection)
    Industry insight: Maximum theta decay at ATM. Iron Fly uses less
    margin than naked straddle. Best on low-IV consolidation days.

    FIX: Drift guard — if spot has moved >3 strikes from morning ATM,
    the monitored strikes are no longer relevant. Skip to avoid
    entering a straddle at a stale strike far from current market.
    """
    if not (dtime(9,30) <= t <= dtime(10,0)): return None  # 15-min rule
    # Drift guard — spot too far from morning ATM
    if not _drift_ok(state):
        drift = _drift_strikes(state)
        state.emit(
            f"S7 skipped — spot drifted {drift:.0f} strikes from morning ATM "            f"{state.atm_strike} (current ATM {_current_atm(state)})", "INFO")
        return None
    broken = [s for s in state.strikes if s.orb_low > 0 and s.current < s.orb_low]
    if len(broken) < len(state.strikes) or len(broken) < 5: return None
    atm = state.atm
    if not atm: return None
    sb = _sb(state)
    legs = _build_legs("S7", atm, sb, hedge_width=2)
    return {
        "code":"S7", "name":"All-Strike Iron Butterfly",
        "structure":"Iron Fly (ATM sell, ±2 hedge)",
        "strike": atm.strike,
        "combined": atm.current,
        "reason": f"All {len(broken)} strikes below ORB low — drift {_drift_strikes(state):.0f} strikes",
        "margin_note": "~50% margin vs naked straddle",
        **legs,
    }


def _s1(state, t, now):
    """
    S1 — ORB Breakdown Iron Fly
    ============================
    PRIMARY strategy. ORB breakdown at the MORNING ATM strike only.
    Structure: Iron Fly at morning ATM
    Hedge: ±2 strikes for defined risk

    PROFESSIONAL RULE — Morning ATM only:
    The ORB (Opening Range Breakout) signal is only valid at the strike
    that was ATM when the range was built (9:15–9:21). If we fire at a
    different strike because spot drifted, we are trading a different
    market context with a stale signal.

    Gate: if spot has moved more than 1 strike (50 pts) from morning ATM,
    the signal is no longer valid — skip entirely. Do not chase.

    Example: morning ATM=23100, spot now=23160 (+60pts = 1.2 strikes)
    → Skip. The ORB at 23100 is no longer relevant at 23160.

    Example: morning ATM=23100, spot now=23130 (+30pts = 0.6 strikes)
    → Valid. Fire at 23100 if its ORB low was broken.
    """
    if not (dtime(9,22) <= t <= dtime(14,0)): return None  # ORB complete at 9:22 — fire immediately

    if not state.atm_strike: return None

    gap = state.config.get("strike_round", 50)

    # Professional gate: if spot has moved >1 strike from morning ATM, skip
    if state.spot_history:
        current_spot = state.spot_history[-1]
        spot_drift_pts = abs(current_spot - state.atm_strike)
        if spot_drift_pts > gap:
            state.emit(
                f"S1 skipped — spot {current_spot:.0f} is {spot_drift_pts:.0f}pts "
                f"from morning ATM {state.atm_strike} (>{gap}pt limit). "
                f"ORB signal no longer valid at current price.", "INFO")
            return None

    # Find the morning ATM strike in candidates
    morning_atm_strike = next(
        (s for s in state.strikes if s.is_atm and not s.fired), None)

    if not morning_atm_strike:
        return None  # Already fired or not found

    # Must have broken its ORB low
    if not (morning_atm_strike.orb_low > 0 and
            morning_atm_strike.current < morning_atm_strike.orb_low):
        return None  # Morning ATM has not broken ORB low

    # ORB range validity check — filter noise ORBs
    if not _orb_range_valid(state, min_pct=0.3):
        state.emit(
            f"S1 skipped — ORB range too tight "
            f"(high={morning_atm_strike.orb_high:.1f} "
            f"low={morning_atm_strike.orb_low:.1f}). Noise, not a signal.", "INFO")
        return None

    morning_atm_strike.fired = True
    sb = _sb(state)
    legs = _build_legs("S1", morning_atm_strike, sb, hedge_width=2)
    return {
        "code":"S1", "name":"ORB Breakdown Iron Fly",
        "structure":"Iron Fly (morning ATM sell, ±2 hedge)",
        "strike": morning_atm_strike.strike,
        "combined": morning_atm_strike.current,
        "reason": (f"Morning ATM {morning_atm_strike.strike} broke ORB low "
                   f"{morning_atm_strike.orb_low:.1f} | "
                   f"Range: {morning_atm_strike.orb_low:.1f}–{morning_atm_strike.orb_high:.1f}"),
        "margin_note": "Defined risk, ~50% margin vs naked",
        **legs,
    }


def _s8(state, t, now):
    """
    S8 — Gap Fade Iron Condor
    ==========================
    Gap day strategy. Market opens >0.4% from prev close.
    Structure: Iron Condor (wider strikes than Iron Fly)
    Hedge: ±3 strikes (wider range for gap days)
    Industry insight: Gaps fade 80% of time within first hour.

    PROFESSIONAL RULES:
    1. Never fade a gap after yesterday moved >2% in same direction.
       Momentum from a big fundamental move extends, not fades.
       Example: 19 Mar fell 3.26% (crude spike). 20 Mar gapped up.
       Fading that gap = betting against the recovery. Wrong.
    2. Wait until 9:30 — first 15 minutes are price discovery noise.
    """
    if not (dtime(9,30) <= t <= dtime(10,0)): return None  # 15-min rule
    atm = state.atm
    if not atm: return None
    prev = state.config.get("prev_close", 0)
    if not prev or not state.spot_locked: return None
    gap_pct = abs(state.spot_locked - prev) / prev * 100
    if gap_pct < 0.4 or atm.current < 50: return None

    # Previous day filter — never fade after a big move
    prev_move = _prev_day_move_pct(state)
    if prev_move > 2.0:
        state.emit(
            f"S8 skipped — yesterday moved {prev_move:.1f}% "
            f"(>{2.0}% threshold). Gap likely extends, not fades.", "INFO")
        return None

    # Direction-aware filter: if gap is CONTINUING (not fading), skip.
    # Gap up + spot still rising fast → momentum not exhausted → don't fade.
    # Gap down + spot still falling fast → same logic.
    gap_up = state.spot_locked > prev
    if _spot_rising_fast(state, window=5, threshold=0.2):
        if gap_up:
            state.emit(
                f"S8 skipped — gap-up day ({gap_pct:.2f}%) but spot still rising. "
                f"Gap continuing, fade not safe.", "INFO")
            return None
    if not gap_up and len(state.spot_history) >= 5:
        recent = state.spot_history[-5:]
        falling = (recent[-1] - recent[0]) / recent[0] * 100 < -0.2
        if falling:
            state.emit(
                f"S8 skipped — gap-down day ({gap_pct:.2f}%) but spot still falling. "
                f"Gap continuing, fade not safe.", "INFO")
            return None

    sb = _sb(state)
    # Wider hedge for gap days (±3 strikes = Iron Condor)
    legs = _build_legs("S8", atm, sb, hedge_width=3)
    return {
        "code":"S8", "name":"Gap Fade Iron Condor",
        "structure":"Iron Condor (ATM sell, ±3 hedge for gap buffer)",
        "strike": atm.strike,
        "combined": atm.current,
        "reason": f"Gap {gap_pct:.2f}% fading — elevated premium, wider hedge",
        "margin_note": "Iron Condor: max defined risk",
        **legs,
    }


def _s2(state, t, now):
    """
    S2 — VWAP Squeeze Iron Fly
    ===========================
    The REAL squeeze: premium was ABOVE VWAP (spike), then pulled back
    below VWAP — sell the reversal. This is the actual squeeze pattern.

    What was wrong before:
    - Fired at 9:22 with 7 candles — VWAP meaningless
    - Condition "below VWAP" fires almost always at open (wrong)
    - EMA check skipped when count < 30 (no guard at all)
    - Today (20 Mar): fired at 9:22 with 7 candles, market rising → lost ₹1,472

    Correct signal requires ALL of:
    1. Minimum 20 candles (ensures VWAP is statistically meaningful)
    2. EMA75 must exist (>=20 candles) and confirm bearish
    3. Combined was ABOVE VWAP at some point in last 10 candles (the spike)
    4. Combined is now BELOW VWAP (the reversal = the squeeze)
    5. Market not rising fast (direction alignment)

    Professional rule: VWAP squeeze is only valid after the market has
    established a VWAP level. That requires 20+ minutes of data, not 7.
    """
    if not (dtime(9,35) <= t <= dtime(10,30)): return None  # 20+ min needed

    atm = state.atm
    if not atm: return None

    # Minimum 20 candles for meaningful VWAP
    if len(atm.combined_history) < 20: return None

    # EMA must exist before firing — no signals without trend confirmation
    if atm._ema_count < 20: return None

    # EMA75 must be bearish (above combined = downward pressure)
    if atm.ema75 <= atm.current: return None

    # Combined must currently be BELOW VWAP (pullback confirmed)
    if atm.current >= atm.vwap_val: return None

    # THE REAL SQUEEZE: combined must have been ABOVE VWAP recently
    # (if it was never above VWAP, there was no spike to reverse)
    recent_10 = atm.combined_history[-10:]
    was_above_vwap = any(c > atm.vwap_val for c in recent_10)
    if not was_above_vwap:
        return None  # No spike to reverse — not a squeeze

    # Direction guard: if spot is rising fast, premium selling is risky
    if _spot_rising_fast(state, window=5, threshold=0.3):
        return None

    cur_atm = _current_atm(state)
    atm_to_use = next((s for s in state.strikes
                       if s.strike == cur_atm), atm)
    sb = _sb(state)
    legs = _build_legs("S2", atm_to_use, sb, hedge_width=2)
    spike_high = max(recent_10)
    return {
        "code":"S2", "name":"VWAP Squeeze Iron Fly",
        "structure":"Iron Fly (current ATM, ±2 hedge)",
        "strike": atm_to_use.strike,
        "combined": atm_to_use.current,
        "reason": (f"VWAP squeeze: spiked to {spike_high:.1f}, "
                   f"reversed below VWAP {atm.vwap_val:.1f} "
                   f"| EMA75={atm.ema75:.1f}"),
        **legs,
    }


def _s3(state, t, now):
    """
    S3 — Breakout Reversal Iron Fly
    ================================
    Premium spike above VWAP then fails — sell the reversal.
    Structure: Iron Fly at ATM
    Hedge: ±2 strikes
    Industry insight: Breakout failures are high probability setups.
    Selling at reversal point captures inflated IV from the spike.
    """
    if not (dtime(9,35) <= t <= dtime(14,0)): return None  # needs 20+ candles for VWAP spike pattern
    atm = state.atm
    if not atm or len(atm.combined_history) < 20: return None  # 20 candles minimum for S3
    recent_high = max(atm.combined_history[-15:])  # 15 candles (was 5 — too short)
    if recent_high < atm.vwap_val * 1.05 or atm.current >= atm.vwap_val: return None
    sb = _sb(state)
    legs = _build_legs("S3", atm, sb, hedge_width=2)
    return {
        "code":"S3", "name":"Breakout Reversal Iron Fly",
        "structure":"Iron Fly (ATM, ±2 hedge)",
        "strike": atm.strike,
        "combined": atm.current,
        "reason": f"Spike to {recent_high:.1f} reversed below VWAP {atm.vwap_val:.1f}",
        **legs,
    }


def _s4(state, t, now):
    """
    S4 — Iron Condor (Range-bound)
    ================================
    Classic Iron Condor for range-bound days.
    Sell ±1 strike, buy ±3 strike hedge.
    Industry insight: Iron Condor is the workhorse of professional
    options income traders. Lower premium but fully defined risk.
    Best when VIX < 13 and market in consolidation.
    Capital efficiency: ~₹35-40K margin per lot (vs ₹1L naked).
    """
    if not (dtime(9,30) <= t <= dtime(10,0)): return None
    atm = state.atm
    if not atm or len(atm.combined_history) < 15: return None
    hist = atm.combined_history[-15:]
    rng = (max(hist)-min(hist)) / atm.current if atm.current else 1
    if rng > 0.05: return None  # tightened 8%→5%: only truly range-bound days
    sb = _sb(state)
    sell_ce  = sb.get(1, atm)
    sell_pe  = sb.get(-1, atm)
    buy_ce   = sb.get(3) or sb.get(2, atm)
    buy_pe   = sb.get(-3) or sb.get(-2, atm)
    combined = sell_ce.current + sell_pe.current
    return {
        "code":"S4", "name":"Iron Condor",
        "structure":"Iron Condor (sell ±1, buy ±3 wings)",
        "strike": atm.strike,
        "combined": combined,
        "sell_ce": sell_ce.ce_symbol, "sell_pe": sell_pe.pe_symbol,
        "buy_ce": buy_ce.ce_symbol,   "buy_pe": buy_pe.pe_symbol,
        "sell_strike": atm.strike,
        "buy_ce_strike": buy_ce.strike,
        "buy_pe_strike": buy_pe.strike,
        "reason": f"Range-bound {rng*100:.1f}% — Iron Condor optimal",
        "margin_note": "Fully defined risk, lowest margin",
    }


def _s6(state, t, now):
    """
    S6 — Theta Decay Strangle (Wide Iron Condor)
    ==============================================
    High IV environment. Sell wider strikes to collect more premium.
    Sell ±1, buy ±4 wings.
    Industry insight: Sell when IV is high, let IV crush + theta work.
    Wider strikes = more premium collected, wider profit zone.
    Use when India VIX > 15 and premium elevated at open.
    """
    if not (dtime(9,45) <= t <= dtime(10,30)): return None
    atm = state.atm
    if not atm or not atm.orb_high: return None
    if atm.current < atm.orb_high * 1.05: return None  # fire only when IV elevated (combined above ORB)
    sb = _sb(state)
    sell_ce = sb.get(1, atm); sell_pe = sb.get(-1, atm)
    buy_ce  = sb.get(4) or sb.get(3, atm)
    buy_pe  = sb.get(-4) or sb.get(-3, atm)
    combined = sell_ce.current + sell_pe.current
    return {
        "code":"S6", "name":"Theta Decay Wide Condor",
        "structure":"Wide Iron Condor (sell ±1, buy ±4 wings)",
        "strike": atm.strike,
        "combined": combined,
        "sell_ce": sell_ce.ce_symbol, "sell_pe": sell_pe.pe_symbol,
        "buy_ce": buy_ce.ce_symbol,   "buy_pe": buy_pe.pe_symbol,
        "sell_strike": atm.strike,
        "buy_ce_strike": buy_ce.strike,
        "buy_pe_strike": buy_pe.strike,
        "reason": f"Elevated IV — sell wide condor, buy ±4 wings",
        "margin_note": "Fully defined, premium elevated",
    }


def _s9(state, t, now):
    """
    S9 — Pre-Expiry Theta Crush Iron Fly
    ======================================
    Expiry day only (Thursday). Rapid theta decay 11AM-12PM.
    ATM Iron Fly with tight ±1 hedge (expiry = minimal movement).
    Industry insight: ATM options lose 50-70% of value in last 2 hours.
    Tight hedge because: (a) movement is limited near expiry,
    (b) ±1 hedge is cheapest and still reduces margin significantly.
    """
    if now.weekday() != 3: return None  # Thursday only
    if not (dtime(11,0) <= t <= dtime(12,0)): return None
    atm = state.atm
    if not atm: return None

    # Yesterday move filter — big move = unpredictable IV on expiry
    prev_move = _prev_day_move_pct(state)
    hedge = 2  # standard expiry hedge
    if prev_move > 2.0:
        state.emit(
            f"S9: yesterday moved {prev_move:.1f}% — widening hedge to ±3 for safety. "
            f"Post-big-move expiry has unpredictable IV crush timing.", "INFO")
        hedge = 3  # wider hedge after big move day
    sb = _sb(state)
    legs = _build_legs("S9", atm, sb, hedge_width=hedge)
    return {
        "code":"S9", "name":"Pre-Expiry Theta Crush",
        "structure":f"Iron Fly (ATM sell, ±{hedge} hedge — expiry day)",
        "strike": atm.strike,
        "combined": atm.current,
        "reason": f"Expiry day — rapid ATM theta decay 11:00-12:00 | hedge=±{hedge}",
        "margin_note": f"Hedge ±{hedge} — {"wider after big move" if hedge > 2 else "tight expiry hedge"}",
        **legs,
    }


def _s10(state, t, now):
    """
    S10 — Gap Directional Buy
    ==========================
    Clear gap day (>1%). Buy ATM CE (gap-up) or ATM PE (gap-down).
    Momentum trade — ride the gap direction for first 90 minutes.

    Structure: Single-leg BUY (CE or PE at ATM)
    Fire window: 9:20–9:25 only (first-candle momentum)
    Exit: 11:00 AM hard cut OR SL (premium -40% OR spot reversal 30pts)

    This is a BUY strategy, so:
    - is_buy = True in the returned signal
    - direction: "CE" or "PE"
    - SL is tracked in _run_engine differently from sell strategies
    """
    if not (dtime(9,20) <= t <= dtime(9,25)): return None
    atm = state.atm
    if not atm: return None
    prev = state.config.get("prev_close", 0)
    if not prev or not state.spot_locked: return None

    gap_pct = (state.spot_locked - prev) / prev * 100  # signed gap
    abs_gap = abs(gap_pct)

    # Only fire on clear gaps ≥1%
    if abs_gap < 1.0: return None

    # Never trade S10 if gap is so large that selling is suspended (>skip threshold)
    # Use the same skip thresholds from config but allow S10 up to 2.5%
    gap_skip_pct = state.config.get("gap_skip_pct", 0)
    if gap_skip_pct and abs_gap >= gap_skip_pct and abs_gap > 2.5:
        state.emit(
            f"S10 skipped — gap {abs_gap:.1f}% exceeds safety limit. "
            f"Extreme gap days have too much intraday risk.", "INFO")
        return None

    direction = "CE" if gap_pct > 0 else "PE"
    buy_symbol = atm.ce_symbol if direction == "CE" else atm.pe_symbol
    entry_ltp  = atm.ce_ltp    if direction == "CE" else atm.pe_ltp

    if not buy_symbol or entry_ltp <= 0:
        state.emit(f"S10 skipped — no {direction} symbol or zero LTP at ATM {atm.strike}", "INFO")
        return None

    return {
        "code":      "S10",
        "name":      "Gap Directional Buy",
        "structure": f"Single-leg BUY {direction} at ATM {atm.strike}",
        "strike":    atm.strike,
        "combined":  entry_ltp,   # for position tracking (single leg)
        "is_buy":    True,
        "direction": direction,
        "buy_symbol": buy_symbol,
        "entry_ltp":  entry_ltp,
        "reason":     (f"Gap {gap_pct:+.2f}% — BUY {direction} for momentum. "
                       f"Hard exit 11:00 AM."),
        "margin_note": "BUY only — max loss = premium paid",
        # No sell_ce/sell_pe/buy_ce/buy_pe — handled separately in _open_position
    }


def _s5(state, t, now):
    """
    S5 — Directional Ratio Spread (Advanced)
    ==========================================
    Strong downtrend. Sell 2x ATM, buy 1x OTM hedge.
    WARNING: Not fully defined risk — advanced traders only.
    Industry insight: Used when strong conviction on direction.
    Ratio creates more premium income but leaves one side exposed.
    Only deploy when EMA75 confirms strong downtrend.
    """
    # S5 is disabled by default — only enable if strategies config explicitly includes S5
    # Undefined risk (partial hedge). Advanced traders only.
    if "S5" not in state.config.get("strategies", []):
        return None
    if not (dtime(9,30) <= t <= dtime(11,0)): return None
    atm = state.atm
    if not atm or atm._ema_count < 20: return None
    if atm.current >= atm.ema75 * 0.95: return None
    sb = _sb(state)
    buy_hedge = sb.get(3) or sb.get(2, atm)
    return {
        "code":"S5", "name":"Ratio Spread (Advanced)",
        "structure":"Sell 2x ATM CE+PE, buy 1x OTM hedge each side",
        "strike": atm.strike,
        "combined": atm.current,
        "sell_ce": atm.ce_symbol, "sell_pe": atm.pe_symbol,
        "buy_ce": buy_hedge.ce_symbol, "buy_pe": buy_hedge.pe_symbol,
        "lots_multiplier": 2,
        "reason": f"Strong downtrend — combined {atm.current:.1f} << EMA {atm.ema75:.1f}",
        "margin_note": "⚠️ Advanced: partial hedge only",
    }
