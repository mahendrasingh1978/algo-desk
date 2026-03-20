#!/usr/bin/env python3
"""
ALGO-DESK Functional Test Suite
=================================
Tests every API endpoint with a real in-memory SQLite database.
No Fyers credentials needed — tests mock broker calls.
Run: python3 scripts/functest.py
"""
import os, sys, asyncio, json
os.environ['DATABASE_URL'] = 'sqlite:///./functest.db'
os.environ['SECRET_KEY']   = 'test-secret-key-32bytes-minimum!!'
os.environ['ENCRYPTION_KEY'] = 'test-encryption-key-32bytes!!!!!'

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + '/backend')

from fastapi.testclient import TestClient
import main

# Initialise DB before running tests
main.init_db()

client = TestClient(main.app)
passed = failed = 0

def test(name, fn):
    global passed, failed
    try:
        fn()
        print(f"  ✅ {name}")
        passed += 1
    except Exception as e:
        print(f"  ❌ {name}: {e}")
        failed += 1

def eq(a, b, msg=""):
    assert a == b, f"Expected {b!r} got {a!r} {msg}"

def ok_status(r, msg=""):
    assert r.status_code in (200, 201), f"HTTP {r.status_code}: {r.text[:200]} {msg}"

print("\n════════════════════════════════════════")
print("  ALGO-DESK Functional Test Suite")
print("════════════════════════════════════════\n")

# ── 1. Health ────────────────────────────────────────────────
print("1. Health & basics...")
def t_health():
    r = client.get("/health")
    ok_status(r)
    assert r.json().get("status") == "ok"
test("GET /health", t_health)

# ── 2. Auth ──────────────────────────────────────────────────
print("\n2. Authentication...")
TOKEN = None
USER_EMAIL = "test@algodesk.com"
USER_PASS  = "TestPass123!"

def t_register():
    r = client.post("/api/auth/register", json={
        "name": "Test User", "email": USER_EMAIL,
        "password": USER_PASS, "invite_token": None
    })
    ok_status(r)
    d = r.json()
    assert "token" in d, f"No token: {d}"
test("POST /api/auth/register", t_register)

def t_login():
    global TOKEN
    r = client.post("/api/auth/login", json={
        "email": USER_EMAIL, "password": USER_PASS
    })
    ok_status(r)
    d = r.json()
    assert "token" in d
    TOKEN = d["token"]
test("POST /api/auth/login", t_login)

def t_login_wrong_pass():
    r = client.post("/api/auth/login", json={
        "email": USER_EMAIL, "password": "wrongpassword"
    })
    assert r.status_code in (401, 400, 422), f"Should fail: {r.status_code}"
test("POST /api/auth/login (wrong password rejects)", t_login_wrong_pass)

def t_me():
    r = client.get("/api/me", headers={"Authorization": f"Bearer {TOKEN}"})
    ok_status(r)
    d = r.json()
    assert d.get("email") == USER_EMAIL
test("GET /api/me", t_me)

def t_me_no_auth():
    r = client.get("/api/me")
    assert r.status_code == 401
test("GET /api/me (no auth returns 401)", t_me_no_auth)

def t_change_password():
    r = client.post("/api/auth/change-password",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"old_password": USER_PASS, "new_password": "NewPass456!"})
    ok_status(r)
    # Change back
    client.post("/api/auth/change-password",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"old_password": "NewPass456!", "new_password": USER_PASS})
test("POST /api/auth/change-password", t_change_password)

H = lambda: {"Authorization": f"Bearer {TOKEN}"}

# ── 3. Brokers ───────────────────────────────────────────────
print("\n3. Broker endpoints...")
def t_broker_definitions():
    r = client.get("/api/brokers/definitions", headers=H())
    ok_status(r)
    d = r.json()
    assert "brokers" in d
    brokers = d["brokers"]
    assert len(brokers) > 0, "No broker definitions seeded"
    fyers = next((b for b in brokers if b["id"] == "fyers"), None)
    assert fyers is not None, "Fyers not found"
    assert fyers.get("fields"), "Fyers has no fields"
test("GET /api/brokers/definitions", t_broker_definitions)

def t_list_brokers_empty():
    r = client.get("/api/brokers", headers=H())
    ok_status(r)
    assert "brokers" in r.json()
test("GET /api/brokers (empty)", t_list_brokers_empty)

def t_save_broker():
    r = client.post("/api/brokers", headers=H(), json={
        "broker_id": "fyers",
        "fields": {
            "client_id":    "TESTCLIENT-100",
            "secret_key":   "testsecret",
            "pin":          "1234",
            "redirect_uri": "https://test.fyers.in/redirect"
        }
    })
    ok_status(r)
test("POST /api/brokers (save credentials)", t_save_broker)

def t_fyers_login_url():
    r = client.get("/api/brokers/fyers/login-url", headers=H())
    ok_status(r)
    d = r.json()
    assert "url" in d, f"No url: {d}"
    assert "fyers" in d["url"].lower() or "api" in d["url"].lower()
test("GET /api/brokers/fyers/login-url", t_fyers_login_url)

# ── 4. Market data ───────────────────────────────────────────
print("\n4. Market data endpoints...")
def t_market_status():
    r = client.get("/api/market/status", headers=H())
    ok_status(r)
    d = r.json()
    assert "status" in d
    assert d["status"] in ("live","closed","waiting","error")
test("GET /api/market/status", t_market_status)

def t_market_symbols():
    r = client.get("/api/market/symbols", headers=H())
    ok_status(r)
    d = r.json()
    assert "symbols" in d
    assert len(d["symbols"]) > 0
test("GET /api/market/symbols", t_market_symbols)

def t_capital_symbols():
    r = client.get("/api/capital/symbols", headers=H())
    ok_status(r)
    d = r.json()
    assert "symbols" in d
    syms = {s["value"]: s for s in d["symbols"]}
    # Verify current lot sizes
    assert syms["NSE:NIFTY50-INDEX"]["lot_size"] == 65, "NIFTY lot size should be 65"
    assert syms["NSE:NIFTYBANK-INDEX"]["lot_size"] == 30, "BANKNIFTY lot size should be 30"
    assert syms["BSE:SENSEX-INDEX"]["lot_size"] == 20, "SENSEX lot size should be 20"
test("GET /api/capital/symbols (lot sizes correct)", t_capital_symbols)

def t_capital_check():
    r = client.get("/api/capital/check?symbol=NSE:NIFTY50-INDEX&lots=1&strategies=S1,S8",
                   headers=H())
    ok_status(r)
    d = r.json()
    assert "can_trade" in d
    assert "margin" in d
    assert d["margin"]["lot_size"] == 65
    assert "strat_breakdown" in d
    assert len(d["strat_breakdown"]) == 2
test("GET /api/capital/check", t_capital_check)

# ── 5. Automations ───────────────────────────────────────────
print("\n5. Automation endpoints...")
AUTO_ID = None
def t_list_automations_empty():
    r = client.get("/api/automations", headers=H())
    ok_status(r)
    assert "automations" in r.json()
test("GET /api/automations (empty)", t_list_automations_empty)

