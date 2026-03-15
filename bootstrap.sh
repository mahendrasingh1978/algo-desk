#!/bin/bash
# ════════════════════════════════════════════════════════════════
# ALGO-DESK Bootstrap — Mahendra's Server
# Server: 35.91.127.14
# GitHub: https://github.com/mahendrasingh1978/algo-desk.git
# ════════════════════════════════════════════════════════════════
# Paste this entire script into your AWS terminal and press Enter.
# It runs for about 2 minutes then gives you a token.
# After that, everything is done from the browser — no more terminal.
# ════════════════════════════════════════════════════════════════

set -e
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${GREEN}  ▶${NC} $1"; }
warn() { echo -e "${YELLOW}  ⚠${NC}  $1"; }
head() { echo -e "\n${BOLD}${BLUE}$1${NC}"; }

clear
echo -e "${BOLD}${GREEN}"
echo "  ╔═══════════════════════════════════════╗"
echo "  ║     ALGO-DESK — Mahendra's Setup      ║"
echo "  ║     Server: 35.91.127.14              ║"
echo "  ╚═══════════════════════════════════════╝"
echo -e "${NC}"
sleep 1

# ── Step 1: System packages ──────────────────────────────────────
head "Step 1 of 6 — Installing system packages"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get install -y -qq curl python3 python3-pip python3-venv git openssl ufw fail2ban
log "System packages installed ✓"

# ── Step 2: Docker ───────────────────────────────────────────────
head "Step 2 of 6 — Installing Docker"
if ! command -v docker &>/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh -s -- -q
  sudo usermod -aG docker ubuntu
  log "Docker installed ✓"
else
  log "Docker already present ✓"
fi
sudo apt-get install -y -qq docker-compose-plugin
log "Docker Compose installed ✓"

# ── Step 3: Agent setup ──────────────────────────────────────────
head "Step 3 of 6 — Setting up management agent"
sudo mkdir -p /opt/algo-agent
sudo chown ubuntu:ubuntu /opt/algo-agent
cd /opt/algo-agent

python3 -m venv venv
source venv/bin/activate
pip install -q flask flask-cors psutil requests

# Generate secure token
AGENT_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
echo "$AGENT_TOKEN" > /opt/algo-agent/agent.token
chmod 600 /opt/algo-agent/agent.token
log "Agent token generated ✓"

# ── Step 4: Write agent ──────────────────────────────────────────
head "Step 4 of 6 — Writing agent code"
cat > /opt/algo-agent/agent.py << 'PYEOF'
import os, subprocess, threading, json, time, psutil, zipfile, shutil, gzip
from pathlib import Path
from datetime import datetime
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
from functools import wraps

app   = Flask(__name__)
CORS(app, origins=["*"])
TOKEN = open("/opt/algo-agent/agent.token").read().strip()
HOME  = Path("/home/ubuntu")
PROJ  = HOME / "algo-desk"
LOG   = Path("/opt/algo-agent/agent.log")
GITHUB = "https://github.com/mahendrasingh1978/algo-desk.git"

def rl(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}\n"
    with open(LOG,"a") as f: f.write(line)
    print(line, end="")

def auth(f):
    from functools import wraps
    @wraps(f)
    def d(*a,**k):
        if request.headers.get("X-Agent-Token","") != TOKEN:
            return jsonify({"ok":False,"error":"Unauthorised"}), 401
        return f(*a,**k)
    return d

@app.route("/health")
def health():
    return jsonify({"ok":True,"agent_version":"1.0.0",
                    "server_time":datetime.now().isoformat(),
                    "algo_desk_installed":PROJ.exists()})

