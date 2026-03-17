"""
ALGO-DESK — Trading Engine v2
==============================
Stop Loss approach: Industry standard for NIFTY short straddle/strangle.
Based on research from AlgoTest, AlgoBulls, professional NIFTY traders.

Core principle (non-negotiable):
  ALL SL and profit target decisions based ONLY on combined premium.
  No hardcoded numbers. No spot price. No individual legs.

Three-layer SL system:
  Layer 1 — Trailing SL      : locks in gains as premium decays
  Layer 2 — VWAP SL          : exit if combined closes above VWAP
  Layer 3 — Max loss backstop : exit if combined rises 30%+ above entry

All layers percentage-based relative to entry combined premium.
All layers configurable per automation.
"""

import logging
from datetime import datetime, time as dtime
from typing import Optional, List
from dataclasses import dataclass, field

log = logging.getLogger("engine")


@dataclass
class StrikeState:
    strike: int
    offset: int
    is_atm: bool = False
    ce_symbol: str = ""
    pe_symbol: str = ""
    combined_history: List[float] = field(default_factory=list)
    orb_high: float = 0.0
    orb_low: float = 0.0
    fired: bool = False
    _vwap_pv: float = 0.0
    _vwap_v: float = 0.0
    vwap_val: float = 0.0
    ema75: float = 0.0
    _ema_count: int = 0

    @property
    def current(self):
        return self.combined_history[-1] if self.combined_history else 0.0

    def update(self, combined: float, volume: float = 1.0):
        self.combined_history.append(combined)
        self._vwap_pv += combined * volume
        self._vwap_v  += volume
        self.vwap_val  = self._vwap_pv / self._vwap_v if self._vwap_v else combined
        k = 2 / 76
        self._ema_count += 1
        self.ema75 = combined * k + self.ema75 * (1 - k) if self.ema75 else combined


class SLState:
    """
    Manages stop loss entirely based on combined premium.
    No fixed numbers. Everything relative to entry combined.
    """
    def __init__(self):
        self.reset()

    def reset(self):
        self.entry_combined = 0.0
        self.trailing_low = 0.0
        self.trailing_sl = 0.0
        self.candles = 0
        self.sl_type = "NONE"

    def activate(self, entry_combined, config):
        self.entry_combined = entry_combined
        self.trailing_low = entry_combined
        max_loss_pct = config.get("max_loss_pct", 30) / 100
        self.trailing_sl = entry_combined * (1 + max_loss_pct)
        self.candles = 0
        self.sl_type = "VWAP"

    def update(self, current, vwap, ema75, ema_count, config):
        """
        Returns (exit: bool, reason: str).
        All checks on combined premium only.
        """
        self.candles += 1
        max_loss_pct = config.get("max_loss_pct", 30) / 100
        trail_pct = config.get("trail_pct", 20) / 100
        min_profit_pct = config.get("min_profit_pct", 15) / 100
        vwap_buf = config.get("vwap_buffer_pct", 2) / 100
        ema_buf = config.get("ema_buffer_pct", 1) / 100
        target_pct = config.get("profit_target_pct", 30) / 100

        # Update trailing SL as premium decays
        if current < self.trailing_low:
            self.trailing_low = current
            self.trailing_sl = self.trailing_low * (1 + trail_pct)

        # Layer 1: Trailing SL
        # Only after minimum profit locked in
        if (self.trailing_low <= self.entry_combined * (1 - min_profit_pct)
                and current >= self.trailing_sl):
            return True, (f"TRAILING_SL entry={self.entry_combined:.1f} "
                         f"low={self.trailing_low:.1f} "
                         f"trail_sl={self.trailing_sl:.1f} "
                         f"current={current:.1f}")

        # Layer 2: VWAP SL (after 3 candles to avoid noise)
        if self.candles >= 3 and vwap > 0:
            if current > vwap * (1 + vwap_buf):
                return True, (f"VWAP_SL combined={current:.1f} "
                             f"vwap={vwap:.1f} "
                             f"buffer={vwap_buf*100:.0f}%")

        # Layer 2b: EMA75 SL when below VWAP (tighter)
        if (ema_count >= 75 and ema75 > 0 and vwap > 0
                and ema75 < vwap and self.candles >= 3):
            if current > ema75 * (1 + ema_buf):
                return True, (f"EMA75_SL combined={current:.1f} "
                             f"ema75={ema75:.1f}")

        # Layer 3: Maximum loss backstop
        if current > self.entry_combined * (1 + max_loss_pct):
            return True, (f"MAX_LOSS combined={current:.1f} "
                         f"entry={self.entry_combined:.1f} "
                         f"max={max_loss_pct*100:.0f}%")

        # Profit target
        if current <= self.entry_combined * target_pct:
            return True, (f"PROFIT_TARGET entry={self.entry_combined:.1f} "
                         f"current={current:.1f} "
                         f"captured={(1-target_pct)*100:.0f}%")

        return False, ""


