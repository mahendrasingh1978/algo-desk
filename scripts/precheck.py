#!/usr/bin/env python3
"""
ALGO-DESK Pre-deployment Check
================================
Run this before every git push to catch errors before they hit the server.
Usage: python3 scripts/precheck.py

Checks:
1. Syntax errors in all Python files
2. SQLAlchemy models used as FastAPI return types (causes 502 on startup)
3. Duplicate FastAPI endpoints (causes startup failure)
4. Missing imports (cross-file consistency)
5. Duplicate JavaScript functions in index.html
6. Unbalanced JS braces
7. Quote errors in JS strings
8. All nav pages exist in HTML
"""

import ast, re, sys
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).parent.parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"

errors = []
warnings = []

def err(msg): errors.append(f"❌ {msg}")
def warn(msg): warnings.append(f"⚠️  {msg}")
def ok(msg): print(f"   ✅ {msg}")

print("\n═══════════════════════════════════════")
print("  ALGO-DESK Pre-deployment Check")
print("═══════════════════════════════════════\n")

# ── 1. Python syntax ─────────────────────────────────────────
print("1. Python syntax check...")
py_files = list(BACKEND.glob("*.py"))
for f in py_files:
    try:
        ast.parse(f.read_text())
        ok(f.name)
    except SyntaxError as e:
        err(f"{f.name} line {e.lineno}: {e.msg}")

# ── 2. SQLAlchemy models as FastAPI return types ─────────────
print("\n2. FastAPI return type annotations...")
SQLA_MODELS = {'Automation','Trade','ShadowTrade','BrokerConnection',
               'BrokerDefinition','User','ResetToken','InviteLink'}
main_src = (BACKEND / "main.py").read_text()
# Find route handler functions with SQLAlchemy return type
route_funcs = re.findall(
    r'@app\.(get|post|put|delete)\([^)]+\).*?\ndef (\w+)\([^)]*\)\s*->\s*(\w+)',
    main_src, re.DOTALL)
for method, fname, ret_type in route_funcs:
    if ret_type in SQLA_MODELS:
        err(f"Endpoint '{fname}' returns SQLAlchemy model '{ret_type}' — "
            f"FastAPI will crash. Remove the return annotation or return a dict.")
    else:
        ok(f"Endpoint '{fname}' return type OK")

# Also check non-route functions that FastAPI might introspect
dep_funcs = re.findall(
    r'def (\w+)\([^)]*Depends[^)]*\)\s*->\s*(\w+)',
    main_src)
for fname, ret_type in dep_funcs:
    if ret_type in SQLA_MODELS:
        err(f"Dependency '{fname}' returns SQLAlchemy model '{ret_type}' — "
            f"remove the return annotation")
    else:
        ok(f"Dependency '{fname}' return type OK")

# ── 3. Duplicate endpoints ───────────────────────────────────
print("\n3. Duplicate endpoint check...")
routes = re.findall(r'@app\.(get|post|put|delete|websocket)\("([^"]+)"', main_src)
counts = Counter(f"{m} {p}" for m, p in routes)
dupes = {k: v for k, v in counts.items() if v > 1}
if dupes:
    for k, v in dupes.items():
        err(f"Duplicate endpoint x{v}: {k}")
else:
    ok(f"No duplicate endpoints ({len(routes)} total)")

# ── 4. Cross-file import consistency ────────────────────────
print("\n4. Import consistency check...")
models_src = (BACKEND / "models.py").read_text()
engine_src  = (BACKEND / "engine.py").read_text()
fyers_src   = (BACKEND / "fyers.py").read_text()

for pattern, src, src_name in [
    (r'from models import (.+)', models_src, 'models.py'),
    (r'from engine import (.+)', engine_src, 'engine.py'),
    (r'from fyers import (.+)',  fyers_src,  'fyers.py'),
]:
    m = re.search(pattern, main_src)
    if m:
        imports = [x.strip() for x in m.group(1).split(',')]
        for imp in imports:
            if (f'class {imp}' in src or f'def {imp}' in src or
                    f'{imp} =' in src):
                ok(f"{imp} from {src_name}")
            else:
                err(f"{imp} imported from {src_name} but not found there")