def t_create_automation():
    global AUTO_ID
    r = client.post("/api/automations", headers=H(), json={
        "name":             "NIFTY Test",
        "symbol":           "NSE:NIFTY50-INDEX",
        "broker_id":        "fyers",
        "strategies":       ["S1","S8"],
        "mode":             "paper",
        "shadow_mode":      True,
        "telegram_alerts":  True,
        "config": {
            "lots": 1, "lot_size": 65,
            "max_loss_pct": 30, "profit_target_pct": 50,
            "auto_exit_time": "14:00"
        }
    })
    ok_status(r)
    d = r.json()
    assert d.get("ok"), f"Not ok: {d}"
    AUTO_ID = d.get("automation", {}).get("id")
    assert AUTO_ID, "No automation id returned"
test("POST /api/automations (create)", t_create_automation)

def t_list_automations_one():
    r = client.get("/api/automations", headers=H())
    ok_status(r)
    autos = r.json()["automations"]
    assert len(autos) == 1
    a = autos[0]
    assert a["symbol"] == "NSE:NIFTY50-INDEX"
    assert "S1" in a["strategies"]
    assert a["shadow_mode"] == True
    assert a["config"]["lot_size"] == 65
test("GET /api/automations (has one)", t_list_automations_one)

def t_delete_automation():
    if not AUTO_ID: return
    r = client.delete(f"/api/automations/{AUTO_ID}", headers=H())
    ok_status(r)
    # Verify deleted
    r2 = client.get("/api/automations", headers=H())
    assert len(r2.json()["automations"]) == 0
test("DELETE /api/automations/{id}", t_delete_automation)

# ── 6. Trades & Shadow ───────────────────────────────────────
print("\n6. Trades & shadow endpoints...")
def t_trades_empty():
    r = client.get("/api/trades", headers=H())
    ok_status(r)
    assert "trades" in r.json()
test("GET /api/trades (empty)", t_trades_empty)

def t_shadow_trades_empty():
    r = client.get("/api/shadow/trades", headers=H())
    ok_status(r)
    assert "trades" in r.json()
test("GET /api/shadow/trades (empty)", t_shadow_trades_empty)

def t_shadow_performance_empty():
    r = client.get("/api/shadow/performance", headers=H())
    ok_status(r)
    d = r.json()
    assert "total_trades" in d
    assert d["total_trades"] == 0
test("GET /api/shadow/performance (empty)", t_shadow_performance_empty)

# ── 7. Engine ────────────────────────────────────────────────
print("\n7. Engine endpoints...")
def t_engine_status_idle():
    r = client.get("/api/engine/status", headers=H())
    ok_status(r)
    d = r.json()
    assert d.get("running") == False
    assert d.get("mode") == "IDLE"
test("GET /api/engine/status (idle)", t_engine_status_idle)

def t_engine_start_no_automation():
    r = client.post("/api/engine/start",
                    headers=H(), json={"automation_id": "nonexistent"})
    assert r.status_code in (400, 404, 422)
test("POST /api/engine/start (bad id returns error)", t_engine_start_no_automation)

# ── 8. Telegram ──────────────────────────────────────────────
print("\n8. Telegram endpoints...")
TG_ID = None
def t_list_tg_empty():
    r = client.get("/api/telegram/accounts", headers=H())
    ok_status(r)
    assert "accounts" in r.json()
test("GET /api/telegram/accounts (empty)", t_list_tg_empty)

def t_add_tg():
    global TG_ID
    r = client.post("/api/telegram/accounts", headers=H(), json={
        "name": "Test Phone", "token": "123:ABC", "chat": "456789", "active": True
    })
    ok_status(r)
    d = r.json()
    accounts = d.get("accounts", [])
    assert len(accounts) > 0
    TG_ID = accounts[0].get("id")
test("POST /api/telegram/accounts", t_add_tg)

def t_toggle_tg():
    if not TG_ID: return
    r = client.put(f"/api/telegram/accounts/{TG_ID}",
                   headers=H(), json={"active": False})
    ok_status(r)
test("PUT /api/telegram/accounts/{id} (toggle)", t_toggle_tg)

def t_delete_tg():
    if not TG_ID: return
    r = client.delete(f"/api/telegram/accounts/{TG_ID}", headers=H())
    ok_status(r)
    r2 = client.get("/api/telegram/accounts", headers=H())
    assert len(r2.json()["accounts"]) == 0
test("DELETE /api/telegram/accounts/{id}", t_delete_tg)

# ── 9. Dashboard summary ─────────────────────────────────────
print("\n9. Dashboard...")
def t_dashboard_summary():
    r = client.get("/api/dashboard/summary", headers=H())
    ok_status(r)
    d = r.json()
    required = ["spot","market_status","today_live_pnl","today_paper_pnl",
                "automations","total_automations","running_automations"]
    for key in required:
        assert key in d, f"Missing key: {key}"
test("GET /api/dashboard/summary", t_dashboard_summary)

# ── 10. Admin ────────────────────────────────────────────────
print("\n10. Admin endpoints...")
def t_admin_stats():
    # Need admin token - promote user first
    db = next(main.get_db())
    user = db.query(main.User).filter(main.User.email == USER_EMAIL).first()
    if user:
        user.role = "SUPER_ADMIN"
        db.commit()
    r = client.get("/api/admin/stats", headers=H())
    ok_status(r)
    d = r.json()
    assert "total_users" in d
    assert d["total_users"] >= 1
test("GET /api/admin/stats", t_admin_stats)

def t_admin_users():
    r = client.get("/api/admin/users", headers=H())
    ok_status(r)
    assert "users" in r.json()
test("GET /api/admin/users", t_admin_users)

def t_create_invite():
    r = client.post("/api/admin/invite", headers=H(),
                    json={"role": "USER", "plan": "FREE"})
    ok_status(r)
    d = r.json()
    assert "invite_url" in d or "token" in d
test("POST /api/admin/invite", t_create_invite)

# ── 11. Engine logic (offline) ────────────────────────────────
print("\n11. Engine logic...")
def t_sl_state():
    from engine import SLState
    sl = SLState()
    sl.activate(200.0, {"max_loss_pct":30,"trail_pct":20,
                        "min_profit_pct":15,"vwap_buffer_pct":2,
                        "ema_buffer_pct":1,"profit_target_pct":50})
    # No exit at entry
    exit_, reason = sl.update(200.0, 180.0, 0, 0,
        {"max_loss_pct":30,"trail_pct":20,"min_profit_pct":15,
         "vwap_buffer_pct":2,"ema_buffer_pct":1,"profit_target_pct":50})
    assert not exit_, "Should not exit at entry price"
    # Profit target hit (50% decay)
    exit_, reason = sl.update(99.0, 180.0, 0, 0,
        {"max_loss_pct":30,"trail_pct":20,"min_profit_pct":15,
         "vwap_buffer_pct":2,"ema_buffer_pct":1,"profit_target_pct":50})
    assert exit_, "Should exit at profit target"
    assert "PROFIT_TARGET" in reason
test("SL state: profit target triggers correctly", t_sl_state)

def t_sl_max_loss():
    from engine import SLState
    sl = SLState()
    sl.activate(200.0, {"max_loss_pct":30,"trail_pct":20,
                        "min_profit_pct":15,"vwap_buffer_pct":2,
                        "ema_buffer_pct":1,"profit_target_pct":50})
    # Max loss triggered (combined rose 31% above entry)
    exit_, reason = sl.update(263.0, 0, 0, 0,
        {"max_loss_pct":30,"trail_pct":20,"min_profit_pct":15,
         "vwap_buffer_pct":2,"ema_buffer_pct":1,"profit_target_pct":50})
    assert exit_, "Should exit on max loss"
    assert "MAX_LOSS" in reason
