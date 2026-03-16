"""
ALGO-DESK v4 — Complete Backend with Fyers OAuth2
==================================================
Self-service broker connection.
Every user connects their own broker.
Refresh token runs forever — no daily manual action.
"""

import os, secrets, hashlib, base64, json, logging, asyncio
from datetime import datetime, timedelta
from typing import Optional

import bcrypt
from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect, BackgroundTasks
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

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("algodesk")

# ── Database ──────────────────────────────────────────────────

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://algodesk:algodesk@postgres:5432/algodesk"
)
engine_db   = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=5)
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
        # Seed admin
        email = os.environ.get("SUPER_ADMIN_EMAIL","")
        pw    = os.environ.get("SUPER_ADMIN_PASSWORD","")
        name  = os.environ.get("SUPER_ADMIN_NAME","Admin")
        if email and not db.query(User).filter(User.email==email).first():
            db.add(User(
                email=email, name=name,
                password_hash=bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode(),
                role="SUPER_ADMIN", plan="PRO", is_active=True, is_verified=True,
            ))
            log.info(f"Admin created: {email}")

        # Seed Fyers broker definition
        if not db.query(BrokerDefinition).filter(BrokerDefinition.broker_id=="fyers").first():
            db.add(BrokerDefinition(
                broker_id="fyers", name="Fyers", flag="🇮🇳",
                market="INDIA", test_method="oauth",
                refresh_desc="Connect once — auto-refreshes forever. No daily login needed.",
                api_base_url="https://api-t1.fyers.in/api/v3",
                sort_order=1,
                fields_config=[
                    {"key":"client_id",    "label":"Client ID",
                     "hint":"myapi.fyers.in → your app → Client ID (format: FYXXXXX-100)",
                     "secret":False},
                    {"key":"secret_key",   "label":"Secret Key",
                     "hint":"myapi.fyers.in → your app → Secret Key",
                     "secret":True},
                    {"key":"pin",          "label":"4-digit PIN",
                     "hint":"Your Fyers trading PIN — used for daily auto-refresh",
                     "secret":True},
                    {"key":"redirect_uri", "label":"Redirect URI",
                     "hint":"Must match exactly what is in your Fyers app settings at myapi.fyers.in",
                     "default":"https://trade.fyers.in/api-login/redirect-uri/index.html",
                     "secret":False},
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
        {"sub":email,"role":role,"exp":datetime.utcnow()+timedelta(hours=12)},
        SECRET, algorithm=ALGO)

def get_current_user(creds: HTTPAuthorizationCredentials=Depends(bearer),
                     db: Session=Depends(get_db)) -> User:
    if not creds: raise HTTPException(401,"Not authenticated")
    try:
        payload = jwt.decode(creds.credentials, SECRET, algorithms=[ALGO])
        email   = payload["sub"]
    except JWTError:
        raise HTTPException(401,"Invalid or expired token")
    user = db.query(User).filter(User.email==email).first()
    if not user or not user.is_active:
        raise HTTPException(401,"User not found or suspended")
    return user

def require_admin(user: User=Depends(get_current_user)) -> User:
    if user.role not in ("SUPER_ADMIN","ADMIN"):
        raise HTTPException(403,"Admin access required")
    return user

@app.on_event("startup")
async def startup():
    init_db()
    asyncio.create_task(_start_scheduler())
    log.info("ALGO-DESK v4 started ✓")

# ── Health ────────────────────────────────────────────────────

@app.get("/health")
def health(db: Session=Depends(get_db)):
    try: db.execute(text("SELECT 1")); db_ok=True
    except: db_ok=False
    return {"status":"ok","version":"4.0.0","db":"ok" if db_ok else "error",
            "time":datetime.now().isoformat()}

# ── Auth ──────────────────────────────────────────────────────

class LoginReq(BaseModel):
    email: str; password: str

@app.post("/api/auth/login")
def login(req: LoginReq, db: Session=Depends(get_db)):
    user = db.query(User).filter(User.email==req.email.lower().strip()).first()
    if not user: raise HTTPException(401,"Invalid email or password")
    if not user.is_active: raise HTTPException(401,"Account suspended")
    if not bcrypt.checkpw(req.password.encode(), user.password_hash.encode()):
        raise HTTPException(401,"Invalid email or password")
    user.last_login = datetime.utcnow(); db.commit()
    return {"token":make_token(user.email,user.role),"name":user.name,
            "email":user.email,"role":user.role,"plan":user.plan}

class RegisterReq(BaseModel):
    email: str; password: str; name: str
    invite_token: Optional[str] = None

@app.post("/api/auth/register")
def register(req: RegisterReq, db: Session=Depends(get_db)):
    email = req.email.lower().strip()
    reg_open = os.environ.get("REGISTRATION_OPEN","true").lower()=="true"
    invite = None
    if req.invite_token:
        invite = db.query(InviteLink).filter(
            InviteLink.token==req.invite_token, InviteLink.used==False).first()
    if not reg_open and not invite:
        raise HTTPException(403,"Registration closed. Ask admin for invite.")
    if db.query(User).filter(User.email==email).first():
        raise HTTPException(400,"Email already registered")
    if len(req.password)<8:
        raise HTTPException(400,"Password must be at least 8 characters")
    user = User(email=email, name=req.name,
                password_hash=bcrypt.hashpw(req.password.encode(),bcrypt.gensalt()).decode(),
                role=invite.role if invite else "USER",
                plan=invite.plan if invite else "FREE",
                is_active=True, is_verified=False)
    db.add(user)
    if invite: invite.used=True; invite.used_by=email
    db.commit()
    return {"ok":True,"token":make_token(email,user.role),
            "name":req.name,"email":email,"role":user.role,"plan":user.plan}

class ResetReq(BaseModel):
    email: str

@app.post("/api/auth/reset-request")
def reset_request(req: ResetReq, db: Session=Depends(get_db)):
    user = db.query(User).filter(User.email==req.email.lower()).first()
    if user:
        token = secrets.token_urlsafe(32)
        db.add(ResetToken(user_id=user.id, token=token,
                          expires_at=datetime.utcnow()+timedelta(hours=24)))
        db.commit()
        domain = os.environ.get("APP_DOMAIN","localhost")
        reset_url = f"https://{domain}/?reset_token={token}"
        log.info(f"Reset URL for {user.email}: {reset_url}")
        return {"ok":True,"message":"Reset link generated","reset_url":reset_url}
    return {"ok":True,"message":"Reset link sent if account exists"}

class ResetPasswordReq(BaseModel):
    token: str; new_password: str

@app.post("/api/auth/reset-password")
def reset_password(req: ResetPasswordReq, db: Session=Depends(get_db)):
    rt = db.query(ResetToken).filter(
        ResetToken.token==req.token, ResetToken.used==False).first()
    if not rt or datetime.utcnow()>rt.expires_at:
        raise HTTPException(400,"Invalid or expired reset link")
    if len(req.new_password)<8:
        raise HTTPException(400,"Password must be at least 8 characters")
    user = db.query(User).filter(User.id==rt.user_id).first()
    user.password_hash = bcrypt.hashpw(req.new_password.encode(),bcrypt.gensalt()).decode()
    rt.used = True; db.commit()
    return {"ok":True,"message":"Password updated. You can now sign in."}

class ChangePwReq(BaseModel):
    current_password: str; new_password: str

@app.post("/api/auth/change-password")
def change_password(req: ChangePwReq, user: User=Depends(get_current_user),
                    db: Session=Depends(get_db)):
    if not bcrypt.checkpw(req.current_password.encode(), user.password_hash.encode()):
        raise HTTPException(400,"Current password incorrect")
    if len(req.new_password)<8:
        raise HTTPException(400,"Password must be at least 8 characters")
    user.password_hash = bcrypt.hashpw(req.new_password.encode(),bcrypt.gensalt()).decode()
    db.commit()
    return {"ok":True}

# ── Profile ───────────────────────────────────────────────────

@app.get("/api/me")
def me(user: User=Depends(get_current_user)):
    return {"id":user.id,"email":user.email,"name":user.name,
            "role":user.role,"plan":user.plan,"timezone":user.timezone or "Asia/Kolkata",
            "telegram_configured":bool(user.telegram_chat)}

class UpdateProfileReq(BaseModel):
    name: Optional[str]=None; timezone: Optional[str]=None
    telegram_token: Optional[str]=None; telegram_chat: Optional[str]=None

@app.put("/api/me")
def update_profile(req: UpdateProfileReq, user: User=Depends(get_current_user),
                   db: Session=Depends(get_db)):
    if req.name:           user.name=req.name
    if req.timezone:       user.timezone=req.timezone
    if req.telegram_token: user.telegram_token=req.telegram_token
    if req.telegram_chat:  user.telegram_chat=req.telegram_chat
    db.commit()
    return {"ok":True}

# ── Admin ─────────────────────────────────────────────────────

@app.get("/api/admin/users")
def list_users(admin: User=Depends(require_admin), db: Session=Depends(get_db)):
    users = db.query(User).all()
    return {"users":[
        {**{k:v for k,v in u.__dict__.items()
            if not k.startswith("_") and k!="password_hash"},
         "broker_count":db.query(BrokerConnection).filter(
             BrokerConnection.user_id==u.id).count()}
        for u in users
    ],"total":len(users)}

class CreateUserReq(BaseModel):
    email: str; name: str; password: str
    role: str="USER"; plan: str="FREE"

@app.post("/api/admin/users")
def create_user(req: CreateUserReq, admin: User=Depends(require_admin),
                db: Session=Depends(get_db)):
    email = req.email.lower().strip()
    if db.query(User).filter(User.email==email).first():
        raise HTTPException(400,"Email already exists")
    db.add(User(email=email, name=req.name,
                password_hash=bcrypt.hashpw(req.password.encode(),bcrypt.gensalt()).decode(),
                role=req.role, plan=req.plan, is_active=True, is_verified=True))
    db.commit()
    return {"ok":True,"message":f"User {email} created"}

@app.post("/api/admin/users/{user_id}/suspend")
def suspend_user(user_id: str, admin: User=Depends(require_admin), db: Session=Depends(get_db)):
    u=db.query(User).filter(User.id==user_id).first()
    if u: u.is_active=False; db.commit()
    return {"ok":True}

@app.post("/api/admin/users/{user_id}/activate")
def activate_user(user_id: str, admin: User=Depends(require_admin), db: Session=Depends(get_db)):
    u=db.query(User).filter(User.id==user_id).first()
    if u: u.is_active=True; db.commit()
    return {"ok":True}

@app.post("/api/admin/users/{user_id}/reset-password")
def admin_reset_pw(user_id: str, admin: User=Depends(require_admin), db: Session=Depends(get_db)):
    u=db.query(User).filter(User.id==user_id).first()
    if not u: raise HTTPException(404,"User not found")
    token=secrets.token_urlsafe(32)
    db.add(ResetToken(user_id=u.id,token=token,expires_at=datetime.utcnow()+timedelta(hours=24)))
    db.commit()
    domain=os.environ.get("APP_DOMAIN","localhost")
    return {"ok":True,"reset_url":f"https://{domain}/?reset_token={token}"}

@app.post("/api/admin/invite")
def create_invite(req: dict, admin: User=Depends(require_admin), db: Session=Depends(get_db)):
    token=secrets.token_urlsafe(24)
    db.add(InviteLink(token=token,created_by=admin.id,
                      role=req.get("role","USER"),plan=req.get("plan","FREE")))
    db.commit()
    domain=os.environ.get("APP_DOMAIN","localhost")
    return {"ok":True,"invite_url":f"https://{domain}/?invite={token}","token":token}

@app.get("/api/admin/stats")
def admin_stats(admin: User=Depends(require_admin), db: Session=Depends(get_db)):
    users=db.query(User).all()
    return {"total_users":len(users),
            "active_users":sum(1 for u in users if u.is_active),
            "total_brokers":db.query(BrokerConnection).count(),
            "total_automations":db.query(Automation).count(),
            "plans":{p:sum(1 for u in users if u.plan==p)
                     for p in ["FREE","STARTER","PRO","ENTERPRISE"]}}

# ── Broker definitions ────────────────────────────────────────

@app.get("/api/brokers/definitions")
def broker_definitions(user: User=Depends(get_current_user), db: Session=Depends(get_db)):
    defs = db.query(BrokerDefinition).filter(BrokerDefinition.is_active==True)\
             .order_by(BrokerDefinition.sort_order).all()
    return {"brokers":[
        {"id":d.broker_id,"name":d.name,"flag":d.flag,"market":d.market,
         "refresh":d.refresh_desc,"test_method":d.test_method,"fields":d.fields_config}
        for d in defs]}

# ── Broker connections — self-service ─────────────────────────

@app.get("/api/brokers")
def list_my_brokers(user: User=Depends(get_current_user), db: Session=Depends(get_db)):
    brokers = db.query(BrokerConnection).filter(BrokerConnection.user_id==user.id).all()
    return {"brokers":[
        {"id":b.id,"broker_id":b.broker_id,"broker_name":b.broker_name,
         "market":b.market,"mode":b.mode,"is_connected":b.is_connected,
         "last_token_refresh":b.last_token_refresh.isoformat() if b.last_token_refresh else None,
         "fields_count":len(b.encrypted_fields or {})}
        for b in brokers]}

class SaveBrokerReq(BaseModel):
    broker_id: str; fields: dict; mode: str="paper"

@app.post("/api/brokers")
def save_broker(req: SaveBrokerReq, user: User=Depends(get_current_user),
                db: Session=Depends(get_db)):
    bd = db.query(BrokerDefinition).filter(BrokerDefinition.broker_id==req.broker_id).first()
    if not bd: raise HTTPException(400,f"Unknown broker: {req.broker_id}")

    # Encrypt all fields
    encrypted = {k+"_enc": encrypt(user.id, str(v).strip())
                 for k,v in req.fields.items() if v and str(v).strip()}

    existing = db.query(BrokerConnection).filter(
        BrokerConnection.user_id==user.id,
        BrokerConnection.broker_id==req.broker_id).first()

    if existing:
        existing.encrypted_fields = encrypted
        existing.mode = req.mode
        existing.is_connected = False  # needs reconnect after credential change
        bc = existing
    else:
        bc = BrokerConnection(user_id=user.id, broker_id=req.broker_id,
                              broker_name=bd.name, market=bd.market,
                              mode=req.mode, encrypted_fields=encrypted)
        db.add(bc)

    db.commit(); db.refresh(bc)
    return {"ok":True,"message":"Credentials saved. Now click Connect to authorise."}

# ── Fyers OAuth2 — self-service connect ──────────────────────

@app.get("/api/brokers/fyers/login-url")
def fyers_login_url(user: User=Depends(get_current_user), db: Session=Depends(get_db)):
    """Returns Fyers login URL — user opens it, logs in, gets auth_code."""
    bc = db.query(BrokerConnection).filter(
        BrokerConnection.user_id==user.id,
        BrokerConnection.broker_id=="fyers").first()
    if not bc or not bc.encrypted_fields:
        raise HTTPException(400,"Save your Fyers credentials first, then click Connect.")

    fields = {k.replace("_enc",""): decrypt(user.id,v)
              for k,v in bc.encrypted_fields.items()}

    conn = FyersConnection(
        user_id=user.id,
        client_id=fields.get("client_id",""),
        secret_key=fields.get("secret_key",""),
        pin=fields.get("pin",""),
        redirect_uri=fields.get("redirect_uri",
            "https://trade.fyers.in/api-login/redirect-uri/index.html"))

    if not conn.client_id:
        raise HTTPException(400,"Client ID not saved. Enter it in credentials first.")

    return {"ok":True,"login_url":conn.login_url()}

class FyersConnectReq(BaseModel):
    auth_code: str

@app.post("/api/brokers/fyers/connect")
async def fyers_connect(req: FyersConnectReq, user: User=Depends(get_current_user),
                         db: Session=Depends(get_db)):
    """User pastes auth_code — system exchanges for tokens and stores them."""
    bc = db.query(BrokerConnection).filter(
        BrokerConnection.user_id==user.id,
        BrokerConnection.broker_id=="fyers").first()
    if not bc: raise HTTPException(400,"Save credentials first.")

    fields = {k.replace("_enc",""): decrypt(user.id,v)
              for k,v in bc.encrypted_fields.items()}

    conn = FyersConnection(
        user_id=user.id,
        client_id=fields.get("client_id",""),
        secret_key=fields.get("secret_key",""),
        pin=fields.get("pin",""),
        redirect_uri=fields.get("redirect_uri",
            "https://trade.fyers.in/api-login/redirect-uri/index.html"))

    result = await conn.exchange_auth_code(req.auth_code.strip())

    if result["ok"]:
        bc.access_token_enc   = result["access_token_enc"]
        bc.refresh_token_enc  = result.get("refresh_token_enc")
        bc.is_connected       = True
        bc.last_tested        = datetime.utcnow()
        bc.last_token_refresh = datetime.utcnow()
        db.commit()
        _send_tg_task(user.id, db,
            "✅ Fyers connected!\nAuto-refresh runs daily at 8:50 AM. No action needed.")
        return {"ok":True,"message":"Fyers connected successfully! Token auto-refreshes daily.",
                "connected":True}
    else:
        return {"ok":False,"message":result["message"],"connected":False}

@app.post("/api/brokers/fyers/refresh")
async def fyers_refresh(user: User=Depends(get_current_user), db: Session=Depends(get_db)):
    """Manual refresh if needed."""
    bc = db.query(BrokerConnection).filter(
        BrokerConnection.user_id==user.id,
        BrokerConnection.broker_id=="fyers").first()
    if not bc or not bc.refresh_token_enc:
        raise HTTPException(400,"Not connected. Use Connect flow first.")

    fields = {k.replace("_enc",""): decrypt(user.id,v)
              for k,v in bc.encrypted_fields.items()}

    conn = FyersConnection(
        user_id=user.id,
        client_id=fields.get("client_id",""),
        secret_key=fields.get("secret_key",""),
        pin=fields.get("pin",""),
        redirect_uri=fields.get("redirect_uri",""),
        refresh_token_enc=bc.refresh_token_enc)

    result = await conn.refresh_access_token()
    if result["ok"]:
        bc.access_token_enc   = result["access_token_enc"]
        if result.get("refresh_token_enc"):
            bc.refresh_token_enc = result["refresh_token_enc"]
        bc.last_token_refresh = datetime.utcnow()
        bc.is_connected       = True
        db.commit()
        return {"ok":True,"message":"Token refreshed successfully"}
    return {"ok":False,"message":result["message"]}

@app.delete("/api/brokers/{broker_id}")
def delete_broker(broker_id: str, user: User=Depends(get_current_user),
                  db: Session=Depends(get_db)):
    db.query(BrokerConnection).filter(
        BrokerConnection.user_id==user.id,
        BrokerConnection.broker_id==broker_id).delete()
    db.commit()
    return {"ok":True}

# ── Automations ───────────────────────────────────────────────

@app.get("/api/automations")
def list_automations(user: User=Depends(get_current_user), db: Session=Depends(get_db)):
    autos = db.query(Automation).filter(Automation.user_id==user.id).all()
    return {"automations":[
        {"id":a.id,"name":a.name,"symbol":a.symbol,"broker_id":a.broker_id,
         "strategies":a.strategies,"mode":a.mode,"status":a.status,"config":a.config}
        for a in autos]}

class SaveAutoReq(BaseModel):
    name: str; symbol: str; broker_id: str
    strategies: list; mode: str="paper"; config: dict={}

@app.post("/api/automations")
def save_automation(req: SaveAutoReq, user: User=Depends(get_current_user),
                    db: Session=Depends(get_db)):
    a = Automation(user_id=user.id, name=req.name, symbol=req.symbol,
                   broker_id=req.broker_id, strategies=req.strategies,
                   mode=req.mode, config=req.config, status="IDLE")
    db.add(a); db.commit(); db.refresh(a)
    return {"ok":True,"automation":{"id":a.id,"name":a.name}}

@app.delete("/api/automations/{auto_id}")
def delete_automation(auto_id: str, user: User=Depends(get_current_user),
                      db: Session=Depends(get_db)):
    db.query(Automation).filter(
        Automation.id==auto_id, Automation.user_id==user.id).delete()
    db.commit()
    return {"ok":True}

# ── Strategies ────────────────────────────────────────────────

@app.get("/api/strategies")
def get_strategies(user: User=Depends(get_current_user)):
    return {"strategies":[
        {"code":"S7","name":"All-Strike Iron Butterfly","tier":"PRO","auto":True},
        {"code":"S1","name":"ORB Breakdown Sell","tier":"STARTER"},
        {"code":"S2","name":"VWAP Squeeze + EMA Cross","tier":"STARTER"},
        {"code":"S8","name":"Opening Gap Fade","tier":"STARTER"},
        {"code":"S3","name":"Breakout Reversal","tier":"STARTER"},
        {"code":"S4","name":"Iron Condor","tier":"PRO"},
        {"code":"S5","name":"Ratio Spread","tier":"PRO"},
        {"code":"S6","name":"Theta Decay Strangle","tier":"PRO"},
        {"code":"S9","name":"Pre-Expiry Theta Crush","tier":"PRO"},
    ]}

# ── Trades ────────────────────────────────────────────────────

@app.get("/api/trades")
def get_trades(user: User=Depends(get_current_user), db: Session=Depends(get_db)):
    trades = db.query(Trade).filter(Trade.user_id==user.id)\
               .order_by(Trade.created_at.desc()).limit(100).all()
    return {"trades":[
        {"id":t.id,"date":t.trade_date,"symbol":t.symbol,
         "strategy":t.strategy_code,"mode":t.mode,"atm":t.atm_strike,
         "entry":t.entry_combined,"exit":t.exit_combined,
         "pnl":t.net_pnl,"exit_reason":t.exit_reason,"is_open":t.is_open}
        for t in trades]}

# ── Engine status ─────────────────────────────────────────────

@app.get("/api/engine/status")
def engine_status(user: User=Depends(get_current_user)):
    return {"running":False,"mode":"IDLE"}

# ── Telegram ──────────────────────────────────────────────────

async def _send_telegram(bot_token: str, chat_id: str, message: str):
    if not bot_token or not chat_id: return
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id":chat_id,"text":f"⬡ ALGO-DESK\n\n{message}"})
    except Exception as e:
        log.error(f"Telegram error: {e}")

def _send_tg_task(user_id: str, db: Session, message: str):
    user = db.query(User).filter(User.id==user_id).first()
    if user and user.telegram_token and user.telegram_chat:
        asyncio.create_task(_send_telegram(user.telegram_token,user.telegram_chat,message))

@app.post("/api/telegram/test")
async def test_telegram(user: User=Depends(get_current_user)):
    if not user.telegram_token or not user.telegram_chat:
        raise HTTPException(400,"Configure Telegram in profile first")
    await _send_telegram(user.telegram_token, user.telegram_chat,
        f"✅ Test from ALGO-DESK\nHello {user.name}! Alerts are working.")
    return {"ok":True}

# ── Daily token refresh scheduler ────────────────────────────

async def _start_scheduler():
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        import pytz
        scheduler = AsyncIOScheduler(timezone=pytz.timezone("Asia/Kolkata"))

        async def refresh_all():
            log.info("8:50 AM — Daily Fyers token refresh...")
            db = SessionLocal()
            try:
                connections = db.query(BrokerConnection).filter(
                    BrokerConnection.broker_id=="fyers",
                    BrokerConnection.is_connected==True).all()
                for bc in connections:
                    user = db.query(User).filter(
                        User.id==bc.user_id, User.is_active==True).first()
                    if not user or not bc.refresh_token_enc: continue
                    fields = {k.replace("_enc",""): decrypt(user.id,v)
                              for k,v in bc.encrypted_fields.items()}
                    conn = FyersConnection(
                        user_id=user.id,
                        client_id=fields.get("client_id",""),
                        secret_key=fields.get("secret_key",""),
                        pin=fields.get("pin",""),
                        redirect_uri=fields.get("redirect_uri",""),
                        refresh_token_enc=bc.refresh_token_enc)
                    result = await conn.refresh_access_token()
                    if result["ok"]:
                        bc.access_token_enc   = result["access_token_enc"]
                        if result.get("refresh_token_enc"):
                            bc.refresh_token_enc = result["refresh_token_enc"]
                        bc.last_token_refresh = datetime.utcnow()
                        bc.is_connected       = True
                        log.info(f"Token refreshed for {user.email} ✓")
                        if user.telegram_token and user.telegram_chat:
                            await _send_telegram(user.telegram_token,user.telegram_chat,
                                "✅ Daily token refresh complete\nFyers ready. Market opens at 9:15 AM.")
                    else:
                        bc.is_connected = False
                        log.error(f"Refresh failed for {user.email}: {result['message']}")
                        if user.telegram_token and user.telegram_chat:
                            await _send_telegram(user.telegram_token,user.telegram_chat,
                                f"⚠️ Token refresh failed: {result['message']}\nPlease reconnect in My Brokers.")
                db.commit()
            except Exception as e:
                log.error(f"Scheduler error: {e}")
            finally:
                db.close()

        scheduler.add_job(refresh_all,"cron",hour=8,minute=50)
        scheduler.start()
        log.info("Daily refresh scheduler started — 8:50 AM IST")
    except Exception as e:
        log.error(f"Scheduler init error: {e}")

# ── Frontend ──────────────────────────────────────────────────

frontend_path = "/app/frontend"
if os.path.exists(frontend_path) and os.listdir(frontend_path):
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")
else:
    @app.get("/")
    def root():
        return JSONResponse({"message":"ALGO-DESK API v4","version":"4.0.0"})
