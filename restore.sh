#!/bin/bash
# ════════════════════════════════════════════════════════════════
# AlgoDesk — New Server Restore Script
# Run this on a fresh Ubuntu 22.04 server to bring everything up
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/mahendrasingh1978/algo-desk/main/restore.sh | bash
#   OR after git clone:
#   chmod +x restore.sh && ./restore.sh
#
# What this does:
#   1. Installs Docker + Docker Compose
#   2. Clones the latest code from GitHub
#   3. Prompts you to fill in .env (or copy from backup)
#   4. Prompts you to restore SSL certs (or generates self-signed for testing)
#   5. Builds and starts all containers
#   6. Verifies everything is healthy
#
# What you need before running:
#   - Your .env file (backed up separately — not in git)
#   - Your SSL certificates OR your domain pointed to this server for certbot
# ════════════════════════════════════════════════════════════════

set -e
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓${NC}  $1"; }
warn() { echo -e "${YELLOW}  ⚠${NC}  $1"; }
err()  { echo -e "${RED}  ✗${NC}  $1"; exit 1; }
head() { echo -e "\n${BOLD}$1${NC}"; }

clear
echo -e "${BOLD}${GREEN}"
echo "  ╔════════════════════════════════════════╗"
echo "  ║     AlgoDesk — Server Restore          ║"
echo "  ╚════════════════════════════════════════╝"
echo -e "${NC}"

# ── Step 1: System packages ──────────────────────────────────────
head "Step 1 — System packages"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get install -y -qq curl git openssl ufw
ok "System packages ready"

# ── Step 2: Docker ───────────────────────────────────────────────
head "Step 2 — Docker"
if ! command -v docker &>/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh -s -- -q
  sudo usermod -aG docker ubuntu
  ok "Docker installed"
else
  ok "Docker already present"
fi
if ! docker compose version &>/dev/null; then
  sudo apt-get install -y -qq docker-compose-plugin
fi
ok "Docker Compose ready"

# ── Step 3: Clone / update repo ─────────────────────────────────
head "Step 3 — Code"
REPO="https://github.com/mahendrasingh1978/algo-desk.git"
APP_DIR="/home/ubuntu/algo-desk"

if [ -d "$APP_DIR/.git" ]; then
  cd "$APP_DIR"
  git pull origin main
  ok "Code updated from GitHub"
else
  git clone "$REPO" "$APP_DIR"
  ok "Code cloned from GitHub"
fi
cd "$APP_DIR"

# ── Step 4: Environment file ─────────────────────────────────────
head "Step 4 — Environment (.env)"
if [ ! -f ".env" ]; then
  warn ".env file not found"
  echo ""
  echo "  Options:"
  echo "  A) Copy your backed-up .env here: cp /path/to/backup/.env $APP_DIR/.env"
  echo "  B) Create from template:          cp .env.example .env && nano .env"
  echo ""
  read -p "  Press ENTER after placing your .env file to continue..." _
  if [ ! -f ".env" ]; then
    err ".env still missing. Cannot continue."
  fi
fi
ok ".env found"

# Validate required keys exist
for key in DB_PASSWORD SECRET_KEY ENCRYPTION_KEY SUPER_ADMIN_EMAIL; do
  if ! grep -q "^${key}=" .env || grep -q "^${key}=$" .env || grep -q "^${key}=change_me" .env; then
    err "$key is missing or still set to placeholder in .env — update it first"
  fi
done
ok ".env validated"

# ── Step 5: SSL certificates ─────────────────────────────────────
head "Step 5 — SSL certificates"
mkdir -p nginx/ssl

if [ -f "nginx/ssl/fullchain.pem" ] && [ -f "nginx/ssl/privkey.pem" ]; then
  ok "SSL certs found"
else
  warn "SSL certificates not found in nginx/ssl/"
  echo ""
  echo "  Options:"
  echo "  A) Copy from backup:  cp /path/to/backup/*.pem $APP_DIR/nginx/ssl/"
  echo "  B) Get new cert with certbot (domain must point to this server's IP):"
  echo "     sudo apt-get install -y certbot"
  echo "     sudo certbot certonly --standalone -d yourdomain.duckdns.org"
  echo "     sudo cp /etc/letsencrypt/live/yourdomain.duckdns.org/fullchain.pem $APP_DIR/nginx/ssl/"
  echo "     sudo cp /etc/letsencrypt/live/yourdomain.duckdns.org/privkey.pem $APP_DIR/nginx/ssl/"
  echo "  C) Generate self-signed (for testing only — browser will warn):"
  echo "     openssl req -x509 -newkey rsa:4096 -keyout nginx/ssl/privkey.pem \\"
  echo "       -out nginx/ssl/fullchain.pem -days 365 -nodes -subj '/CN=localhost'"
  echo ""
  read -p "  Press ENTER after placing SSL certs to continue..." _
  if [ ! -f "nginx/ssl/fullchain.pem" ]; then
    err "SSL certs still missing. Cannot start nginx."
  fi
fi
sudo chmod 600 nginx/ssl/*.pem 2>/dev/null || true

# ── Step 6: Build and start ──────────────────────────────────────
head "Step 6 — Build and start containers"
docker compose down 2>/dev/null || true
docker compose build --no-cache
docker compose up -d
ok "Containers started"

# ── Step 7: Health check ─────────────────────────────────────────
head "Step 7 — Health check"
echo "  Waiting 20 seconds for services to initialise..."
sleep 20

HEALTH=$(curl -sk http://localhost/health 2>/dev/null || echo "fail")
if echo "$HEALTH" | grep -q "ok\|healthy\|alive"; then
  ok "Health check passed"
else
  warn "Health endpoint returned: $HEALTH"
  echo "  Check logs: docker logs algo-desk-backend-1 --tail 30"
fi

STATUS=$(docker compose ps --format "table {{.Name}}\t{{.Status}}" 2>/dev/null)
echo ""
echo "$STATUS"
echo ""

# ── Step 8: Firewall ─────────────────────────────────────────────
head "Step 8 — Firewall"
sudo ufw allow 22/tcp  >/dev/null 2>&1 || true
sudo ufw allow 80/tcp  >/dev/null 2>&1 || true
sudo ufw allow 443/tcp >/dev/null 2>&1 || true
sudo ufw --force enable >/dev/null 2>&1 || true
ok "Firewall configured (22, 80, 443)"

# ── Done ─────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}  ════════════════════════════════════════${NC}"
echo -e "${BOLD}${GREEN}  AlgoDesk is up!${NC}"
DOMAIN=$(grep "^APP_DOMAIN=" .env | cut -d= -f2 | tr -d '"')
echo -e "  Open: ${BOLD}${DOMAIN:-https://your-domain}${NC}"
echo ""
echo "  If DB is fresh (new server) — log in with your SUPER_ADMIN credentials from .env"
echo "  If DB restored from backup  — all users, trades, automations are back"
echo -e "${BOLD}${GREEN}  ════════════════════════════════════════${NC}"
echo ""
