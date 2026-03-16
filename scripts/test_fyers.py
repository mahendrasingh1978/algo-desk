#!/usr/bin/env python3
"""
ALGO-DESK — Fyers Connection & Strategy Test
=============================================
Run this directly on the AWS server to test everything
before integrating into the main application.

Usage:
  python3 test_fyers.py

What it tests:
  1. Exchange auth_code for tokens
  2. Get NIFTY spot price
  3. Get option chain — ATM + 7 strikes
  4. Poll live prices — calculate VWAP + EMA
  5. Run strategy signal checks on live data
  6. Simulate paper orders (no real orders placed)
  7. Test SL logic

Run during market hours: 9:15 AM - 3:30 PM IST weekdays
"""

import asyncio
import hashlib
import json
import os
import time
from datetime import datetime, date
from typing import Optional
import sys

# ── Install deps if missing ───────────────────────────────────
try:
    import httpx
except ImportError:
    os.system("pip3 install httpx --break-system-packages -q")
    import httpx

try:
    import numpy as np
    import pandas as pd
except ImportError:
    os.system("pip3 install numpy pandas --break-system-packages -q")
    import numpy as np
    import pandas as pd

# ═══════════════════════════════════════════════════════════════
# CONFIG — fill these in
# ═══════════════════════════════════════════════════════════════

CLIENT_ID  = os.environ.get("FYERS_CLIENT_ID",  "")   # e.g. FYXXXXX-100
SECRET_KEY = os.environ.get("FYERS_SECRET_KEY", "")
PIN        = os.environ.get("FYERS_PIN",        "")
AUTH_CODE  = os.environ.get("FYERS_AUTH_CODE",  "")   # fresh code from login URL

# Tokens (auto-filled after step 1)
ACCESS_TOKEN  = os.environ.get("FYERS_ACCESS_TOKEN",  "")
REFRESH_TOKEN = os.environ.get("FYERS_REFRESH_TOKEN", "")

API  = "https://api-t1.fyers.in/api/v3"
DATA = "https://api-t1.fyers.in/data"

# ── Test settings ─────────────────────────────────────────────
SYMBOL       = "NSE:NIFTY50-INDEX"
STRIKE_SIDES = 3          # ATM ± 3 = 7 strikes
POLL_SECONDS = 15         # poll every 15 seconds
MAX_POLLS    = 40         # run for ~10 minutes then stop
MIN_PREMIUM  = 50.0       # minimum combined premium to consider

# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def p(msg, kind="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    icons = {"INFO":"  ","OK":"✓ ","WARN":"⚠ ","ERROR":"✕ ","SIGNAL":"★ ",
             "ORDER":"→ ","SL":"🔴","DATA":"  ","HEAD":""}
    print(f"[{ts}] {icons.get(kind,'  ')}{msg}")

def app_hash():
    return hashlib.sha256(f"{CLIENT_ID}:{SECRET_KEY}".encode()).hexdigest()

def auth_header():
    return {"Authorization": f"{CLIENT_ID}:{ACCESS_TOKEN}",
            "Content-Type": "application/json"}

def nearest_strike(spot, gap=50):
    return round(spot / gap) * gap

# ── VWAP ──────────────────────────────────────────────────────
class VWAPCalc:
    def __init__(self):
        self.cum_pv = 0.0
        self.cum_v  = 0.0
    def update(self, price, volume=1.0):
        self.cum_pv += price * volume
        self.cum_v  += volume
        return self.cum_pv / self.cum_v if self.cum_v > 0 else price

# ── EMA ───────────────────────────────────────────────────────
class EMACalc:
    def __init__(self, period=75):
        self.period  = period
        self.k       = 2 / (period + 1)
        self.value   = None
        self.count   = 0
    def update(self, price):
        self.count += 1
        if self.value is None:
            self.value = price
        else:
            self.value = price * self.k + self.value * (1 - self.k)
        return self.value

# ═══════════════════════════════════════════════════════════════
# STEP 1 — AUTH
# ═══════════════════════════════════════════════════════════════

