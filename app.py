import os
import json
import signal
import subprocess
import shutil
import zipfile
import hashlib
import psutil
import threading
import time
import requests
import sys
import re
from pathlib import Path
from functools import wraps
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file, abort, Response
import io

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "super-secret-vps-key-2026")
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB Upload Limit

# --- Persistent Directory Setup ---
PERSISTENT_DIR = os.environ.get("RENDER_DISK_PATH", Path(__file__).parent)
BASE_DIR = Path(PERSISTENT_DIR)

DATA_FILE = BASE_DIR / "data.json"
SERVERS_DIR = BASE_DIR / "servers"
SERVERS_DIR.mkdir(parents=True, exist_ok=True)

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "SHAPPNO004XX")

DATA_LOCK = threading.Lock()
RUNNING_PROCESSES = {}
RESET_TIMERS = {}
WATCHDOG_THREADS = {}

# ─── Data & Settings ───
def load_data():
    with DATA_LOCK:
        if DATA_FILE.exists():
            try: return json.loads(DATA_FILE.read_text())
            except: pass
        return {
            "servers": {}, "users": {},
            "settings": {
                "site_name": "—͞SᎻꫝᎮᎮƝ᥆ꤪꤨꤨ  𝐂𝚘𝙳𝚎𝚡",
                "theme_color": "#00ff41",
                "free_user_password": "freeuser",
                "maintenance": False
            }
        }

def save_data(data):
    with DATA_LOCK:
        DATA_FILE.write_text(json.dumps(data, indent=2, default=str))

@app.context_processor
def inject_globals():
    data = load_data()
    return {
        "theme_color": data["settings"].get("theme_color", "#00ff41"),
        "site_name": data["settings"].get("site_name", "—͞SᎻꫝᎮᎮƝ᥆ꤪꤨꤨ  𝐂𝚘𝙳𝚎𝚡"),
        "free_user_password": data["settings"].get("free_user_password", "freeuser")
    }

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("username"): return redirect(url_for("login"))
        data = load_data()
        if data["settings"].get("maintenance") and session.get("username") != "__admin__":
            return render_template("maintenance.html", message="SYSTEM UNDER MAINTENANCE")
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin"): return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated

# ─── File Management Helper ───
def list_files(directory, base=""):
    result = []
    if not directory.exists(): return result
    try:
        for entry in sorted(directory.iterdir(), key=lambda e: (e.is_file(), e.name)):
            rel = f"{base}/{entry.name}" if base else entry.name
            if entry.is_dir():
                result.append({"name": entry.name, "path": rel, "type": "dir", "size": 0})
                result.extend(list_files(entry, rel))
            else:
                result.append({"name": entry.name, "path": rel, "type": "file", "size": entry.stat().st_size})
    except: pass
    return result

# ─── AI Auto-Package Installer ───
def auto_install_packages(script_path, log_file):
    try:
        if not str(script_path).endswith('.py'): return
        with open(script_path, 'r', errors='ignore') as f: content = f.read()
        
        imports = re.findall(r'^(?:import|from)\s+([a-zA-Z0-9_]+)', content, re.MULTILINE)
        builtins = sys.builtin_module_names
        ignore_list = ['os', 'sys', 'time', 'json', 'threading', 'requests', 'flask', 'datetime', 'math', 're', 'subprocess', 'random', 'asyncio', 'io', 'base64']
        
        for imp in set(imports):
            if imp not in builtins and imp not in ignore_list:
                pkg = imp
                if imp == 'telegram': pkg = 'python-telegram-bot'
                elif imp == 'bs4': pkg = 'beautifulsoup4'
                elif imp == 'telebot': pkg = 'pyTelegramBotAPI'
                elif imp == 'telethon': pkg = 'telethon'
                elif imp == 'pyrogram': pkg = 'pyrogram'
                
                try: __import__(imp)
                except ImportError:
                    msg = f"\n[⚙️ SYSTEM] Auto-Installing missing package: {pkg}...\n"
                    if log_file: log_file.write(msg); log_file.flush()
                    subprocess.run([sys.executable, "-m", "pip", "install", pkg])
    except Exception as e:
        if log_file: log_file.write(f"\n[!] Auto-install error: {e}\n")

