"""
ALGO-DESK v5 — Complete Backend
================================
All endpoints are real. State shared across pages.
Token refreshed on every call — matches N8N approach.
"""

import os, secrets, hashlib, logging, asyncio
from datetime import datetime, timedelta
from typing import Optional

import bcrypt
from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from jose import JWTError, jwt
from pydantic import BaseModel
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session

from models import Base, User, BrokerConnection, BrokerDefinition, Automation, Trade, ShadowTrade, ResetToken, InviteLink, run_migrations
from fyers import FyersConnection, encrypt, decrypt
from engine import EngineState, StrikeState, check_all_strategies, check_sl, nearest_strike

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

# Margin multipliers per strategy structure (Iron Fly vs Condor)
# Based on SPAN margin calculation approximations
# Naked short: ~12-15% of notional. Hedged: ~4-6% of notional
HEDGE_MARGIN_PCT = {
    1: 0.04,   # ±1 tight Iron Fly — tightest hedge, lowest margin
    2: 0.05,   # ±2 standard Iron Fly
    3: 0.045,  # ±3 Iron Condor — margin benefit from wider hedge
    4: 0.04,   # ±4 Wide Condor — widest hedge
    0: 0.06,   # default / no hedge specified
}

def estimate_margin(symbol: str, lots: int, lot_size: int,
                    hedge_width: int, spot_price: float) -> dict:
    """
    Estimate margin required for an Iron Fly/Condor position.
    Formula: spot × lot_size × lots × margin_pct × 4 legs
    4 legs because sell legs use margin, buy legs reduce it.
    Returns margin in rupees with breakdown.
    """
    reg = SYMBOL_REGISTRY.get(symbol, {})
    actual_lot = lot_size or reg.get("lot_size", 75)
    margin_pct = HEDGE_MARGIN_PCT.get(hedge_width, 0.05)

    # Sell legs drive margin (buy legs provide relief)
    # For Iron Fly: 2 sell legs at ATM
    # SPAN gives credit for hedge but not full offset
    sell_notional  = spot_price * actual_lot * lots
    # SPAN margin for 2 sell legs, with hedge benefit
    required = sell_notional * margin_pct * 2  # 2 sell legs

    # Add premium component (collected upfront, reduces net outflow)
    # Typical ATM premium ≈ 0.5-1% of spot
    est_premium = spot_price * 0.007 * actual_lot * lots  # ~0.7% per leg × 2
    net_outflow = max(required - est_premium, required * 0.6)

    return {
        "symbol":        symbol,
        "label":         reg.get("label", symbol),
        "spot":          spot_price,
        "lot_size":      actual_lot,
        "lots":          lots,
        "hedge_width":   hedge_width,
        "gross_margin":  round(required, 0),
        "est_premium":   round(est_premium, 0),
        "net_required":  round(net_outflow, 0),
        "per_lot":       round(net_outflow / lots if lots else 0, 0),
        "note":          "Estimated SPAN margin. Use broker calculator for exact figure."
    }

# Per-user market data cache
# Each user with a connected broker gets their own live feed entry.
# user_id -> {"spot":float, "atm":int, "chain":dict, "status":str, "message":str}
user_market_cache: dict = {}

def _user_cache(user_id: str) -> dict:
    """Get a user's market cache, or a default disconnected state."""
    return user_market_cache.get(user_id, {
        "spot": 0.0, "atm": 0, "chain": {},
        "updated": None, "status": "waiting",
        "message": "Connect your broker in My Brokers to see live data.",
    })

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
         "exp": datetime.utcnow() + timedelta(hours=12)},
        SECRET, algorithm=ALGO)

def get_current_user(creds: HTTPAuthorizationCredentials = Depends(bearer),
                     db: Session = Depends(get_db)) -> User:
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

def require_admin(user: User = Depends(get_current_user)) -> User:
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

