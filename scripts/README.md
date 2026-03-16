# ALGO-DESK Test Scripts

## Purpose
Run these scripts directly on the AWS server to validate everything BEFORE integrating into the application. Fix issues here first — no Docker rebuild needed.

## How to run on AWS server

### 1. SSH into server
```bash
ssh -i ~/Downloads/ALGO\ DESK/ALGODESK.pem ubuntu@35.91.127.14
```

### 2. Clone or pull latest
```bash
cd ~/algo-desk && git pull
```

### 3. Get a fresh auth_code
Open this URL in your browser (replace YOUR_CLIENT_ID):
```
https://api-t1.fyers.in/api/v3/generate-authcode?client_id=YOUR_CLIENT_ID&redirect_uri=https://trade.fyers.in/api-login/redirect-uri/index.html&response_type=code&state=algo_desk
```

Log in → copy the auth_code from the redirect URL (the value after `auth_code=`)

### 4. Run live test (during market hours 9:15–15:30 IST)
```bash
cd ~/algo-desk/scripts
FYERS_CLIENT_ID=FYXXXXX-100 \
FYERS_SECRET_KEY=your_secret \
FYERS_PIN=1234 \
FYERS_AUTH_CODE=eyJhbGci... \
python3 test_fyers.py
```

After first run, it prints your ACCESS_TOKEN. Save it:
```bash
export FYERS_ACCESS_TOKEN=eyJhbGci...  # from output
export FYERS_CLIENT_ID=FYXXXXX-100
```

### 5. Run historical / backtest test
```bash
cd ~/algo-desk/scripts
FYERS_CLIENT_ID=FYXXXXX-100 \
FYERS_ACCESS_TOKEN=eyJhbGci... \
python3 test_historical.py
```

## What each script validates

### test_fyers.py
- Auth code exchange works
- Option chain returns real symbols (no manual symbol construction)
- VWAP and EMA calculate correctly from polled data
- Strategy signals (S1, S7, S8) fire correctly
- SL logic works
- Paper order logging works

### test_historical.py  
- What NIFTY historical candle data Fyers returns (1min, 5min, daily)
- What options historical data is available (current vs expired)
- Whether synthetic premium approach is accurate enough for backtest
- Mini backtest on 7 days of real NIFTY data

## Expected output

### Good output (market open, valid token)
```
[09:22:01] ✓ Access token received (xxx chars)
[09:22:02] ✓ NIFTY spot: ₹23,450.0
[09:22:03] ✓ Got 7 strikes from option chain
[09:22:03]   Strike  CE LTP  PE LTP   Combined  Symbol (CE)
[09:22:03]   23200   12.5    145.3    157.8   NSE:NIFTY2561723200CE
...
[09:22:18] ★ SIGNAL FIRED: [S1] ORB Breakdown Sell
```

### If market is closed
```
[10:00:01] ✓ NIFTY spot: ₹23,450.0
[10:00:02] ⚠ Option chain error: Market is closed
[10:00:02]   Continuing with mock data for strategy logic testing...
```

## Fixing issues found in scripts

If a test script shows an error, fix it directly:
```bash
nano ~/algo-desk/scripts/test_fyers.py
# fix the issue
python3 test_fyers.py
```

No Docker, no GitHub, no rebuild. Fix in seconds.

Once both scripts run cleanly during market hours, the application integration is straightforward.