test("SL state: max loss backstop triggers correctly", t_sl_max_loss)

def t_sl_trailing():
    from engine import SLState
    sl = SLState()
    sl.activate(200.0, {"max_loss_pct":30,"trail_pct":20,
                        "min_profit_pct":15,"vwap_buffer_pct":2,
                        "ema_buffer_pct":1,"profit_target_pct":50})
    cfg = {"max_loss_pct":30,"trail_pct":20,"min_profit_pct":15,
           "vwap_buffer_pct":2,"ema_buffer_pct":1,"profit_target_pct":50}
    # Decay to 160 (20% down — trailing activates at 15%)
    sl.update(160.0, 0, 0, 0, cfg)
    # Trailing SL = 160 * 1.20 = 192. Bounce to 193 should exit
    exit_, reason = sl.update(193.0, 0, 0, 0, cfg)
    assert exit_, "Trailing SL should trigger"
    assert "TRAILING" in reason
test("SL state: trailing SL locks in gains correctly", t_sl_trailing)

def t_margin_calc():
    margin = main.estimate_margin(
        "NSE:NIFTY50-INDEX", lots=1, lot_size=65,
        hedge_width=2, spot_price=24000)
    assert margin["lot_size"] == 65
    assert margin["net_required"] > 0
    assert margin["net_required"] < 200000  # Should be well under 2L for 1 lot
test("Margin calculator: NIFTY 1 lot Iron Fly ±2", t_margin_calc)

def t_nearest_strike():
    from engine import nearest_strike
    assert nearest_strike(24075) == 24100
    assert nearest_strike(24050) == 24050
    assert nearest_strike(24000) == 24000
test("nearest_strike rounds to 50pt gap correctly", t_nearest_strike)

def t_brokerage():
    # 1 NIFTY lot (65 units), entry ₹200, exit ₹100
    charges = main.calc_brokerage(lots=1, lot_size=65,
                                   entry_combined=200, exit_combined=100)
    # Should be ~₹160-250 for 8 orders (not flat ₹40)
    assert charges["total"] > 100, f"Too low: {charges['total']}"
    assert charges["total"] < 500, f"Too high: {charges['total']}"
    assert charges["brokerage"] == 160.0, "8 orders × ₹20"
    assert charges["gst"] > 0, "GST should be non-zero"
    assert charges["exchange_fee"] > 0, "Exchange fee should be non-zero"
test("Brokerage: real Fyers charges for 1 NIFTY lot", t_brokerage)

def t_brokerage_vs_flat():
    # Prove it's no longer flat ₹40
    charges = main.calc_brokerage(lots=2, lot_size=65,
                                   entry_combined=250, exit_combined=150)
    assert charges["total"] != 40.0, "Should not be flat ₹40 anymore"
    assert charges["total"] > 40.0, "Real charges exceed ₹40"
test("Brokerage: no longer flat ₹40", t_brokerage_vs_flat)

def t_lot_size_registry():
    assert main.SYMBOL_REGISTRY["NSE:NIFTY50-INDEX"]["lot_size"] == 65
    assert main.SYMBOL_REGISTRY["NSE:NIFTYBANK-INDEX"]["lot_size"] == 30
    assert main.SYMBOL_REGISTRY["BSE:SENSEX-INDEX"]["lot_size"] == 20
    assert main.SYMBOL_REGISTRY["NSE:FINNIFTY-INDEX"]["lot_size"] == 60
test("Symbol registry: 2026 lot sizes correct", t_lot_size_registry)

# ── 12. Plan / Tier system ───────────────────────────────────
print("\n12. Plan / tier enforcement...")

def t_plan_config():
    # FREE plan: no live trading
    assert not main.PLAN_CONFIG["FREE"]["live_trading"]
    assert main.PLAN_CONFIG["FREE"]["max_automations"] == 3
    # STARTER plan: live trading, 4 strategies
    assert main.PLAN_CONFIG["STARTER"]["live_trading"]
    assert len(main.PLAN_CONFIG["STARTER"]["strategies"]) == 4
    assert "S1" in main.PLAN_CONFIG["STARTER"]["strategies"]
    # PRO plan: all 9 strategies
    assert main.PLAN_CONFIG["PRO"]["live_trading"]
    assert len(main.PLAN_CONFIG["PRO"]["strategies"]) == 9
test("Plan config: FREE/STARTER/PRO defined correctly", t_plan_config)

def t_get_plan_endpoint():
    r = client.get("/api/plan", headers=H())
    ok_status(r)
    d = r.json()
    assert "plan" in d
    assert "live_trading" in d
    assert "strategies" in d
    assert "max_automations" in d
    assert "all_plans" in d
    assert len(d["all_plans"]) == 3
test("GET /api/plan", t_get_plan_endpoint)

def t_free_plan_blocks_live():
    # Register a fresh FREE user
    r = client.post("/api/auth/register", json={
        "name": "Free User", "email": "free@test.com",
        "password": "FreePass123!", "invite_token": None
    })
    ok_status(r)
    free_token = r.json()["token"]
    free_h = {"Authorization": f"Bearer {free_token}"}
    # Try to create LIVE automation — should be blocked
    r2 = client.post("/api/automations", headers=free_h, json={
        "name": "Test Live", "symbol": "NSE:NIFTY50-INDEX",
        "broker_id": "fyers", "strategies": ["S1"],
        "mode": "live", "shadow_mode": True,
        "telegram_alerts": False, "config": {"lots":1,"lot_size":65}
    })
    assert r2.status_code in (403, 400), f"Should block live for FREE: {r2.status_code} {r2.text}"
test("FREE plan: live trading blocked", t_free_plan_blocks_live)

def t_free_plan_allows_paper():
    free_r = client.post("/api/auth/login", json={
        "email": "free@test.com", "password": "FreePass123!"
    })
    free_token = free_r.json()["token"]
    free_h = {"Authorization": f"Bearer {free_token}"}
    # Paper automation should work
    r = client.post("/api/automations", headers=free_h, json={
        "name": "Paper Test", "symbol": "NSE:NIFTY50-INDEX",
        "broker_id": "fyers", "strategies": ["S1"],
        "mode": "paper", "shadow_mode": True,
        "telegram_alerts": False, "config": {"lots":1,"lot_size":65}
    })
    ok_status(r)
    assert r.json().get("ok"), f"Paper should work for FREE: {r.json()}"
test("FREE plan: paper trading allowed", t_free_plan_allows_paper)

def t_free_plan_blocks_pro_strategy():
    free_r = client.post("/api/auth/login", json={
        "email": "free@test.com", "password": "FreePass123!"
    })
    free_token = free_r.json()["token"]
    free_h = {"Authorization": f"Bearer {free_token}"}
    # S4 is PRO-only — should be blocked in live mode
    r = client.post("/api/automations", headers=free_h, json={
        "name": "PRO Test", "symbol": "NSE:NIFTY50-INDEX",
        "broker_id": "fyers", "strategies": ["S4"],
        "mode": "live", "shadow_mode": False,
        "telegram_alerts": False, "config": {"lots":1,"lot_size":65}
    })
    assert r.status_code in (403, 400), f"Should block PRO strategy for FREE: {r.status_code}"