async def step1_auth():
    global ACCESS_TOKEN, REFRESH_TOKEN

    if ACCESS_TOKEN:
        p("Using existing access token from environment", "OK")
        return True

    if not AUTH_CODE:
        p("No auth_code provided.", "WARN")
        p("Get one by opening this URL in your browser:", "INFO")
        if CLIENT_ID:
            url = (f"https://api-t1.fyers.in/api/v3/generate-authcode"
                   f"?client_id={CLIENT_ID}"
                   f"&redirect_uri=https://trade.fyers.in/api-login/redirect-uri/index.html"
                   f"&response_type=code&state=algo_desk")
            p(f"{url}", "INFO")
        p("Then run: FYERS_AUTH_CODE=<code> python3 test_fyers.py", "INFO")
        return False

    if not CLIENT_ID or not SECRET_KEY:
        p("Set FYERS_CLIENT_ID and FYERS_SECRET_KEY environment variables", "ERROR")
        return False

    p("Exchanging auth_code for tokens...", "INFO")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{API}/validate-authcode",
            headers={"Content-Type": "application/json"},
            json={"grant_type": "authorization_code",
                  "appIdHash": app_hash(),
                  "code": AUTH_CODE})
    d = r.json()

    if d.get("s") == "ok":
        ACCESS_TOKEN  = d["access_token"]
        REFRESH_TOKEN = d.get("refresh_token", "")
        p(f"Access token received ({len(ACCESS_TOKEN)} chars)", "OK")
        p(f"Refresh token received ({len(REFRESH_TOKEN)} chars)", "OK")
        p(f"Save these for reuse:", "INFO")
        p(f"  export FYERS_ACCESS_TOKEN={ACCESS_TOKEN}", "INFO")
        p(f"  export FYERS_REFRESH_TOKEN={REFRESH_TOKEN}", "INFO")
        return True
    else:
        p(f"Auth failed: {d.get('message','Unknown error')}", "ERROR")
        if "expired" in str(d.get("message","")).lower():
            p("Auth code expired — get a fresh one (valid ~60 seconds)", "WARN")
        return False

# ═══════════════════════════════════════════════════════════════
# STEP 2 — SPOT PRICE
# ═══════════════════════════════════════════════════════════════

async def get_spot() -> Optional[float]:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{API}/quotes",
            headers=auth_header(),
            params={"symbols": SYMBOL})
    d = r.json()
    if d.get("s") == "ok" and d.get("d"):
        ltp = float(d["d"][0]["v"]["lp"])
        return ltp
    p(f"Spot price error: {d.get('message','')}", "ERROR")
    return None

# ═══════════════════════════════════════════════════════════════
# STEP 3 — OPTION CHAIN
# ═══════════════════════════════════════════════════════════════

async def get_option_chain(spot: float) -> dict:
    """
    Gets option chain and returns structured data.
    Symbols come directly from API — no manual construction.
    """
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{API}/option-chain",
            headers=auth_header(),
            params={"symbol": SYMBOL, "strikecount": STRIKE_SIDES * 2 + 2})
    d = r.json()

    if d.get("s") != "ok":
        p(f"Option chain error: {d.get('message','')}", "ERROR")
        return {}

    atm = nearest_strike(spot)
    chain = {}

    for row in d.get("data", {}).get("optionChain", []):
        strike   = int(row.get("strikePrice", 0))
        opt_type = row.get("option_type", "")
        ltp      = float(row.get("ltp", 0))
        sym      = row.get("symbol", "")
        vol      = float(row.get("volume", 0))
        oi       = float(row.get("oi", 0))

        # Only keep strikes within our range
        if abs(strike - atm) > STRIKE_SIDES * 50:
            continue

        if strike not in chain:
            chain[strike] = {
                "strike": strike,
                "offset": (strike - atm) // 50,
                "ce_ltp": 0, "pe_ltp": 0,
                "ce_symbol": "", "pe_symbol": "",
                "ce_vol": 0, "pe_vol": 0,
                "combined": 0,
            }

        if opt_type == "CE":
            chain[strike]["ce_ltp"]    = ltp
            chain[strike]["ce_symbol"] = sym
            chain[strike]["ce_vol"]    = vol
        elif opt_type == "PE":
            chain[strike]["pe_ltp"]    = ltp
            chain[strike]["pe_symbol"] = sym
            chain[strike]["pe_vol"]    = vol

    # Calculate combined premium per strike
    for st in chain.values():
        st["combined"] = st["ce_ltp"] + st["pe_ltp"]

    return chain

# ═══════════════════════════════════════════════════════════════
# STRATEGY SIGNALS
# ═══════════════════════════════════════════════════════════════

