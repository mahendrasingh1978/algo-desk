"""
ALGO-DESK v5 — Complete Backend
================================
All endpoints are real. State shared across pages.
Token refreshed on every call — matches N8N approach.
"""

import os, secrets, hashlib, logging, asyncio, json
import pytz
from datetime import datetime, timedelta
from typing import Optional

import bcrypt
from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from jose import JWTError, jwt
from pydantic import BaseModel
from dotenv import load_dotenv
from sqlalchemy import create_engine, text, func
from sqlalchemy.orm import sessionmaker, Session

from models import Base, User, BrokerConnection, BrokerDefinition, Automation, Trade, ShadowTrade, ResetToken, InviteLink, TradingEvent, ClaudeAssessment, ServerSettings, run_migrations
from fyers import FyersConnection, encrypt, decrypt
from engine import EngineState, StrikeState, check_all_strategies, check_sl, nearest_strike, get_position_size
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# Per-user shadow engine states (paper simulation)
shadow_engines: dict = {}  # user_id -> {auto_id -> EngineState}

# Available symbols per broker (fetched from broker on connect)
# Populated with defaults + any fetched from broker API
NIFTY_SYMBOLS = [
    {"value": "NSE:NIFTY50-INDEX",    "label": "NIFTY 50"},
    {"value": "NSE:NIFTYBANK-INDEX",  "label": "BANK NIFTY"},
    {"value": "NSE:FINNIFTY-INDEX",   "label": "FINNIFTY"},
    {"value": "NSE:MIDCPNIFTY-INDEX", "label": "MIDCAP NIFTY"},
    {"value": "BSE:SENSEX-INDEX",     "label": "SENSEX"},
    {"value": "NSE:NIFTYIT-INDEX",    "label": "NIFTY IT"},
    {"value": "NSE:NIFTYPHARMA-INDEX","label": "NIFTY PHARMA"},
]

# ── Symbol registry — current lot sizes as of Jan 2026 ──────────
# Source: NSE Circular 176/2025, effective Jan 2026
# NIFTY: 75→65, BANKNIFTY: 35→30, FINNIFTY: 65→60, MIDCPNIFTY: 140→120
# SENSEX: 20 (BSE, unchanged)
SYMBOL_REGISTRY = {
    "NSE:NIFTY50-INDEX":    {"lot_size": 65,  "label": "NIFTY 50",    "strike_gap": 50},
    "NSE:NIFTYBANK-INDEX":  {"lot_size": 30,  "label": "BANK NIFTY",  "strike_gap": 100},
    "NSE:FINNIFTY-INDEX":   {"lot_size": 60,  "label": "FINNIFTY",    "strike_gap": 50},
    "NSE:MIDCPNIFTY-INDEX": {"lot_size": 120, "label": "MIDCAP NIFTY","strike_gap": 25},
    "BSE:SENSEX-INDEX":     {"lot_size": 20,  "label": "SENSEX",      "strike_gap": 100},
    "NSE:NIFTYNXT50-INDEX": {"lot_size": 25,  "label": "NIFTY NEXT 50","strike_gap": 50},
}

# -- Google Gemini AI -- per-user API key support
# Uses google-genai SDK (new stable SDK, pip install google-genai)
try:
    from google import genai as _genai
    _GENAI_AVAILABLE = True
except ImportError:
    _genai = None
    _GENAI_AVAILABLE = False

_GEMINI_MODEL  = "gemini-2.5-flash"   # confirmed working — tested against API
_GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.5-pro"]

def _simple_encrypt(text: str) -> str:
    """Base64 encode API key for storage."""
    import base64
    return base64.b64encode(text.encode()).decode()

def _simple_decrypt(encoded: str) -> str:
    import base64
    return base64.b64decode(encoded.encode()).decode()

def _get_gemini_client(user_ai_config: dict):
    """Return (client, enabled). Uses per-user Gemini key or server env key.
    Returns a google.genai.Client — use client.models.generate_content(model=..., contents=...)
    """
    if not _GENAI_AVAILABLE:
        return None, False
    key_enc = (user_ai_config or {}).get("api_key_enc", "")
    key = ""
    if key_enc:
        try:
            key = _simple_decrypt(key_enc)
        except Exception:
            pass
    if not key:
        key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        return None, False
    try:
        client = _genai.Client(api_key=key)
        return client, True
    except Exception:
        return None, False

# Legacy alias so engine gate code still works
def _get_claude_client(user_ai_config: dict):
    return _get_gemini_client(user_ai_config)


def calc_brokerage(lots: int, lot_size: int, 
                   entry_combined: float, exit_combined: float) -> dict:
    """
    Calculate real Fyers brokerage for a 4-leg Iron Fly/Condor.
    
    Fyers charges (F&O options):
    - Brokerage:      ₹20 flat per executed order (4 legs × 2 sides = 8 orders)
    - STT:            0.1% of premium × qty on SELL side only (at exercise/expiry)
                      For intraday close: STT on sell premium
    - Exchange fee:   0.05% of premium × qty (NSE) or 0.05% (BSE)
    - SEBI charges:   ₹10 per crore of turnover
    - Stamp duty:     0.003% of buy premium (Maharashtra)
    - GST:            18% on (brokerage + exchange fee + SEBI)
    
    Total realistic cost for 1 NIFTY lot Iron Fly: ~₹150-250 per round trip
    """
    qty = lots * lot_size
    
    # Entry: 2 sell legs + 2 buy legs = 4 orders
    # Exit:  2 buy legs + 2 sell legs = 4 orders
    # Total: 8 orders at ₹20 each
    brokerage = 20 * 8  # ₹160 for 8 orders
    
    # Exchange transaction fee: 0.05% of premium turnover
    # Turnover = (entry_combined + exit_combined) × qty
    turnover = (entry_combined + exit_combined) * qty
    exchange_fee = turnover * 0.0005
    
    # STT: 0.1% × premium × qty on sell legs at exit (not collected at intraday close by NSE)
    # For closed positions before expiry: STT = 0 on options (only on exercise)
    stt = 0  # Zero for squared-off options positions (SEBI circular)
    
    # SEBI charges: ₹10 per crore = 0.0001% of turnover
    sebi = turnover * 0.000001
    
    # Stamp duty: 0.003% of buy side premium only
    buy_premium = (exit_combined) * qty  # buying back the short
    stamp = buy_premium * 0.00003
    
    # GST: 18% on (brokerage + exchange_fee + sebi)
    gst = (brokerage + exchange_fee + sebi) * 0.18
    
    total = brokerage + exchange_fee + stt + sebi + stamp + gst
    
    return {
        "brokerage":    round(brokerage, 2),
        "exchange_fee": round(exchange_fee, 2),
        "stt":          round(stt, 2),
        "sebi":         round(sebi, 2),
        "stamp":        round(stamp, 2),
        "gst":          round(gst, 2),
        "total":        round(total, 2),
    }

# Margin multipliers per strategy structure (Iron Fly vs Condor)
# Based on SPAN margin calculation approximations
# Naked short: ~12-15% of notional. Hedged: ~4-6% of notional
HEDGE_MARGIN_PCT = {
    1: 0.04,   # ±1 tight Iron Fly — tightest hedge, lowest margin
    2: 0.05,   # ±2 standard Iron Fly
    3: 0.045,  # ±3 Iron Condor — margin benefit from wider hedge
    4: 0.04,   # ±4 Wide Condor — widest hedge
   20: 0.035,  # ±1000pt OTM hedge — near max margin relief (hedge cost ≈ ₹0)
    0: 0.06,   # default / no hedge specified
}

# Strategy-specific margin config
# Each strategy has: hedge_width, structure, typical_iv_pct, premium_pct_of_spot
STRATEGY_MARGIN_CONFIG = {
    # hedge widths match engine auto_hedges — used for accurate SPAN margin estimates
    "S1": {"hedge":  2, "structure": "Iron Fly (±100pt)",        "premium_pct": 0.014, "label": "ORB Breakdown"},
    "S2": {"hedge":  2, "structure": "Iron Fly (±100pt)",        "premium_pct": 0.013, "label": "VWAP Squeeze"},
    "S3": {"hedge":  2, "structure": "Iron Fly (±100pt)",        "premium_pct": 0.012, "label": "Breakout Rev."},
    "S4": {"hedge": 20, "structure": "Iron Condor (±1000pt)",    "premium_pct": 0.009, "label": "Iron Condor"},
    "S5": {"hedge":  2, "structure": "Ratio Spread (±100pt)",    "premium_pct": 0.018, "label": "Ratio Spread"},
    "S6": {"hedge":  4, "structure": "Wide Condor (±200pt)",     "premium_pct": 0.011, "label": "Theta Strangle"},
    "S7": {"hedge":  2, "structure": "Iron Fly (±100pt)",        "premium_pct": 0.014, "label": "All-Strike Fly"},
    "S8": {"hedge":  3, "structure": "Iron Condor (±150pt)",     "premium_pct": 0.010, "label": "Gap Fade"},
    "S9": {"hedge":  1, "structure": "Tight Iron Fly (±50pt)",   "premium_pct": 0.008, "label": "Expiry Crush"},
}

def estimate_margin(symbol: str, lots: int, lot_size: int,
                    hedge_width: int, spot_price: float,
                    strategy_code: str = None) -> dict:
    """
    Estimate SPAN margin and per-leg P&L for an Iron Fly/Condor.

    Per-leg breakdown:
    - Sell CE (ATM): collects premium, uses margin
    - Sell PE (ATM): collects premium, uses margin
    - Buy CE hedge:  pays premium, provides margin relief + defines max loss
    - Buy PE hedge:  pays premium, provides margin relief + defines max loss

    SPAN margin logic:
    - Sell legs drive margin requirement
    - Buy legs give ~60% margin relief each
    - Net margin ≈ spot × lot × 5% × 2 sell legs × hedge_relief_factor
    """
    reg = SYMBOL_REGISTRY.get(symbol, {})
    actual_lot  = lot_size or reg.get("lot_size", 65)
    gap         = reg.get("strike_gap", 50)
    margin_pct  = HEDGE_MARGIN_PCT.get(hedge_width, 0.05)

    # Use strategy config if provided
    if strategy_code and strategy_code in STRATEGY_MARGIN_CONFIG:
        sc = STRATEGY_MARGIN_CONFIG[strategy_code]
        hedge_width  = sc["hedge"]
        margin_pct   = HEDGE_MARGIN_PCT.get(hedge_width, 0.05)
        premium_pct  = sc["premium_pct"]
        structure    = sc["structure"]
    else:
        premium_pct  = 0.007 * 2   # ~0.7% per ATM leg × 2 legs
        structure    = {1:"Iron Fly",2:"Iron Fly",3:"Iron Condor",4:"Wide Condor"}.get(hedge_width,"Iron Fly")

    qty = actual_lot * lots

    # ── Per-leg estimates ──────────────────────────────────────
    # ATM premium (sell legs): typically 0.6-0.9% of spot per leg
    atm_premium_per_leg  = spot_price * (premium_pct / 2)
    # OTM hedge premium: typically 20-35% of ATM premium per leg
    # OTM hedge premium as a fraction of ATM premium per leg:
    # ±1/2 tight fly: 28% of ATM  ±3 condor: 22%  ±4-5: 18%
    # ±10+ very OTM (e.g. ±1000pt): ~3% — real premium nearly ₹0
    hedge_pct = 0.28 if hedge_width <= 2 else 0.22 if hedge_width == 3 else 0.18 if hedge_width <= 5 else 0.03
    hedge_premium_per_leg = atm_premium_per_leg * hedge_pct

    # Total premium collected (net)
    premium_collected = (atm_premium_per_leg * 2 - hedge_premium_per_leg * 2) * qty
    # Gross premium (sell legs only)
    gross_premium = atm_premium_per_leg * 2 * qty
    # Hedge cost
    hedge_cost = hedge_premium_per_leg * 2 * qty

    # ── SPAN margin ────────────────────────────────────────────
    # Sell CE margin
    sell_ce_margin = spot_price * actual_lot * lots * margin_pct
    # Sell PE margin (same structure)
    sell_pe_margin = spot_price * actual_lot * lots * margin_pct
    # Hedge relief: each buy leg reduces margin by ~60% of its notional
    hedge_relief = hedge_premium_per_leg * qty * 0.6 * 2
    gross_margin = sell_ce_margin + sell_pe_margin
    net_margin   = max(gross_margin - hedge_relief, gross_margin * 0.55)

    # ── Max loss / max profit ─────────────────────────────────
    # Max profit = net premium collected (if both legs expire worthless)
    max_profit = premium_collected
    # Max loss = (hedge_width × gap × qty) − premium_collected
    # e.g. ±2 on NIFTY = 100pt risk × 65 qty × lots − premium
    max_loss = (hedge_width * gap * qty) - premium_collected

    # ── Break-even points ─────────────────────────────────────
    net_premium_per_unit = premium_collected / qty if qty else 0
    be_upper = spot_price + net_premium_per_unit
    be_lower = spot_price - net_premium_per_unit

    # ── Profit target (50% decay default) ────────────────────
    profit_at_50pct = premium_collected * 0.50

    legs = [
        {"leg": "Sell CE (ATM)",
         "action": "SELL", "type": "credit",
         "est_premium": round(atm_premium_per_leg, 1),
         "qty": qty,
         "value": round(atm_premium_per_leg * qty, 0),
         "note": f"ATM call — collect ₹{atm_premium_per_leg:.0f}/unit"},
        {"leg": "Sell PE (ATM)",
         "action": "SELL", "type": "credit",
         "est_premium": round(atm_premium_per_leg, 1),
         "qty": qty,
         "value": round(atm_premium_per_leg * qty, 0),
         "note": f"ATM put — collect ₹{atm_premium_per_leg:.0f}/unit"},
        {"leg": f"Buy CE (+{hedge_width} strike)",
         "action": "BUY", "type": "debit",
         "est_premium": round(hedge_premium_per_leg, 1),
         "qty": qty,
         "value": round(hedge_premium_per_leg * qty, 0),
         "note": f"OTM call hedge — pay ₹{hedge_premium_per_leg:.0f}/unit"},
        {"leg": f"Buy PE (-{hedge_width} strike)",
         "action": "BUY", "type": "debit",
         "est_premium": round(hedge_premium_per_leg, 1),
         "qty": qty,
         "value": round(hedge_premium_per_leg * qty, 0),
         "note": f"OTM put hedge — pay ₹{hedge_premium_per_leg:.0f}/unit"},
    ]

    return {
        "symbol":           symbol,
        "label":            reg.get("label", symbol),
        "structure":        structure,
        "spot":             round(spot_price, 1),
        "lot_size":         actual_lot,
        "lots":             lots,
        "qty":              qty,
        "hedge_width":      hedge_width,
        "strike_gap":       gap,
        # Margin
        "gross_margin":     round(gross_margin, 0),
        "hedge_relief":     round(hedge_relief, 0),
        "net_required":     round(net_margin, 0),
        "per_lot":          round(net_margin / lots if lots else 0, 0),
        # Premium
        "gross_premium":    round(gross_premium, 0),
        "hedge_cost":       round(hedge_cost, 0),
        "net_premium":      round(premium_collected, 0),
        "net_per_unit":     round(net_premium_per_unit, 1),
        # P&L
        "max_profit":       round(max_profit, 0),
        "max_loss":         round(max_loss, 0),
        "profit_at_50pct":  round(profit_at_50pct, 0),
        "be_upper":         round(be_upper, 0),
        "be_lower":         round(be_lower, 0),
        # Legs
        "legs":             legs,
        "note": "Estimated using typical ATM IV. Use Fyers SPAN calculator for exact margin.",
    }

# ── Plan/tier definitions ─────────────────────────────────────────
# FREE:    Paper trading only. All 9 strategies in paper mode.
#          Broker connection for data only (no live orders).
# STARTER: Live trading. Strategies S1, S2, S3, S8.
#          Up to 2 automations.
# PRO:     Live trading. All 9 strategies.
#          Up to 10 automations. Priority support.
# Note: Admin/SUPER_ADMIN always get PRO access.

PLAN_CONFIG = {
    "FREE": {
        "live_trading":   False,
        "strategies":     ["S1","S2","S3","S4","S5","S6","S7","S8","S9","S10"],  # all in paper
        "max_automations": 3,
        "shadow_mode":    True,
        "label":          "Free",
        "description":    "Paper trading · All 10 strategies simulated · No live orders",
    },
    "PRO": {
        "live_trading":   True,
        "strategies":     ["S1","S2","S3","S4","S5","S6","S7","S8","S9","S10"],
        "max_automations": 10,
        "shadow_mode":    True,
        "label":          "Pro",
        "description":    "Live trading · All 10 strategies · 10 automations · ₹5,000/mo",
    },
    # Legacy alias — existing STARTER users get full PRO access
    "STARTER": {
        "live_trading":   True,
        "strategies":     ["S1","S2","S3","S4","S5","S6","S7","S8","S9","S10"],
        "max_automations": 10,
        "shadow_mode":    True,
        "label":          "Pro",
        "description":    "Live trading · All 10 strategies · 10 automations · ₹5,000/mo",
    },
}

def get_plan(user) -> dict:
    """Get effective plan — admins always get PRO."""
    if user.role in ("SUPER_ADMIN", "ADMIN"):
        return PLAN_CONFIG["PRO"]
    return PLAN_CONFIG.get(user.plan, PLAN_CONFIG["FREE"])

def check_plan_can_live(user) -> bool:
    return get_plan(user).get("live_trading", False)

def check_plan_strategy(user, strategy_code: str) -> bool:
    return strategy_code in get_plan(user).get("strategies", [])

# Per-user market data cache
# Each user with a connected broker gets their own live feed entry.
# user_id -> {"spot":float, "atm":int, "chain":dict, "status":str, "message":str}
user_market_cache: dict = {}

# Per-user, per-symbol cache for multi-symbol support
# user_id -> {symbol -> {"spot":float,"atm":int,"chain":dict,"updated":str,"status":str}}
user_symbol_cache: dict = {}

# Per-user funds cache — persists last successful balance fetch so it can be shown outside hours
# user_id -> {"available_balance":float, "funds":dict, "matched_key":str, "cached_at":str, "mode":str}
_funds_cache: dict = {}

# ── Email (SMTP) ─────────────────────────────────────────────

def _resolve_domain(email_cfg: dict, request: Request = None) -> str:
    """Priority: email config app_domain → request Host header → APP_DOMAIN env → fallback IP."""
    if email_cfg.get("app_domain"):
        return email_cfg["app_domain"].strip()
    if request:
        host = request.headers.get("host", "").split(":")[0]
        if host and host not in ("localhost", "127.0.0.1", "0.0.0.0"):
            return host
    return os.environ.get("APP_DOMAIN", "35.87.77.90")

def _get_email_config(db) -> dict:
    """Fetch email config from server_settings. Returns {} if not configured."""
    row = db.query(ServerSettings).filter(ServerSettings.key == "email_config").first()
    if not row or not row.value:
        return {}
    return row.value

def _send_email(cfg: dict, to_email: str, subject: str, html_body: str) -> tuple[bool, str]:
    """
    Send an email using stored SMTP config.
    Returns (success: bool, error_message: str).
    """
    if not cfg.get("enabled"):
        return False, "Email not configured"
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"{cfg.get('from_name','AlgoDesk')} <{cfg.get('from_email',cfg.get('smtp_user',''))}>"
        msg["To"]      = to_email
        msg.attach(MIMEText(html_body, "html"))

        host = cfg.get("smtp_host", "smtp.gmail.com")
        port = int(cfg.get("smtp_port", 587))
        user = cfg.get("smtp_user", "")
        pwd  = decrypt("_server", cfg["smtp_password_enc"]) if cfg.get("smtp_password_enc") else cfg.get("smtp_password", "")

        from_addr = cfg.get("from_email", user)
        with smtplib.SMTP(host, port, timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.login(user, pwd)
            server.sendmail(from_addr, [to_email], msg.as_string())
        logger.info(f"Email sent to {to_email} | subject: {subject}")
        return True, ""
    except Exception as e:
        logger.error(f"Email send failed to {to_email}: {e}")
        return False, str(e)

def _email_welcome(cfg: dict, to_email: str, name: str, temp_password: str, login_url: str) -> tuple:
    """Send welcome email with temp password."""
    subject = "Welcome to AlgoDesk — Your account is ready"
    html = f"""
    <div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:24px">
      <h2 style="color:#4f8ef7">Welcome to AlgoDesk, {name}!</h2>
      <p>Your account has been created. Use the credentials below to log in:</p>
      <div style="background:#f4f4f4;border-radius:8px;padding:16px;margin:16px 0">
        <div><strong>Email:</strong> {to_email}</div>
        <div style="margin-top:8px"><strong>Temporary Password:</strong> <code style="background:#e0e0e0;padding:2px 6px;border-radius:4px">{temp_password}</code></div>
      </div>
      <p style="color:#e55">You will be asked to set a new password when you first log in.</p>
      <a href="{login_url}" style="display:inline-block;background:#4f8ef7;color:#fff;padding:10px 24px;border-radius:6px;text-decoration:none;margin-top:8px">Log in to AlgoDesk →</a>
      <p style="margin-top:24px;font-size:12px;color:#999">If you did not expect this email, you can ignore it.</p>
    </div>
    """
    return _send_email(cfg, to_email, subject, html)

def _email_reset_link(cfg: dict, to_email: str, name: str, reset_url: str):
    """Send password reset link email."""
    subject = "AlgoDesk — Reset your password"
    html = f"""
    <div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:24px">
      <h2 style="color:#4f8ef7">Reset your AlgoDesk password</h2>
      <p>Hi {name}, we received a request to reset your password.</p>
      <a href="{reset_url}" style="display:inline-block;background:#4f8ef7;color:#fff;padding:10px 24px;border-radius:6px;text-decoration:none;margin:16px 0">Reset Password →</a>
      <p style="font-size:12px;color:#999">This link expires in 24 hours. If you did not request this, you can ignore it.</p>
      <p style="font-size:11px;color:#bbb">Link: {reset_url}</p>
    </div>
    """
    return _send_email(cfg, to_email, subject, html)


def _user_cache(user_id: str) -> dict:
    """Get a user's market cache, or a default disconnected state."""
    return user_market_cache.get(user_id, {
        "spot": 0.0, "atm": 0, "chain": {},
        "updated": None, "status": "waiting",
        "message": "Connect your broker in My Brokers to see live data.",
    })

# ── NSE Holiday cache ─────────────────────────────────────────
# Loaded from DB on startup and refreshed whenever sync runs.
# Avoids DB hit on every market-open check.
_holiday_dates: set  = set()   # "YYYY-MM-DD" strings for current + next year
_holiday_cache_year: int = 0   # year the cache was last loaded for

def _load_holiday_cache():
    """Reload NSE holiday dates from DB into memory. Call on startup + after sync."""
    global _holiday_dates, _holiday_cache_year
    import pytz
    ist  = pytz.timezone("Asia/Kolkata")
    year = datetime.now(ist).year
    db   = SessionLocal()
    try:
        rows = db.query(TradingEvent.event_date).filter(
            TradingEvent.suspend_trading == True,
            TradingEvent.event_date >= f"{year}-01-01",
            TradingEvent.event_date <= f"{year + 1}-12-31",
        ).all()
        _holiday_dates    = {r.event_date for r in rows}
        _holiday_cache_year = year
        log.info(f"Holiday cache loaded: {len(_holiday_dates)} suspended dates for {year}/{year+1}")
    except Exception as e:
        log.warning(f"Holiday cache load failed: {e}")
    finally:
        db.close()

def _market_open_now() -> bool:
    """Return True if Indian market is currently open (9:15–15:30, Mon–Fri IST, not a holiday)."""
    import pytz
    from datetime import time as dtime
    ist = pytz.timezone("Asia/Kolkata")
    now = datetime.now(ist)
    if now.weekday() >= 5:
        return False
    today = now.strftime("%Y-%m-%d")
    if today in _holiday_dates:
        return False
    return dtime(9, 15) <= now.time() <= dtime(15, 30)

def _next_market_open_msg() -> str:
    """Return human-readable next market opening time, skipping weekends and holidays."""
    import pytz
    from datetime import time as dtime, timedelta
    ist    = pytz.timezone("Asia/Kolkata")
    now    = datetime.now(ist)
    open_t = dtime(9, 15)

    # Walk forward day by day until we find a trading day
    candidate = now if now.time() < open_t else now + timedelta(days=1)
    for _ in range(14):   # safety: max 2 weeks forward
        if candidate.weekday() < 5 and candidate.strftime("%Y-%m-%d") not in _holiday_dates:
            if candidate.date() == now.date():
                return "Opens today at 9:15 AM IST"
            elif (candidate.date() - now.date()).days == 1:
                return "Opens tomorrow at 9:15 AM IST"
            else:
                return f"Opens {candidate.strftime('%A, %d %b')} at 9:15 AM IST"
        candidate += timedelta(days=1)
    return "Opens soon at 9:15 AM IST"

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("algodesk")

# ── Database ──────────────────────────────────────────────────

DATABASE_URL = os.environ.get("DATABASE_URL",
    "postgresql://algodesk:algodesk@postgres:5432/algodesk")
engine_db    = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=5)
SessionLocal = sessionmaker(bind=engine_db, autocommit=False, autoflush=False)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    Base.metadata.create_all(bind=engine_db)
    run_migrations(engine_db)
    db = SessionLocal()
    try:
        pass

        # Seed admin
        email = os.environ.get("SUPER_ADMIN_EMAIL", "")
        pw    = os.environ.get("SUPER_ADMIN_PASSWORD", "")
        name  = os.environ.get("SUPER_ADMIN_NAME", "Admin")
        if email and not db.query(User).filter(User.email == email).first():
            db.add(User(email=email, name=name,
                        password_hash=bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode(),
                        role="SUPER_ADMIN", plan="PRO",
                        is_active=True, is_verified=True))
            log.info(f"Admin created: {email}")

        # Seed Fyers definition
        existing_fyers = db.query(BrokerDefinition).filter(
            BrokerDefinition.broker_id == "fyers").first()
        if not existing_fyers:
            db.add(BrokerDefinition(
                broker_id="fyers", name="Fyers", flag="🇮🇳",
                market="INDIA", test_method="oauth",
                refresh_desc="Connect once — auto-refreshes on every use.",
                api_base_url="https://api-t1.fyers.in/api/v3",
                sort_order=1,
                symbols=NIFTY_SYMBOLS,
                fields_config=[
                    {"key":"client_id","label":"Client ID",
                     "hint":"myapi.fyers.in → your app → Client ID (e.g. FYXXXXX-100)",
                     "secret":False},
                    {"key":"secret_key","label":"Secret Key",
                     "hint":"myapi.fyers.in → your app → Secret Key",
                     "secret":True},
                    {"key":"pin","label":"4-digit PIN",
                     "hint":"Your Fyers trading PIN — used for automatic token refresh",
                     "secret":True},
                    {"key":"redirect_uri","label":"Redirect URI",
                     "hint":"Must exactly match your Fyers app setting",
                     "default":"https://trade.fyers.in/api-login/redirect-uri/index.html",
                     "secret":False},
                ]
            ))
            log.info("Fyers broker definition seeded")
        else:
            # Update symbols if empty
            if not existing_fyers.symbols:
                existing_fyers.symbols = NIFTY_SYMBOLS
                log.info("Updated Fyers symbols")

        db.commit()
    except Exception as e:
        log.error(f"DB init error: {e}")
        db.rollback()
    finally:
        db.close()

# ── App ───────────────────────────────────────────────────────

