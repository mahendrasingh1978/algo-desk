"""
ALGO-DESK v4 — Complete Backend
=================================
All endpoints. PostgreSQL. Scheduler. WebSocket.
Nothing hardcoded. Everything from database.
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
from fyers import FyersEngine, encrypt, decrypt
from engine import EngineState, StrikeState, check_all_strategies, check_sl, nearest_strike

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("algodesk")

# ═══════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://algodesk:algodesk@localhost:5432/algodesk"
)

engine_db = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=5)
SessionLocal = sessionmaker(bind=engine_db, autocommit=False, autoflush=False)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    """Create all tables and seed initial data."""
    Base.metadata.create_all(bind=engine_db)
    db = SessionLocal()
    try:
        # Seed admin user
        email = os.environ.get("SUPER_ADMIN_EMAIL", "")
        pw    = os.environ.get("SUPER_ADMIN_PASSWORD", "")
        name  = os.environ.get("SUPER_ADMIN_NAME", "Admin")
        if email and not db.query(User).filter(User.email == email).first():
            pw_hash = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
            db.add(User(
                email=email, name=name, password_hash=pw_hash,
                role="SUPER_ADMIN", plan="PRO", is_active=True, is_verified=True,
            ))
            log.info(f"Admin created: {email}")

        # Seed broker definitions if empty
        if not db.query(BrokerDefinition).first():
            _seed_broker_definitions(db)

        db.commit()
    except Exception as e:
        log.error(f"DB init error: {e}")
        db.rollback()
    finally:
        db.close()

def _seed_broker_definitions(db: Session):
    """Seed Fyers broker definition. Admin can add more from UI."""
    brokers = [
        {
            "broker_id": "fyers",
            "name": "Fyers",
            "flag": "🇮🇳",
            "market": "INDIA",
            "refresh_desc": "Auto-TOTP daily at 8:50 AM — no manual login ever needed",
            "test_method": "totp",
            "api_base_url": "https://api-t1.fyers.in/api/v3",
            "sort_order": 1,
            "fields_config": [
                {"key": "client_id",     "label": "Client ID",       "secret": False,
                 "hint": "Go to myapi.fyers.in → your app → Client ID (format: FYXXXXX-100)"},
                {"key": "secret_key",    "label": "Secret Key",      "secret": True,
                 "hint": "myapi.fyers.in → your app → Secret Key"},
                {"key": "redirect_uri",  "label": "Redirect URI",    "secret": False,
                 "hint": "Paste exactly: https://trade.fyers.in/api-login/redirect-uri/index.html",
                 "default": "https://trade.fyers.in/api-login/redirect-uri/index.html"},
                {"key": "username",      "label": "Fyers User ID",   "secret": False,
                 "hint": "Your Fyers client ID (same as Client ID above, e.g. FYXXXXX)"},
                {"key": "pin",           "label": "4-digit PIN",     "secret": True,
                 "hint": "Your Fyers trading PIN"},
                {"key": "totp_key",      "label": "TOTP Secret Key", "secret": True,
                 "hint": "Fyers → My Account → Security Settings → Two Factor Auth → Enable TOTP → External Authenticator → copy the 32-character key shown (NOT the QR code)"},
            ],
        },
        {
            "broker_id": "zerodha",
            "name": "Zerodha (Kite)",
            "flag": "🇮🇳",
            "market": "INDIA",
            "refresh_desc": "Auto-TOTP daily",
            "test_method": "totp",
            "api_base_url": "https://api.kite.trade",
            "sort_order": 2,
            "fields_config": [
                {"key": "api_key",    "label": "API Key",    "secret": False,
                 "hint": "kite.trade → Apps → your app → API Key"},
                {"key": "api_secret", "label": "API Secret", "secret": True,
                 "hint": "kite.trade → Apps → your app → API Secret"},
                {"key": "user_id",    "label": "User ID",    "secret": False,
                 "hint": "Your Zerodha client ID (e.g. AB1234)"},
                {"key": "password",   "label": "Password",   "secret": True,
                 "hint": "Your Zerodha login password"},
                {"key": "totp_key",   "label": "TOTP Key",   "secret": True,
                 "hint": "The text key shown when you set up Google Authenticator for Zerodha"},
            ],
        },
        {
            "broker_id": "alpaca",
            "name": "Alpaca (US)",
            "flag": "🇺🇸",
            "market": "US",
            "refresh_desc": "API key never expires — set up once, works forever",
            "test_method": "api_key",
            "api_base_url": "https://paper-api.alpaca.markets",
            "sort_order": 3,
            "fields_config": [
                {"key": "api_key_id", "label": "API Key ID",   "secret": False,
                 "hint": "alpaca.markets → Paper or Live Trading → API Keys → Key ID (starts with PK...)"},
                {"key": "secret_key", "label": "Secret Key",   "secret": True,
                 "hint": "alpaca.markets → API Keys → Secret Key (only shown once at creation)"},
                {"key": "mode",       "label": "Account Mode", "secret": False,
                 "type": "select", "options": ["paper", "live"],
                 "hint": "paper = test with fake money, live = real money"},
            ],
        },
    ]
    for b in brokers:
        db.add(BrokerDefinition(**b))
    log.info(f"Seeded {len(brokers)} broker definitions")

# ═══════════════════════════════════════════════════════════════
# APP + AUTH
# ═══════════════════════════════════════════════════════════════

app = FastAPI(title="ALGO-DESK", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

bearer = HTTPBearer(auto_error=False)
SECRET = os.environ.get("SECRET_KEY", secrets.token_hex(32))
ALGO   = "HS256"

def make_token(email: str, role: str) -> str:
    return jwt.encode(
        {"sub": email, "role": role, "exp": datetime.utcnow() + timedelta(hours=12)},
        SECRET, algorithm=ALGO
    )

def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(bearer),
    db: Session = Depends(get_db)
) -> User:
    if not creds:
        raise HTTPException(401, "Not authenticated")
    try:
        payload = jwt.decode(creds.credentials, SECRET, algorithms=[ALGO])
        email = payload["sub"]
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

# ═══════════════════════════════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup():
    init_db()
    log.info("ALGO-DESK started ✓")

# ═══════════════════════════════════════════════════════════════
# HEALTH
# ═══════════════════════════════════════════════════════════════

@app.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    return {"status": "ok", "version": "4.0.0",
            "db": "ok" if db_ok else "error",
            "time": datetime.now().isoformat()}

# ═══════════════════════════════════════════════════════════════
# AUTH ENDPOINTS
# ═══════════════════════════════════════════════════════════════

class LoginReq(BaseModel):
    email: str
    password: str

@app.post("/api/auth/login")
def login(req: LoginReq, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == req.email.lower().strip()).first()
    if not user:
        raise HTTPException(401, "Invalid email or password")
    if not user.is_active:
        raise HTTPException(401, "Account suspended. Contact admin.")
    if not bcrypt.checkpw(req.password.encode(), user.password_hash.encode()):
        raise HTTPException(401, "Invalid email or password")
    user.last_login = datetime.utcnow()
    db.commit()
    return {
        "token": make_token(user.email, user.role),
        "name": user.name, "email": user.email,
        "role": user.role, "plan": user.plan,
    }

class RegisterReq(BaseModel):
    email: str
    password: str
    name: str
    invite_token: Optional[str] = None

@app.post("/api/auth/register")
def register(req: RegisterReq, db: Session = Depends(get_db)):
    email = req.email.lower().strip()
    reg_open = os.environ.get("REGISTRATION_OPEN", "true").lower() == "true"
    invite = None
    if req.invite_token:
        invite = db.query(InviteLink).filter(
            InviteLink.token == req.invite_token,
            InviteLink.used == False
        ).first()
    if not reg_open and not invite:
        raise HTTPException(403, "Registration is closed. Ask admin for an invite link.")
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(400, "Email already registered")
    if len(req.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    pw_hash = bcrypt.hashpw(req.password.encode(), bcrypt.gensalt()).decode()
    user = User(
        email=email, name=req.name, password_hash=pw_hash,
        role=invite.role if invite else "USER",
        plan=invite.plan if invite else "FREE",
        is_active=True, is_verified=False,
    )
    db.add(user)
    if invite:
        invite.used = True
        invite.used_by = email
    db.commit()
    return {"ok": True, "token": make_token(email, user.role),
            "name": req.name, "email": email, "role": user.role, "plan": user.plan}

class ResetRequestReq(BaseModel):
    email: str

@app.post("/api/auth/reset-request")
def reset_request(req: ResetRequestReq, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == req.email.lower()).first()
    if user:
        token = secrets.token_urlsafe(32)
        db.add(ResetToken(
            user_id=user.id, token=token,
            expires_at=datetime.utcnow() + timedelta(hours=24)
        ))
        db.commit()
        domain = os.environ.get("APP_DOMAIN", "localhost")
        reset_url = f"https://{domain}/?reset_token={token}"
        # Send via Telegram if configured
        if user.telegram_chat and user.telegram_token:
            asyncio.create_task(_send_telegram(
                user.telegram_token, user.telegram_chat,
                f"🔑 ALGO-DESK Password Reset\n\nClick to reset: {reset_url}\n\nExpires in 24 hours."
            ))
        log.info(f"Reset requested for {user.email}: {reset_url}")
        return {"ok": True, "message": "Reset link sent via Telegram. If no Telegram configured, ask your admin.",
                "reset_url": reset_url}
    return {"ok": True, "message": "Reset link sent if account exists."}

class ResetPasswordReq(BaseModel):
    token: str
    new_password: str

@app.post("/api/auth/reset-password")
def reset_password(req: ResetPasswordReq, db: Session = Depends(get_db)):
    rt = db.query(ResetToken).filter(
        ResetToken.token == req.token,
        ResetToken.used == False
    ).first()
    if not rt or datetime.utcnow() > rt.expires_at:
        raise HTTPException(400, "Invalid or expired reset link")
    if len(req.new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    user = db.query(User).filter(User.id == rt.user_id).first()
    user.password_hash = bcrypt.hashpw(req.new_password.encode(), bcrypt.gensalt()).decode()
    rt.used = True
    db.commit()
    return {"ok": True, "message": "Password updated. You can now sign in."}

class ChangePasswordReq(BaseModel):
    current_password: str
    new_password: str

@app.post("/api/auth/change-password")
def change_password(req: ChangePasswordReq,
                    user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    if not bcrypt.checkpw(req.current_password.encode(), user.password_hash.encode()):
        raise HTTPException(400, "Current password is incorrect")
    if len(req.new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    user.password_hash = bcrypt.hashpw(req.new_password.encode(), bcrypt.gensalt()).decode()
    db.commit()
    return {"ok": True}

# ═══════════════════════════════════════════════════════════════
# USER PROFILE
# ═══════════════════════════════════════════════════════════════

@app.get("/api/me")
def me(user: User = Depends(get_current_user)):
    return {"id": user.id, "email": user.email, "name": user.name,
            "role": user.role, "plan": user.plan, "timezone": user.timezone,
            "telegram_configured": bool(user.telegram_chat)}

class UpdateProfileReq(BaseModel):
    name: Optional[str] = None
    timezone: Optional[str] = None
    telegram_token: Optional[str] = None
    telegram_chat: Optional[str] = None

@app.put("/api/me")
def update_profile(req: UpdateProfileReq,
                   user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    if req.name:           user.name = req.name
    if req.timezone:       user.timezone = req.timezone
    if req.telegram_token: user.telegram_token = req.telegram_token
    if req.telegram_chat:  user.telegram_chat = req.telegram_chat
    db.commit()
    return {"ok": True}

# ═══════════════════════════════════════════════════════════════
# ADMIN — USER MANAGEMENT
# ═══════════════════════════════════════════════════════════════

@app.get("/api/admin/users")
def list_users(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    users = db.query(User).all()
    result = []
    for u in users:
        broker_count = db.query(BrokerConnection).filter(BrokerConnection.user_id == u.id).count()
        result.append({
            "id": u.id, "email": u.email, "name": u.name,
            "role": u.role, "plan": u.plan,
            "is_active": u.is_active, "is_verified": u.is_verified,
            "broker_count": broker_count,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_login": u.last_login.isoformat() if u.last_login else None,
        })
    return {"users": result, "total": len(result)}

class CreateUserReq(BaseModel):
    email: str
    name: str
    password: str
    role: str = "USER"
    plan: str = "FREE"

@app.post("/api/admin/users")
def create_user(req: CreateUserReq,
                admin: User = Depends(require_admin),
                db: Session = Depends(get_db)):
    email = req.email.lower().strip()
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(400, "Email already exists")
    pw_hash = bcrypt.hashpw(req.password.encode(), bcrypt.gensalt()).decode()
    user = User(email=email, name=req.name, password_hash=pw_hash,
                role=req.role, plan=req.plan, is_active=True, is_verified=True)
    db.add(user)
    db.commit()
    return {"ok": True, "message": f"User {email} created"}

class UpdateUserReq(BaseModel):
    name:      Optional[str]  = None
    role:      Optional[str]  = None
    plan:      Optional[str]  = None
    is_active: Optional[bool] = None
    password:  Optional[str]  = None

@app.put("/api/admin/users/{user_id}")
def update_user(user_id: str, req: UpdateUserReq,
                admin: User = Depends(require_admin),
                db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user: raise HTTPException(404, "User not found")
    if req.name:           user.name = req.name
    if req.role:           user.role = req.role
    if req.plan:           user.plan = req.plan
    if req.is_active is not None: user.is_active = req.is_active
    if req.password:
        user.password_hash = bcrypt.hashpw(req.password.encode(), bcrypt.gensalt()).decode()
    db.commit()
    return {"ok": True}

@app.post("/api/admin/users/{user_id}/suspend")
def suspend_user(user_id: str, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if user: user.is_active = False; db.commit()
    return {"ok": True}

@app.post("/api/admin/users/{user_id}/activate")
def activate_user(user_id: str, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if user: user.is_active = True; db.commit()
    return {"ok": True}

@app.post("/api/admin/users/{user_id}/reset-password")
def admin_reset_pw(user_id: str, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user: raise HTTPException(404, "User not found")
    token = secrets.token_urlsafe(32)
    db.add(ResetToken(user_id=user.id, token=token,
                      expires_at=datetime.utcnow() + timedelta(hours=24)))
    db.commit()
    domain = os.environ.get("APP_DOMAIN", "localhost")
    return {"ok": True, "reset_url": f"https://{domain}/?reset_token={token}",
            "message": "Send this link to the user"}

@app.post("/api/admin/invite")
def create_invite(req: dict, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    token = secrets.token_urlsafe(24)
    db.add(InviteLink(token=token, created_by=admin.id,
                      role=req.get("role","USER"), plan=req.get("plan","FREE")))
    db.commit()
    domain = os.environ.get("APP_DOMAIN", "localhost")
    return {"ok": True,
            "invite_url": f"https://{domain}/?invite={token}",
            "token": token}

@app.get("/api/admin/stats")
def admin_stats(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    users = db.query(User).all()
    return {
        "total_users": len(users),
        "active_users": sum(1 for u in users if u.is_active),
        "total_brokers": db.query(BrokerConnection).count(),
        "total_automations": db.query(Automation).count(),
        "plans": {
            p: sum(1 for u in users if u.plan == p)
            for p in ["FREE","STARTER","PRO","ENTERPRISE"]
        }
    }

# ═══════════════════════════════════════════════════════════════
# BROKER DEFINITIONS (admin manages, users consume)
# ═══════════════════════════════════════════════════════════════

@app.get("/api/brokers/definitions")
def broker_definitions(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    defs = db.query(BrokerDefinition).filter(BrokerDefinition.is_active == True)\
             .order_by(BrokerDefinition.sort_order).all()
    return {"brokers": [
        {"id": d.broker_id, "name": d.name, "flag": d.flag,
         "market": d.market, "refresh": d.refresh_desc,
         "test_method": d.test_method, "fields": d.fields_config}
        for d in defs
    ]}

class AddBrokerDefReq(BaseModel):
    broker_id:     str
    name:          str
    flag:          str = "🏦"
    market:        str = "INDIA"
    refresh_desc:  str = "Auto-managed"
    test_method:   str = "totp"
    api_base_url:  Optional[str] = None
    fields_config: list

@app.post("/api/admin/broker-definitions")
def add_broker_def(req: AddBrokerDefReq,
                   admin: User = Depends(require_admin),
                   db: Session = Depends(get_db)):
    if db.query(BrokerDefinition).filter(BrokerDefinition.broker_id == req.broker_id).first():
        raise HTTPException(400, "Broker ID already exists")
    db.add(BrokerDefinition(**req.dict()))
    db.commit()
    return {"ok": True}

@app.put("/api/admin/broker-definitions/{broker_id}")
def update_broker_def(broker_id: str, req: dict,
                      admin: User = Depends(require_admin),
                      db: Session = Depends(get_db)):
    bd = db.query(BrokerDefinition).filter(BrokerDefinition.broker_id == broker_id).first()
    if not bd: raise HTTPException(404, "Not found")
    for k, v in req.items():
        if hasattr(bd, k): setattr(bd, k, v)
    db.commit()
    return {"ok": True}

# ═══════════════════════════════════════════════════════════════
# BROKER CONNECTIONS (user self-service)
# ═══════════════════════════════════════════════════════════════

@app.get("/api/brokers")
def list_my_brokers(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    brokers = db.query(BrokerConnection).filter(BrokerConnection.user_id == user.id).all()
    return {"brokers": [
        {"id": b.id, "broker_id": b.broker_id, "broker_name": b.broker_name,
         "market": b.market, "mode": b.mode, "is_connected": b.is_connected,
         "last_tested": b.last_tested.isoformat() if b.last_tested else None,
         "last_token_refresh": b.last_token_refresh.isoformat() if b.last_token_refresh else None,
         "fields_count": len(b.encrypted_fields or {})}
        for b in brokers
    ]}

class SaveBrokerReq(BaseModel):
    broker_id: str
    fields:    dict
    mode:      str = "paper"

@app.post("/api/brokers")
def save_broker(req: SaveBrokerReq,
                user: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    bd = db.query(BrokerDefinition).filter(BrokerDefinition.broker_id == req.broker_id).first()
    if not bd: raise HTTPException(400, f"Unknown broker: {req.broker_id}")

    # Encrypt all fields
    encrypted = {}
    for k, v in req.fields.items():
        if v and str(v).strip():
            encrypted[k + "_enc"] = encrypt(user.id, str(v).strip())

    # Upsert
    existing = db.query(BrokerConnection).filter(
        BrokerConnection.user_id == user.id,
        BrokerConnection.broker_id == req.broker_id
    ).first()

    if existing:
        existing.encrypted_fields = encrypted
        existing.mode = req.mode
        existing.is_connected = False
        bc = existing
    else:
        bc = BrokerConnection(
            user_id=user.id, broker_id=req.broker_id,
            broker_name=bd.name, market=bd.market,
            mode=req.mode, encrypted_fields=encrypted,
        )
        db.add(bc)

    db.commit()
    db.refresh(bc)
    return {"ok": True, "message": "Credentials saved and encrypted", "connection_id": bc.id}

@app.post("/api/brokers/{broker_id}/test")
async def test_broker(broker_id: str,
                      user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    bc = db.query(BrokerConnection).filter(
        BrokerConnection.user_id == user.id,
        BrokerConnection.broker_id == broker_id
    ).first()
    if not bc: raise HTTPException(404, "Broker not configured")

    bd = db.query(BrokerDefinition).filter(BrokerDefinition.broker_id == broker_id).first()

    result = await _test_connection(broker_id, bc, bd, user.id)

    bc.is_connected = result["connected"]
    bc.last_tested  = datetime.utcnow()
    db.commit()

    return result

async def _test_connection(broker_id: str, bc: BrokerConnection,
                            bd: Optional[BrokerDefinition], user_id: str) -> dict:
    """Generic connection test based on test_method from broker definition."""
    import httpx

    test_method = bd.test_method if bd else "totp"

    if broker_id == "fyers" or test_method == "totp":
        # For TOTP brokers: validate credentials format without making API calls
        eng = FyersEngine(user_id, bc.encrypted_fields or {}, mode=bc.mode)
        return await eng.validate_credentials()

    elif test_method == "api_key" and broker_id == "alpaca":
        fields = {k.replace("_enc",""): decrypt(user_id, v)
                  for k, v in (bc.encrypted_fields or {}).items()}
        mode = fields.get("mode","paper")
        base = ("https://paper-api.alpaca.markets" if mode=="paper"
                else "https://api.alpaca.markets")
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{base}/v2/account",
                    headers={"APCA-API-KEY-ID":     fields.get("api_key_id",""),
                             "APCA-API-SECRET-KEY": fields.get("secret_key","")})
            if r.status_code == 200:
                acc = r.json()
                return {"connected": True,
                        "message": f"✓ Alpaca connected. Account: {acc.get('account_number','')} | Cash: ${float(acc.get('cash',0)):.2f}"}
            return {"connected": False, "message": f"Alpaca auth failed (HTTP {r.status_code}). Check API keys."}
        except Exception as e:
            return {"connected": False, "message": f"Connection error: {str(e)}"}

    else:
        # Generic — just check fields present
        required = [f["key"] for f in (bd.fields_config or []) if not f.get("optional")]
        missing = [k for k in required if not (bc.encrypted_fields or {}).get(k+"_enc")]
        if missing:
            return {"connected": False, "message": f"Missing fields: {', '.join(missing)}"}
        return {"connected": True,
                "message": f"✓ Credentials saved for {bd.name if bd else broker_id}. Will authenticate at market open."}

@app.delete("/api/brokers/{broker_id}")
def delete_broker(broker_id: str,
                  user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    db.query(BrokerConnection).filter(
        BrokerConnection.user_id == user.id,
        BrokerConnection.broker_id == broker_id
    ).delete()
    db.commit()
    return {"ok": True}

# ═══════════════════════════════════════════════════════════════
# AUTOMATIONS
# ═══════════════════════════════════════════════════════════════

@app.get("/api/automations")
def list_automations(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    autos = db.query(Automation).filter(Automation.user_id == user.id).all()
    return {"automations": [
        {"id": a.id, "name": a.name, "symbol": a.symbol,
         "broker_id": a.broker_id, "strategies": a.strategies,
         "mode": a.mode, "status": a.status, "config": a.config,
         "created_at": a.created_at.isoformat() if a.created_at else None}
        for a in autos
    ]}

class SaveAutomationReq(BaseModel):
    name:       str
    symbol:     str
    broker_id:  str
    strategies: list
    mode:       str = "paper"
    config:     dict = {}

@app.post("/api/automations")
def save_automation(req: SaveAutomationReq,
                    user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    a = Automation(user_id=user.id, name=req.name, symbol=req.symbol,
                   broker_id=req.broker_id, strategies=req.strategies,
                   mode=req.mode, config=req.config, status="IDLE")
    db.add(a); db.commit(); db.refresh(a)
    return {"ok": True, "automation": {"id": a.id, "name": a.name}}

@app.delete("/api/automations/{auto_id}")
def delete_automation(auto_id: str,
                      user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    db.query(Automation).filter(
        Automation.id == auto_id,
        Automation.user_id == user.id
    ).delete()
    db.commit()
    return {"ok": True}

# ═══════════════════════════════════════════════════════════════
# STRATEGIES
# ═══════════════════════════════════════════════════════════════

@app.get("/api/strategies")
def get_strategies(user: User = Depends(get_current_user)):
    return {"strategies": [
        {"code":"S7","name":"All-Strike Iron Butterfly","tier":"PRO","auto":True},
        {"code":"S1","name":"ORB Breakdown Sell",       "tier":"STARTER"},
        {"code":"S2","name":"VWAP Squeeze + EMA Cross", "tier":"STARTER"},
        {"code":"S8","name":"Opening Gap Fade",          "tier":"STARTER"},
        {"code":"S3","name":"Breakout Reversal",         "tier":"STARTER"},
        {"code":"S4","name":"Iron Condor",               "tier":"PRO"},
        {"code":"S5","name":"Ratio Spread",              "tier":"PRO"},
        {"code":"S6","name":"Theta Decay Strangle",      "tier":"PRO"},
        {"code":"S9","name":"Pre-Expiry Theta Crush",    "tier":"PRO"},
    ]}

# ═══════════════════════════════════════════════════════════════
# TRADES
# ═══════════════════════════════════════════════════════════════

@app.get("/api/trades")
def get_trades(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    trades = db.query(Trade).filter(Trade.user_id == user.id)\
               .order_by(Trade.created_at.desc()).limit(100).all()
    return {"trades": [
        {"id": t.id, "date": t.trade_date, "symbol": t.symbol,
         "strategy": t.strategy_code, "mode": t.mode,
         "atm": t.atm_strike, "entry": t.entry_combined,
         "exit": t.exit_combined, "pnl": t.net_pnl,
         "exit_reason": t.exit_reason, "is_open": t.is_open}
        for t in trades
    ]}

# ═══════════════════════════════════════════════════════════════
# ENGINE WebSocket — real-time updates
# ═══════════════════════════════════════════════════════════════

# Active engine states per user
active_engines: dict[str, EngineState] = {}
ws_clients: dict[str, list[WebSocket]] = {}

@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    await websocket.accept()
    if user_id not in ws_clients:
        ws_clients[user_id] = []
    ws_clients[user_id].append(websocket)
    try:
        while True:
            # Send engine state every 5 seconds
            if user_id in active_engines:
                state = active_engines[user_id]
                atm = state.atm
                await websocket.send_json({
                    "type": "tick",
                    "spot": state.spot_history[-1] if state.spot_history else 0,
                    "atm": state.atm_strike,
                    "combined": atm.current if atm else 0,
                    "vwap": atm.vwap_val if atm else 0,
                    "ema75": atm.ema75 if atm else 0,
                    "orb_low": atm.orb_low if atm else 0,
                    "position": state.position,
                    "day_pnl": state.day_pnl,
                    "mode": "IN_TRADE" if state.position else ("MONITORING" if state.orb_complete else "ORB_BUILD"),
                    "log": state.log[-20:],
                    "strikes": [
                        {"strike": s.strike, "combined": s.current,
                         "orb_low": s.orb_low, "orb_high": s.orb_high,
                         "fired": s.fired}
                        for s in state.strikes
                    ]
                })
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        ws_clients[user_id].remove(websocket)

# ═══════════════════════════════════════════════════════════════
# ENGINE START/STOP
# ═══════════════════════════════════════════════════════════════

class StartEngineReq(BaseModel):
    automation_id: str

@app.post("/api/engine/start")
async def start_engine(req: StartEngineReq,
                       user: User = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    auto = db.query(Automation).filter(
        Automation.id == req.automation_id,
        Automation.user_id == user.id
    ).first()
    if not auto: raise HTTPException(404, "Automation not found")

    bc = db.query(BrokerConnection).filter(
        BrokerConnection.user_id == user.id,
        BrokerConnection.broker_id == auto.broker_id
    ).first()
    if not bc: raise HTTPException(400, "Broker not connected")
    if not bc.is_connected:
        raise HTTPException(400, "Broker connection not validated. Go to My Brokers and test connection first.")

    # Build engine state
    config = {**auto.config, "strategies": auto.strategies, "mode": auto.mode}
    state = EngineState(config)
    active_engines[user.id] = state

    # Start engine loop
    fyers_eng = FyersEngine(
        user.id,
        bc.encrypted_fields or {},
        access_token_enc=bc.access_token_enc,
        mode=auto.mode
    )

    asyncio.create_task(_run_engine(user.id, auto, state, fyers_eng, db))
    auto.status = "RUNNING"
    db.commit()

    return {"ok": True, "message": f"Engine started for {auto.name}"}

@app.post("/api/engine/stop")
async def stop_engine(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.id in active_engines:
        active_engines[user.id].is_running = False
        del active_engines[user.id]
    db.query(Automation).filter(Automation.user_id == user.id)\
      .update({"status": "IDLE"})
    db.commit()
    return {"ok": True}

@app.get("/api/engine/status")
def engine_status(user: User = Depends(get_current_user)):
    state = active_engines.get(user.id)
    if not state:
        return {"running": False, "mode": "IDLE"}
    atm = state.atm
    return {
        "running": True,
        "mode": "IN_TRADE" if state.position else "MONITORING",
        "spot": state.spot_history[-1] if state.spot_history else 0,
        "atm": state.atm_strike,
        "combined": atm.current if atm else 0,
        "position": state.position,
        "day_pnl": state.day_pnl,
    }

@app.post("/api/engine/force-exit")
async def force_exit(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    state = active_engines.get(user.id)
    if state and state.position:
        state.position["force_exit"] = True
    return {"ok": True}

# ═══════════════════════════════════════════════════════════════
# ENGINE LOOP
# ═══════════════════════════════════════════════════════════════

async def _run_engine(user_id: str, auto: Automation,
                      state: EngineState, fyers: FyersEngine,
                      db_factory):
    """Main engine loop. Runs every 60 seconds."""
    state.is_running = True
    config = state.config
    symbol = auto.symbol
    gap    = state.gap
    sides  = state.sides
    lot_sz = int(config.get("lot_size", 25))
    lots   = int(config.get("lots", 1))

    state.emit(f"Engine started. Symbol: {symbol}. Mode: {auto.mode.upper()}", "START")

    # If token expired, try to authenticate
    if not fyers._token:
        state.emit("No active token. Attempting authentication...", "AUTH")
        ok, msg, enc_token = await fyers.authenticate()
        if ok and enc_token:
            state.emit(f"Authentication successful ✓", "OK")
            # Save token to DB
            db = SessionLocal()
            try:
                bc = db.query(BrokerConnection).filter(
                    BrokerConnection.user_id == user_id,
                    BrokerConnection.broker_id == auto.broker_id
                ).first()
                if bc:
                    bc.access_token_enc = enc_token
                    bc.last_token_refresh = datetime.utcnow()
                    bc.is_connected = True
                    db.commit()
            finally:
                db.close()
        else:
            state.emit(f"Authentication failed: {msg}", "ERROR")
            state.emit("Engine stopped. Check broker credentials.", "ERROR")
            return

    while state.is_running:
        try:
            now = datetime.now()
            t   = now.time()

            # Auto-exit time
            exit_time_str = config.get("auto_exit_time", "14:00")
            eh, em = map(int, exit_time_str.split(":"))
            if t >= dtime(eh, em):
                if state.position:
                    await _close_position(state, fyers, "AUTO_EXIT", lot_sz, lots)
                state.emit(f"Auto-exit at {exit_time_str}. Engine stopping.", "INFO")
                break

            # Max loss / max profit check
            max_loss   = float(config.get("max_loss",   5000))
            max_profit = float(config.get("max_profit", 15000))
            if state.day_pnl <= -max_loss:
                if state.position:
                    await _close_position(state, fyers, "MAX_LOSS", lot_sz, lots)
                state.emit(f"Max daily loss ₹{max_loss} hit. Engine stopping.", "SL")
                break
            if state.day_pnl >= max_profit:
                if state.position:
                    await _close_position(state, fyers, "MAX_PROFIT", lot_sz, lots)
                state.emit(f"Max daily profit ₹{max_profit} hit. Engine stopping.", "OK")
                break

            # Get spot price
            spot = await fyers.get_ltp(symbol)
            if spot is None:
                state.emit("Could not get spot price. Retrying...", "WARN")
                await asyncio.sleep(30)
                continue

            state.spot_history.append(spot)

            # Lock ATM at 9:15
            if t >= dtime(9, 15) and not state.atm_strike:
                state.spot_locked = spot
                state.atm_strike  = nearest_strike(spot, gap)
                # Build 7-strike grid
                for i in range(-sides, sides+1):
                    sk = StrikeState(
                        strike=state.atm_strike + i * gap,
                        offset=i,
                        is_atm=(i == 0)
                    )
                    state.strikes.append(sk)
                state.emit(f"ATM locked: {state.atm_strike} | Spot: {spot:.1f} | ±{sides} strikes grid built", "OK")

            # Get option chain and update strike states
            if state.strikes:
                chain = await fyers.get_option_chain(symbol, strike_count=sides*2+2)
                for sk in state.strikes:
                    data = chain.get(sk.strike)
                    if data:
                        combined = data["ce_ltp"] + data["pe_ltp"]
                        sk.combined_history.append(combined)
                        sk.ce_symbol = data.get("ce_symbol", "")
                        sk.pe_symbol = data.get("pe_symbol", "")
                        # ORB window: collect high/low
                        if dtime(9,15) <= t <= dtime(9,21):
                            if sk.orb_high == 0:
                                sk.orb_high = sk.orb_low = combined
                            sk.orb_high = max(sk.orb_high, combined)
                            sk.orb_low  = min(sk.orb_low,  combined)

            # ORB complete at 9:22
            if t >= dtime(9, 22) and not state.orb_complete:
                state.orb_complete = True
                atm = state.atm
                if atm:
                    state.emit(f"ORB complete. ATM {atm.strike}: Low={atm.orb_low:.1f} High={atm.orb_high:.1f}", "OK")
                # Check all-7 breakdown
                broken = [s for s in state.strikes
                          if s.orb_low > 0 and s.current < s.orb_low * 0.98]
                if len(broken) >= len(state.strikes):
                    state.all_breakdown = True
                    state.emit(f"ALL-7 BREAKDOWN DETECTED ★ Routing to S7 Iron Butterfly", "SIGNAL")
                    _send_tg_task(user_id, db_factory, "★ ALL-7 BREAKDOWN — Iron Butterfly signal")
                else:
                    state.no_breakdown = len(broken) == 0

            # Check SL on open position
            if state.position:
                reason = check_sl(state)
                if reason or state.position.get("force_exit"):
                    reason = reason or "FORCE_EXIT"
                    await _close_position(state, fyers, reason, lot_sz, lots)
                    _send_tg_task(user_id, db_factory,
                                  f"🔴 Position closed: {reason} | P&L: ₹{state.day_pnl:.0f}")

            # Check strategies for new signal
            elif state.orb_complete:
                signal = check_all_strategies(state, now)
                if signal:
                    state.emit(f"SIGNAL: [{signal['code']}] {signal['reason']}", "SIGNAL")
                    await _open_position(state, fyers, signal, lot_sz, lots, user_id, auto.id, db_factory)
                    _send_tg_task(user_id, db_factory,
                                  f"🟢 Signal: {signal['code']} | {signal['name']}\n"
                                  f"Strike: {signal['strike']} | Combined: {signal['combined']:.1f}\n"
                                  f"Mode: {auto.mode.upper()}")

        except Exception as e:
            log.error(f"[engine:{user_id}] Loop error: {e}")
            state.emit(f"Engine error: {str(e)}", "ERROR")

        await asyncio.sleep(60)  # Wait 1 minute between ticks

    state.is_running = False
    state.emit("Engine stopped.", "INFO")

async def _open_position(state: EngineState, fyers: FyersEngine,
                          signal: dict, lot_sz: int, lots: int,
                          user_id: str, auto_id: str, db_factory):
    """Place entry orders and record position."""
    qty = lot_sz * lots
    orders = []

    # Place sell legs
    for leg, symbol_key in [("sell_ce", "ce_symbol"), ("sell_pe", "pe_symbol")]:
        sk = next((s for s in state.strikes if s.strike == signal[leg]), None)
        sym = sk.ce_symbol if "ce" in leg else (sk.pe_symbol if sk else "")
        if sym:
            r = await fyers.place_order(sym, "SELL", qty)
            orders.append({"leg": leg, "symbol": sym, "order_id": r.get("order_id"),
                           "fill_price": r.get("fill_price", 0), "ok": r["ok"]})
            state.emit(f"ORDER: SELL {qty}x {sym} [{leg}] → {r.get('message','')}", "ORDER")

    # Place hedge legs (buy)
    for leg, strike_key in [("buy_ce", "buy_ce"), ("buy_pe", "buy_pe")]:
        strike_val = signal.get(strike_key)
        if strike_val:
            sk = next((s for s in state.strikes if s.strike == strike_val), None)
            sym = (sk.ce_symbol if "ce" in leg else sk.pe_symbol) if sk else ""
            if sym:
                r = await fyers.place_order(sym, "BUY", qty)
                orders.append({"leg": leg, "symbol": sym, "order_id": r.get("order_id"),
                               "fill_price": r.get("fill_price", 0), "ok": r["ok"]})
                state.emit(f"ORDER: BUY {qty}x {sym} [{leg}] → {r.get('message','')}", "ORDER")

    # Extra sell legs for butterfly/condor
    for leg in ["sell_ce2", "sell_pe2"]:
        strike_val = signal.get(leg)
        if strike_val:
            sk = next((s for s in state.strikes if s.strike == strike_val), None)
            sym = (sk.ce_symbol if "ce" in leg else sk.pe_symbol) if sk else ""
            if sym:
                r = await fyers.place_order(sym, "SELL", qty)
                orders.append({"leg": leg, "symbol": sym, "order_id": r.get("order_id"),
                               "fill_price": r.get("fill_price", 0), "ok": r["ok"]})
                state.emit(f"ORDER: SELL {qty}x {sym} [{leg}] → {r.get('message','')}", "ORDER")

    atm = state.atm
    combined = atm.current if atm else signal["combined"]

    state.position = {
        "signal":         signal,
        "entry_combined": combined,
        "entry_time":     datetime.now().isoformat(),
        "orders":         orders,
        "lot_size":       lot_sz,
        "lots":           lots,
    }

    # Save to DB
    db = SessionLocal()
    try:
        trade = Trade(
            user_id=user_id, automation_id=auto_id,
            trade_date=datetime.now().strftime("%Y-%m-%d"),
            symbol=signal.get("symbol", state.config.get("underlying","")),
            strategy_code=signal["code"],
            mode=state.config.get("mode","paper"),
            atm_strike=state.atm_strike,
            sell_ce_strike=signal["sell_ce"],
            sell_pe_strike=signal["sell_pe"],
            buy_ce_strike=signal.get("buy_ce"),
            buy_pe_strike=signal.get("buy_pe"),
            entry_combined=combined,
            net_credit=combined,
            lots=lots, lot_size=lot_sz,
            entry_time=datetime.now(),
            is_open=True,
            signal_data=signal,
            orders=orders,
        )
        db.add(trade)
        db.commit()
        state.position["trade_id"] = trade.id
    finally:
        db.close()

async def _close_position(state: EngineState, fyers: FyersEngine,
                           reason: str, lot_sz: int, lots: int):
    """Place exit orders and calculate P&L."""
    if not state.position:
        return
    qty = lot_sz * lots
    pos = state.position
    atm = state.atm
    exit_combined = atm.current if atm else pos["entry_combined"]

    state.emit(f"CLOSING position. Reason: {reason}", "EXIT")

    # Reverse all legs
    for order in pos.get("orders", []):
        if not order.get("symbol"):
            continue
        side = "BUY" if "sell" in order["leg"] else "SELL"
        r = await fyers.place_order(order["symbol"], side, qty)
        state.emit(f"CLOSE: {side} {qty}x {order['symbol']} → {r.get('message','')}", "ORDER")

    # Calculate P&L
    net_credit = pos["entry_combined"]
    pnl = (net_credit - exit_combined) * lots * lot_sz
    state.day_pnl += pnl
    state.emit(f"P&L: ₹{pnl:.0f} | Day total: ₹{state.day_pnl:.0f}", "OK" if pnl > 0 else "SL")

    # Update DB
    trade_id = pos.get("trade_id")
    if trade_id:
        db = SessionLocal()
        try:
            trade = db.query(Trade).filter(Trade.id == trade_id).first()
            if trade:
                trade.exit_combined = exit_combined
                trade.exit_time     = datetime.now()
                trade.exit_reason   = reason
                trade.gross_pnl     = pnl
                trade.net_pnl       = pnl - trade.brokerage
                trade.is_open       = False
                db.commit()
        finally:
            db.close()

    state.position = None

# ═══════════════════════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════════════════════

async def _send_telegram(bot_token: str, chat_id: str, message: str):
    if not bot_token or not chat_id:
        return
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": f"⬡ ALGO-DESK\n\n{message}",
                      "parse_mode": "HTML"}
            )
    except Exception as e:
        log.error(f"Telegram error: {e}")

def _send_tg_task(user_id: str, db_factory, message: str):
    """Fire-and-forget Telegram alert."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if user and user.telegram_token and user.telegram_chat:
            asyncio.create_task(_send_telegram(
                user.telegram_token, user.telegram_chat, message
            ))
    finally:
        db.close()

