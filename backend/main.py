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

from models import Base, User, BrokerConnection, BrokerDefinition, Automation, Trade, ResetToken, InviteLink
from fyers import FyersConnection, encrypt, decrypt
from engine import EngineState, StrikeState, check_all_strategies, check_sl, nearest_strike

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
    db = SessionLocal()
    try:
        # Run migrations for any missing columns
        with engine_db.connect() as conn:
            migrations = [
                "ALTER TABLE broker_connections ADD COLUMN IF NOT EXISTS refresh_token_enc TEXT",
                "ALTER TABLE broker_connections ADD COLUMN IF NOT EXISTS access_token_enc TEXT",
                "ALTER TABLE broker_connections ADD COLUMN IF NOT EXISTS last_token_refresh TIMESTAMP",
                "ALTER TABLE broker_connections ADD COLUMN IF NOT EXISTS token_expires_at TIMESTAMP",
            ]
            for m in migrations:
                try:
                    conn.execute(text(m))
                    conn.commit()
                except Exception:
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
        if not db.query(BrokerDefinition).filter(BrokerDefinition.broker_id == "fyers").first():
            db.add(BrokerDefinition(
                broker_id="fyers", name="Fyers", flag="🇮🇳",
                market="INDIA", test_method="oauth",
                refresh_desc="Connect once — token refreshes automatically on every use. Never expires.",
                api_base_url="https://api-t1.fyers.in/api/v3",
                sort_order=1,
                fields_config=[
                    {"key": "client_id",    "label": "Client ID",
                     "hint": "myapi.fyers.in → your app → Client ID (e.g. FYXXXXX-100)",
                     "secret": False},
                    {"key": "secret_key",   "label": "Secret Key",
                     "hint": "myapi.fyers.in → your app → Secret Key",
                     "secret": True},
                    {"key": "pin",          "label": "4-digit PIN",
                     "hint": "Your Fyers trading PIN — used for automatic token refresh",
                     "secret": True},
                    {"key": "redirect_uri", "label": "Redirect URI",
                     "hint": "Must exactly match your Fyers app setting",
                     "default": "https://trade.fyers.in/api-login/redirect-uri/index.html",
                     "secret": False},
                ]
            ))
            log.info("Fyers broker definition seeded")

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
        return {"running": False, "mode": "IDLE",
                "position": None, "day_pnl": 0}
    atm = state.atm
    return {
        "running":    True,
        "mode":       "IN_TRADE" if state.position else "MONITORING",
        "spot":       state.spot_history[-1] if state.spot_history else 0,
        "atm":        state.atm_strike,
        "combined":   atm.current if atm else 0,
        "vwap":       atm.vwap_val if atm else 0,
        "ema75":      atm.ema75 if atm else 0,
        "position":   state.position,
        "day_pnl":    state.day_pnl,
        "log":        state.log[-10:],
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

import httpx

@app.post("/api/telegram/test")
async def test_telegram(user: User = Depends(get_current_user)):
    if not user.telegram_token or not user.telegram_chat:
        raise HTTPException(400, "Set Telegram bot token and chat ID in profile first")
    await _send_telegram(user.telegram_token, user.telegram_chat,
        f"✅ Test successful\nHello {user.name}! Alerts are working.")
    return {"ok": True}

# ── WebSocket ─────────────────────────────────────────────────

@app.websocket("/ws/{user_id}")
async def ws_endpoint(websocket: WebSocket, user_id: str):
    await websocket.accept()
    if user_id not in ws_clients:
        ws_clients[user_id] = []
    ws_clients[user_id].append(websocket)
    try:
        while True:
            state = active_engines.get(user_id)
            if state:
                atm = state.atm
                await websocket.send_json({
                    "type": "tick",
                    "spot":     state.spot_history[-1] if state.spot_history else 0,
                    "atm":      state.atm_strike,
                    "combined": atm.current if atm else 0,
                    "vwap":     atm.vwap_val if atm else 0,
                    "ema75":    atm.ema75 if atm else 0,
                    "orb_low":  atm.orb_low if atm else 0,
                    "position": state.position,
                    "day_pnl":  state.day_pnl,
                    "mode":     "IN_TRADE" if state.position else "MONITORING",
                    "log":      state.log[-20:],
                    "strikes": [
                        {"strike": s.strike, "combined": s.current,
                         "orb_low": s.orb_low, "fired": s.fired,
                         "is_atm": s.is_atm}
                        for s in state.strikes
                    ]
                })
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        if user_id in ws_clients:
            ws_clients[user_id] = [w for w in ws_clients[user_id]
                                    if w != websocket]

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
                    state.emit(f"SIGNAL [{signal['code']}]: {signal['reason']}", "SIGNAL")
                    await _open_position(state, conn, signal,
                                          lot_sz, lots, user_id, auto.id)
                    db3 = SessionLocal()
                    try:
                        u = db3.query(User).filter(User.id == user_id).first()
                        if u and u.telegram_token:
                            await _send_telegram(u.telegram_token, u.telegram_chat,
                                f"🟢 {signal['code']}: {signal['name']}\n"
                                f"Strike: {signal['strike']} | "
                                f"Combined: {signal['combined']:.1f}\n"
                                f"Mode: {auto.mode.upper()}")
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
    qty = lot_sz * lots
    orders = []
    for leg, sym_attr in [("sell_ce", "ce_symbol"), ("sell_pe", "pe_symbol")]:
        sym = signal.get(leg)
        if sym:
            r = await conn.place_order(sym, "SELL", qty)
            orders.append({"leg": leg, "symbol": sym,
                           "order_id": r.get("order_id"), "ok": r["ok"]})
            state.emit(f"SELL {qty}x {sym} → {r.get('message','')}", "ORDER")
    for leg in ["buy_ce", "buy_pe"]:
        sym = signal.get(leg)
        if sym:
            r = await conn.place_order(sym, "BUY", qty)
            orders.append({"leg": leg, "symbol": sym,
                           "order_id": r.get("order_id"), "ok": r["ok"]})
            state.emit(f"BUY  {qty}x {sym} → {r.get('message','')}", "ORDER")

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

    for order in state.position.get("orders", []):
        sym  = order.get("symbol")
        side = "BUY" if "sell" in order.get("leg", "") else "SELL"
        if sym:
            r = await conn.place_order(sym, side, qty)
            state.emit(f"CLOSE {side} {qty}x {sym}", "ORDER")

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

# ── Frontend ──────────────────────────────────────────────────

frontend_path = "/app/frontend"
if os.path.exists(frontend_path) and os.listdir(frontend_path):
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")
else:
    @app.get("/")
    def root():
        return JSONResponse({"message": "ALGO-DESK v5"})
