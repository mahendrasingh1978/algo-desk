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
    # Default gap=50
    assert nearest_strike(24075) == 24100  # rounds to nearest 50
    assert nearest_strike(24050) == 24050
    assert nearest_strike(24049) == 24050
    assert nearest_strike(24000) == 24000
test("nearest_strike rounds to 50pt gap correctly", t_nearest_strike)

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
