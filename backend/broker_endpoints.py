"""
Add these endpoints to main.py
Replaces the existing broker connect/test endpoints completely.

Broker connection flow (self-service per user):
  1. GET  /api/brokers/fyers/login-url  → returns URL for user to open
  2. POST /api/brokers/fyers/connect    → user pastes auth_code, system gets tokens
  3. GET  /api/brokers                  → shows connected status
  4. Auto: daily refresh at 8:50 AM    → no user action ever needed again
"""

# ── ADD TO IMPORTS in main.py ─────────────────────────────────
# from fyers import FyersConnection, encrypt, decrypt

# ── REPLACE broker endpoints with these ──────────────────────


@app.get("/api/brokers/fyers/login-url")
def fyers_login_url(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Returns the Fyers login URL for this user.
    User opens it, logs in, gets redirected with auth_code.
    """
    bc = db.query(BrokerConnection).filter(
        BrokerConnection.user_id == user.id,
        BrokerConnection.broker_id == "fyers"
    ).first()

    if not bc or not bc.encrypted_fields:
        raise HTTPException(400,
            "Set up your Fyers credentials first — "
            "enter Client ID, Secret Key, PIN and Redirect URI and save.")

    fields = {k.replace("_enc", ""): decrypt(user.id, v)
              for k, v in bc.encrypted_fields.items()}

    conn = FyersConnection(
        user_id=user.id,
        client_id=fields.get("client_id", ""),
        secret_key=fields.get("secret_key", ""),
        pin=fields.get("pin", ""),
        redirect_uri=fields.get("redirect_uri",
            "https://trade.fyers.in/api-login/redirect-uri/index.html"),
    )

    if not conn.client_id:
        raise HTTPException(400, "Client ID not found. Save your credentials first.")

    return {
        "ok": True,
        "login_url": conn.login_url(),
        "message": "Open this URL, log in to Fyers, then paste the auth_code back here.",
        "redirect_uri": conn.redirect_uri,
    }


class FyersConnectReq(BaseModel):
    auth_code: str


@app.post("/api/brokers/fyers/connect")
async def fyers_connect(
    req: FyersConnectReq,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    User pastes auth_code → system exchanges for tokens → stored encrypted.
    This is called ONCE. After this, daily refresh handles everything.
    """
    bc = db.query(BrokerConnection).filter(
        BrokerConnection.user_id == user.id,
        BrokerConnection.broker_id == "fyers"
    ).first()

    if not bc:
        raise HTTPException(400, "Save your Fyers credentials first.")

    fields = {k.replace("_enc", ""): decrypt(user.id, v)
              for k, v in bc.encrypted_fields.items()}

    conn = FyersConnection(
        user_id=user.id,
        client_id=fields.get("client_id", ""),
        secret_key=fields.get("secret_key", ""),
        pin=fields.get("pin", ""),
        redirect_uri=fields.get("redirect_uri",
            "https://trade.fyers.in/api-login/redirect-uri/index.html"),
    )

    # Exchange auth_code for tokens
    result = await conn.exchange_auth_code(req.auth_code.strip())

    if result["ok"]:
        # Store encrypted tokens in DB
        bc.access_token_enc  = result["access_token_enc"]
        bc.refresh_token_enc = result.get("refresh_token_enc")
        bc.is_connected      = True
        bc.last_tested       = datetime.utcnow()
        bc.last_token_refresh = datetime.utcnow()
        db.commit()

        # Send Telegram confirmation
        _send_tg_task(user.id, db, "✅ Fyers connected successfully!\n\nToken will auto-refresh daily at 8:50 AM. No action needed.")

        return {
            "ok": True,
            "message": "Fyers connected! Token auto-refreshes daily. No action needed.",
            "connected": True,
        }
    else:
        return {"ok": False, "message": result["message"], "connected": False}


@app.post("/api/brokers/fyers/refresh")
async def fyers_manual_refresh(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Manual refresh — user can trigger if needed."""
    bc = db.query(BrokerConnection).filter(
        BrokerConnection.user_id == user.id,
        BrokerConnection.broker_id == "fyers"
    ).first()

    if not bc or not bc.refresh_token_enc:
        raise HTTPException(400, "Not connected. Use the Connect flow first.")

    fields = {k.replace("_enc", ""): decrypt(user.id, v)
              for k, v in bc.encrypted_fields.items()}

    conn = FyersConnection(
        user_id=user.id,
        client_id=fields.get("client_id", ""),
        secret_key=fields.get("secret_key", ""),
        pin=fields.get("pin", ""),
        redirect_uri=fields.get("redirect_uri", ""),
        refresh_token_enc=bc.refresh_token_enc,
    )

    result = await conn.refresh_access_token()

    if result["ok"]:
        bc.access_token_enc   = result["access_token_enc"]
        if result.get("refresh_token_enc"):
            bc.refresh_token_enc = result["refresh_token_enc"]
        bc.last_token_refresh = datetime.utcnow()
        bc.is_connected       = True
        db.commit()
        return {"ok": True, "message": "Token refreshed successfully"}
    else:
        return {"ok": False, "message": result["message"]}


# ── DAILY REFRESH SCHEDULER ───────────────────────────────────
# Replace the _daily_token_refresh function in main.py with this:

async def _daily_fyers_refresh():
    """
    Runs at 8:50 AM IST every day.
    Refreshes Fyers tokens for ALL users automatically.
    Users never need to do anything — runs forever.
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    import pytz

    scheduler = AsyncIOScheduler(timezone=pytz.timezone("Asia/Kolkata"))

    async def refresh_all_users():
        log.info("8:50 AM — Daily Fyers token refresh starting...")
        db = SessionLocal()
        try:
            # Get all active Fyers connections
            connections = db.query(BrokerConnection).filter(
                BrokerConnection.broker_id == "fyers",
                BrokerConnection.is_connected == True
            ).all()

            log.info(f"Refreshing tokens for {len(connections)} users...")

            for bc in connections:
                user = db.query(User).filter(
                    User.id == bc.user_id,
                    User.is_active == True
                ).first()
                if not user:
                    continue

                # Skip if no refresh token
                if not bc.refresh_token_enc:
                    log.warning(f"No refresh token for {user.email} — skipping")
                    continue

                fields = {k.replace("_enc", ""): decrypt(user.id, v)
                          for k, v in bc.encrypted_fields.items()}

                conn = FyersConnection(
                    user_id=user.id,
                    client_id=fields.get("client_id", ""),
                    secret_key=fields.get("secret_key", ""),
                    pin=fields.get("pin", ""),
                    redirect_uri=fields.get("redirect_uri", ""),
                    refresh_token_enc=bc.refresh_token_enc,
                )

                result = await conn.refresh_access_token()

                if result["ok"]:
                    bc.access_token_enc   = result["access_token_enc"]
                    if result.get("refresh_token_enc"):
                        bc.refresh_token_enc = result["refresh_token_enc"]
                    bc.last_token_refresh = datetime.utcnow()
                    bc.is_connected       = True
                    log.info(f"Token refreshed for {user.email} ✓")

                    # Telegram alert
                    if user.telegram_token and user.telegram_chat:
                        await _send_telegram(
                            user.telegram_token, user.telegram_chat,
                            f"✅ Daily token refresh complete\n"
                            f"Fyers connected and ready for today's trading.\n"
                            f"Market opens at 9:15 AM."
                        )
                else:
                    log.error(f"Refresh failed for {user.email}: {result['message']}")
                    bc.is_connected = False

                    if user.telegram_token and user.telegram_chat:
                        await _send_telegram(
                            user.telegram_token, user.telegram_chat,
                            f"⚠️ Token refresh failed: {result['message']}\n"
                            f"Please reconnect Fyers in My Brokers."
                        )

            db.commit()
            log.info("Daily refresh complete.")

        except Exception as e:
            log.error(f"Daily refresh error: {e}")
        finally:
            db.close()

    scheduler.add_job(refresh_all_users, "cron", hour=8, minute=50)
    scheduler.start()
    log.info("Daily token refresh scheduler started — runs at 8:50 AM IST")
