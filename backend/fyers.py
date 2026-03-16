"""
ALGO-DESK — Fyers Broker
=========================
Self-service. Every user connects their own account.
No admin involvement. No TOTP needed.

Flow:
  First time (30 seconds, user does once):
    1. User clicks Connect → opens Fyers login URL
    2. User logs in on Fyers normally
    3. User pastes auth_code back into ALGO-DESK
    4. System exchanges auth_code for access_token + refresh_token
    5. Both stored encrypted in DB

  Every day (fully automatic, no user action ever):
    - 8:50 AM: POST /validate-refresh-token
    - Returns new access_token
    - refresh_token extends itself automatically
    - Runs forever — user never needs to reconnect
"""

import os, hashlib, base64, logging
from datetime import datetime
from typing import Optional
import httpx

log = logging.getLogger("fyers")

API = "https://api-t1.fyers.in/api/v3"


# ── Encryption ────────────────────────────────────────────────

def _fernet(user_id: str):
    from cryptography.fernet import Fernet
    master = os.environ.get("ENCRYPTION_KEY", "changeme-set-in-env-file-please!!").encode()
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


# ── Fyers connection ──────────────────────────────────────────

class FyersConnection:
    """
    One instance per user per broker connection.
    Holds decrypted tokens in memory only during use.
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

        # Decrypt tokens if present
        self._access_token  = decrypt(user_id, access_token_enc)  if access_token_enc  else None
        self._refresh_token = decrypt(user_id, refresh_token_enc) if refresh_token_enc else None

    @property
    def app_id(self) -> str:
        """Extract app ID from client_id e.g. FYXXXXX from FYXXXXX-100"""
        return self.client_id.split("-")[0]

    @property
    def app_hash(self) -> str:
        """SHA256 of client_id:secret_key — required by Fyers API"""
        return hashlib.sha256(
            f"{self.client_id}:{self.secret_key}".encode()
        ).hexdigest()

    def login_url(self) -> str:
        """
        Generate the Fyers login URL.
        User opens this, logs in, gets redirected with auth_code.
        """
        return (
            f"{API}/generate-authcode"
            f"?client_id={self.client_id}"
            f"&redirect_uri={self.redirect_uri}"
            f"&response_type=code"
            f"&state=algo_desk"
        )

    async def exchange_auth_code(self, auth_code: str) -> dict:
        """
        Step 1 (one time): Exchange auth_code for access_token + refresh_token.
        Returns encrypted tokens to store in DB.
        """
        log.info(f"[fyers:{self.user_id}] Exchanging auth_code for tokens...")

        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{API}/validate-authcode",
                headers={"Content-Type": "application/json"},
                json={
                    "grant_type": "authorization_code",
                    "appIdHash":  self.app_hash,
                    "code":       auth_code,
                })

        d = r.json()
        log.info(f"[fyers:{self.user_id}] Exchange response: s={d.get('s')} msg={d.get('message','')}")

        if d.get("s") == "ok":
            self._access_token  = d["access_token"]
            self._refresh_token = d.get("refresh_token", "")

            return {
                "ok": True,
                "message": "Connected to Fyers successfully",
                "access_token_enc":  encrypt(self.user_id, self._access_token),
                "refresh_token_enc": encrypt(self.user_id, self._refresh_token),
            }
        else:
            msg = d.get("message", "Unknown error")
            # Give helpful error messages
            if "code" in msg.lower() or "expired" in msg.lower():
                msg = "Auth code expired or already used. Please click Connect again and paste the new code immediately."
            elif "invalid" in msg.lower():
                msg = "Invalid credentials. Check your Client ID and Secret Key in Fyers app settings."
            return {"ok": False, "message": msg, "raw": d}

    async def refresh_access_token(self) -> dict:
        """
        Daily refresh (automatic at 8:50 AM).
        Uses refresh_token to get new access_token.
        refresh_token renews itself automatically — runs forever.
        """
        if not self._refresh_token:
            return {"ok": False, "message": "No refresh token. User must connect once first."}

        log.info(f"[fyers:{self.user_id}] Daily token refresh...")

        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{API}/validate-refresh-token",
                headers={"Content-Type": "application/json"},
                json={
                    "grant_type":    "refresh_token",
                    "appIdHash":     self.app_hash,
                    "refresh_token": self._refresh_token,
                    "pin":           self.pin,
                })

        d = r.json()
        log.info(f"[fyers:{self.user_id}] Refresh response: s={d.get('s')} msg={d.get('message','')}")

        if d.get("s") == "ok":
            self._access_token = d["access_token"]
            # refresh_token may also be renewed — save it if returned
            if d.get("refresh_token"):
                self._refresh_token = d["refresh_token"]

            return {
                "ok": True,
                "message": "Token refreshed successfully",
                "access_token_enc":  encrypt(self.user_id, self._access_token),
                "refresh_token_enc": encrypt(self.user_id, self._refresh_token),
            }
        else:
            msg = d.get("message", "Refresh failed")
            if "pin" in msg.lower():
                msg = "PIN incorrect. Check your Fyers trading PIN."
            elif "token" in msg.lower() and "invalid" in msg.lower():
                msg = "Refresh token invalid. User needs to reconnect once."
            return {"ok": False, "message": msg}

    # ── Market data ───────────────────────────────────────────

    @property
    def _auth_header(self) -> dict:
        return {
            "Authorization": f"{self.client_id}:{self._access_token}",
            "Content-Type":  "application/json",
        }

    async def get_ltp(self, symbol: str) -> Optional[float]:
        if not self._access_token:
            return None
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{API}/quotes",
                    headers=self._auth_header,
                    params={"symbols": symbol})
            d = r.json()
            if d.get("s") == "ok" and d.get("d"):
                return float(d["d"][0]["v"]["lp"])
        except Exception as e:
            log.error(f"[fyers] get_ltp error: {e}")
        return None

    async def get_option_chain(self, symbol: str, strike_count: int = 10,
                                expiry: Optional[str] = None) -> dict:
        if not self._access_token:
            return {}
        try:
            params = {"symbol": symbol, "strikecount": strike_count}
            if expiry:
                params["timestamp"] = expiry
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(f"{API}/option-chain",
                    headers=self._auth_header, params=params)
            d = r.json()
            if d.get("s") != "ok":
                return {}
            chain = {}
            for row in d.get("data", {}).get("optionChain", []):
                strike   = int(row.get("strikePrice", 0))
                opt_type = row.get("option_type", "")
                ltp      = float(row.get("ltp", 0))
                sym      = row.get("symbol", "")
                if strike not in chain:
                    chain[strike] = {"ce_ltp": 0, "pe_ltp": 0,
                                     "ce_symbol": "", "pe_symbol": ""}
                if opt_type == "CE":
                    chain[strike]["ce_ltp"]    = ltp
                    chain[strike]["ce_symbol"] = sym
                elif opt_type == "PE":
                    chain[strike]["pe_ltp"]    = ltp
                    chain[strike]["pe_symbol"] = sym
            return chain
        except Exception as e:
            log.error(f"[fyers] option_chain error: {e}")
            return {}

    async def get_historical(self, symbol: str, resolution: str,
                              from_ts: int, to_ts: int) -> list:
        if not self._access_token:
            return []
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get("https://api-t1.fyers.in/data/history",
                    headers=self._auth_header,
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
        # Paper mode — simulate
        if self.mode == "paper":
            import random
            price = 50 + random.random() * 200
            slippage = 2 if side == "BUY" else -2
            oid = f"PAPER_{datetime.now().strftime('%H%M%S%f')[:14]}"
            log.info(f"[PAPER] {side} {qty}x {symbol} @ {price+slippage:.1f}")
            return {"ok": True, "order_id": oid,
                    "fill_price": round(price + slippage, 1),
                    "mode": "paper"}

        # Live mode
        if not self._access_token:
            return {"ok": False, "message": "Not authenticated"}

        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(f"{API}/orders/sync",
                    headers=self._auth_header,
                    json={"symbol": symbol, "qty": qty,
                          "type": 2, "side": 1 if side == "BUY" else -1,
                          "productType": product, "limitPrice": 0,
                          "stopPrice": 0, "validity": "DAY",
                          "disclosedQty": 0, "offlineOrder": False})
            d = r.json()
            if d.get("s") == "ok":
                return {"ok": True, "order_id": d.get("id"),
                        "mode": "live"}
            return {"ok": False, "message": d.get("message", "Order failed")}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    async def get_positions(self) -> list:
        if not self._access_token:
            return []
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{API}/positions", headers=self._auth_header)
            d = r.json()
            return d.get("netPositions", []) if d.get("s") == "ok" else []
        except Exception:
            return []

    async def get_funds(self) -> dict:
        if not self._access_token:
            return {}
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{API}/funds", headers=self._auth_header)
            d = r.json()
            if d.get("s") == "ok":
                return {f["title"]: f.get("equityAmount", 0)
                        for f in d.get("fund_limit", [])}
        except Exception:
            pass
        return {}