# ── 5. Stale references ──────────────────────────────────────
print("\n5. Stale reference check...")
stale_refs = [
    ('SimulationTrade', main_src, 'main.py'),
    ('run_simulation_service', main_src, 'main.py'),
    ('sim_sessions', main_src, 'main.py'),
]
for ref, src, fname in stale_refs:
    count = src.count(ref)
    if count:
        err(f"Stale reference '{ref}' found {count}x in {fname}")
    else:
        ok(f"No stale '{ref}' in {fname}")

# ── 6. Frontend JS checks ────────────────────────────────────
print("\n6. Frontend JavaScript check...")
html = (FRONTEND / "index.html").read_text()
scripts = re.findall(r'<script>(.*?)</script>', html, re.DOTALL)
js = '\n'.join(scripts)

# Duplicate functions
funcs = re.findall(r'^(?:async )?function (\w+)', js, re.MULTILINE)
dupes_js = {f: c for f, c in Counter(funcs).items() if c > 1}
if dupes_js:
    for f, c in dupes_js.items():
        err(f"Duplicate JS function x{c}: {f}()")
else:
    ok(f"No duplicate JS functions ({len(funcs)} total)")

# Brace balance
opens  = js.count('{')
closes = js.count('}')
if opens != closes:
    err(f"Unbalanced JS braces: {opens} open, {closes} close")
else:
    ok(f"Balanced braces ({opens})")

# Check for unescaped onclick nav() calls inside single-quoted JS strings
# e.g. + '<button onclick="nav('automate')"> — breaks the outer string
import re as _re
nav_in_str = []
for i, line in enumerate(js.split('\n'), 1):
    stripped = line.strip()
    if (stripped.startswith("+ '") or stripped.startswith("'<")):
        if _re.search(r"nav\('[^'\\\\]+'\)", line):
            nav_in_str.append(f"Line {i}: {stripped[:80]}")
if nav_in_str:
    for n in nav_in_str:
        err(f"Unescaped nav() in JS string — {n}")
else:
    ok("No unescaped nav() calls in JS strings")

# Quote errors
js_lines = js.split('\n')
quote_errors = []
for i, line in enumerate(js_lines, 1):
    if '`' in line or line.strip().startswith('//'):
        continue
    clean = line.replace("\\'", "XX").replace('\\"', "XX")
    in_dq = in_sq = False
    for ch in clean:
        if ch == '"' and not in_sq: in_dq = not in_dq
        elif ch == "'" and not in_dq: in_sq = not in_sq
    if in_sq:
        quote_errors.append(f"Line {i}: {line.strip()[:60]}")
if quote_errors:
    for e in quote_errors[:5]:
        err(f"Quote error — {e}")
else:
    ok("No JS quote errors")

# ── 7. Nav pages ─────────────────────────────────────────────
print("\n7. Frontend nav/page check...")
pages = ['dashboard','brokers','automate','live','trades',
         'performance','help','profile','admin']
for p in pages:
    has_page = f'id="pg-{p}"' in html
    has_nav  = f"nav('{p}')" in html or f'nav("{p}")' in html
    if has_page and has_nav:
        ok(p)
    else:
        err(f"Page '{p}': div={has_page} nav={has_nav}")

# ── Summary ──────────────────────────────────────────────────
print("\n═══════════════════════════════════════")
if errors:
    print(f"  FAILED — {len(errors)} error(s) found:\n")
    for e in errors:
        print(f"  {e}")
    if warnings:
        print(f"\n  {len(warnings)} warning(s):")
        for w in warnings:
            print(f"  {w}")
    print("\n  DO NOT DEPLOY until errors are fixed.\n")
    sys.exit(1)
else:
    if warnings:
        print(f"  PASSED with {len(warnings)} warning(s):")
        for w in warnings:
            print(f"  {w}")
    else:
        print("  ALL CHECKS PASSED — safe to deploy.")
    print("═══════════════════════════════════════\n")
    sys.exit(0)