class EngineState:
    def __init__(self, config):
        self.config = config
        self.is_running = False
        self.spot_history = []
        self.atm_strike = None
        self.spot_locked = None
        self.strikes = []
        self.orb_complete = False
        self.position = None
        self.day_pnl = 0.0
        self.sl_state = SLState()
        self.log = []

    @property
    def atm(self):
        return next((s for s in self.strikes if s.is_atm), None)

    def emit(self, msg, kind="INFO"):
        self.log.append({"ts": datetime.now().strftime("%H:%M:%S"),
                         "msg": msg, "kind": kind})
        if len(self.log) > 200:
            self.log = self.log[-200:]
        log.info(f"[engine][{kind}] {msg}")


def nearest_strike(spot, gap=50):
    return round(spot / gap) * gap


def check_sl(state):
    if not state.position:
        return None
    atm = state.atm
    if not atm:
        return None
    current = atm.current
    vwap = atm.vwap_val
    ema75 = atm.ema75
    ema_count = atm._ema_count
    config = {
        "max_loss_pct":      state.config.get("max_loss_pct", 30),
        "trail_pct":         state.config.get("trail_pct", 20),
        "min_profit_pct":    state.config.get("min_profit_pct", 15),
        "vwap_buffer_pct":   state.config.get("vwap_buffer_pct", 2),
        "ema_buffer_pct":    state.config.get("ema_buffer_pct", 1),
        "profit_target_pct": state.config.get("profit_target_pct", 30),
    }
    exit_, reason = state.sl_state.update(current, vwap, ema75, ema_count, config)
    if exit_:
        state.emit(f"SL triggered: {reason}", "SL")
        return reason
    if state.sl_state.candles % 5 == 0:
        state.emit(
            f"Position: combined={current:.1f} vwap={vwap:.1f} "
            f"entry={state.sl_state.entry_combined:.1f} "
            f"trail_sl={state.sl_state.trailing_sl:.1f}",
            "INFO")
    return None


def check_all_strategies(state, now):
    if not state.orb_complete or state.position:
        return None
    enabled = set(state.config.get("strategies", ["S1", "S8"]))
    t = now.time()
    checks = [
        ("S7", _s7), ("S1", _s1), ("S8", _s8),
        ("S2", _s2), ("S3", _s3), ("S4", _s4),
        ("S6", _s6), ("S9", _s9), ("S5", _s5),
    ]
    for code, fn in checks:
        if code in enabled:
            sig = fn(state, t, now)
            if sig:
                return sig
    return None


def _sb(state):
    return {s.offset: s for s in state.strikes}


def _s7(state, t, now):
    if not (dtime(9,22) <= t <= dtime(10,0)): return None
    broken = [s for s in state.strikes if s.orb_low > 0 and s.current < s.orb_low]
    if len(broken) < len(state.strikes) or len(broken) < 5: return None
    atm = state.atm
    if not atm: return None
    sb = _sb(state)
    return {"code":"S7","name":"All-Strike Iron Butterfly","strike":atm.strike,
            "sell_ce":atm.ce_symbol,"sell_pe":atm.pe_symbol,
            "buy_ce":sb.get(3,atm).ce_symbol,"buy_pe":sb.get(-3,atm).pe_symbol,
            "combined":atm.current,
            "reason":f"All {len(broken)} strikes below ORB low simultaneously"}


def _s1(state, t, now):
    if not (dtime(9,22) <= t <= dtime(14,0)): return None
    candidates = [s for s in state.strikes
                  if s.orb_low > 0 and s.current < s.orb_low and not s.fired]
    if not candidates: return None
    w = sorted(candidates, key=lambda s: abs(s.offset))[0]
    w.fired = True
    sb = _sb(state)
    bce = sb.get(w.offset+2) or sb.get(2)
    bpe = sb.get(w.offset-2) or sb.get(-2)
    return {"code":"S1","name":"ORB Breakdown Sell","strike":w.strike,
            "sell_ce":w.ce_symbol,"sell_pe":w.pe_symbol,
            "buy_ce":bce.ce_symbol if bce else None,
            "buy_pe":bpe.pe_symbol if bpe else None,
            "combined":w.current,
            "reason":f"Strike {w.strike} broke ORB low {w.orb_low:.1f}"}


def _s8(state, t, now):
    if not (dtime(9,22) <= t <= dtime(9,45)): return None
    atm = state.atm
    if not atm: return None
    prev = state.config.get("prev_close", 0)
    if not prev or not state.spot_locked: return None
    gap = abs(state.spot_locked - prev) / prev * 100
    if gap < 0.4 or atm.current < 50: return None
    sb = _sb(state)
    return {"code":"S8","name":"Opening Gap Fade","strike":atm.strike,
            "sell_ce":atm.ce_symbol,"sell_pe":atm.pe_symbol,
            "buy_ce":sb.get(2,atm).ce_symbol,"buy_pe":sb.get(-2,atm).pe_symbol,
            "combined":atm.current,"reason":f"Gap {gap:.2f}% — premium elevated"}


