"""
ALGO-DESK — Database Models v2
================================
Changes from v1:
  - User: multiple Telegram accounts (JSON list)
  - Automation: shadow_mode flag, telegram_alerts flag, symbol from broker
  - ShadowTrade: separate table for paper simulation results
  - Migrations: all additive, never destructive
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

# ── Users ─────────────────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"
    id             = Column(String, primary_key=True, default=_uuid)
    email          = Column(String(255), unique=True, nullable=False, index=True)
    name           = Column(String(255), nullable=False)
    password_hash  = Column(String(255), nullable=False)
    role           = Column(String(50), default="USER")
    plan           = Column(String(50), default="FREE")
    is_active      = Column(Boolean, default=True)
    is_verified    = Column(Boolean, default=False)
    timezone       = Column(String(50), default="Asia/Kolkata")
    # Single Telegram (legacy)
    telegram_token = Column(String(255), nullable=True)
    telegram_chat  = Column(String(100), nullable=True)
    # Multiple Telegram accounts
    # JSON list: [{"name":"Phone","token":"xxx","chat":"yyy","active":true}, ...]
    telegram_accounts = Column(JSON, default=list)
    created_at     = Column(DateTime, default=datetime.utcnow)
    last_login     = Column(DateTime, nullable=True)

    brokers        = relationship("BrokerConnection", back_populates="user", cascade="all, delete")
    automations    = relationship("Automation", back_populates="user", cascade="all, delete")
    trades         = relationship("Trade", back_populates="user")
    shadow_trades  = relationship("ShadowTrade", back_populates="user")
    reset_tokens   = relationship("ResetToken", back_populates="user", cascade="all, delete")

# ── Password reset ────────────────────────────────────────────────
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
    id         = Column(String, primary_key=True, default=_uuid)
    token      = Column(String(255), unique=True, nullable=False, index=True)
    created_by = Column(String, ForeignKey("users.id"), nullable=False)
    role       = Column(String(50), default="USER")
    plan       = Column(String(50), default="FREE")
    used       = Column(Boolean, default=False)
    used_by    = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)

# ── Broker definitions ────────────────────────────────────────────
class BrokerDefinition(Base):
    __tablename__  = "broker_definitions"
    id             = Column(String, primary_key=True, default=_uuid)
    broker_id      = Column(String(50), unique=True, nullable=False)
    name           = Column(String(100), nullable=False)
    flag           = Column(String(10), default="🏦")
    market         = Column(String(50), default="INDIA")
    refresh_desc   = Column(String(200), default="Auto-managed")
    test_method    = Column(String(20), default="oauth")
    api_base_url   = Column(String(255), nullable=True)
    fields_config  = Column(JSON, nullable=False)
    # Available symbols from this broker
    # JSON list: [{"value":"NSE:NIFTY50-INDEX","label":"NIFTY 50"}, ...]
    symbols        = Column(JSON, default=list)
    is_active      = Column(Boolean, default=True)
    sort_order     = Column(Integer, default=0)
    created_at     = Column(DateTime, default=datetime.utcnow)

# ── Broker connections ────────────────────────────────────────────
class BrokerConnection(Base):
    __tablename__      = "broker_connections"
    id                 = Column(String, primary_key=True, default=_uuid)
    user_id            = Column(String, ForeignKey("users.id"), nullable=False)
    broker_id          = Column(String(50), nullable=False)
    broker_name        = Column(String(100), nullable=False)
    market             = Column(String(50), default="INDIA")
    mode               = Column(String(20), default="paper")
    encrypted_fields   = Column(JSON, default=dict)
    access_token_enc   = Column(Text, nullable=True)
    refresh_token_enc  = Column(Text, nullable=True)
    token_expires_at   = Column(DateTime, nullable=True)
    is_connected       = Column(Boolean, default=False)
    last_tested        = Column(DateTime, nullable=True)
    last_token_refresh = Column(DateTime, nullable=True)
    created_at         = Column(DateTime, default=datetime.utcnow)
    user               = relationship("User", back_populates="brokers")
    __table_args__     = (UniqueConstraint("user_id", "broker_id"),)

# ── Automations ───────────────────────────────────────────────────
class Automation(Base):
    __tablename__   = "automations"
    id              = Column(String, primary_key=True, default=_uuid)
    user_id         = Column(String, ForeignKey("users.id"), nullable=False)
    name            = Column(String(200), nullable=False)
    symbol          = Column(String(100), nullable=False)
    broker_id       = Column(String(50), nullable=False)
    strategies      = Column(JSON, default=list)
    mode            = Column(String(20), default="paper")  # paper | live
    status          = Column(String(20), default="IDLE")   # IDLE | RUNNING
    # Shadow mode: runs paper simulation automatically, stores results
    shadow_mode     = Column(Boolean, default=True)
    # Telegram alerts for paper mode trades
    telegram_alerts = Column(Boolean, default=True)
    config          = Column(JSON, default=dict)
    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    user            = relationship("User", back_populates="automations")
    trades          = relationship("Trade", back_populates="automation")
    shadow_trades   = relationship("ShadowTrade", back_populates="automation")

# ── Live trades ───────────────────────────────────────────────────
class Trade(Base):
    __tablename__   = "trades"
    id              = Column(String, primary_key=True, default=_uuid)
    user_id         = Column(String, ForeignKey("users.id"), nullable=False)
    automation_id   = Column(String, ForeignKey("automations.id"), nullable=True)
    trade_date      = Column(String(10), nullable=False)
    symbol          = Column(String(100), nullable=False)
    strategy_code   = Column(String(50), nullable=False)
    mode            = Column(String(20), nullable=False)
    atm_strike      = Column(Integer, nullable=False)
    sell_ce_strike  = Column(Integer, nullable=False)
    sell_pe_strike  = Column(Integer, nullable=False)
    buy_ce_strike   = Column(Integer, nullable=True)
    buy_pe_strike   = Column(Integer, nullable=True)
    entry_combined  = Column(Float, nullable=False)
    exit_combined   = Column(Float, nullable=True)
    net_credit      = Column(Float, nullable=False)
    lots            = Column(Integer, default=1)
    lot_size        = Column(Integer, default=65)
    entry_time      = Column(DateTime, nullable=False)
    exit_time       = Column(DateTime, nullable=True)
    exit_reason     = Column(String(100), nullable=True)
    gross_pnl       = Column(Float, nullable=True)
    brokerage       = Column(Float, default=40.0)
    net_pnl         = Column(Float, nullable=True)
    is_open         = Column(Boolean, default=True)
    signal_data     = Column(JSON, default=dict)
    orders          = Column(JSON, default=list)
    created_at      = Column(DateTime, default=datetime.utcnow)
    user            = relationship("User", back_populates="trades")
    automation      = relationship("Automation", back_populates="trades")
    __table_args__  = (Index("idx_trades_user_date", "user_id", "trade_date"),)

# ── Shadow trades (paper simulation) ─────────────────────────────
class ShadowTrade(Base):
    """
    Paper simulation trades. Separate from real trades.
    Populated automatically by shadow engine during market hours.
    Used for performance analysis and confidence building.
    """
    __tablename__    = "shadow_trades"
    id               = Column(String, primary_key=True, default=_uuid)
    user_id          = Column(String, ForeignKey("users.id"), nullable=False)
    automation_id    = Column(String, ForeignKey("automations.id"), nullable=True)
    trade_date       = Column(String(10), nullable=False)
    symbol           = Column(String(100), nullable=False)
    strategy_code    = Column(String(50), nullable=False)
    atm_strike       = Column(Integer, nullable=False)
    # Entry
    entry_combined   = Column(Float, nullable=False)
    entry_time       = Column(DateTime, nullable=False)
    entry_spot       = Column(Float, nullable=True)
    # Exit
    exit_combined    = Column(Float, nullable=True)
    exit_time        = Column(DateTime, nullable=True)
    exit_spot        = Column(Float, nullable=True)
    exit_reason      = Column(String(100), nullable=True)
    # P&L
    lots             = Column(Integer, default=1)
    lot_size         = Column(Integer, default=65)
    gross_pnl        = Column(Float, nullable=True)
    net_pnl          = Column(Float, nullable=True)
    is_open          = Column(Boolean, default=True)
    # Signal details
    signal_data      = Column(JSON, default=dict)
    # SL tracking
    sl_tracking      = Column(JSON, default=dict)  # vwap, trailing_low etc at exit
    # Extra fields for industry-standard reporting
    brokerage        = Column(Float, default=0.0)
    hedge_width      = Column(Integer, default=2)
    max_profit       = Column(Float, nullable=True)   # max profit if held to expiry
    max_loss         = Column(Float, nullable=True)   # defined max loss (hedge width × gap × qty)
    # Recovery: track monitoring state for restart
    last_monitored   = Column(DateTime, nullable=True)
    created_at       = Column(DateTime, default=datetime.utcnow)
    user             = relationship("User", back_populates="shadow_trades")
    automation       = relationship("Automation", back_populates="shadow_trades")
    __table_args__   = (Index("idx_shadow_user_date", "user_id", "trade_date"),)

# ── Auto-migrations ───────────────────────────────────────────────
MIGRATIONS = [
    # Broker connections
    "ALTER TABLE broker_connections ADD COLUMN IF NOT EXISTS refresh_token_enc TEXT",
    "ALTER TABLE broker_connections ADD COLUMN IF NOT EXISTS access_token_enc TEXT",
    "ALTER TABLE broker_connections ADD COLUMN IF NOT EXISTS last_token_refresh TIMESTAMP",
    "ALTER TABLE broker_connections ADD COLUMN IF NOT EXISTS token_expires_at TIMESTAMP",
    # Users
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_token VARCHAR(255)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_chat VARCHAR(100)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_accounts JSON DEFAULT '[]'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS timezone VARCHAR(50) DEFAULT 'Asia/Kolkata'",
    # Automations
    "ALTER TABLE automations ADD COLUMN IF NOT EXISTS shadow_mode BOOLEAN DEFAULT TRUE",
    "ALTER TABLE automations ADD COLUMN IF NOT EXISTS telegram_alerts BOOLEAN DEFAULT TRUE",
    # Broker definitions
    "ALTER TABLE broker_definitions ADD COLUMN IF NOT EXISTS symbols JSON DEFAULT '[]'",
    # Shadow trades — extra reporting fields
    "ALTER TABLE shadow_trades ADD COLUMN IF NOT EXISTS brokerage FLOAT DEFAULT 0",
    "ALTER TABLE shadow_trades ADD COLUMN IF NOT EXISTS hedge_width INTEGER DEFAULT 2",
    "ALTER TABLE shadow_trades ADD COLUMN IF NOT EXISTS max_profit FLOAT",
    "ALTER TABLE shadow_trades ADD COLUMN IF NOT EXISTS max_loss FLOAT",
    "ALTER TABLE shadow_trades ADD COLUMN IF NOT EXISTS last_monitored TIMESTAMP",
    "ALTER TABLE shadow_trades ADD COLUMN IF NOT EXISTS exit_spot FLOAT",
    # Fix lot_size defaults
    "ALTER TABLE shadow_trades ALTER COLUMN lot_size SET DEFAULT 65",
    "ALTER TABLE trades ALTER COLUMN lot_size SET DEFAULT 65",
    "ALTER TABLE trades ALTER COLUMN brokerage SET DEFAULT 0",
]

def run_migrations(engine):
    from sqlalchemy import text
    with engine.connect() as conn:
        for sql in MIGRATIONS:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                pass
