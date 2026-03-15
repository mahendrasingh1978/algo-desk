"""
ALGO-DESK v3 — Main Backend
"""
import os
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from jose import JWTError, jwt
from passlib.context import CryptContext
from datetime import datetime, timedelta
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="ALGO-DESK", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

pwd_ctx   = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer    = HTTPBearer()
SECRET    = os.environ.get("SECRET_KEY", "changeme-please-set-in-env")
ALGO      = "HS256"

# In-memory user store — replace with DB in production
USERS = {}

def make_token(email: str) -> str:
    exp = datetime.utcnow() + timedelta(hours=12)
    return jwt.encode({"sub": email, "exp": exp}, SECRET, algorithm=ALGO)

def verify_token(creds: HTTPAuthorizationCredentials = Depends(bearer)):
    try:
        payload = jwt.decode(creds.credentials, SECRET, algorithms=[ALGO])
        return payload["sub"]
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")


# ── Health ────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "version": "3.0.0", "time": datetime.now().isoformat()}


# ── Bootstrap (called once by installer to create admin) ──────────
class BootstrapReq(BaseModel):
    email: str
    password: str
    name: str
    role: str = "SUPER_ADMIN"

@app.post("/api/auth/bootstrap")
def bootstrap(req: BootstrapReq, x_bootstrap_key: str = None):
    # Allow bootstrap if no users exist yet
    if USERS:
        raise HTTPException(400, "Already bootstrapped")
    USERS[req.email] = {
        "name": req.name,
        "email": req.email,
        "password_hash": pwd_ctx.hash(req.password),
        "role": req.role,
    }
    return {"ok": True, "message": "Admin account created"}


# ── Auth ──────────────────────────────────────────────────────────
class LoginReq(BaseModel):
    email: str
    password: str

@app.post("/api/auth/login")
def login(req: LoginReq):
    # Check env admin first
    env_email = os.environ.get("SUPER_ADMIN_EMAIL", "")
    env_pass  = os.environ.get("SUPER_ADMIN_PASSWORD", "")
    if req.email == env_email and req.password == env_pass:
        return {
            "token": make_token(req.email),
            "name": os.environ.get("SUPER_ADMIN_NAME", "Admin"),
            "role": "SUPER_ADMIN",
        }
    # Check in-memory users
    user = USERS.get(req.email)
    if user and pwd_ctx.verify(req.password, user["password_hash"]):
        return {"token": make_token(req.email), "name": user["name"], "role": user["role"]}
    raise HTTPException(401, "Invalid email or password")


# ── User profile ──────────────────────────────────────────────────
@app.get("/api/me")
def me(email: str = Depends(verify_token)):
    env_email = os.environ.get("SUPER_ADMIN_EMAIL", "")
    if email == env_email:
        return {
            "email": email,
            "name": os.environ.get("SUPER_ADMIN_NAME", "Admin"),
            "role": "SUPER_ADMIN",
            "plan": "PRO",
        }
    user = USERS.get(email, {})
    return {"email": email, "name": user.get("name",""), "role": user.get("role","USER"), "plan": "PRO"}


# ── Engine status placeholder ─────────────────────────────────────
@app.get("/api/engine/status")
def engine_status(email: str = Depends(verify_token)):
    return {"mode": "IDLE", "engine_running": False}


# ── Strategies ────────────────────────────────────────────────────
@app.get("/api/strategies")
def get_strategies(email: str = Depends(verify_token)):
    return {"strategies": [
        {"code": "S7", "name": "All-Strike Iron Butterfly", "enabled": True},
        {"code": "S1", "name": "ORB Breakdown Sell",        "enabled": True},
        {"code": "S2", "name": "VWAP Squeeze + EMA Cross",  "enabled": True},
        {"code": "S8", "name": "Opening Gap Fade",           "enabled": True},
        {"code": "S3", "name": "Breakout Reversal",          "enabled": True},
        {"code": "S4", "name": "Iron Condor",                "enabled": True},
        {"code": "S5", "name": "Ratio Spread",               "enabled": False},
        {"code": "S6", "name": "Theta Decay Strangle",       "enabled": True},
        {"code": "S9", "name": "Pre-Expiry Theta Crush",     "enabled": True},
    ]}


# ── Serve frontend ────────────────────────────────────────────────
frontend_path = "/app/frontend"
if os.path.exists(frontend_path):
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")