app = FastAPI(title="ALGO-DESK", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

bearer = HTTPBearer(auto_error=False)
SECRET = os.environ.get("SECRET_KEY", secrets.token_hex(32))
ALGO   = "HS256"

def make_token(email, role):
    return jwt.encode(
        {"sub": email, "role": role,
         "exp": datetime.utcnow() + timedelta(hours=24)},
        SECRET, algorithm=ALGO)

def get_current_user(creds: HTTPAuthorizationCredentials = Depends(bearer),
                     db: Session = Depends(get_db)):
    if not creds:
        raise HTTPException(401, "Not authenticated")
    try:
        payload = jwt.decode(creds.credentials, SECRET, algorithms=[ALGO])
        email   = payload["sub"]
    except JWTError:
        raise HTTPException(401, "Invalid or expired token")
    user = db.query(User).filter(User.email == email).first()
    if not user or not user.is_active:
        raise HTTPException(401, "User not found or suspended")
    return user

def require_admin(user: User = Depends(get_current_user)):
    if user.role not in ("SUPER_ADMIN", "ADMIN"):
        raise HTTPException(403, "Admin access required")
    return user

def _get_fyers(user: User, db: Session) -> Optional[FyersConnection]:
    """Helper to get a ready FyersConnection for a user."""
    bc = db.query(BrokerConnection).filter(
        BrokerConnection.user_id == user.id,
        BrokerConnection.broker_id == "fyers",
        BrokerConnection.is_connected == True
    ).first()
    if not bc:
        return None
    fields = {k.replace("_enc", ""): decrypt(user.id, v)
              for k, v in (bc.encrypted_fields or {}).items()}
    return FyersConnection(
        user_id=user.id,
        client_id=fields.get("client_id", ""),
        secret_key=fields.get("secret_key", ""),
        pin=fields.get("pin", ""),
        redirect_uri=fields.get("redirect_uri", ""),
        fyers_id=fields.get("fyers_id", ""),
        totp_key=fields.get("totp_key", ""),
        access_token_enc=bc.access_token_enc,
        refresh_token_enc=bc.refresh_token_enc,
        mode=bc.mode or "paper",
    )

def _save_tokens(user_id: str, conn: FyersConnection,
                 refresh_result: dict, db: Session):
    """Save refreshed tokens back to DB."""
    bc = db.query(BrokerConnection).filter(
        BrokerConnection.user_id == user_id,
        BrokerConnection.broker_id == "fyers"
    ).first()
    if bc and refresh_result.get("ok"):
        if refresh_result.get("access_token_enc"):
            bc.access_token_enc = refresh_result["access_token_enc"]
        if refresh_result.get("refresh_token_enc"):
            bc.refresh_token_enc = refresh_result["refresh_token_enc"]
        bc.last_token_refresh = datetime.utcnow()
        bc.is_connected = True
        db.commit()

# ── Startup ───────────────────────────────────────────────────


# ── Claude AI Integration ─────────────────────────────────────────

async def _run_claude_assessment(user_id: str, db_session) -> dict:
    """
    Run Claude's morning assessment for a user.
    Called at 9:10 AM before market opens.
    Returns structured JSON with trading recommendation.
    """
    import pytz
    ist = pytz.timezone("Asia/Kolkata")
    _now  = datetime.now(ist)
    today = _now.strftime("%Y-%m-%d")
    dow   = _now.strftime("%A")  # Monday, Tuesday...
    # NIFTY expiry is Tuesday (weekday 1), SENSEX is Thursday (weekday 3)
    is_expiry  = _now.weekday() in (1, 3)
    is_weekend = _now.weekday() >= 5  # Saturday=5, Sunday=6

    # Gather context
    cache  = _user_cache(user_id)
    vix    = cache.get("vix", 0)
    spot   = cache.get("spot", 0)
    prev_close = cache.get("prev_close", 0)
    gap_pct = ((spot - prev_close) / prev_close * 100) if prev_close else 0

    # Get user's recent performance
    from datetime import timedelta
    since = (datetime.now(ist) - timedelta(days=30)).strftime("%Y-%m-%d")
    recent_trades = db_session.query(ShadowTrade).filter(
        ShadowTrade.user_id == user_id,
        ShadowTrade.trade_date >= since,
        ShadowTrade.is_open == False
    ).all()
    n = len(recent_trades)
    wins = sum(1 for t in recent_trades if (t.net_pnl or 0) > 0)
    win_rate = round(wins/n*100, 1) if n else 0

    # Strategy-level win rates
    strat_stats = {}
    for t in recent_trades:
        s = t.strategy_code
        if s not in strat_stats:
            strat_stats[s] = {"n":0, "wins":0}
        strat_stats[s]["n"] += 1
        if (t.net_pnl or 0) > 0:
            strat_stats[s]["wins"] += 1
    strat_summary = ", ".join(
        f"{s}: {round(v['wins']/v['n']*100)}% win ({v['n']} trades)"
        for s,v in strat_stats.items() if v["n"] >= 3
    ) or "insufficient data"

    # ── Weekend short-circuit — give weekly recap, not a trading recommendation ──
    if is_weekend:
        week_pnl = sum(t.net_pnl or 0 for t in recent_trades
                       if t.trade_date >= (_now - timedelta(days=7)).strftime("%Y-%m-%d"))
        week_trades = sum(1 for t in recent_trades
                          if t.trade_date >= (_now - timedelta(days=7)).strftime("%Y-%m-%d"))
        next_session = "Monday" if _now.weekday() == 6 else "Monday"
        return {
            "trade_today":             False,
            "confidence":              "weekend",
            "risk_level":              "low",
            "recommended_strategies":  [],
            "avoid_strategies":        [],
            "suggested_hedge":         2,
            "vix_assessment":          "Markets closed — VIX not relevant until Monday open.",
            "gap_assessment":          f"Last 7 days: {week_trades} trades, ₹{week_pnl:+.0f} net P&L.",
            "reason":                  (
                f"Weekend — market is closed. "
                f"This week: {week_trades} paper trades with ₹{week_pnl:+.0f} net. "
                f"Win rate last 30 days: {win_rate}%. "
                f"Next session: {next_session}."
            ),
            "event_warning":           "",
            "weekend":                 True,
        }

    # Check event calendar
    events_today = db_session.query(TradingEvent).filter(
        TradingEvent.user_id == user_id,
        TradingEvent.event_date == today,
        TradingEvent.suspend_trading == True
    ).all()
    event_str = ", ".join(e.event_name for e in events_today)

    prompt = f"""You are an expert Indian options trading risk advisor for an algo trader.
Respond ONLY with valid JSON — no markdown, no explanation outside the JSON.
IMPORTANT: Use ONLY the date and market data provided below. Do NOT guess or invent dates.

TODAY IS: {dow}, {today}
TRADING INSTRUMENT: NIFTY 50 options (NSE)

MARKET DATA ({dow} {today}):
- India VIX: {vix if vix else 'unknown'}
- NIFTY spot: {spot if spot else 'unknown'}
- Previous close: {prev_close if prev_close else 'unknown'}
- Gap from prev close: {gap_pct:.2f}%
- Day of week: {dow}{' (EXPIRY DAY — NIFTY/SENSEX weekly expiry)' if is_expiry else ''}
- Events today: {event_str if event_str else 'None'}

USER PERFORMANCE (last 30 days):
- Total paper trades: {n}
- Overall win rate: {win_rate}%
- By strategy: {strat_summary}

AVAILABLE STRATEGIES: S1 (ORB Breakdown), S2 (VWAP Squeeze), S3 (Breakout Reversal),
S4 (Iron Condor range-bound), S6 (Theta Strangle high IV), S7 (All-Strike Butterfly),
S8 (Gap Fade), S9 (Expiry Day Theta Crush - Tue/Thu expiry only), S5 (Ratio Spread advanced)

Respond with this exact JSON structure:
{{
  "trade_today": true or false,
  "confidence": "high" or "medium" or "low",
  "risk_level": "low" or "medium" or "high",
  "recommended_strategies": ["S1", "S9"],
  "avoid_strategies": ["S2"],
  "suggested_hedge": 2,
  "vix_assessment": "one concise line about VIX conditions",
  "gap_assessment": "one concise line about gap and what it means",
  "reason": "2-3 sentences explaining today recommendation",
  "event_warning": "empty string or warning about events"
}}"""

    # Get Gemini client using the same user object already fetched
    user_obj = db_session.query(User).filter(User.id == user_id).first()
    ai_cfg   = (user_obj.ai_config or {}) if user_obj else {}
    gemini, enabled = _get_gemini_client(ai_cfg)
    if not enabled or not gemini:
        return {
            "trade_today": True, "confidence": "low",
            "risk_level": "medium",
            "reason": "AI not configured — add Gemini API key in Profile → AI Settings.",
            "recommended_strategies": [], "avoid_strategies": [],
            "suggested_hedge": 2, "vix_assessment": "",
            "gap_assessment": "", "event_warning": ""
        }
    model_name = ai_cfg.get("model", _GEMINI_MODEL)
    try:
        response = gemini.models.generate_content(model=model_name, contents=prompt)
        raw = response.text.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw.strip())
        result["raw_response"] = raw
        return result
    except Exception as e:
        log.error(f"Gemini assessment failed: {e}")
        return {
            "trade_today": True, "confidence": "low",
            "risk_level": "medium", "reason": f"AI assessment unavailable: {str(e)}",
            "recommended_strategies": [], "avoid_strategies": [],
            "suggested_hedge": 2, "vix_assessment": "",
            "gap_assessment": "", "event_warning": ""
        }


async def _analyse_closed_trade(trade, user_ai_config: dict) -> str:
    """Quick one-line Gemini insight after a trade closes."""
    gemini, enabled = _get_gemini_client(user_ai_config)
    if not enabled or not gemini:
        return ""
    if not (user_ai_config or {}).get("use_for_analysis", True):
        return ""
    try:
        sig = trade.signal_data or {}
        prompt = (f"NIFTY options trade result (one sentence insight only):\n"
                  f"Strategy: {trade.strategy_code} | "
                  f"Entry: Rs{trade.entry_combined} | "
                  f"Exit: Rs{trade.exit_combined} | "
                  f"Reason: {trade.exit_reason} | "
                  f"Net PnL: Rs{trade.net_pnl} | "
                  f"Signal: {sig.get('reason','?')} | "
                  f"Entry time: {trade.entry_time}\n"
                  f"In exactly one sentence, what caused this outcome?")
        model_name = (user_ai_config or {}).get("model", _GEMINI_MODEL)
        response = gemini.models.generate_content(model=model_name, contents=prompt)
        return response.text.strip()
    except Exception:
        return ""


def _to_ist(dt) -> str:
    """Format datetime as ISO string with IST time for frontend date math.
    New records (post Jan 2026 fix) are stored as IST-naive.
    Old records were stored as UTC-naive. Heuristic: if the hour is
    < 4 it is almost certainly UTC (market opens 9:15 IST = 3:45 UTC).
    In that case add 5h30m offset to convert to IST.
    Returns full ISO string (YYYY-MM-DDTHH:MM:SS) so new Date() parses correctly.
    """
    if dt is None: return None
    h = dt.hour
    if h < 4:
        from datetime import timedelta
        dt = dt + timedelta(hours=5, minutes=30)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")

@app.on_event("startup")
async def startup():
    init_db()
    _load_holiday_cache()
    asyncio.create_task(_market_data_service())
    asyncio.create_task(_auto_resume_engines())
    asyncio.create_task(_auto_sync_holidays())
    log.info("ALGO-DESK v5 started ✓")


async def _market_data_service():
    """
    Per-user market data service.
    Each user with a connected broker gets their own live feed.
    Users without a broker see no live data — correct behaviour.
    Runs every 60s during market hours only.
    """
    import pytz
    from datetime import time as dtime
    ist = pytz.timezone("Asia/Kolkata")
    log.info("Per-user market data service started")
    await asyncio.sleep(8)

    while True:
        try:
            now = datetime.now(ist)
            t   = now.time()
            is_mkt = dtime(9,15) <= t <= dtime(15,30) and now.weekday() < 5

            if not is_mkt:
                # Market closed.
                # SEBI April 2026: tokens expire at 3 AM IST daily.
                # At 8:00–9:14 AM IST: do daily TOTP headless re-auth for each user.
                # Outside that window: just keep cache fresh with last known spot.
                db_tok = SessionLocal()
                try:
                    # Include ALL Fyers connections that have credentials stored.
                    # TOTP-configured brokers self-heal even if is_connected=False
                    # (token expired at 3 AM is not a manual disconnect).
                    # Brokers without encrypted_fields have nothing to work with.
                    bcs_tok = db_tok.query(BrokerConnection).filter(
                        BrokerConnection.broker_id == "fyers",
                        BrokerConnection.encrypted_fields != None,  # noqa
                    ).all()
                    # Re-auth window: 3:05 AM → 9:14 AM IST.
                    # Starts 5 min after token expires (3 AM) so it's fresh
                    # well before market opens. Wide window = plenty of retry time.
                    _reauth_window = dtime(3, 5) <= t <= dtime(9, 14)

                    for bc_tok in bcs_tok:
                        user_tok = db_tok.query(User).filter(
                            User.id == bc_tok.user_id,
                            User.is_active == True).first()
                        if not user_tok:
                            continue
                        try:
                            fields_tok = {k.replace("_enc",""):decrypt(user_tok.id,v)
                                      for k,v in (bc_tok.encrypted_fields or {}).items()}

                            # ── Check if today's re-auth is needed ───────────
                            _reauth_needed = True
                            if bc_tok.last_token_refresh:
                                import pytz as _ptz
                                _last_ist = (bc_tok.last_token_refresh
                                             .replace(tzinfo=_ptz.utc)
                                             .astimezone(ist))
                                _reauth_needed = _last_ist.date() < now.date()

                            # ── 8:00–9:14 AM IST: TOTP daily re-auth ────────
                            if _reauth_window and _reauth_needed:
                                _has_totp = (fields_tok.get("totp_key")
                                             and fields_tok.get("fyers_id"))
                                if _has_totp:
                                    conn_tok = FyersConnection(
                                        user_id=user_tok.id,
                                        client_id=fields_tok.get("client_id",""),
                                        secret_key=fields_tok.get("secret_key",""),
                                        pin=fields_tok.get("pin",""),
                                        redirect_uri=fields_tok.get("redirect_uri",""),
                                        fyers_id=fields_tok.get("fyers_id",""),
                                        totp_key=fields_tok.get("totp_key",""),
                                        access_token_enc=bc_tok.access_token_enc,
                                    )
                                    login_result = await conn_tok.headless_login()
                                    if login_result.get("ok"):
                                        bc_tok.access_token_enc       = login_result["access_token_enc"]
                                        bc_tok.refresh_token_enc      = login_result.get("refresh_token_enc") or ""
                                        bc_tok.last_token_refresh     = datetime.utcnow()
                                        bc_tok.refresh_token_issued_at = datetime.utcnow()
                                        bc_tok.is_connected           = True
                                        db_tok.commit()
                                        log.info(f"[{user_tok.email}] Daily TOTP re-auth OK")
                                    else:
                                        _msg = login_result.get("message","")
                                        log.error(f"[{user_tok.email}] TOTP re-auth FAILED: {_msg}")
                                        try:
                                            await _send_telegram_all(user_tok,
                                                f"⚠️ AlgoDesk: Daily Fyers re-auth FAILED.\n"
                                                f"Reason: {_msg}\n"
                                                f"Fix your TOTP key in My Brokers before market opens at 9:15 AM.")
                                        except Exception:
                                            pass
                                else:
                                    # No TOTP key configured — warn user, cannot auto re-auth
                                    # validate-refresh-token is DISABLED by Fyers (SEBI April 2026)
                                    log.warning(f"[{user_tok.email}] No TOTP key configured — "
                                                f"cannot auto re-auth. Add fyers_id + totp_key in My Brokers.")

                            # ── Update dashboard spot price cache ────────────
                            # Only attempt if token was refreshed today (valid until 3 AM)
                            if not _reauth_needed and bc_tok.access_token_enc:
                                conn_spot = FyersConnection(
                                    user_id=user_tok.id,
                                    client_id=fields_tok.get("client_id",""),
                                    secret_key=fields_tok.get("secret_key",""),
                                    pin=fields_tok.get("pin",""),
                                    redirect_uri=fields_tok.get("redirect_uri",""),
                                    fyers_id=fields_tok.get("fyers_id",""),
                                    totp_key=fields_tok.get("totp_key",""),
                                    access_token_enc=bc_tok.access_token_enc,
                                )
                                _closed_spot = user_market_cache.get(user_tok.id, {}).get("spot", 0)
                                if not _closed_spot:
                                    try:
                                        _sq = await conn_spot.get_quotes(["NSE:NIFTY50-INDEX"])
                                        if _sq.get("ok"):
                                            _sq_d = _sq["quotes"].get("NSE:NIFTY50-INDEX", {})
                                            _closed_spot = _sq_d.get("ltp", 0)
                                    except Exception:
                                        pass
                                user_market_cache[user_tok.id] = {
                                    **user_market_cache.get(user_tok.id, {}),
                                    "spot":   _closed_spot or user_market_cache.get(user_tok.id, {}).get("spot", 0),
                                    "atm":    (round(_closed_spot/50)*50) if _closed_spot
                                              else user_market_cache.get(user_tok.id, {}).get("atm", 0),
                                    "status":  "closed",
                                    "message": f"Market closed · {_next_market_open_msg()}",
                                }
                        except Exception as tok_e:
                            log.debug(f"Closed-market service [{user_tok.email}]: {tok_e}")
                except Exception as e:
                    log.error(f"Closed-market service error: {e}")
                finally:
                    db_tok.close()
                await asyncio.sleep(300)  # check every 5 min when closed
                continue

            db = SessionLocal()
            try:
                # Get all users with connected Fyers broker.
                # Include brokers re-authed today via TOTP (is_connected may lag by 1 tick).
                from datetime import date as _date
                _today_utc_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
                bcs = db.query(BrokerConnection).filter(
                    BrokerConnection.broker_id == "fyers",
                    (BrokerConnection.is_connected == True) |
                    (BrokerConnection.last_token_refresh >= _today_utc_start)
                ).all()

                for bc in bcs:
                    user = db.query(User).filter(
                        User.id == bc.user_id,
                        User.is_active == True).first()
                    # Allow TOTP-only brokers (no refresh_token_enc) as long as access_token_enc exists
                    if not user or not bc.access_token_enc:
                        continue

                    # Build connection for this user
                    fields = {k.replace("_enc",""):decrypt(user.id,v)
                              for k,v in (bc.encrypted_fields or {}).items()}
                    conn = FyersConnection(
                        user_id=user.id,
                        client_id=fields.get("client_id",""),
                        secret_key=fields.get("secret_key",""),
                        pin=fields.get("pin",""),
                        redirect_uri=fields.get("redirect_uri",""),
                        fyers_id=fields.get("fyers_id",""),
                        totp_key=fields.get("totp_key",""),
                        access_token_enc=bc.access_token_enc,
                        refresh_token_enc=bc.refresh_token_enc,
                    )

                    # Determine which symbols this user needs
                    user_autos = db.query(Automation).filter(
                        Automation.user_id == user.id,
                        Automation.status.in_(["RUNNING", "IDLE"])
                    ).all()
                    needed_symbols = list({
                        a.symbol for a in user_autos
                        if a.symbol and a.symbol in SYMBOL_REGISTRY
                    }) or ["NSE:NIFTY50-INDEX"]
                    if "NSE:NIFTY50-INDEX" not in needed_symbols:
                        needed_symbols.insert(0, "NSE:NIFTY50-INDEX")

                    # Fetch NIFTY first (primary cache)
                    result = await conn.get_spot_and_chain(
                        "NSE:NIFTY50-INDEX", strike_count=25)

                    if result.get("ok"):
                        # Save refreshed tokens back to this user's DB row
                        rt = result.get("refresh_tokens", {})
                        if rt.get("ok"):
                            if rt.get("access_token_enc"):
                                bc.access_token_enc = rt["access_token_enc"]
                            if rt.get("refresh_token_enc"):
                                bc.refresh_token_enc = rt["refresh_token_enc"]
                            bc.last_token_refresh = datetime.utcnow()

                        # Update this user's personal cache (primary = NIFTY)
                        # Preserve daily market data (VIX, prev_close) fetched once at 9:15
                        ts  = datetime.now(ist).strftime("%H:%M:%S")
                        _prev_cache = user_market_cache.get(user.id, {})
                        user_market_cache[user.id] = {
                            "spot":             result["spot"],
                            "atm":              result["atm"],
                            "chain":            result["chain"],
                            "updated":          ts,
                            "status":           "live",
                            "message":          f"Live · {ts} IST",
                            # Daily fields — preserved across ticks, set once at 9:15
                            "vix":              _prev_cache.get("vix", 0),
                            "prev_close":       _prev_cache.get("prev_close", 0),
                            "prev_day_move_pct": _prev_cache.get("prev_day_move_pct", 0),
                            "vix_date":         _prev_cache.get("vix_date", ""),
                        }
                        # Store in symbol cache too
                        if user.id not in user_symbol_cache:
                            user_symbol_cache[user.id] = {}
                        user_symbol_cache[user.id]["NSE:NIFTY50-INDEX"] = {
                            "spot": result["spot"], "atm": result["atm"],
                            "chain": result["chain"], "updated": ts, "status": "live",
                        }

                        # ── Once-per-day: fetch India VIX + prev close ──
                        # These feed Guard 2 (VIX filter), Guard 5 (gap skip),
                        # S8 (gap fade), S10 (gap directional), AI prompt context.
                        # No time gate — retries on any market-hours tick until fetched.
                        # This ensures server restarts during the day still populate VIX.
                        _today_mds = datetime.now(ist).strftime("%Y-%m-%d")
                        if user_market_cache.get(user.id, {}).get("vix_date") != _today_mds:
                            try:
                                # India VIX live value
                                _vix_res = await conn.get_quotes(["NSE:INDIAVIX-INDEX"])
                                if _vix_res.get("ok"):
                                    _vix_val = _vix_res["quotes"].get("NSE:INDIAVIX-INDEX", {}).get("ltp", 0)
                                    if _vix_val:
                                        user_market_cache[user.id]["vix"]      = round(_vix_val, 2)
                                        user_market_cache[user.id]["vix_date"] = _today_mds
                                        log.info(f"[{user.email}] India VIX: {_vix_val:.1f}")
                                    else:
                                        log.warning(f"[{user.email}] VIX returned 0 — Guard 2 inactive today")
                                else:
                                    log.warning(f"[{user.email}] VIX fetch failed: {_vix_res.get('message')}")

                                # Previous day close via quotes API (prev_close_price field)
                                # Avoids broken daily-history endpoint (Fyers 422 on some tokens)
                                _nq = await conn.get_quotes(["NSE:NIFTY50-INDEX"])
                                if _nq.get("ok"):
                                    _nq_data = _nq["quotes"].get("NSE:NIFTY50-INDEX", {})
                                    _prev_c   = round(_nq_data.get("prev_close", 0), 0)
                                    _today_o  = round(_nq_data.get("open",       0), 0)
                                    if _prev_c > 0:
                                        # gap_pct = how much today gapped from yesterday's close
                                        _pmove = round(abs(_today_o - _prev_c) / _prev_c * 100, 2) if _today_o > 0 else 0
                                        user_market_cache[user.id]["prev_close"]        = _prev_c
                                        user_market_cache[user.id]["prev_day_move_pct"] = _pmove
                                        log.info(f"[{user.email}] Prev close: {_prev_c:.0f} | Today gap: {_pmove:.1f}%")
                                    else:
                                        log.warning(f"[{user.email}] Prev close returned 0 — S8/S10/Gap guards inactive")
                                else:
                                    log.warning(f"[{user.email}] Prev close fetch failed — S8/S10/Gap guards inactive")
                            except Exception as _e_vix:
                                log.warning(f"[{user.email}] VIX/prev_close fetch error: {_e_vix}")

                    # Fetch additional symbols (BankNifty, FinNifty etc.)
                    for sym in needed_symbols:
                        if sym == "NSE:NIFTY50-INDEX":
                            continue
                        try:
                            sym_result = await conn.get_spot_and_chain(sym, strike_count=5)
                            if sym_result.get("ok"):
                                ts2 = datetime.now(ist).strftime("%H:%M:%S")
                                user_symbol_cache[user.id][sym] = {
                                    "spot": sym_result["spot"], "atm": sym_result["atm"],
                                    "chain": sym_result["chain"], "updated": ts2, "status": "live",
                                }
                                log.info(f"[{user.email}] {sym.split(':')[1]}={sym_result['spot']:.1f}")
                        except Exception as sym_e:
                            log.debug(f"[{user.email}] {sym} fetch error: {sym_e}")

                        # Feed all running engines for this user
                        for eng in _all_engines(user.id):
                            if eng and eng.is_running:
                                eng.spot_history.append(result["spot"])
                                if eng.strikes:
                                    for sk in eng.strikes:
                                        cd = result["chain"].get(sk.strike)
                                        if cd:
                                            sk.update(cd["combined"],
                                                      ce_ltp=cd.get("ce_ltp", 0),
                                                      pe_ltp=cd.get("pe_ltp", 0))
                                        sk.ce_symbol = cd.get("ce_symbol","")
                                        sk.pe_symbol = cd.get("pe_symbol","")
                                        if dtime(9,15) <= t <= dtime(9,21):
                                            if sk.orb_high == 0:
                                                sk.orb_high = sk.orb_low = cd["combined"]
                                            sk.orb_high = max(sk.orb_high, cd["combined"])
                                            sk.orb_low  = min(sk.orb_low,  cd["combined"])
                            if t >= dtime(9,22) and not eng.orb_complete:
                                eng.orb_complete = True
                                atm_sk = eng.atm
                                if atm_sk:
                                    eng.emit(
                                        f"ORB complete. ATM {atm_sk.strike}: "
                                        f"Low={atm_sk.orb_low:.1f} "
                                        f"High={atm_sk.orb_high:.1f}", "OK")

                        log.info(f"[{user.email}] NIFTY={result['spot']:.1f} ATM={result['atm']}")

                    else:
                        # Token or data error for this user
                        msg = result.get("message","Data fetch failed")
                        # Preserve ALL existing daily data (vix, prev_close, atm, etc.)
                        # — only overwrite live-tick fields so VIX/prev_close survive
                        _err_prev = user_market_cache.get(user.id, {})
                        user_market_cache[user.id] = {
                            **_err_prev,
                            "status": "error",
                            "message": msg,
                        }
                        # Only hard-disconnect on definitive permanent auth failures.
                        # Normal 3 AM token expiry is expected under SEBI April 2026 rules
                        # and is recovered automatically by the 8:30 AM TOTP re-auth.
                        _hard_fail = ("invalid" in msg.lower() and "token" in msg.lower()) or \
                                     "revoked" in msg.lower() or \
                                     "not authorized" in msg.lower()
                        if _hard_fail:
                            bc.is_connected = False
                            user_market_cache[user.id]["message"] = (
                                "Fyers authorisation revoked — please reconnect in My Brokers.")
                        log.warning(f"[{user.email}] Market data error: {msg}")

                db.commit()

            except Exception as e:
                log.error(f"Market data service DB error: {e}")
            finally:
                db.close()

        except Exception as e:
            log.error(f"Market data service: {e}")

        await asyncio.sleep(60)


async def _auto_resume_engines():
    await asyncio.sleep(30)
    db = SessionLocal()
    try:
        # ── Reset stale RUNNING states from before restart ───────
        # Capture the list first, then reset to IDLE, then resume selectively
        to_resume = db.query(Automation).filter(Automation.status=="RUNNING").all()
        for auto in to_resume:
            auto.status = "IDLE"
        db.commit()
        log.info(f"Startup: reset {len(to_resume)} stale RUNNING automation(s) to IDLE")

        # ── Resume running live automations ─────────────────────
        for auto in to_resume:
            user = db.query(User).filter(
                User.id==auto.user_id, User.is_active==True).first()
            if not user:
                continue
            conn = _get_fyers(user, db)
            if not conn:
                auto.status = "IDLE"; db.commit(); continue
            config = {**auto.config, "strategies":auto.strategies, "mode":auto.mode}
            state = EngineState(config)
            _set_engine(user.id, auto.id, state)
            asyncio.create_task(_run_engine(user.id, auto, state, conn, db))
            log.info(f"Auto-resumed: {user.email} / {auto.name}")

        # ── Close orphaned open shadow trades from before restart ──
        # Any ShadowTrade that is still open but not monitored for >2 hours
        # was orphaned by a server restart — close it at last known combined
        import pytz
        ist = pytz.timezone("Asia/Kolkata")
        cutoff = datetime.utcnow() - __import__("datetime").timedelta(hours=2)
        orphans = db.query(ShadowTrade).filter(
            ShadowTrade.is_open == True,
        ).all()
        for ot in orphans:
            # Check if last_monitored was more than 2 hours ago
            last = ot.last_monitored or ot.entry_time
            if last and (datetime.utcnow() - last).total_seconds() > 7200:
                # Close at entry (we don't know exit price — conservative)
                ot.exit_combined = ot.entry_combined
                ot.exit_time     = datetime.utcnow()
                ot.exit_reason   = "SERVER_RESTART (position data lost)"
                ot.gross_pnl     = 0.0
                ot.net_pnl       = 0.0
                ot.is_open       = False
                ot.sl_tracking   = {"note": "Closed due to server restart — P&L unknown"}
                db.commit()
                log.warning(f"Closed orphaned shadow trade {ot.id} for user {ot.user_id}")

        # ── Close orphaned open live trades too ──────────────────
        orphan_live = db.query(Trade).filter(Trade.is_open == True).all()
        for ot in orphan_live:
            age = (datetime.utcnow() - ot.entry_time).total_seconds() if ot.entry_time else 999999
            # If open for more than 8 hours (overnight), mark as orphaned
            if age > 28800:
                ot.exit_combined = ot.entry_combined
                ot.exit_time     = datetime.utcnow()
                ot.exit_reason   = "SERVER_RESTART (check broker app)"
                ot.gross_pnl     = 0.0
                ot.net_pnl       = 0.0
                ot.is_open       = False
                db.commit()
                log.warning(f"Closed orphaned live trade {ot.id} — check Fyers for actual status")

    except Exception as e:
        log.error(f"Auto-resume: {e}")
    finally:
        db.close()

# ── Health ────────────────────────────────────────────────────

@app.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    return {"status": "ok", "version": "5.0.0",
            "db": "ok" if db_ok else "error",
            "time": datetime.now().isoformat()}

# ── Auth ──────────────────────────────────────────────────────

class LoginReq(BaseModel):
    email: str
    password: str

@app.post("/api/auth/login")
def login(req: LoginReq, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == req.email.lower().strip()).first()
    if not user or not user.is_active:
        raise HTTPException(401, "Invalid email or password")
    if not bcrypt.checkpw(req.password.encode(), user.password_hash.encode()):
        raise HTTPException(401, "Invalid email or password")
    user.last_login = datetime.utcnow()
    db.commit()
    return {"token": make_token(user.email, user.role),
            "name": user.name, "email": user.email,
            "role": user.role, "plan": user.plan,
            "must_change_password": bool(getattr(user, 'must_change_password', False))}

class RegisterReq(BaseModel):
    email: str; password: str; name: str
    invite_token: Optional[str] = None

