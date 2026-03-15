"""
ALGO-DESK — Database Models
All tables. All relationships. Persists across restarts.
"""
import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, Float, Integer, Boolean,
    DateTime, Text, JSON, ForeignKey, UniqueConstraint, Index
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

def _uuid():
    return str(uuid.uuid4())

# ── Users ────────────────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"
    id            = Column(String, primary_key=True, default=_uuid)
    email         = Column(String(255), unique=True, nullable=False, index=True)
    name          = Column(String(255), nullable=False)
    password_hash = Column(String(255), nullable=False)
    role          = Column(String(50), default="USER")       # SUPER_ADMIN, ADMIN, USER
    plan          = Column(String(50), default="FREE")       # FREE, STARTER, PRO, ENTERPRISE
    is_active     = Column(Boolean, default=True)
    is_verified   = Column(Boolean, default=False)
    timezone      = Column(String(50), default="Asia/Kolkata")
    telegram_token= Column(String(255), nullable=True)
    telegram_chat = Column(String(100), nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)
    last_login    = Column(DateTime, nullable=True)

    brokers       = relationship("BrokerConnection", back_populates="user", cascade="all, delete")
    automations   = relationship("Automation", back_populates="user", cascade="all, delete")
    trades        = relationship("Trade", back_populates="user")
    reset_tokens  = relationship("ResetToken", back_populates="user", cascade="all, delete")

# ── Password reset tokens ─────────────────────────────────────────
class ResetToken(Base):
    __tablename__ = "reset_tokens"
    id         = Column(String, primary_key=True, default=_uuid)
    user_id    = Column(String, ForeignKey("users.id"), nullable=False)
    token      = Column(String(255), unique=True, nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)
    used       = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    user       = relationship("User", back_populates="reset_tokens")

# ── Invite links ──────────────────────────────────────────────────
class InviteLink(Base):
    __tablename__ = "invite_links"
    id          = Column(String, primary_key=True, default=_uuid)
    token       = Column(String(255), unique=True, nullable=False, index=True)
    created_by  = Column(String, ForeignKey("users.id"), nullable=False)
    role        = Column(String(50), default="USER")
    plan        = Column(String(50), default="FREE")
    used        = Column(Boolean, default=False)
    used_by     = Column(String, nullable=True)
    created_at  = Column(DateTime, default=datetime.utcnow)
    expires_at  = Column(DateTime, nullable=True)

# ── Broker definitions (admin-managed, no hardcoding) ─────────────
class BrokerDefinition(Base):
    """
    Admin adds/edits broker types from the UI.
    fields_config is JSON list of field definitions.
    test_method: 'api_key' | 'totp' | 'oauth' | 'session'
    """
    __tablename__ = "broker_definitions"
    id            = Column(String, primary_key=True, default=_uuid)
    broker_id     = Column(String(50), unique=True, nullable=False)  # "fyers"
    name          = Column(String(100), nullable=False)              # "Fyers"
    flag          = Column(String(10), default="🏦")
    market        = Column(String(50), default="INDIA")
    refresh_desc  = Column(String(200), default="Auto-managed")
    test_method   = Column(String(20), default="totp")
    api_base_url  = Column(String(255), nullable=True)
    fields_config = Column(JSON, nullable=False)  # list of field defs
    is_active     = Column(Boolean, default=True)
    sort_order    = Column(Integer, default=0)
    created_at    = Column(DateTime, default=datetime.utcnow)

# ── Broker connections (per user) ─────────────────────────────────
class BrokerConnection(Base):
    __tablename__ = "broker_connections"
    id              = Column(String, primary_key=True, default=_uuid)
    user_id         = Column(String, ForeignKey("users.id"), nullable=False)
    broker_id       = Column(String(50), nullable=False)       # "fyers"
    broker_name     = Column(String(100), nullable=False)
    market          = Column(String(50), default="INDIA")
    mode            = Column(String(20), default="paper")      # paper | live
    encrypted_fields= Column(JSON, default=dict)               # encrypted creds
    access_token_enc= Column(Text, nullable=True)              # encrypted token
    token_expires_at= Column(DateTime, nullable=True)
    is_connected    = Column(Boolean, default=False)
    last_tested     = Column(DateTime, nullable=True)
    last_token_refresh = Column(DateTime, nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow)
    user            = relationship("User", back_populates="brokers")
    __table_args__  = (UniqueConstraint("user_id", "broker_id"),)

# ── Automations ───────────────────────────────────────────────────
class Automation(Base):
    __tablename__ = "automations"
    id             = Column(String, primary_key=True, default=_uuid)
    user_id        = Column(String, ForeignKey("users.id"), nullable=False)
    name           = Column(String(200), nullable=False)
    symbol         = Column(String(100), nullable=False)
    broker_id      = Column(String(50), nullable=False)
    strategies     = Column(JSON, default=list)
    mode           = Column(String(20), default="paper")
    status         = Column(String(20), default="IDLE")        # IDLE | RUNNING | IN_TRADE
    config         = Column(JSON, default=dict)
    created_at     = Column(DateTime, default=datetime.utcnow)
    updated_at     = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    user           = relationship("User", back_populates="automations")
    trades         = relationship("Trade", back_populates="automation")

# ── Trades ────────────────────────────────────────────────────────
class Trade(Base):
    __tablename__ = "trades"
    id             = Column(String, primary_key=True, default=_uuid)
    user_id        = Column(String, ForeignKey("users.id"), nullable=False)
    automation_id  = Column(String, ForeignKey("automations.id"), nullable=True)
    trade_date     = Column(String(10), nullable=False)
    symbol         = Column(String(100), nullable=False)
    strategy_code  = Column(String(50), nullable=False)
    mode           = Column(String(20), nullable=False)
    atm_strike     = Column(Integer, nullable=False)
    sell_ce_strike = Column(Integer, nullable=False)
    sell_pe_strike = Column(Integer, nullable=False)
    buy_ce_strike  = Column(Integer, nullable=True)
    buy_pe_strike  = Column(Integer, nullable=True)
    entry_combined = Column(Float, nullable=False)
    exit_combined  = Column(Float, nullable=True)
    net_credit     = Column(Float, nullable=False)
    lots           = Column(Integer, default=1)
    lot_size       = Column(Integer, default=25)
    entry_time     = Column(DateTime, nullable=False)
    exit_time      = Column(DateTime, nullable=True)
    exit_reason    = Column(String(100), nullable=True)
    gross_pnl      = Column(Float, nullable=True)
    brokerage      = Column(Float, default=40.0)
    net_pnl        = Column(Float, nullable=True)
    is_open        = Column(Boolean, default=True)
    signal_data    = Column(JSON, default=dict)
    orders         = Column(JSON, default=list)    # order IDs from broker
    created_at     = Column(DateTime, default=datetime.utcnow)
    user           = relationship("User", back_populates="trades")
    automation     = relationship("Automation", back_populates="trades")
    __table_args__ = (Index("idx_trades_user_date", "user_id", "trade_date"),)
