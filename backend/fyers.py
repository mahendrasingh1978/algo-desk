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
    One instance per user. Matches N8N flow exactly.
    Token refreshed before every data call.
    """

    def __init__(self, user_id: str, client_id: str, secret_key: str,
                 pin: str, redirect_uri: str,
                 access_token_enc: Optional[str] = None,
                 refresh_token_enc: Optional[str] = None,
                 mode: str = "paper"):
        self.user_id      = user_id
        self.client_id    = client_id
        self.secret_key   = secret_key
        self.pin          = pin
        self.redirect_uri = redirect_uri
        self.mode         = mode
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

    # ── EVERY CALL: refresh token first (matches N8N) ─────────

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
        # Refresh token first — every time (N8N approach)
        refresh = await self.refresh_token()
        if not refresh["ok"]:
            return {"ok": False, "message": refresh["message"]}

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

            return {
                "ok": True, "spot": spot, "atm": atm,
                "chain": chain,
                "refresh_tokens": refresh,  # return new tokens for DB update
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
                           product: str = "INTRADAY") -> dict:
        if self.mode == "paper":
            import random
            price = 50 + random.random() * 200
            slippage = 2 if side == "BUY" else -2
            return {"ok": True,
                    "order_id": f"PAPER_{datetime.now().strftime('%H%M%S%f')[:14]}",
                    "fill_price": round(price + slippage, 1),
                    "mode": "paper"}

        if not self._access:
            return {"ok": False, "message": "Not authenticated"}

        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(f"{API}/orders/sync",
                    headers=self._auth,
                    json={"symbol": symbol, "qty": qty, "type": 2,
                          "side": 1 if side == "BUY" else -1,
                          "productType": product, "limitPrice": 0,
                          "stopPrice": 0, "validity": "DAY",
                          "disclosedQty": 0, "offlineOrder": False})
            d = r.json()
            if d.get("s") == "ok":
                return {"ok": True, "order_id": d.get("id"), "mode": "live"}
            return {"ok": False, "message": d.get("message", "Order failed")}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    async def get_funds(self) -> dict:
        if not self._access:
            return {}
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{API}/funds", headers=self._auth)
            d = r.json()
            if d.get("s") == "ok":
                return {f["title"]: f.get("equityAmount", 0)
                        for f in d.get("fund_limit", [])}
        except Exception:
            pass
        return {}
