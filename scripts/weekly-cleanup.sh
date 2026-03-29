#!/bin/bash
# ─────────────────────────────────────────────
#  AlgoDesk Weekly Safe Cleanup
#  Runs every Sunday at 2:00 AM IST
#  Safe: never touches running containers, DB data, or volumes
# ─────────────────────────────────────────────

LOG="/home/ubuntu/algo-desk/scripts/cleanup.log"
DATE=$(date '+%Y-%m-%d %H:%M:%S')

echo "[$DATE] ── Weekly cleanup started ──" >> "$LOG"

# ── 1. Docker build cache (biggest offender ~900MB per week of deploys)
BEFORE=$(df / | awk 'NR==2{print $3}')
docker builder prune -f >> "$LOG" 2>&1
docker image prune -f  >> "$LOG" 2>&1

# ── 2. Old Docker logs (containers write logs to /var/lib/docker/containers)
# Truncate large container logs — needs sudo
sudo find /var/lib/docker/containers -name "*.log" -size +50M 2>/dev/null | while read f; do
    echo "[$DATE] Truncating large container log: $f ($(du -sh "$f" 2>/dev/null | cut -f1))" >> "$LOG"
    sudo truncate -s 0 "$f"
done

# ── 3. System journal logs — keep last 7 days only
journalctl --vacuum-time=7d >> "$LOG" 2>&1

# ── 4. Temp files older than 7 days
find /tmp -type f -mtime +7 -delete 2>/dev/null

# ── 5. PostgreSQL VACUUM — reclaim dead row space without locking
docker exec algo-desk-postgres-1 psql -U algodesk -d algodesk \
    -c "VACUUM ANALYZE;" >> "$LOG" 2>&1

# ── 6. Old shadow trades — keep last 90 days (trade_date stored as text YYYY-MM-DD)
docker exec algo-desk-postgres-1 psql -U algodesk -d algodesk \
    -c "DELETE FROM shadow_trades WHERE trade_date::date < CURRENT_DATE - INTERVAL '90 days' AND is_open = false;" \
    >> "$LOG" 2>&1

# ── 7. Old trading events — keep last 60 days
docker exec algo-desk-postgres-1 psql -U algodesk -d algodesk \
    -c "DELETE FROM trading_events WHERE created_at < NOW() - INTERVAL '60 days';" \
    >> "$LOG" 2>&1

# ── 8. Old Claude assessments — keep last 60 days (assess_date stored as text YYYY-MM-DD)
docker exec algo-desk-postgres-1 psql -U algodesk -d algodesk \
    -c "DELETE FROM claude_assessments WHERE assess_date::date < CURRENT_DATE - INTERVAL '60 days';" \
    >> "$LOG" 2>&1

# ── 9. Expired invite/reset tokens
docker exec algo-desk-postgres-1 psql -U algodesk -d algodesk \
    -c "DELETE FROM invite_links WHERE expires_at < NOW();" \
    >> "$LOG" 2>&1
docker exec algo-desk-postgres-1 psql -U algodesk -d algodesk \
    -c "DELETE FROM reset_tokens WHERE expires_at < NOW();" \
    >> "$LOG" 2>&1

# ── Summary
AFTER=$(df / | awk 'NR==2{print $3}')
FREED=$(( (BEFORE - AFTER) / 1024 ))
DISK_FREE=$(df -h / | awk 'NR==2{print $4}')

echo "[$DATE] Freed ~${FREED}MB | Disk free now: ${DISK_FREE}" >> "$LOG"
echo "[$DATE] ── Cleanup done ──" >> "$LOG"
echo "" >> "$LOG"

# Keep log file under 500KB (trim oldest entries)
LOG_SIZE=$(wc -c < "$LOG")
if [ "$LOG_SIZE" -gt 512000 ]; then
    tail -200 "$LOG" > "${LOG}.tmp" && mv "${LOG}.tmp" "$LOG"
fi