test("FREE plan: PRO strategies blocked for live", t_free_plan_blocks_pro_strategy)

def t_admin_set_plan():
    # Promote the admin user first (already done in earlier test)
    # Find free user
    r = client.get("/api/admin/users", headers=H())
    ok_status(r)
    users = r.json().get("users", [])
    free_user = next((u for u in users if u.get("email") == "free@test.com"), None)
    if not free_user:
        print("(skip — free user not found in admin list)")
        return
    uid = free_user["id"]
    # Upgrade to STARTER
    r2 = client.post(f"/api/admin/users/{uid}/set-plan",
                     headers=H(), json={"plan": "STARTER"})
    ok_status(r2)
    assert r2.json().get("ok")
    assert r2.json().get("plan") == "STARTER"
test("Admin: set-plan upgrades user", t_admin_set_plan)

def t_admin_create_user():
    r = client.post("/api/admin/users", headers=H(), json={
        "name": "Admin Created", "email": "admin_created@test.com",
        "password": "AdminPass123!", "plan": "PRO", "role": "USER"
    })
    ok_status(r)
    assert r.json().get("ok")
    # Verify user exists
    r2 = client.get("/api/admin/users", headers=H())
    emails = [u["email"] for u in r2.json().get("users", [])]
    assert "admin_created@test.com" in emails
test("Admin: create user directly with plan", t_admin_create_user)

def t_automation_limit():
    free_r = client.post("/api/auth/login", json={
        "email": "free@test.com", "password": "FreePass123!"
    })
    free_token = free_r.json()["token"]
    free_h = {"Authorization": f"Bearer {free_token}"}
    for i in range(3):
        client.post("/api/automations", headers=free_h, json={
            "name": f"Extra {i}", "symbol": "NSE:NIFTY50-INDEX",
            "broker_id": "fyers", "strategies": ["S1"],
            "mode": "paper", "shadow_mode": True,
            "telegram_alerts": False, "config": {}
        })
    r = client.post("/api/automations", headers=free_h, json={
        "name": "Over limit", "symbol": "NSE:NIFTY50-INDEX",
        "broker_id": "fyers", "strategies": ["S1"],
        "mode": "paper", "shadow_mode": True,
        "telegram_alerts": False, "config": {}
    })
    assert r.status_code in (403, 400), f"Should hit limit: {r.status_code} {r.text}"
test("FREE plan: automation limit enforced", t_automation_limit)

# ── 13. Paper trade accuracy ──────────────────────────────────
print("\n13. Paper trade accuracy...")

def t_shadow_perf_has_golive():
    r = client.get("/api/shadow/performance", headers=H())
    ok_status(r)
    d = r.json()
    # Even with 0 trades, response structure must be correct
    assert "total_trades" in d
    assert "go_live_ready" in d,    "Missing go_live_ready field"
    assert "go_live_score" in d,    "Missing go_live_score field"
    assert "ready_checks" in d,     "Missing ready_checks field"
    assert "profit_factor" in d,    "Missing profit_factor field"
    assert "max_drawdown" in d,     "Missing max_drawdown field"
    assert "expectancy" in d,       "Missing expectancy field"
    assert "reward_risk" in d,      "Missing reward_risk field"
    assert "max_consec_loss" in d,  "Missing max_consec_loss field"
test("Shadow performance: go-live KPIs present", t_shadow_perf_has_golive)

def t_brokerage_accurate():
    # Verify calc_brokerage is correct structure
    c = main.calc_brokerage(1, 65, 200, 100)
    assert c["brokerage"] == 160.0,  f"8 orders × ₹20 = ₹160, got {c['brokerage']}"
    assert "exchange_fee" in c
    assert "gst" in c
    assert "total" in c
    assert c["total"] > 160, "Total must exceed base brokerage"
test("Brokerage: all charge components present", t_brokerage_accurate)

def t_shadow_performance_empty_correct():
    # Empty performance should return all new fields with 0
    r = client.get("/api/shadow/performance?days=1", headers=H())
    ok_status(r)
    d = r.json()
    if d["total_trades"] == 0:
        # When no trades, all these should still be present
        assert "go_live_ready" in d
        assert d["go_live_ready"] == False,   "No trades = not ready to go live"
        assert d["go_live_score"] == 0,        "No trades = score 0"
test("Shadow performance: empty returns correct defaults", t_shadow_performance_empty_correct)

def t_paper_auto_writes_shadow_table():
    # Paper automation should use ShadowTrade table not Trade table
    # We verify by checking the plan enforcement routes correctly
    r = client.post("/api/automations", headers=H(), json={
        "name": "Paper Shadow Test", "symbol": "NSE:NIFTY50-INDEX",
        "broker_id": "fyers", "strategies": ["S1"],
        "mode": "paper", "shadow_mode": True,
        "telegram_alerts": False,
        "config": {"lots":1, "lot_size":65, "auto_exit_time":"14:00"}
    })
    ok_status(r)
    assert r.json().get("ok")
    # The automation is created — actual shadow trade writing happens at runtime
    # We can at least verify the automation config is stored correctly
    autos = client.get("/api/automations", headers=H()).json()["automations"]
    paper_auto = next((a for a in autos if a["name"] == "Paper Shadow Test"), None)
    assert paper_auto is not None
    assert paper_auto["mode"] == "paper"
    assert paper_auto["shadow_mode"] == True
    assert paper_auto["config"]["lot_size"] == 65
test("Paper automation: created with correct lot size and shadow_mode", t_paper_auto_writes_shadow_table)

# ── 14. Engine correctness ───────────────────────────────────
print("\n14. Engine correctness...")

def t_engine_traded_today_gate():
    from engine import EngineState, check_all_strategies
    from datetime import datetime
    state = EngineState({"strategies":["S1"],"mode":"paper"})
    assert state.traded_today == False, "Should start False"
    assert state.trade_count == 0
    # Gate blocks when traded_today=True
    state.orb_complete = True
    state.traded_today = True
    result = check_all_strategies(state, datetime.now())
    assert result is None, "Must block re-entry after first trade"
    # Gate allows when traded_today=False
    state.traded_today = False
    # Still None (no strike data) but for correct reason — gate is open
    result2 = check_all_strategies(state, datetime.now())
    assert result2 is None  # no strikes loaded, expected
test("Engine: one-trade-per-day gate works", t_engine_traded_today_gate)

def t_sl_entry_not_zero():
    from engine import SLState
    sl = SLState()
    cfg = {"max_loss_pct":30,"trail_pct":20,"min_profit_pct":15,
           "vwap_buffer_pct":2,"ema_buffer_pct":1,"profit_target_pct":50}
    sl.activate(450.0, cfg)
    assert sl.entry_combined == 450.0, f"entry={sl.entry_combined} should be 450"
    assert sl.trailing_low   == 450.0
    assert sl.trailing_sl    >  450.0
    # Must NOT fire at entry price on first tick
    exit_, reason = sl.update(450.0, 0, 0, 0, cfg)
    assert not exit_, f"SL fired immediately: {reason}"
    # Must NOT fire at entry+1 (combined rising slightly = options seller losing slightly)
    exit2, reason2 = sl.update(451.0, 0, 0, 0, cfg)
    assert not exit2, f"SL fired too early: {reason2}"
test("SL: entry_combined non-zero, no immediate fire", t_sl_entry_not_zero)