# ─── Process & Watchdog Engine ───
def is_process_alive(pid):
    try: return psutil.Process(pid).is_running() and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except: return False

def kill_process(pid):
    try:
        p = psutil.Process(pid)
        for child in p.children(recursive=True): child.terminate()
        p.terminate()
        p.wait(timeout=3)
    except: pass

def _silent_start(name):
    try:
        data = load_data()
        cfg = data["servers"].get(name)
        if not cfg: return
        ext_dir = SERVERS_DIR / name / "extracted"
        main_file = cfg.get("main_file") or "main.py"
        main_path = ext_dir / main_file
        log_path = SERVERS_DIR / name / "logs.txt"
        
        if main_path.exists():
            log_file = open(log_path, "a")
            # Auto Install Magic Trigger
            auto_install_packages(main_path, log_file)
            
            log_file.write(f"\n[{datetime.now().isoformat()}] Engine Started...\n")
            cmd = ["node", main_file] if main_file.endswith((".js")) else ["python", "-u", main_file]
            env = os.environ.copy()
            env["PORT"] = str(cfg.get("port", 8080))
            
            proc = subprocess.Popen(cmd, cwd=str(ext_dir), stdout=log_file, stderr=log_file, env=env, preexec_fn=os.setsid)
            RUNNING_PROCESSES[name] = {"proc": proc, "log_file": log_file}
            cfg["status"] = "running"
            cfg["pid"] = proc.pid
            data["servers"][name] = cfg
            save_data(data)
    except: pass

def global_process_watchdog(name):
    while True:
        time.sleep(5)
        data = load_data()
        cfg = data["servers"].get(name)
        if not cfg or cfg.get("status") != "running": break
        if not cfg.get("pid") or not is_process_alive(cfg.get("pid")):
            _silent_start(name)

def _do_auto_reset(name):
    data = load_data(); cfg = data["servers"].get(name)
    if not cfg: return
    if name in RUNNING_PROCESSES:
        try: os.killpg(os.getpgid(RUNNING_PROCESSES[name]["proc"].pid), signal.SIGTERM)
        except: pass
        del RUNNING_PROCESSES[name]
    elif cfg.get("pid"): kill_process(cfg.get("pid"))
    
    _silent_start(name)
    secs = int(cfg.get("auto_reset", {}).get("seconds", 0))
    if secs > 0: _schedule_reset(name, secs)

def _schedule_reset(name, secs):
    if name in RESET_TIMERS: RESET_TIMERS[name]["timer"].cancel()
    t = threading.Timer(secs, _do_auto_reset, args=[name])
    t.daemon = True; t.start()
    RESET_TIMERS[name] = {"timer": t, "total_seconds": secs}

def _init_boot_recovery():
    time.sleep(2)
    data = load_data()
    for name, cfg in data["servers"].items():
        if cfg.get("status") == "running":
            _silent_start(name)
            t = threading.Thread(target=global_process_watchdog, args=(name,), daemon=True)
            WATCHDOG_THREADS[name] = t; t.start()

threading.Thread(target=_init_boot_recovery, daemon=True).start()

# ─── Auth Routes ───
@app.route("/")
def index(): return redirect(url_for("dashboard")) if session.get("username") else redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    data = load_data()
    if data["settings"].get("maintenance") and not session.get("admin"):
        return render_template("maintenance.html", message=data["settings"].get("maintenance_msg", "OFFLINE"))
        
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        global_free_password = data["settings"].get("free_user_password", "freeuser")
        
        if password == global_free_password and username:
            if username not in data["users"]:
                data["users"][username] = {"joined": datetime.now().isoformat()}
                save_data(data)
            session["username"] = username
            return redirect(url_for("dashboard"))
        return render_template("login.html", error="Invalid Free User Password")
    return render_template("login.html", error=None)

