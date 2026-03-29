---
name: deploy
description: Deploy backend or frontend changes to the running AlgoDesk containers.
disable-model-invocation: true
allowed-tools: Bash
---

Deploy the latest changes to AlgoDesk. $ARGUMENTS can be "backend", "frontend", or "all" (default: all).

## Backend deploy

```bash
docker cp /home/ubuntu/algo-desk/backend/main.py algo-desk-backend-1:/app/main.py
docker cp /home/ubuntu/algo-desk/backend/engine.py algo-desk-backend-1:/app/engine.py
docker cp /home/ubuntu/algo-desk/backend/fyers.py algo-desk-backend-1:/app/fyers.py
docker compose -f /home/ubuntu/algo-desk/docker-compose.yml restart backend
```

Then wait 8 seconds and verify:
```bash
sleep 8 && docker logs algo-desk-backend-1 --tail 6
```

Confirm "Application startup complete" is in output.

## Frontend deploy

Frontend is volume-mounted — changes are live immediately. No action needed.
Just confirm the file was saved correctly.

## Always finish with

- Show last 6 lines of backend logs
- Confirm startup succeeded or show the exact error
- Never mark complete if logs show an exception or startup failure