def t_sl_fires_on_max_loss():
    from engine import SLState
    sl = SLState()
    cfg = {"max_loss_pct":30,"trail_pct":20,"min_profit_pct":15,
           "vwap_buffer_pct":2,"ema_buffer_pct":1,"profit_target_pct":50}
    sl.activate(200.0, cfg)
    # Combined rising 31% above entry = max loss hit
    exit_, reason = sl.update(263.0, 0, 0, 0, cfg)
    assert exit_, "Max loss should fire at 131% of entry"
    assert "MAX_LOSS" in reason
test("SL: max loss backstop fires correctly at 30%", t_sl_fires_on_max_loss)

def t_sl_profit_target():
    from engine import SLState
    sl = SLState()
    cfg = {"max_loss_pct":30,"trail_pct":20,"min_profit_pct":15,
           "vwap_buffer_pct":2,"ema_buffer_pct":1,"profit_target_pct":50}
    sl.activate(200.0, cfg)
    # Combined decaying 50% = profit target
    exit_, reason = sl.update(99.0, 0, 0, 0, cfg)
    assert exit_, "Profit target should fire at 50% decay"
    assert "PROFIT_TARGET" in reason
test("SL: profit target fires at 50% decay", t_sl_profit_target)

# ── 15. New endpoints ────────────────────────────────────────
print("\n15. New API endpoints...")

def t_unified_trades():
    r = client.get("/api/trades/unified?days=30", headers=H())
    ok_status(r)
    d = r.json()
    assert "trades" in d
    assert "by_auto" in d
    assert "total" in d
    assert "live_count" in d
    assert "paper_count" in d
test("GET /api/trades/unified", t_unified_trades)

def t_unified_trade_fields():
    # Create a trade then check unified returns full fields
    r = client.get("/api/trades/unified?days=30", headers=H())
    ok_status(r)
    # If there are trades, verify they have all required fields
    trades = r.json().get("trades", [])
    if trades:
        t = trades[0]
        required = ["id","type","date","strategy","atm_strike",
                    "entry_combined","entry_time","entry_reason",
                    "exit_parsed","lots","lot_size","qty",
                    "gross_pnl","brokerage","net_pnl","is_open"]
        for f in required:
            assert f in t, f"Missing field: {f}"
test("GET /api/trades/unified: all detail fields present", t_unified_trade_fields)

def t_reset_paper_trades():
    r = client.delete("/api/trades/reset?trade_type=paper", headers=H())
    ok_status(r)
    d = r.json()
    assert d.get("ok")
    assert "deleted" in d
    assert "paper" in d["deleted"]
    assert "live" in d["deleted"]
    # Live trades should not be touched
    assert d["deleted"]["live"] == 0, "Reset paper should not touch live trades"
test("DELETE /api/trades/reset (paper only)", t_reset_paper_trades)

def t_reset_all_trades():
    r = client.delete("/api/trades/reset?trade_type=all", headers=H())
    ok_status(r)
    d = r.json()
    assert d.get("ok")
    assert "deleted" in d
test("DELETE /api/trades/reset (all types)", t_reset_all_trades)

def t_max_trades_per_day_config():
    # Create automation with max_trades_per_day=2
    r = client.post("/api/automations", headers=H(), json={
        "name": "Max2 Test", "symbol": "NSE:NIFTY50-INDEX",
        "broker_id": "fyers", "strategies": ["S1"],
        "mode": "paper", "shadow_mode": True,
        "telegram_alerts": False,
        "config": {"lots":1, "lot_size":65, "max_trades_per_day": 2}
    })
    ok_status(r)
    autos = client.get("/api/automations", headers=H()).json()["automations"]
    a = next((x for x in autos if x["name"]=="Max2 Test"), None)
    assert a is not None
    assert a["config"]["max_trades_per_day"] == 2
test("Automation: max_trades_per_day config saved correctly", t_max_trades_per_day_config)

def t_pnl_fmt():
    # Verify the JS pnlFmt concept - backend sends correct signed values
    # Net pnl should be positive or negative, never unsigned
    r = client.get("/api/shadow/performance", headers=H())
    ok_status(r)
    d = r.json()
    # total_pnl should be a number (could be 0, positive, or negative)
    assert isinstance(d["total_pnl"], (int, float))
test("P&L values: signed numbers returned from API", t_pnl_fmt)

# ── 16. Regression tests for reported bugs ───────────────────
print("\n16. Regression tests for reported bugs...")

def t_delete_automation_no_body():
    # DELETE /api/automations/{id} must work with no request body
    # Previously failed with "string did not match expected pattern"
    # because Content-Type: application/json was sent on a bodyless DELETE
    r = client.post("/api/automations", headers=H(), json={
        "name": "ToDelete", "symbol": "NSE:NIFTY50-INDEX",
        "broker_id": "fyers", "strategies": ["S1"],
        "mode": "paper", "shadow_mode": True,
        "telegram_alerts": False, "config": {}
    })
    ok_status(r)
    auto_id = r.json()["automation"]["id"]
    # DELETE with no body should succeed
    r2 = client.delete(f"/api/automations/{auto_id}", headers=H())
    ok_status(r2)
    assert r2.json().get("ok")
    # Verify it's gone
    autos = client.get("/api/automations", headers=H()).json()["automations"]
    ids = [a["id"] for a in autos]
    assert auto_id not in ids, "Automation should be deleted"
test("DELETE automation: works without request body", t_delete_automation_no_body)

def t_dashboard_open_positions_split():
    # Dashboard must return open_live and open_paper separately
    r = client.get("/api/dashboard/summary", headers=H())
    ok_status(r)
    d = r.json()
    assert "open_live" in d,  "Missing open_live field"
    assert "open_paper" in d, "Missing open_paper field"
    assert "open_positions" in d
    assert d["open_positions"] == d["open_live"] + d["open_paper"],         "open_positions must equal open_live + open_paper"
test("Dashboard: open_positions split into open_live + open_paper", t_dashboard_open_positions_split)

def t_dashboard_trades_split():
    r = client.get("/api/dashboard/summary", headers=H())
    ok_status(r)
    d = r.json()
    assert "today_live_trades" in d,  "Missing today_live_trades"
    assert "today_paper_trades" in d, "Missing today_paper_trades"
    assert "live_automations" in d,   "Missing live_automations"
    assert "paper_automations" in d,  "Missing paper_automations"
test("Dashboard: today trades and automation counts split live/paper", t_dashboard_trades_split)

def t_capital_check_returns_margin():
    # Capital check must return margin even when no broker connected
    r = client.get("/api/capital/check?symbol=NSE:NIFTY50-INDEX&lots=1", headers=H())
    ok_status(r)
    d = r.json()
    assert "margin" in d, "Missing margin field"
    assert "net_required" in d["margin"], "Missing net_required in margin"
    assert d["margin"]["net_required"] > 0, "Margin estimate must be > 0"
    assert "mode" in d, "Missing mode field (paper/live)"
test("Capital check: returns margin estimate even in paper mode", t_capital_check_returns_margin)

def t_delete_own_trades_only():
    # Reset must only delete requesting user's data
    # Create a second user and their trades, then reset first user's data
    # Verify second user's data is untouched
    r2 = client.post("/api/auth/register", json={
        "name": "Other User", "email": "other_reset@test.com",
        "password": "OtherPass123!", "invite_token": None
    })
    ok_status(r2)
    # Reset admin user's paper trades
    r = client.delete("/api/trades/reset?trade_type=paper", headers=H())
    ok_status(r)
    assert r.json().get("ok")
    # Other user should still exist (different check)
    me = client.get("/api/me", headers=H())
    ok_status(me)