@app.route("/logout")
def logout(): session.clear(); return redirect(url_for("login"))

# ─── Dashboard Routes ───
@app.route("/dashboard")
@login_required
def dashboard():
    data = load_data()
    user_servers = {k: v for k, v in data["servers"].items() if v.get("owner") == session["username"]}
    running = sum(1 for v in user_servers.values() if v.get("status") == "running")
    return render_template("dashboard.html", servers=user_servers, running=running, total=len(user_servers), username=session["username"])

@app.route("/api/stats")
@login_required
def stats(): return jsonify({"cpu": psutil.cpu_percent(), "ram": psutil.virtual_memory().percent, "disk": psutil.disk_usage("/").percent})

# ─── Server Management Routes ───
@app.route("/server/create", methods=["POST"])
@login_required
def create_server():
    name = request.form.get("name", "").strip().replace(" ", "-")
    data = load_data()
    if name and name not in data["servers"]:
        port = 10000 + len(data["servers"]) + 1
        data["servers"][name] = {
            "name": name, "owner": session["username"], "runtime": request.form.get("runtime", "python"),
            "status": "stopped", "main_file": "", "port": port, "pid": None, "auto_reset": {"enabled": False, "seconds": 0}, "packages": []
        }
        save_data(data)
        (SERVERS_DIR / name / "extracted").mkdir(parents=True, exist_ok=True)
    return redirect(url_for("dashboard"))