def _s2(state, t, now):
    if not (dtime(9,22) <= t <= dtime(10,30)): return None
    atm = state.atm
    if not atm or len(atm.combined_history) < 10: return None
    if atm.current >= atm.vwap_val: return None
    if atm._ema_count >= 30 and atm.ema75 > atm.vwap_val: return None
    sb = _sb(state)
    return {"code":"S2","name":"VWAP Squeeze","strike":atm.strike,
            "sell_ce":atm.ce_symbol,"sell_pe":atm.pe_symbol,
            "buy_ce":sb.get(2,atm).ce_symbol,"buy_pe":sb.get(-2,atm).pe_symbol,
            "combined":atm.current,
            "reason":f"Below VWAP {atm.vwap_val:.1f} — bearish momentum"}


def _s3(state, t, now):
    if not (dtime(9,22) <= t <= dtime(14,0)): return None
    atm = state.atm
    if not atm or len(atm.combined_history) < 5: return None
    recent_high = max(atm.combined_history[-5:])
    if recent_high < atm.vwap_val * 1.05 or atm.current >= atm.vwap_val: return None
    sb = _sb(state)
    return {"code":"S3","name":"Breakout Reversal","strike":atm.strike,
            "sell_ce":atm.ce_symbol,"sell_pe":atm.pe_symbol,
            "buy_ce":sb.get(1,atm).ce_symbol,"buy_pe":sb.get(-1,atm).pe_symbol,
            "combined":atm.current,
            "reason":f"Spike to {recent_high:.1f} reversed below VWAP"}


def _s4(state, t, now):
    if not (dtime(9,30) <= t <= dtime(10,0)): return None
    atm = state.atm
    if not atm or len(atm.combined_history) < 15: return None
    hist = atm.combined_history[-15:]
    rng = (max(hist)-min(hist)) / atm.current if atm.current else 1
    if rng > 0.08: return None
    sb = _sb(state)
    s1=sb.get(1,atm); sm1=sb.get(-1,atm)
    s3=sb.get(3) or sb.get(2,atm); sm3=sb.get(-3) or sb.get(-2,atm)
    return {"code":"S4","name":"Iron Condor","strike":atm.strike,
            "sell_ce":s1.ce_symbol,"sell_pe":sm1.pe_symbol,
            "buy_ce":s3.ce_symbol,"buy_pe":sm3.pe_symbol,
            "combined":s1.current+sm1.current,
            "reason":f"Range-bound — {rng*100:.1f}% range in 15 candles"}


def _s6(state, t, now):
    if not (dtime(9,45) <= t <= dtime(10,30)): return None
    atm = state.atm
    if not atm or not atm.orb_high: return None
    if atm.current < atm.orb_high * 0.95: return None
    sb = _sb(state)
    s1=sb.get(1,atm); sm1=sb.get(-1,atm)
    s4=sb.get(4) or sb.get(3,atm); sm4=sb.get(-4) or sb.get(-3,atm)
    return {"code":"S6","name":"Theta Decay Strangle","strike":atm.strike,
            "sell_ce":s1.ce_symbol,"sell_pe":sm1.pe_symbol,
            "buy_ce":s4.ce_symbol,"buy_pe":sm4.pe_symbol,
            "combined":s1.current+sm1.current,
            "reason":f"Elevated premium {atm.current:.1f} near ORB high"}


def _s9(state, t, now):
    if now.weekday() != 3: return None
    if not (dtime(11,0) <= t <= dtime(12,0)): return None
    atm = state.atm
    if not atm: return None
    sb = _sb(state)
    return {"code":"S9","name":"Pre-Expiry Theta Crush","strike":atm.strike,
            "sell_ce":atm.ce_symbol,"sell_pe":atm.pe_symbol,
            "buy_ce":sb.get(1,atm).ce_symbol,"buy_pe":sb.get(-1,atm).pe_symbol,
            "combined":atm.current,"reason":"Expiry day — rapid theta decay 11:00-12:00"}


def _s5(state, t, now):
    if not (dtime(9,30) <= t <= dtime(11,0)): return None
    atm = state.atm
    if not atm or atm._ema_count < 20: return None
    if atm.current >= atm.ema75 * 0.95: return None
    sb = _sb(state)
    s3=sb.get(3) or sb.get(2,atm); sm3=sb.get(-3) or sb.get(-2,atm)
    return {"code":"S5","name":"Ratio Spread","strike":atm.strike,
            "sell_ce":atm.ce_symbol,"sell_pe":atm.pe_symbol,
            "buy_ce":s3.ce_symbol,"buy_pe":sm3.pe_symbol,
            "lots_multiplier":2,
            "combined":atm.current,
            "reason":f"Strong downtrend — combined {atm.current:.1f} below EMA {atm.ema75:.1f}"}