@app.post("/api/telegram/test")
async def test_telegram(user: User = Depends(get_current_user)):
    if not user.telegram_token or not user.telegram_chat:
        raise HTTPException(400, "Telegram not configured. Save your Bot Token and Chat ID in profile first.")
    await _send_telegram(
        user.telegram_token, user.telegram_chat,
        f"✅ Test message from ALGO-DESK\n\nHello {user.name}! Telegram alerts are working correctly."
    )
    return {"ok": True, "message": "Test message sent"}

# ═══════════════════════════════════════════════════════════════
# SCHEDULED TOKEN REFRESH (8:50 AM IST daily)
# ═══════════════════════════════════════════════════════════════

async def _daily_token_refresh():
    """Refresh Fyers tokens for all active users at 8:50 AM IST."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    import pytz

    scheduler = AsyncIOScheduler(timezone=pytz.timezone("Asia/Kolkata"))

    async def refresh_all():
        log.info("Daily token refresh starting...")
        db = SessionLocal()
        try:
            connections = db.query(BrokerConnection).filter(
                BrokerConnection.broker_id == "fyers"
            ).all()
            for bc in connections:
                user = db.query(User).filter(User.id == bc.user_id).first()
                if not user or not user.is_active:
                    continue
                eng = FyersEngine(user.id, bc.encrypted_fields or {}, mode=bc.mode)
                ok, msg, enc_token = await eng.authenticate()
                if ok and enc_token:
                    bc.access_token_enc    = enc_token
                    bc.last_token_refresh  = datetime.utcnow()
                    bc.is_connected        = True
                    log.info(f"Token refreshed for {user.email}")
                    if user.telegram_token and user.telegram_chat:
                        await _send_telegram(
                            user.telegram_token, user.telegram_chat,
                            f"✅ Daily token refresh successful\nFyers connected and ready for today's trading."
                        )
                else:
                    log.error(f"Token refresh failed for {user.email}: {msg}")
                    if user.telegram_token and user.telegram_chat:
                        await _send_telegram(
                            user.telegram_token, user.telegram_chat,
                            f"❌ Token refresh failed: {msg}\nCheck your Fyers credentials."
                        )
            db.commit()
        except Exception as e:
            log.error(f"Token refresh error: {e}")
        finally:
            db.close()

    scheduler.add_job(refresh_all, "cron", hour=8, minute=50)
    scheduler.start()
    log.info("Token refresh scheduler started (8:50 AM IST daily)")

@app.on_event("startup")
async def start_scheduler():
    asyncio.create_task(_daily_token_refresh())

# ═══════════════════════════════════════════════════════════════
# SERVE FRONTEND
# ═══════════════════════════════════════════════════════════════

frontend_path = "/app/frontend"
if os.path.exists(frontend_path) and any(
    f.endswith(".html") for f in os.listdir(frontend_path)
):
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")
else:
    @app.get("/")
    def root():
        return JSONResponse({"message": "ALGO-DESK API v4", "version": "4.0.0"})