def check_s7(strikes, atm, enabled_strategies):
    """S7: All 7 strikes break ORB low simultaneously."""
    if "S7" not in enabled_strategies:
        return None
    broken = [s for s in strikes.values()
              if s.get("orb_low", 0) > 0 and s["combined"] < s["orb_low"] * 0.98]
    if len(broken) >= len(strikes) and len(broken) >= 5:
        atm_data = strikes.get(atm, {})
        return {
            "code": "S7", "name": "All-Strike Iron Butterfly",
            "strike": atm,
            "sell_ce": atm_data.get("ce_symbol"),
            "sell_pe": atm_data.get("pe_symbol"),
            "buy_ce": strikes.get(atm + 150, {}).get("ce_symbol"),
            "buy_pe": strikes.get(atm - 150, {}).get("pe_symbol"),
            "combined": atm_data.get("combined", 0),
            "reason": f"All {len(broken)} strikes broke ORB low",
        }
    return None

def check_s1(strikes, atm, enabled_strategies):
    """S1: Any strike breaks ORB low."""
    if "S1" not in enabled_strategies:
        return None
    candidates = [s for s in strikes.values()
                  if s.get("orb_low", 0) > 0
                  and s["combined"] < s["orb_low"]
                  and not s.get("fired", False)]
    if not candidates:
        return None
    # Pick ATM first, then by offset
    winner = sorted(candidates, key=lambda s: abs(s["offset"]))[0]
    winner["fired"] = True
    return {
        "code": "S1", "name": "ORB Breakdown Sell",
        "strike": winner["strike"],
        "sell_ce": winner["ce_symbol"],
        "sell_pe": winner["pe_symbol"],
        "buy_ce": strikes.get(winner["strike"] + 100, {}).get("ce_symbol"),
        "buy_pe": strikes.get(winner["strike"] - 100, {}).get("pe_symbol"),
        "combined": winner["combined"],
        "reason": f"Strike {winner['strike']} broke ORB low {winner['orb_low']:.1f}",
    }

def check_s8(strikes, atm, spot, prev_close, enabled_strategies, now):
    """S8: Opening gap fade."""
    if "S8" not in enabled_strategies or prev_close == 0:
        return None
    t = now.time()
    from datetime import time as dtime
    if not (dtime(9,22) <= t <= dtime(9,45)):
        return None
    gap_pct = abs(spot - prev_close) / prev_close * 100
    atm_data = strikes.get(atm, {})
    if gap_pct > 0.4 and atm_data.get("combined", 0) > MIN_PREMIUM:
        return {
            "code": "S8", "name": "Opening Gap Fade",
            "strike": atm,
            "sell_ce": atm_data.get("ce_symbol"),
            "sell_pe": atm_data.get("pe_symbol"),
            "buy_ce": strikes.get(atm + 100, {}).get("ce_symbol"),
            "buy_pe": strikes.get(atm - 100, {}).get("pe_symbol"),
            "combined": atm_data.get("combined", 0),
            "reason": f"Gap {gap_pct:.2f}% from prev close",
        }
    return None

def check_sl(position, current_combined, vwap, ema75):
    """Returns exit reason or None."""
    if not position:
        return None
    entry = position["entry_combined"]
    # Hard SL: 150% of entry
    if current_combined > entry * 1.5:
        return "HARD_SL"
    # Profit target: 50% decay
    if current_combined <= entry * 0.5:
        return "PROFIT_TARGET"
    # VWAP SL
    if vwap > 0 and current_combined > vwap * 1.05:
        return "VWAP_SL"
    # EMA75 SL (once we have enough data)
    if ema75 > 0 and ema75 < vwap and current_combined > ema75 * 1.01:
        return "EMA_SL"
    return None

# ═══════════════════════════════════════════════════════════════
# MAIN TEST LOOP
# ═══════════════════════════════════════════════════════════════

