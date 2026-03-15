"""
ALGO-DESK — Trading Engine
===========================
Runs per automation. Manages the full trading lifecycle:
  - ORB window collection (9:15-9:21)
  - Strategy signal detection (9:22+)
  - Order placement via broker
  - SL monitoring every minute
  - Auto-exit at configured time
  - Telegram alerts
  - Trade persistence to DB
"""

import logging
import asyncio
from datetime import datetime, time as dtime
from typing import Optional
import numpy as np
import pandas as pd

log = logging.getLogger("engine")

# ── Math helpers ──────────────────────────────────────────────────

def ema(values: list, period: int) -> float:
    if not values: return 0.0
    return float(pd.Series(values).ewm(span=period, adjust=False).mean().iloc[-1])

def vwap(prices: list, volumes: list) -> float:
    if not prices: return 0.0
    vols = volumes if volumes and sum(volumes) > 0 else [1.0] * len(prices)
    return sum(p * v for p, v in zip(prices, vols)) / sum(vols)

def rsi(values: list, period: int = 14) -> float:
    if len(values) < period + 1: return 50.0
    s = pd.Series(values)
    delta = s.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    r = (100 - (100 / (1 + rs))).iloc[-1]
    return float(r) if not pd.isna(r) else 50.0

def bollinger_width(values: list, period: int = 15) -> float:
    if len(values) < period: return 1.0
    s = pd.Series(values[-period:])
    mid = s.mean()
    return float(s.std() * 2 / mid) if mid > 0 else 1.0

def iv_percentile(history: list, lookback: int = 30) -> float:
    if len(history) < lookback: return 50.0
    w = history[-lookback:]
    cur, mn, mx = w[-1], min(w), max(w)
    return (cur - mn) / (mx - mn) * 100 if mx != mn else 50.0

def nearest_strike(spot: float, gap: int) -> int:
    return round(spot / gap) * gap

# ── Engine state ──────────────────────────────────────────────────

class StrikeState:
    def __init__(self, strike: int, offset: int, is_atm: bool = False):
        self.strike   = strike
        self.offset   = offset
        self.is_atm   = is_atm
        self.orb_high = 0.0
        self.orb_low  = 0.0
        self.combined_history: list[float] = []
        self.vol_history:      list[float] = []
        self.ce_symbol = ""
        self.pe_symbol = ""
        self.fired     = False

    @property
    def current(self) -> float:
        return self.combined_history[-1] if self.combined_history else 0.0

    @property
    def vwap_val(self) -> float:
        return vwap(self.combined_history, self.vol_history)

    @property
    def ema75(self) -> float:
        return ema(self.combined_history, 75)

    @property
    def ema20(self) -> float:
        return ema(self.combined_history, 20)

class EngineState:
    def __init__(self, config: dict):
        self.config        = config
        self.spot_locked   = 0.0
        self.atm_strike    = 0
        self.strikes: list[StrikeState] = []
        self.orb_complete  = False
        self.no_breakdown  = False
        self.all_breakdown = False
        self.position      = None
        self.is_running    = False
        self.day_pnl       = 0.0
        self.spot_history: list[float] = []
        self.prev_close    = 0.0
        self.is_expiry_day = config.get("is_expiry_day", False)
        self.log: list[dict] = []

    @property
    def gap(self) -> int:
        return int(self.config.get("strike_round", 50))

    @property
    def sides(self) -> int:
        return int(self.config.get("strike_sides", 3))

    @property
    def atm(self) -> Optional[StrikeState]:
        return next((s for s in self.strikes if s.is_atm), None)

    def emit(self, msg: str, kind: str = "INFO"):
        entry = {"ts": datetime.now().strftime("%H:%M:%S"), "msg": msg, "kind": kind}
        self.log.append(entry)
        if len(self.log) > 200:
            self.log = self.log[-200:]
        log.info(f"[engine] {msg}")

# ── Strategy checks ───────────────────────────────────────────────

