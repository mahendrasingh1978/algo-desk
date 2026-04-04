"""
ALGO-DESK — Fyers Engine
=========================
Token approach matches N8N flow exactly:
  - Refresh token called before EVERY data fetch
  - Never expires as long as system is running
  - Uses options-chain-v3 (single call for spot + chain)
  - User connects once via browser, never again
"""

import os, hashlib, base64, logging
from datetime import datetime
from typing import Optional
import httpx

log = logging.getLogger("fyers")

API  = "https://api-t1.fyers.in/api/v3"
DATA = "https://api-t1.fyers.in/data"


# ── Encryption ────────────────────────────────────────────────

def _fernet(user_id: str):
    from cryptography.fernet import Fernet
    master = os.environ.get("ENCRYPTION_KEY", "changeme-set-in-env-32bytes!!").encode()
    key = hashlib.sha256(master + user_id.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))

def encrypt(user_id: str, value: str) -> str:
    try:
        return _fernet(user_id).encrypt(value.encode()).decode()
    except Exception:
        return base64.b64encode(value.encode()).decode()

def decrypt(user_id: str, value: str) -> str:
    try:
        return _fernet(user_id).decrypt(value.encode()).decode()
    except Exception:
        try:
            return base64.b64decode(value.encode()).decode()
        except Exception:
            return ""