@app.on_event("startup")
async def startup():
    init_db()
    asyncio.create_task(_market_data_service())
    asyncio.create_task(_auto_resume_engines())
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
                # Update all user caches to closed
                for uid in list(user_market_cache.keys()):
                    user_market_cache[uid]["status"]  = "closed"
                    user_market_cache[uid]["message"] = "Market closed · Opens 9:15 AM IST Mon-Fri"
                await asyncio.sleep(60)
                continue

            db = SessionLocal()
            try:
                # Get all users with connected Fyers broker
                bcs = db.query(BrokerConnection).filter(
                    BrokerConnection.broker_id == "fyers",
                    BrokerConnection.is_connected == True
                ).all()

                for bc in bcs:
                    user = db.query(User).filter(
                        User.id == bc.user_id,
                        User.is_active == True).first()
                    if not user or not bc.refresh_token_enc:
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
                        access_token_enc=bc.access_token_enc,
                        refresh_token_enc=bc.refresh_token_enc,
                    )

                    # Fetch live data — token auto-refreshes inside
                    result = await conn.get_spot_and_chain(
                        "NSE:NIFTY50-INDEX", strike_count=7)

                    if result.get("ok"):
                        # Save refreshed tokens back to this user's DB row
                        rt = result.get("refresh_tokens", {})
                        if rt.get("ok"):
                            if rt.get("access_token_enc"):
                                bc.access_token_enc = rt["access_token_enc"]
                            if rt.get("refresh_token_enc"):
                                bc.refresh_token_enc = rt["refresh_token_enc"]
                            bc.last_token_refresh = datetime.utcnow()

                        # Update this user's personal cache
                        user_market_cache[user.id] = {
                            "spot":    result["spot"],
                            "atm":     result["atm"],
                            "chain":   result["chain"],
                            "updated": datetime.now(ist).strftime("%H:%M:%S"),
                            "status":  "live",
                            "message": f"Live · {datetime.now(ist).strftime('%H:%M:%S')} IST",
                        }

                        # Feed this user's running engine if active
                        eng = active_engines.get(user.id)
                        if eng and eng.is_running:
                            eng.spot_history.append(result["spot"])
                            if eng.strikes:
                                for sk in eng.strikes:
                                    cd = result["chain"].get(sk.strike)
                                    if cd:
                                        sk.update(cd["combined"])
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
                        user_market_cache[user.id] = {
                            "spot": user_market_cache.get(user.id,{}).get("spot",0),
                            "status": "error",
                            "message": msg,
                        }
                        # If token expired, mark disconnected so user knows to reconnect
                        if "token" in msg.lower() or "expired" in msg.lower():
                            bc.is_connected = False
                            user_market_cache[user.id]["message"] = (
                                "Token expired. Please reconnect in My Brokers.")
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
        running = db.query(Automation).filter(Automation.status=="RUNNING").all()
        for auto in running:
            user = db.query(User).filter(
                User.id==auto.user_id, User.is_active==True).first()
            if not user:
                continue
            conn = _get_fyers(user, db)
            if not conn:
                auto.status = "IDLE"; db.commit(); continue
            config = {**auto.config, "strategies":auto.strategies, "mode":auto.mode}
            state = EngineState(config)
            active_engines[user.id] = state
            asyncio.create_task(_run_engine(user.id, auto, state, conn, db))
            log.info(f"Auto-resumed: {user.email} / {auto.name}")
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
            "role": user.role, "plan": user.plan}

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
def reset_request(req: ResetReq, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == req.email.lower()).first()
    if user:
        token = secrets.token_urlsafe(32)
        db.add(ResetToken(user_id=user.id, token=token,
                          expires_at=datetime.utcnow() + timedelta(hours=24)))
        db.commit()
        domain = os.environ.get("APP_DOMAIN", "localhost")
        reset_url = f"https://{domain}/?reset_token={token}"
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
    current_password: str; new_password: str