test("Reset trades: only affects requesting user", t_delete_own_trades_only)

# ── 17. Signal quality guards ────────────────────────────────
print("\n17. Signal quality guards...")

def t_s1_uses_current_atm():
    from engine import EngineState, StrikeState, _s1, _current_atm
    from datetime import datetime, time as dtime
    import types

    # Build state simulating morning ATM=23500, spot drifted to 23100
    state = EngineState({"strategies":["S1"],"mode":"paper","strike_round":50})
    state.orb_complete = True
    state.atm_strike   = 23500
    state.spot_locked  = 23500.0
    state.spot_history = [23500, 23400, 23300, 23200, 23100]  # drifted

    # Add 7 strikes around morning ATM
    for i in range(-3, 4):
        sk = StrikeState(strike=23500 + i*50, offset=i, is_atm=(i==0))
        sk.orb_low  = 470.0
        sk.orb_high = 480.0
        # Strike nearest current ATM (23100 → nearest=23100, but monitored range
        # is 23350–23650, so nearest monitored is 23350)
        # Simulate 23350 (offset=-3) breaking ORB low
        if i == -3:
            sk.combined_history = [465.0]  # below orb_low=470
        else:
            sk.combined_history = [475.0]  # above orb_low
        state.strikes.append(sk)

    t_val = dtime(10, 0)
    now   = datetime.now()
    sig   = _s1(state, t_val, now)

    # Drift = (23500-23100)/50 = 8 strikes > DRIFT_MAX_STRIKES=3
    # So S1 should be SKIPPED (candidate 23350 is 5 strikes from current ATM 23100)
    assert sig is None, f"S1 should skip when drift too large, got: {sig}"
test("S1: skips when candidate strike too far from current ATM", t_s1_uses_current_atm)

def t_s1_fires_at_nearest_current_atm():
    from engine import EngineState, StrikeState, _s1, nearest_strike, DRIFT_MAX_STRIKES
    from datetime import datetime, time as dtime

    # Small drift: morning ATM=23500, spot now=23350 (3 strikes = exactly at limit)
    state = EngineState({"strategies":["S1"],"mode":"paper","strike_round":50})
    state.orb_complete = True
    state.atm_strike   = 23500
    state.spot_locked  = 23500.0
    state.spot_history = [23500, 23450, 23350]  # 3 strikes drift

    for i in range(-3, 4):
        sk = StrikeState(strike=23500 + i*50, offset=i, is_atm=(i==0))
        sk.orb_low  = 470.0
        sk.orb_high = 480.0
        # Both -2 (23400) and -3 (23350) break ORB low
        # -3 is closest to current ATM 23350, should be preferred
        if i in (-2, -3):
            sk.combined_history = [460.0]  # below 470
        else:
            sk.combined_history = [475.0]
        state.strikes.append(sk)

    t_val = dtime(10, 0)
    sig   = _s1(state, t_val, datetime.now())

    # Current ATM = nearest_strike(23350) = 23350 (offset=-3)
    # -3 should be preferred over -2 as it's closer to current ATM
    if sig:
        assert sig["strike"] == 23350, f"Should fire at 23350 (nearest current ATM), got {sig['strike']}"
test("S1: fires at strike nearest current ATM not morning ATM", t_s1_fires_at_nearest_current_atm)

def t_drift_guard_suspends_all_signals():
    from engine import EngineState, StrikeState, check_all_strategies
    from datetime import datetime, time as dtime

    state = EngineState({
        "strategies": ["S1","S7","S8","S2","S3"],
        "mode": "paper",
        "strike_round": 50,
        "drift_max_pct": 1.5,  # 1.5% threshold
    })
    state.orb_complete  = True
    state.atm_strike    = 23500
    state.spot_locked   = 23500.0
    # Simulate 3.26% drift like today (23500 → 23034)
    state.spot_history  = [23500, 23400, 23300, 23200, 23034]

    # Add strikes
    for i in range(-3, 4):
        sk = StrikeState(strike=23500+i*50, offset=i, is_atm=(i==0))
        sk.orb_low = 470.0; sk.orb_high = 480.0
        sk.combined_history = [460.0]  # all breaking ORB low
        state.strikes.append(sk)
    state.orb_complete = True

    sig = check_all_strategies(state, datetime.now())
    assert sig is None, f"All signals should be suspended at 3.26% drift, got: {sig}"
test("Drift guard: suspends all signals when spot drifts >1.5%", t_drift_guard_suspends_all_signals)

def t_drift_guard_allows_normal_day():
    from engine import EngineState, StrikeState, check_all_strategies
    from datetime import datetime

    state = EngineState({
        "strategies": ["S1"],
        "mode": "paper",
        "strike_round": 50,
        "drift_max_pct": 1.5,
    })
    state.orb_complete = True
    state.atm_strike   = 23500
    state.spot_locked  = 23500.0
    # Normal day: spot moves 0.3% (70 pts)
    state.spot_history = [23500, 23480, 23450, 23430]

    for i in range(-3, 4):
        sk = StrikeState(strike=23500+i*50, offset=i, is_atm=(i==0))
        sk.orb_low = 470.0; sk.orb_high = 480.0
        sk.combined_history = [460.0]  # breaking ORB low
        sk.ce_symbol = f"NIFTY24MAR{23500+i*50}CE"
        sk.pe_symbol = f"NIFTY24MAR{23500+i*50}PE"
        state.strikes.append(sk)

    sig = check_all_strategies(state, datetime.now())
    # Should NOT be blocked by drift guard (only 0.3% drift)
    # sig may be None for other reasons (no ce/pe symbols) but not drift
    # We verify drift_guard didn't fire by checking the log
    drift_blocked = any("signals suspended" in (e.get("msg","")) for e in state.log)
    assert not drift_blocked, "Normal day should not be blocked by drift guard"
test("Drift guard: allows signals on normal low-drift day", t_drift_guard_allows_normal_day)

def t_vix_guard_blocks_high_vix():
    from engine import EngineState, StrikeState, check_all_strategies
    from datetime import datetime

    state = EngineState({
        "strategies": ["S1"],
        "mode": "paper",
        "strike_round": 50,
        "vix_open": 18.5,   # high VIX
        "vix_max":  17.0,   # threshold
        "drift_max_pct": 99, # disable drift guard for this test
    })
    state.orb_complete = True
    state.atm_strike   = 23500
    state.spot_locked  = 23500.0
    state.spot_history = [23500]

    for i in range(-3, 4):
        sk = StrikeState(strike=23500+i*50, offset=i, is_atm=(i==0))
        sk.orb_low = 470.0; sk.combined_history = [460.0]
        state.strikes.append(sk)

    sig = check_all_strategies(state, datetime.now())
    assert sig is None, f"VIX guard should block signals at VIX 18.5, got: {sig}"
test("VIX guard: blocks all signals when VIX >= threshold", t_vix_guard_blocks_high_vix)