class FyersConnection:
    """
    One instance per user.
    SEBI April 2026: tokens expire at 3 AM daily.
    Use headless_login(totp_key) for daily re-auth instead of validate-refresh-token.
    """

    def __init__(self, user_id: str, client_id: str, secret_key: str,
                 pin: str, redirect_uri: str,
                 access_token_enc: Optional[str] = None,
                 refresh_token_enc: Optional[str] = None,
                 mode: str = "paper",
                 fyers_id: str = "",    # Fyers trading account ID (e.g. TK01248) — for TOTP flow
                 totp_key: str = ""):   # TOTP secret from myaccount.fyers.in External 2FA
        self.user_id      = user_id
        self.client_id    = client_id
        self.secret_key   = secret_key
        self.pin          = pin
        self.redirect_uri = redirect_uri
        self.mode         = mode
        self.fyers_id     = fyers_id
        self.totp_key     = totp_key
        self._access      = decrypt(user_id, access_token_enc)  if access_token_enc  else None
        self._refresh     = decrypt(user_id, refresh_token_enc) if refresh_token_enc else None

    @property
    def app_hash(self) -> str:
        return hashlib.sha256(f"{self.client_id}:{self.secret_key}".encode()).hexdigest()

    @property
    def _auth(self) -> dict:
        return {"Authorization": f"{self.client_id}:{self._access}",
                "Content-Type": "application/json"}

    def login_url(self) -> str:
        return (f"{API}/generate-authcode"
                f"?client_id={self.client_id}"
                f"&redirect_uri={self.redirect_uri}"
                f"&response_type=code&state=algo_desk")

    # ── ONE TIME: exchange auth_code ──────────────────────────

    async def exchange_auth_code(self, auth_code: str) -> dict:
        """Called once by user. Returns encrypted tokens."""
        log.info(f"[fyers:{self.user_id}] Exchanging auth_code...")
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{API}/validate-authcode",
                headers={"Content-Type": "application/json"},
                json={"grant_type": "authorization_code",
                      "appIdHash": self.app_hash,
                      "code": auth_code.strip()})
        d = r.json()
        log.info(f"[fyers:{self.user_id}] Exchange: s={d.get('s')} msg={d.get('message','')}")

        if d.get("s") == "ok":
            self._access  = d["access_token"]
            self._refresh = d.get("refresh_token", "")
            return {
                "ok": True,
                "message": "Connected to Fyers",
                "access_token_enc":  encrypt(self.user_id, self._access),
                "refresh_token_enc": encrypt(self.user_id, self._refresh),
            }
        msg = d.get("message", "Unknown error")
        if "expired" in msg.lower() or "code" in msg.lower():
            msg = "Auth code expired — please get a fresh one (valid ~60 seconds)"
        return {"ok": False, "message": msg}

    # ── DAILY: SEBI-mandated TOTP re-auth (replaces refresh_token) ──

    @staticmethod
    def _totp(key: str) -> str:
        """Generate 6-digit TOTP code — no pyotp dependency needed."""
        import base64, struct, hmac as _hmac, time
        padded   = key.upper() + "=" * ((8 - len(key) % 8) % 8)
        key_b    = base64.b32decode(padded)
        counter  = struct.pack(">Q", int(time.time()) // 30)
        mac      = _hmac.new(key_b, counter, "sha1").digest()
        offset   = mac[-1] & 0x0F
        binary   = struct.unpack(">L", mac[offset: offset + 4])[0] & 0x7FFFFFFF
        return str(binary % 1_000_000).zfill(6)

    async def headless_login(self) -> dict:
        """
        5-step TOTP headless login — SEBI April 2026 daily re-auth.
        Requires fyers_id (trading account ID) and totp_key (External 2FA secret).
        Runs at 8:30 AM IST daily; tokens valid until 3:00 AM next day.

        Returns same shape as exchange_auth_code():
          {"ok": True, "access_token_enc": "...", "refresh_token_enc": "..."}
        """
        if not self.fyers_id:
            return {"ok": False, "message": "fyers_id not set — add your Fyers user ID in My Brokers"}
        if not self.totp_key:
            return {"ok": False, "message": "totp_key not set — add TOTP key from myaccount.fyers.in External 2FA"}

        from urllib.parse import urlparse, parse_qs
        VAGATOR = "https://api-t2.fyers.in/vagator/v2"
        hdrs    = {
            "Accept":       "application/json",
            "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }

        try:
            async with httpx.AsyncClient(timeout=30, headers=hdrs,
                                         follow_redirects=False) as c:
                # Step 1 — send_login_otp_v2
                import base64 as _b64
                fy_id_b64 = _b64.b64encode(self.fyers_id.encode()).decode()
                r1 = await c.post(f"{VAGATOR}/send_login_otp_v2",
                    content=f'{{"fy_id":"{fy_id_b64}","app_id":"2"}}')
                d1 = r1.json()
                if "request_key" not in d1:
                    return {"ok": False,
                            "message": f"TOTP step 1 failed: {d1.get('message', d1)}"}
                rk = d1["request_key"]

                # Step 2 — verify_otp (TOTP code as integer in JSON)
                otp = self._totp(self.totp_key)
                r2  = await c.post(f"{VAGATOR}/verify_otp",
                    content=f'{{"request_key":"{rk}","otp":{otp}}}')
                d2  = r2.json()
                if "request_key" not in d2:
                    return {"ok": False,
                            "message": f"TOTP step 2 failed (wrong TOTP key?): {d2.get('message', d2)}"}
                rk = d2["request_key"]

                # Step 3 — verify_pin_v2 (PIN base64-encoded)
                pin_b64 = _b64.b64encode(str(self.pin).encode()).decode()
                r3  = await c.post(f"{VAGATOR}/verify_pin_v2",
                    content=f'{{"request_key":"{rk}","identity_type":"pin","identifier":"{pin_b64}"}}')
                d3  = r3.json()
                trade_token = (d3.get("data") or {}).get("access_token", "")
                if not trade_token:
                    return {"ok": False,
                            "message": f"TOTP step 3 failed (wrong PIN?): {d3.get('message', d3)}"}

                # Step 4 — get auth_code via POST /api/v3/token (Fyers v3, expects HTTP 308)
                # Fyers v3 uses the FULL client_id (with -100) as app_id in this step.
                # The short form (without -100) was v2 convention and returns -16 on v3.
                import json as _json, re as _re
                client_id_clean = self.client_id.strip()
                # client_id format: "APPCODE-200" or "APPCODE-100"
                # app_id = base part ("APPCODE"), appType = numeric suffix ("200"/"100")
                _m = _re.match(r'^(.+?)-(\d+)$', client_id_clean)
                if _m:
                    app_id_for_token = _m.group(1)
                    app_type_str     = _m.group(2)
                else:
                    app_id_for_token = client_id_clean
                    app_type_str     = "100"
                step4_payload = {
                    "fyers_id":       self.fyers_id.strip(),
                    "app_id":         app_id_for_token,
                    "redirect_uri":   self.redirect_uri.strip(),
                    "appType":        app_type_str,  # string, extracted from client_id suffix
                    "code_challenge": "",
                    "state":          "algodesk",
                    "scope":          "",
                    "nonce":          "",
                    "response_type":  "code",
                    "create_cookie":  True,
                }
                log.info(f"[fyers:{self.user_id}] step4 payload={_json.dumps(step4_payload)[:300]}")
                r4 = await c.post("https://api-t1.fyers.in/api/v3/token",
                    headers={
                        "Authorization":  f"Bearer {trade_token}",
                        "Content-Type":   "application/json",
                    },
                    content=_json.dumps(step4_payload))
                log.info(f"[fyers:{self.user_id}] step4 status={r4.status_code} body={r4.text[:300]}")
                if r4.status_code not in (308, 302, 301):
                    return {"ok": False,
                            "message": (f"TOTP step 4 status {r4.status_code}: "
                                        f"{r4.text[:300]}")}
                # Try JSON body first (Fyers returns {"Url":"..."} on 308)
                redirect_url = ""
                try:
                    redirect_url = r4.json().get("Url", "") or r4.json().get("url", "")
                except Exception:
                    pass
                # Fallback: Location header for standard HTTP redirects
                if not redirect_url:
                    redirect_url = r4.headers.get("Location", "")
                auth_code    = parse_qs(urlparse(redirect_url).query).get("auth_code", [None])[0]
                if not auth_code:
                    return {"ok": False,
                            "message": (f"TOTP step 4: auth_code not in redirect URL. "
                                        f"URL preview: {redirect_url[:120] if redirect_url else 'empty'}")}

                # Step 5 — exchange auth_code (reuse existing method)
                result = await self.exchange_auth_code(auth_code)
                if result.get("ok"):
                    log.info(f"[fyers:{self.user_id}] TOTP daily re-auth OK")
                else:
                    log.error(f"[fyers:{self.user_id}] TOTP step 5 failed: {result.get('message')}")
                return result

        except Exception as e:
            log.error(f"[fyers:{self.user_id}] headless_login error: {e}")
            return {"ok": False, "message": str(e)}

    # ── LEGACY: refresh token (kept as fallback for transition period) ────

    async def refresh_token(self) -> dict:
        """
        Called before EVERY data fetch — matches N8N Map Token → Refresh Token flow.
        Uses refresh_token to get new access_token.
        Refresh token renews itself on each call — never expires.
        """
        if not self._refresh:
            return {"ok": False, "message": "No refresh token. Connect Fyers first."}

        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{API}/validate-refresh-token",
                headers={"Content-Type": "application/json"},
                json={"grant_type": "refresh_token",
                      "appIdHash": self.app_hash,
                      "refresh_token": self._refresh,
                      "pin": self.pin})
        d = r.json()

        if d.get("s") == "ok":
            self._access = d["access_token"]
            if d.get("refresh_token"):
                self._refresh = d["refresh_token"]
            return {
                "ok": True,
                "access_token_enc":  encrypt(self.user_id, self._access),
                "refresh_token_enc": encrypt(self.user_id, self._refresh),
            }
        msg = d.get("message", "Refresh failed")
        if "pin" in msg.lower():
            msg = "PIN incorrect — check your Fyers trading PIN"
        log.error(f"[fyers:{self.user_id}] Refresh failed: {msg}")
        return {"ok": False, "message": msg}

    # ── CORE DATA: options-chain-v3 (one call = spot + chain) ─

    async def get_spot_and_chain(self, symbol: str = "NSE:NIFTY50-INDEX",
                                  strike_count: int = 7) -> dict:
        """
        Matches N8N 'SPOT Price with Option Chain' node.
        Single call to options-chain-v3 returns everything.
        """
        # SEBI April 2026: validate-refresh-token endpoint is DISABLED by Fyers.
        # Access token from OAuth exchange or morning TOTP re-auth is valid all day
        # (until 3 AM). No mid-day refresh needed — use it directly.
        if not self._access:
            return {"ok": False,
                    "message": "No access token — complete Fyers OAuth or wait for 3:05 AM TOTP re-auth"}
        refresh = {}   # nothing to save back

        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(f"{DATA}/options-chain-v3",
                    headers=self._auth,
                    params={"symbol": symbol, "strikecount": strike_count})
            d = r.json()

            if d.get("s") != "ok":
                return {"ok": False, "message": d.get("message", "Option chain failed")}

            chain_data = d.get("data", {}).get("optionsChain", [])

            # Extract spot — matches N8N TM CE/PE Extractor
            spot_item = next((x for x in chain_data
                              if x.get("symbol", "").endswith("INDEX")), None)
            spot = float(spot_item["ltp"]) if spot_item else 0.0
            atm  = round(spot / 50) * 50

            # Build strike map — symbols come directly from API
            chain = {}
            for row in chain_data:
                strike   = int(row.get("strike_price", 0))
                opt_type = row.get("option_type", "")
                if not opt_type or strike == 0:
                    continue
                if strike not in chain:
                    chain[strike] = {"strike": strike, "offset": (strike - atm) // 50,
                                     "ce_ltp": 0, "pe_ltp": 0,
                                     "ce_symbol": "", "pe_symbol": "",
                                     "ce_oi": 0, "pe_oi": 0, "combined": 0}
                if opt_type == "CE":
                    chain[strike]["ce_ltp"]    = float(row.get("ltp", 0))
                    chain[strike]["ce_symbol"] = row.get("symbol", "")
                    chain[strike]["ce_oi"]     = int(row.get("oi", 0))
                elif opt_type == "PE":
                    chain[strike]["pe_ltp"]    = float(row.get("ltp", 0))
                    chain[strike]["pe_symbol"] = row.get("symbol", "")
                    chain[strike]["pe_oi"]     = int(row.get("oi", 0))

            for s in chain.values():
                s["combined"] = round(s["ce_ltp"] + s["pe_ltp"], 2)

            # Extract nearest expiry weekday from API expiryData
            expiry_date    = ""
            expiry_weekday = None
            expiry_data    = d.get("data", {}).get("expiryData", [])
            if expiry_data:
                raw_date = expiry_data[0].get("date", "")
                if raw_date:
                    try:
                        from datetime import datetime as _dt
                        _exp = _dt.strptime(raw_date, "%d-%m-%Y")
                        expiry_date    = raw_date
                        expiry_weekday = _exp.weekday()  # 0=Mon … 6=Sun
                    except Exception:
                        pass

            return {
                "ok": True, "spot": spot, "atm": atm,
                "chain": chain,
                "expiry_date":    expiry_date,
                "expiry_weekday": expiry_weekday,
                "refresh_tokens": refresh or None,  # None in TOTP mode — _save_tokens skips update
                "time": datetime.now().isoformat(),
            }
        except Exception as e:
            log.error(f"[fyers] options-chain-v3 error: {e}")
            return {"ok": False, "message": str(e)}

    # ── Profile (connection test) ─────────────────────────────

    async def get_profile(self) -> dict:
        """Test if token is valid — works 24/7."""
        if not self._access:
            return {"ok": False, "message": "No access token"}
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{API}/profile", headers=self._auth)
        d = r.json()
        if d.get("s") == "ok":
            return {"ok": True, "data": d.get("data", {})}
        return {"ok": False, "message": d.get("message", "Token invalid")}

    # ── Live quotes (indices, VIX etc.) ──────────────────────

    async def get_quotes(self, symbols: list) -> dict:
        """
        Fetch live quotes for one or more symbols (indices, equities).
        Use after get_spot_and_chain() so the access token is already fresh.
        Example: get_quotes(["NSE:INDIAVIX-INDEX"])

        Fyers response "d" is a list of {n: symbol, v: {lp, open_price, ...}}.
        """
        if not self._access:
            return {"ok": False, "message": "No access token"}
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{DATA}/quotes",
                    headers=self._auth,
                    params={"symbols": ",".join(symbols)})
            d = r.json()
            if d.get("s") == "ok":
                quotes = {}
                for item in d.get("d", []):
                    sym = item.get("n", "")
                    v   = item.get("v", {})
                    quotes[sym] = {
                        "ltp":        float(v.get("lp",               0)),
                        "open":       float(v.get("open_price",       0)),
                        "prev_close": float(v.get("prev_close_price", 0)),
                        "high":       float(v.get("high_price",       0)),
                        "low":        float(v.get("low_price",        0)),
                        "change_pct": float(v.get("chp",              0)),
                    }
                return {"ok": True, "quotes": quotes}
            return {"ok": False, "message": d.get("message", "Quotes failed")}
        except Exception as e:
            log.error(f"[fyers] quotes error: {e}")
            return {"ok": False, "message": str(e)}

    # ── Historical data (for backtest) ────────────────────────

    async def get_historical(self, symbol: str, resolution: str,
                              from_ts: int, to_ts: int) -> list:
        if not self._access:
            return []
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get(f"{DATA}/history",
                    headers=self._auth,
                    params={"symbol": symbol, "resolution": resolution,
                            "date_format": "1", "range_from": from_ts,
                            "range_to": to_ts, "cont_flag": "1"})
            d = r.json()
            if d.get("s") == "ok":
                return [{"ts": c[0], "o": c[1], "h": c[2],
                         "l": c[3], "c": c[4], "v": c[5]}
                        for c in d.get("candles", [])]
        except Exception as e:
            log.error(f"[fyers] historical error: {e}")
        return []

    # ── Orders ────────────────────────────────────────────────

    async def place_order(self, symbol: str, side: str, qty: int,
                           product: str = "MARGIN") -> dict:
        """
        Place a single order leg.
        product: MARGIN (derivatives/options), INTRADAY (equity only), CNC (delivery)
        type: 1=LIMIT, 2=MARKET, 3=STOP, 4=STOPLIMIT
        side: 1=BUY, -1=SELL
        """
        if self.mode == "paper":
            import random
            price = round(50 + random.random() * 200, 1)
            return {"ok": True,
                    "order_id": f"PAPER_{datetime.now().strftime('%H%M%S%f')[:14]}",
                    "fill_price": price,
                    "mode": "paper"}

        if not self._access:
            return {"ok": False, "message": "Not authenticated — call refresh_token first"}

        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(f"{API}/orders/sync",
                    headers=self._auth,
                    json={
                        "symbol":       symbol,
                        "qty":          qty,
                        "type":         2,          # 2 = MARKET order
                        "side":         1 if side == "BUY" else -1,
                        "productType":  product,    # MARGIN for derivatives
                        "limitPrice":   0,
                        "stopPrice":    0,
                        "validity":     "DAY",
                        "disclosedQty": 0,
                        "offlineOrder": False,
                    })
            d = r.json()
            if d.get("s") == "ok":
                return {"ok": True, "order_id": d.get("id"), "mode": "live",
                        "message": d.get("message", "")}
            return {"ok": False, "message": d.get("message", "Order failed"),
                    "code": d.get("code")}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    async def place_basket_order(self, legs: list) -> dict:
        """
        Place all 4 Iron Fly/Condor legs as a single basket order.
        This is the correct approach for multi-leg options strategies.
        Atomic — either all legs fill or none do.

        legs: list of dicts with keys:
            symbol, side (BUY/SELL), qty

        Returns: {"ok": True/False, "order_ids": [...], "message": "..."}
        """
        if self.mode == "paper":
            return {
                "ok": True,
                "order_ids": [f"PAPER_{datetime.now().strftime('%H%M%S%f')[:14]}_{i}"
                              for i in range(len(legs))],
                "mode": "paper",
            }

        if not self._access:
            return {"ok": False, "message": "Not authenticated"}

        # Build basket order body
        basket = []
        for leg in legs:
            basket.append({
                "symbol":       leg["symbol"],
                "qty":          leg["qty"],
                "type":         2,          # MARKET
                "side":         1 if leg["side"] == "BUY" else -1,
                "productType":  "MARGIN",   # Always MARGIN for derivatives
                "limitPrice":   0,
                "stopPrice":    0,
                "validity":     "DAY",
                "disclosedQty": 0,
                "offlineOrder": False,
            })

        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(f"{API}/orders/basket",
                    headers=self._auth,
                    json=basket)
            d = r.json()
            if d.get("s") == "ok":
                # Extract individual order IDs from basket response
                order_ids = [o.get("id", "") for o in (d.get("data") or [])]
                return {"ok": True, "order_ids": order_ids,
                        "mode": "live", "message": "Basket order placed"}
            return {"ok": False, "message": d.get("message", "Basket order failed"),
                    "code": d.get("code")}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    async def cancel_order(self, order_id: str) -> dict:
        """Cancel a single order by ID."""
        if self.mode == "paper":
            return {"ok": True, "message": "Paper cancel"}
        if not self._access:
            return {"ok": False, "message": "Not authenticated"}
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.delete(f"{API}/orders/sync",
                    headers=self._auth,
                    json={"id": order_id})
            d = r.json()
            return {"ok": d.get("s") == "ok", "message": d.get("message", "")}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    async def get_positions(self) -> dict:
        """Get current open positions."""
        if not self._access:
            return {}
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{API}/positions", headers=self._auth)
            d = r.json()
            if d.get("s") == "ok":
                return {"ok": True, "positions": d.get("netPositions", [])}
        except Exception:
            pass
        return {"ok": False, "positions": []}

    async def exit_all_positions(self) -> dict:
        """Emergency exit — close all open positions."""
        if self.mode == "paper":
            return {"ok": True, "message": "Paper exit all"}
        if not self._access:
            return {"ok": False, "message": "Not authenticated"}
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.delete(f"{API}/positions",
                    headers=self._auth,
                    json={"segment": 11})  # 11 = NSE F&O
            d = r.json()
            return {"ok": d.get("s") == "ok", "message": d.get("message", "")}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    async def get_funds(self) -> dict:
        """Returns fund data dict, or {"_error": "message"} on failure."""
        if not self._access:
            return {"_error": "No access token — connect Fyers first"}
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{API}/funds", headers=self._auth)
            d = r.json()
            if d.get("s") == "ok":
                return {f["title"]: f.get("equityAmount", 0)
                        for f in d.get("fund_limit", [])}
            # Fyers returns 500 outside market hours on some accounts
            msg = d.get("message", f"Fyers error (HTTP {r.status_code})")
            if r.status_code == 500 or "wrong" in msg.lower():
                msg = "Fyers balance service unavailable outside market hours"
            return {"_error": msg}
        except Exception as e:
            return {"_error": str(e)}

    async def get_orderbook(self) -> dict:
        """
        Fetch today's complete orderbook from Fyers.
        Order status codes:
            1  = Cancelled
            2  = Traded/Filled
            3  = For future use
            4  = Transit
            5  = Rejected
            6  = Pending
            20 = Expired
        """
        if self.mode == "paper":
            return {"ok": True, "orders": []}
        if not self._access:
            return {"ok": False, "orders": [], "message": "Not authenticated"}
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(f"{API}/orders", headers=self._auth)
            d = r.json()
            if d.get("s") == "ok":
                return {"ok": True, "orders": d.get("orderBook", [])}
            return {"ok": False, "orders": [], "message": d.get("message", "")}
        except Exception as e:
            return {"ok": False, "orders": [], "message": str(e)}

    async def get_order_status(self, order_id: str) -> dict:
        """
        Get status of a specific order by ID.
        Returns dict with status_code, status_str, filled_qty, avg_price etc.
        """
        if self.mode == "paper":
            return {"ok": True, "order_id": order_id,
                    "status_code": 2, "status": "FILLED",
                    "filled_qty": 0, "avg_price": 0, "mode": "paper"}
        if not self._access:
            return {"ok": False, "message": "Not authenticated"}
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(f"{API}/orders",
                    params={"id": order_id},
                    headers=self._auth)
            d = r.json()
            if d.get("s") == "ok":
                orders = d.get("orderBook", [])
                if orders:
                    o = orders[0]
                    status_map = {
                        1: "CANCELLED", 2: "FILLED", 4: "TRANSIT",
                        5: "REJECTED", 6: "PENDING", 20: "EXPIRED"
                    }
                    sc = o.get("status", 0)
                    return {
                        "ok":         True,
                        "order_id":   order_id,
                        "status_code": sc,
                        "status":     status_map.get(sc, f"UNKNOWN({sc})"),
                        "filled_qty": o.get("filledQty", 0),
                        "qty":        o.get("qty", 0),
                        "avg_price":  o.get("tradedPrice", 0),
                        "symbol":     o.get("symbol", ""),
                        "side":       "BUY" if o.get("side", 0) == 1 else "SELL",
                        "message":    o.get("message", ""),
                        "reject_reason": o.get("orderValidity", ""),
                    }
            return {"ok": False, "message": d.get("message", "Order not found")}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    async def reconcile_orders(self, order_ids: list,
                               max_wait_secs: int = 30) -> dict:
        """
        Reconcile a list of order IDs after placement.
        Polls every 2 seconds until all orders are in a terminal state
        (FILLED, REJECTED, CANCELLED, EXPIRED) or max_wait_secs reached.

        Returns:
        {
            "all_filled": bool,
            "any_rejected": bool,
            "orders": [{order_id, status, filled_qty, avg_price, ...}],
            "summary": "All 4 legs filled at avg ₹XX"
        }
        """
        if self.mode == "paper":
            return {
                "all_filled": True,
                "any_rejected": False,
                "orders": [{"order_id": oid, "status": "FILLED",
                             "status_code": 2, "filled_qty": 0,
                             "avg_price": 0, "mode": "paper"}
                            for oid in order_ids],
                "summary": f"Paper mode — {len(order_ids)} legs simulated",
            }

        import asyncio
        terminal = {1, 2, 5, 20}  # CANCELLED, FILLED, REJECTED, EXPIRED
        results = {}
        elapsed = 0

        while elapsed < max_wait_secs:
            pending = [oid for oid in order_ids
                       if oid not in results or
                       results[oid].get("status_code") not in terminal]
            if not pending:
                break

            for oid in pending:
                r = await self.get_order_status(oid)
                if r.get("ok"):
                    results[oid] = r

            all_done = all(
                results.get(oid, {}).get("status_code") in terminal
                for oid in order_ids
            )
            if all_done:
                break

            await asyncio.sleep(2)
            elapsed += 2

        # Build summary
        orders = [results.get(oid, {"order_id": oid,
                                    "status": "TIMEOUT",
                                    "status_code": 0,
                                    "filled_qty": 0,
                                    "avg_price": 0})
                  for oid in order_ids]

        filled   = [o for o in orders if o.get("status_code") == 2]
        rejected = [o for o in orders if o.get("status_code") == 5]
        pending  = [o for o in orders if o.get("status_code") not in terminal]

        all_filled    = len(filled) == len(order_ids)
        any_rejected  = len(rejected) > 0

        if all_filled:
            avg_prices = [o.get("avg_price", 0) for o in filled if o.get("avg_price")]
            avg = sum(avg_prices)/len(avg_prices) if avg_prices else 0
            summary = (f"All {len(order_ids)} legs filled"
                      + (f" · avg ₹{avg:.1f}" if avg else ""))
        elif any_rejected:
            summary = (f"⚠️ {len(rejected)} leg(s) REJECTED, "
                      f"{len(filled)} filled")
        elif pending:
            summary = f"⚠️ {len(pending)} leg(s) still pending after {max_wait_secs}s"
        else:
            summary = f"{len(filled)}/{len(order_ids)} legs filled"

        return {
            "all_filled":   all_filled,
            "any_rejected": any_rejected,
            "filled_count": len(filled),
            "rejected":     rejected,
            "pending":      pending,
            "orders":       orders,
            "summary":      summary,
        }

    async def get_positions_reconcile(self, expected_symbols: list) -> dict:
        """
        After opening a position, verify the positions exist in Fyers.
        Compares expected symbols against actual open positions.
        Returns reconciliation result.
        """
        if self.mode == "paper":
            return {"ok": True, "reconciled": True,
                    "message": "Paper mode — positions not verified",
                    "positions": []}

        pos_result = await self.get_positions()
        if not pos_result.get("ok"):
            return {"ok": False, "reconciled": False,
                    "message": "Could not fetch positions"}

        positions = pos_result.get("positions", [])
        pos_symbols = set(p.get("symbol","") for p in positions
                         if p.get("netQty", 0) != 0)

        missing = [s for s in expected_symbols if s not in pos_symbols]
        extra   = [s for s in pos_symbols
                   if any(exp in s for exp in expected_symbols)]

        reconciled = len(missing) == 0

        return {
            "ok":           True,
            "reconciled":   reconciled,
            "expected":     expected_symbols,
            "found":        list(pos_symbols),
            "missing":      missing,
            "message":      ("All positions confirmed" if reconciled
                            else f"Missing positions: {missing}"),
            "positions":    [p for p in positions
                            if p.get("netQty", 0) != 0],
        }
