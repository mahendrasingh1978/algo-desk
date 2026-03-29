---
name: debug-engine
description: Debug why an automation engine is not firing trades, stuck, or behaving unexpectedly. Use when engine is not working, trades not firing, or signals missing.
disable-model-invocation: false
allowed-tools: Bash, Read, Grep
---

Debug the AlgoDesk trading engine for $ARGUMENTS (automation name or user, or general if blank).

## Step 1 — Check container is alive

```bash
docker compose -f /home/ubuntu/algo-desk/docker-compose.yml ps
docker logs algo-desk-backend-1 --tail 30 2>&1
```

## Step 2 — Check engine state via API

```bash
curl -sk https://algodeskai.duckdns.org/api/engine/status \
  -H "Authorization: Bearer <token>"
```

Look for: `running`, `engine_mode`, `guard_status`, `position`

## Step 3 — Common causes to check

| Symptom | Likely cause | Where to look |
|---------|-------------|---------------|
| Engine shows RUNNING but no trades | Guard rails blocking | `guard_status` field, check VIX, event calendar |
| Engine shows IDLE | Not started or crashed | Docker logs for exception |
| "Outside market hours" forever | Timezone issue or weekend | Check IST time |
| "Data error" repeating | Broker token expired | `last_token_refresh` in DB |
| Trades fire in paper but not live | Mode mismatch | `auto.mode` vs `conn.mode` |
| Day reset not happening | atm_strike still set from yesterday | Check 9:15 reset log line |

## Step 4 — Read engine log

The engine emits structured logs. Look for:
- `[START]` — engine began
- `[WARN]` — guard blocked a signal
- `[OK]` — trade fired
- `[ERROR]` — something failed

## Output

State what's wrong, exactly why based on evidence, and what to fix.