async def run_test():
    p("=" * 60, "HEAD")
    p("ALGO-DESK — Fyers Live Test", "HEAD")
    p(f"Date: {date.today()}  Time: {datetime.now().strftime('%H:%M:%S')}", "HEAD")
    p("=" * 60, "HEAD")

    # Step 1: Auth
    p("\n── Step 1: Authentication ──────────────────────────", "HEAD")
    if not await step1_auth():
        return

    # Step 2: Initial spot price
    p("\n── Step 2: Spot Price ──────────────────────────────", "HEAD")
    spot = await get_spot()
    if not spot:
        p("Cannot get spot price. Check token validity.", "ERROR")
        return
    p(f"NIFTY spot: ₹{spot:,.1f}", "OK")

    atm = nearest_strike(spot)
    p(f"ATM strike: {atm}", "OK")

    # Step 3: Option chain
    p("\n── Step 3: Option Chain ────────────────────────────", "HEAD")
    chain = await get_option_chain(spot)
    if not chain:
        p("Cannot get option chain. Market may be closed.", "WARN")
        p("Option chain only available during market hours (9:15-15:30 IST)", "INFO")
        p("Continuing with mock data for strategy logic testing...", "INFO")
        # Create mock data for testing strategy logic
        for i in range(-STRIKE_SIDES, STRIKE_SIDES + 1):
            strike = atm + i * 50
            chain[strike] = {
                "strike": strike, "offset": i,
                "ce_ltp": max(5, 150 - abs(i) * 40 + (5 - abs(i)) * 10),
                "pe_ltp": max(5, 150 - abs(i) * 40 + abs(i) * 10),
                "ce_symbol": f"NSE:NIFTY_MOCK_{strike}CE",
                "pe_symbol": f"NSE:NIFTY_MOCK_{strike}PE",
                "ce_vol": 1000, "pe_vol": 1000,
                "combined": 0,
            }
            chain[strike]["combined"] = chain[strike]["ce_ltp"] + chain[strike]["pe_ltp"]

    p(f"Got {len(chain)} strikes from option chain", "OK")
    p(f"\n{'Strike':>8} {'CE LTP':>8} {'PE LTP':>8} {'Combined':>10} {'Symbol (CE)':>30}", "DATA")
    p("-" * 70, "DATA")
    for strike in sorted(chain.keys()):
        s = chain[strike]
        marker = " ← ATM" if strike == atm else ""
        p(f"{strike:>8} {s['ce_ltp']:>8.1f} {s['pe_ltp']:>8.1f} "
          f"{s['combined']:>10.1f} {s.get('ce_symbol','N/A'):>30}{marker}", "DATA")

    # Step 4: ORB window simulation
    p("\n── Step 4: ORB Window (simulating 6 candles) ───────", "HEAD")
    p("In live trading this builds from 9:15–9:21 AM.", "INFO")
    p("Simulating ORB with current prices as reference...", "INFO")

    # Set ORB values using current data
    for s in chain.values():
        s["orb_high"] = s["combined"] * 1.02  # simulate high
        s["orb_low"]  = s["combined"] * 0.98  # simulate low
        s["fired"]    = False

    atm_data = chain.get(atm, {})
    p(f"ATM {atm}: Combined={atm_data.get('combined',0):.1f} "
      f"ORB High={atm_data.get('orb_high',0):.1f} "
      f"ORB Low={atm_data.get('orb_low',0):.1f}", "OK")

    # Step 5: VWAP + EMA tracking
    p("\n── Step 5: Live Polling — VWAP + EMA ──────────────", "HEAD")
    p(f"Polling every {POLL_SECONDS}s for {MAX_POLLS} iterations "
      f"(~{MAX_POLLS * POLL_SECONDS // 60} mins). Ctrl+C to stop.", "INFO")

    vwap_calc = VWAPCalc()
    ema_calc  = EMACalc(75)
    position  = None
    prev_close = spot * 0.998  # approximate
    trades    = []
    poll_count = 0

    enabled_strategies = ["S7", "S1", "S8", "S2", "S3", "S4", "S6", "S9"]

    try:
        while poll_count < MAX_POLLS:
            poll_count += 1
            now = datetime.now()

            # Get fresh spot
            fresh_spot = await get_spot()
            if fresh_spot:
                spot = fresh_spot

            # Get fresh chain
            fresh_chain = await get_option_chain(spot)
            if fresh_chain:
                chain.update(fresh_chain)

            atm_data = chain.get(atm, {})
            combined = atm_data.get("combined", 0)

            # Update VWAP + EMA
            vwap_val = vwap_calc.update(combined)
            ema_val  = ema_calc.update(combined)

            p(f"\n[Poll {poll_count}/{MAX_POLLS}] Spot=₹{spot:,.1f} "
              f"ATM={atm} Combined={combined:.1f} "
              f"VWAP={vwap_val:.1f} EMA75={ema_val:.1f}", "DATA")

            # Check SL on open position
            if position:
                sl_reason = check_sl(position, combined, vwap_val, ema_val)
                if sl_reason:
                    p(f"SL triggered: {sl_reason}", "SL")
                    pnl = (position["entry_combined"] - combined) * 25
                    p(f"CLOSE position. Entry={position['entry_combined']:.1f} "
                      f"Exit={combined:.1f} PnL=₹{pnl:.0f}", "ORDER")
                    trades.append({**position, "exit": combined,
                                   "pnl": pnl, "reason": sl_reason})
                    position = None
                else:
                    pnl = (position["entry_combined"] - combined) * 25
                    p(f"Position open: Entry={position['entry_combined']:.1f} "
                      f"Current={combined:.1f} PnL=₹{pnl:.0f}", "INFO")

            # Check strategies (only after ORB complete)
            elif poll_count >= 2:
                signal = None

                # Check S7 first
                signal = check_s7(chain, atm, enabled_strategies)

                # Then S1
                if not signal:
                    signal = check_s1(chain, atm, enabled_strategies)

                # Then S8
                if not signal:
                    signal = check_s8(chain, atm, spot, prev_close,
                                     enabled_strategies, now)

                if signal:
                    p(f"SIGNAL FIRED: [{signal['code']}] {signal['name']}", "SIGNAL")
                    p(f"  Strike: {signal['strike']}", "SIGNAL")
                    p(f"  Reason: {signal['reason']}", "SIGNAL")
                    p(f"  Combined premium: {signal['combined']:.1f}", "SIGNAL")
                    p(f"  Sell CE: {signal['sell_ce']}", "SIGNAL")
                    p(f"  Sell PE: {signal['sell_pe']}", "SIGNAL")
                    if signal.get("buy_ce"):
                        p(f"  Hedge CE: {signal['buy_ce']}", "SIGNAL")
                        p(f"  Hedge PE: {signal['buy_pe']}", "SIGNAL")
                    p(f"  [PAPER] Orders logged — no real orders placed", "ORDER")
                    position = {
                        "signal": signal,
                        "strategy": signal["code"],
                        "entry_combined": combined,
                        "entry_time": now.isoformat(),
                    }
                else:
                    p(f"  No signal. Monitoring...", "INFO")

            # Print strike table every 4 polls
            if poll_count % 4 == 0:
                p(f"\n  {'Strike':>8} {'Combined':>10} {'ORB Low':>10} {'Status':>12}", "DATA")
                for strike in sorted(chain.keys()):
                    s = chain[strike]
                    orb_low = s.get("orb_low", 0)
                    status = "BELOW ORB" if (orb_low > 0 and s["combined"] < orb_low) else "Above ORB"
                    marker = " ←ATM" if strike == atm else ""
                    p(f"  {strike:>8} {s['combined']:>10.1f} "
                      f"{orb_low:>10.1f} {status:>12}{marker}", "DATA")

            await asyncio.sleep(POLL_SECONDS)

    except KeyboardInterrupt:
        p("\nStopped by user.", "INFO")

    # Summary
    p("\n" + "=" * 60, "HEAD")
    p("TEST SUMMARY", "HEAD")
    p("=" * 60, "HEAD")
    p(f"Polls completed: {poll_count}", "INFO")
    p(f"Trades logged:   {len(trades)}", "INFO")
    if trades:
        total_pnl = sum(t.get("pnl", 0) for t in trades)
        p(f"Total PnL:       ₹{total_pnl:.0f}", "OK" if total_pnl > 0 else "WARN")
        for i, t in enumerate(trades, 1):
            p(f"  Trade {i}: {t['strategy']} | "
              f"Entry={t['entry_combined']:.1f} "
              f"Exit={t.get('exit', 0):.1f} "
              f"PnL=₹{t.get('pnl', 0):.0f} "
              f"Reason={t.get('reason', '')}", "INFO")
    if position:
        p(f"Open position: {position['strategy']} "
          f"entry={position['entry_combined']:.1f}", "WARN")

    p("\nWhat to check:", "INFO")
    p("  ✓ Did option chain return real symbols?", "INFO")
    p("  ✓ Are the combined premiums realistic (50-300 range)?", "INFO")
    p("  ✓ Did VWAP and EMA update correctly each poll?", "INFO")
    p("  ✓ Did any strategy signals fire? Were they sensible?", "INFO")
    p("  ✓ Did SL logic trigger correctly?", "INFO")

if __name__ == "__main__":
    # Prompt for missing config
    if not CLIENT_ID:
        CLIENT_ID = input("Enter Fyers Client ID (FYXXXXX-100): ").strip()
    if not SECRET_KEY:
        import getpass
        SECRET_KEY = getpass.getpass("Enter Secret Key: ").strip()
    if not PIN:
        PIN = getpass.getpass("Enter PIN: ").strip()

    if not ACCESS_TOKEN and not AUTH_CODE:
        print(f"\nOpen this URL in your browser to get auth_code:")
        print(f"https://api-t1.fyers.in/api/v3/generate-authcode"
              f"?client_id={CLIENT_ID}"
              f"&redirect_uri=https://trade.fyers.in/api-login/redirect-uri/index.html"
              f"&response_type=code&state=algo_desk\n")
        AUTH_CODE = input("Paste auth_code from redirect URL: ").strip()

    asyncio.run(run_test())