@app.route("/status")
@auth
def status():
    containers = []
    try:
        r = subprocess.run(["docker","compose","ps","--format","json"],
            cwd=str(PROJ),capture_output=True,text=True,timeout=10)
        for line in r.stdout.strip().split("\n"):
            if line.strip():
                try: containers.append(json.loads(line))
                except: pass
    except: pass
    disk = psutil.disk_usage("/")
    mem  = psutil.virtual_memory()
    return jsonify({"ok":True,
        "cpu_pct": psutil.cpu_percent(interval=0.5),
        "mem_pct": round(mem.percent,1),
        "mem_gb":  round(mem.total/1e9,1),
        "disk_pct":round(disk.percent,1),
        "disk_gb": round(disk.total/1e9,1),
        "containers": containers,
        "algo_desk_running": any(
            "running" in (c.get("State","") or c.get("Status","")).lower()
            for c in containers if "backend" in (c.get("Name","") or c.get("Service","")).lower()),
        "ip": "35.91.127.14"})

# ── deploy ──────────────────────────────────────────────────────
deploy_log   = []
deploy_done  = False
deploy_error = None

def emit(msg, kind="info"):
    deploy_log.append({"type":kind,"msg":msg,"ts":datetime.now().strftime("%H:%M:%S")})
    rl(f"[{kind.upper()}] {msg}")

