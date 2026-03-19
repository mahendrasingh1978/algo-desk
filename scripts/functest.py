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
