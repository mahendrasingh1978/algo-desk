#!/bin/bash
# ════════════════════════════════════════════════════════════════
# AlgoDesk — Database + Config Backup
# Run manually:   bash scripts/backup.sh
# Run on schedule: add to crontab (see bottom of this file)
#
# Creates a timestamped backup of:
#   - PostgreSQL database (all trades, users, automations, settings)
#   - .env file
#   - SSL certificates
#
# Backup location: /home/ubuntu/algo-desk-backups/
# ════════════════════════════════════════════════════════════════

set -e
APP_DIR="/home/ubuntu/algo-desk"
BACKUP_DIR="/home/ubuntu/algo-desk-backups"
DATE=$(date +%Y-%m-%d_%H-%M)
DEST="$BACKUP_DIR/$DATE"

mkdir -p "$DEST"

echo "► Backing up database..."
docker exec algo-desk-postgres-1 pg_dump \
  -U "$(grep DB_USER $APP_DIR/.env | cut -d= -f2)" \
  "$(grep DB_NAME $APP_DIR/.env | cut -d= -f2)" \
  > "$DEST/database.sql"
echo "  ✓ database.sql ($(du -sh $DEST/database.sql | cut -f1))"

echo "► Backing up .env..."
cp "$APP_DIR/.env" "$DEST/.env"
echo "  ✓ .env"

echo "► Backing up SSL certs..."
cp -r "$APP_DIR/nginx/ssl/" "$DEST/ssl/"
echo "  ✓ ssl/"

echo "► Creating archive..."
cd "$BACKUP_DIR"
tar -czf "algodesk-backup-$DATE.tar.gz" "$DATE/"
rm -rf "$DATE/"
echo "  ✓ algodesk-backup-$DATE.tar.gz ($(du -sh $BACKUP_DIR/algodesk-backup-$DATE.tar.gz | cut -f1))"

echo "► Keeping last 14 backups..."
ls -t "$BACKUP_DIR"/algodesk-backup-*.tar.gz | tail -n +15 | xargs rm -f 2>/dev/null || true

echo ""
echo "  Backup complete: $BACKUP_DIR/algodesk-backup-$DATE.tar.gz"
echo ""
echo "  To restore on a new server:"
echo "    tar -xzf algodesk-backup-$DATE.tar.gz"
echo "    cp $DATE/.env /home/ubuntu/algo-desk/.env"
echo "    cp -r $DATE/ssl/ /home/ubuntu/algo-desk/nginx/ssl/"
echo "    cat $DATE/database.sql | docker exec -i algo-desk-postgres-1 psql -U <DB_USER> <DB_NAME>"

# ── To run daily at 2am, add this line to crontab (crontab -e):
# 0 2 * * * bash /home/ubuntu/algo-desk/scripts/backup.sh >> /home/ubuntu/algo-desk/scripts/backup.log 2>&1