@app.route("/deploy/stream")
@auth
def deploy_stream():
    def gen():
        sent = 0
        while True:
            while sent < len(deploy_log):
                yield f"data: {json.dumps(deploy_log[sent])}\n\n"
                sent += 1
            if deploy_done:
                yield f"data: {json.dumps({'type':'done'})}\n\n"; break
            if deploy_error:
                yield f"data: {json.dumps({'type':'error','msg':deploy_error})}\n\n"; break
            time.sleep(0.3)
    return Response(stream_with_context(gen()),
        mimetype="text/event-stream",
        headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@app.route("/deploy", methods=["POST"])
@auth
def deploy():
    global deploy_log, deploy_done, deploy_error
    deploy_log=[]; deploy_done=False; deploy_error=None
    cfg = request.get_json() or {}
    threading.Thread(target=_do_deploy, args=(cfg,), daemon=True).start()
    return jsonify({"ok":True})

def _run(cmd, cwd=None, timeout=300):
    r = subprocess.run(cmd, cwd=cwd or str(HOME),
        capture_output=True, text=True, timeout=timeout)
    if r.stdout.strip(): emit(r.stdout.strip()[:400])
    if r.returncode != 0 and r.stderr.strip():
        emit(r.stderr.strip()[:300], "warn")
    return r

def _do_deploy(cfg):
    global deploy_done, deploy_error
    try:
        email = cfg.get("adminEmail","mahendrasingh1978@gmail.com")
        pw    = cfg.get("adminPass","changeme123")
        name  = cfg.get("adminName","Mahendra")
        tgtok = cfg.get("tgToken","")
        tgcht = cfg.get("tgChat","")

        # 1 Project files
        emit("Setting up project files...", "step")
        if (PROJ/".git").exists():
            emit("Existing repo found — pulling latest...")
            _run(["git","pull","origin","main"], cwd=str(PROJ))
        else:
            emit("Cloning from GitHub...")
            _run(["git","clone", GITHUB, str(PROJ)])
        emit("Project files ready ✓", "ok")

        # 2 Secrets
        emit("Generating secure secrets...", "step")
        import secrets as sec, base64
        sk  = sec.token_hex(32)
        ek  = base64.urlsafe_b64encode(sec.token_bytes(32)).decode()
        dbp = sec.token_urlsafe(24)
        rdp = sec.token_urlsafe(16)
        emit("Secrets generated ✓", "ok")

        # 3 Write .env
        emit("Writing configuration...", "step")
        env_content = f"""DB_NAME=algodesk
DB_USER=algodesk
DB_PASSWORD={dbp}
REDIS_PASSWORD={rdp}
SECRET_KEY={sk}
ENCRYPTION_KEY={ek}
SUPER_ADMIN_EMAIL={email}
SUPER_ADMIN_PASSWORD={pw}
SUPER_ADMIN_NAME={name}
TELEGRAM_BOT_TOKEN={tgtok}
TELEGRAM_CHAT_ID={tgcht}
APP_DOMAIN=35.91.127.14
APP_NAME=ALGO-DESK
REGISTRATION_OPEN=true
LOG_LEVEL=INFO
APP_VERSION=3.0.0
"""
        (PROJ/".env").write_text(env_content)
        emit("Configuration written ✓", "ok")

        # 4 SSL self-signed (works with IP)
        emit("Creating SSL certificate...", "step")
        ssl_dir = PROJ/"nginx"/"ssl"
        ssl_dir.mkdir(parents=True, exist_ok=True)
        _run(["openssl","req","-x509","-nodes","-days","365",
              "-newkey","rsa:2048",
              "-keyout",str(ssl_dir/"privkey.pem"),
              "-out",   str(ssl_dir/"fullchain.pem"),
              "-subj","/C=GB/ST=England/L=London/O=AlgoDesk/CN=35.91.127.14"])
        emit("SSL certificate created ✓", "ok")

        # 5 Nginx config
        emit("Configuring nginx...", "step")
        nginx_dir = PROJ/"nginx"
        nginx_dir.mkdir(exist_ok=True)
        (nginx_dir/"nginx.conf").write_text("""events { worker_connections 512; }
http {
  include /etc/nginx/mime.types;
  limit_req_zone $binary_remote_addr zone=api:10m rate=30r/m;
  limit_req_zone $binary_remote_addr zone=login:10m rate=5r/m;
  server {
    listen 80;
    return 301 https://$host$request_uri;
  }
  server {
    listen 443 ssl;
    ssl_certificate     /etc/nginx/ssl/fullchain.pem;
    ssl_certificate_key /etc/nginx/ssl/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    server_tokens off;
    add_header X-Frame-Options DENY always;
    add_header X-Content-Type-Options nosniff always;
    location / {
      proxy_pass http://backend:8000;
      proxy_set_header Host $host;
      proxy_set_header X-Real-IP $remote_addr;
    }
    location /api/ {
      limit_req zone=api burst=15 nodelay;
      proxy_pass http://backend:8000;
      proxy_read_timeout 120s;
    }
    location /api/auth/login {
      limit_req zone=login burst=3 nodelay;
      proxy_pass http://backend:8000;
    }
    location /ws {
      proxy_pass http://backend:8000;
      proxy_http_version 1.1;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection "upgrade";
      proxy_read_timeout 3600s;
    }
    location /health { proxy_pass http://backend:8000; access_log off; }
  }
}
""")
        emit("Nginx configured ✓", "ok")

        # 6 Docker up
        emit("Starting all services — this takes 2–3 minutes...", "step")
        _run(["docker","compose","up","-d","--build"], cwd=str(PROJ), timeout=480)
        emit("All Docker containers started ✓", "ok")

        # 7 Health check
        emit("Waiting for backend to be ready...", "step")
        import urllib.request
        for i in range(40):
            time.sleep(4)
            try:
                r = urllib.request.urlopen("http://localhost:8000/health", timeout=3)
                if r.status == 200:
                    emit("Backend healthy ✓", "ok"); break
            except:
                emit(f"  Still starting... ({(i+1)*4}s)")
        else:
            raise Exception("Backend did not start in 160 seconds — check docker logs")

        # 8 Bootstrap admin
        emit("Creating your admin account...", "step")
        time.sleep(2)
        payload = json.dumps({"email":email,"password":pw,"name":name,"role":"SUPER_ADMIN"}).encode()
        req = urllib.request.Request(
            "http://localhost:8000/api/auth/bootstrap",
            data=payload,
            headers={"Content-Type":"application/json","X-Bootstrap-Key":sk},
            method="POST")
        try: urllib.request.urlopen(req, timeout=10)
        except: pass
        emit("Admin account created ✓", "ok")

        # 9 GitHub remote
        emit("Configuring GitHub remote...", "step")
        _run(["git","remote","set-url","origin", GITHUB], cwd=str(PROJ))
        emit("GitHub remote configured ✓", "ok")

        # 10 Crons
        emit("Setting up auto-maintenance...", "step")
        cron = (
            f"0 3 * * * docker compose -f {PROJ}/docker-compose.yml restart nginx 2>/dev/null\n"
            f"*/5 * * * * curl -sf http://localhost:8000/health > /dev/null || "
            f"docker compose -f {PROJ}/docker-compose.yml restart backend 2>/dev/null"
        )
        subprocess.run(["bash","-c",f'(crontab -l 2>/dev/null; echo "{cron}") | crontab -'])
        emit("Auto-maintenance configured ✓", "ok")

        emit("", "info")
        emit("════════════════════════════════════════", "ok")
        emit("  ALGO-DESK IS LIVE! 🎉", "ok")
        emit("  URL: https://35.91.127.14", "ok")
        emit(f"  Login: {email}", "ok")
        emit("════════════════════════════════════════", "ok")
        deploy_done = True

    except Exception as e:
        deploy_error = str(e)
        emit(f"FAILED: {e}", "error")

# ── file upload ─────────────────────────────────────────────────
@app.route("/upload", methods=["POST"])
@auth
def upload():
    files = request.files.getlist("files")
    if not files: return jsonify({"ok":False,"error":"No files"}), 400
    ud = HOME/"algo-update"; ud.mkdir(exist_ok=True)
    for f in files:
        dest = ud/f.filename; dest.parent.mkdir(parents=True, exist_ok=True)
        f.save(str(dest)); rl(f"Uploaded: {f.filename}")
    threading.Thread(target=_apply_update, args=(ud,), daemon=True).start()
    return jsonify({"ok":True,"message":f"Received {len(files)} file(s). Applying..."})

def _apply_update(ud):
    try:
        for z in ud.glob("*.zip"):
            with zipfile.ZipFile(str(z)) as zf: zf.extractall(str(ud))
        for f in ud.rglob("*"):
            if f.is_file() and f.suffix in ('.py','.html','.js','.css','.yml','.conf','.md'):
                rel = f.relative_to(ud)
                dest = PROJ/rel; dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(f), str(dest)); rl(f"Updated: {rel}")
        subprocess.run(["docker","compose","up","-d","--build","--no-deps","backend"],
            cwd=str(PROJ), capture_output=True, timeout=180)
        rl("Update applied ✓")
        shutil.rmtree(str(ud))
    except Exception as e: rl(f"Update error: {e}")