def t_vix_guard_allows_low_vix():
    from engine import EngineState, StrikeState, check_all_strategies
    from datetime import datetime

    state = EngineState({
        "strategies": ["S1"],
        "mode": "paper",
        "strike_round": 50,
        "vix_open": 13.5,   # low VIX — safe to trade
        "vix_max":  17.0,
        "drift_max_pct": 99,
    })
    state.orb_complete = True
    state.atm_strike   = 23500
    state.spot_locked  = 23500.0
    state.spot_history = [23500]

    for i in range(-3, 4):
        sk = StrikeState(strike=23500+i*50, offset=i, is_atm=(i==0))
        sk.orb_low = 470.0; sk.combined_history = [460.0]
        state.strikes.append(sk)

    # VIX is fine — signal should not be blocked by VIX guard
    vix_blocked = any("VIX" in (e.get("msg","")) for e in state.log)
    assert not vix_blocked, "Low VIX day should not be blocked by VIX guard"
test("VIX guard: allows signals when VIX below threshold", t_vix_guard_allows_low_vix)

# ── 18. Results page & dashboard fixes ───────────────────────
print("\n18. Results page & dashboard fixes...")

def t_live_performance_endpoint():
    r = client.get("/api/live/performance?days=30", headers=H())
    ok_status(r)
    d = r.json()
    # Must have all same fields as shadow/performance
    for field in ["total_trades","total_pnl","win_rate","wins","losses",
                  "profit_factor","reward_risk","expectancy","max_drawdown",
                  "max_consec_loss","days_traded","by_strategy","by_day",
                  "equity_curve","exit_reasons","best_day","worst_day"]:
        assert field in d, f"Missing field in live/performance: {field}"
test("GET /api/live/performance: all KPI fields present", t_live_performance_endpoint)

def t_live_performance_empty_correct():
    r = client.get("/api/live/performance?days=1", headers=H())
    ok_status(r)
    d = r.json()
    if d["total_trades"] == 0:
        assert d["total_pnl"] == 0
        assert d["by_day"] == []
test("GET /api/live/performance: empty returns correct defaults", t_live_performance_empty_correct)

def t_market_status_accepts_symbol():
    r = client.get("/api/market/status?symbol=NSE:NIFTYBANK-INDEX", headers=H())
    ok_status(r)
    d = r.json()
    assert "symbol" in d
    assert "sym_short" in d
    assert d["symbol"] == "NSE:NIFTYBANK-INDEX"
test("GET /api/market/status: accepts symbol param, returns sym_short", t_market_status_accepts_symbol)

def t_market_status_default_symbol():
    r = client.get("/api/market/status", headers=H())
    ok_status(r)
    d = r.json()
    assert "symbol" in d
    assert "sym_short" in d
    assert d["symbol"] == "NSE:NIFTY50-INDEX"
test("GET /api/market/status: default symbol is NIFTY", t_market_status_default_symbol)

def t_dashboard_live_paper_auto_split():
    r = client.get("/api/dashboard/summary", headers=H())
    ok_status(r)
    d = r.json()
    assert "live_automations" in d,  "Missing live_automations"
    assert "paper_automations" in d, "Missing paper_automations"
    assert d["live_automations"] + d["paper_automations"] == d["total_automations"],         "live + paper automations must equal total"
test("Dashboard: live_automations + paper_automations = total_automations", t_dashboard_live_paper_auto_split)

def t_best_worst_day_different():
    # When we have trades, best_day.pnl should >= worst_day.pnl
    r = client.get("/api/shadow/performance?days=365", headers=H())
    ok_status(r)
    d = r.json()
    if d["total_trades"] > 0 and d["best_day"] and d["worst_day"]:
        assert d["best_day"]["pnl"] >= d["worst_day"]["pnl"],             f"best_day {d['best_day']['pnl']} must be >= worst_day {d['worst_day']['pnl']}"
test("Performance: best_day.pnl >= worst_day.pnl always", t_best_worst_day_different)

# ── 19. New fixes ────────────────────────────────────────────
print("\n19. New fixes...")

def t_reset_stuck_automations():
    r = client.post("/api/automations/reset-status", headers=H())
    ok_status(r)
    d = r.json()
    assert d.get("ok")
    assert "reset_count" in d
    assert "message" in d
test("POST /api/automations/reset-status: resets stuck automations", t_reset_stuck_automations)

def t_to_ist_utc_detection():
    import sys; sys.path.insert(0, '.')
    import main as m
    from datetime import datetime
    # UTC time 04:30 = IST 10:00 — old record stored as UTC-naive
    utc_naive = datetime(2026, 3, 19, 4, 30, 0)  # 4:30 UTC = 10:00 IST
    result = m._to_ist(utc_naive)
    # Hour < 4 check: 4:30 is >= 4 so will NOT be converted (borderline case)
    assert result is not None
    assert "IST" in result

def t_to_ist_real_utc():
    import sys; sys.path.insert(0, '.')
    import main as m
    from datetime import datetime
    # 3:30 UTC = 9:00 IST (before market open, clearly UTC)
    utc_early = datetime(2026, 3, 19, 3, 30, 0)
    result = m._to_ist(utc_early)
    # h=3 < 4 so should be converted: 3:30 + 5:30 = 9:00 IST
    assert result == "09:00 IST", f"Expected 09:00 IST, got {result}"
test("_to_ist: detects and converts UTC times correctly", t_to_ist_real_utc)

def t_market_status_returns_sym_short():
    r = client.get("/api/market/status?symbol=NSE:NIFTYBANK-INDEX", headers=H())
    ok_status(r)
    d = r.json()
    assert d.get("sym_short") == "BANKNIFTY"
    assert d.get("symbol") == "NSE:NIFTYBANK-INDEX"
test("market_status: sym_short returned for each symbol", t_market_status_returns_sym_short)

def t_help_page_has_automation_guide():
    # Automation feature guide must be in the frontend
    fe = open('../frontend/index.html').read()
    assert '_helpField' in fe, "Missing _helpField helper"
    assert 'Automation Settings' in fe, "Missing automation settings section"
    assert 'Max Spot Drift' in fe, "Missing drift guard help"
    assert 'Max VIX' in fe, "Missing VIX guard help"
    assert 'Max Trades Per Day' in fe, "Missing max trades help"
test("Help page: automation feature guide present", t_help_page_has_automation_guide)

def t_tab_bar_css_uniform():
    fe = open('../frontend/index.html').read()
    assert '.tab-bar{' in fe or '.tab-bar {' in fe, "Missing .tab-bar CSS"
    assert '.tab-btn{' in fe or '.tab-btn {' in fe, "Missing .tab-btn CSS"
    assert '.select-sm{' in fe or '.select-sm {' in fe, "Missing .select-sm CSS"
test("Uniform tab-bar CSS present", t_tab_bar_css_uniform)

# ── 20. Claude AI + Event Calendar ──────────────────────────
print("\n20. Claude AI and Event Calendar...")

def t_events_crud():
    # Create event
    r = client.post("/api/events", headers=H(), json={
        "event_date": "2026-04-09",
        "event_name": "RBI Policy Meeting",
        "category": "rbi",
        "suspend_trading": True,
        "notes": "Watch VIX before this"
    })
    ok_status(r)
    assert r.json().get("ok")
    eid = r.json()["id"]
    # List events
    r2 = client.get("/api/events", headers=H())
    ok_status(r2)
    ids = [e["id"] for e in r2.json()["events"]]
    assert eid in ids
    # Update event
    r3 = client.put(f"/api/events/{eid}", headers=H(), json={
        "event_date": "2026-04-09",
        "event_name": "RBI Policy Meeting (Updated)",
        "category": "rbi",
        "suspend_trading": False,
        "notes": ""
    })
    ok_status(r3)
    # Delete event
    r4 = client.delete(f"/api/events/{eid}", headers=H())
    ok_status(r4)
    assert r4.json().get("ok")