@app.post("/api/auth/register")
def register(req: RegisterReq, db: Session = Depends(get_db)):
    email = req.email.lower().strip()
    reg_open = os.environ.get("REGISTRATION_OPEN", "true").lower() == "true"
    invite = None
    if req.invite_token:
        invite = db.query(InviteLink).filter(
            InviteLink.token == req.invite_token,
            InviteLink.used == False).first()
    if not reg_open and not invite:
        raise HTTPException(403, "Registration closed. Ask admin for invite.")
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(400, "Email already registered")
    if len(req.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    user = User(email=email, name=req.name,
                password_hash=bcrypt.hashpw(req.password.encode(), bcrypt.gensalt()).decode(),
                role=invite.role if invite else "USER",
                plan=invite.plan if invite else "FREE",
                is_active=True, is_verified=False)
    db.add(user)
    if invite:
        invite.used = True; invite.used_by = email
    db.commit()
    return {"ok": True, "token": make_token(email, user.role),
            "name": req.name, "email": email,
            "role": user.role, "plan": user.plan}

class ResetReq(BaseModel):
    email: str

@app.post("/api/auth/reset-request")
def reset_request(req: ResetReq, request: Request, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == req.email.lower()).first()
    if user:
        token = secrets.token_urlsafe(32)
        db.add(ResetToken(user_id=user.id, token=token,
                          expires_at=datetime.utcnow() + timedelta(hours=24)))
        db.commit()
        email_cfg = _get_email_config(db)
        domain = _resolve_domain(email_cfg, request)
        reset_url = f"https://{domain}/?reset_token={token}"
        if email_cfg.get("enabled"):
            _email_reset_link(email_cfg, user.email, user.name or user.email.split("@")[0], reset_url)
        return {"ok": True, "reset_url": reset_url,
                "message": "Reset link generated"}
    return {"ok": True, "message": "Reset link sent if account exists"}

class ResetPwReq(BaseModel):
    token: str; new_password: str

@app.post("/api/auth/reset-password")
def reset_password(req: ResetPwReq, db: Session = Depends(get_db)):
    rt = db.query(ResetToken).filter(
        ResetToken.token == req.token,
        ResetToken.used == False).first()
    if not rt or datetime.utcnow() > rt.expires_at:
        raise HTTPException(400, "Invalid or expired reset link")
    if len(req.new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    user = db.query(User).filter(User.id == rt.user_id).first()
    user.password_hash = bcrypt.hashpw(req.new_password.encode(), bcrypt.gensalt()).decode()
    rt.used = True
    db.commit()
    return {"ok": True, "message": "Password updated"}

class ChangePwReq(BaseModel):
    current_password: str = None
    old_password: str = None
    new_password: str

@app.post("/api/auth/change-password")
def change_password(req: ChangePwReq, user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    old_pw = req.current_password or req.old_password or ""
    if not old_pw:
        raise HTTPException(400, "Current password required")
    if not bcrypt.checkpw(old_pw.encode(), user.password_hash.encode()):
        raise HTTPException(400, "Current password incorrect")
    if len(req.new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    user.password_hash = bcrypt.hashpw(req.new_password.encode(), bcrypt.gensalt()).decode()
    user.must_change_password = False
    db.commit()
    return {"ok": True}

# ── Profile ───────────────────────────────────────────────────

@app.get("/api/me")
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    bc = db.query(BrokerConnection).filter(
        BrokerConnection.user_id == user.id,
        BrokerConnection.is_connected == True).first()
    ai_cfg = user.ai_config or {}
    _, ai_on = _get_gemini_client(ai_cfg)
    return {
        "id": user.id, "email": user.email, "name": user.name,
        "role": user.role, "plan": user.plan,
        "timezone": user.timezone or "Asia/Kolkata",
        "telegram_configured": bool(user.telegram_chat),
        "broker_connected": bool(bc),
        "broker_name": bc.broker_name if bc else None,
        "broker_mode": bc.mode if bc else None,
        "ai_enabled": ai_on,
        "ai_model": ai_cfg.get("model", _GEMINI_MODEL),
        "ai_use_trading": ai_cfg.get("use_for_trading", True),
        "ai_use_analysis": ai_cfg.get("use_for_analysis", True),
        "ai_key_set": bool(ai_cfg.get("api_key_enc", "")),
        "is_verified": user.is_verified,
    }

class UpdateProfileReq(BaseModel):
    name: Optional[str] = None
    timezone: Optional[str] = None
    telegram_token: Optional[str] = None
    telegram_chat: Optional[str] = None

@app.get("/api/plan")
def get_user_plan(user: User = Depends(get_current_user)):
    """Returns current user plan details and feature access."""
    plan = get_plan(user)
    return {
        "plan":            user.plan,
        "label":           plan["label"],
        "description":     plan["description"],
        "live_trading":    plan["live_trading"],
        "strategies":      plan["strategies"],
        "max_automations": plan["max_automations"],
        "all_plans":       {k: {"label":v["label"],
                               "description":v["description"],
                               "live_trading":v["live_trading"],
                               "max_automations":v["max_automations"]}
                            for k,v in PLAN_CONFIG.items()},
    }

@app.put("/api/me")
def update_profile(req: UpdateProfileReq, user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    if req.name:           user.name = req.name
    if req.timezone:       user.timezone = req.timezone
    if req.telegram_token: user.telegram_token = req.telegram_token
    if req.telegram_chat:  user.telegram_chat = req.telegram_chat
    db.commit()
    return {"ok": True}

# ── Broker definitions ────────────────────────────────────────

@app.get("/api/brokers/definitions")
def broker_definitions(user: User = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    defs = db.query(BrokerDefinition).filter(
        BrokerDefinition.is_active == True
    ).order_by(BrokerDefinition.sort_order).all()
    return {"brokers": [
        {"id": d.broker_id, "name": d.name, "flag": d.flag,
         "market": d.market, "refresh": d.refresh_desc,
         "test_method": d.test_method, "fields": d.fields_config}
        for d in defs]}

# ── Broker connections ────────────────────────────────────────

@app.get("/api/brokers")
def list_brokers(user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    brokers = db.query(BrokerConnection).filter(
        BrokerConnection.user_id == user.id).all()
    def _has_field(b, key):
        return (key + "_enc") in (b.encrypted_fields or {})
    return {"brokers": [
        {"id": b.id, "broker_id": b.broker_id,
         "broker_name": b.broker_name, "market": b.market,
         "mode": b.mode, "is_connected": b.is_connected,
         "last_token_refresh": b.last_token_refresh.isoformat()
             if b.last_token_refresh else None,
         "totp_configured": (_has_field(b, "totp_key") and _has_field(b, "fyers_id")),
         "fields_count": len(b.encrypted_fields or {})}
        for b in brokers]}

class SaveBrokerReq(BaseModel):
    broker_id: str; fields: dict; mode: str = "paper"

@app.post("/api/brokers")
def save_broker(req: SaveBrokerReq, user: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    bd = db.query(BrokerDefinition).filter(
        BrokerDefinition.broker_id == req.broker_id).first()
    if not bd:
        raise HTTPException(400, f"Unknown broker: {req.broker_id}")

    encrypted = {}
    for k, v in req.fields.items():
        if v and str(v).strip():
            val = str(v).strip()
            if not val and bd.fields_config:
                defn = next((f for f in bd.fields_config if f["key"] == k), {})
                val = defn.get("default", "")
            if val:
                encrypted[k + "_enc"] = encrypt(user.id, val)

    existing = db.query(BrokerConnection).filter(
        BrokerConnection.user_id == user.id,
        BrokerConnection.broker_id == req.broker_id).first()

    if existing:
        existing.encrypted_fields = encrypted
        existing.mode = req.mode
        existing.is_connected = False
        bc = existing
    else:
        bc = BrokerConnection(
            user_id=user.id, broker_id=req.broker_id,
            broker_name=bd.name, market=bd.market,
            mode=req.mode, encrypted_fields=encrypted)
        db.add(bc)

    db.commit()
    return {"ok": True, "message": "Credentials saved. Click Connect to authorise."}

@app.get("/api/brokers/fyers/login-url")
def fyers_login_url(user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    bc = db.query(BrokerConnection).filter(
        BrokerConnection.user_id == user.id,
        BrokerConnection.broker_id == "fyers").first()
    if not bc:
        raise HTTPException(400, "Save credentials first")
    fields = {k.replace("_enc", ""): decrypt(user.id, v)
              for k, v in (bc.encrypted_fields or {}).items()}
    conn = FyersConnection(
        user_id=user.id,
        client_id=fields.get("client_id", ""),
        secret_key=fields.get("secret_key", ""),
        pin=fields.get("pin", ""),
        redirect_uri=fields.get("redirect_uri",
            "https://trade.fyers.in/api-login/redirect-uri/index.html"),
        fyers_id=fields.get("fyers_id", ""),
        totp_key=fields.get("totp_key", ""))
    if not conn.client_id:
        raise HTTPException(400, "Client ID not saved")
    url_str = conn.login_url()
    return {"ok": True, "url": url_str, "login_url": url_str}

class FyersConnectReq(BaseModel):
    auth_code: str

@app.post("/api/brokers/fyers/connect")
async def fyers_connect(req: FyersConnectReq,
                        user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    bc = db.query(BrokerConnection).filter(
        BrokerConnection.user_id == user.id,
        BrokerConnection.broker_id == "fyers").first()
    if not bc:
        raise HTTPException(400, "Save credentials first")

    fields = {k.replace("_enc", ""): decrypt(user.id, v)
              for k, v in (bc.encrypted_fields or {}).items()}
    conn = FyersConnection(
        user_id=user.id,
        client_id=fields.get("client_id", ""),
        secret_key=fields.get("secret_key", ""),
        pin=fields.get("pin", ""),
        redirect_uri=fields.get("redirect_uri",
            "https://trade.fyers.in/api-login/redirect-uri/index.html"),
        fyers_id=fields.get("fyers_id", ""),
        totp_key=fields.get("totp_key", ""))

    result = await conn.exchange_auth_code(req.auth_code.strip())
    if result["ok"]:
        bc.access_token_enc        = result["access_token_enc"]
        bc.refresh_token_enc       = result["refresh_token_enc"]
        bc.is_connected            = True
        bc.last_token_refresh      = datetime.utcnow()
        bc.refresh_token_issued_at = datetime.utcnow()
        db.commit()
        return {"ok": True,
                "message": ("Fyers connected! ✅ Set up your TOTP Key in My Brokers for "
                            "automated daily re-auth (SEBI April 2026 requirement)."),
                "connected": True}
    return {"ok": False, "message": result["message"], "connected": False}

@app.post("/api/brokers/fyers/test-auth")
async def fyers_test_auth(user: User = Depends(get_current_user),
                          db: Session = Depends(get_db)):
    """Trigger a manual TOTP re-auth attempt right now (for testing outside the 3–9 AM window)."""
    bc = db.query(BrokerConnection).filter(
        BrokerConnection.user_id == user.id,
        BrokerConnection.broker_id == "fyers").first()
    if not bc:
        return {"ok": False, "message": "No Fyers connection found"}
    fields = {k.replace("_enc", ""): decrypt(user.id, v)
              for k, v in (bc.encrypted_fields or {}).items()}
    if not fields.get("totp_key") or not fields.get("fyers_id"):
        return {"ok": False, "message": "totp_key and fyers_id must be set in My Brokers"}
    conn = FyersConnection(
        user_id=user.id,
        client_id=fields.get("client_id", ""),
        secret_key=fields.get("secret_key", ""),
        pin=fields.get("pin", ""),
        redirect_uri=fields.get("redirect_uri",
            "https://trade.fyers.in/api-login/redirect-uri/index.html"),
        fyers_id=fields.get("fyers_id", ""),
        totp_key=fields.get("totp_key", ""),
        access_token_enc=bc.access_token_enc)
    result = await conn.headless_login()
    if result.get("ok"):
        bc.access_token_enc       = result["access_token_enc"]
        bc.refresh_token_enc      = result.get("refresh_token_enc") or ""
        bc.last_token_refresh     = datetime.utcnow()
        bc.is_connected           = True
        db.commit()
        return {"ok": True, "message": "✅ TOTP re-auth successful — token refreshed"}
    return {"ok": False, "message": result.get("message", "Re-auth failed")}

@app.delete("/api/brokers/{broker_id}")
def delete_broker(broker_id: str, user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    db.query(BrokerConnection).filter(
        BrokerConnection.user_id == user.id,
        BrokerConnection.broker_id == broker_id).delete()
    db.commit()
    return {"ok": True}

# ── Market data — real Fyers data ─────────────────────────────

@app.get("/api/market/live")
async def market_live(symbol: str = "NSE:NIFTY50-INDEX",
                      user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    """
    Returns this user's live market data from their personal cache.
    Cache is populated by background service using their own broker.
    If no broker connected — returns waiting status, not an error.
    """
    cache = _user_cache(user.id)

    # Return from cache if live
    if cache.get("spot") and cache.get("status") == "live":
        return {"ok":True, "spot":cache["spot"], "atm":cache["atm"],
                "chain":cache["chain"], "updated":cache["updated"],
                "status":"live"}

    # Cache not live — try direct fetch once
    conn = _get_fyers(user, db)
    if not conn:
        return {"ok":False,
                "spot":0, "atm":0, "chain":{},
                "status": cache.get("status","waiting"),
                "message": cache.get("message",
                    "Connect your broker in My Brokers to see live data.")}

    result = await conn.get_spot_and_chain(symbol)
    if result.get("ok"):
        if result.get("refresh_tokens"):
            _save_tokens(user.id, conn, result["refresh_tokens"], db)
        # Store in user cache
        import pytz
        user_market_cache[user.id] = {
            "spot":result["spot"], "atm":result["atm"],
            "chain":result["chain"],
            "updated":datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%H:%M:%S"),
            "status":"live",
            "message":f"Live · {datetime.now().strftime('%H:%M:%S')}",
        }
        return result

    # ── Market closed or options chain unavailable ────────────────
    # Fall back to profile + quotes to verify token is valid 24/7
    profile = await conn.get_profile()
    if not profile.get("ok"):
        return {"ok": False, "spot": 0, "atm": 0, "chain": {},
                "status": "error",
                "message": profile.get("message", "Token invalid — reconnect broker")}

    # Token is valid — try to get spot price via quotes endpoint
    spot = 0.0
    try:
        quotes = await conn.get_quotes([symbol])
        if quotes.get("ok"):
            q = quotes["quotes"].get(symbol, {})
            spot = q.get("ltp", 0.0) or q.get("prev_close", 0.0)
    except Exception:
        pass

    name = profile.get("data", {}).get("name", "")
    return {
        "ok": True,
        "spot": spot,
        "atm": 0,
        "chain": {},
        "status": "closed",
        "message": f"Broker connected ({name}) — market closed, options chain unavailable",
    }


@app.get("/api/market/status")
def market_status(
    symbol: str = "NSE:NIFTY50-INDEX",
    user: User = Depends(get_current_user)
):
    """Quick status check for this user's market data.
    Returns spot/ATM for the requested symbol if available in chain cache.
    """
    sym_short = {
        "NSE:NIFTY50-INDEX":    "NIFTY",
        "NSE:NIFTYBANK-INDEX":  "BANKNIFTY",
        "NSE:FINNIFTY-INDEX":   "FINNIFTY",
        "NSE:MIDCPNIFTY-INDEX": "MIDCAP NIFTY",
        "BSE:SENSEX-INDEX":     "SENSEX",
    }.get(symbol, symbol)

    # Real-time market open check — overrides stale cache status
    mkt_open = _market_open_now()

    # Check per-symbol cache first (multi-symbol support)
    sym_cache = (user_symbol_cache.get(user.id) or {}).get(symbol)
    if sym_cache:
        actual_status = "live" if mkt_open else "closed"
        msg = (f"Live · {sym_cache.get('updated', '')} IST" if mkt_open
               else f"Market closed · {_next_market_open_msg()}")
        return {
            "spot":    sym_cache.get("spot", 0),
            "atm":     sym_cache.get("atm", 0),
            "symbol":  symbol,
            "sym_short": sym_short,
            "chain":   sym_cache.get("chain", {}),
            "status":  actual_status,
            "message": msg,
            "updated": sym_cache.get("updated"),
        }

    # Fall back to primary NIFTY cache ONLY if this is the default symbol
    default_sym = "NSE:NIFTY50-INDEX"
    if symbol == default_sym:
        cache = _user_cache(user.id)
        # Override cached status with real-time check
        cached_status = cache.get("status", "waiting")
        if cached_status == "live" and not mkt_open:
            cached_status = "closed"
        return {
            "spot":    cache.get("spot", 0),
            "atm":     cache.get("atm", 0),
            "symbol":  symbol,
            "sym_short": sym_short,
            "chain":   cache.get("chain", {}),
            "status":  cached_status,
            "message": (cache.get("message", "Connect your broker to see live data.")
                        if mkt_open else f"Market closed · {_next_market_open_msg()}"),
            "updated": cache.get("updated"),
        }
    # For other symbols, return empty — no automation uses this symbol yet
    return {
        "spot":    0,
        "atm":     0,
        "symbol":  symbol,
        "sym_short": sym_short,
        "chain":   {},
        "status":  "waiting",
        "message": f"No active automation for {sym_short} — add one to see live data.",
        "updated": None,
    }


@app.get("/api/market/all-symbols")
def market_all_symbols(user: User = Depends(get_current_user)):
    """Returns live data for all symbols currently cached for this user."""
    sym_data = user_symbol_cache.get(user.id) or {}
    result = {}
    for sym, data in sym_data.items():
        reg = SYMBOL_REGISTRY.get(sym, {})
        result[sym] = {
            "spot":      data.get("spot", 0),
            "atm":       data.get("atm", 0),
            "updated":   data.get("updated"),
            "status":    data.get("status", "waiting"),
            "label":     reg.get("label", sym),
            "lot_size":  reg.get("lot_size", 65),
        }
    return {"symbols": result}

@app.get("/api/market/symbols")
def get_symbols(user: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    """Returns available symbols from user's connected broker."""
    bc = db.query(BrokerConnection).filter(
        BrokerConnection.user_id == user.id,
        BrokerConnection.is_connected == True).first()
    if bc:
        bd = db.query(BrokerDefinition).filter(
            BrokerDefinition.broker_id == bc.broker_id).first()
        if bd and bd.symbols:
            return {"symbols": bd.symbols}
    return {"symbols": NIFTY_SYMBOLS}

@app.get("/api/market/profile")
async def market_profile(user: User = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    """Test connection — works 24/7."""
    conn = _get_fyers(user, db)
    if not conn:
        return {"ok": False, "message": "No broker connected"}
    # Token is valid all day (until 3 AM IST); no refresh needed — just use it directly
    try:
        profile = await conn.get_profile()
        if profile.get("_error"):
            return {"ok": False, "connected": False, "message": profile["_error"]}
        return {"ok": True, "connected": True, "profile": profile.get("data", {}),
                "message": "Fyers connected ✓"}
    except Exception as e:
        return {"ok": False, "connected": False, "message": str(e)}

@app.get("/api/market/funds")
async def market_funds(user: User = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    """
    Fetch account balance from Fyers — works 24/7, not just market hours.
    Returns raw fund keys AND a parsed available_balance for easy display.
    """
    conn = _get_fyers(user, db)
    if not conn:
        return {"ok": False, "funds": {}, "available_balance": 0,
                "message": "No broker connected — add Fyers in My Brokers"}

    try:
        funds = await conn.get_funds()
    except Exception as e:
        return {"ok": False, "funds": {}, "available_balance": 0,
                "mode": conn.mode,
                "message": f"Could not reach Fyers: {str(e)}"}

    # get_funds() returns {"_error": "msg"} on failure — surface the real reason
    if funds.get("_error"):
        err = funds["_error"]
        # Return last cached balance if we have one
        cached = _funds_cache.get(user.id)
        if cached:
            return {
                "ok":               True,
                "mode":             conn.mode,
                "broker_connected": True,
                "funds":            cached["funds"],
                "available_balance": cached["available_balance"],
                "matched_key":      cached["matched_key"],
                "cached":           True,
                "cached_at":        cached["cached_at"],
                "message":          err,
            }
        return {"ok": False, "funds": {}, "available_balance": 0,
                "mode": conn.mode, "broker_connected": True,
                "message": err}

    # Parse available balance — try every key Fyers might use
    available = 0
    matched_key = None
    KNOWN_KEYS = [
        "Available Balance", "Available cash", "Available Cash",
        "Cash Available", "Clear Balance", "Net Balance",
        "Payin", "Total Balance", "Equity Amount",
        "available_balance", "availableBalance",
    ]
    for key in KNOWN_KEYS:
        val = funds.get(key, 0)
        if val and float(val) > 0:
            available = float(val)
            matched_key = key
            break

    # If no key matched, take the largest positive value
    if not available and funds:
        pos = {k: float(v) for k, v in funds.items()
               if isinstance(v, (int, float)) and float(v) > 0}
        if pos:
            matched_key = max(pos, key=pos.get)
            available = pos[matched_key]

    ist = pytz.timezone("Asia/Kolkata")
    cached_at = datetime.now(ist).strftime("%a %d %b, %I:%M %p")

    # Save to per-user funds cache so we can serve it outside market hours
    _funds_cache[user.id] = {
        "available_balance": round(available, 2),
        "funds":             funds,
        "matched_key":       matched_key,
        "cached_at":         cached_at,
        "mode":              conn.mode,
    }

    return {
        "ok":               True,
        "mode":             conn.mode,
        "broker_connected": True,
        "funds":            funds,
        "available_balance": round(available, 2),
        "matched_key":      matched_key,
        "cached":           False,
        "message":          f"Balance from '{matched_key}'" if matched_key
                            else ("Paper mode — automations use paper trading" if conn.mode == "paper"
                                  else "Could not parse balance from Fyers response"),
    }

# ── Strategies ────────────────────────────────────────────────

@app.get("/api/strategies")
def get_strategies(user: User = Depends(get_current_user)):
    return {"strategies": [
        {"code": "S7", "name": "All-Strike Iron Butterfly",
         "tier": "PRO", "auto": True,
         "description": "Fires when ALL 7 strikes break ORB low simultaneously. Highest conviction."},
        {"code": "S1", "name": "ORB Breakdown Sell",
         "tier": "PRO",
         "description": "Primary strategy. Any strike breaks ORB low. ATM priority."},
        {"code": "S2", "name": "VWAP Squeeze + EMA Cross",
         "tier": "PRO",
         "description": "S1 fallback. Premium tight below VWAP, EMA75 bearish, RSI<45."},
        {"code": "S8", "name": "Opening Gap Fade",
         "tier": "PRO",
         "description": "Gap >0.4% from prev close. Premium compressing."},
        {"code": "S3", "name": "Breakout Reversal",
         "tier": "PRO",
         "description": "Premium spikes above VWAP then reverses below."},
        {"code": "S4", "name": "Iron Condor",
         "tier": "PRO",
         "description": "Bollinger squeeze. Sell ±1, buy ±3 wings."},
        {"code": "S5", "name": "Ratio Spread",
         "tier": "PRO",
         "description": "Downtrend. Sell 2×ATM, buy 1×far OTM."},
        {"code": "S6", "name": "Theta Decay Strangle",
         "tier": "PRO",
         "description": "IV >65pct. Sell ±1, buy ±4 wings."},
        {"code": "S9", "name": "Pre-Expiry Theta Crush",
         "tier": "PRO",
         "description": "Expiry day only 11:00-12:00. Tight butterfly."},
        {"code": "S10", "name": "Gap Directional Buy",
         "tier": "PRO",
         "description": "Clear gap >1%. BUY ATM CE (gap-up) or PE (gap-down). Exit 11:00 AM."},
    ]}

# ── Automations ───────────────────────────────────────────────

@app.get("/api/automations")
def list_automations(user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    autos = db.query(Automation).filter(Automation.user_id == user.id).all()
    return {"automations": [
        {"id": a.id, "name": a.name, "symbol": a.symbol,
         "broker_id": a.broker_id, "strategies": a.strategies,
         "mode": a.mode, "status": a.status, "config": a.config,
         "shadow_mode": a.shadow_mode,
         "telegram_alerts": a.telegram_alerts,
         "is_running": bool(active_engines.get(user.id, {}).get(a.id)),
         "guard_status": getattr(active_engines.get(user.id, {}).get(a.id), "guard_status", "")}
        for a in autos]}

class SaveAutoReq(BaseModel):
    name: str
    symbol: str
    broker_id: str
    strategies: list = []
    mode: str = "paper"
    shadow_mode: bool = True
    telegram_alerts: bool = True
    config: dict = {}
    # max_trades_per_day stored in config: 1 (default), 2, 3, 0=unlimited

@app.post("/api/automations")
def save_automation(req: SaveAutoReq, user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    plan = get_plan(user)

    # Check live trading permission
    if req.mode == "live" and not plan["live_trading"]:
        raise HTTPException(403,
            "Live trading requires a paid plan. "
            "Upgrade to Starter or Pro in your profile.")

    # Check automation limit
    existing = db.query(Automation).filter(
        Automation.user_id == user.id).count()
    if existing >= plan["max_automations"]:
        raise HTTPException(403,
            f"Your {plan['label']} plan allows up to "
            f"{plan['max_automations']} automation(s). "
            f"Upgrade to add more.")

    # Check strategy permissions
    locked = [s for s in (req.strategies or [])
              if not check_plan_strategy(user, s)]
    if locked:
        raise HTTPException(403,
            f"Strategies {locked} require a higher plan. "
            f"Upgrade to Pro to unlock all strategies.")

    # Paper mode enforced for FREE plan
    mode = req.mode
    if not plan["live_trading"]:
        mode = "paper"

    a = Automation(user_id=user.id, name=req.name, symbol=req.symbol,
                   broker_id=req.broker_id, strategies=req.strategies,
                   mode=mode, shadow_mode=req.shadow_mode,
                   telegram_alerts=req.telegram_alerts,
                   config=req.config, status="IDLE")
    db.add(a); db.commit(); db.refresh(a)
    return {"ok": True, "id": a.id, "automation": {"id": a.id, "name": a.name}}

@app.put("/api/automations/{auto_id}")
def update_automation(auto_id: str, req: SaveAutoReq,
                      user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    a = db.query(Automation).filter(
        Automation.id == auto_id,
        Automation.user_id == user.id).first()
    if not a:
        raise HTTPException(404, "Automation not found")
    if a.status == "RUNNING":
        raise HTTPException(400, "Stop the automation before editing it")
    plan = get_plan(user)
    if req.mode == "live" and not plan["live_trading"]:
        raise HTTPException(403, "Live trading requires a paid plan.")
    locked = [s for s in (req.strategies or []) if not check_plan_strategy(user, s)]
    if locked:
        raise HTTPException(403, f"Strategies {locked} require a higher plan.")
    mode = req.mode if plan["live_trading"] else "paper"
    a.name = req.name
    a.symbol = req.symbol
    a.broker_id = req.broker_id
    a.strategies = req.strategies
    a.mode = mode
    a.shadow_mode = req.shadow_mode
    a.telegram_alerts = req.telegram_alerts
    a.config = req.config
    db.commit()
    return {"ok": True, "id": a.id}

@app.delete("/api/automations/{auto_id}")
async def delete_automation(auto_id: str, user: User = Depends(get_current_user),
                             db: Session = Depends(get_db)):
    # Stop this specific engine if running
    eng = _get_engine(user.id, auto_id)
    if eng and eng.is_running:
        eng.is_running = False
        await asyncio.sleep(1)  # Let engine loop notice
    _del_engine(user.id, auto_id)

    # Force-clear status on the automation regardless
    auto = db.query(Automation).filter(
        Automation.id == auto_id,
        Automation.user_id == user.id).first()
    if auto:
        auto.status = "IDLE"
        db.commit()

    # ── Fix FK constraint: null out automation_id in trade history ──
    # Preserves trade history but removes the reference blocking deletion
    db.query(Trade).filter(Trade.automation_id == auto_id).update(
        {"automation_id": None}, synchronize_session=False)
    db.query(ShadowTrade).filter(ShadowTrade.automation_id == auto_id).update(
        {"automation_id": None}, synchronize_session=False)
    db.commit()

    db.query(Automation).filter(
        Automation.id == auto_id,
        Automation.user_id == user.id).delete(synchronize_session=False)
    db.commit()
    return {"ok": True}

# ── Engine ────────────────────────────────────────────────────

active_engines: dict = {}      # user_id -> {auto_id: EngineState}
_engine_tasks: dict  = {}      # user_id -> {auto_id: asyncio.Task}
ws_clients: dict = {}          # user_id -> [WebSocket]

# ── Engine helpers ─────────────────────────────────────────────
def _get_engine(user_id: str, auto_id: str = None):
    """Get engine state. If auto_id given return that specific one, else first running."""
    engines = active_engines.get(user_id, {})
    if auto_id:
        return engines.get(auto_id)
    for st in engines.values():
        if st.is_running:
            return st
    return next(iter(engines.values()), None)

def _set_engine(user_id: str, auto_id: str, state):
    if user_id not in active_engines:
        active_engines[user_id] = {}
    active_engines[user_id][auto_id] = state

def _cancel_engine_task(user_id: str, auto_id: str):
    """Cancel the asyncio.Task for an engine if one exists."""
    task = (_engine_tasks.get(user_id) or {}).pop(auto_id, None)
    if task and not task.done():
        task.cancel()

def _del_engine(user_id: str, auto_id: str):
    _cancel_engine_task(user_id, auto_id)
    if user_id in active_engines:
        active_engines[user_id].pop(auto_id, None)
        if not active_engines[user_id]:
            del active_engines[user_id]

def _all_engines(user_id: str):
    return list(active_engines.get(user_id, {}).values())

@app.post("/api/engine/start")
async def start_engine(req: dict, user: User = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    auto_id = req.get("automation_id")
    auto = db.query(Automation).filter(
        Automation.id == auto_id,
        Automation.user_id == user.id).first()
    if not auto:
        raise HTTPException(404, "Automation not found")

    conn = _get_fyers(user, db)
    if not conn:
        raise HTTPException(400, "Fyers not connected. Go to My Brokers and connect first.")

    # Cancel any stale task for this automation before starting a fresh one
    _cancel_engine_task(user.id, auto_id)
    old_state = _get_engine(user.id, auto_id)
    if old_state:
        old_state.is_running = False

    config = {**auto.config, "strategies": auto.strategies, "mode": auto.mode}
    state  = EngineState(config)
    _set_engine(user.id, auto_id, state)

    task = asyncio.create_task(_run_engine(user.id, auto, state, conn, db))
    if user.id not in _engine_tasks:
        _engine_tasks[user.id] = {}
    _engine_tasks[user.id][auto_id] = task

    auto.status = "RUNNING"
    db.commit()
    return {"ok": True, "message": f"Engine started: {auto.name}"}

@app.post("/api/engine/stop")
async def stop_engine(req: dict = None, user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    auto_id = (req or {}).get("automation_id")

    if auto_id:
        # Stop only this specific automation — cancel task first, then clear state
        eng = _get_engine(user.id, auto_id)
        if eng:
            eng.is_running = False
        _del_engine(user.id, auto_id)   # also cancels task via _cancel_engine_task
        db.query(Automation).filter(
            Automation.id == auto_id,
            Automation.user_id == user.id
        ).update({"status": "IDLE"})
    else:
        # No specific id — stop ALL (emergency stop from Live Monitor)
        for eng in _all_engines(user.id):
            eng.is_running = False
        # Cancel all tasks for this user
        for aid in list((_engine_tasks.get(user.id) or {}).keys()):
            _cancel_engine_task(user.id, aid)
        if user.id in active_engines:
            del active_engines[user.id]
        db.query(Automation).filter(
            Automation.user_id == user.id,
            Automation.status == "RUNNING"
        ).update({"status": "IDLE"})

    db.commit()
    return {"ok": True}

@app.post("/api/engine/force-exit")
async def force_exit(user: User = Depends(get_current_user)):
    # Force exit on the engine that currently has an open position
    for eng in _all_engines(user.id):
        if eng.position:
            eng.position["force_exit"] = True
    return {"ok": True}

@app.get("/api/engine/status")
def engine_status(user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    state = _get_engine(user.id)
    if not state:
        return {"running": False, "mode": "IDLE", "engine_mode": None,
                "position": None, "day_pnl": 0}
    atm = state.atm
    # Also load today's paper/shadow trades for live monitor history
    today = datetime.now().strftime("%Y-%m-%d")
    shadow_today = db.query(ShadowTrade).filter(
        ShadowTrade.user_id == user.id,
        ShadowTrade.trade_date == today
    ).order_by(ShadowTrade.created_at.desc()).limit(10).all()
    shadow_history = [
        {"strategy": t.strategy_code, "entry": t.entry_combined,
         "exit": t.exit_combined, "pnl": t.net_pnl,
         "exit_reason": t.exit_reason, "is_open": t.is_open,
         "entry_time": t.entry_time.strftime("%H:%M") if t.entry_time else None,
         "exit_time": t.exit_time.strftime("%H:%M") if t.exit_time else None}
        for t in shadow_today
    ]
    return {
        "running":       True,
        "mode":          "IN_TRADE" if state.position else "MONITORING",
        "engine_mode":   state.config.get("mode", "paper"),
        "spot":          state.spot_history[-1] if state.spot_history else 0,
        "atm":           state.atm_strike,
        "combined":      atm.current if atm else 0,
        "vwap":          atm.vwap_val if atm else 0,
        "ema75":         atm.ema75 if atm else 0,
        "position":      state.position,
        "day_pnl":       state.day_pnl,
        "log":           state.log[-10:],
        "today_trades":  shadow_history,
        "guard_status":  getattr(state, "guard_status", ""),
        "events_suspended": getattr(state, "events_suspended", False),
        "ai_suspended":  getattr(state, "ai_suspended", False),
    }

# ── Trades ────────────────────────────────────────────────────

@app.get("/api/trades")
def get_trades(user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    trades = db.query(Trade).filter(Trade.user_id == user.id)\
               .order_by(Trade.created_at.desc()).limit(100).all()
    return {"trades": [
        {"id": t.id, "date": t.trade_date, "symbol": t.symbol,
         "strategy": t.strategy_code, "mode": t.mode,
         "atm": t.atm_strike, "entry": t.entry_combined,
         "exit": t.exit_combined, "pnl": t.net_pnl,
         "exit_reason": t.exit_reason, "is_open": t.is_open,
         "entry_time": t.entry_time.isoformat() if t.entry_time else None}
        for t in trades]}

@app.get("/api/trades/unified")
def get_unified_trades(
    days: int = 30,
    automation_id: str = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Unified trade history — live and paper combined, grouped by automation.
    Returns full entry/exit detail including signal reason, SL tracking,
    combined premium at every stage.
    """
    from datetime import timedelta
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")

    # Fetch live trades
    live_q = db.query(Trade).filter(
        Trade.user_id == user.id,
        Trade.trade_date >= since
    )
    if automation_id:
        live_q = live_q.filter(Trade.automation_id == automation_id)
    live_trades = live_q.order_by(Trade.entry_time.desc()).all()

    # Fetch paper trades from ShadowTrade
    paper_q = db.query(ShadowTrade).filter(
        ShadowTrade.user_id == user.id,
        ShadowTrade.trade_date >= since
    )
    if automation_id:
        paper_q = paper_q.filter(ShadowTrade.automation_id == automation_id)
    paper_trades = paper_q.order_by(ShadowTrade.entry_time.desc()).all()

    def _parse_reason(reason: str) -> dict:
        """Parse exit reason into human-readable parts."""
        if not reason: return {"type": "UNKNOWN", "detail": "", "friendly": "Unknown"}
        r = reason.upper()
        if "PROFIT_TARGET" in r:
            return {"type": "PROFIT_TARGET", "detail": reason,
                    "friendly": "✅ Profit target — premium decayed 50%",
                    "outcome": "WIN"}
        elif "PROFIT_LOCK" in r:
            return {"type": "PROFIT_LOCK", "detail": reason,
                    "friendly": "🔒 Profit lock — premium rebounded to locked level",
                    "outcome": "WIN"}
        elif "TRAILING_SL" in r:
            return {"type": "TRAILING_SL", "detail": reason,
                    "friendly": "🔄 Trailing SL — premium bounced from low",
                    "outcome": "MANAGED"}
        elif "VWAP_SL" in r or "VWAP SL" in r:
            return {"type": "VWAP_SL", "detail": reason,
                    "friendly": "📊 VWAP SL — premium rose above VWAP",
                    "outcome": "LOSS"}
        elif "MAX_LOSS" in r:
            return {"type": "MAX_LOSS", "detail": reason,
                    "friendly": "🛑 Max loss backstop hit",
                    "outcome": "LOSS"}
        elif "EMA75_SL" in r or "EMA75 SL" in r:
            return {"type": "EMA75_SL", "detail": reason,
                    "friendly": "📉 EMA75 SL — profit locked at EMA75 level",
                    "outcome": "LOSS"}
        elif "AUTO_EXIT" in r:
            return {"type": "AUTO_EXIT", "detail": reason,
                    "friendly": "⏰ Auto exit at scheduled time",
                    "outcome": "TIMED"}
        elif "MARKET_CLOSE" in r:
            return {"type": "MARKET_CLOSE", "detail": reason,
                    "friendly": "🔔 Market closed — position squared off",
                    "outcome": "TIMED"}
        elif "FORCE_EXIT" in r:
            return {"type": "FORCE_EXIT", "detail": reason,
                    "friendly": "⚡ Force exit triggered manually",
                    "outcome": "MANUAL"}
        else:
            return {"type": "OTHER", "detail": reason,
                    "friendly": reason, "outcome": "UNKNOWN"}

    def _format_live(t) -> dict:
        sig = t.signal_data or {}
        reason_parsed = _parse_reason(t.exit_reason)
        decay_pct = 0
        if t.entry_combined and t.exit_combined:
            decay_pct = round((1 - t.exit_combined / t.entry_combined) * 100, 1)
        return {
            "id":             t.id,
            "type":           "live",
            "automation_id":  t.automation_id,
            "date":           t.trade_date,
            "strategy":       t.strategy_code,
            "symbol":         t.symbol,
            "atm_strike":     t.atm_strike,
            # Entry detail
            "entry_combined": round(t.entry_combined or 0, 1),
            "entry_time":     (_to_ist(t.entry_time)) if t.entry_time else None,
            "entry_reason":   sig.get("reason", ""),
            "signal_name":    sig.get("name", ""),
            "hedge_width":    sig.get("hedge_width", 2),
            "sell_ce_strike": t.sell_ce_strike,
            "sell_pe_strike": t.sell_pe_strike,
            # Exit detail
            "exit_combined":  round(t.exit_combined or 0, 1) if t.exit_combined else None,
            "exit_time":      (_to_ist(t.exit_time)) if t.exit_time else None,
            "ai_insight":     getattr(t, 'ai_insight', '') or "",
            "exit_reason":    t.exit_reason,
            "exit_parsed":    reason_parsed,
            "decay_pct":      decay_pct,
            # P&L
            "lots":           t.lots,
            "lot_size":       t.lot_size,
            "qty":            (t.lots or 1) * (t.lot_size or 65),
            "gross_pnl":      round(t.gross_pnl or 0, 0),
            "brokerage":      round(t.brokerage or 0, 0),
            "net_pnl":        round(t.net_pnl or 0, 0),
            "is_open":        t.is_open,
            # Orders placed
            "orders":         t.orders or [],
        }

    def _format_paper(t) -> dict:
        sig = t.signal_data or {}
        sl  = t.sl_tracking or {}
        reason_parsed = _parse_reason(t.exit_reason)
        decay_pct = 0
        if t.entry_combined and t.exit_combined:
            decay_pct = round((1 - t.exit_combined / t.entry_combined) * 100, 1)
        return {
            "id":             t.id,
            "type":           "paper",
            "automation_id":  t.automation_id,
            "date":           t.trade_date,
            "strategy":       t.strategy_code,
            "symbol":         t.symbol,
            "atm_strike":     t.atm_strike,
            # Entry detail
            "entry_combined": round(t.entry_combined or 0, 1),
            "entry_time":     (_to_ist(t.entry_time)) if t.entry_time else None,
            "entry_reason":   sig.get("reason", ""),
            "signal_name":    sig.get("name", ""),
            "hedge_width":    sig.get("hedge_width", t.hedge_width or 2),
            "entry_spot":     round(t.entry_spot or 0, 0),
            # Exit detail
            "exit_combined":  round(t.exit_combined or 0, 1) if t.exit_combined else None,
            "exit_time":      (_to_ist(t.exit_time)) if t.exit_time else None,
            "exit_spot":      round(t.exit_spot, 0) if t.exit_spot else None,
            "ai_insight":     t.ai_insight or "",
            "exit_reason":    t.exit_reason,
            "exit_parsed":    reason_parsed,
            "decay_pct":      decay_pct,
            # SL tracking at exit
            "sl_at_exit":     {
                "vwap":         sl.get("vwap", 0),
                "ema75":        sl.get("ema75", 0),
                "trailing_low": sl.get("trailing_low", 0),
                "sl_type":      sl.get("sl_type", ""),
                "candles":      sl.get("candles", 0),
            },
            # P&L
            "lots":           t.lots,
            "lot_size":       t.lot_size,
            "qty":            (t.lots or 1) * (t.lot_size or 65),
            "gross_pnl":      round(t.gross_pnl or 0, 0),
            "brokerage":      round(t.brokerage or 0, 0),
            "net_pnl":        round(t.net_pnl or 0, 0),
            "max_profit":     round(t.max_profit or 0, 0),
            "max_loss":       round(t.max_loss or 0, 0),
            "is_open":        t.is_open,
        }

    # Combine and sort by entry_time desc
    all_trades = ([_format_live(t) for t in live_trades] +
                  [_format_paper(t) for t in paper_trades])
    all_trades.sort(key=lambda x: (x["date"], x["entry_time"] or ""), reverse=True)

    # Group by automation_id
    by_auto = {}
    autos = db.query(Automation).filter(
        Automation.user_id == user.id).all()
    auto_map = {a.id: a.name for a in autos}

    for t in all_trades:
        aid = t["automation_id"] or "manual"
        if aid not in by_auto:
            by_auto[aid] = {
                "automation_id":   aid,
                "automation_name": auto_map.get(aid, "Manual"),
                "trades":          [],
                "live_pnl":        0,
                "paper_pnl":       0,
                "total_trades":    0,
                "wins":            0,
            }
        by_auto[aid]["trades"].append(t)
        by_auto[aid]["total_trades"] += 1
        if not t["is_open"]:
            pnl = t["net_pnl"] or 0
            if t["type"] == "live":
                by_auto[aid]["live_pnl"] += pnl
            else:
                by_auto[aid]["paper_pnl"] += pnl
            if pnl > 0:
                by_auto[aid]["wins"] += 1

    for aid in by_auto:
        g = by_auto[aid]
        closed = [t for t in g["trades"] if not t["is_open"]]
        g["live_pnl"]  = round(g["live_pnl"], 0)
        g["paper_pnl"] = round(g["paper_pnl"], 0)
        g["win_rate"]  = round(g["wins"] / len(closed) * 100, 1) if closed else 0

    return {
        "trades":     all_trades,
        "by_auto":    list(by_auto.values()),
        "total":      len(all_trades),
        "live_count": len(live_trades),
        "paper_count": len(paper_trades),
    }


@app.get("/api/live/performance")
def live_performance(
    days: int = 30,
    automation_id: str = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Performance summary for live trades — mirrors shadow/performance structure."""
    from datetime import timedelta
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    q = db.query(Trade).filter(
        Trade.user_id == user.id,
        Trade.trade_date >= since,
        Trade.is_open == False,
        Trade.mode == "live"
    )
    if automation_id:
        q = q.filter(Trade.automation_id == automation_id)
    trades = q.all()

    if not trades:
        return {"total_trades":0,"total_pnl":0,"win_rate":0,"avg_pnl":0,
                "wins":0,"losses":0,"profit_factor":0,"avg_win":0,"avg_loss":0,
                "reward_risk":0,"expectancy":0,"max_drawdown":0,"max_consec_loss":0,
                "days_traded":0,"by_strategy":{},"by_day":[],"equity_curve":[],
                "exit_reasons":{},"best_day":None,"worst_day":None,"days":days}

    total_pnl = sum(t.net_pnl or 0 for t in trades)
    wins   = [t for t in trades if (t.net_pnl or 0) > 0]
    losses = [t for t in trades if (t.net_pnl or 0) <= 0]
    n = len(trades)

    by_strat = {}
    for t in trades:
        s = t.strategy_code
        if s not in by_strat:
            by_strat[s] = {"trades":0,"wins":0,"total_pnl":0}
        by_strat[s]["trades"] += 1
        by_strat[s]["total_pnl"] += t.net_pnl or 0
        if (t.net_pnl or 0) > 0: by_strat[s]["wins"] += 1
    for s in by_strat:
        nn = by_strat[s]["trades"]
        by_strat[s]["win_rate"] = round(by_strat[s]["wins"]/nn*100,1) if nn else 0
        by_strat[s]["avg_pnl"]  = round(by_strat[s]["total_pnl"]/nn,0) if nn else 0
        by_strat[s]["total_pnl"]= round(by_strat[s]["total_pnl"],0)

    day_map = {}
    for t in trades:
        d = t.trade_date
        if d not in day_map: day_map[d] = {"date":d,"trades":0,"pnl":0,"wins":0,"live":0,"paper":0}
        day_map[d]["trades"] += 1
        day_map[d]["pnl"]    += t.net_pnl or 0
        day_map[d]["live"]   += 1
        if (t.net_pnl or 0) > 0: day_map[d]["wins"] += 1
    by_day = sorted(day_map.values(), key=lambda x: x["date"])
    for d in by_day: d["pnl"] = round(d["pnl"],0)

    exit_reasons = {}
    for t in trades:
        r = (t.exit_reason or "UNKNOWN").split(" | ")[0].strip()
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

    equity, equity_curve = 0, []
    for d in by_day:
        equity += d["pnl"]; equity_curve.append({"date":d["date"],"equity":round(equity,0)})

    best_day  = max(by_day, key=lambda x: x["pnl"]) if by_day else None
    worst_day = min(by_day, key=lambda x: x["pnl"]) if by_day else None

    gross_profit = sum(t.net_pnl for t in wins)
    gross_loss   = abs(sum(t.net_pnl for t in losses)) if losses else 0
    profit_factor = round(gross_profit/gross_loss,2) if gross_loss else 99.0
    avg_win  = round(gross_profit/len(wins),0) if wins else 0
    avg_loss = round(-gross_loss/len(losses),0) if losses else 0
    win_rate = round(len(wins)/n*100, 1) if n else 0
    reward_risk = round(avg_win/abs(avg_loss),2) if avg_loss else 99.0
    expectancy = round((win_rate/100*avg_win) - ((1-win_rate/100)*abs(avg_loss)),0)

    consec = max_consec = 0
    for t in sorted(trades, key=lambda x: x.trade_date):
        if (t.net_pnl or 0) <= 0: consec += 1; max_consec = max(max_consec, consec)
        else: consec = 0

    peak = max_dd = running = 0
    for d in by_day:
        running += d["pnl"]
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)

    return {
        "total_trades":n, "total_pnl":round(total_pnl,0),
        "wins":len(wins), "losses":len(losses),
        "win_rate":round(win_rate,1), "avg_pnl":round(total_pnl/n,0),
        "profit_factor":profit_factor, "avg_win":avg_win, "avg_loss":avg_loss,
        "reward_risk":reward_risk, "expectancy":expectancy,
        "max_drawdown":round(max_dd,0), "max_consec_loss":max_consec,
        "days_traded":len(by_day),
        "by_strategy":by_strat, "by_day":by_day, "equity_curve":equity_curve,
        "exit_reasons":exit_reasons, "best_day":best_day, "worst_day":worst_day,
        "days":days,
    }


@app.get("/api/trades/summary")
def trades_summary(user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    trades = db.query(Trade).filter(Trade.user_id == user.id).all()
    closed = [t for t in trades if not t.is_open and t.net_pnl is not None]
    total  = sum(t.net_pnl for t in closed)
    wins   = sum(1 for t in closed if t.net_pnl > 0)
    return {
        "total_trades": len(closed),
        "total_pnl": round(total, 2),
        "wins": wins,
        "losses": len(closed) - wins,
        "win_rate": round(wins / len(closed) * 100, 1) if closed else 0,
        "open_trades": sum(1 for t in trades if t.is_open),
    }

# ── Admin ─────────────────────────────────────────────────────

@app.get("/api/admin/users")
def list_users(admin: User = Depends(require_admin),
               db: Session = Depends(get_db)):
    users = db.query(User).all()
    return {"users": [
        {"id": u.id, "email": u.email, "name": u.name,
         "role": u.role, "plan": u.plan,
         "is_active": u.is_active,
         "broker_count": db.query(BrokerConnection).filter(
             BrokerConnection.user_id == u.id).count(),
         "last_login": u.last_login.isoformat() if u.last_login else None,
         "created_at": u.created_at.isoformat() if u.created_at else None}
        for u in users
    ], "total": len(users)}

class CreateUserReq(BaseModel):
    email: str; name: str; password: str
    role: str = "USER"; plan: str = "FREE"

@app.post("/api/admin/users")
def create_user(req: CreateUserReq, request: Request, admin: User = Depends(require_admin),
                db: Session = Depends(get_db)):
    email = req.email.lower().strip()
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(400, "Email already exists")
    user = User(email=email, name=req.name,
                password_hash=bcrypt.hashpw(req.password.encode(), bcrypt.gensalt()).decode(),
                role=req.role, plan=req.plan,
                is_active=True, is_verified=True)
    db.add(user)
    db.commit()
    # Set must_change_password flag if password was provided by admin
    user.must_change_password = True
    db.commit()
    # Send welcome email if email is configured
    email_cfg = _get_email_config(db)
    if email_cfg.get("enabled") and req.password:
        domain = _resolve_domain(email_cfg, request)
        login_url = f"https://{domain}/"
        ok, err = _email_welcome(email_cfg, user.email, user.name, req.password, login_url)
        if not ok:
            logger.warning(f"Welcome email failed for {user.email}: {err}")
        else:
            logger.info(f"Welcome email sent to {user.email}")
    else:
        logger.info(f"Welcome email skipped for {user.email} — enabled:{email_cfg.get('enabled')} has_pass:{bool(req.password)}")
    return {"ok": True}

@app.post("/api/admin/users/{user_id}/suspend")
def suspend_user(user_id: str, admin: User = Depends(require_admin),
                 db: Session = Depends(get_db)):
    u = db.query(User).filter(User.id == user_id).first()
    if u: u.is_active = False; db.commit()
    return {"ok": True}

@app.post("/api/admin/users/{user_id}/activate")
def activate_user(user_id: str, admin: User = Depends(require_admin),
                  db: Session = Depends(get_db)):
    u = db.query(User).filter(User.id == user_id).first()
    if u: u.is_active = True; db.commit()
    return {"ok": True}

# ── AI (Gemini) Settings ──────────────────────────────────────────

@app.post("/api/ai/config")
async def save_ai_config(req: dict, user: User = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    """Save Gemini API key and AI preferences."""
    ai_cfg = dict(user.ai_config or {})
    key = req.get("api_key", "").strip()
    if key:
        ai_cfg["api_key_enc"] = _simple_encrypt(key)
    model = req.get("model", "").strip()
    if model:
        ai_cfg["model"] = model
    if "use_for_trading" in req:
        ai_cfg["use_for_trading"] = bool(req["use_for_trading"])
    if "use_for_analysis" in req:
        ai_cfg["use_for_analysis"] = bool(req["use_for_analysis"])
    if "news_suspend_enabled" in req:
        ai_cfg["news_suspend_enabled"] = bool(req["news_suspend_enabled"])
    if "news_risk_threshold" in req:
        ai_cfg["news_risk_threshold"] = req["news_risk_threshold"]
    user.ai_config = ai_cfg
    db.commit()
    return {"ok": True, "message": "AI settings saved",
            "key_set": bool(ai_cfg.get("api_key_enc", ""))}

@app.delete("/api/ai/config/key")
async def remove_ai_key(user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    """Remove stored Gemini API key."""
    cfg = dict(user.ai_config or {})
    cfg.pop("api_key_enc", None)
    user.ai_config = cfg
    db.commit()
    return {"ok": True}

@app.get("/api/ai/test")
async def test_ai_connection(user: User = Depends(get_current_user)):
    """Test Gemini API key — makes a minimal API call."""
    gemini, enabled = _get_gemini_client(user.ai_config or {})
    if not enabled or not gemini:
        return {"ok": False, "message": "No API key configured"}
    try:
        ai_cfg2 = user.ai_config or {}
        model_name = ai_cfg2.get("model", _GEMINI_MODEL)
        response = gemini.models.generate_content(model=model_name, contents="Reply with exactly: OK")
        return {"ok": True, "message": "Gemini connected \u2713 (" + response.text.strip()[:30] + ")"}
    except Exception as e:
        return {"ok": False, "message": str(e)}

@app.get("/api/ai/models")
def get_ai_models(user: User = Depends(get_current_user)):
    """Return available Gemini model options."""
    return {"models": [
        {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash", "note": "Free · Recommended · Confirmed working"},
        {"id": "gemini-2.5-pro",   "label": "Gemini 2.5 Pro",   "note": "Paid · Highest quality"},
    ]}


@app.post("/api/automations/reset-status")
async def reset_all_automation_status(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Force-reset all RUNNING automations to IDLE for this user.
    Safe to call any time — only affects DB status, not running engines.
    Use when automations are stuck in RUNNING state after a server restart.
    """
    updated = db.query(Automation).filter(
        Automation.user_id == user.id,
        Automation.status == "RUNNING"
    ).update({"status": "IDLE"}, synchronize_session=False)
    db.commit()
    # Also stop any active engine for this user
    for eng in _all_engines(user.id):
        eng.is_running = False
    if user.id in active_engines:
        del active_engines[user.id]
    return {"ok": True, "reset_count": updated,
            "message": f"{updated} automation(s) reset to IDLE"}


@app.post("/api/admin/users/{user_id}/set-plan")
def admin_set_plan(user_id: str, req: dict,
                   admin: User = Depends(require_admin),
                   db: Session = Depends(get_db)):
    """Admin: change a user's plan (FREE/PRO)."""
    u = db.query(User).filter(User.id == user_id).first()
    if not u: raise HTTPException(404, "User not found")
    new_plan = req.get("plan", "FREE")
    if new_plan not in ("FREE", "PRO"):
        raise HTTPException(400, f"Invalid plan: {new_plan}")
    u.plan = new_plan
    db.commit()
    return {"ok": True, "plan": new_plan}

LIFECYCLE_STAGES = ["DEMO", "PAPER", "LIVE_READY", "PAID"]

@app.post("/api/admin/users/{user_id}/lifecycle")
def admin_set_lifecycle(user_id: str, req: dict,
                        admin: User = Depends(require_admin),
                        db: Session = Depends(get_db)):
    """Admin: move user through the customer journey pipeline."""
    u = db.query(User).filter(User.id == user_id).first()
    if not u: raise HTTPException(404, "User not found")
    stage = req.get("stage", "DEMO")
    if stage not in LIFECYCLE_STAGES:
        raise HTTPException(400, f"Invalid stage: {stage}. Use one of {LIFECYCLE_STAGES}")
    u.lifecycle_stage = stage
    # When confirmed PAID → automatically grant PRO access
    if stage == "PAID" and u.plan == "FREE":
        u.plan = "PRO"
    # When moved back to DEMO/PAPER → strip live access (drop to FREE)
    if stage in ("DEMO", "PAPER") and u.plan == "PRO":
        u.plan = "FREE"
    db.commit()
    return {"ok": True, "stage": stage, "plan": u.plan}

@app.delete("/api/trades/reset")
def reset_trade_history(
    trade_type: str = "paper",   # "paper", "live", or "all"
    days: int = None,              # None = all history
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    User-initiated reset of their own trade history.
    trade_type: 'paper' = shadow_trades only
                'live'  = trades table only (mode='live')
                'all'   = both tables
    days: if set, only delete trades older than N days
    Never touches other users or other data.
    """
    from datetime import timedelta
    deleted = {"paper": 0, "live": 0}

    cutoff = None
    if days:
        cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")

    if trade_type in ("paper", "all"):
        q = db.query(ShadowTrade).filter(ShadowTrade.user_id == user.id)
        if cutoff:
            q = q.filter(ShadowTrade.trade_date <= cutoff)
        deleted["paper"] = q.count()
        q.delete(synchronize_session=False)

    if trade_type in ("live", "all"):
        q = db.query(Trade).filter(Trade.user_id == user.id)
        if cutoff:
            q = q.filter(Trade.trade_date <= cutoff)
        deleted["live"] = q.count()
        q.delete(synchronize_session=False)

    db.commit()
    return {
        "ok":      True,
        "deleted": deleted,
        "message": (f"Deleted {deleted['paper']} paper trades and "
                    f"{deleted['live']} live trades")
    }


@app.post("/api/admin/users/{user_id}/reset-password")
def admin_reset_pw(user_id: str, request: Request, admin: User = Depends(require_admin),
                   db: Session = Depends(get_db)):
    u = db.query(User).filter(User.id == user_id).first()
    if not u: raise HTTPException(404, "User not found")
    token = secrets.token_urlsafe(32)
    db.add(ResetToken(user_id=u.id, token=token,
                      expires_at=datetime.utcnow() + timedelta(hours=24)))
    db.commit()
    email_cfg = _get_email_config(db)
    domain = _resolve_domain(email_cfg, request)
    return {"ok": True, "reset_url": f"https://{domain}/?reset_token={token}"}

def _email_invite_link(to_email: str, invite_url: str, plan_label: str, db=None):
    """Send invite link to a prospective user."""
    try:
        cfg = _get_email_config(db) if db else {}
        if not cfg or not cfg.get("enabled"):
            return
        subject = "You're invited to AlgoDesk"
        html = f"""
        <div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:24px">
          <h2 style="color:#4f8ef7">You're invited to AlgoDesk</h2>
          <p>You've been invited to join AlgoDesk — an automated options trading platform.</p>
          <div style="background:#f4f4f4;border-radius:8px;padding:16px;margin:16px 0">
            <div><strong>Plan:</strong> {plan_label}</div>
          </div>
          <a href="{invite_url}" style="display:inline-block;background:#4f8ef7;color:#fff;padding:10px 24px;border-radius:6px;text-decoration:none;margin-top:8px">Create Your Account →</a>
          <p style="margin-top:16px;font-size:12px;color:#999">This link is unique to you — do not share it.</p>
        </div>
        """
        _send_email(cfg, to_email, subject, html)
    except Exception as e:
        logger.warning(f"Invite email failed to {to_email}: {e}")

@app.post("/api/admin/invite")
def create_invite(req: dict, request: Request, admin: User = Depends(require_admin),
                  db: Session = Depends(get_db)):
    token = secrets.token_urlsafe(24)
    plan  = req.get("plan", "FREE")
    db.add(InviteLink(token=token, created_by=admin.id,
                      role=req.get("role", "USER"),
                      plan=plan))
    db.commit()
    email_cfg  = _get_email_config(db)
    domain     = _resolve_domain(email_cfg, request)
    invite_url = f"https://{domain}/?invite={token}"
    # Optionally email the invite directly
    to_email   = (req.get("email") or "").strip()
    emailed    = False
    if to_email:
        plan_label = "Pro — Live Trading (₹5,000/mo)" if plan == "PRO" else "Free — Paper Trading"
        _email_invite_link(to_email, invite_url, plan_label, db)
        emailed = True
    return {"ok": True, "invite_url": invite_url, "token": token, "emailed": emailed}

@app.get("/api/admin/stats")
def admin_stats(admin: User = Depends(require_admin),
                db: Session = Depends(get_db)):
    users = db.query(User).all()
    active = [u for u in users if u.is_active]

    # Pipeline: count users at each lifecycle stage
    pipeline = {s: sum(1 for u in active if getattr(u, "lifecycle_stage", "DEMO") == s)
                for s in LIFECYCLE_STAGES}

    # MRR = only users with stage=PAID (confirmed payment)
    # Use recorded subscription_amount if set, otherwise configured platform price
    default_price = _get_subscription_price(db)
    PLAN_PRICE = {"FREE": 0, "STARTER": default_price, "PRO": default_price}
    paid_users  = [u for u in active if getattr(u, "lifecycle_stage", "DEMO") == "PAID"]
    mrr = sum((u.subscription_amount or default_price) for u in paid_users)
    plan_counts = {p: sum(1 for u in active if u.plan == p)
                   for p in ["FREE", "STARTER", "PRO"]}

    # Trade counts
    from datetime import timedelta
    today = datetime.utcnow().strftime("%Y-%m-%d")
    trades_today = db.query(Trade).filter(Trade.trade_date == today).count()
    shadow_today = db.query(ShadowTrade).filter(ShadowTrade.trade_date == today).count()

    # Running engines
    running = sum(1 for engs in active_engines.values() for eng in engs.values() if eng.is_running)

    return {
        "total_users":       len(users),
        "active_users":      len(active),
        "total_brokers":     db.query(BrokerConnection).filter(
            BrokerConnection.is_connected == True).count(),
        "total_automations": db.query(Automation).count(),
        "running_engines":   running,
        "trades_today":      trades_today,
        "shadow_today":      shadow_today,
        "plans":             plan_counts,
        "pipeline":          pipeline,
        "paid_count":        len(paid_users),
        "mrr":               mrr,
        "arr":               mrr * 12,
        "plan_price":        PLAN_PRICE,
    }

@app.get("/api/admin/user-performance")
def admin_user_performance(admin: User = Depends(require_admin),
                            db: Session = Depends(get_db)):
    """
    Per-user paper + live trade performance summary for the admin panel.
    Returns each user's P&L, win rate, trade count, strategies used, last active.
    """
    users = db.query(User).order_by(User.created_at.desc()).all()
    import pytz
    ist = pytz.timezone("Asia/Kolkata")
    today = datetime.now(ist).strftime("%Y-%m-%d")
    week_ago  = (datetime.now(ist) - timedelta(days=7)).strftime("%Y-%m-%d")
    month_ago = (datetime.now(ist) - timedelta(days=30)).strftime("%Y-%m-%d")

    default_price = _get_subscription_price(db)
    PLAN_PRICE = {"FREE": 0, "STARTER": default_price, "PRO": default_price}
    current_month = datetime.now(ist).strftime("%Y-%m")

    result = []
    platform_paper_pnl   = 0.0
    platform_live_pnl    = 0.0
    platform_paper_total = 0
    platform_live_total  = 0
    platform_sub_revenue = 0.0   # sum of all recorded subscription payments

    for u in users:
        # ── Paper trades (shadow_trades) ──
        paper_all = db.query(ShadowTrade).filter(
            ShadowTrade.user_id == u.id,
            ShadowTrade.is_open == False
        ).all()
        paper_today = [t for t in paper_all if t.trade_date == today]
        paper_week  = [t for t in paper_all if t.trade_date >= week_ago]
        paper_month = [t for t in paper_all if t.trade_date >= month_ago]

        paper_pnl_today = sum(t.net_pnl or 0 for t in paper_today)
        paper_pnl_week  = sum(t.net_pnl or 0 for t in paper_week)
        paper_pnl_total = sum(t.net_pnl or 0 for t in paper_all)
        paper_wins  = sum(1 for t in paper_all if (t.net_pnl or 0) > 0)
        paper_n     = len(paper_all)
        paper_win_rate = round(paper_wins / paper_n * 100, 1) if paper_n else 0
        paper_strategies = list({t.strategy_code for t in paper_all if t.strategy_code})

        # ── Live trades ──
        live_all = db.query(Trade).filter(
            Trade.user_id == u.id,
            Trade.is_open == False
        ).all()
        live_today = [t for t in live_all if t.trade_date == today]
        live_week  = [t for t in live_all if t.trade_date >= week_ago]
        live_month = [t for t in live_all if t.trade_date >= month_ago]

        live_pnl_today = sum(t.net_pnl or 0 for t in live_today)
        live_pnl_week  = sum(t.net_pnl or 0 for t in live_week)
        live_pnl_total = sum(t.net_pnl or 0 for t in live_all)
        live_wins  = sum(1 for t in live_all if (t.net_pnl or 0) > 0)
        live_n     = len(live_all)
        live_win_rate = round(live_wins / live_n * 100, 1) if live_n else 0
        live_strategies = list({t.strategy_code for t in live_all if t.strategy_code})

        # ── Broker connected? ──
        broker = db.query(BrokerConnection).filter(
            BrokerConnection.user_id == u.id,
            BrokerConnection.is_connected == True
        ).first()

        # ── Running automations ──
        running_autos = sum(1 for engs in [active_engines.get(u.id, {})] for eng in engs.values() if eng.is_running)
        total_autos   = db.query(Automation).filter(Automation.user_id == u.id).count()

        # ── Last active trade ──
        last_paper = max((t.trade_date for t in paper_all), default=None)
        last_live  = max((t.trade_date for t in live_all), default=None)
        last_active = max(filter(None, [last_paper, last_live]), default=None)

        # ── Monthly P&L breakdown — last 6 months ──
        monthly: dict = {}
        for i in range(6):
            mo = datetime.now(ist) - timedelta(days=30 * i)
            key = mo.strftime("%Y-%m")
            monthly[key] = {"label": mo.strftime("%b %Y"), "paper": 0.0, "live": 0.0,
                            "paper_trades": 0, "live_trades": 0}
        for t in paper_all:
            mo = t.trade_date[:7] if t.trade_date and len(t.trade_date) >= 7 else None
            if mo and mo in monthly:
                monthly[mo]["paper"] += float(t.net_pnl or 0)
                monthly[mo]["paper_trades"] += 1
        for t in live_all:
            mo = t.trade_date[:7] if t.trade_date and len(t.trade_date) >= 7 else None
            if mo and mo in monthly:
                monthly[mo]["live"] += float(t.net_pnl or 0)
                monthly[mo]["live_trades"] += 1
        monthly_list = [{"month": k, **v} for k, v in sorted(monthly.items(), reverse=True)]

        # ── Subscription status ──
        lifecycle_now = getattr(u, "lifecycle_stage", "DEMO") or "DEMO"
        sub_expires = getattr(u, "subscription_expires_at", None)
        days_left   = None
        if lifecycle_now != "PAID":
            sub_status = lifecycle_now.lower()   # "demo" | "paper" | "live_ready"
        elif sub_expires:
            days_left  = (sub_expires - datetime.utcnow()).days
            sub_status = "expired" if days_left < 0 else "expiring" if days_left <= 7 else "active"
        else:
            sub_status = "active"

        # ── Value / ROI metrics ──
        # plan_price  = expected monthly charge for this plan
        # amount_paid = what was actually recorded (manual now, auto later from payment gateway)
        # Falls back to plan_price if no payment recorded yet
        plan_price  = PLAN_PRICE.get(u.plan, 0)
        amount_paid = getattr(u, "subscription_amount", 0) or 0
        effective_monthly_cost = amount_paid if amount_paid > 0 else plan_price

        # This month P&L
        cm_data           = monthly.get(current_month, {"paper": 0.0, "live": 0.0})
        this_month_paper  = round(cm_data["paper"], 2)
        this_month_live   = round(cm_data["live"], 2)
        this_month_net    = round(this_month_paper + this_month_live, 2)

        # ROI = P&L ÷ subscription cost  (None if free / no cost)
        def _roi(pnl, cost):
            if not cost: return None
            return round(pnl / cost, 2)

        # Estimated months active (for all-time ROI)
        months_active = max(1, (datetime.utcnow() - u.created_at).days // 30)
        est_total_paid = effective_monthly_cost * months_active

        # Lifecycle stage
        lifecycle = getattr(u, "lifecycle_stage", "DEMO") or "DEMO"

        # Onboarding step (1-6) — computed from activity
        _ob_step = 1
        if broker:                      _ob_step = 2
        if paper_n > 0:                 _ob_step = 3
        if paper_n >= 20:               _ob_step = 4
        _has_live_auto = db.query(Automation).filter(
            Automation.user_id == u.id, Automation.mode == "live"
        ).count() > 0
        if _has_live_auto:              _ob_step = 5
        if live_pnl_total > 0:         _ob_step = 6

        platform_paper_pnl   += paper_pnl_total
        platform_live_pnl    += live_pnl_total
        platform_paper_total += paper_n
        platform_live_total  += live_n
        # Revenue only from confirmed-PAID users
        if lifecycle == "PAID":
            platform_sub_revenue += amount_paid

        result.append({
            "user_id":        u.id,
            "name":           u.name,
            "email":          u.email,
            "plan":           u.plan,
            "role":           u.role,
            "is_active":      u.is_active,
            "lifecycle_stage": lifecycle,
            "broker_connected": bool(broker),
            "broker_mode": broker.mode if broker else "—",
            "automations": {"total": total_autos, "running": running_autos},
            "onboarding_step": _ob_step,
            "paper": {
                "total_trades": paper_n,
                "today":        len(paper_today),
                "week":         len(paper_week),
                "month":        len(paper_month),
                "pnl_today":    round(paper_pnl_today, 2),
                "pnl_week":     round(paper_pnl_week, 2),
                "pnl_month":    round(paper_pnl_total, 2),
                "pnl_total":    round(paper_pnl_total, 2),
                "win_rate":     paper_win_rate,
                "strategies":   sorted(paper_strategies),
            },
            "live": {
                "total_trades": live_n,
                "today":        len(live_today),
                "week":         len(live_week),
                "month":        len(live_month),
                "pnl_today":    round(live_pnl_today, 2),
                "pnl_week":     round(live_pnl_week, 2),
                "pnl_month":    round(live_pnl_total, 2),
                "pnl_total":    round(live_pnl_total, 2),
                "win_rate":     live_win_rate,
                "strategies":   sorted(live_strategies),
            },
            "monthly":    monthly_list,
            "subscription": {
                "expires_at": sub_expires.strftime("%Y-%m-%d") if sub_expires else None,
                "days_left":  days_left,
                "status":     sub_status,
                "amount":     getattr(u, "subscription_amount", 0) or 0,
                "notes":      getattr(u, "subscription_notes", "") or "",
            },
            "last_active": last_active,
            "value": {
                # What the user pays
                "plan_price":            plan_price,           # listed price for their plan
                "amount_paid":           amount_paid,          # actual last payment (0 = not yet recorded)
                "effective_monthly_cost": effective_monthly_cost,
                "months_active":         months_active,
                "est_total_paid":        round(est_total_paid, 2),
                # What they've earned this calendar month
                "this_month_paper":      this_month_paper,
                "this_month_live":       this_month_live,
                "this_month_net":        this_month_net,
                # ROI — this month
                "roi_this_month":        _roi(this_month_net, effective_monthly_cost),
                "roi_paper_this_month":  _roi(this_month_paper, effective_monthly_cost),
                # ROI — all time (paper + live vs estimated total paid)
                "alltime_net_pnl":       round(paper_pnl_total + live_pnl_total, 2),
                "roi_alltime":           _roi(paper_pnl_total + live_pnl_total, est_total_paid),
                # Value flag: are they making more than they're paying?
                "is_profitable":         this_month_net > effective_monthly_cost if effective_monthly_cost else None,
            },
        })

    return {
        "users": result,
        "platform": {
            "paper_pnl_total":   round(platform_paper_pnl, 2),
            "live_pnl_total":    round(platform_live_pnl, 2),
            "paper_trades":      platform_paper_total,
            "live_trades":       platform_live_total,
            "total_pnl":         round(platform_paper_pnl + platform_live_pnl, 2),
            "sub_revenue":       round(platform_sub_revenue, 2),
            # Value delivered vs revenue collected
            "platform_roi":      round((platform_paper_pnl + platform_live_pnl) / platform_sub_revenue, 2)
                                 if platform_sub_revenue else None,
        }
    }

# ── Admin Email Config ────────────────────────────────────────

class SubscriptionReq(BaseModel):
    expires_at: str = None   # YYYY-MM-DD
    amount: float  = None    # INR paid
    notes:  str    = None    # payment ref / notes
    plan:   str    = None    # optionally update plan too

@app.post("/api/admin/users/{user_id}/subscription")
def set_subscription(user_id: str, req: SubscriptionReq,
                     admin: User = Depends(require_admin),
                     db: Session = Depends(get_db)):
    """Admin: manually record subscription expiry, amount paid and notes."""
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(404, "User not found")
    if req.expires_at:
        try:
            u.subscription_expires_at = datetime.strptime(req.expires_at, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "expires_at must be YYYY-MM-DD")
    if req.amount is not None:
        u.subscription_amount = req.amount
    if req.notes is not None:
        u.subscription_notes = req.notes
    if req.plan and req.plan in ("FREE", "PRO"):
        u.plan = req.plan
    db.commit()
    return {"ok": True, "message": "Subscription updated"}

@app.get("/api/admin/subscription-overview")
def subscription_overview(admin: User = Depends(require_admin),
                           db: Session = Depends(get_db)):
    """Platform-level subscription health — pipeline stages, active, expiring soon, expired."""
    users = db.query(User).filter(User.is_active == True).all()
    now   = datetime.utcnow()
    active_count = expiring = expired = 0
    expiring_list: list = []
    expired_list:  list = []
    # Pipeline counts — one bucket per lifecycle stage
    pipeline = {s: 0 for s in LIFECYCLE_STAGES}
    mrr = 0.0
    _default_price = _get_subscription_price(db)
    for u in users:
        stage = getattr(u, "lifecycle_stage", "DEMO") or "DEMO"
        if stage in pipeline:
            pipeline[stage] += 1
        # MRR only from PAID
        if stage == "PAID":
            mrr += (u.subscription_amount or _default_price)
        # Expiry alerts only matter for PAID users
        if stage != "PAID":
            continue
        if not u.subscription_expires_at:
            active_count += 1
            continue
        days = (u.subscription_expires_at - now).days
        if days < 0:
            expired += 1
            expired_list.append({"name": u.name, "email": u.email, "plan": u.plan,
                                  "days": days})
        elif days <= 7:
            expiring += 1
            expiring_list.append({"name": u.name, "email": u.email, "plan": u.plan,
                                   "days": days,
                                   "expires": u.subscription_expires_at.strftime("%d %b %Y")})
        else:
            active_count += 1
    return {
        "pipeline":      pipeline,
        "total_paid":    pipeline.get("PAID", 0),
        "active":        active_count,
        "expiring_soon": expiring_list,
        "expired":       expired_list,
        "free":          pipeline.get("DEMO", 0) + pipeline.get("PAPER", 0),
        "mrr":           mrr,
        "alerts":        expiring + expired,
    }

@app.get("/api/admin/email-config")
def get_email_config(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Get current email/SMTP configuration."""
    cfg = _get_email_config(db)
    # Mask password in response
    safe = {k: v for k, v in cfg.items() if k != "smtp_password_enc"}
    safe["smtp_password_set"] = bool(cfg.get("smtp_password_enc"))
    return {"ok": True, "config": safe}

@app.post("/api/admin/email-config")
def save_email_config(req: dict, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Save email/SMTP configuration. Encrypts the password."""
    cfg = dict(req)
    # Encrypt password if provided
    if cfg.get("smtp_password"):
        cfg["smtp_password_enc"] = encrypt("_server", cfg.pop("smtp_password"))
    else:
        cfg.pop("smtp_password", None)
        # Keep existing encrypted password if not changing
        existing = _get_email_config(db)
        if existing.get("smtp_password_enc"):
            cfg["smtp_password_enc"] = existing["smtp_password_enc"]

    row = db.query(ServerSettings).filter(ServerSettings.key == "email_config").first()
    if row:
        row.value = cfg
        row.updated_at = datetime.utcnow()
    else:
        import uuid as _uuid
        row = ServerSettings(id=str(_uuid.uuid4()), key="email_config", value=cfg)
        db.add(row)
    db.commit()
    return {"ok": True, "message": "Email configuration saved"}

@app.get("/api/admin/subscription-price")
def get_subscription_price(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    row = db.query(ServerSettings).filter(ServerSettings.key == "subscription_price").first()
    price = (row.value or {}).get("price", 5000) if row else 5000
    return {"price": price}

@app.post("/api/admin/subscription-price")
def save_subscription_price(req: dict, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    price = int(req.get("price", 5000))
    row = db.query(ServerSettings).filter(ServerSettings.key == "subscription_price").first()
    if row:
        row.value = {"price": price}
        row.updated_at = datetime.utcnow()
    else:
        import uuid as _uuid
        row = ServerSettings(id=str(_uuid.uuid4()), key="subscription_price", value={"price": price})
        db.add(row)
    db.commit()
    return {"ok": True, "price": price}

def _get_subscription_price(db) -> int:
    """Read the platform-wide default subscription price from ServerSettings."""
    row = db.query(ServerSettings).filter(ServerSettings.key == "subscription_price").first()
    return (row.value or {}).get("price", 5000) if row else 5000

@app.post("/api/admin/email-config/test")
def test_email_config(req: dict, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Send a test email to verify SMTP config."""
    cfg = _get_email_config(db)
    if not cfg.get("enabled"):
        raise HTTPException(400, "Email not enabled — save config first")
    to = req.get("to") or admin.email
    ok, err = _send_email(cfg, to,
        "AlgoDesk — Test Email",
        f"<div style='font-family:sans-serif;padding:24px'><h2 style='color:#4f8ef7'>AlgoDesk Email Working</h2><p>SMTP is configured correctly. Sent to: {to}</p></div>"
    )
    if ok:
        return {"ok": True, "message": f"Test email sent to {to}"}
    raise HTTPException(500, f"Email failed: {err}")

# ── Telegram ──────────────────────────────────────────────────

async def _send_telegram(bot_token: str, chat_id: str, msg: str):
    if not bot_token or not chat_id: return
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": f"⬡ ALGO-DESK\n\n{msg}"})
    except Exception as e:
        log.error(f"Telegram: {e}")

async def _send_telegram_all(user: User, msg: str, account_ids: list = None):
    """Send to Telegram accounts for this user.
    account_ids: if non-empty list, only send to those account IDs.
                 If None or empty, send to all active accounts.
    """
    sent = 0
    filter_ids = account_ids if account_ids else None
    for acct in (user.telegram_accounts or []):
        if filter_ids and acct.get("id") not in filter_ids:
            continue
        if acct.get("active") and acct.get("token") and acct.get("chat"):
            await _send_telegram(acct["token"], acct["chat"], msg)
            sent += 1
    # Legacy single account fallback (only when no account_ids filter)
    if sent == 0 and not filter_ids and user.telegram_token and user.telegram_chat:
        await _send_telegram(user.telegram_token, user.telegram_chat, msg)

import httpx

@app.get("/api/telegram/accounts")
def get_telegram_accounts(user: User = Depends(get_current_user)):
    accounts = user.telegram_accounts or []
    # Include legacy as first account if exists and not already in list
    if user.telegram_token and user.telegram_chat and not accounts:
        accounts = [{"id":"legacy","name":"Default",
                     "token":user.telegram_token,
                     "chat":user.telegram_chat,"active":True}]
    return {"accounts": accounts}

class TelegramAccountReq(BaseModel):
    name: str
    token: str
    chat: str
    active: bool = True

@app.post("/api/telegram/accounts")
def add_telegram_account(req: TelegramAccountReq,
                         user: User = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    accounts = list(user.telegram_accounts or [])
    accounts.append({"id": str(__import__("uuid").uuid4())[:8], "name": req.name,
                     "token": req.token, "chat": req.chat,
                     "active": req.active})
    user.telegram_accounts = accounts
    db.commit()
    return {"ok": True, "accounts": accounts}

@app.delete("/api/telegram/accounts/{acct_id}")
def delete_telegram_account(acct_id: str,
                             user: User = Depends(get_current_user),
                             db: Session = Depends(get_db)):
    user.telegram_accounts = [a for a in (user.telegram_accounts or [])
                               if a.get("id") != acct_id]
    db.commit()
    return {"ok": True}

@app.put("/api/telegram/accounts/{acct_id}")
def toggle_telegram_account(acct_id: str, req: dict,
                             user: User = Depends(get_current_user),
                             db: Session = Depends(get_db)):
    accounts = list(user.telegram_accounts or [])
    for a in accounts:
        if a.get("id") == acct_id:
            a["active"] = req.get("active", True)
    user.telegram_accounts = accounts
    db.commit()
    return {"ok": True}

@app.post("/api/telegram/test/{acct_id}")
async def test_telegram_account(acct_id: str,
                                user: User = Depends(get_current_user)):
    accounts = user.telegram_accounts or []
    acct = next((a for a in accounts if a.get("id") == acct_id), None)
    if not acct:
        # Try legacy
        if acct_id == "legacy" and user.telegram_token:
            await _send_telegram(user.telegram_token, user.telegram_chat,
                f"✅ Test\nHello {user.name}! This account is working.")
            return {"ok": True}
        raise HTTPException(404, "Account not found")
    await _send_telegram(acct["token"], acct["chat"],
        f"✅ Test from ALGO-DESK\nHello {user.name}!\nAccount [{acct['name']}] is working.")
    return {"ok": True}

@app.post("/api/telegram/test")
async def test_telegram(user: User = Depends(get_current_user)):
    if not user.telegram_token or not user.telegram_chat:
        raise HTTPException(400, "Set Telegram bot token and chat ID in profile first")
    await _send_telegram(user.telegram_token, user.telegram_chat,
        f"✅ Test successful\nHello {user.name}! Alerts are working.")
    return {"ok": True}


@app.post("/api/telegram/set-webhook")
async def set_telegram_webhook(req: dict, user: User = Depends(get_current_user)):
    """Register webhook URL with Telegram so bot can receive commands."""
    # Support acct_id to look up token from accounts list
    acct_id = req.get("acct_id")
    if acct_id:
        acct = next((a for a in (user.telegram_accounts or []) if a.get("id") == acct_id), None)
        bot_token = acct["token"] if acct else None
    else:
        bot_token = req.get("bot_token") or user.telegram_token
    webhook_url = req.get("webhook_url")
    if not bot_token:
        raise HTTPException(400, "No bot token — add in Profile → Telegram first")
    if not webhook_url:
        raise HTTPException(400, "webhook_url required")
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"https://api.telegram.org/bot{bot_token}/setWebhook",
                json={"url": webhook_url, "allowed_updates": ["message"]})
            data = r.json()
        if data.get("ok"):
            return {"ok": True, "message": "Webhook registered ✓"}
        return {"ok": False, "message": data.get("description", "Failed")}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/telegram/webhook")
async def telegram_webhook(req: dict, db: Session = Depends(get_db)):
    """
    Telegram bot webhook — receives commands from users.
    Supported commands:
      /start   — show help
      /status  — engine + market status
      /stop    — stop running engine
      /engine <automation_name> — start engine for named automation
      /help    — show all commands
    """
    msg = req.get("message") or req.get("edited_message")
    if not msg:
        return {"ok": True}

    chat_id = str(msg.get("chat", {}).get("id", ""))
    text = (msg.get("text") or "").strip()
    if not chat_id or not text.startswith("/"):
        return {"ok": True}

    # Find user by chat_id (check all telegram accounts)
    all_users = db.query(User).filter(User.is_active == True).all()
    matched_user = None
    matched_token = None
    for u in all_users:
        for acct in (u.telegram_accounts or []):
            if acct.get("active") and str(acct.get("chat", "")) == chat_id:
                matched_user = u
                matched_token = acct.get("token")
                break
        if not matched_user and u.telegram_chat == chat_id and u.telegram_token:
            matched_user = u
            matched_token = u.telegram_token
        if matched_user:
            break

    async def reply(text_msg: str):
        if matched_token:
            await _send_telegram(matched_token, chat_id, text_msg)

    if not matched_user:
        await reply("❌ Chat ID not linked to any AlgoDesk account.\nAdd this bot in Profile → Telegram.")
        return {"ok": True}

    cmd = text.split()[0].lower().lstrip("/")

    if cmd in ("start", "help"):
        await reply(
            f"👋 Hi {matched_user.name}! AlgoDesk Bot Commands:\n\n"
            "/status — Engine + market status\n"
            "/stop — Stop running engine\n"
            "/engine <name> — Start automation by name\n"
            "/help — Show this message\n\n"
            "You will receive trade alerts automatically when engine is running."
        )

    elif cmd == "status":
        eng = _get_engine(matched_user.id)
        cache = _user_cache(matched_user.id)
        spot = cache.get("spot", 0)
        mkt_status = cache.get("status", "waiting")
        if eng and eng.is_running:
            pos = eng.position
            pos_txt = f"Position: {pos['strategy_code']} | Entry: ₹{pos.get('entry_combined',0):.1f}" if pos else "No open position"
            await reply(
                f"🟢 Engine RUNNING\n"
                f"NIFTY: ₹{spot:,.1f} ({mkt_status})\n"
                f"{pos_txt}\n"
                f"Day P&L: ₹{eng.day_pnl:.0f}"
            )
        else:
            await reply(
                f"⚪ Engine IDLE\n"
                f"NIFTY: ₹{spot:,.1f} ({mkt_status})\n"
                f"Use /engine <name> to start"
            )

    elif cmd == "stop":
        running_engs = [e for e in _all_engines(matched_user.id) if e.is_running]
        eng = running_engs[0] if running_engs else None
        if running_engs:
            for e in running_engs:
                e.is_running = False
            if matched_user.id in active_engines:
                del active_engines[matched_user.id]
            db.query(Automation).filter(
                Automation.user_id == matched_user.id,
                Automation.status == "RUNNING"
            ).update({"status": "IDLE"})
            db.commit()
            await reply("🛑 Engine stopped via Telegram command.")
        else:
            await reply("⚪ Engine is not running.")

    elif cmd == "engine":
        parts = text.split(maxsplit=1)
        auto_name = parts[1].strip() if len(parts) > 1 else ""
        autos = db.query(Automation).filter(
            Automation.user_id == matched_user.id
        ).all()
        auto = next((a for a in autos if auto_name.lower() in a.name.lower()), None)
        if not auto and autos:
            auto = autos[0]  # start first automation if no match
        if not auto:
            await reply("❌ No automations found. Create one in the app first.")
        elif auto.status == "RUNNING":
            await reply(f"⚠️ {auto.name} is already running.")
        else:
            # Start the engine
            from fyers import FyersConnection, decrypt
            bc = db.query(BrokerConnection).filter(
                BrokerConnection.user_id == matched_user.id,
                BrokerConnection.broker_id == "fyers",
                BrokerConnection.is_connected == True
            ).first()
            if not bc:
                await reply("❌ Fyers not connected. Connect in My Brokers first.")
            else:
                fields = {k.replace("_enc", ""): decrypt(matched_user.id, v)
                          for k, v in (bc.encrypted_fields or {}).items()}
                conn = FyersConnection(
                    user_id=matched_user.id,
                    client_id=fields.get("client_id", ""),
                    secret_key=fields.get("secret_key", ""),
                    pin=fields.get("pin", ""),
                    redirect_uri=fields.get("redirect_uri", ""),
                    fyers_id=fields.get("fyers_id", ""),
                    totp_key=fields.get("totp_key", ""),
                    access_token_enc=bc.access_token_enc,
                    refresh_token_enc=bc.refresh_token_enc,
                )
                from engine import EngineState
                config = {**auto.config, "strategies": auto.strategies, "mode": auto.mode}
                state = EngineState(config)
                _set_engine(matched_user.id, auto.id, state)
                asyncio.create_task(_run_engine(matched_user.id, auto, state, conn, db))
                auto.status = "RUNNING"
                db.commit()
                await reply(f"✅ Engine started: {auto.name}\nMode: {auto.mode.upper()}")
    else:
        await reply(f"Unknown command: /{cmd}\nSend /help for available commands.")

    return {"ok": True}


# ── WebSocket ─────────────────────────────────────────────────

# ── Shadow trades (paper simulation) ─────────────────────────────

@app.get("/api/shadow/trades")
def get_shadow_trades(
    days: int = 30,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Returns shadow (paper) trade history for performance analysis."""
    from datetime import timedelta
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    trades = db.query(ShadowTrade).filter(
        ShadowTrade.user_id == user.id,
        ShadowTrade.trade_date >= since
    ).order_by(ShadowTrade.created_at.desc()).all()

    return {"trades": [
        {"id": t.id, "date": t.trade_date, "symbol": t.symbol,
         "strategy": t.strategy_code, "atm": t.atm_strike,
         "entry": t.entry_combined, "exit": t.exit_combined,
         "entry_time": t.entry_time.isoformat() if t.entry_time else None,
         "exit_time": t.exit_time.isoformat() if t.exit_time else None,
         "exit_reason": t.exit_reason,
         "pnl": t.net_pnl, "lots": t.lots,
         "is_open": t.is_open,
         "entry_spot": t.entry_spot,
         "sl_tracking": t.sl_tracking or {}}
        for t in trades
    ]}


@app.get("/api/shadow/performance")
def shadow_performance(
    days: int = 30,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Performance summary for shadow trades."""
    from datetime import timedelta
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    trades = db.query(ShadowTrade).filter(
        ShadowTrade.user_id == user.id,
        ShadowTrade.trade_date >= since,
        ShadowTrade.is_open == False
    ).all()

    if not trades:
        # Return full schema with zeros — go_live_ready=False, score=0
        empty_checks = {
            "min_trades":      {"pass":False,"value":0,     "threshold":"≥20 trades",       "desc":"Statistical significance"},
            "win_rate":        {"pass":False,"value":"0%",  "threshold":"≥55%",              "desc":"Minimum viable win rate"},
            "profit_factor":   {"pass":False,"value":0,     "threshold":"≥1.5",              "desc":"Gross profit vs gross loss"},
            "positive_equity": {"pass":False,"value":"₹0",  "threshold":">₹0",              "desc":"Overall profitable"},
            "max_consec_loss": {"pass":False,"value":0,     "threshold":"≤4 consecutive",    "desc":"Manageable losing streaks"},
            "reward_risk":     {"pass":False,"value":0,     "threshold":"≥1.0",              "desc":"Avg win ≥ avg loss"},
            "days_traded":     {"pass":False,"value":0,     "threshold":"≥10 trading days",  "desc":"Tested across enough sessions"},
        }
        return {
            "total_trades":0,"total_pnl":0,"win_rate":0,"avg_pnl":0,
            "profit_factor":0,"avg_win":0,"avg_loss":0,"reward_risk":0,
            "expectancy":0,"max_drawdown":0,"max_consec_loss":0,"days_traded":0,
            "go_live_ready":False,"go_live_score":0,"ready_checks":empty_checks,
            "wins":0,"losses":0,
            "best_day":None,"worst_day":None,
            "by_strategy":{},"by_day":[],"equity_curve":[],"exit_reasons":{},"days":days
        }

    total_pnl = sum(t.net_pnl or 0 for t in trades)
    wins   = [t for t in trades if (t.net_pnl or 0) > 0]
    losses = [t for t in trades if (t.net_pnl or 0) <= 0]

    # By strategy
    by_strat = {}
    for t in trades:
        s = t.strategy_code
        if s not in by_strat:
            by_strat[s] = {"trades":0,"wins":0,"total_pnl":0,"avg_pnl":0}
        by_strat[s]["trades"] += 1
        by_strat[s]["total_pnl"] += t.net_pnl or 0
        if (t.net_pnl or 0) > 0:
            by_strat[s]["wins"] += 1
    for s in by_strat:
        n = by_strat[s]["trades"]
        by_strat[s]["win_rate"] = round(by_strat[s]["wins"]/n*100,1) if n else 0
        by_strat[s]["avg_pnl"]  = round(by_strat[s]["total_pnl"]/n,0) if n else 0
        by_strat[s]["total_pnl"]= round(by_strat[s]["total_pnl"],0)

    # By day
    day_map = {}
    for t in trades:
        d = t.trade_date
        if d not in day_map:
            day_map[d] = {"date":d,"trades":0,"pnl":0,"wins":0}
        day_map[d]["trades"] += 1
        day_map[d]["pnl"]    += t.net_pnl or 0
        if (t.net_pnl or 0) > 0:
            day_map[d]["wins"] += 1
    by_day = sorted(day_map.values(), key=lambda x: x["date"])
    for d in by_day:
        d["pnl"] = round(d["pnl"], 0)

    # Exit reasons
    exit_reasons = {}
    for t in trades:
        r = t.exit_reason or "UNKNOWN"
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

    # Running equity
    equity = 0
    equity_curve = []
    for d in by_day:
        equity += d["pnl"]
        equity_curve.append({"date":d["date"],"equity":round(equity,0)})

    best_day  = max(by_day, key=lambda x: x["pnl"]) if by_day else None
    worst_day = min(by_day, key=lambda x: x["pnl"]) if by_day else None

    # ── Industry-standard performance metrics ─────────────────
    n = len(trades)
    total_wins   = len(wins)
    total_losses = len(losses)
    win_rate     = total_wins / n * 100

    # Profit factor = gross profit / gross loss (>1.5 is good, >2.0 is excellent)
    gross_profit = sum(t.net_pnl for t in wins)
    gross_loss   = abs(sum(t.net_pnl for t in losses)) if losses else 0
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 99.0

    # Average win / average loss ratio (>1.5 is healthy)
    avg_win  = round(gross_profit / total_wins, 0) if total_wins else 0
    avg_loss = round(-gross_loss / total_losses, 0) if total_losses else 0
    reward_risk = round(avg_win / abs(avg_loss), 2) if avg_loss else 99.0

    # Max consecutive losses (drawdown risk indicator)
    consec_loss = max_consec = current_consec = 0
    for t in sorted(trades, key=lambda x: x.trade_date):
        if (t.net_pnl or 0) <= 0:
            current_consec += 1
            max_consec = max(max_consec, current_consec)
        else:
            current_consec = 0

    # Max drawdown from equity curve peak
    peak = 0
    max_dd = 0
    running_eq = 0
    for d in by_day:
        running_eq += d["pnl"]
        if running_eq > peak:
            peak = running_eq
        dd = peak - running_eq
        if dd > max_dd:
            max_dd = dd

    # Expectancy = (win_rate × avg_win) - (loss_rate × avg_loss)
    loss_rate = 1 - win_rate/100
    expectancy = round((win_rate/100 * avg_win) - (loss_rate * abs(avg_loss)), 0)

    # Days traded (not just total days in range)
    days_traded = len(by_day)

    # ── Go-Live readiness assessment ────────────────────────────
    # Industry thresholds for options selling strategies:
    ready_checks = {
        "min_trades":        {"pass": n >= 20,           "value": n,                  "threshold": "≥20 trades",           "desc": "Statistical significance"},
        "win_rate":          {"pass": win_rate >= 55,    "value": f"{win_rate:.1f}%", "threshold": "≥55%",                 "desc": "Minimum viable win rate"},
        "profit_factor":     {"pass": profit_factor >= 1.5, "value": profit_factor,  "threshold": "≥1.5",                 "desc": "Gross profit vs gross loss"},
        "positive_equity":   {"pass": total_pnl > 0,    "value": f"₹{total_pnl:,.0f}", "threshold": ">₹0",              "desc": "Overall profitable"},
        "max_consec_loss":   {"pass": max_consec <= 4,  "value": max_consec,         "threshold": "≤4 consecutive",       "desc": "Manageable losing streaks"},
        "reward_risk":       {"pass": reward_risk >= 1.0, "value": reward_risk,       "threshold": "≥1.0",                "desc": "Avg win ≥ avg loss"},
        "days_traded":       {"pass": days_traded >= 10, "value": days_traded,        "threshold": "≥10 trading days",     "desc": "Tested across enough sessions"},
    }
    checks_passed = sum(1 for c in ready_checks.values() if c["pass"])
    go_live_score = round(checks_passed / len(ready_checks) * 100)
    go_live_ready = go_live_score >= 85  # Need to pass ≥6 of 7 checks

    return {
        # Core metrics
        "total_trades":   n,
        "total_pnl":      round(total_pnl, 0),
        "wins":           total_wins,
        "losses":         total_losses,
        "win_rate":       round(win_rate, 1),
        "avg_pnl":        round(total_pnl / n, 0),
        # Industry metrics
        "profit_factor":  profit_factor,
        "avg_win":        avg_win,
        "avg_loss":       avg_loss,
        "reward_risk":    reward_risk,
        "expectancy":     expectancy,
        "max_drawdown":   round(max_dd, 0),
        "max_consec_loss": max_consec,
        "days_traded":    days_traded,
        # Go-live assessment
        "go_live_ready":  go_live_ready,
        "go_live_score":  go_live_score,
        "ready_checks":   ready_checks,
        # Detail
        "best_day":       best_day,
        "worst_day":      worst_day,
        "by_strategy":    by_strat,
        "by_day":         by_day,
        "equity_curve":   equity_curve,
        "exit_reasons":   exit_reasons,
        "days":           days,
    }


@app.get("/api/backtest")
def get_backtest(user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    """
    Backtest analysis based on stored shadow (paper) trade history.
    Returns strategy-level stats, day-of-week breakdown, exit reason
    distribution, hourly entry performance, and monthly P&L trend.
    """
    trades = db.query(ShadowTrade).filter(
        ShadowTrade.user_id == user.id,
        ShadowTrade.is_open == False,
        ShadowTrade.net_pnl.isnot(None),
    ).order_by(ShadowTrade.trade_date).all()

    if not trades:
        return {"ok": True, "trades_count": 0, "by_strategy": [],
                "by_weekday": [], "by_hour": [], "by_exit": [],
                "monthly": [], "equity_curve": [], "summary": {}}

    total_pnl   = 0.0
    wins        = 0
    losses      = 0
    equity      = 0.0
    equity_curve = []

    # Accumulators
    by_strategy  = {}   # code -> {wins, losses, pnl, entries}
    by_weekday   = {}   # 0-4 -> {wins, losses, pnl, count}
    by_hour      = {}   # hour -> {wins, losses, pnl, count}
    by_exit      = {}   # reason -> count
    by_month     = {}   # YYYY-MM -> pnl

    DAYS = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]

    for t in trades:
        pnl  = t.net_pnl or 0.0
        code = t.strategy_code or "?"
        total_pnl += pnl
        equity    += pnl
        is_win     = pnl > 0

        if is_win: wins   += 1
        else:      losses += 1

        equity_curve.append({"date": t.trade_date, "equity": round(equity, 2)})

        # By strategy
        s = by_strategy.setdefault(code, {"code": code, "wins": 0, "losses": 0,
                                          "pnl": 0.0, "count": 0,
                                          "avg_entry": 0.0, "_entry_sum": 0.0,
                                          "best": None, "worst": None})
        s["count"] += 1
        s["pnl"]   += pnl
        s["_entry_sum"] += t.entry_combined or 0.0
        if is_win: s["wins"]   += 1
        else:      s["losses"] += 1
        if s["best"]  is None or pnl > s["best"]:  s["best"]  = pnl
        if s["worst"] is None or pnl < s["worst"]: s["worst"] = pnl

        # By weekday
        try:
            wd = datetime.strptime(t.trade_date, "%Y-%m-%d").weekday()
            w  = by_weekday.setdefault(wd, {"day": DAYS[wd], "wins": 0,
                                            "losses": 0, "pnl": 0.0, "count": 0})
            w["count"] += 1; w["pnl"] += pnl
            if is_win: w["wins"] += 1
            else:      w["losses"] += 1
        except Exception:
            pass

        # By entry hour
        if t.entry_time:
            try:
                import pytz
                ist = pytz.timezone("Asia/Kolkata")
                et  = t.entry_time.replace(tzinfo=pytz.utc).astimezone(ist)
                hr  = et.hour
                h   = by_hour.setdefault(hr, {"hour": f"{hr}:00", "wins": 0,
                                              "losses": 0, "pnl": 0.0, "count": 0})
                h["count"] += 1; h["pnl"] += pnl
                if is_win: h["wins"] += 1
                else:      h["losses"] += 1
            except Exception:
                pass

        # By exit reason (simplified label)
        raw_reason = (t.exit_reason or "OTHER").upper()
        if   "PROFIT_LOCK"   in raw_reason: label = "Profit Lock"
        elif "PROFIT_TARGET" in raw_reason: label = "Profit Target"
        elif "PROFIT"        in raw_reason: label = "Profit Target"
        elif "TRAILING"      in raw_reason: label = "Trailing SL"
        elif "VWAP"          in raw_reason: label = "VWAP SL"
        elif "EMA75"         in raw_reason: label = "EMA75 SL"
        elif "MAX_LOSS"      in raw_reason: label = "Max Loss"
        elif "AUTO"          in raw_reason: label = "Auto Exit"
        elif "MARKET"        in raw_reason: label = "Market Close"
        else:                               label = "Other"
        by_exit[label] = by_exit.get(label, 0) + 1

        # By month
        month = t.trade_date[:7]  # YYYY-MM
        by_month[month] = by_month.get(month, 0.0) + pnl

    total = len(trades)

    # Finalise strategy stats
    strat_list = []
    for code, s in sorted(by_strategy.items()):
        cnt = s["count"]
        strat_list.append({
            "code":       code,
            "count":      cnt,
            "wins":       s["wins"],
            "losses":     s["losses"],
            "win_rate":   round(s["wins"] / cnt * 100, 1) if cnt else 0,
            "total_pnl":  round(s["pnl"], 2),
            "avg_pnl":    round(s["pnl"] / cnt, 2) if cnt else 0,
            "avg_entry":  round(s["_entry_sum"] / cnt, 1) if cnt else 0,
            "best_trade": round(s["best"] or 0, 0),
            "worst_trade":round(s["worst"] or 0, 0),
        })

    weekday_list = [by_weekday[k] for k in sorted(by_weekday)]
    for w in weekday_list:
        w["pnl"]      = round(w["pnl"], 2)
        w["win_rate"] = round(w["wins"] / w["count"] * 100, 1) if w["count"] else 0

    hour_list = [by_hour[k] for k in sorted(by_hour)]
    for h in hour_list:
        h["pnl"]      = round(h["pnl"], 2)
        h["win_rate"] = round(h["wins"] / h["count"] * 100, 1) if h["count"] else 0

    monthly_list = [{"month": m, "pnl": round(p, 2)}
                    for m, p in sorted(by_month.items())]

    exit_list = [{"reason": k, "count": v} for k, v in
                 sorted(by_exit.items(), key=lambda x: -x[1])]

    return {
        "ok":           True,
        "trades_count": total,
        "summary": {
            "total_pnl":  round(total_pnl, 2),
            "wins":       wins,
            "losses":     losses,
            "win_rate":   round(wins / total * 100, 1) if total else 0,
            "avg_pnl":    round(total_pnl / total, 2) if total else 0,
            "best_trade": round(max(t.net_pnl or 0 for t in trades), 2),
            "worst_trade":round(min(t.net_pnl or 0 for t in trades), 2),
        },
        "by_strategy":  strat_list,
        "by_weekday":   weekday_list,
        "by_hour":      hour_list,
        "by_exit":      exit_list,
        "monthly":      monthly_list,
        "equity_curve": equity_curve,
    }


# ── Shadow engine helper ──────────────────────────────────────────

async def _run_shadow_trade(user_id: str, auto: Automation,
                             signal: dict, entry_combined: float,
                             entry_spot: float, db_factory):
    """
    Runs a shadow (paper) trade from entry to exit.
    Monitors combined premium every 60s using market data cache.
    Stores complete result in shadow_trades table.
    Sends Telegram alert if configured.
    """
    import pytz
    from datetime import time as dtime
    ist = pytz.timezone("Asia/Kolkata")

    config = {**auto.config, "strategies": auto.strategies}
    # Get lot size from config, falling back to symbol registry
    _reg_lot = SYMBOL_REGISTRY.get(auto.symbol, {}).get("lot_size", 65)
    lot_sz = int(config.get("lot_size") or _reg_lot)
    lots   = int(config.get("lots", 1))

    # Create shadow trade record
    db = SessionLocal()
    try:
        # S2 direction metadata for real-world validation
        entry_insight = None
        if signal.get("code") == "S2":
            atm_strike = signal.get("strike", 0)
            spot_drift = entry_spot - atm_strike if atm_strike else 0
            direction = "UP" if spot_drift > 25 else "DOWN" if spot_drift < -25 else "FLAT"
            entry_insight = (
                f"[S2 Validation] Spot={entry_spot:.0f} ATM={atm_strike} "
                f"Drift={spot_drift:+.0f}pts ({direction}) | {signal.get('reason','')}"
            )

        st = ShadowTrade(
            user_id=user_id, automation_id=auto.id,
            trade_date=datetime.now(ist).strftime("%Y-%m-%d"),
            symbol=auto.symbol, strategy_code=signal["code"],
            atm_strike=signal.get("strike", 0),
            entry_combined=entry_combined, entry_spot=entry_spot,
            entry_time=datetime.utcnow(),
            lots=lots, lot_size=lot_sz,
            is_open=True, signal_data=signal,
            ai_insight=entry_insight,
        )
        db.add(st); db.commit(); db.refresh(st)
        trade_id = st.id

        # Send paper mode entry alert
        if auto.telegram_alerts:
            user = db.query(User).filter(User.id == user_id).first()
            if user:
                _tg_ids = (auto.config or {}).get("telegram_accounts", [])
                await _send_telegram_all(user,
                    f"📋 [PAPER MODE] {signal['code']}: {signal['name']}\n"
                    f"Symbol: {auto.symbol}\n"
                    f"Strike: {signal.get('strike')}\n"
                    f"Combined: ₹{entry_combined:.1f}\n"
                    f"Time: {datetime.now(ist).strftime('%H:%M')} IST\n"
                    f"⚠️ This is a simulation — no real orders placed.",
                    account_ids=_tg_ids if _tg_ids else None)
    finally:
        db.close()

    # Monitor position using market data cache
    from engine import SLState, nearest_strike
    sl = SLState()
    sl.activate(entry_combined, config)
    sl_tracking = {}
    combined_history = [entry_combined]  # seed with entry combined
    ema75_val = entry_combined            # seed EMA with entry combined

    while True:
        await asyncio.sleep(60)
        try:
            now = datetime.now(ist)
            t   = now.time()

            # Auto-exit at configured time
            exit_time = config.get("auto_exit_time", "14:00")
            eh, em = map(int, exit_time.split(":"))
            if t >= dtime(eh, em):
                # Use current combined at exit time, not entry_combined
                exit_price = current if current > 0 else entry_combined
                await _close_shadow_trade(trade_id, user_id, "AUTO_EXIT",
                    exit_price, auto, lots, lot_sz, sl_tracking)
                return

            # Market closed
            if t > dtime(15, 30):
                exit_price = current if current > 0 else entry_combined
                await _close_shadow_trade(trade_id, user_id, "MARKET_CLOSE",
                    exit_price, auto, lots, lot_sz, sl_tracking)
                return

            # Get current combined from cache
            cache = _user_cache(user_id)
            if not cache.get("chain"):
                continue

            atm = signal.get("strike", nearest_strike(cache.get("spot", 0)))
            chain_entry = cache["chain"].get(atm)
            if not chain_entry:
                continue

            current = chain_entry.get("combined", entry_combined)

            # Track VWAP and EMA75 of combined premium properly
            # Use the StrikeState from cache if available, else calculate inline
            combined_history.append(current)
            candle_count = len(combined_history)

            # VWAP: cumulative average of combined premium from entry
            vwap = sum(combined_history) / candle_count

            # EMA75: exponential moving average with span=75
            k75 = 2 / (75 + 1)
            if candle_count == 1:
                ema75_val = current
            else:
                ema75_val = current * k75 + ema75_val * (1 - k75)

            sl_tracking = {
                "current":      current,
                "trailing_low": sl.trailing_low,
                "sl_type":      sl.sl_type,
                "candles":      candle_count,
                "vwap":         round(vwap, 2),
                "ema75":        round(ema75_val, 2),
            }

            should_exit, reason = sl.update(current, vwap, ema75_val, candle_count, config)
            if should_exit:
                await _close_shadow_trade(trade_id, user_id, reason,
                    current, auto, lots, lot_sz, sl_tracking)
                return

        except Exception as e:
            log.error(f"Shadow trade monitor: {e}")


async def _close_shadow_trade(trade_id, user_id, reason,
                               exit_combined, auto, lots, lot_sz, sl_tracking):
    """Close a shadow trade and send Telegram summary."""
    import pytz
    ist = pytz.timezone("Asia/Kolkata")
    db = SessionLocal()
    try:
        t = db.query(ShadowTrade).filter(ShadowTrade.id == trade_id).first()
        if not t or not t.is_open:
            return
        pnl = (t.entry_combined - exit_combined) * lots * lot_sz
        # Real brokerage calculation (not flat ₹40)
        charges = calc_brokerage(lots, lot_sz, t.entry_combined, exit_combined)
        t.exit_combined = exit_combined
        t.exit_time     = datetime.utcnow()
        t.exit_reason   = reason
        t.gross_pnl     = round(pnl, 2)
        t.brokerage     = round(charges["total"], 2)
        t.net_pnl       = round(pnl - charges["total"], 2)
        t.is_open       = False
        t.sl_tracking   = sl_tracking
        db.commit()

        if auto.telegram_alerts:
            user = db.query(User).filter(User.id == user_id).first()
            if user:
                emoji = "✅" if t.net_pnl > 0 else "🔴"
                _tg_ids2 = (auto.config or {}).get("telegram_accounts", [])
                await _send_telegram_all(user,
                    f"{emoji} [PAPER MODE] Trade Closed\n"
                    f"Strategy: {t.strategy_code}\n"
                    f"Symbol: {auto.symbol}\n"
                    f"Entry: ₹{t.entry_combined:.1f} → Exit: ₹{exit_combined:.1f}\n"
                    f"Qty: {lots} lot(s) × {lot_sz} = {lots*lot_sz} units\n"
                    f"Gross P&L: ₹{pnl:+.0f}\n"
                    f"Charges:   ₹{charges['total']:.0f} "
                    f"(brok ₹{charges['brokerage']:.0f} + "
                    f"fees ₹{charges['exchange_fee']:.0f} + "
                    f"GST ₹{charges['gst']:.0f})\n"
                    f"Net P&L:   ₹{t.net_pnl:+.0f}\n"
                    f"Exit: {reason}\n"
                    f"⚠️ Simulation only — no real money",
                    account_ids=_tg_ids2 if _tg_ids2 else None)
    finally:
        db.close()


@app.get("/api/dashboard/summary")
async def dashboard_summary(user: User = Depends(get_current_user),
                             symbol: str = "NSE:NIFTY50-INDEX",
                             db: Session = Depends(get_db)):
    """Combined KPIs across all user automations for dashboard."""
    import pytz
    from datetime import timedelta
    ist = pytz.timezone("Asia/Kolkata")
    today = datetime.now(ist).strftime("%Y-%m-%d")
    month_ago = (datetime.now(ist) - timedelta(days=30)).strftime("%Y-%m-%d")

    # All automations
    autos = db.query(Automation).filter(Automation.user_id == user.id).all()

    # Today's trades (live)
    today_trades = db.query(Trade).filter(
        Trade.user_id == user.id,
        Trade.trade_date == today
    ).all()

    # Today's shadow trades
    today_shadow = db.query(ShadowTrade).filter(
        ShadowTrade.user_id == user.id,
        ShadowTrade.trade_date == today
    ).all()

    # Month's closed trades
    month_trades = db.query(Trade).filter(
        Trade.user_id == user.id,
        Trade.trade_date >= month_ago,
        Trade.is_open == False
    ).all()

    month_shadow = db.query(ShadowTrade).filter(
        ShadowTrade.user_id == user.id,
        ShadowTrade.trade_date >= month_ago,
        ShadowTrade.is_open == False
    ).all()

    # Per-automation status
    auto_status = []
    for a in autos:
        eng = _get_engine(user.id, a.id)
        a_today = [t for t in today_trades if t.automation_id == a.id]
        a_shadow = [t for t in today_shadow if t.automation_id == a.id]
        a_pnl = sum(t.net_pnl or 0 for t in a_today if not t.is_open)
        a_shadow_pnl = sum(t.net_pnl or 0 for t in a_shadow if not t.is_open)
        auto_status.append({
            "id": a.id, "name": a.name,
            "symbol": a.symbol.split(":")[1] if ":" in a.symbol else a.symbol,
            "mode": a.mode,
            "shadow_mode": a.shadow_mode,
            "status": a.status,
            "strategies": a.strategies,
            "today_trades": len(a_today),
            "today_pnl": round(a_pnl, 0),
            "today_shadow_trades": len(a_shadow),
            "today_shadow_pnl": round(a_shadow_pnl, 0),
            "open_position": any(t.is_open for t in a_today),
        })

    today_live_pnl = sum(t.net_pnl or 0 for t in today_trades if not t.is_open)
    today_paper_pnl = sum(t.net_pnl or 0 for t in today_shadow if not t.is_open)
    month_live_pnl        = sum(t.net_pnl or 0 for t in month_trades)
    month_paper_pnl       = sum(t.net_pnl or 0 for t in month_shadow)
    month_live_trades_ct  = len(month_trades)
    month_paper_trades_ct = len(month_shadow)

    cache = _user_cache(user.id)
    mkt_open = _market_open_now()
    mkt_status = cache.get("status", "waiting")
    if mkt_status == "live" and not mkt_open:
        mkt_status = "closed"

    # Use per-symbol cache when a non-default symbol is requested
    sym_spot, sym_atm = cache.get("spot", 0), cache.get("atm", 0)
    if symbol != "NSE:NIFTY50-INDEX":
        sym_cache = (user_symbol_cache.get(user.id) or {}).get(symbol, {})
        if sym_cache.get("spot", 0) > 0:
            sym_spot = sym_cache["spot"]
            sym_atm  = sym_cache["atm"]
        else:
            sym_spot, sym_atm = 0, 0   # no data for this symbol yet

    # ── Onboarding progress ────────────────────────────────────
    broker_conn = db.query(BrokerConnection).filter(
        BrokerConnection.user_id == user.id
    ).first()
    paper_total = db.query(ShadowTrade).filter(
        ShadowTrade.user_id == user.id,
        ShadowTrade.is_open == False
    ).count()
    live_total_count = db.query(Trade).filter(
        Trade.user_id == user.id,
        Trade.is_open == False
    ).count()
    live_pnl_total = db.query(Trade).filter(
        Trade.user_id == user.id,
        Trade.is_open == False
    ).with_entities(func.sum(Trade.net_pnl)).scalar() or 0
    has_live_auto = any(a.mode == "live" for a in autos)

    # Auto-advance lifecycle (only forward, never backward)
    current_lc = getattr(user, "lifecycle_stage", "DEMO") or "DEMO"
    new_lc = current_lc
    if current_lc == "DEMO" and (broker_conn or paper_total > 0):
        new_lc = "PAPER"
    elif current_lc == "PAPER" and paper_total >= 20:
        new_lc = "LIVE_READY"
    if new_lc != current_lc:
        user.lifecycle_stage = new_lc
        db.commit()

    # Compute onboarding step 1-6
    ob_step = 1
    if broker_conn:                         ob_step = 2
    if paper_total > 0:                     ob_step = 3
    if paper_total >= 20:                   ob_step = 4
    if has_live_auto:                       ob_step = 5
    if live_pnl_total > 0:                  ob_step = 6

    return {
        "spot":             sym_spot,
        "atm":              sym_atm,
        "vix":              round(cache.get("vix", 0), 2),
        "prev_close":       cache.get("prev_close", 0),
        "market_status":    mkt_status,
        "market_updated":   cache.get("updated"),
        "market_message":   (f"Market closed · {_next_market_open_msg()}" if not mkt_open
                             else cache.get("message", "")),
        "today_live_pnl":   round(today_live_pnl, 0),
        "today_paper_pnl":  round(today_paper_pnl, 0),
        "today_live_trades": len([t for t in today_trades if not t.is_open]),
        "today_paper_trades": len([t for t in today_shadow if not t.is_open]),
        "open_live":        len([t for t in today_trades if t.is_open]),
        "open_paper":       len([t for t in today_shadow if t.is_open]),
        "open_positions":   len([t for t in today_trades if t.is_open]) + len([t for t in today_shadow if t.is_open]),
        "month_live_pnl":    round(month_live_pnl, 0),
        "month_paper_pnl":   round(month_paper_pnl, 0),
        "month_live_trades":  month_live_trades_ct,
        "month_paper_trades": month_paper_trades_ct,
        "automations":      auto_status,
        "total_automations": len(autos),
        "running_automations": len([a for a in autos if a.status=="RUNNING"]),
        "live_automations":  len([a for a in autos if a.mode=="live"]),
        "paper_automations": len([a for a in autos if a.mode=="paper"]),
        "onboarding": {
            "step":              ob_step,
            "broker_connected":  bool(broker_conn),
            "paper_trade_count": paper_total,
            "live_trade_count":  live_total_count,
            "live_pnl_total":    round(live_pnl_total, 0),
            "has_live_auto":     has_live_auto,
            "lifecycle":         new_lc,
        },
    }


@app.get("/api/capital/check")
async def capital_check(
    symbol: str = "NSE:NIFTY50-INDEX",
    lots: int = 1,
    lot_size: int = 0,
    hedge_width: int = 2,
    strategies: str = "S1,S8",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Pre-trade capital check.
    Fetches live account funds from Fyers and compares against
    estimated margin requirement for the given strategy configuration.
    Returns go/no-go with breakdown.
    """
    conn = _get_fyers(user, db)
    strategy_list = [s.strip() for s in strategies.split(",") if s.strip()]

    # Get live spot price — use per-symbol cache for non-NIFTY symbols
    sym_cache = (user_symbol_cache.get(user.id) or {}).get(symbol)
    if sym_cache and sym_cache.get("spot", 0) > 0:
        spot = sym_cache["spot"]
    else:
        spot = _user_cache(user.id).get("spot", 0) if symbol == "NSE:NIFTY50-INDEX" else 0

    # Fallback spot prices if cache empty (e.g. outside market hours)
    if not spot:
        fallback_spots = {
            "NSE:NIFTY50-INDEX":    23000,
            "NSE:NIFTYBANK-INDEX":  49000,
            "NSE:FINNIFTY-INDEX":   23500,
            "BSE:SENSEX-INDEX":     76000,
            "NSE:MIDCPNIFTY-INDEX": 12000,
        }
        spot = fallback_spots.get(symbol, 23000)

    # Get registry lot size if not overridden
    reg = SYMBOL_REGISTRY.get(symbol, {})
    actual_lot_size = lot_size or reg.get("lot_size", 65)

    # Strategy-specific hedge widths — sourced from STRATEGY_MARGIN_CONFIG (matches engine)
    hedges = [STRATEGY_MARGIN_CONFIG.get(s, {}).get("hedge", hedge_width) for s in strategy_list]
    max_hedge = max(hedges) if hedges else hedge_width

    # Calculate margin estimate
    margin = estimate_margin(symbol, lots, actual_lot_size, max_hedge, spot)

    # Fetch actual account funds
    funds = {}
    funds_error = None
    available = 0

    if conn:
        try:
            funds = await conn.get_funds()
            if not funds:
                funds_error = "Fyers returned empty funds — token may be expired"
            else:
                # Try all known Fyers balance key names
                for key in ["Available Balance", "Available cash", "Available Cash",
                            "Cash Available", "Clear Balance", "Net Balance",
                            "Payin", "Total Balance", "Equity Amount",
                            "available_balance", "availableBalance"]:
                    val = funds.get(key, 0)
                    if val and float(val) > 0:
                        available = float(val)
                        break
                # Fallback: largest positive numeric value
                if not available:
                    pos = {k: float(v) for k, v in funds.items()
                           if isinstance(v, (int, float)) and float(v) > 0}
                    if pos:
                        available = max(pos.values())
        except Exception as e:
            funds_error = str(e)

    can_trade = available >= margin["net_required"]
    shortfall = max(0, margin["net_required"] - available)
    buffer = available - margin["net_required"]

    # Per-strategy full breakdown
    strat_breakdown = []
    for s in strategy_list:
        sc   = STRATEGY_MARGIN_CONFIG.get(s, {})
        hw   = sc.get("hedge", hedge_width)
        sm   = estimate_margin(symbol, lots, actual_lot_size, hw, spot, s)
        strat_breakdown.append({
            "strategy":      s,
            "label":         sc.get("label", s),
            "structure":     sm["structure"],
            "hedge_width":   hw,
            "net_required":  sm["net_required"],
            "per_lot":       sm["per_lot"],
            "net_premium":   sm["net_premium"],
            "max_profit":    sm["max_profit"],
            "max_loss":      sm["max_loss"],
            "profit_at_50":  sm["profit_at_50pct"],
            "be_upper":      sm["be_upper"],
            "be_lower":      sm["be_lower"],
            "can_trade":     available >= sm["net_required"],
            "legs":          sm["legs"],
        })

    # Most capital-efficient strategy (lowest margin, still can trade)
    tradeable = [s for s in strat_breakdown if s["can_trade"]]
    best_fit   = min(tradeable, key=lambda s: s["net_required"]) if tradeable else None

    return {
        "ok":            True,
        "symbol":        symbol,
        "label":         reg.get("label", symbol),
        "spot":          round(spot, 1),
        "lot_size":      actual_lot_size,
        "lots":          lots,
        "qty":           actual_lot_size * lots,
        "strategies":    strategy_list,
        "margin":        margin,
        "available":     round(available, 0),
        "can_trade":     can_trade,
        "shortfall":     round(shortfall, 0),
        "buffer":        round(buffer, 0),
        "buffer_lots":   int(buffer / margin["per_lot"]) if margin.get("per_lot") and margin["per_lot"] > 0 else 0,
        "funds":         funds,
        "funds_error":   funds_error,
        "mode":          conn.mode if conn else "no_broker",
        "strat_breakdown": strat_breakdown,
        "best_fit":      best_fit,
        "recommendation": (
            "✅ Sufficient funds — can proceed"
            if can_trade else
            f"❌ Insufficient funds — need ₹{shortfall:,.0f} more"
        ),
    }


# ── Trading Events Calendar ──────────────────────────────────────

@app.get("/api/events")
def list_events(user: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    events = db.query(TradingEvent).filter(
        TradingEvent.user_id == user.id
    ).order_by(TradingEvent.event_date).all()
    return {"events": [
        {"id": e.id, "date": e.event_date, "name": e.event_name,
         "category": e.category, "suspend": e.suspend_trading,
         "notes": e.notes,
         "auto_synced": getattr(e, "auto_synced", False),
         "source":      getattr(e, "source", "manual")}
        for e in events
    ]}

class EventReq(BaseModel):
    event_date: str
    event_name: str
    category: str = "other"
    suspend_trading: bool = True
    notes: str = ""

@app.post("/api/events")
def create_event(req: EventReq, user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    e = TradingEvent(user_id=user.id, event_date=req.event_date,
                     event_name=req.event_name, category=req.category,
                     suspend_trading=req.suspend_trading, notes=req.notes)
    db.add(e); db.commit(); db.refresh(e)
    return {"ok": True, "id": e.id}

@app.put("/api/events/{event_id}")
def update_event(event_id: str, req: EventReq,
                 user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    e = db.query(TradingEvent).filter(
        TradingEvent.id == event_id, TradingEvent.user_id == user.id).first()
    if not e: raise HTTPException(404, "Event not found")
    e.event_name = req.event_name; e.event_date = req.event_date
    e.category = req.category; e.suspend_trading = req.suspend_trading
    e.notes = req.notes; db.commit()
    return {"ok": True}

@app.delete("/api/events/{event_id}")
def delete_event(event_id: str, user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    db.query(TradingEvent).filter(
        TradingEvent.id == event_id,
        TradingEvent.user_id == user.id).delete(synchronize_session=False)
    db.commit()
    return {"ok": True}

@app.post("/api/events/seed-defaults")
def seed_default_events(user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    """Pre-populate with known 2026 Indian market events."""
    defaults = [
        # RBI Policy dates 2026 (approximate — user should verify)
        {"date":"2026-04-09","name":"RBI Monetary Policy","cat":"rbi"},
        {"date":"2026-06-05","name":"RBI Monetary Policy","cat":"rbi"},
        {"date":"2026-08-07","name":"RBI Monetary Policy","cat":"rbi"},
        {"date":"2026-10-09","name":"RBI Monetary Policy","cat":"rbi"},
        {"date":"2026-12-04","name":"RBI Monetary Policy","cat":"rbi"},
        # US Fed 2026 (remaining)
        {"date":"2026-03-18","name":"US Fed Rate Decision","cat":"fed"},
        {"date":"2026-05-07","name":"US Fed Rate Decision","cat":"fed"},
        {"date":"2026-06-17","name":"US Fed Rate Decision","cat":"fed"},
        {"date":"2026-07-29","name":"US Fed Rate Decision","cat":"fed"},
        {"date":"2026-09-16","name":"US Fed Rate Decision","cat":"fed"},
        {"date":"2026-11-05","name":"US Fed Rate Decision","cat":"fed"},
        {"date":"2026-12-16","name":"US Fed Rate Decision","cat":"fed"},
    ]
    added = 0
    for d in defaults:
        existing = db.query(TradingEvent).filter(
            TradingEvent.user_id == user.id,
            TradingEvent.event_date == d["date"],
            TradingEvent.event_name == d["name"]).first()
        if not existing:
            db.add(TradingEvent(user_id=user.id, event_date=d["date"],
                                event_name=d["name"], category=d["cat"],
                                suspend_trading=True))
            added += 1
    db.commit()
    return {"ok": True, "added": added}


# ── Live NSE Holiday Sync ─────────────────────────────────────────

async def _fetch_nager_holidays(year: int) -> list:
    """
    Fetch Indian national public holidays from Nager Date API.
    Free, no auth, returns official Indian national holidays.
    Returns list of {"date": "YYYY-MM-DD", "name": str}.
    """
    import httpx
    url = f"https://date.nager.at/api/v3/PublicHolidays/{year}/IN"
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(url, headers={"Accept": "application/json"})
            if r.status_code == 200:
                return [{"date": h["date"], "name": h["localName"] or h["name"]}
                        for h in r.json()]
    except Exception as e:
        log.warning(f"Nager Date API fetch failed for {year}: {e}")
    return []


async def _fetch_nse_holidays(year: int) -> list:
    """
    Fetch NSE trading holidays from NSE India website.
    NSE observes additional festival holidays (Holi, Diwali, Ganesh Chaturthi etc.)
    not covered by Indian national holidays.
    Returns list of {"date": "YYYY-MM-DD", "name": str}.
    """
    import httpx, re
    headers = {
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer":         "https://www.nseindia.com/",
    }
    results = []
    try:
        async with httpx.AsyncClient(timeout=20, headers=headers,
                                     follow_redirects=True) as c:
            # Establish session cookie by hitting the main page first
            await c.get("https://www.nseindia.com/")
            r = await c.get("https://www.nseindia.com/api/holiday-master?type=trading")
            if r.status_code == 200:
                data = r.json()
                # Response: {"CM": [{"tradingDate": "14-Jan-2026", "description": "..."}]}
                for segment in data.values():
                    if not isinstance(segment, list):
                        continue
                    for item in segment:
                        raw = item.get("tradingDate") or item.get("trade_date", "")
                        name = item.get("description") or item.get("holiday_desc", "NSE Holiday")
                        try:
                            # NSE returns "14-Jan-2026" format
                            dt = datetime.strptime(raw.strip(), "%d-%b-%Y")
                            if dt.year == year:
                                results.append({"date": dt.strftime("%Y-%m-%d"), "name": name.strip()})
                        except Exception:
                            pass
                    break  # CM segment is enough (all segments have same holidays)
    except Exception as e:
        log.warning(f"NSE holiday API fetch failed for {year}: {e}")
    return results


async def _sync_holidays_for_user(user_id: str, db, year: int) -> int:
    """Merge Nager + NSE holidays into trading_events for this user. Returns count added/updated."""
    nager  = await _fetch_nager_holidays(year)
    nse    = await _fetch_nse_holidays(year)

    # Merge: NSE is authoritative (it's the actual market calendar).
    # Nager fills gaps for national holidays NSE might not list.
    seen_dates: set = set()
    merged: list    = []
    for h in nse:
        if h["date"] not in seen_dates:
            merged.append({**h, "source": "nse"})
            seen_dates.add(h["date"])
    for h in nager:
        if h["date"] not in seen_dates:
            merged.append({**h, "source": "nager"})
            seen_dates.add(h["date"])

    added = 0
    for h in merged:
        existing = db.query(TradingEvent).filter(
            TradingEvent.user_id    == user_id,
            TradingEvent.event_date == h["date"],
            TradingEvent.auto_synced == True,
        ).first()
        if existing:
            # Update name/source in case it changed
            existing.event_name = h["name"]
            existing.source     = h["source"]
        else:
            # Don't overwrite user-created events on the same date
            user_event = db.query(TradingEvent).filter(
                TradingEvent.user_id    == user_id,
                TradingEvent.event_date == h["date"],
                TradingEvent.auto_synced == False,
            ).first()
            if not user_event:
                db.add(TradingEvent(
                    user_id         = user_id,
                    event_date      = h["date"],
                    event_name      = h["name"],
                    category        = "holiday",
                    suspend_trading = True,
                    auto_synced     = True,
                    source          = h["source"],
                    notes           = f"Auto-synced from {h['source'].upper()}",
                ))
                added += 1
    db.commit()
    return added


@app.post("/api/events/sync-holidays")
async def sync_holidays(user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    """
    Fetch live NSE trading holidays from NSE India + Nager Date API
    and upsert into the user's event calendar.
    Runs for current year and next year.
    """
    import pytz
    year  = datetime.now(pytz.timezone("Asia/Kolkata")).year
    total = 0
    for y in [year, year + 1]:
        total += await _sync_holidays_for_user(user.id, db, y)
    _load_holiday_cache()   # refresh in-memory cache
    return {"ok": True, "added": total,
            "message": f"Synced {total} new holidays from NSE & national calendar"}


async def _auto_sync_holidays():
    """
    Background task: sync holidays on startup (after 10s delay) and
    refresh every January 1st for the new year.
    """
    import pytz
    await asyncio.sleep(10)   # let DB init settle
    db = SessionLocal()
    try:
        users = db.query(User).filter(User.is_active == True).all()
        year  = datetime.now(pytz.timezone("Asia/Kolkata")).year
        for u in users:
            try:
                for y in [year, year + 1]:
                    await _sync_holidays_for_user(u.id, db, y)
            except Exception as e:
                log.warning(f"Auto holiday sync failed for {u.email}: {e}")
        _load_holiday_cache()
        log.info("Auto holiday sync complete")
    except Exception as e:
        log.error(f"Auto holiday sync error: {e}")
    finally:
        db.close()

    # Sleep until next Jan 1 IST, then loop yearly
    while True:
        now      = datetime.now(pytz.timezone("Asia/Kolkata"))
        next_jan = datetime(now.year + 1, 1, 1, 1, 0, tzinfo=pytz.timezone("Asia/Kolkata"))
        wait_sec = (next_jan - now).total_seconds()
        await asyncio.sleep(max(wait_sec, 86400))  # at least 1 day
        db2 = SessionLocal()
        try:
            year2 = datetime.now(pytz.timezone("Asia/Kolkata")).year
            users2 = db2.query(User).filter(User.is_active == True).all()
            for u in users2:
                try:
                    for y in [year2, year2 + 1]:
                        await _sync_holidays_for_user(u.id, db2, y)
                except Exception:
                    pass
            _load_holiday_cache()
            log.info(f"Annual holiday sync complete for {year2}")
        finally:
            db2.close()


# ── AI Morning Assessment ─────────────────────────────────────────

@app.get("/api/claude/assessment")
async def get_claude_assessment(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get today's Claude assessment. Triggers generation if not yet done."""
    import pytz
    today = datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d")
    existing = db.query(ClaudeAssessment).filter(
        ClaudeAssessment.user_id == user.id,
        ClaudeAssessment.assess_date == today
    ).first()
    if existing:
        return {"ok": True, "assessment": {
            "date": existing.assess_date,
            "trade_today": existing.trade_today,
            "confidence": existing.confidence,
            "risk_level": existing.risk_level,
            "recommended_strategies": existing.recommended_strategies,
            "avoid_strategies": existing.avoid_strategies,
            "suggested_hedge": existing.suggested_hedge,
            "vix_assessment": existing.vix_assessment,
            "gap_assessment": existing.gap_assessment,
            "reason": existing.reason,
            "event_warning": existing.event_warning,
            "ai_enabled": True,
            "generated_at": existing.created_at.strftime("%H:%M IST") if existing.created_at else "",
        }}
    # Generate fresh
    result = await _run_claude_assessment(user.id, db)
    assess = ClaudeAssessment(
        user_id=user.id, assess_date=today,
        trade_today=result.get("trade_today", True),
        confidence=result.get("confidence", "medium"),
        risk_level=result.get("risk_level", "medium"),
        recommended_strategies=result.get("recommended_strategies", []),
        avoid_strategies=result.get("avoid_strategies", []),
        suggested_hedge=result.get("suggested_hedge", 2),
        vix_assessment=result.get("vix_assessment", ""),
        gap_assessment=result.get("gap_assessment", ""),
        reason=result.get("reason", ""),
        event_warning=result.get("event_warning", ""),
        raw_response=result.get("raw_response", "")
    )
    try:
        db.add(assess); db.commit()
    except Exception:
        db.rollback()  # unique constraint if already exists
    user_ai2 = user.ai_config or {}
    _, enabled2 = _get_gemini_client(user_ai2)
    return {"ok": True, "assessment": {**result,
            "date": today, "ai_enabled": enabled2,
            "generated_at": datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%H:%M IST")}}

@app.post("/api/claude/assessment/refresh")
async def refresh_claude_assessment(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Force refresh today's assessment."""
    import pytz
    today = datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d")
    db.query(ClaudeAssessment).filter(
        ClaudeAssessment.user_id == user.id,
        ClaudeAssessment.assess_date == today
    ).delete(synchronize_session=False)
    db.commit()
    return await get_claude_assessment(user=user, db=db)

@app.post("/api/claude/ask")
async def claude_ask(
    req: dict,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Free-form question to Claude with user's trading context."""
    user_ai = user.ai_config or {}
    gemini, enabled = _get_gemini_client(user_ai)
    if not enabled:
        raise HTTPException(503, "Gemini AI not configured. Add your Google AI API key in Profile → AI Settings.")
    question = req.get("question", "").strip()
    if not question:
        raise HTTPException(400, "Question is required")

    import pytz
    ist = pytz.timezone("Asia/Kolkata")
    from datetime import timedelta
    since = (datetime.now(ist) - timedelta(days=30)).strftime("%Y-%m-%d")

    trades = db.query(ShadowTrade).filter(
        ShadowTrade.user_id == user.id,
        ShadowTrade.trade_date >= since,
        ShadowTrade.is_open == False
    ).all()
    n = len(trades)
    wins = sum(1 for t in trades if (t.net_pnl or 0) > 0)
    total_pnl = sum(t.net_pnl or 0 for t in trades)

    context = (f"Trader context: {n} paper trades last 30 days, "
               f"{round(wins/n*100,1) if n else 0}% win rate, "
               f"total P&L Rs{round(total_pnl,0)}. "
               f"Platform: AlgoDesk (NIFTY options, Iron Fly/Condor, short premium). ")

    try:
        model_name = user_ai.get("model", _GEMINI_MODEL)
        response = gemini.models.generate_content(model=model_name, contents=context + "\n\nQuestion: " + question)
        return {"ok": True, "answer": response.text.strip()}
    except Exception as e:
        raise HTTPException(500, f"Gemini error: {str(e)}")


# ── /api/ai/* route aliases (frontend uses these) ───────────────
# These forward to the same handlers as /api/claude/*

@app.get("/api/ai/assessment")
async def get_ai_assessment(user: User = Depends(get_current_user),
                             db: Session = Depends(get_db)):
    return await get_claude_assessment(user=user, db=db)

@app.post("/api/ai/assessment/refresh")
async def refresh_ai_assessment(user: User = Depends(get_current_user),
                                 db: Session = Depends(get_db)):
    return await refresh_claude_assessment(user=user, db=db)

@app.post("/api/ai/ask")
async def ai_ask(req: dict, user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    return await claude_ask(req=req, user=user, db=db)


@app.get("/api/capital/symbols")
def capital_symbols(user: User = Depends(get_current_user)):
    """Returns full symbol registry with current lot sizes."""
    return {
        "symbols": [
            {"value": sym, **info}
            for sym, info in SYMBOL_REGISTRY.items()
        ]
    }


@app.get("/api/engine/reconcile")
async def reconcile_positions(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Manual reconciliation endpoint.
    Fetches live orderbook and positions from Fyers and returns
    a full reconciliation report for the frontend to display.
    """
    conn = _get_fyers(user, db)
    if not conn:
        return {"ok": False, "message": "No broker connected"}
    if conn.mode == "paper":
        return {"ok": True, "mode": "paper",
                "message": "Paper mode — no real orders to reconcile",
                "orderbook": [], "positions": []}

    # Fetch orderbook and positions in parallel
    try:
        ob_result, pos_result = await asyncio.gather(
            conn.get_orderbook(),
            conn.get_positions(),
        )
    except Exception as e:
        return {"ok": False, "message": str(e)}

    # Process orderbook
    status_map = {
        1: "CANCELLED", 2: "FILLED", 4: "TRANSIT",
        5: "REJECTED", 6: "PENDING", 20: "EXPIRED"
    }
    orders = []
    for o in (ob_result.get("orders") or []):
        sc = o.get("status", 0)
        orders.append({
            "order_id":   o.get("id",""),
            "symbol":     o.get("symbol",""),
            "side":       "BUY" if o.get("side",0) == 1 else "SELL",
            "qty":        o.get("qty", 0),
            "filled_qty": o.get("filledQty", 0),
            "avg_price":  o.get("tradedPrice", 0),
            "status":     status_map.get(sc, f"UNKNOWN({sc})"),
            "status_code": sc,
            "product":    o.get("productType",""),
            "time":       o.get("orderDateTime",""),
            "message":    o.get("message",""),
        })

    # Process positions
    positions = []
    for p in (pos_result.get("positions") or []):
        net_qty = p.get("netQty", 0)
        if net_qty == 0:
            continue
        positions.append({
            "symbol":    p.get("symbol",""),
            "net_qty":   net_qty,
            "avg_price": p.get("netAvg", 0),
            "ltp":       p.get("ltp", 0),
            "pnl":       p.get("pl", 0),
            "product":   p.get("productType",""),
            "side":      "LONG" if net_qty > 0 else "SHORT",
        })

    # Match against today's trades in DB
    today = datetime.now().strftime("%Y-%m-%d")
    today_trades = db.query(Trade).filter(
        Trade.user_id == user.id,
        Trade.trade_date == today
    ).all()

    # Check if open trades have matching positions
    open_trades = [t for t in today_trades if t.is_open]
    mismatches = []
    for trade in open_trades:
        pos_symbols = {p["symbol"] for p in positions}
        # This is a simplified check — full check requires matching each leg
        if not pos_symbols:
            mismatches.append({
                "trade_id": trade.id,
                "strategy": trade.strategy_code,
                "issue": "Trade is open in DB but no positions found in Fyers"
            })

    return {
        "ok":           True,
        "mode":         "live",
        "orders":       orders,
        "positions":    positions,
        "open_trades":  len(open_trades),
        "mismatches":   mismatches,
        "reconciled":   len(mismatches) == 0,
        "timestamp":    datetime.now().isoformat(),
        "summary": {
            "total_orders":    len(orders),
            "filled":  len([o for o in orders if o["status_code"] == 2]),
            "rejected": len([o for o in orders if o["status_code"] == 5]),
            "pending":  len([o for o in orders if o["status_code"] == 6]),
            "open_positions": len(positions),
        }
    }


@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket, user_id: str):
    """WebSocket endpoint — sends engine status updates every 5 seconds."""
    from fastapi import WebSocket
    await websocket.accept()
    try:
        while True:
            state = _get_engine(user_id)
            if state:
                atm = state.atm
                data = {
                    "running":     state.is_running,
                    "mode":        "IN_TRADE" if state.position else "MONITORING",
                    "engine_mode": state.config.get("mode","paper"),
                    "spot":        state.spot_history[-1] if state.spot_history else 0,
                    "atm":         state.atm_strike,
                    "combined":    round(atm.current, 2) if atm else 0,
                    "vwap":        round(atm.vwap_val, 2) if atm else 0,
                    "position":    state.position,
                    "day_pnl":     round(state.day_pnl, 2),
                    "log":         state.log[-5:],
                }
            else:
                data = {"running": False, "mode": "IDLE"}
            await websocket.send_json(data)
            await asyncio.sleep(5)
    except Exception:
        pass


# ── Engine loop ───────────────────────────────────────────────

async def _run_engine(user_id: str, auto: Automation,
                      state: EngineState, conn: FyersConnection,
                      db_factory):
    """
    Main engine loop — matches N8N schedule trigger.
    Runs every 60 seconds. Refreshes token before each data call.
    """
    from engine import check_all_strategies, check_sl, nearest_strike, StrikeState
    import pytz
    from datetime import time as dtime

    state.is_running = True
    symbol  = auto.symbol
    # Get lot size from config, falling back to symbol registry
    # Always use registry lot size — it reflects SEBI-mandated changes
    # Config lot_size is used only as a multiplier hint (lots count), not lot size
    _sym_reg_lot = SYMBOL_REGISTRY.get(auto.symbol, {}).get("lot_size", 65)
    _cfg_lot = state.config.get("lot_size", 0)
    # If config lot_size matches an old value (75 for NIFTY) use registry
    if _cfg_lot == 75 and "NIFTY50" in auto.symbol:
        lot_sz = 65   # NIFTY lot size changed Nov 2025
    elif _cfg_lot == 35 and "NIFTYBANK" in auto.symbol:
        lot_sz = 30   # BANKNIFTY lot size changed
    elif _cfg_lot == 65 and "FINNIFTY" in auto.symbol:
        lot_sz = 60   # FINNIFTY lot size changed
    elif _cfg_lot == 140 and "MIDCPNIFTY" in auto.symbol:
        lot_sz = 120  # MIDCPNIFTY lot size changed
    else:
        lot_sz = int(_cfg_lot or _sym_reg_lot)
    lots    = get_position_size(state.config)

    state.emit(
        f"Engine started — {auto.name} | Mode: {auto.mode.upper()} | "
        f"Lots: {lots} (sizing: {state.config.get('position_sizing','fixed')})", "START")

    ist = pytz.timezone("Asia/Kolkata")

    # ── Defensive re-auth on engine start ─────────────────────────────────
    # Safety net: if morning TOTP re-auth failed or server restarted after 3 AM,
    # attempt re-auth now before entering the main loop so we don't trade on a
    # dead token.
    if auto.mode != "paper" and conn.fyers_id and conn.totp_key:
        _db_ra = SessionLocal()
        try:
            _bc_ra = _db_ra.query(BrokerConnection).filter(
                BrokerConnection.user_id == user_id,
                BrokerConnection.broker_id == "fyers").first()
            _reauth_needed = True
            if _bc_ra and _bc_ra.last_token_refresh:
                import pytz as _ptz
                _last_ist = (_bc_ra.last_token_refresh
                             .replace(tzinfo=_ptz.utc)
                             .astimezone(ist))
                _reauth_needed = _last_ist.date() < datetime.now(ist).date()
            if _reauth_needed:
                state.emit("Token not refreshed today — attempting TOTP re-auth before start…", "INFO")
                _ra_result = await conn.headless_login()
                if _ra_result.get("ok"):
                    conn._access = decrypt(user_id, _ra_result["access_token_enc"])
                    conn._refresh = ""
                    if _bc_ra:
                        _bc_ra.access_token_enc       = _ra_result["access_token_enc"]
                        _bc_ra.refresh_token_enc      = _ra_result.get("refresh_token_enc") or ""
                        _bc_ra.last_token_refresh     = datetime.utcnow()
                        _bc_ra.refresh_token_issued_at = datetime.utcnow()
                        _bc_ra.is_connected           = True
                        _db_ra.commit()
                    state.emit("TOTP re-auth successful — token is fresh", "OK")
                else:
                    state.emit(f"TOTP re-auth failed: {_ra_result.get('message','')} — will attempt data calls anyway", "WARN")
        except Exception as _ra_e:
            state.emit(f"Re-auth check error: {_ra_e}", "WARN")
        finally:
            _db_ra.close()

    while state.is_running:
        try:
            # Self-check: if a newer engine replaced us in active_engines, exit immediately.
            # This prevents ghost engines running invisibly after a restart.
            if (active_engines.get(user_id) or {}).get(auto_id) is not state:
                log.info(f"[engine:{auto_id}] Stale engine detected — newer instance replaced us, stopping.")
                break

            now = datetime.now(ist)
            t   = now.time()

            # Only run during market hours (9:15–15:30 Mon–Fri)
            if not (dtime(9, 15) <= t <= dtime(15, 30) and now.weekday() < 5):
                state.emit("Outside market hours. Waiting...", "INFO")
                await asyncio.sleep(60)
                continue

            # ── Day-of-week gate ──────────────────────────────────
            # run_days: list of weekday ints 0=Mon … 4=Fri, default all
            run_days = state.config.get("run_days", [0,1,2,3,4])
            if run_days and now.weekday() not in run_days:
                state.emit(
                    f"Skipping today ({now.strftime('%A')}) — not in run_days {run_days}", "INFO")
                await asyncio.sleep(3600)  # sleep 1hr, recheck
                continue

            # ── Skip-dates gate ───────────────────────────────────
            today_str = now.strftime("%Y-%m-%d")
            skip_dates = state.config.get("skip_dates", [])
            if today_str in skip_dates:
                state.emit(f"Skipping {today_str} — in skip_dates list", "INFO")
                await asyncio.sleep(3600)
                continue

            # ── Event calendar gate (once per day at 9:15) ────────
            if t >= dtime(9, 15) and not state.event_checked:
                state.event_checked = True
                db_ev = SessionLocal()
                try:
                    events_sus = db_ev.query(TradingEvent).filter(
                        TradingEvent.user_id == user_id,
                        TradingEvent.event_date == today_str,
                        TradingEvent.suspend_trading == True
                    ).all()
                    if events_sus:
                        names = ", ".join(e.event_name for e in events_sus)
                        state.emit(
                            f"⚠️ Trading suspended today — Event: {names}. "                            f"To trade anyway, toggle suspend off in Event Calendar.", "WARN")
                        state.events_suspended = True
                        state.guard_status = f"Event Gate — {names}"
                    else:
                        state.events_suspended = False
                except Exception:
                    state.events_suspended = False
                finally:
                    db_ev.close()

            if getattr(state, 'events_suspended', False):
                await asyncio.sleep(60)
                continue

            # ── AI assessment gate (once per day at 9:15) ──────────
            if t >= dtime(9, 15) and not state.ai_checked:
                state.ai_checked = True
                db_cl = SessionLocal()
                try:
                    user_cl = db_cl.query(User).filter(User.id == user_id).first()
                    ai_cfg  = (user_cl.ai_config or {}) if user_cl else {}
                    use_trading = ai_cfg.get("use_for_trading", False)
                    if use_trading:
                        assess = db_cl.query(ClaudeAssessment).filter(
                            ClaudeAssessment.user_id == user_id,
                            ClaudeAssessment.assess_date == today_str
                        ).first()
                        if assess:
                            state.ai_avoid = assess.avoid_strategies or []
                            risk_lvl = assess.risk_level or "medium"
                            # Per-automation setting takes priority; fall back to global
                            news_suspend = config.get("ai_news_gate", ai_cfg.get("news_suspend_enabled", True))
                            news_threshold = config.get("ai_news_threshold", ai_cfg.get("news_risk_threshold", "high"))
                            # Determine if news/risk gate should block trading
                            risk_blocks = {
                                "any":    True,
                                "medium": risk_lvl in ("medium","high"),
                                "high":   risk_lvl == "high",
                            }.get(news_threshold, risk_lvl == "high")
                            if not assess.trade_today:
                                reason_msg = assess.reason or "AI assessment"
                                state.emit(
                                    f"🤖 AI GATE: Skip today — {reason_msg} | "
                                    f"Risk={risk_lvl} | Confidence={assess.confidence}. "
                                    f"Disable AI trading gate in Profile to override.",
                                    "WARN")
                                state.ai_suspended = True
                                state.guard_status = f"AI Gate — Skip today (Risk: {risk_lvl})"
                            elif news_suspend and risk_blocks and assess.event_warning:
                                # News/event override — high risk day with a warning
                                state.emit(
                                    f"📰 AI NEWS GATE: Suspended — {assess.event_warning} | "
                                    f"Risk={risk_lvl}. Toggle 'Suspend on high-risk days' in Profile to override.",
                                    "WARN")
                                state.ai_suspended = True
                                state.guard_status = f"AI News Gate — {assess.event_warning[:60]}"
                            else:
                                state.ai_suspended = False
                                if state.ai_avoid:
                                    state.emit(
                                        f"🤖 AI: Trade ✅ | Avoid: {state.ai_avoid} | "
                                        f"Risk={risk_lvl} | {assess.reason}", "INFO")
                                elif assess.event_warning:
                                    state.emit(f"📰 AI: {assess.event_warning} | Risk={risk_lvl} (within threshold, continuing)", "INFO")
                        else:
                            state.ai_avoid = []
                            state.ai_suspended = False
                    else:
                        state.ai_avoid = []
                        state.ai_suspended = False
                except Exception as e:
                    log.error(f"AI gate error: {e}")
                    state.ai_avoid = []
                    state.ai_suspended = False
                finally:
                    db_cl.close()

            if getattr(state, 'ai_suspended', False):
                await asyncio.sleep(60)
                continue

            # Get fresh data (token auto-refreshes inside)
            data = await conn.get_spot_and_chain(symbol, strike_count=25)

            if not data.get("ok"):
                state.emit(f"Data error: {data.get('message')}", "WARN")
                state._data_fail_count = getattr(state, "_data_fail_count", 0) + 1
                # After 5 consecutive failures (~5 min) alert user via Telegram
                if state._data_fail_count == 5:
                    db_tf = SessionLocal()
                    try:
                        u_tf = db_tf.query(User).filter(User.id == user_id).first()
                        if u_tf:
                            await _send_telegram_all(u_tf,
                                f"⚠️ AlgoDesk: Broker data failing for 5+ min on automation '{auto.name}'. "
                                f"Reason: {data.get('message','')}. Check Fyers connection.")
                    finally:
                        db_tf.close()
                await asyncio.sleep(60)
                continue
            state._data_fail_count = 0  # reset on success

            # Save refreshed tokens
            db = SessionLocal()
            try:
                _save_tokens(user_id, conn, data.get("refresh_tokens", {}), db)
            finally:
                db.close()

            spot  = data["spot"]
            chain = data["chain"]
            state.spot_history.append(spot)
            if len(state.spot_history) > 400:
                state.spot_history = state.spot_history[-400:]

            # Lock ATM at 9:15
            if t >= dtime(9, 15) and not state.atm_strike:
                state.spot_locked = spot
                state.atm_strike  = nearest_strike(spot)
                sides = state.config.get("strike_sides", 20)  # ±1000pt window (20 strikes × 50pts)
                gap   = state.config.get("strike_round", 50)
                for i in range(-sides, sides + 1):
                    sk = StrikeState(
                        strike=state.atm_strike + i * gap,
                        offset=i, is_atm=(i == 0))
                    state.strikes.append(sk)
                # Capture expiry weekday from live options data (once per day)
                if state.expiry_weekday is None and data.get("expiry_weekday") is not None:
                    state.expiry_weekday = data["expiry_weekday"]
                    _expiry_day_name = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][state.expiry_weekday]
                    state.emit(
                        f"ATM locked: {state.atm_strike} | Spot: {spot:.1f} | "
                        f"Expiry: {data.get('expiry_date','')} ({_expiry_day_name})", "OK")
                else:
                    state.emit(f"ATM locked: {state.atm_strike} | Spot: {spot:.1f}", "OK")

            # ── Inject VIX + prev_close into engine config ──
            # No time gate — retries every tick until data lands in market cache.
            # Guards 2/5 and S8/S10 silently skip if these are 0, so we warn when set.
            _mc = user_market_cache.get(user_id, {})
            if _mc.get("vix") and not state.config.get("vix_open"):
                state.config["vix_open"] = _mc["vix"]
                _vmax = state.config.get("vix_max", 17.0)
                state.emit(
                    f"India VIX: {_mc['vix']:.1f} "
                    f"(Guard 2 threshold: {_vmax:.1f} — "
                    f"{'🔴 WILL BLOCK today' if _mc['vix'] >= _vmax else '✅ within limit'})",
                    "INFO")
            if _mc.get("prev_close") and not state.config.get("prev_close"):
                state.config["prev_close"]        = _mc["prev_close"]
                state.config["prev_day_move_pct"] = _mc.get("prev_day_move_pct", 0)
                state.emit(
                    f"Prev close: {_mc['prev_close']:.0f} | "
                    f"Yesterday move: {_mc.get('prev_day_move_pct', 0):.1f}% "
                    f"(S8/S10 + Gap guard active)",
                    "INFO")

            # Update strike data from chain
            if state.strikes:
                for sk in state.strikes:
                    cd = chain.get(sk.strike)
                    if cd:
                        sk.update(cd["combined"])   # computes VWAP + EMA75 — do NOT use .append()
                        sk.ce_symbol = cd.get("ce_symbol", "")
                        sk.pe_symbol = cd.get("pe_symbol", "")
                        # Build ORB
                        if dtime(9, 15) <= t <= dtime(9, 21):
                            if sk.orb_high == 0:
                                sk.orb_high = sk.orb_low = cd["combined"]
                            sk.orb_high = max(sk.orb_high, cd["combined"])
                            sk.orb_low  = min(sk.orb_low,  cd["combined"])

            # ORB complete
            if t >= dtime(9, 22) and not state.orb_complete:
                state.orb_complete = True
                atm = state.atm
                if atm:
                    state.emit(
                        f"ORB complete. ATM {atm.strike}: "
                        f"Low={atm.orb_low:.1f} High={atm.orb_high:.1f}", "OK")

            # M8: Re-initialise strikes at 10:30 if spot drifted >2 strikes from morning ATM
            # Ensures S9 (fires at 11am) trades at real current ATM, not stale 9:15 level
            if (t >= dtime(10, 30) and not getattr(state, "_strikes_refreshed", False)
                    and state.atm_strike and state.spot_history):
                live_atm = nearest_strike(state.spot_history[-1])
                gap = state.config.get("strike_round", 50)
                drift = abs(live_atm - state.atm_strike) / gap
                if drift >= 2:
                    sides = state.config.get("strike_sides", 20)
                    new_strikes = []
                    for i in range(-sides, sides + 1):
                        sk = StrikeState(strike=live_atm + i * gap, offset=i, is_atm=(i == 0))
                        cd = chain.get(sk.strike)
                        if cd:
                            sk.update(cd["combined"], ce_ltp=cd.get("ce_ltp", 0), pe_ltp=cd.get("pe_ltp", 0))
                            sk.ce_symbol = cd.get("ce_symbol", "")
                            sk.pe_symbol = cd.get("pe_symbol", "")
                        new_strikes.append(sk)
                    state.strikes = new_strikes
                    state.atm_strike = live_atm
                    state._strikes_refreshed = True
                    state.emit(f"Strikes refreshed at 10:30 — live ATM {live_atm} (was {state.atm_strike}, drift {drift:.0f} strikes)", "INFO")

            # ── Full day reset at 9:15 each morning ──────────────────
            # Triggers when traded_today is True (meaning yesterday had activity)
            # OR when atm_strike is set (meaning yesterday's state is stale)
            if t == dtime(9, 15) and (state.traded_today or state.atm_strike):
                state.traded_today    = False
                state.trade_count     = 0
                state.atm_strike      = None
                state.spot_locked     = None
                state.strikes         = []
                state.orb_complete    = False
                state.event_checked   = False
                state.events_suspended = False
                state.ai_checked      = False
                state.ai_suspended    = False
                state.ai_avoid        = []
                state.guard_status    = ""
                state.day_pnl         = 0.0
                state.spot_history    = []
                state.sl_state        = SLState()
                state._strikes_refreshed = False
                state.reentry_count     = 0
                state.reentry_pending   = False
                state.reentry_direction = ""
                state.expiry_weekday    = None  # re-read from live options data each day
                # Clear daily injected config values so they re-lock today
                state.config.pop("vix_open", None)
                state.config.pop("prev_close", None)
                state.emit("New trading day — all daily state reset", "OK")

            # Auto-exit time
            exit_time = state.config.get("auto_exit_time", "14:00")
            eh, em = map(int, exit_time.split(":"))
            if t >= dtime(eh, em) and state.position:
                await _close_position(state, conn, "AUTO_EXIT",
                                       lot_sz, lots, user_id)

            # Check SL
            elif state.position:
                atm  = state.atm
                pos_sig = state.position.get("signal", {})

                # ── S10 BUY SL checks ─────────────────────────────
                if pos_sig.get("is_buy"):
                    direction  = pos_sig.get("direction", "CE")
                    entry_ltp  = state.position.get("entry_combined", 0)
                    entry_spot = state.position.get("entry_spot", 0)
                    cur_ltp    = (atm.ce_ltp if direction == "CE" else atm.pe_ltp) if atm else 0

                    # Update peak LTP (ratchet — never goes down)
                    if cur_ltp > 0:
                        prev_peak = state.position.get("s10_peak_ltp", entry_ltp)
                        state.position["s10_peak_ltp"] = max(prev_peak, cur_ltp)
                    peak_ltp = state.position.get("s10_peak_ltp", entry_ltp)

                    # Arm cost SL once +20% reached (only once, never disarms)
                    if entry_ltp > 0 and not state.position.get("s10_cost_armed"):
                        if cur_ltp >= entry_ltp * 1.20:
                            state.position["s10_cost_armed"] = True
                            state.emit(
                                f"S10 +20% reached (entry={entry_ltp:.1f} now={cur_ltp:.1f}) "
                                f"— SL moved to cost ₹{entry_ltp:.1f}", "INFO")

                    # Arm trail SL once +50% reached (only once, never disarms)
                    if entry_ltp > 0 and not state.position.get("s10_trail_armed"):
                        if cur_ltp >= entry_ltp * 1.50:
                            state.position["s10_trail_armed"] = True
                            trail_sl = entry_ltp + 0.5 * (peak_ltp - entry_ltp)
                            state.emit(
                                f"S10 +50% reached (entry={entry_ltp:.1f} now={cur_ltp:.1f}) "
                                f"— Trail SL armed at ₹{trail_sl:.1f}", "INFO")

                    # Compute current trail SL level (entry + 50% of peak profit)
                    trail_sl = (entry_ltp + 0.5 * (peak_ltp - entry_ltp)
                                if state.position.get("s10_trail_armed") else None)

                    # Hard exit at 11:00 AM
                    if t >= dtime(11, 0):
                        await _close_position(state, conn, "S10_HARD_EXIT_11AM",
                                               lot_sz, lots, user_id)

                    # Spot reversal SL: 30pts against direction (checked before premium SLs)
                    elif entry_spot and state.spot_history:
                        cur_spot = state.spot_history[-1]
                        reversal = cur_spot - entry_spot
                        if direction == "CE" and reversal <= -30:
                            await _close_position(state, conn,
                                f"S10_SPOT_REVERSAL entry={entry_spot:.0f} now={cur_spot:.0f}",
                                lot_sz, lots, user_id)
                        elif direction == "PE" and reversal >= 30:
                            await _close_position(state, conn,
                                f"S10_SPOT_REVERSAL entry={entry_spot:.0f} now={cur_spot:.0f}",
                                lot_sz, lots, user_id)

                    # Trail SL: price fell back to entry + 50% of peak profit
                    elif trail_sl is not None and cur_ltp > 0 and cur_ltp <= trail_sl:
                        await _close_position(state, conn,
                            f"S10_TRAIL_SL peak={peak_ltp:.1f} trail={trail_sl:.1f} now={cur_ltp:.1f}",
                            lot_sz, lots, user_id)

                    # Cost SL: once +20% hit, don't let it fall back below entry
                    elif state.position.get("s10_cost_armed") and cur_ltp > 0 and cur_ltp <= entry_ltp:
                        await _close_position(state, conn,
                            f"S10_COST_SL entry={entry_ltp:.1f} now={cur_ltp:.1f}",
                            lot_sz, lots, user_id)

                    # Loss SL: before +20% hit, cap loss at 40%
                    elif not state.position.get("s10_cost_armed") and cur_ltp > 0 and cur_ltp <= entry_ltp * 0.60:
                        await _close_position(state, conn,
                            f"S10_PREMIUM_SL entry={entry_ltp:.1f} now={cur_ltp:.1f}",
                            lot_sz, lots, user_id)

                else:
                    # Standard SL for selling strategies
                    reason = check_sl(state)
                    if reason or state.position.get("force_exit"):
                        reason = reason or "FORCE_EXIT"
                        # Save entry_spot before _close_position wipes state.position
                        _pre_entry_spot = (state.position or {}).get(
                            "entry_spot", state.atm_strike or 0)
                        await _close_position(state, conn, reason,
                                               lot_sz, lots, user_id)
                        db2 = SessionLocal()
                        try:
                            u = db2.query(User).filter(User.id == user_id).first()
                            if u:
                                mode_tag = "🔴 LIVE" if auto.mode == "live" else "📋 PAPER"
                                _tg_close = (auto.config or {}).get("telegram_accounts", [])
                                await _send_telegram_all(u,
                                    f"{mode_tag} Position closed\nReason: {reason}\nP&L: ₹{state.day_pnl:.0f}",
                                    account_ids=_tg_close if _tg_close else None)
                        finally:
                            db2.close()

                        # ── Re-entry after VWAP/EMA SL ──────────────────────
                        if (state.config.get("reentry_on_sl", False) and
                                ("VWAP" in reason or "EMA" in reason)):
                            # Use expiry weekday from live options data if available,
                            # else fall back to known defaults: NIFTY=Tue(1), SENSEX=Thu(3)
                            _expiry_wd = state.expiry_weekday
                            _is_expiry = (now.weekday() == _expiry_wd
                                          if _expiry_wd is not None
                                          else now.weekday() in (1, 3))
                            _remax = int(state.config.get(
                                "reentry_max_expiry" if _is_expiry else "reentry_max_count",
                                1 if _is_expiry else 2))
                            if state.reentry_count < _remax:
                                _live_spot = (state.spot_history[-1]
                                              if state.spot_history else _pre_entry_spot)
                                _direction = "UP" if _live_spot > _pre_entry_spot else "DOWN"
                                state.reentry_count    += 1
                                state.reentry_pending   = True
                                state.reentry_direction = _direction
                                state.traded_today      = False  # re-open the gate
                                state.emit(
                                    f"Re-entry {state.reentry_count}/{_remax} queued — "
                                    f"market moved {_direction} "
                                    f"(entry spot {_pre_entry_spot:.0f} → now {_live_spot:.0f}). "
                                    f"Will enter {state.config.get('reentry_offset', 2)} strikes "
                                    f"{'above' if _direction == 'UP' else 'below'} live ATM "
                                    f"on next tick.", "INFO")

            # Check signals — re-entry takes priority, then normal strategy signals
            elif state.orb_complete:
                # ── Fire pending re-entry first ────────────────────────────
                if state.reentry_pending and not state.position:
                    state.reentry_pending = False
                    from engine import _reentry_signal
                    _re_offset = int(state.config.get("reentry_offset", 2))
                    signal = _reentry_signal(state, state.reentry_direction, _re_offset)
                    if signal:
                        signal["hedge_width"] = 20
                        state.emit(
                            f"Re-entry signal: sell at {signal['strike']} "
                            f"({state.reentry_direction}, {_re_offset} strikes "
                            f"from live ATM {nearest_strike(state.spot_history[-1]) if state.spot_history else '?'})",
                            "SIGNAL")
                        await _open_position(state, conn, signal, lot_sz, lots, user_id, auto.id)
                    else:
                        state.emit("Re-entry signal skipped — strike unavailable", "WARN")
                    await asyncio.sleep(60)
                    continue

                # ── Normal signal flow ──────────────────────────────────────
                max_trades = int(state.config.get("max_trades_per_day", 1))
                at_limit = (max_trades > 0 and state.trade_count >= max_trades)
                if at_limit:
                    if state.sl_state.candles % 15 == 0:
                        state.emit(
                            f"Trade limit reached: {state.trade_count}/{max_trades} today — watching but will not re-enter", "INFO")
                signal = check_all_strategies(state, now) if not at_limit else None
                if signal:
                    # Authoritative hedge width per strategy.
                    # Signal already carries hedge_width from _build_legs — this
                    # dict ensures the DB record and SL calculation match the
                    # actual order placement regardless of user config defaults.
                    auto_hedges = {
                        "S1":20, "S1_RE":20,           # ±1000pt Iron Fly
                        "S2":20, "S3":20, "S7":20,     # ±1000pt Iron Fly variants
                        "S4":20, "S6":4, "S8":3,       # Condors / gap fade
                        "S9":1,                         # Expiry tight hedge
                    }
                    hw = auto_hedges.get(signal["code"],
                                        signal.get("hedge_width",
                                        state.config.get("hedge_width", 20)))
                    signal["hedge_width"] = hw
                    state.emit(f"SIGNAL [{signal['code']}]: {signal['reason']} | hedge=±{hw}", "SIGNAL")
                    await _open_position(state, conn, signal,
                                          lot_sz, lots, user_id, auto.id)
                    db3 = SessionLocal()
                    try:
                        u = db3.query(User).filter(User.id == user_id).first()
                        if u:
                            mode_tag = "🔴 LIVE" if auto.mode=="live" else "📋 PAPER"
                            tg_ids = (auto.config or {}).get("telegram_accounts", [])
                            msg = (f"{mode_tag} Signal: {signal['code']} — {signal['name']}\n"
                                   f"Strike: {signal.get('strike','')} | Combined: ₹{signal.get('combined',0):.1f}\n"
                                   f"Hedge: ±{signal.get('hedge_width',20)} | Reason: {signal.get('reason','')}\n"
                                   f"{'⚠️ Simulation only' if auto.mode=='paper' else '✅ Live order placed'}")
                            await _send_telegram_all(u, msg, account_ids=tg_ids if tg_ids else None)
                    finally:
                        db3.close()
            else:
                atm = state.atm
                if atm:
                    state.emit(
                        f"Spot={spot:.1f} ATM={state.atm_strike} "
                        f"Combined={atm.current:.1f} "
                        f"VWAP={atm.vwap_val:.1f}", "INFO")

        except Exception as e:
            log.error(f"[engine:{user_id}] {e}")
            state.emit(f"Error: {str(e)}", "ERROR")

        await asyncio.sleep(60)

    state.is_running = False
    state.emit("Engine stopped.", "INFO")


async def _open_position(state, conn, signal, lot_sz, lots, user_id, auto_id):
    """
    Places all 4 legs of an Iron Fly/Condor as a basket order (live mode).
    Basket orders are atomic — all legs fill together or none do.
    This prevents partial fills leaving you unhedged.
    Falls back to individual orders if basket fails.
    """
    qty = lot_sz * lots
    is_live = conn.mode != "paper"
    orders = []

    # ── S10 BUY strategy: single-leg order ────────────────────────
    if signal.get("is_buy"):
        buy_sym = signal.get("buy_symbol")
        if not buy_sym:
            state.emit("❌ S10: no buy_symbol in signal", "ERROR")
            return
        state.emit(f"S10 BUY: placing {qty}x {buy_sym}", "ORDER")
        if is_live:
            r = await conn.place_order(buy_sym, "BUY", qty)
        else:
            r = {"ok": True, "order_id": "PAPER"}
        if not r.get("ok"):
            state.emit(f"❌ S10 BUY failed: {r.get('message')}", "ERROR")
            return
        orders = [{"leg":"buy_option","symbol":buy_sym,"side":"BUY",
                   "order_id":r.get("order_id","PAPER"),"ok":True}]
        state.emit(f"✅ S10 BUY {qty}x {buy_sym} id={r.get('order_id','PAPER')}", "ORDER")

        entry_ltp = signal.get("entry_ltp", signal.get("combined", 0))
        state.position = {
            "signal": signal, "entry_combined": entry_ltp,
            "entry_time": datetime.now().isoformat(),
            "entry_spot": state.spot_history[-1] if state.spot_history else 0,
            "orders": orders,
        }

        db = SessionLocal()
        try:
            import pytz as _pytz
            _ist = _pytz.timezone("Asia/Kolkata")
            ist_now = datetime.now(_ist).replace(tzinfo=None)
            trade_mode = state.config.get("mode", "paper")

            if trade_mode == "paper":
                trade = ShadowTrade(
                    user_id=user_id, automation_id=auto_id,
                    trade_date=ist_now.strftime("%Y-%m-%d"),
                    symbol=state.config.get("symbol", "NSE:NIFTY50-INDEX"),
                    strategy_code=signal["code"],
                    atm_strike=state.atm_strike,
                    entry_combined=entry_ltp,
                    entry_spot=state.spot_history[-1] if state.spot_history else 0,
                    entry_time=ist_now,
                    lots=lots, lot_size=lot_sz, hedge_width=0,
                    max_profit=entry_ltp * qty * 3,   # rough: 3x on a big gap move
                    max_loss=entry_ltp * qty,          # max loss = premium paid
                    is_open=True, signal_data=signal, last_monitored=ist_now)
                db.add(trade); db.commit()
                state.position["trade_id"] = trade.id
                state.position["trade_table"] = "shadow"
            else:
                trade = Trade(
                    user_id=user_id, automation_id=auto_id,
                    trade_date=ist_now.strftime("%Y-%m-%d"),
                    symbol=state.config.get("symbol", "NIFTY"),
                    strategy_code=signal["code"], mode=trade_mode,
                    atm_strike=state.atm_strike,
                    sell_ce_strike=0, sell_pe_strike=0,
                    entry_combined=entry_ltp, net_credit=-entry_ltp,  # debit (buy)
                    lots=lots, lot_size=lot_sz, entry_time=ist_now,
                    is_open=True, signal_data=signal, orders=orders)
                db.add(trade); db.commit()
                state.position["trade_id"] = trade.id
                state.position["trade_table"] = "live"
        finally:
            db.close()

        state.traded_today = True
        state.trade_count += 1
        state.emit(
            f"S10 position open: BUY {signal['direction']} @ ₹{entry_ltp:.1f} | "
            f"SL: ₹{entry_ltp*0.60:.1f} (40% drop) or spot reversal 30pts | "
            f"Hard exit 11:00 AM", "OK")
        return

    # Build all 4 legs for basket order
    legs_to_place = []
    leg_map = []

    # BUY hedge legs placed FIRST — hedge must be in place before selling
    # Prevents naked short exposure and ensures margin relief from first order
    for leg, side in [("buy_ce","BUY"),("buy_pe","BUY"),
                      ("sell_ce","SELL"),("sell_pe","SELL")]:
        sym = signal.get(leg)
        if sym:
            legs_to_place.append({"symbol":sym, "side":side, "qty":qty})
            leg_map.append({"leg":leg, "symbol":sym, "side":side})

    if not legs_to_place:
        state.emit("❌ No symbols in signal — cannot place orders", "ERROR")
        return

    # Try basket order first (best approach for multi-leg options)
    if is_live and hasattr(conn, "place_basket_order"):
        state.emit(f"Placing basket order: {len(legs_to_place)} legs", "ORDER")
        result = await conn.place_basket_order(legs_to_place)
        if result.get("ok"):
            order_ids = result.get("order_ids", [])
            for i, lm in enumerate(leg_map):
                oid = order_ids[i] if i < len(order_ids) else "unknown"
                orders.append({"leg":lm["leg"],"symbol":lm["symbol"],
                               "side":lm["side"],"order_id":oid,"ok":True})
                state.emit(f"✅ {lm['side']} {qty}x {lm['symbol']} id={oid}", "ORDER")

            # ── RECONCILE ENTRY ORDERS ────────────────────────────
            # Wait for all legs to reach terminal state (max 30s)
            state.emit("Reconciling entry orders...", "ORDER")
            recon = await conn.reconcile_orders(order_ids, max_wait_secs=30)
            state.emit(f"Reconcile: {recon['summary']}", "OK" if recon["all_filled"] else "WARN")

            if recon.get("any_rejected") and is_live:
                # Some legs rejected — emergency close whatever filled
                state.emit("❌ Entry rejection — closing filled legs", "ERROR")
                for o in recon.get("orders", []):
                    if o.get("status_code") == 2:  # filled
                        sym = o.get("symbol") or next(
                            (lm["symbol"] for lm in leg_map
                             if lm.get("order_id") == o.get("order_id")), None)
                        if sym:
                            side = "BUY" if o.get("side") == "SELL" else "SELL"
                            await conn.place_order(sym, side, qty)
                            state.emit(f"↩ Emergency close {sym}", "ORDER")
                # Also store rejection details
                for r_order in recon.get("rejected", []):
                    state.emit(
                        f"REJECTED: {r_order.get('symbol','')} "
                        f"reason={r_order.get('message','')}",
                        "ERROR")
                return

            # Store actual fill prices in orders list
            for o in recon.get("orders", []):
                for stored_order in orders:
                    if stored_order.get("order_id") == o.get("order_id"):
                        stored_order["fill_price"] = o.get("avg_price", 0)
                        stored_order["filled_qty"] = o.get("filled_qty", 0)
                        stored_order["status"] = o.get("status", "")

            # Verify positions exist in Fyers account
            expected_syms = [lm["symbol"] for lm in leg_map]
            pos_recon = await conn.get_positions_reconcile(expected_syms)
            if pos_recon.get("reconciled"):
                state.emit("✅ Position reconciled — all legs confirmed in account",
                           "OK")
            else:
                state.emit(f"⚠️ Position check: {pos_recon.get('message','')}",
                           "WARN")

        else:
            state.emit(f"⚠️ Basket order failed: {result.get('message')} — falling back to individual orders", "WARN")
            # Fall through to individual orders below
            orders = []

    # Individual orders (paper mode OR basket failed)
    if not orders:
        failed = []
        for lm in leg_map:
            placed = False
            for attempt in range(3):
                r = await conn.place_order(lm["symbol"], lm["side"], qty)
                if r.get("ok"):
                    orders.append({"leg":lm["leg"],"symbol":lm["symbol"],
                                  "side":lm["side"],
                                  "order_id":r.get("order_id","PAPER"),"ok":True})
                    state.emit(f"✅ {lm['side']} {qty}x {lm['symbol']} id={r.get('order_id','PAPER')}", "ORDER")
                    placed = True
                    break
                else:
                    if attempt < 2:
                        state.emit(f"⚠️ {lm['side']} {lm['symbol']} attempt {attempt+1}: {r.get('message')}", "WARN")
                        await asyncio.sleep(2)
                    else:
                        state.emit(f"❌ {lm['side']} {lm['symbol']} FAILED: {r.get('message')}", "ERROR")
                        failed.append(lm["leg"])

        # If sell legs failed on live — abort and reverse any fills
        if is_live and any(f in failed for f in ["sell_ce","sell_pe"]):
            state.emit("❌ Sell legs failed — reversing all fills", "ERROR")
            for o in orders:
                rev_side = "BUY" if o["side"]=="SELL" else "SELL"
                await conn.place_order(o["symbol"], rev_side, qty)
                state.emit(f"↩ Reversed {o['symbol']}", "ORDER")
            return

    atm = state.atm
    combined = atm.current if atm else signal["combined"]
    state.position = {
        "signal": signal, "entry_combined": combined,
        "entry_time": datetime.now().isoformat(),
        "orders": orders,
    }

    # ── CRITICAL: Activate SL with real entry price ───────────────
    from datetime import date as _date
    _today_wd  = _date.today().weekday()
    _expiry_wd = state.expiry_weekday if state.expiry_weekday is not None else 3
    _dte       = min((_expiry_wd - _today_wd) % 7, 3)
    _dte_map   = state.config.get("dte_profit_map", {})
    _dte_profit_pct = _dte_map.get(str(_dte), _dte_map.get("default", 40))
    _sl_rupees = state.config.get("sl_rupees", 18)

    _sl_cfg = {
        "sl_rupees":         _sl_rupees,
        "trail_pct":         state.config.get("trail_pct",         20),
        "min_profit_pct":    state.config.get("min_profit_pct",    15),
        "vwap_buffer_pct":   state.config.get("vwap_buffer_pct",    2),
        "ema_buffer_pct":    state.config.get("ema_buffer_pct",     1),
        "dte_profit_pct":    _dte_profit_pct,
    }
    state.sl_state.reset()
    state.sl_state.activate(combined, _sl_cfg)
    state.emit(
        f"SL armed: entry=₹{combined:.1f} | "
        f"max_loss=₹{combined + _sl_rupees:.1f} (+₹{_sl_rupees}) | "
        f"target=₹{combined*(1-_dte_profit_pct/100):.1f} ({_dte_profit_pct}% decay, DTE {_dte}) | "
        f"trail at ₹{combined*(1-_sl_cfg['min_profit_pct']/100):.1f}",
        "OK")

    db = SessionLocal()
    try:
        import pytz as _pytz
        _ist = _pytz.timezone("Asia/Kolkata")
        ist_now = datetime.now(_ist).replace(tzinfo=None)  # Store as IST naive for consistency
        trade_mode = state.config.get("mode", "paper")

        if trade_mode == "paper":
            # Paper trades → ShadowTrade table for Results page
            # Calculate expected max profit and max loss for this position
            hw = signal.get("hedge_width", 2)
            gap = SYMBOL_REGISTRY.get(
                state.config.get("symbol", "NSE:NIFTY50-INDEX"), {}
            ).get("strike_gap", 50)
            max_profit = combined * qty           # if both legs expire worthless
            max_loss = (hw * gap * qty) - combined * qty  # defined by hedge

            trade = ShadowTrade(
                user_id=user_id, automation_id=auto_id,
                trade_date=ist_now.strftime("%Y-%m-%d"),
                symbol=state.config.get("symbol", "NSE:NIFTY50-INDEX"),
                strategy_code=signal["code"],
                atm_strike=state.atm_strike,
                entry_combined=combined, entry_spot=state.spot_history[-1] if state.spot_history else 0,
                entry_time=ist_now,
                lots=lots, lot_size=lot_sz,
                hedge_width=hw,
                max_profit=round(max_profit, 0),
                max_loss=round(max_loss, 0),
                is_open=True, signal_data=signal,
                last_monitored=ist_now)
            db.add(trade); db.commit()
            state.position["trade_id"] = trade.id
            state.position["trade_table"] = "shadow"
        else:
            # Live trades → Trade table
            trade = Trade(
                user_id=user_id, automation_id=auto_id,
                trade_date=ist_now.strftime("%Y-%m-%d"),
                symbol=state.config.get("symbol", "NIFTY"),
                strategy_code=signal["code"], mode=trade_mode,
                atm_strike=state.atm_strike,
                sell_ce_strike=signal.get("sell_ce_strike", signal["strike"]),
                sell_pe_strike=signal.get("sell_pe_strike", signal["strike"]),
                entry_combined=combined, net_credit=combined,
                lots=lots, lot_size=lot_sz, entry_time=ist_now,
                is_open=True, signal_data=signal, orders=orders)
            db.add(trade); db.commit()
            state.position["trade_id"] = trade.id
            state.position["trade_table"] = "live"
    finally:
        db.close()


async def _close_position(state, conn, reason, lot_sz, lots, user_id):
    if not state.position: return
    qty = lot_sz * lots
    atm = state.atm
    sig = state.position.get("signal", {})

    # ── S10 BUY close: single-leg SELL ────────────────────────────
    if sig.get("is_buy"):
        direction = sig.get("direction", "CE")
        cur_ltp = (atm.ce_ltp if direction == "CE" else atm.pe_ltp) if atm else 0
        entry_ltp = state.position.get("entry_combined", 0)
        exit_combined = cur_ltp if cur_ltp > 0 else entry_ltp
        pnl_per_lot = (exit_combined - entry_ltp) * lot_sz
        pnl_total   = pnl_per_lot * lots

        # Place closing SELL
        buy_sym = sig.get("buy_symbol")
        if buy_sym and conn.mode != "paper":
            r = await conn.place_order(buy_sym, "SELL", qty)
            state.emit(f"S10 CLOSE: SELL {qty}x {buy_sym} — {r.get('ok')}", "ORDER")
        else:
            state.emit(f"S10 CLOSE (paper): SELL {qty}x {buy_sym or 'N/A'}", "ORDER")

        state.day_pnl += pnl_total
        state.emit(
            f"S10 closed [{reason}] — entry=₹{entry_ltp:.1f} exit=₹{exit_combined:.1f} "
            f"P&L=₹{pnl_total:.0f}", "OK")

        # Update DB record
        db = SessionLocal()
        try:
            import pytz as _pytz
            _ist = _pytz.timezone("Asia/Kolkata")
            ist_now = datetime.now(_ist).replace(tzinfo=None)
            trade_id = state.position.get("trade_id")
            if trade_id:
                tbl = state.position.get("trade_table", "shadow")
                if tbl == "shadow":
                    t_rec = db.query(ShadowTrade).filter(ShadowTrade.id == trade_id).first()
                    if t_rec:
                        t_rec.exit_combined = exit_combined
                        t_rec.exit_time = ist_now
                        t_rec.exit_reason = reason
                        t_rec.pnl = pnl_total
                        t_rec.is_open = False
                        db.commit()
                else:
                    t_rec = db.query(Trade).filter(Trade.id == trade_id).first()
                    if t_rec:
                        t_rec.exit_combined = exit_combined
                        t_rec.exit_time = ist_now
                        t_rec.exit_reason = reason
                        t_rec.pnl = pnl_total
                        t_rec.is_open = False
                        db.commit()
        finally:
            db.close()

        state.position = None
        state.sl_state.reset()
        return

    exit_combined = atm.current if atm else state.position["entry_combined"]

    # Build closing legs (reverse of entry)
    close_legs = []
    for order in state.position.get("orders", []):
        sym  = order.get("symbol")
        side = "BUY" if order.get("side","SELL") == "SELL" else "SELL"
        if sym:
            close_legs.append({"symbol":sym, "side":side, "qty":qty})

    # Try basket close first
    if conn.mode != "paper" and close_legs and hasattr(conn, "place_basket_order"):
        result = await conn.place_basket_order(close_legs)
        if result.get("ok"):
            state.emit(f"✅ Basket close executed: {len(close_legs)} legs", "ORDER")
            close_order_ids = result.get("order_ids", [])

            # ── RECONCILE EXIT ORDERS ────────────────────────────
            state.emit("Reconciling exit orders...", "ORDER")
            recon = await conn.reconcile_orders(close_order_ids, max_wait_secs=30)
            state.emit(f"Exit reconcile: {recon['summary']}",
                       "OK" if recon["all_filled"] else "WARN")

            if recon.get("any_rejected"):
                state.emit("⚠️ Some exit legs rejected — check Fyers app manually",
                           "ERROR")
                # Store rejection details for the trade record
                exit_rejections = [r.get("message","") for r in recon.get("rejected",[])]
                reason += f" | EXIT_WARN: {', '.join(exit_rejections)}"

            # Get actual exit price from fill data
            if recon.get("orders"):
                fill_prices = [o.get("avg_price",0) for o in recon["orders"]
                               if o.get("status_code") == 2 and o.get("avg_price")]
                if fill_prices:
                    exit_combined = sum(fill_prices) / len(fill_prices)
                    state.emit(f"Actual exit price: ₹{exit_combined:.1f}", "OK")

            # Verify positions are actually closed
            if close_legs:
                symbols_to_check = [cl["symbol"] for cl in close_legs]
                pos_check = await conn.get_positions_reconcile(symbols_to_check)
                if not pos_check.get("reconciled") and pos_check.get("ok"):
                    still_open = pos_check.get("found", [])
                    if still_open:
                        state.emit(
                            f"⚠️ Positions may still be open: {still_open}",
                            "ERROR")
                    else:
                        state.emit("✅ Exit confirmed — no open positions remain", "OK")
        else:
            state.emit(f"⚠️ Basket close failed: {result.get('message')} — trying individually", "WARN")
            for cl in close_legs:
                r = await conn.place_order(cl["symbol"], cl["side"], qty)
                state.emit(f"CLOSE {cl['side']} {qty}x {cl['symbol']}", "ORDER")
    else:
        # Paper mode or no basket support
        for cl in close_legs:
            r = await conn.place_order(cl["symbol"], cl["side"], qty)
            state.emit(f"CLOSE {cl['side']} {qty}x {cl['symbol']}", "ORDER")

    pnl = (state.position["entry_combined"] - exit_combined) * lots * lot_sz
    state.day_pnl += pnl
    state.emit(f"Closed. P&L: ₹{pnl:.0f} | Day: ₹{state.day_pnl:.0f}",
               "OK" if pnl > 0 else "SL")

    trade_id    = state.position.get("trade_id")
    trade_table = state.position.get("trade_table", "live")
    charges     = calc_brokerage(lots, lot_sz,
                    state.position.get("entry_combined", exit_combined),
                    exit_combined)

    if trade_id:
        db = SessionLocal()
        try:
            if trade_table == "shadow":
                # Paper trade — update ShadowTrade
                t = db.query(ShadowTrade).filter(ShadowTrade.id == trade_id).first()
                if t and t.is_open:
                    t.exit_combined  = exit_combined
                    import pytz as _ptz2; _i2 = _ptz2.timezone("Asia/Kolkata")
                    t.exit_time      = datetime.now(_i2).replace(tzinfo=None)
                    t.exit_spot      = state.spot_history[-1] if state.spot_history else 0
                    t.exit_reason    = reason
                    t.gross_pnl      = round(pnl, 2)
                    t.brokerage      = round(charges["total"], 2)
                    t.net_pnl        = round(pnl - charges["total"], 2)
                    t.sl_tracking    = {
                        "exit_combined":  round(exit_combined, 2),
                        "entry_combined": round(state.position.get("entry_combined",0), 2),
                        "decay_pct":      round((1 - exit_combined /
                                           max(state.position.get("entry_combined",1),0.01)) * 100, 1),
                        "reason":         reason,
                        "charges_detail": charges,
                        # SL state at exit — same fields as shadow monitor for UI parity
                        "vwap":           round(state.atm.vwap_val if state.atm else 0, 2),
                        "ema75":          round(state.atm.ema75 if state.atm else 0, 2),
                        "trailing_low":   round(state.sl_state.trailing_low if hasattr(state,'sl_state') and state.sl_state else 0, 2),
                        "sl_type":        state.sl_state.sl_type if hasattr(state,'sl_state') and state.sl_state else "",
                        "candles":        state.sl_state.candles if hasattr(state,'sl_state') and state.sl_state else 0,
                    }
                    t.last_monitored = datetime.now()
                    t.is_open        = False
                    db.commit()
                    state.emit(
                        f"📋 Paper trade closed | Entry ₹{t.entry_combined:.1f} "
                        f"→ Exit ₹{exit_combined:.1f} | "
                        f"Gross ₹{pnl:+.0f} | Charges ₹{charges['total']:.0f} | "
                        f"Net ₹{(pnl - charges['total']):+.0f}", "OK" if pnl > 0 else "SL")
            else:
                # Live trade — update Trade
                t = db.query(Trade).filter(Trade.id == trade_id).first()
                if t:
                    t.exit_combined = exit_combined
                    t.exit_time     = datetime.now()
                    t.exit_reason   = reason
                    t.gross_pnl     = round(pnl, 2)
                    t.brokerage     = round(charges["total"], 2)
                    t.net_pnl       = round(pnl - charges["total"], 2)
                    t.sl_tracking   = {
                        "exit_combined":  round(exit_combined, 2),
                        "entry_combined": round(state.position.get("entry_combined",0), 2),
                        "decay_pct":      round((1 - exit_combined /
                                           max(state.position.get("entry_combined",1),0.01)) * 100, 1),
                        "reason":         reason,
                        "charges_detail": charges,
                        "vwap":           round(state.atm.vwap_val if state.atm else 0, 2),
                        "ema75":          round(state.atm.ema75 if state.atm else 0, 2),
                        "trailing_low":   round(state.sl_state.trailing_low if hasattr(state,'sl_state') and state.sl_state else 0, 2),
                        "sl_type":        state.sl_state.sl_type if hasattr(state,'sl_state') and state.sl_state else "",
                        "candles":        state.sl_state.candles if hasattr(state,'sl_state') and state.sl_state else 0,
                    }
                    t.is_open       = False
                    db.commit()
        finally:
            db.close()

    state.position    = None
    state.traded_today = True  # One trade per day gate — prevents re-entry
    state.trade_count += 1

# ── Simulation endpoints ─────────────────────────────────────

@app.get("/api/shadow/today")
def sim_today(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Today's simulation status — current session + today's trades."""
    from datetime import date
    today = date.today().strftime("%Y-%m-%d")

    trades = db.query(ShadowTrade).filter(
        ShadowTrade.user_id == user.id,
        ShadowTrade.trade_date == today
    ).all()

    session = {}.get(user.id)
    session_log = session.log[-20:] if session else []
    day_pnl = session.day_pnl if session else sum(t.net_pnl or 0 for t in trades if not t.is_open)
    open_trade = next((t for t in trades if t.is_open), None)

    return {
        "running":   bool(session),
        "day_pnl":   round(day_pnl, 0),
        "trades":    len(trades),
        "open_position": {
            "strategy":   open_trade.strategy_code,
            "entry":      open_trade.entry_combined,
            "entry_time": open_trade.entry_time.strftime("%H:%M") if open_trade.entry_time else None,
        } if open_trade else None,
        "log": session_log,
        "today_trades": [
            {
                "strategy": t.strategy_code,
                "entry":    t.entry_combined,
                "exit":     t.exit_combined,
                "pnl":      round(t.net_pnl or 0, 0),
                "reason":   t.exit_reason,
                "is_open":  t.is_open,
            }
            for t in trades
        ]
    }


# ── Frontend ──────────────────────────────────────────────────

frontend_path = "/app/frontend"
if os.path.exists(frontend_path) and os.listdir(frontend_path):
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")
else:
    @app.get("/")
    def root():
        return JSONResponse({"message": "ALGO-DESK v5"})