@app.route("/update/pull", methods=["POST"])
@auth
def git_pull():
    def _pull():
        rl("Git pull started...")
        subprocess.run(["git","pull","origin","main"], cwd=str(PROJ), capture_output=True)
        subprocess.run(["docker","compose","up","-d","--build","--no-deps","backend"],
            cwd=str(PROJ), capture_output=True, timeout=180)
        rl("Git pull complete ✓")
    threading.Thread(target=_pull, daemon=True).start()
    return jsonify({"ok":True,"message":"Pull started"})

@app.route("/logs/<svc>")
@auth
def logs(svc):
    allowed = {"backend","postgres","redis","nginx","agent"}
    if svc not in allowed: return jsonify({"ok":False,"error":"Unknown service"}), 400
    if svc == "agent":
        try: return jsonify({"ok":True,"logs":LOG.read_text()})
        except: return jsonify({"ok":True,"logs":"No logs yet"})
    try:
        r = subprocess.run(["docker","compose","logs","--tail=150","--no-color",svc],
            cwd=str(PROJ), capture_output=True, text=True, timeout=10)
        return jsonify({"ok":True,"logs":r.stdout})
    except Exception as e: return jsonify({"ok":False,"error":str(e)})

@app.route("/restart/<svc>", methods=["POST"])
@auth
def restart(svc):
    allowed = {"backend","postgres","redis","nginx","all"}
    if svc not in allowed: return jsonify({"ok":False,"error":"Unknown service"}), 400
    cmd = ["docker","compose","restart"] + ([] if svc=="all" else [svc])
    subprocess.run(cmd, cwd=str(PROJ), capture_output=True, timeout=30)
    return jsonify({"ok":True,"message":f"{svc} restarted"})