def check_all_strategies(state: EngineState, now: datetime) -> Optional[dict]:
    """
    Check all strategies in priority order.
    Returns signal dict or None.
    """
    t = now.time()
    enabled = state.config.get("strategies", ["S7","S1","S2","S8","S3","S4","S6","S9"])
    atm = state.atm
    if not atm or atm.current == 0:
        return None

    # ── S7: All-Strike Iron Butterfly ────────────────────────────
    if "S7" in enabled and state.all_breakdown and not state.position:
        broken = [s for s in state.strikes if s.orb_low > 0 and s.current < s.orb_low * 0.98]
        if len(broken) >= len(state.strikes) and atm.current >= state.config.get("min_premium", 120):
            gap = state.gap
            return {
                "code": "S7", "name": "All-Strike Iron Butterfly",
                "strike": atm.strike,
                "sell_ce": atm.strike, "sell_pe": atm.strike,
                "sell_ce2": atm.strike + gap, "sell_pe2": atm.strike - gap,
                "buy_ce": atm.strike + 3*gap, "buy_pe": atm.strike - 3*gap,
                "combined": atm.current,
                "reason": f"All {len(state.strikes)} strikes broke ORB low",
            }

    # ── S1: ORB Breakdown ────────────────────────────────────────
    if "S1" in enabled and state.orb_complete and t >= dtime(9,22) and not state.position:
        if not state.all_breakdown:
            candidates = [s for s in state.strikes
                          if not s.fired and s.orb_low > 0 and s.current < s.orb_low]
            if candidates:
                winner = sorted(candidates, key=lambda s: (abs(s.offset), s.current/s.orb_low))[0]
                winner.fired = True
                gap = state.gap
                hedge = state.config.get("hedging_enabled", True)
                return {
                    "code": "S1", "name": "ORB Breakdown Sell",
                    "strike": winner.strike,
                    "sell_ce": winner.strike, "sell_pe": winner.strike,
                    "sell_ce2": None, "sell_pe2": None,
                    "buy_ce": winner.strike + 2*gap if hedge else None,
                    "buy_pe": winner.strike - 2*gap if hedge else None,
                    "combined": winner.current,
                    "reason": f"Strike {winner.strike} broke ORB low {winner.orb_low:.1f}",
                }

    # ── S2: VWAP Squeeze ─────────────────────────────────────────
    if "S2" in enabled and state.no_breakdown and dtime(9,22) <= t <= dtime(10,30) and not state.position:
        c, v, e = atm.current, atm.vwap_val, atm.ema75
        if (v > 0 and e > 0 and c < v and e < v
                and abs(c-v)/v < 0.025 and rsi(atm.combined_history) < 45):
            gap = state.gap
            return {
                "code": "S2", "name": "VWAP Squeeze",
                "strike": atm.strike,
                "sell_ce": atm.strike, "sell_pe": atm.strike,
                "sell_ce2": None, "sell_pe2": None,
                "buy_ce": atm.strike + 2*gap, "buy_pe": atm.strike - 2*gap,
                "combined": c,
                "reason": f"VWAP squeeze: combined {c:.1f} below VWAP {v:.1f}",
            }

    # ── S8: Gap Fade ─────────────────────────────────────────────
    if "S8" in enabled and dtime(9,22) <= t <= dtime(9,45) and not state.position:
        if state.prev_close > 0:
            gap_pct = abs(state.spot_locked - state.prev_close) / state.prev_close * 100
            c, v = atm.current, atm.vwap_val
            if gap_pct > 0.4 and c > 100 and c < v:
                gap = state.gap
                return {
                    "code": "S8", "name": "Opening Gap Fade",
                    "strike": atm.strike,
                    "sell_ce": atm.strike, "sell_pe": atm.strike,
                    "sell_ce2": None, "sell_pe2": None,
                    "buy_ce": atm.strike + 2*gap, "buy_pe": atm.strike - 2*gap,
                    "combined": c,
                    "reason": f"Gap fade: {gap_pct:.2f}% gap, premium compressing",
                }

    # ── S3: Breakout Reversal ────────────────────────────────────
    if "S3" in enabled and t >= dtime(9,22) and not state.position:
        c, v, e = atm.current, atm.vwap_val, atm.ema75
        if not hasattr(state, '_s3_was_above'): state._s3_was_above = False
        if not hasattr(state, '_s3_spike'): state._s3_spike = 0.0
        vb = state.config.get("vwap_buffer", 5) / 100
        if c > v*(1+vb) and c > e:
            state._s3_was_above = True
            state._s3_spike = max(state._s3_spike, c)
        if state._s3_was_above and c < v and c < e:
            state._s3_was_above = False
            spike = state._s3_spike; state._s3_spike = 0.0
            gap = state.gap
            return {
                "code": "S3", "name": "Breakout Reversal",
                "strike": atm.strike,
                "sell_ce": atm.strike, "sell_pe": atm.strike,
                "sell_ce2": None, "sell_pe2": None,
                "buy_ce": atm.strike + gap, "buy_pe": atm.strike - gap,
                "combined": c,
                "reason": f"Reversal from spike {spike:.1f} back below VWAP",
            }

    # ── S4: Iron Condor ──────────────────────────────────────────
    if "S4" in enabled and dtime(9,30) <= t <= dtime(10,0) and not state.position:
        c, v = atm.current, atm.vwap_val
        gap = state.gap
        if (len(atm.combined_history) >= 15 and
                bollinger_width(atm.combined_history) < 0.08 and
                30 < iv_percentile(atm.combined_history) < 65 and
                40 < rsi(atm.combined_history) < 60):
            return {
                "code": "S4", "name": "Iron Condor",
                "strike": atm.strike,
                "sell_ce": atm.strike + gap, "sell_pe": atm.strike - gap,
                "sell_ce2": None, "sell_pe2": None,
                "buy_ce": atm.strike + 3*gap, "buy_pe": atm.strike - 3*gap,
                "combined": c,
                "reason": "Range-bound day: Bollinger squeeze",
            }

    # ── S6: Theta Decay Strangle ─────────────────────────────────
    if "S6" in enabled and dtime(9,45) <= t <= dtime(10,30) and not state.position:
        c, v = atm.current, atm.vwap_val
        gap = state.gap
        iv_pct = iv_percentile(atm.combined_history)
        if (len(atm.combined_history) >= 30 and iv_pct > 65
                and abs(c-v)/v < 0.04):
            return {
                "code": "S6", "name": "Theta Decay Strangle",
                "strike": atm.strike,
                "sell_ce": atm.strike + gap, "sell_pe": atm.strike - gap,
                "sell_ce2": None, "sell_pe2": None,
                "buy_ce": atm.strike + 4*gap, "buy_pe": atm.strike - 4*gap,
                "combined": c,
                "reason": f"High IV {iv_pct:.0f}pct, premium stable near VWAP",
            }

    # ── S9: Pre-Expiry Theta Crush ───────────────────────────────
    if "S9" in enabled and state.is_expiry_day and not state.position:
        expiry_start = dtime(*[int(x) for x in state.config.get("expiry_start","11:00").split(":")])
        expiry_end   = dtime(*[int(x) for x in state.config.get("expiry_end","12:00").split(":")])
        if expiry_start <= t <= expiry_end:
            c = atm.current
            gap = state.gap
            if (c > state.config.get("min_premium_expiry", 80) and
                    iv_percentile(atm.combined_history) > 40 and
                    abs(state.spot_locked - atm.strike) / atm.strike < 0.003):
                return {
                    "code": "S9", "name": "Pre-Expiry Theta Crush",
                    "strike": atm.strike,
                    "sell_ce": atm.strike, "sell_pe": atm.strike,
                    "sell_ce2": None, "sell_pe2": None,
                    "buy_ce": atm.strike + 2*gap, "buy_pe": atm.strike - 2*gap,
                    "combined": c,
                    "reason": f"Expiry theta crush: tight butterfly",
                }

    return None

def check_sl(state: EngineState) -> Optional[str]:
    """Check stop loss. Returns exit reason or None."""
    if not state.position:
        return None
    atm = state.atm
    if not atm:
        return None

    pos    = state.position
    c      = atm.current
    v      = atm.vwap_val
    e      = atm.ema75
    config = state.config

    vb  = config.get("vwap_buffer", 5) / 100
    eb  = config.get("ema_buffer", 1) / 100
    hsl = config.get("hard_sl_pct", 150) / 100
    pt  = config.get("profit_target_pct", 50) / 100

    # Hard stop
    if c > pos["entry_combined"] * (1 + hsl):
        return "HARD_SL"

    # Profit target
    if c <= pos["entry_combined"] * (1 - pt):
        return "PROFIT_TARGET"

    # Trailing SL
    if e > 0 and e < v:
        sl_val = e * (1 + eb)
        if c > sl_val:
            return "EMA_SL"
    else:
        sl_val = v * (1 + vb)
        if c > sl_val:
            return "VWAP_SL"

    return None