@app.route("/server/<name>")
@login_required
def server_detail(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg: return "Not found", 404
    ed = SERVERS_DIR / name / "extracted"
    files = list_files(ed)
    return render_template("server.html", server_name=name, config=cfg, files=files)

@app.route("/server/<name>/upload", methods=["POST"])
@login_required
def upload(name):
    data = load_data()
    f = request.files.get("file")
    cfg = data["servers"].get(name)
    if not cfg or not f: return jsonify({"success": False})
    
    ext_dir = SERVERS_DIR / name / "extracted"
    upload_path = SERVERS_DIR / name / f.filename
    f.save(upload_path)
    
    if f.filename.endswith(".zip"):
        with zipfile.ZipFile(upload_path, "r") as z: z.extractall(ext_dir)
        upload_path.unlink()
    else:
        shutil.copy(upload_path, ext_dir / f.filename)
        if not cfg.get("main_file") and f.filename.endswith((".py", ".js")): 
            cfg["main_file"] = f.filename; save_data(data)
        upload_path.unlink()
    return jsonify({"success": True})

@app.route("/server/<name>/start", methods=["POST"])
@login_required
def start(name):
    _do_auto_reset(name)
    t = threading.Thread(target=global_process_watchdog, args=(name,), daemon=True)
    WATCHDOG_THREADS[name] = t; t.start()
    return jsonify({"success": True})

@app.route("/server/<name>/stop", methods=["POST"])
@login_required
def stop(name):
    data = load_data(); cfg = data["servers"].get(name)
    if name in RUNNING_PROCESSES:
        try: os.killpg(os.getpgid(RUNNING_PROCESSES[name]["proc"].pid), signal.SIGTERM)
        except: pass
        del RUNNING_PROCESSES[name]
    if cfg.get("pid"): kill_process(cfg.get("pid"))
    cfg["status"] = "stopped"; cfg["pid"] = None; save_data(data)
    return jsonify({"success": True})

@app.route("/server/<name>/logs")
@login_required
def get_logs(name):
    lp = SERVERS_DIR / name / "logs.txt"
    return jsonify({"logs": lp.read_text(errors="replace")[-5000:] if lp.exists() else "No logs yet."})

@app.route("/server/<name>/auto-reset/settings", methods=["POST"])
@login_required
def auto_reset_settings(name):
    data = load_data(); cfg = data["servers"].get(name); pl = request.json
    cfg["auto_reset"] = {"enabled": pl.get("enabled", False), "seconds": int(pl.get("seconds", 0))}
    save_data(data)
    if cfg["auto_reset"]["enabled"]: _schedule_reset(name, cfg["auto_reset"]["seconds"])
    return jsonify({"success": True})

@app.route("/server/<name>/settings", methods=["POST"])
@login_required
def save_settings(name):
    data = load_data(); cfg = data["servers"].get(name); payload = request.json
    cfg["main_file"] = payload.get("main_file", cfg.get("main_file", ""))
    save_data(data)
    return jsonify({"success": True})

# ─── Package Routes ───
@app.route("/server/<name>/packages/install", methods=["POST"])
@login_required
def install_package(name):
    data = load_data(); cfg = data["servers"].get(name); pl = request.json
    pkg = pl.get("name", "").strip()
    if not pkg: return jsonify({"success": False})
    
    lp = SERVERS_DIR / name / "logs.txt"
    try:
        with open(lp, "a") as f:
            f.write(f"\n[SYSTEM] Manual Install: {pkg}...\n")
        subprocess.run(["pip", "install", pkg], stdout=open(lp, "a"), stderr=subprocess.STDOUT)
    except: pass
    return jsonify({"success": True})

# ─── Termux-like Tunnel ───
@app.route("/proxy/<name>/", defaults={"path": ""}, methods=["GET", "POST", "PUT"])
@app.route("/proxy/<name>/<path:path>", methods=["GET", "POST", "PUT"])
def proxy(name, path):
    cfg = load_data()["servers"].get(name)
    if not cfg or cfg.get("status") != "running": return "Project Offline", 503
    try:
        resp = requests.request(method=request.method, url=f"http://127.0.0.1:{cfg['port']}/{path}",
                                headers={k:v for k,v in request.headers if k.lower() != 'host'},
                                data=request.get_data(), params=request.args, timeout=10)
        return Response(resp.content, resp.status_code)
    except: return "Proxy Gateway Timeout", 502

# ─── Admin Panel ───
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True; return redirect(url_for("admin"))
        return render_template("admin_login.html", error="Wrong Master Password")
    return render_template("admin_login.html")

@app.route("/admin")
@admin_required
def admin(): return render_template("admin.html", data=load_data())

@app.route("/admin/settings/save", methods=["POST"])
@admin_required
def admin_settings_save():
    data = load_data(); pl = request.json
    if "site_name" in pl: data["settings"]["site_name"] = pl["site_name"]
    if "free_user_password" in pl: data["settings"]["free_user_password"] = pl["free_user_password"]
    if "theme_color" in pl: data["settings"]["theme_color"] = pl["theme_color"]
    save_data(data)
    return jsonify({"success": True})

@app.route("/admin/user/<username>/files")
@admin_required
def admin_user_files(username):
    data = load_data()
    user_servers = {k: v for k, v in data["servers"].items() if v.get("owner") == username}
    file_data = {}
    for name, cfg in user_servers.items():
        ed = SERVERS_DIR / name / "extracted"
        file_data[name] = {"config": cfg, "files": list_files(ed)}
    return render_template("admin_files.html", username=username, file_data=file_data)

@app.route("/admin/user/<username>/delete", methods=["POST"])
@admin_required
def admin_delete_user(username):
    data = load_data()
    to_delete = [k for k, v in data["servers"].items() if v.get("owner") == username]
    for name in to_delete:
        if data["servers"][name].get("pid"): kill_process(data["servers"][name].get("pid"))
        shutil.rmtree(SERVERS_DIR / name, ignore_errors=True)
        del data["servers"][name]
    data["users"].pop(username, None)
    save_data(data)
    return redirect(url_for("admin"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)