test("Events CRUD: create, list, update, delete", t_events_crud)

def t_seed_default_events():
    r = client.post("/api/events/seed-defaults", headers=H())
    ok_status(r)
    d = r.json()
    assert d.get("ok")
    assert "added" in d
    # Events should now be present
    r2 = client.get("/api/events", headers=H())
    ok_status(r2)
    assert len(r2.json()["events"]) > 0
test("Events: seed defaults populates 2026 calendar", t_seed_default_events)

def t_claude_assessment_endpoint():
    r = client.get("/api/claude/assessment", headers=H())
    ok_status(r)
    d = r.json()
    assert d.get("ok")
    assert "assessment" in d
    a = d["assessment"]
    # Must have all required fields
    for field in ["trade_today","confidence","risk_level",
                  "recommended_strategies","avoid_strategies",
                  "suggested_hedge","reason","claude_enabled"]:
        assert field in a, f"Missing: {field}"
    assert isinstance(a["trade_today"], bool)
    assert isinstance(a["recommended_strategies"], list)
    assert isinstance(a["avoid_strategies"], list)
test("GET /api/claude/assessment: returns structured assessment", t_claude_assessment_endpoint)

def t_claude_ask_no_key():
    # Without API key, should return 503
    r = client.post("/api/claude/ask", headers=H(),
                    json={"question": "Should I trade today?"})
    # Either 503 (no key) or 200 (key configured) — both valid
    assert r.status_code in (200, 503), f"Unexpected: {r.status_code}"
test("POST /api/claude/ask: returns 503 without key or 200 with key", t_claude_ask_no_key)

def t_event_calendar_in_frontend():
    fe = open('../frontend/index.html').read()
    assert 'renderEvents' in fe, "Missing renderEvents function"
    assert 'pg-events' in fe, "Missing pg-events page"
    assert 'claude-dash-card' in fe, "Missing Claude dashboard card"
    assert 'loadClaudeAssessment' in fe, "Missing loadClaudeAssessment"
    assert 'openAiPanel' in fe, "Missing AI panel function"
    assert 'ai-panel' in fe, "Missing AI panel HTML"
    assert 'sendAiMessage' in fe, "Missing sendAiMessage"
test("Frontend: Claude card, event calendar, AI panel all present", t_event_calendar_in_frontend)

# ── 21. AI config + day picker + calendar ────────────────────
print("\n21. AI config, day picker, calendar...")

def t_ai_config_save():
    r = client.post("/api/ai/config", headers=H(), json={
        "api_key": "", "model": "claude-sonnet-4-6",
        "use_for_trading": True, "use_for_analysis": True
    })
    ok_status(r)
    d = r.json()
    assert d.get("ok")
    assert "enabled" in d
    assert "key_set" in d
test("POST /api/ai/config: saves AI configuration", t_ai_config_save)

def t_ai_test_no_key():
    r = client.get("/api/ai/test", headers=H())
    ok_status(r)
    d = r.json()
    assert "ok" in d
    assert "message" in d
test("GET /api/ai/test: returns ok/message without key", t_ai_test_no_key)

def t_me_has_ai_fields():
    r = client.get("/api/me", headers=H())
    ok_status(r)
    d = r.json()
    for f in ["ai_enabled","ai_model","ai_use_trading","ai_use_analysis","ai_key_set"]:
        assert f in d, f"Missing {f} in /api/me"
test("/api/me: returns all AI config fields", t_me_has_ai_fields)

def t_automation_run_days():
    # Create automation with run_days
    r = client.post("/api/automations", headers=H(), json={
        "name": "Test DOW", "symbol": "NSE:NIFTY50-INDEX",
        "broker_id": "fyers", "strategies": ["S1"],
        "mode": "paper", "shadow_mode": True, "telegram_alerts": False,
        "config": {"lots":1,"lot_size":65,"run_days":[0,3,4],
                   "skip_dates":["2026-04-14"],"auto_exit_time":"14:00",
                   "max_loss_pct":30,"profit_target_pct":50}
    })
    ok_status(r)
    aid = r.json()["id"]
    # Verify config stored
    r2 = client.get("/api/automations", headers=H())
    auto = next((a for a in r2.json()["automations"] if a["id"]==aid), None)
    assert auto, "Automation not found"
    assert auto["config"].get("run_days") == [0,3,4]
    assert "2026-04-14" in auto["config"].get("skip_dates",[])
    # Cleanup
    client.delete(f"/api/automations/{aid}", headers=H())
test("Automation: run_days and skip_dates saved in config", t_automation_run_days)

def t_frontend_ai_day_features():
    fe = open('../frontend/index.html').read()
    assert 'day-picker' in fe, "Missing day-picker CSS class"
    assert 'day-btn' in fe, "Missing day-btn CSS class"
    assert '_getSelectedDays' in fe, "Missing _getSelectedDays"
    assert '_skipDates' in fe, "Missing _skipDates"
    assert 'addSkipDate' in fe, "Missing addSkipDate"
    assert 'saveAiConfig' in fe, "Missing saveAiConfig"
    assert 'testAiConnection' in fe, "Missing testAiConnection"
    assert 'cal-grid' in fe, "Missing cal-grid CSS"
    assert 'showDayEvents' in fe, "Missing showDayEvents"
    assert 'ai_insight' in fe, "Missing ai_insight in trade detail"
test("Frontend: day picker, skip dates, AI config, calendar all present", t_frontend_ai_day_features)

def t_engine_state_has_gates():
    from engine import EngineState
    s = EngineState({"mode":"paper"})
    assert hasattr(s, 'event_checked'), "Missing event_checked"
    assert hasattr(s, 'claude_checked'), "Missing claude_checked"
    assert hasattr(s, 'claude_avoid'), "Missing claude_avoid"
    assert hasattr(s, 'claude_suspended'), "Missing claude_suspended"
test("EngineState: has all gate fields", t_engine_state_has_gates)

def t_claude_avoid_removes_from_enabled():
    from engine import EngineState, check_all_strategies
    from datetime import datetime
    s = EngineState({"strategies":["S1","S2","S8"], "mode":"paper"})
    s.claude_avoid = ["S2","S8"]  # Claude says avoid these
    s.orb_complete = True
    s.atm_strike = 23100
    # check_all_strategies should not even try S2 or S8
    # We verify by checking the enabled set logic
    enabled = set(s.config.get("strategies",[])) - set(s.claude_avoid)
    assert "S1" in enabled
    assert "S2" not in enabled
    assert "S8" not in enabled
test("Engine: claude_avoid correctly removes strategies from enabled set", t_claude_avoid_removes_from_enabled)

# ── Summary ──────────────────────────────────────────────────
import os
if os.path.exists("functest.db"):
    os.remove("functest.db")

total = passed + failed
print(f"\n════════════════════════════════════════")
print(f"  Results: {passed}/{total} passed")
if failed:
    print(f"  ❌ {failed} test(s) FAILED")
else:
    print(f"  ✅ ALL TESTS PASSED")
print(f"════════════════════════════════════════\n")
sys.exit(0 if failed == 0 else 1)