@app.post("/api/auth/change-password")
def change_password(req: ChangePwReq, user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    if not bcrypt.checkpw(req.current_password.encode(), user.password_hash.encode()):
        raise HTTPException(400, "Current password incorrect")
    if len(req.new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    user.password_hash = bcrypt.hashpw(req.new_password.encode(), bcrypt.gensalt()).decode()
    db.commit()
    return {"ok": True}

# ── Profile ───────────────────────────────────────────────────

@app.get("/api/me")
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    bc = db.query(BrokerConnection).filter(
        BrokerConnection.user_id == user.id,
        BrokerConnection.is_connected == True).first()
    return {
        "id": user.id, "email": user.email, "name": user.name,
        "role": user.role, "plan": user.plan,
        "timezone": user.timezone or "Asia/Kolkata",
        "telegram_configured": bool(user.telegram_chat),
        "broker_connected": bool(bc),
        "broker_name": bc.broker_name if bc else None,
        "broker_mode": bc.mode if bc else None,
    }

class UpdateProfileReq(BaseModel):
    name: Optional[str] = None
    timezone: Optional[str] = None
    telegram_token: Optional[str] = None
    telegram_chat: Optional[str] = None

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
    return {"brokers": [
        {"id": b.id, "broker_id": b.broker_id,
         "broker_name": b.broker_name, "market": b.market,
         "mode": b.mode, "is_connected": b.is_connected,
         "last_token_refresh": b.last_token_refresh.isoformat()
             if b.last_token_refresh else None,
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
            "https://trade.fyers.in/api-login/redirect-uri/index.html"))
    if not conn.client_id:
        raise HTTPException(400, "Client ID not saved")
    return {"ok": True, "login_url": conn.login_url()}

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
            "https://trade.fyers.in/api-login/redirect-uri/index.html"))

    result = await conn.exchange_auth_code(req.auth_code.strip())
    if result["ok"]:
        bc.access_token_enc   = result["access_token_enc"]
        bc.refresh_token_enc  = result["refresh_token_enc"]
        bc.is_connected       = True
        bc.last_token_refresh = datetime.utcnow()
        db.commit()
        return {"ok": True, "message": "Fyers connected! Token auto-refreshes on every use.",
                "connected": True}
    return {"ok": False, "message": result["message"], "connected": False}

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


@app.get("/api/market/status")
def market_status(user: User = Depends(get_current_user)):
    """Quick status check for this user's market data."""
    cache = _user_cache(user.id)
    return {
        "spot":    cache.get("spot", 0),
        "atm":     cache.get("atm", 0),
        "chain":   cache.get("chain", {}),
        "status":  cache.get("status", "waiting"),
        "message": cache.get("message", "Connect your broker to see live data."),
        "updated": cache.get("updated"),
    }


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
    # Just refresh token to test
    result = await conn.refresh_token()
    if result["ok"]:
        _save_tokens(user.id, conn, result, db)
        profile = await conn.get_profile()
        return {"ok": True, "connected": True, "profile": profile.get("data", {}),
                "message": "Fyers connected ✓"}
    return {"ok": False, "connected": False, "message": result["message"]}

@app.get("/api/market/funds")
async def market_funds(user: User = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    conn = _get_fyers(user, db)
    if not conn:
        return {"ok": False, "funds": {}}
    await conn.refresh_token()
    funds = await conn.get_funds()
    return {"ok": True, "funds": funds}

# ── Strategies ────────────────────────────────────────────────

@app.get("/api/strategies")
def get_strategies(user: User = Depends(get_current_user)):
    return {"strategies": [
        {"code": "S7", "name": "All-Strike Iron Butterfly",
         "tier": "PRO", "auto": True,
         "description": "Fires when ALL 7 strikes break ORB low simultaneously. Highest conviction."},
        {"code": "S1", "name": "ORB Breakdown Sell",
         "tier": "STARTER",
         "description": "Primary strategy. Any strike breaks ORB low. ATM priority."},
        {"code": "S2", "name": "VWAP Squeeze + EMA Cross",
         "tier": "STARTER",
         "description": "S1 fallback. Premium tight below VWAP, EMA75 bearish, RSI<45."},
        {"code": "S8", "name": "Opening Gap Fade",
         "tier": "STARTER",
         "description": "Gap >0.4% from prev close. Premium compressing."},
        {"code": "S3", "name": "Breakout Reversal",
         "tier": "STARTER",
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
         "is_running": a.id in active_engines}
        for a in autos]}

class SaveAutoReq(BaseModel):
    name: str; symbol: str; broker_id: str
    strategies: list; mode: str = "paper"; config: dict = {}

@app.post("/api/automations")
def save_automation(req: SaveAutoReq, user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    a = Automation(user_id=user.id, name=req.name, symbol=req.symbol,
                   broker_id=req.broker_id, strategies=req.strategies,
                   mode=req.mode, config=req.config, status="IDLE")
    db.add(a); db.commit(); db.refresh(a)
    return {"ok": True, "automation": {"id": a.id, "name": a.name}}

@app.delete("/api/automations/{auto_id}")
def delete_automation(auto_id: str, user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    db.query(Automation).filter(
        Automation.id == auto_id,
        Automation.user_id == user.id).delete()
    db.commit()
    return {"ok": True}

# ── Engine ────────────────────────────────────────────────────

active_engines: dict = {}      # user_id -> EngineState
ws_clients: dict = {}          # user_id -> [WebSocket]

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

    config = {**auto.config, "strategies": auto.strategies, "mode": auto.mode}
    state  = EngineState(config)
    active_engines[user.id] = state

    asyncio.create_task(_run_engine(user.id, auto, state, conn, db))

    auto.status = "RUNNING"
    db.commit()
    return {"ok": True, "message": f"Engine started: {auto.name}"}

@app.post("/api/engine/stop")
async def stop_engine(user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    if user.id in active_engines:
        active_engines[user.id].is_running = False
        del active_engines[user.id]
    db.query(Automation).filter(
        Automation.user_id == user.id,
        Automation.status == "RUNNING"
    ).update({"status": "IDLE"})
    db.commit()
    return {"ok": True}

@app.post("/api/engine/force-exit")
async def force_exit(user: User = Depends(get_current_user)):
    if user.id in active_engines:
        state = active_engines[user.id]
        if state.position:
            state.position["force_exit"] = True
    return {"ok": True}

@app.get("/api/engine/status")
def engine_status(user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    state = active_engines.get(user.id)
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
        "running":      True,
        "mode":         "IN_TRADE" if state.position else "MONITORING",
        "engine_mode":  state.config.get("mode", "paper"),
        "spot":         state.spot_history[-1] if state.spot_history else 0,
        "atm":          state.atm_strike,
        "combined":     atm.current if atm else 0,
        "vwap":         atm.vwap_val if atm else 0,
        "ema75":        atm.ema75 if atm else 0,
        "position":     state.position,
        "day_pnl":      state.day_pnl,
        "log":          state.log[-10:],
        "today_trades": shadow_history,
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
def create_user(req: CreateUserReq, admin: User = Depends(require_admin),
                db: Session = Depends(get_db)):
    email = req.email.lower().strip()
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(400, "Email already exists")
    db.add(User(email=email, name=req.name,
                password_hash=bcrypt.hashpw(req.password.encode(), bcrypt.gensalt()).decode(),
                role=req.role, plan=req.plan,
                is_active=True, is_verified=True))
    db.commit()
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

@app.post("/api/admin/users/{user_id}/reset-password")
def admin_reset_pw(user_id: str, admin: User = Depends(require_admin),
                   db: Session = Depends(get_db)):
    u = db.query(User).filter(User.id == user_id).first()
    if not u: raise HTTPException(404, "User not found")
    token = secrets.token_urlsafe(32)
    db.add(ResetToken(user_id=u.id, token=token,
                      expires_at=datetime.utcnow() + timedelta(hours=24)))
    db.commit()
    domain = os.environ.get("APP_DOMAIN", "localhost")
    return {"ok": True, "reset_url": f"https://{domain}/?reset_token={token}"}

@app.post("/api/admin/invite")
def create_invite(req: dict, admin: User = Depends(require_admin),
                  db: Session = Depends(get_db)):
    token = secrets.token_urlsafe(24)
    db.add(InviteLink(token=token, created_by=admin.id,
                      role=req.get("role", "USER"),
                      plan=req.get("plan", "FREE")))
    db.commit()
    domain = os.environ.get("APP_DOMAIN", "localhost")
    return {"ok": True,
            "invite_url": f"https://{domain}/?invite={token}",
            "token": token}

@app.get("/api/admin/stats")
def admin_stats(admin: User = Depends(require_admin),
                db: Session = Depends(get_db)):
    users = db.query(User).all()
    return {
        "total_users":     len(users),
        "active_users":    sum(1 for u in users if u.is_active),
        "total_brokers":   db.query(BrokerConnection).filter(
            BrokerConnection.is_connected == True).count(),
        "total_automations": db.query(Automation).count(),
        "plans": {p: sum(1 for u in users if u.plan == p)
                  for p in ["FREE", "STARTER", "PRO", "ENTERPRISE"]},
    }

# ── Telegram ──────────────────────────────────────────────────

async def _send_telegram(bot_token: str, chat_id: str, msg: str):
    if not bot_token or not chat_id: return
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": f"⬡ ALGO-DESK\n\n{msg}"})
    except Exception as e:
        log.error(f"Telegram: {e}")

async def _send_telegram_all(user: User, msg: str):
    """Send to all active Telegram accounts configured by this user."""
    sent = 0
    # New multi-account list
    for acct in (user.telegram_accounts or []):
        if acct.get("active") and acct.get("token") and acct.get("chat"):
            await _send_telegram(acct["token"], acct["chat"], msg)
            sent += 1
    # Legacy single account fallback
    if sent == 0 and user.telegram_token and user.telegram_chat:
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
    accounts.append({"id": _uuid()[:8], "name": req.name,
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
        return {"total_trades":0,"total_pnl":0,"win_rate":0,
                "avg_pnl":0,"best_day":None,"worst_day":None,
                "by_strategy":{},"by_day":[],"days":days}

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

    return {
        "total_trades":  len(trades),
        "total_pnl":     round(total_pnl, 0),
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      round(len(wins)/len(trades)*100,1),
        "avg_pnl":       round(total_pnl/len(trades),0),
        "best_day":      best_day,
        "worst_day":     worst_day,
        "by_strategy":   by_strat,
        "by_day":        by_day,
        "equity_curve":  equity_curve,
        "exit_reasons":  exit_reasons,
        "days":          days,
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
    lot_sz = int(config.get("lot_size", 25))
    lots   = int(config.get("lots", 1))

    # Create shadow trade record
    db = SessionLocal()
    try:
        st = ShadowTrade(
            user_id=user_id, automation_id=auto.id,
            trade_date=datetime.now(ist).strftime("%Y-%m-%d"),
            symbol=auto.symbol, strategy_code=signal["code"],
            atm_strike=signal.get("strike", 0),
            entry_combined=entry_combined, entry_spot=entry_spot,
            entry_time=datetime.utcnow(),
            lots=lots, lot_size=lot_sz,
            is_open=True, signal_data=signal,
        )
        db.add(st); db.commit(); db.refresh(st)
        trade_id = st.id

        # Send paper mode entry alert
        if auto.telegram_alerts:
            user = db.query(User).filter(User.id == user_id).first()
            if user:
                await _send_telegram_all(user,
                    f"📋 [PAPER MODE] {signal['code']}: {signal['name']}\n"
                    f"Symbol: {auto.symbol}\n"
                    f"Strike: {signal.get('strike')}\n"
                    f"Combined: ₹{entry_combined:.1f}\n"
                    f"Time: {datetime.now(ist).strftime('%H:%M')} IST\n"
                    f"⚠️ This is a simulation — no real orders placed.")
    finally:
        db.close()

    # Monitor position using market data cache
    from engine import SLState, nearest_strike
    sl = SLState()
    sl.activate(entry_combined, config)
    sl_tracking = {}

    while True:
        await asyncio.sleep(60)
        try:
            now = datetime.now(ist)
            t   = now.time()

            # Auto-exit at configured time
            exit_time = config.get("auto_exit_time", "14:00")
            eh, em = map(int, exit_time.split(":"))
            if t >= dtime(eh, em):
                await _close_shadow_trade(trade_id, user_id, "AUTO_EXIT",
                    entry_combined, auto, lots, lot_sz, sl_tracking)
                return

            # Market closed
            if t > dtime(15, 30):
                await _close_shadow_trade(trade_id, user_id, "MARKET_CLOSE",
                    entry_combined, auto, lots, lot_sz, sl_tracking)
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
            vwap    = 0.0  # simplified — full VWAP needs history
            ema75   = 0.0

            sl_tracking = {
                "current": current,
                "trailing_low": sl.trailing_low,
                "trailing_sl": sl.trailing_sl,
                "candles": sl.candles,
            }

            should_exit, reason = sl.update(current, vwap, ema75, 0, config)
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
        t.exit_combined = exit_combined
        t.exit_time     = datetime.utcnow()
        t.exit_reason   = reason
        t.gross_pnl     = round(pnl, 2)
        t.net_pnl       = round(pnl - 40, 2)
        t.is_open       = False
        t.sl_tracking   = sl_tracking
        db.commit()

        if auto.telegram_alerts:
            user = db.query(User).filter(User.id == user_id).first()
            if user:
                emoji = "✅" if pnl > 0 else "🔴"
                await _send_telegram_all(user,
                    f"{emoji} [PAPER MODE] Trade Closed\n"
                    f"Strategy: {t.strategy_code}\n"
                    f"Entry: ₹{t.entry_combined:.1f} → Exit: ₹{exit_combined:.1f}\n"
                    f"P&L: ₹{pnl:+.0f}\n"
                    f"Reason: {reason}\n"
                    f"⚠️ Simulation only — no real money")
    finally:
        db.close()


@app.get("/api/dashboard/summary")
async def dashboard_summary(user: User = Depends(get_current_user),
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
        eng = active_engines.get(user.id + ":" + a.id) or active_engines.get(user.id)
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
    month_live_pnl = sum(t.net_pnl or 0 for t in month_trades)
    month_paper_pnl = sum(t.net_pnl or 0 for t in month_shadow)

    cache = _user_cache(user.id)

    return {
        "spot":             cache.get("spot", 0),
        "atm":              cache.get("atm", 0),
        "market_status":    cache.get("status", "waiting"),
        "market_updated":   cache.get("updated"),
        "market_message":   cache.get("message", ""),
        "today_live_pnl":   round(today_live_pnl, 0),
        "today_paper_pnl":  round(today_paper_pnl, 0),
        "today_live_trades": len([t for t in today_trades if not t.is_open]),
        "today_paper_trades": len([t for t in today_shadow if not t.is_open]),
        "open_positions":   len([t for t in today_trades if t.is_open]),
        "month_live_pnl":   round(month_live_pnl, 0),
        "month_paper_pnl":  round(month_paper_pnl, 0),
        "automations":      auto_status,
        "total_automations": len(autos),
        "running_automations": len([a for a in autos if a.status=="RUNNING"]),
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

    # Get live spot price from cache
    cache = _user_cache(user.id)
    spot = cache.get("spot", 0)

    # Fallback spot prices if cache empty
    if not spot:
        fallback_spots = {
            "NSE:NIFTY50-INDEX":    24000,
            "NSE:NIFTYBANK-INDEX":  52000,
            "NSE:FINNIFTY-INDEX":   24000,
            "BSE:SENSEX-INDEX":     80000,
            "NSE:MIDCPNIFTY-INDEX": 12000,
        }
        spot = fallback_spots.get(symbol, 24000)

    # Get registry lot size if not overridden
    reg = SYMBOL_REGISTRY.get(symbol, {})
    actual_lot_size = lot_size or reg.get("lot_size", 75)

    # Strategy-specific hedge widths
    auto_hedges = {"S9": 1, "S8": 3, "S6": 4}
    # Use the widest hedge among selected strategies (conservative estimate)
    hedges = [auto_hedges.get(s, hedge_width) for s in strategy_list]
    max_hedge = max(hedges) if hedges else hedge_width

    # Calculate margin estimate
    margin = estimate_margin(symbol, lots, actual_lot_size, max_hedge, spot)

    # Fetch actual account funds
    funds = {}
    funds_error = None
    available = 0

    if conn and conn.mode != "paper":
        try:
            funds = await conn.get_funds()
            # Fyers returns keys like "Available Balance", "Clear Balance" etc.
            # Try common keys
            for key in ["Available Balance", "Available cash", "Cash Available",
                        "Clear Balance", "Net Balance", "Payin"]:
                if key in funds and funds[key] > 0:
                    available = funds[key]
                    break
            if not available:
                available = max(funds.values()) if funds else 0
        except Exception as e:
            funds_error = str(e)
    elif conn and conn.mode == "paper":
        available = 999999  # Paper mode — unlimited
        funds = {"Paper Mode": 999999}

    can_trade = available >= margin["net_required"]
    shortfall = max(0, margin["net_required"] - available)
    buffer = available - margin["net_required"]

    # Per-strategy breakdown
    strat_breakdown = []
    for s in strategy_list:
        hw = auto_hedges.get(s, hedge_width)
        sm = estimate_margin(symbol, lots, actual_lot_size, hw, spot)
        strat_breakdown.append({
            "strategy":     s,
            "hedge_width":  hw,
            "net_required": sm["net_required"],
            "can_trade":    available >= sm["net_required"],
        })

    return {
        "ok":            True,
        "symbol":        symbol,
        "label":         reg.get("label", symbol),
        "spot":          round(spot, 1),
        "lot_size":      actual_lot_size,
        "lots":          lots,
        "strategies":    strategy_list,
        "margin":        margin,
        "available":     round(available, 0),
        "can_trade":     can_trade,
        "shortfall":     round(shortfall, 0),
        "buffer":        round(buffer, 0),
        "funds":         funds,
        "funds_error":   funds_error,
        "mode":          conn.mode if conn else "no_broker",
        "strat_breakdown": strat_breakdown,
        "recommendation": (
            "✅ Sufficient funds — can proceed"
            if can_trade else
            f"❌ Insufficient funds — need ₹{shortfall:,.0f} more"
        ),
    }


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
    lot_sz  = int(state.config.get("lot_size", 25))
    lots    = int(state.config.get("lots", 1))

    state.emit(f"Engine started — {auto.name} | Mode: {auto.mode.upper()}", "START")

    ist = pytz.timezone("Asia/Kolkata")

    while state.is_running:
        try:
            now = datetime.now(ist)
            t   = now.time()

            # Only run during market hours (9:15–15:30 Mon–Fri)
            if not (dtime(9, 15) <= t <= dtime(15, 30) and now.weekday() < 5):
                state.emit("Outside market hours. Waiting...", "INFO")
                await asyncio.sleep(60)
                continue

            # Get fresh data (token auto-refreshes inside)
            data = await conn.get_spot_and_chain(symbol)

            if not data.get("ok"):
                state.emit(f"Data error: {data.get('message')}", "WARN")
                await asyncio.sleep(60)
                continue

            # Save refreshed tokens
            db = SessionLocal()
            try:
                _save_tokens(user_id, conn, data.get("refresh_tokens", {}), db)
            finally:
                db.close()

            spot  = data["spot"]
            chain = data["chain"]
            state.spot_history.append(spot)

            # Lock ATM at 9:15
            if t >= dtime(9, 15) and not state.atm_strike:
                state.spot_locked = spot
                state.atm_strike  = nearest_strike(spot)
                sides = state.config.get("strike_sides", 3)
                gap   = state.config.get("strike_round", 50)
                for i in range(-sides, sides + 1):
                    sk = StrikeState(
                        strike=state.atm_strike + i * gap,
                        offset=i, is_atm=(i == 0))
                    state.strikes.append(sk)
                state.emit(f"ATM locked: {state.atm_strike} | Spot: {spot:.1f}", "OK")

            # Update strike data from chain
            if state.strikes:
                for sk in state.strikes:
                    cd = chain.get(sk.strike)
                    if cd:
                        sk.combined_history.append(cd["combined"])
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

            # Auto-exit time
            exit_time = state.config.get("auto_exit_time", "14:00")
            eh, em = map(int, exit_time.split(":"))
            if t >= dtime(eh, em) and state.position:
                await _close_position(state, conn, "AUTO_EXIT",
                                       lot_sz, lots, user_id)

            # Check SL
            elif state.position:
                atm  = state.atm
                reason = check_sl(state)
                if reason or state.position.get("force_exit"):
                    reason = reason or "FORCE_EXIT"
                    await _close_position(state, conn, reason,
                                           lot_sz, lots, user_id)
                    # Telegram
                    db2 = SessionLocal()
                    try:
                        u = db2.query(User).filter(User.id == user_id).first()
                        if u and u.telegram_token:
                            await _send_telegram(u.telegram_token, u.telegram_chat,
                                f"🔴 Position closed: {reason}\nP&L: ₹{state.day_pnl:.0f}")
                    finally:
                        db2.close()

            # Check signals
            elif state.orb_complete:
                signal = check_all_strategies(state, now)
                if signal:
                    # Auto-set hedge width per strategy if not overridden
                    auto_hedges = {"S9":1,"S8":3,"S6":4}
                    hw = auto_hedges.get(signal["code"], state.config.get("hedge_width",2))
                    signal["hedge_width"] = hw
                    state.emit(f"SIGNAL [{signal['code']}]: {signal['reason']} | hedge=±{hw}", "SIGNAL")
                    await _open_position(state, conn, signal,
                                          lot_sz, lots, user_id, auto.id)
                    db3 = SessionLocal()
                    try:
                        u = db3.query(User).filter(User.id == user_id).first()
                        if u:
                            mode_tag = "🔴 LIVE" if auto.mode=="live" else "📋 PAPER"
                            tg_ids = auto.config.get("telegram_accounts", [])
                            msg = (f"{mode_tag} Signal: {signal['code']} — {signal['name']}\n"
                                   f"Strike: {signal.get('strike','')} | Combined: ₹{signal.get('combined',0):.1f}\n"
                                   f"Hedge: ±{signal.get('hedge_width',2)} | Reason: {signal.get('reason','')}\n"
                                   f"{'⚠️ Simulation only' if auto.mode=='paper' else '✅ Live order placed'}")
                            await _send_telegram_all(u, msg)
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

    # Build all 4 legs for basket order
    legs_to_place = []
    leg_map = []

    for leg, side in [("sell_ce","SELL"),("sell_pe","SELL"),
                      ("buy_ce","BUY"),("buy_pe","BUY")]:
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

    db = SessionLocal()
    try:
        trade = Trade(
            user_id=user_id, automation_id=auto_id,
            trade_date=datetime.now().strftime("%Y-%m-%d"),
            symbol=state.config.get("symbol", "NIFTY"),
            strategy_code=signal["code"], mode=state.config.get("mode", "paper"),
            atm_strike=state.atm_strike,
            sell_ce_strike=signal.get("sell_ce_strike", signal["strike"]),
            sell_pe_strike=signal.get("sell_pe_strike", signal["strike"]),
            entry_combined=combined, net_credit=combined,
            lots=lots, lot_size=lot_sz, entry_time=datetime.now(),
            is_open=True, signal_data=signal, orders=orders)
        db.add(trade); db.commit()
        state.position["trade_id"] = trade.id
    finally:
        db.close()


async def _close_position(state, conn, reason, lot_sz, lots, user_id):
    if not state.position: return
    qty = lot_sz * lots
    atm = state.atm
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

    trade_id = state.position.get("trade_id")
    if trade_id:
        db = SessionLocal()
        try:
            t = db.query(Trade).filter(Trade.id == trade_id).first()
            if t:
                t.exit_combined = exit_combined
                t.exit_time     = datetime.now()
                t.exit_reason   = reason
                t.gross_pnl     = pnl
                t.net_pnl       = pnl - t.brokerage
                t.is_open       = False
                db.commit()
        finally:
            db.close()

    state.position = None

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