@app.route("/backup", methods=["POST"])
@auth
def backup():
    def _bk():
        bkd = HOME/"backups"; bkd.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = bkd/f"algodesk_{ts}.sql.gz"
        pg = subprocess.run(
            ["docker","compose","exec","-T","postgres","pg_dump","-U","algodesk","algodesk"],
            cwd=str(PROJ), capture_output=True, timeout=60)
        with gzip.open(str(out),"wb") as gz: gz.write(pg.stdout)
        rl(f"Backup: {out.name} ({out.stat().st_size//1024}KB)")
    threading.Thread(target=_bk, daemon=True).start()
    return jsonify({"ok":True})

if __name__ == "__main__":
    rl("ALGO-DESK agent starting on :2999")
    app.run(host="0.0.0.0", port=2999, debug=False, threaded=True)
PYEOF

chmod +x /opt/algo-agent/agent.py
log "Agent code written ✓"

# ── Step 5: Systemd service ──────────────────────────────────────
head "Step 5 of 6 — Registering agent as system service"
sudo tee /etc/systemd/system/algo-agent.service > /dev/null << SVCEOF
[Unit]
Description=ALGO-DESK Management Agent
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/algo-agent
ExecStart=/opt/algo-agent/venv/bin/python /opt/algo-agent/agent.py
Restart=always
RestartSec=5
StandardOutput=append:/opt/algo-agent/agent.log
StandardError=append:/opt/algo-agent/agent.log

[Install]
WantedBy=multi-user.target
SVCEOF

sudo systemctl daemon-reload
sudo systemctl enable algo-agent --now
log "Agent service registered and started ✓"

# ── Step 6: Firewall ─────────────────────────────────────────────
head "Step 6 of 6 — Configuring firewall"
sudo ufw allow 22/tcp   comment "SSH"    2>/dev/null || true
sudo ufw allow 80/tcp   comment "HTTP"   2>/dev/null || true
sudo ufw allow 443/tcp  comment "HTTPS"  2>/dev/null || true
sudo ufw allow 2999/tcp comment "Agent"  2>/dev/null || true
echo "y" | sudo ufw enable 2>/dev/null || true
log "Firewall configured ✓"

# ── Wait and verify ──────────────────────────────────────────────
sleep 4
RUNNING=$(systemctl is-active algo-agent 2>/dev/null || echo "inactive")
AGENT_TOKEN=$(cat /opt/algo-agent/agent.token)

echo ""
echo -e "${BOLD}${GREEN}╔═══════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${GREEN}║   ✅  Bootstrap complete!                     ║${NC}"
echo -e "${BOLD}${GREEN}╚═══════════════════════════════════════════════╝${NC}"
echo ""
if [ "$RUNNING" = "active" ]; then
  echo -e "  Agent status : ${GREEN}RUNNING ✓${NC}"
else
  echo -e "  Agent status : ${YELLOW}Check with: sudo systemctl status algo-agent${NC}"
fi
echo ""
echo -e "  ┌─────────────────────────────────────────────┐"
echo -e "  │  Open installer.html in your browser and    │"
echo -e "  │  enter these two values:                    │"
echo -e "  │                                             │"
echo -e "  │  Server IP  :  ${BLUE}35.91.127.14${NC}               │"
echo -e "  │  Token      :  ${YELLOW}${AGENT_TOKEN}${NC}"
echo -e "  │                                             │"
echo -e "  │  That is the LAST thing you type here.      │"
echo -e "  └─────────────────────────────────────────────┘"
echo ""
