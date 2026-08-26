import os
import re
import io
import sys
import json
import time
import shutil
import hashlib
import secrets
import zipfile
import subprocess
import threading
import collections
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
import ssl
from zeroconf import Zeroconf, ServiceInfo
import socket
import getpass
PASSWORD = "change-me"
PANEL_VERSION = "3.2.1"
RELEASE_NOTE = "Implementation of the translation system. \n Added the ability to download and update languages ​​from GitHub."
HOST = "0.0.0.0"
PORT = 8877
LUANTI_BIN = "luanti"
WORLD_DIR = os.path.expanduser("~/.minetest/worlds/world")
MODS_DIR = os.path.expanduser("~/.minetest/mods")
FILES_ROOT = os.path.expanduser("~/.minetest")
CONFIG_FILE = os.path.expanduser("~/.minetest/minetest.conf")
DEBUG_FILE = os.path.expanduser("~/.minetest/debug.txt")
EXTRA_ARGS = ["--server", "--world", WORLD_DIR]
SCRIPT_PATH = os.path.abspath(__file__)
UPDATE_URL = "https://raw.githubusercontent.com/survivalier/Luanti-Panel/refs/heads/main/main.py"
LANG_REPO_API_URL = "https://api.github.com/repos/survivalier/Luanti-Panel/contents/lang"
LANG_REPO_RAW_BASE = "https://raw.githubusercontent.com/survivalier/Luanti-Panel/main/lang/"
CONSOLE_BUFFER_SIZE = 2000
PASSWORD_HASH = hashlib.sha256(PASSWORD.encode()).hexdigest()
SESSIONS = {}
SESSION_DURATION = 12 * 3600  # 12h
console_buffer = collections.deque(maxlen=CONSOLE_BUFFER_SIZE)
console_lock = threading.Lock()
console_subscribers = []
server_process = None
server_lock = threading.Lock()
server_start_time = None
def console_push(line):
    with console_lock:
        console_buffer.append(line)
        for q in console_subscribers:
            q.append(line)
def reader_thread(proc):
    for raw in iter(proc.stdout.readline, b""):
        try:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
        except Exception:
            line = repr(raw)
        console_push(line)
    console_push("[panel] The server process has stopped.")
def start_server():
    global server_process, server_start_time
    with server_lock:
        if server_process is not None and server_process.poll() is None:
            return False, "The server is already running."
        cmd = [LUANTI_BIN] + EXTRA_ARGS
        try:
            server_process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                cwd=os.path.expanduser("~"),
            )
        except FileNotFoundError:
            return False, f"Binary not found : {LUANTI_BIN}"
        server_start_time = time.time()
        t = threading.Thread(target=reader_thread, args=(server_process,), daemon=True)
        t.start()
        console_push(f"[panel] Server started (pid={server_process.pid}).")
        return True, "Server started."
def stop_server():
    global server_process
    with server_lock:
        if server_process is None or server_process.poll() is not None:
            return False, "The server is not running."
        try:
            server_process.stdin.write(b"/shutdown\n")
            server_process.stdin.flush()
        except Exception:
            pass
        try:
            server_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server_process.terminate()
            try:
                server_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server_process.kill()
        console_push("[panel] Server stopped.")
        return True, "Server stopped."
def restart_server():
    with server_lock:
        running = server_process is not None and server_process.poll() is None
    if running:
        stop_server()
    return start_server()
def server_status():
    with server_lock:
        running = server_process is not None and server_process.poll() is None
        pid = server_process.pid if running else None
        uptime = int(time.time() - server_start_time) if running and server_start_time else 0
        return {"running": running, "pid": pid, "uptime": uptime}
def send_command(cmd_text):
    with server_lock:
        if server_process is None or server_process.poll() is not None:
            return False, "Server is not running."
        try:
            server_process.stdin.write((cmd_text + "\n").encode("utf-8"))
            server_process.stdin.flush()
            console_push(f"> {cmd_text}")
            return True, "Order sent."
        except Exception as e:
            return False, str(e)
def download_panel_update():
    """Download the latest version of the panel script from GitHub."""
    req = Request(UPDATE_URL, headers={"User-Agent": "LuantiPanel-Updater"})
    try:
        with urlopen(req, timeout=30) as resp:
            data = resp.read()
    except HTTPError as e:
        raise RuntimeError(f"Download failed (HTTP {e.code}).")
    except URLError as e:
        raise RuntimeError(f"Download failed : {e.reason}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise RuntimeError("The downloaded file is not a valid text script.")
    if "class Handler" not in text or "PASSWORD" not in text:
        raise RuntimeError("The downloaded file does not look like a valid panel script.")
    return text
def extract_panel_version(source_text):
    """Extracts the value of PANEL_VERSION from the source code of a panel script."""
    m = re.search(r'^PANEL_VERSION\s*=\s*"([^"]*)"\s*$', source_text, re.MULTILINE)
    if not m:
        raise RuntimeError("Unable to determine the version of the downloaded file.")
    return m.group(1)
def extract_release_note(source_text):
    """Extracts the value of RELEASE_NOTE from the source code of a panel script.
Returns an empty string if the field is missing (older panel versions)."""
    m = re.search(r'^RELEASE_NOTE\s*=\s*"([^"]*)"\s*$', source_text, re.MULTILINE)
    return m.group(1) if m else ""
def parse_version_tuple(v):
    """Converts a version string (ex: '1.2.3') to a comparable integer tuple. 
Non-numeric segments are ignored."""
    parts = []
    for chunk in re.split(r"[.\-+]", v or ""):
        m = re.match(r"\d+", chunk)
        parts.append(int(m.group(0)) if m else 0)
    return tuple(parts) if parts else (0,)
def check_for_update():
    """Compares the local version of the panel to the one available on GitHub."""
    source = download_panel_update()
    remote_version = extract_panel_version(source)
    remote_release_note = extract_release_note(source)
    local_t = parse_version_tuple(PANEL_VERSION)
    remote_t = parse_version_tuple(remote_version)
    return {
        "current_version": PANEL_VERSION,
        "remote_version": remote_version,
        "current_release_note": RELEASE_NOTE,
        "remote_release_note": remote_release_note,
        "up_to_date": remote_t <= local_t,
        "update_available": remote_t > local_t,
    }
def apply_new_password(source_text, new_password):
    """Replaces the PASSWORD line = "..." of the script downloaded by the new chosen password."""
    escaped = new_password.replace("\\", "\\\\").replace('"', '\\"')
    pattern = re.compile(r'^PASSWORD\s*=\s*".*"\s*$', re.MULTILINE)
    if not pattern.search(source_text):
        raise ValueError("Unable to locate PASSWORD line in new file.")
    return pattern.sub(f'PASSWORD = "{escaped}"', source_text, count=1)
def write_panel_source(new_source):
    """Save the old script then write the new version to disk."""
    backup_path = SCRIPT_PATH + ".bak"
    try:
        shutil.copy2(SCRIPT_PATH, backup_path)
    except OSError:
        pass
    tmp_path = SCRIPT_PATH + ".new"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(new_source)
    os.replace(tmp_path, SCRIPT_PATH)
def schedule_panel_restart(delay=0.6):
    """Restarts the panel process (re-exec) after a short delay, to let 
time for the HTTP update response to be sent to the browser."""
    def _do_restart():
        time.sleep(delay)
        console_push("[panel] Restarting the panel after updating…")
        os.execv(sys.executable, [sys.executable] + sys.argv)
    threading.Thread(target=_do_restart, daemon=True).start()
def schedule_panel_shutdown(delay=0.5):
    """Completely stops the panel process after a short delay."""
    def _do_shutdown():
        time.sleep(delay)
        console_push("[panel] Panel extinction requested.")
        os._exit(0)
    threading.Thread(target=_do_shutdown, daemon=True).start()
LANG_DIR = os.path.join(os.path.dirname(SCRIPT_PATH), "lang")
def safe_lang_path(code):
    """Prevents escaping the LANG_DIR folder (path traversal) for translation files."""
    code = re.sub(r"[^a-zA-Z0-9_-]", "", code or "")
    if not code:
        raise ValueError("Invalid language code.")
    full = os.path.normpath(os.path.join(LANG_DIR, code + ".json"))
    base = os.path.normpath(LANG_DIR)
    if not full.startswith(base + os.sep):
        raise ValueError("Invalid path.")
    return full
def list_available_langs():
    """Scans LANG_DIR and returns the list of available languages ​​(translation addons)."""
    if not os.path.isdir(LANG_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(LANG_DIR)):
        if not fn.endswith(".json"):
            continue
        code = fn[:-5]
        name = code
        flag = ""
        try:
            with open(os.path.join(LANG_DIR, fn), encoding="utf-8") as f:
                meta = json.load(f).get("_meta", {})
            name = meta.get("name", code)
            flag = meta.get("flag", "")
        except Exception:
            pass
        out.append({"code": code, "name": name, "flag": flag})
    return out
def get_local_lang_hash(code):
    """Computes the SHA256 hash of a locally installed language file."""
    try:
        full = safe_lang_path(code)
        if not os.path.isfile(full):
            return None
        with open(full, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception:
        return None
def get_remote_lang_hash(code):
    """Fetches the SHA256 hash of a language file from GitHub."""
    try:
        req = Request(LANG_REPO_RAW_BASE + code + ".json", headers={"User-Agent": "LuantiPanel-LangBrowser"})
        with urlopen(req, timeout=15) as resp:
            return hashlib.sha256(resp.read()).hexdigest()
    except Exception:
        return None
def lang_update_available(code):
    """Checks if a language has an update available on GitHub."""
    local_hash = get_local_lang_hash(code)
    if not local_hash:
        return False
    remote_hash = get_remote_lang_hash(code)
    return remote_hash and remote_hash != local_hash
def list_remote_langs():
    """Lists the .json language files available in the project's GitHub repository,
reads each file's _meta (name, flag), and flags which ones are already installed locally."""
    req = Request(LANG_REPO_API_URL, headers={"User-Agent": "LuantiPanel-LangBrowser", "Accept": "application/vnd.github+json"})
    try:
        with urlopen(req, timeout=15) as resp:
            entries = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        raise RuntimeError(f"Download failed (HTTP {e.code}).")
    except URLError as e:
        raise RuntimeError(f"Download failed : {e.reason}")
    installed_codes = {l["code"] for l in list_available_langs()}
    out = []
    for entry in entries:
        fn = entry.get("name", "")
        if not fn.endswith(".json"):
            continue
        code = fn[:-5]
        name, flag = code, ""
        try:
            raw_req = Request(LANG_REPO_RAW_BASE + fn, headers={"User-Agent": "LuantiPanel-LangBrowser"})
            with urlopen(raw_req, timeout=15) as raw_resp:
                meta = json.loads(raw_resp.read().decode("utf-8")).get("_meta", {})
            name = meta.get("name", code)
            flag = meta.get("flag", "")
        except Exception:
            pass
        is_installed = code in installed_codes
        has_update = is_installed and lang_update_available(code)
        out.append({"code": code, "name": name, "flag": flag, "installed": is_installed, "has_update": has_update})
    out.sort(key=lambda l: l["name"].lower())
    return out
def download_remote_lang(code):
    """Downloads a single language file from the GitHub repository and installs it into LANG_DIR."""
    full = safe_lang_path(code)
    req = Request(LANG_REPO_RAW_BASE + code + ".json", headers={"User-Agent": "LuantiPanel-LangBrowser"})
    try:
        with urlopen(req, timeout=15) as resp:
            raw = resp.read()
    except HTTPError as e:
        raise RuntimeError(f"Download failed (HTTP {e.code}).")
    except URLError as e:
        raise RuntimeError(f"Download failed : {e.reason}")
    try:
        json.loads(raw.decode("utf-8"))
    except Exception:
        raise RuntimeError("The downloaded file is not valid JSON.")
    os.makedirs(LANG_DIR, exist_ok=True)
    tmp_path = full + ".tmp"
    with open(tmp_path, "wb") as f:
        f.write(raw)
    os.replace(tmp_path, full)
def safe_mod_path(relfolder):
    """Prevents any escape from the MODS_DIR folder (path traversal). 
Accepts either a standalone mod ("mymod") or a submod of a modpack 
("modpack/submod") — never more than one level of nesting."""
    relfolder = (relfolder or "").strip().strip("/")
    parts = [p for p in relfolder.split("/") if p]
    if not parts or len(parts) > 2 or any(p in (".", "..") for p in parts):
        raise ValueError("Invalid mod name.")
    full = os.path.normpath(os.path.join(MODS_DIR, *parts))
    base = os.path.normpath(MODS_DIR)
    if full != base and not full.startswith(base + os.sep):
        raise ValueError("Invalid path.")
    return full
def world_mt_path():
    return os.path.join(WORLD_DIR, "world.mt")
def read_world_mt():
    path = world_mt_path()
    lines = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    return lines
def write_world_mt(lines):
    os.makedirs(WORLD_DIR, exist_ok=True)
    with open(world_mt_path(), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
def get_enabled_mods():
    """Returns {mod_technical_name: bool enabled}. 
In world.mt, a mod is considered activated as soon as its value is not 
"false" — for a standalone mod the value is "true", but for a mod 
being part of a modpack, Luanti stores its relative path instead, 
ex: load_mod_nations_chat = mods/example
    """
    enabled = {}
    for line in read_world_mt():
        m = re.match(r"^\s*load_mod_(.+?)\s*=\s*(.*)$", line)
        if m:
            val = m.group(2).strip()
            enabled[m.group(1)] = val != "" and val.lower() != "false"
    return enabled
def set_mod_enabled(modname, enabled, rel_path=None):
    """Enables/disables a mod in world.mt. 
rel_path (ex: "mods/nationsmod/nations_chat") must be provided for a 
mod belonging to a modpack; otherwise the value "true" is used."""
    lines = read_world_mt()
    key = f"load_mod_{modname}"
    new_value = (rel_path if rel_path else "true") if enabled else "false"
    found = False
    new_lines = []
    for line in lines:
        m = re.match(rf"^\s*{re.escape(key)}\s*=", line, re.IGNORECASE)
        if m:
            new_lines.append(f"{key} = {new_value}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"{key} = {new_value}")
    write_world_mt(new_lines)
def is_modpack_dir(path):
    return os.path.isfile(os.path.join(path, "modpack.conf")) or os.path.isfile(os.path.join(path, "modpack.txt"))
def is_mod_dir(path):
    return os.path.isfile(os.path.join(path, "init.lua")) or os.path.isfile(os.path.join(path, "mod.conf"))
def mod_technical_name(path, fallback):
    """Reads the name declared in mod.conf (name = ...) if present, otherwise 
falls back on the folder name."""
    conf = os.path.join(path, "mod.conf")
    if os.path.isfile(conf):
        try:
            with open(conf, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = re.match(r"^\s*name\s*=\s*(.+?)\s*$", line)
                    if m:
                        return m.group(1)
        except OSError:
            pass
    return fallback
def list_mods():
    os.makedirs(MODS_DIR, exist_ok=True)
    enabled_map = get_enabled_mods()
    mods = []
    for entry in sorted(os.listdir(MODS_DIR)):
        full = os.path.join(MODS_DIR, entry)
        if not os.path.isdir(full) or entry.startswith("_tmp_"):
            continue
        if is_modpack_dir(full):
            for sub in sorted(os.listdir(full)):
                subfull = os.path.join(full, sub)
                if not os.path.isdir(subfull) or not is_mod_dir(subfull):
                    continue
                techname = mod_technical_name(subfull, sub)
                mods.append({
                    "name": techname,
                    "folder": f"{entry}/{sub}",
                    "modpack": entry,
                    "enabled": enabled_map.get(techname, False),
                    "size": dir_size(subfull),
                })
        elif is_mod_dir(full):
            techname = mod_technical_name(full, entry)
            mods.append({
                "name": techname,
                "folder": entry,
                "modpack": None,
                "enabled": enabled_map.get(techname, False),
                "size": dir_size(full),
            })
    return mods
def toggle_modpack(modpack_name, enabled):
    """Enables or disables all mods in a modpack at once."""
    full = safe_mod_path(modpack_name)
    if not os.path.isdir(full) or not is_modpack_dir(full):
        raise ValueError("Modpack not found.")
    for sub in sorted(os.listdir(full)):
        subfull = os.path.join(full, sub)
        if not os.path.isdir(subfull) or not is_mod_dir(subfull):
            continue
        techname = mod_technical_name(subfull, sub)
        rel_path = f"mods/{modpack_name}/{sub}"
        set_mod_enabled(techname, enabled, rel_path if enabled else None)
def delete_modpack(modpack_name):
    """Deletes an entire modpack (folder + all its world.mt entries)."""
    full = safe_mod_path(modpack_name)
    if not os.path.isdir(full) or not is_modpack_dir(full):
        raise ValueError("Modpack not found.")
    technames = []
    for sub in sorted(os.listdir(full)):
        subfull = os.path.join(full, sub)
        if os.path.isdir(subfull) and is_mod_dir(subfull):
            technames.append(mod_technical_name(subfull, sub))
    shutil.rmtree(full)
    if technames:
        pattern = re.compile(
            r"^\s*load_mod_(" + "|".join(re.escape(t) for t in technames) + r")\s*=",
            re.IGNORECASE,
        )
        lines = [l for l in read_world_mt() if not pattern.match(l)]
        write_world_mt(lines)
def dir_size(path):
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total
def delete_mod(relfolder, technical_name):
    full = safe_mod_path(relfolder)
    if os.path.isdir(full):
        shutil.rmtree(full)
    key = f"load_mod_{technical_name}"
    lines = [l for l in read_world_mt() if not re.match(rf"^\s*{re.escape(key)}\s*=", l, re.IGNORECASE)]
    write_world_mt(lines)
def install_mod_from_git(url):
    os.makedirs(MODS_DIR, exist_ok=True)
    name = os.path.splitext(os.path.basename(urlparse(url).path.rstrip("/")))[0]
    if not name:
        name = f"mod_{secrets.token_hex(4)}"
    dest = safe_mod_path(name)
    if os.path.exists(dest):
        dest = dest + "_" + secrets.token_hex(3)
    proc = subprocess.run(
        ["git", "clone", "--depth", "1", url, dest],
        capture_output=True, text=True, timeout=120
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "Git clone failed.")
    return os.path.basename(dest)
def install_mod_from_zip(filename, data):
    os.makedirs(MODS_DIR, exist_ok=True)
    tmp_dir = os.path.join(MODS_DIR, "_tmp_" + secrets.token_hex(4))
    os.makedirs(tmp_dir, exist_ok=True)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for member in z.namelist():
                # prevents path traversal via a malicious zip archive
                norm = os.path.normpath(member)
                if norm.startswith("..") or os.path.isabs(norm):
                    raise ValueError("Suspicious archive (invalid path).")
            z.extractall(tmp_dir)
        entries = [e for e in os.listdir(tmp_dir) if not e.startswith(".")]
        if len(entries) == 1 and os.path.isdir(os.path.join(tmp_dir, entries[0])):
            modname = entries[0]
            src = os.path.join(tmp_dir, entries[0])
        else:
            modname = os.path.splitext(filename)[0]
            src = tmp_dir
        dest = safe_mod_path(modname)
        if os.path.exists(dest):
            dest = dest + "_" + secrets.token_hex(3)
            modname = os.path.basename(dest)
        shutil.move(src, dest)
        return modname
    finally:
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)
CONFIG_FIELDS = [
    {"key": "port", "label": "Port", "type": "number", "default": "30000",
     "desc": "Network port on which the server listens for player connections."},
    {"key": "server_name", "label": "Server name", "type": "text", "default": "My Luanti server",
     "desc": "Name shown in the public server list and in-game."},
    {"key": "server_description", "label": "Description", "type": "text", "default": "",
     "desc": "Short description shown in the server list."},
    {"key": "motd", "label": "Message of the day (MOTD)", "type": "text", "default": "",
     "desc": "Message shown to players when they connect."},
    {"key": "max_users", "label": "Max players", "type": "number", "default": "15",
     "desc": "Maximum number of players connected at the same time."},
    {"key": "default_privs", "label": "Default privileges", "type": "text", "default": "interact, shout",
     "desc": "Privileges automatically granted to a new player (e.g. interact, shout, fly)."},
    {"key": "creative_mode", "label": "Creative mode", "type": "bool", "default": "false",
     "desc": "Enables an unlimited creative inventory for all players."},
    {"key": "enable_damage", "label": "Damage enabled", "type": "bool", "default": "true",
     "desc": "Enables damage from falls, hunger, mobs, and more."},
    {"key": "enable_pvp", "label": "PvP enabled", "type": "bool", "default": "false",
     "desc": "Allows players to hurt one another."},
    {"key": "disallow_empty_password", "label": "Disallow empty passwords", "type": "bool", "default": "true",
     "desc": "Prevents accounts with empty passwords from connecting."},
    {"key": "strict_protocol_version_checking", "label": "Strict protocol version checking", "type": "bool", "default": "false",
     "desc": "Rejects clients whose protocol version does not match exactly."},
    {"key": "static_spawnpoint", "label": "Fixed spawn point", "type": "text", "default": "",
     "desc": "Fixed spawn coordinates, in x,y,z format (empty = random)."},
    {"key": "max_block_send_distance", "label": "Block send distance", "type": "number", "default": "10",
     "desc": "Terrain distance sent to players, in mapblocks. Affects network performance."},
    {"key": "active_block_range", "label": "Active block range", "type": "number", "default": "4",
     "desc": "Distance within which blocks are actively simulated (mobs, crops, and more)."},
    {"key": "time_speed", "label": "Time speed", "type": "number", "default": "72",
     "desc": "Day/night cycle speed (72 = one real day lasts 20 minutes)."},
    {"key": "kick_msg_crash", "label": "Crash kick message", "type": "text", "default": "",
     "desc": "Message shown to players if the server crashes and disconnects them."},

]
def read_conf_raw():
    if not os.path.exists(CONFIG_FILE):
        return ""
    with open(CONFIG_FILE, "r", encoding="utf-8", errors="replace") as f:
        return f.read()
def write_conf_raw(text):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        f.write(text)
def parse_conf(text):
    values = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_.\[\]]+)\s*=\s*(.*)$", stripped)
        if m:
            values[m.group(1)] = m.group(2).strip()
    return values
def update_conf_keys(updates):
    """Updates or adds keys in minetest.conf without touching the rest of the file."""
    raw = read_conf_raw()
    lines = raw.splitlines() if raw else []
    remaining = dict(updates)
    new_lines = []
    for line in lines:
        stripped = line.strip()
        m = re.match(r"^#?\s*([A-Za-z0-9_.\[\]]+)\s*=", stripped)
        if m and m.group(1) in remaining:
            key = m.group(1)
            new_lines.append(f"{key} = {remaining.pop(key)}")
        else:
            new_lines.append(line)
    for key, val in remaining.items():
        new_lines.append(f"{key} = {val}")
    write_conf_raw("\n".join(new_lines) + "\n")
def get_config_view():
    raw = read_conf_raw()
    parsed = parse_conf(raw)
    fields = []
    for f in CONFIG_FIELDS:
        fields.append({**f, "value": parsed.get(f["key"], f["default"])})
    return {"fields": fields, "raw": raw}
def get_server_port():
    parsed = parse_conf(read_conf_raw())
    return parsed.get("port", "30000")
def tail_text_file(path, max_lines=500, max_bytes=400_000):
    if not os.path.exists(path):
        return []
    size = os.path.getsize(path)
    read_size = min(size, max_bytes)
    with open(path, "rb") as f:
        f.seek(size - read_size)
        data = f.read()
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return lines[-max_lines:]
def clear_debug_file():
    os.makedirs(os.path.dirname(DEBUG_FILE), exist_ok=True)
    with open(DEBUG_FILE, "w", encoding="utf-8"):
        pass
def _parse_ss_output(output, port):
    conns = []
    port_suffix = ":" + str(port)
    for line in output.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        netid, state, recvq, sendq, local, peer = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
        if not local.endswith(port_suffix):
            continue
        peer_display = None if peer in ("*:*", "0.0.0.0:*", "[::]:*") else peer
        conns.append({"proto": netid, "state": state, "local": local, "peer": peer_display,
                       "recv_q": recvq, "send_q": sendq})
    return conns
def _parse_netstat_output(output, port):
    conns = []
    port_suffix = ":" + str(port)
    for line in output.splitlines():
        if not (line.startswith("tcp") or line.startswith("udp")):
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        proto, recvq, sendq, local, peer = parts[0], parts[1], parts[2], parts[3], parts[4]
        state = parts[5] if len(parts) > 5 else ""
        if not local.endswith(port_suffix):
            continue
        peer_display = None if peer in ("*:*", "0.0.0.0:*", "[::]:*") else peer
        conns.append({"proto": proto, "state": state, "local": local, "peer": peer_display,
                       "recv_q": recvq, "send_q": sendq})
    return conns
def get_network_connections():
    port = get_server_port()
    if shutil.which("ss"):
        try:
            proc = subprocess.run(["ss", "-tuna"], capture_output=True, text=True, timeout=5)
            return {"port": port, "tool": "ss", "connections": _parse_ss_output(proc.stdout, port), "error": None}
        except Exception as e:
            return {"port": port, "tool": "ss", "connections": [], "error": str(e)}
    if shutil.which("netstat"):
        try:
            proc = subprocess.run(["netstat", "-tuna"], capture_output=True, text=True, timeout=5)
            return {"port": port, "tool": "netstat", "connections": _parse_netstat_output(proc.stdout, port), "error": None}
        except Exception as e:
            return {"port": port, "tool": "netstat", "connections": [], "error": str(e)}
    return {"port": port, "tool": None, "connections": [],
            "error": "No network tool found. Install it with: pkg install iproute2"}
def safe_rel_path(rel):
    rel = (rel or "").strip().lstrip("/")
    full = os.path.normpath(os.path.join(FILES_ROOT, rel))
    base = os.path.normpath(FILES_ROOT)
    if full != base and not full.startswith(base + os.sep):
        raise ValueError("Invalid path.")
    return full
def list_dir(rel):
    full = safe_rel_path(rel)
    if not os.path.isdir(full):
        raise ValueError("Folder not found.")
    items = []
    for entry in sorted(os.listdir(full)):
        p = os.path.join(full, entry)
        items.append({
            "name": entry,
            "is_dir": os.path.isdir(p),
            "size": os.path.getsize(p) if os.path.isfile(p) else 0,
        })
    return items
def parse_multipart(body, content_type):
    m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type)
    if not m:
        raise ValueError("missing boundary")
    boundary = (m.group(1) or m.group(2)).strip().encode()
    delim = b"--" + boundary
    parts = body.split(delim)
    fields = {}
    files = {}
    for part in parts:
        if part in (b"", b"--\r\n", b"--"):
            continue
        part = part.strip(b"\r\n")
        if not part:
            continue
        header_end = part.find(b"\r\n\r\n")
        if header_end == -1:
            continue
        raw_headers = part[:header_end].decode("utf-8", errors="replace")
        content = part[header_end + 4:]
        cd = re.search(r'name="([^"]*)"(?:; filename="([^"]*)")?', raw_headers)
        if not cd:
            continue
        field_name = cd.group(1)
        filename = cd.group(2)
        if filename is not None:
            files[field_name] = (filename, content)
        else:
            fields[field_name] = content.decode("utf-8", errors="replace")
    return fields, files
LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Luanti Panel — Login</title>
<style>
:root{--bg:#0d0f14;--panel:#171a22;--panel2:#1e222c;--accent:#5b8cff;--accent2:#4a7cff;--good:#3ecf8e;--bad:#ff6b6b;--muted:#8a8f98;--border:#262b36}
*{box-sizing:border-box}
body{font-family:-apple-system,system-ui,"Segoe UI",sans-serif;background:radial-gradient(circle at 30% 20%,#161a24,#0d0f14);color:#e6e6e6;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.box{background:var(--panel);padding:36px;border-radius:16px;width:300px;box-shadow:0 12px 32px rgba(0,0,0,.45);border:1px solid var(--border)}
.logo{width:44px;height:44px;border-radius:12px;background:linear-gradient(135deg,var(--accent),#8a5bff);display:flex;align-items:center;justify-content:center;margin-bottom:16px}
h1{font-size:18px;margin:0 0 4px;font-weight:700}
p.sub{color:var(--muted);font-size:13px;margin:0 0 22px}
.field{position:relative;margin-bottom:14px}
.field svg{position:absolute;left:11px;top:50%;transform:translateY(-50%);color:var(--muted)}
input{width:100%;padding:11px 12px 11px 38px;border-radius:8px;border:1px solid var(--border);background:#0d0f14;color:#eee;font-size:14px}
input:focus{outline:none;border-color:var(--accent)}
button.submit{width:100%;padding:11px;border:0;border-radius:8px;background:var(--accent2);color:#fff;font-weight:600;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:8px;font-size:14px}
button.submit:hover{background:var(--accent)}
#err{color:var(--bad);font-size:13px;min-height:16px;margin-bottom:6px;display:flex;align-items:center;gap:6px}
</style></head>
<body>
<div class="box">
<div class="logo"><svg width="26" height="26" viewBox="0 0 24 24" fill="#fff"><path d="M6.11 0L1.76 2.516v4.478L3.638 8.08L.073 10.137v6.97L12.013 24l11.773-6.96l.14-.083v-6.672l-3.323-1.92V6.148l-1.061-.613l-1.156.774v.775l-1.11-.64v-.948c-.002-.11-.053-.182-.138-.24l-4.166-2.404a.28.28 0 0 0-.28 0l-2.62 1.515v-2.08Zm0 .64l3.41 1.966v4.297L6.11 8.867L2.312 6.676V2.834Zm6.721 2.77l3.613 2.086l-4.382 2.531a.277.277 0 0 0 0 .48l3.27 1.891l-7.2 4.07l-7.227-4.171L4.19 8.398l.684.397v2.217l1.236.715l1.239-.715V8.795l2.722-1.572V5.008Zm3.89 2.569v.466l-3.56 2.059l-.406-.234zm2.84.208l.487.282v4.33l-.496.287l-.614-.354V6.605ZM17 6.926l1.387.8v3.327l1.166.674l1.05-.61V9.006l2.77 1.6v.49L19.548 13.3l-3.381-1.951v-.944a.28.28 0 0 0-.139-.246l-2.314-1.338ZM5.429 9.113l.681.397l.686-.397v1.576l-.686.397l-.681-.397Zm-4.8 1.662l7.362 4.252c.086.05.19.051.278.002l7.343-4.154v.473l-7.76 4.386v1.43l.864.498v1.11l3.297 1.902l6.925-4.08v-1.19l1.11-.64v-1.112q1.661-.96 3.324-1.916v1.024l-2.217 1.277v.557l-1.11.638v1.11l-1.107.64v2.28l-6.93 4.095l-3.599-2.08V20.17l-1.06-.611v-1.11c-.385-.225-.773-.445-1.159-.67v-2.215l-3.324-1.92v1.11l-1.107-.64v3.325l-1.131-.652Zm15.26 1.053c1.21.697 2.402 1.392 3.604 2.082v.533l-1.107.641v1.191l-6.375 3.758l-2.742-1.582v-1.11l-.86-.495v-.787zm7.483 1.57v3.24l-3.879 2.24v-1.577l1.11-.64v-1.108l1.107-.64v-.556zM3.421 14.604l2.217 1.28v1.577l-1.446-.834l-1.879 1.086v-2.64l1.108.64zm1.32 1.392l-.138.24l.119.069l.138-.24zm.36.207l-.14.24l.12.07l.139-.24zm-.909 1.065l1.446.834l1.11.638v1.11l1.106.642v.469l-5.027-2.904Z"></path></svg></div>
<h1>Luanti Panel</h1>
<p class="sub">Sign in to the administration panel</p>
<div id="err"></div>
<div class="field">
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect><path d="M7 11V7a5 5 0 0 1 10 0v4"></path></svg>
  <input type="password" id="pw" placeholder="Password" autofocus>
</div>
<button class="submit" onclick="login()">
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M12 5l7 7-7 7"></path></svg>
  Sign in
</button>
</div>
<script>
document.getElementById('pw').addEventListener('keydown', e => { if(e.key==='Enter') login(); });
async function login(){
  const pw = document.getElementById('pw').value;
  const r = await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({password: pw})});
  if(r.ok){ location.href = '/'; } else { document.getElementById('err').textContent = '⚠ Incorrect password.'; }
}
</script>
</body></html>"""
DASHBOARD_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Luanti Panel</title>
<style>
:root{--bg:#0d0f14;--panel:#171a22;--panel2:#1e222c;--accent:#5b8cff;--accent2:#4a7cff;--good:#3ecf8e;--bad:#ff6b6b;--muted:#8a8f98;--border:#262b36}
*{box-sizing:border-box}
body{font-family:-apple-system,system-ui,"Segoe UI",sans-serif;background:var(--bg);color:#e6e6e6;margin:0}
header{display:flex;align-items:center;justify-content:space-between;padding:14px 20px;background:var(--panel);position:sticky;top:0;z-index:10;border-bottom:1px solid var(--border)}
.brand{display:flex;align-items:center;gap:10px}
.brand-icon{width:32px;height:32px;border-radius:9px;background:linear-gradient(135deg,var(--accent),#8a5bff);display:flex;align-items:center;justify-content:center;flex-shrink:0}
header h1{font-size:15px;margin:0;font-weight:700}
.brand-version{font-size:10.5px;color:var(--muted);font-weight:500;margin-top:1px}
.status-pill{display:flex;align-items:center;gap:6px;font-size:12.5px;color:var(--muted);background:var(--panel2);padding:5px 10px;border-radius:20px}
.status-dot{width:8px;height:8px;border-radius:50%;background:var(--bad);flex-shrink:0}
.status-dot.on{background:var(--good);box-shadow:0 0 6px var(--good)}
.icon-btn{background:var(--panel2);border:1px solid var(--border);color:var(--muted);width:34px;height:34px;border-radius:8px;cursor:pointer;display:flex;align-items:center;justify-content:center;position:relative}
.icon-btn:hover{color:#eee;border-color:var(--accent)}
.update-dot{position:absolute;top:-2px;right:-2px;width:9px;height:9px;border-radius:50%;background:var(--good);border:2px solid var(--panel);display:none}
.right-group{display:flex;align-items:center;gap:10px}
nav{display:flex;gap:4px;padding:10px 20px;background:#11141b;overflow-x:auto;border-bottom:1px solid var(--border)}
nav button{background:none;border:0;color:var(--muted);padding:8px 14px;border-radius:8px;cursor:pointer;font-size:13.5px;white-space:nowrap;display:flex;align-items:center;gap:7px;font-weight:500}
nav button.active{background:var(--accent2);color:#fff}
nav button:hover:not(.active){background:var(--panel2)}
main{padding:16px 20px;max-width:920px;margin:0 auto}
.tab{display:none}
.tab.active{display:block}
.card{background:var(--panel);border-radius:12px;padding:18px;margin-bottom:14px;border:1px solid var(--border)}
.card h3{margin:0 0 14px;font-size:14.5px;display:flex;align-items:center;gap:8px;color:#f0f0f0}
.card h3 svg{color:var(--accent)}
button.btn{background:var(--accent2);color:#fff;border:0;padding:9px 16px;border-radius:8px;cursor:pointer;font-size:13.5px;display:inline-flex;align-items:center;gap:7px;font-weight:600}
button.btn:hover{background:var(--accent)}
button.btn.danger{background:rgba(255,107,107,.15);color:var(--bad)}
button.btn.danger:hover{background:rgba(255,107,107,.28)}
button.btn.ghost{background:var(--panel2);color:#ddd}
button.btn.ghost:hover{background:#2a2f3b}
button.btn:disabled{opacity:.4;cursor:not-allowed}
input,select{background:#0d0f14;border:1px solid var(--border);color:#eee;padding:9px 10px;border-radius:8px;font-size:13.5px}
input:focus{outline:none;border-color:var(--accent)}
#console{background:#000;color:#7ee787;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;padding:12px;height:360px;overflow-y:auto;border-radius:10px;white-space:pre-wrap;border:1px solid var(--border)}
.row{display:flex;gap:8px;margin-top:10px}
.row input{flex:1}
.mod-item,.file-item{display:flex;align-items:center;justify-content:space-between;padding:11px 4px;border-bottom:1px solid var(--border)}
.mod-item:last-child,.file-item:last-child{border-bottom:none}
.item-left{display:flex;align-items:center;gap:10px;min-width:0}
.item-left svg{flex-shrink:0;color:var(--muted)}
.item-left.clickable{cursor:pointer}
.mod-name,.file-name{font-weight:600;font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.size{color:var(--muted);font-size:11.5px;margin-left:8px;font-weight:400}
.switch{position:relative;display:inline-block;width:40px;height:22px;flex-shrink:0}
.switch input{display:none}
.slider{position:absolute;inset:0;background:#333;border-radius:22px;cursor:pointer;transition:.2s}
.slider:before{content:"";position:absolute;width:16px;height:16px;left:3px;top:3px;background:#fff;border-radius:50%;transition:.2s}
input:checked + .slider{background:var(--good)}
input:checked + .slider:before{transform:translateX(18px)}
.actions{display:flex;gap:8px;align-items:center;flex-shrink:0}
.muted{color:var(--muted);font-size:12.5px}
.dropzone{border:2px dashed var(--border);border-radius:12px;padding:26px;text-align:center;color:var(--muted);cursor:pointer;display:flex;flex-direction:column;align-items:center;gap:8px;font-size:13.5px}
.dropzone:hover{border-color:var(--accent);color:#ddd}
.dropzone.drag{border-color:var(--accent);color:#eee;background:rgba(91,140,255,.06)}
.breadcrumb{color:var(--muted);font-size:13px;margin-bottom:12px;display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.breadcrumb span{cursor:pointer;color:var(--accent);display:flex;align-items:center;gap:4px}
.breadcrumb span:hover{text-decoration:underline}
.empty{color:var(--muted);font-size:13px;text-align:center;padding:20px 0;display:flex;flex-direction:column;align-items:center;gap:8px}
.icon-btn-sm{background:none;border:0;color:var(--muted);cursor:pointer;padding:5px;border-radius:6px;display:flex}
.icon-btn-sm:hover{color:var(--bad);background:rgba(255,107,107,.1)}
.modpack-group{margin-bottom:4px}
.modpack-header{display:flex;align-items:center;justify-content:space-between;padding:10px 8px;background:var(--panel2);border-radius:8px;margin-bottom:2px}
.modpack-header .mod-name{color:#f0f0f0}
.modpack-header .icon-btn-sm:hover{color:var(--accent);background:rgba(91,140,255,.12)}
.modpack-body{padding-left:10px;margin-left:8px;border-left:2px solid var(--border)}
.modpack-body .mod-item:last-child{border-bottom:none}
.mod-item-indented{padding-left:8px}
.lvl-error{color:var(--bad);font-weight:600}
.lvl-warning{color:#f0c975;font-weight:600}
.lvl-action{color:#7ee787}
.lvl-info{color:#7ab8ff}
.lvl-verbose{color:var(--muted)}
.term-cmd{color:#c893ff;font-weight:600}
.term-panel{color:#5b8cff;font-style:italic}
.term-tag{color:#f0c975}
#console .term-cmd, #debugConsole .term-cmd{color:#c893ff}
.action-toast{position:fixed;bottom:20px;right:20px;background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:10px 16px;display:flex;align-items:center;gap:10px;box-shadow:0 12px 32px rgba(0,0,0,.5);opacity:0;transform:translateY(12px);pointer-events:none;transition:opacity .18s ease,transform .18s ease;z-index:200;font-size:13px;color:#eee}
.action-toast.show{opacity:1;transform:translateY(0)}
.action-spinner{width:20px;height:20px;color:var(--accent);flex-shrink:0}
.net-table{width:100%;border-collapse:collapse;font-size:12.5px}
.net-table th{text-align:left;color:var(--muted);font-weight:600;padding:6px 8px;border-bottom:1px solid var(--border);white-space:nowrap}
.net-table td{padding:7px 8px;border-bottom:1px solid var(--border);white-space:nowrap;font-family:ui-monospace,monospace}
.net-table tr:last-child td{border-bottom:none}
.badge{display:inline-block;padding:2px 8px;border-radius:20px;font-size:11px;font-weight:600}
.badge.udp{background:rgba(91,140,255,.15);color:#7aa2ff}
.badge.tcp{background:rgba(126,231,135,.15);color:#7ee787}
.net-summary{display:flex;gap:20px;margin-bottom:14px;flex-wrap:wrap}
.net-stat{background:var(--panel2);border-radius:10px;padding:10px 16px;min-width:110px}
.net-stat .n{font-size:20px;font-weight:700;color:#f0f0f0}
.net-stat .l{font-size:11.5px;color:var(--muted)}
#console {
    overflow-y: auto;
    overflow-x: hidden;
    scrollbar-width: thin;
    scrollbar-color: #777 transparent;
}

#console::-webkit-scrollbar,
#debugConsole::-webkit-scrollbar {
    width: 5px;
}

#console::-webkit-scrollbar-track,
#debugConsole::-webkit-scrollbar-track {
    background: transparent;
}

#console::-webkit-scrollbar-thumb,
#debugConsole::-webkit-scrollbar-thumb {
    background: #777;
    border-radius: 999px;
}

#console::-webkit-scrollbar-thumb:hover,
#debugConsole::-webkit-scrollbar-thumb:hover {
    background: #999;
}

#console::-webkit-scrollbar-button,
#debugConsole::-webkit-scrollbar-button {
    display: none;
    width: 0;
    height: 0;
}

#console::-webkit-scrollbar-corner,
#debugConsole::-webkit-scrollbar-corner {
    background: transparent;
}
.modal-overlay{position:fixed;inset:0;background:rgba(5,6,10,.68);backdrop-filter:blur(2px);display:none;align-items:center;justify-content:center;z-index:300;padding:16px}
.modal-box{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:24px;width:100%;max-width:420px;box-shadow:0 20px 60px rgba(0,0,0,.55)}
.modal-box h3{margin:0 0 6px;font-size:16px;display:flex;align-items:center;gap:9px;color:#f5f5f5}
.modal-box h3 svg{color:var(--accent)}
.modal-box p{color:var(--muted);font-size:13px;line-height:1.5;margin:0 0 16px}
.modal-box label{display:block;font-size:12.5px;font-weight:600;margin-bottom:6px;color:#ddd}
.modal-box input[type=password]{width:100%;margin-bottom:6px}
.modal-error{color:var(--bad);font-size:12.5px;margin:6px 0 4px;display:none}
.modal-actions{display:flex;gap:8px;margin-top:16px;justify-content:flex-end}
.modal-step{color:var(--good);font-size:12.5px;margin-top:12px}
.upd-spin{position:relative;display:inline-flex;width:15px;height:15px;flex-shrink:0}
.upd-spin svg{position:absolute;inset:0;width:100%;height:100%}
.upd-spin .upd-icon{padding:3.5px;color:#fff}
.modal-overlay.locked{cursor:default}
.update-progress{text-align:center;padding:8px 4px 4px}
.upd-spin-lg{position:relative;display:inline-flex;width:64px;height:64px;color:var(--accent);margin-bottom:18px}
.upd-spin-lg svg{position:absolute;inset:0;width:100%;height:100%}
.upd-spin-lg .upd-icon-lg{padding:16px;color:#fff}
.update-progress h4{margin:0 0 8px;font-size:15px;color:#f5f5f5;font-weight:700}
.update-progress p{margin:0;color:var(--muted);font-size:13px;line-height:1.5}
.version-check{background:var(--panel2);border-radius:10px;padding:12px 14px;margin-bottom:16px;font-size:13px}
.version-check .vc-row{display:flex;align-items:center;justify-content:space-between;padding:3px 0}
.version-check .vc-label{color:var(--muted)}
.version-check .vc-value{font-weight:600;font-family:ui-monospace,monospace}
.version-check .vc-status{margin-top:8px;padding-top:8px;border-top:1px solid var(--border);display:flex;align-items:center;gap:7px;font-weight:600}
.version-check .vc-status.uptodate{color:var(--good)}
.version-check .vc-status.available{color:#f0c975}
.version-check .vc-status.error{color:var(--bad);font-weight:500}
.version-check .vc-loading{display:flex;align-items:center;gap:8px;color:var(--muted)}
.update-wrap{display:flex;align-items:flex-start;gap:16px}
.update-main{flex:1;min-width:0}
.modal-box.with-notes{max-width:700px}
.release-notes-btn{background:none;border:none;color:var(--accent);font-size:12.5px;font-weight:600;cursor:pointer;padding:0;margin:0 0 16px;display:inline-flex;align-items:center;gap:5px}
.release-notes-btn:hover{text-decoration:underline}
.release-notes-panel{width:230px;flex-shrink:0;background:var(--panel2);border:1px solid var(--border);border-radius:10px;padding:14px 16px;font-size:13px}
.release-notes-panel h4{margin:0 0 10px;font-size:13px;color:#f5f5f5;display:flex;align-items:center;gap:6px}
.release-notes-panel h4 svg{color:var(--accent)}
.release-notes-panel .rn-block+.rn-block{margin-top:12px;padding-top:12px;border-top:1px solid var(--border)}
.release-notes-panel .rn-version{color:var(--muted);font-weight:600;font-family:ui-monospace,monospace;margin-bottom:4px}
.release-notes-panel .rn-text{line-height:1.5;color:#ddd;white-space:pre-wrap}
.release-notes-panel .rn-empty{color:var(--muted);font-style:italic}
@media (max-width:640px){.update-wrap{flex-direction:column}.release-notes-panel{width:100%}.modal-box.with-notes{max-width:420px}}
.vc-spin{width:14px;height:14px;flex-shrink:0;animation:vcspin 1s linear infinite}
@keyframes vcspin{to{transform:rotate(360deg)}}
</style></head>
<body>
<header>
  <div class="brand">
    <div class="brand-icon"><svg width="20" height="20" viewBox="0 0 24 24" fill="#fff"><path d="M6.11 0L1.76 2.516v4.478L3.638 8.08L.073 10.137v6.97L12.013 24l11.773-6.96l.14-.083v-6.672l-3.323-1.92V6.148l-1.061-.613l-1.156.774v.775l-1.11-.64v-.948c-.002-.11-.053-.182-.138-.24l-4.166-2.404a.28.28 0 0 0-.28 0l-2.62 1.515v-2.08Zm0 .64l3.41 1.966v4.297L6.11 8.867L2.312 6.676V2.834Zm6.721 2.77l3.613 2.086l-4.382 2.531a.277.277 0 0 0 0 .48l3.27 1.891l-7.2 4.07l-7.227-4.171L4.19 8.398l.684.397v2.217l1.236.715l1.239-.715V8.795l2.722-1.572V5.008Zm3.89 2.569v.466l-3.56 2.059l-.406-.234zm2.84.208l.487.282v4.33l-.496.287l-.614-.354V6.605ZM17 6.926l1.387.8v3.327l1.166.674l1.05-.61V9.006l2.77 1.6v.49L19.548 13.3l-3.381-1.951v-.944a.28.28 0 0 0-.139-.246l-2.314-1.338ZM5.429 9.113l.681.397l.686-.397v1.576l-.686.397l-.681-.397Zm-4.8 1.662l7.362 4.252c.086.05.19.051.278.002l7.343-4.154v.473l-7.76 4.386v1.43l.864.498v1.11l3.297 1.902l6.925-4.08v-1.19l1.11-.64v-1.112q1.661-.96 3.324-1.916v1.024l-2.217 1.277v.557l-1.11.638v1.11l-1.107.64v2.28l-6.93 4.095l-3.599-2.08V20.17l-1.06-.611v-1.11c-.385-.225-.773-.445-1.159-.67v-2.215l-3.324-1.92v1.11l-1.107-.64v3.325l-1.131-.652Zm15.26 1.053c1.21.697 2.402 1.392 3.604 2.082v.533l-1.107.641v1.191l-6.375 3.758l-2.742-1.582v-1.11l-.86-.495v-.787zm7.483 1.57v3.24l-3.879 2.24v-1.577l1.11-.64v-1.108l1.107-.64v-.556zM3.421 14.604l2.217 1.28v1.577l-1.446-.834l-1.879 1.086v-2.64l1.108.64zm1.32 1.392l-.138.24l.119.069l.138-.24zm.36.207l-.14.24l.12.07l.139-.24zm-.909 1.065l1.446.834l1.11.638v1.11l1.106.642v.469l-5.027-2.904Z"></path></svg></div>
    <div>
      <h1>Luanti Panel</h1>
      <div class="brand-version" id="brandVersion">v…</div>
    </div>
  </div>
  <div class="right-group">
    <div class="status-pill"><span class="status-dot" id="dot"></span><span id="statusText">...</span></div>
    <button class="icon-btn" onclick="license()" title="License">
      <svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 21H6a3 3 0 0 1-3-3v-1h10v2a2 2 0 0 0 4 0V5a2 2 0 1 1 2 2h-2m2-4H8a3 3 0 0 0-3 3v11M9 7h4m-4 4h4"/></svg>
    </button>
    <button class="icon-btn" onclick="openUpdateModal()" title="Update panel">
      <span class="update-dot" id="updateDot"></span>
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
    </button>
    <button class="icon-btn" onclick="logout()" title="Sign out">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"></path><polyline points="16 17 21 12 16 7"></polyline><line x1="21" y1="12" x2="9" y2="12"></line></svg>
    </button>
    <button class="icon-btn" onclick="power()" title="System">
      <svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><g fill="none" fill-rule="evenodd"><path d="m12.593 23.258l-.011.002l-.071.035l-.02.004l-.014-.004l-.071-.035q-.016-.005-.024.005l-.004.01l-.017.428l.005.02l.01.013l.104.074l.015.004l.012-.004l.104-.074l.012-.016l.004-.017l-.017-.427q-.004-.016-.017-.018m.265-.113l-.013.002l-.185.093l-.01.01l-.003.011l.018.43l.005.012l.008.007l.201.093q.019.005.029-.008l.004-.014l-.034-.614q-.005-.018-.02-.022m-.715.002a.02.02 0 0 0-.027.006l-.006.014l-.034.614q.001.018.017.024l.015-.002l.201-.093l.01-.008l.004-.011l.017-.43l-.003-.012l-.01-.01z"/><path fill="currentColor" d="M13.5 3a1.5 1.5 0 0 0-3 0v10a1.5 1.5 0 0 0 3 0zM7.854 5.75a1.5 1.5 0 1 0-1.661-2.5A10.49 10.49 0 0 0 1.5 12c0 5.799 4.701 10.5 10.5 10.5S22.5 17.799 22.5 12c0-3.654-1.867-6.87-4.693-8.75a1.5 1.5 0 0 0-1.66 2.5a7.5 7.5 0 1 1-8.292 0Z"/></g></svg>
    </button>
  </div>
</header>
<nav>
  <button class="active" onclick="showTab('server')">
    <svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 16 16"><path fill="currentColor" d="M14 11a1 1 0 0 1 1 1v2a1 1 0 0 1-1 1H2a1 1 0 0 1-1-1v-2a1 1 0 0 1 1-1zM3 12a1 1 0 1 0 0 2a1 1 0 0 0 0-2m3 0a1 1 0 1 0 0 2a1 1 0 0 0 0-2m8-6a1 1 0 0 1 1 1v2a1 1 0 0 1-1 1H2a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1zM3 7a1 1 0 1 0 0 2a1 1 0 0 0 0-2m3 0a1 1 0 1 0 0 2a1 1 0 0 0 0-2m8-6a1 1 0 0 1 1 1v2a1 1 0 0 1-1 1H2a1 1 0 0 1-1-1V2a1 1 0 0 1 1-1zM3 2a1 1 0 1 0 0 2a1 1 0 0 0 0-2m3 0a1 1 0 1 0 0 2a1 1 0 0 0 0-2"/></svg>
    Server
  </button>
  <button onclick="showTab('mods')">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="3" x2="6" y2="15"></line><circle cx="18" cy="6" r="3"></circle><circle cx="6" cy="18" r="3"></circle><path d="M18 9a9 9 0 0 1-9 9"></path></svg>
    Mods
  </button>
  <button onclick="showTab('files')">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>
    Files
  </button>
  <button onclick="showTab('config')">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg>
    Configuration
  </button>
  <button onclick="showTab('debug')">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="8" y="6" width="8" height="14" rx="4"></rect><path d="M19 7l-3 2"></path><path d="M5 7l3 2"></path><path d="M19 19l-3-2"></path><path d="M5 19l3-2"></path><line x1="12" y1="2" x2="12" y2="6"></line><line x1="3" y1="13" x2="8" y2="13"></line><line x1="16" y1="13" x2="21" y2="13"></line></svg>
    Debug
  </button>
  <button onclick="showTab('network')">
    <svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><path fill="currentColor" d="M15 20a1 1 0 0 0-1-1h-1v-2h4a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2H7a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h4v2h-1a1 1 0 0 0-1 1H2v2h7a1 1 0 0 0 1 1h4a1 1 0 0 0 1-1h7v-2zm-8-5V5h10v10z"/></svg>
    Network
  </button>
</nav>
<main>
<div class="tab active" id="tab-server">
  <div class="card">
    <h3><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>Server control</h3>
    <div class="actions">
      <button class="btn" id="btnStart" onclick="startServer()">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
        Start
      </button>
      <button class="btn danger" id="btnStop" onclick="stopServer()">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><rect x="5" y="5" width="14" height="14" rx="1"></rect></svg>
        Stop
      </button>
      <button class="btn ghost" id="btnRestart" onclick="restartServer()">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"></polyline><polyline points="1 20 1 14 7 14"></polyline><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg>
        Restart
      </button>
    </div>
    <p class="muted" id="uptime" style="margin-top:12px;margin-bottom:0"></p>
  </div>
  <div class="card">
    <h3><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 17 10 11 4 5"></polyline><line x1="12" y1="19" x2="20" y2="19"></line></svg>Console</h3>
    <div id="console"></div>
    <div class="row">
      <input id="cmdInput" placeholder="Server command (e.g. /status)" onkeydown="if(event.key==='Enter')sendCmd()">
      <button class="btn" onclick="sendCmd()">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"></line><polygon points="22 2 15 22 11 13 2 9 22 2"></polygon></svg>
        Send
      </button>
      <button class="btn ghost" onclick="clearConsole()" title="Clear console output">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path></svg>
        Clear
      </button>
    </div>
  </div>
</div>
<div class="tab" id="tab-mods">
  <div class="card">
    <h3><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="3" x2="6" y2="15"></line><circle cx="18" cy="6" r="3"></circle><circle cx="6" cy="18" r="3"></circle><path d="M18 9a9 9 0 0 1-9 9"></path></svg>Install a mod</h3>
    <div class="row">
      <input id="gitUrl" placeholder="Git repository URL (https://...)">
      <button class="btn" onclick="installGit()">
        <svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><path fill="currentColor" d="M12 2A10 10 0 0 0 2 12c0 4.42 2.87 8.17 6.84 9.5c.5.08.66-.23.66-.5v-1.69c-2.77.6-3.36-1.34-3.36-1.34c-.46-1.16-1.11-1.47-1.11-1.47c-.91-.62.07-.6.07-.6c1 .07 1.53 1.03 1.53 1.03c.87 1.52 2.34 1.07 2.91.83c.09-.65.35-1.09.63-1.34c-2.22-.25-4.55-1.11-4.55-4.92c0-1.11.38-2 1.03-2.71c-.1-.25-.45-1.29.1-2.64c0 0 .84-.27 2.75 1.02c.79-.22 1.65-.33 2.5-.33s1.71.11 2.5.33c1.91-1.29 2.75-1.02 2.75-1.02c.55 1.35.2 2.39.1 2.64c.65.71 1.03 1.6 1.03 2.71c0 3.82-2.34 4.66-4.57 4.91c.36.31.69.92.69 1.85V21c0 .27.16.59.67.5C19.14 20.16 22 16.42 22 12A10 10 0 0 0 12 2"/></svg>
        Clone
      </button>
    </div>
    <div class="dropzone" id="dropzone" onclick="document.getElementById('zipInput').click()">
      <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M16 16l-4-4-4 4"></path><path d="M12 12v9"></path><path d="M20.39 18.39A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.3"></path></svg>
      Click or drop a mod .zip file here
    </div>
    <input type="file" id="zipInput" accept=".zip" style="display:none" onchange="uploadZip(this.files[0])">
  </div>
  <div class="card">
    <h3><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><path fill="currentColor" d="M21.93 7.67a1 1 0 0 0-.07-.17c-.02-.03-.04-.05-.06-.08c-.03-.04-.06-.09-.1-.13c-.03-.03-.06-.04-.08-.07c-.04-.03-.07-.06-.11-.09h-.01l-9.01-5a.99.99 0 0 0-.97 0l-9.01 5H2.5c-.04.02-.08.06-.11.09a.3.3 0 0 0-.08.07c-.04.04-.07.08-.1.13c-.02-.03-.04-.05-.06-.08c-.03.05-.05.11-.07.17c0 .02-.02.05-.03.07c-.02.08-.04.17-.04.26v8c0 .36.2.7.51.87l9 5s.1.04.14.06c.03.01.06.03.09.03a1.1 1.1 0 0 0 .5 0c.03 0 .06-.02.09-.03c.05-.02.1-.03.14-.06l9-5c.32-.18.51-.51.51-.87V8c0-.09-.01-.18-.04-.26c0-.02-.02-.05-.03-.07ZM12 4.15l6.94 3.86l-2.44 1.36l-6.94-3.86zm-4.5 2.5l6.94 3.86L12 11.87L5.06 8.01zM20 15.42l-7 3.89V13.6l2.5-1.39v3.21l2-1.11V11.1L20 9.71z"/></svg>Installed mods</h3>
    <div id="modsList"></div>
  </div>
</div>
<div class="tab" id="tab-files">
  <div class="card">
    <div class="breadcrumb" id="breadcrumb"></div>
    <div class="row">
      <input type="file" id="fileUpload" onchange="uploadFile(this.files[0])" style="flex:1">
      <button class="btn ghost" onclick="mkdir()">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path><line x1="12" y1="11" x2="12" y2="17"></line><line x1="9" y1="14" x2="15" y2="14"></line></svg>
        Folder
      </button>
    </div>
    <div id="filesList" style="margin-top:10px"></div>
  </div>
</div>
<div class="tab" id="tab-config">
  <div class="card">
    <h3><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg>Current settings</h3>
    <p class="muted" style="margin:-6px 0 16px">These settings modify <code>minetest.conf</code>. The server must be restarted for them to take effect.</p>
    <div id="configFields"></div>
    <div class="row" style="margin-top:4px">
      <button class="btn" onclick="saveConfigFields()">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"></path><polyline points="17 21 17 13 7 13 7 21"></polyline><polyline points="7 3 7 8 15 8"></polyline></svg>
        Save
      </button>
    </div>
  </div>
  <div class="card">
    <h3><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 17 10 11 4 5"></polyline><line x1="12" y1="19" x2="20" y2="19"></line></svg>Advanced configuration (raw minetest.conf)</h3>
    <p class="muted" style="margin:-6px 0 14px">Not all Luanti settings can be listed individually (there are hundreds depending on the version and mods). This field displays <code>minetest.conf</code> as-is: you can add, edit, or remove any key using the format <code>parameter_name = value</code>, one per line.</p>
    <textarea id="rawConfig" spellcheck="false" style="width:100%;height:260px;background:#0d0f14;border:1px solid var(--border);border-radius:8px;color:#eee;font-family:ui-monospace,monospace;font-size:12.5px;padding:10px;resize:vertical"></textarea>
    <div class="row">
      <button class="btn ghost" onclick="loadConfig()">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"></polyline><polyline points="1 20 1 14 7 14"></polyline><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg>
        Reload
      </button>
      <button class="btn" onclick="saveRawConfig()">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"></path><polyline points="17 21 17 13 7 13 7 21"></polyline><polyline points="7 3 7 8 15 8"></polyline></svg>
        Save raw file
      </button>
    </div>
  </div>
</div>
<div class="tab" id="tab-debug">
  <div class="card">
    <h3><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="8" y="6" width="8" height="14" rx="4"></rect><path d="M19 7l-3 2"></path><path d="M5 7l3 2"></path><path d="M19 19l-3-2"></path><path d="M5 19l3-2"></path><line x1="12" y1="2" x2="12" y2="6"></line><line x1="3" y1="13" x2="8" y2="13"></line><line x1="16" y1="13" x2="21" y2="13"></line></svg>Debug log</h3>
    <p class="muted" style="margin:-6px 0 14px"><code>debug.txt</code> contains the server's internal logs. Each line generally starts with a level: <b style="color:var(--bad)">ERROR</b> (blocking error, often a mod crash), <b style="color:#f0c975">WARNING</b> (non-blocking problem), <b style="color:#7ee787">ACTION</b> (connection/disconnection, chat, block placement), <b>INFO</b> (general information), <b class="muted">VERBOSE / TRACE</b> (technical details). Useful for diagnosing a crashing mod or a Lua script error.</p>
    <div class="row" style="margin-bottom:10px">
      <input id="debugFilter" placeholder="Filter (e.g. ERROR, mod name...)" oninput="renderDebug()" style="flex:1">
      <button class="btn ghost" onclick="loadDebug()" title="Refresh">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"></polyline><polyline points="1 20 1 14 7 14"></polyline><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg>
      </button>
      <button class="btn danger" onclick="clearDebug()">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path></svg>
        Clear
      </button>
    </div>
    <div id="debugConsole" style="background:#000;color:#d8dee9;font-family:ui-monospace,monospace;font-size:12px;padding:12px;height:420px;overflow-y:auto;border-radius:10px;white-space:pre-wrap;border:1px solid var(--border)"></div>
  </div>
</div>
<div class="tab" id="tab-network">
  <div class="card">
    <h3 style="justify-content:space-between;display:flex">
      <span style="display:flex;align-items:center;gap:8px"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"></polyline></svg>Network traffic — port <span id="netPort">…</span></span>
      <button class="icon-btn" onclick="loadNetwork()" title="Refresh">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"></polyline><polyline points="1 20 1 14 7 14"></polyline><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg>
      </button>
    </h3>
    <p class="muted" style="margin:-6px 0 16px">List of active connections (players and peers) on the Luanti server port, read from the system socket table (<code>ss</code>/<code>netstat</code>) — automatically refreshed every 4 seconds while this tab is open. This shows who is connected and the state of the network queues, not packet contents: packet-by-packet inspection would require a capture tool (e.g. <code>tcpdump</code>), which generally requires root privileges on Android and is not provided here.</p>
    <div class="net-summary">
      <div class="net-stat"><div class="n" id="netPeerCount">0</div><div class="l">connected peers</div></div>
      <div class="net-stat"><div class="n" id="netSocketCount">0</div><div class="l">listening/active sockets</div></div>
    </div>
    <div id="netError" class="muted" style="display:none;margin-bottom:10px;color:var(--bad)"></div>
    <div style="overflow-x:auto">
      <table class="net-table" id="netTable">
        <thead><tr><th>Protocol</th><th>State</th><th>Local address</th><th>Remote address</th><th>Recv-Q</th><th>Send-Q</th></tr></thead>
        <tbody id="netTableBody"></tbody>
      </table>
    </div>
    <div id="netEmpty" class="empty" style="display:none"></div>
  </div>
</div>
</main>
<div class="action-toast" id="actionToast">
  <svg class="action-spinner" xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><g fill="currentColor"><circle cx="12" cy="3.5" r="1.5"><animate attributeName="fill-opacity" dur="2.4s" keyTimes="0;0.125;0.25;1" repeatCount="indefinite" values="1;1;0;0"/></circle><circle cx="16.25" cy="4.64" r="1.5" opacity="0"><set fill="freeze" attributeName="opacity" begin="0.2s" to="1"/><animate attributeName="fill-opacity" begin="0.2s" dur="2.4s" keyTimes="0;0.125;0.25;1" repeatCount="indefinite" values="1;1;0;0"/></circle><circle cx="19.36" cy="7.75" r="1.5" opacity="0"><set fill="freeze" attributeName="opacity" begin="0.4s" to="1"/><animate attributeName="fill-opacity" begin="0.4s" dur="2.4s" keyTimes="0;0.125;0.25;1" repeatCount="indefinite" values="1;1;0;0"/></circle><circle cx="20.5" cy="12" r="1.5" opacity="0"><set fill="freeze" attributeName="opacity" begin="0.6s" to="1"/><animate attributeName="fill-opacity" begin="0.6s" dur="2.4s" keyTimes="0;0.125;0.25;1" repeatCount="indefinite" values="1;1;0;0"/></circle><circle cx="19.36" cy="16.25" r="1.5" opacity="0"><set fill="freeze" attributeName="opacity" begin="0.8s" to="1"/><animate attributeName="fill-opacity" begin="0.8s" dur="2.4s" keyTimes="0;0.125;0.25;1" repeatCount="indefinite" values="1;1;0;0"/></circle><circle cx="16.25" cy="19.36" r="1.5" opacity="0"><set fill="freeze" attributeName="opacity" begin="1s" to="1"/><animate attributeName="fill-opacity" begin="1s" dur="2.4s" keyTimes="0;0.125;0.25;1" repeatCount="indefinite" values="1;1;0;0"/></circle><circle cx="12" cy="20.5" r="1.5" opacity="0"><set fill="freeze" attributeName="opacity" begin="1.2s" to="1"/><animate attributeName="fill-opacity" begin="1.2s" dur="2.4s" keyTimes="0;0.125;0.25;1" repeatCount="indefinite" values="1;1;0;0"/></circle><circle cx="7.75" cy="19.36" r="1.5" opacity="0"><set fill="freeze" attributeName="opacity" begin="1.4s" to="1"/><animate attributeName="fill-opacity" begin="1.4s" dur="2.4s" keyTimes="0;0.125;0.25;1" repeatCount="indefinite" values="1;1;0;0"/></circle><circle cx="4.64" cy="16.25" r="1.5" opacity="0"><set fill="freeze" attributeName="opacity" begin="1.6s" to="1"/><animate attributeName="fill-opacity" begin="1.6s" dur="2.4s" keyTimes="0;0.125;0.25;1" repeatCount="indefinite" values="1;1;0;0"/></circle><circle cx="3.5" cy="12" r="1.5" opacity="0"><set fill="freeze" attributeName="opacity" begin="1.8s" to="1"/><animate attributeName="fill-opacity" begin="1.8s" dur="2.4s" keyTimes="0;0.125;0.25;1" repeatCount="indefinite" values="1;1;0;0"/></circle><circle cx="4.64" cy="7.75" r="1.5" opacity="0"><set fill="freeze" attributeName="opacity" begin="2s" to="1"/><animate attributeName="fill-opacity" begin="2s" dur="2.4s" keyTimes="0;0.125;0.25;1" repeatCount="indefinite" values="1;1;0;0"/></circle><circle cx="7.75" cy="4.64" r="1.5" opacity="0"><set fill="freeze" attributeName="opacity" begin="2.2s" to="1"/><animate attributeName="fill-opacity" begin="2.2s" dur="2.4s" keyTimes="0;0.125;0.25;1" repeatCount="indefinite" values="1;1;0;0"/></circle></g></svg>
  <span id="actionLabel"></span>
</div>
<div class="modal-overlay" id="updateModal">
  <div class="modal-box" id="updateModalBox">
    <div class="update-wrap">
      <div class="update-main">
        <div id="updateFormView">
          <h3><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>Update panel</h3>
          <div class="version-check" id="versionCheck">
            <div class="vc-loading">
              <svg class="vc-spin" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 3c4.97 0 9 4.03 9 9"></path></svg>
              Checking version on GitHub…
            </div>
          </div>
          <button type="button" class="release-notes-btn" id="releaseNotesBtn" onclick="toggleReleaseNotes()" style="display:none">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line></svg>
            <span id="releaseNotesBtnLabel">Release notes</span>
          </button>
          <p>This will stop the Luanti server, download the latest panel version from GitHub, and restart it. The update replaces the panel file, so set a new login password.</p>
          <label for="updatePassword">New panel password</label>
          <input type="password" id="updatePassword" placeholder="New password">
          <div class="modal-error" id="updateError"></div>
          <div class="modal-actions">
            <button class="btn ghost" id="updateCancelBtn" onclick="closeUpdateModal()">Cancel</button>
            <button class="btn" id="updateConfirmBtn" onclick="confirmUpdate()">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
              Download and update
            </button>
          </div>
        </div>
        <div id="updateProgressView" class="update-progress" style="display:none">
          <span class="upd-spin-lg">
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 3c4.97 0 9 4.03 9 9"><animateTransform attributeName="transform" dur="1.5s" repeatCount="indefinite" type="rotate" values="0 12 12;360 12 12"/></path></svg>
            <svg class="upd-icon-lg" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="currentColor" d="M12 2a1 1 0 0 1 1 1v10.586l2.293-2.293a1 1 0 0 1 1.414 1.414l-4 4a1 1 0 0 1-1.414 0l-4-4a1 1 0 1 1 1.414-1.414L11 13.586V3a1 1 0 0 1 1-1M5 17a1 1 0 0 1 1 1v2h12v-2a1 1 0 1 1 2 0v2a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-2a1 1 0 0 1 1-1"/></svg>
          </span>
          <h4>Update in progress…</h4>
          <p id="updateStep">Stopping the server, downloading and installing the update…</p>
        </div>
      </div>
      <div class="release-notes-panel" id="releaseNotesPanel" style="display:none"></div>
    </div>
  </div>
</div>
<div class="modal-overlay" id="licenseModal" style="display:none">
  <div class="modal-box" style="max-width:800px;max-height:85vh;display:flex;flex-direction:column">
    <h3><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 21H6a3 3 0 0 1-3-3v-1h10v2a2 2 0 0 0 4 0V5a2 2 0 1 1 2 2h-2m2-4H8a3 3 0 0 0-3 3v11M9 7h4m-4 4h4"/></svg>License</h3>
    <div id="licenseContent" style="overflow:auto;flex:1;background:#0d0f14;border:1px solid var(--border);border-radius:8px;padding:12px;white-space:pre-wrap"></div>
    <div class="modal-actions">
      <button class="btn ghost" onclick="closeLicense()">Close</button>
    </div>
  </div>
</div>
<div class="modal-overlay" id="powerModal">
  <div class="modal-box">
    <h3><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><g fill="none" fill-rule="evenodd"><path fill="currentColor" d="M13.5 3a1.5 1.5 0 0 0-3 0v10a1.5 1.5 0 0 0 3 0zM7.854 5.75a1.5 1.5 0 1 0-1.661-2.5A10.49 10.49 0 0 0 1.5 12c0 5.799 4.701 10.5 10.5 10.5S22.5 17.799 22.5 12c0-3.654-1.867-6.87-4.693-8.75a1.5 1.5 0 0 0-1.66 2.5a7.5 7.5 0 1 1-8.292 0Z"/></g></svg>System</h3>
    <p>The Luanti server will be shut down cleanly before the selected action.</p>
    <div class="modal-actions" style="justify-content:space-between">
      <button class="btn ghost" id="powerCancelBtn" onclick="closePowerModal()">Cancel</button>
      <div style="display:flex;gap:8px">
        <button class="btn ghost" onclick="confirmPanelRestart()">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"></polyline><polyline points="1 20 1 14 7 14"></polyline><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg>
          Restart panel
        </button>
        <button class="btn danger" onclick="confirmPanelShutdown()">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18.36 6.64a9 9 0 1 1-12.73 0"></path><line x1="12" y1="2" x2="12" y2="12"></line></svg>
          Shut down panel
        </button>
      </div>
    </div>
  </div>
</div>
<div class="modal-overlay" id="alertModal">
  <div class="modal-box" style="max-width:380px">
    <h3 id="alertModalTitle"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z"/></svg>Error</h3>
    <p id="alertModalMessage" style="margin-bottom:0"></p>
    <div class="modal-actions">
      <button class="btn" onclick="closeAlertBox()">OK</button>
    </div>
  </div>
</div>
<script>
const ICONS = {
  folder: '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><path fill="currentColor" d="M4 20q-.825 0-1.412-.587T2 18V6q0-.825.588-1.412T4 4h6l2 2h8q.825 0 1.413.588T22 8v10q0 .825-.587 1.413T20 20z"/></svg>',
  file: '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><path fill="currentColor" d="M13 9V3.5L18.5 9M6 2c-1.11 0-2 .89-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6z"/></svg>',
  package: '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><path fill="currentColor" d="M21.93 7.67a1 1 0 0 0-.07-.17c-.02-.03-.04-.05-.06-.08c-.03-.04-.06-.09-.1-.13c-.03-.03-.06-.04-.08-.07c-.04-.03-.07-.06-.11-.09h-.01l-9.01-5a.99.99 0 0 0-.97 0l-9.01 5H2.5c-.04.02-.08.06-.11.09a.3.3 0 0 0-.08.07c-.04.04-.07.08-.1.13c-.02.03-.04.05-.06.08c-.03.05-.05.11-.07.17c0 .02-.02.05-.03.07c-.02.08-.04.17-.04.26v8c0 .36.2.7.51.87l9 5s.1.04.14.06c.03.01.06.03.09.03a1.1 1.1 0 0 0 .5 0c.03 0 .06-.02.09-.03c.05-.02.1-.03.14-.06l9-5c.32-.18.51-.51.51-.87V8c0-.09-.01-.18-.04-.26c0-.03-.02-.05-.03-.07ZM12 4.15l6.94 3.86l-2.44 1.36l-6.94-3.86zm-4.5 2.5l6.94 3.86L12 11.87L5.06 8.01zM20 15.42l-7 3.89V13.6l2.5-1.39v3.21l2-1.11V11.1L20 9.71z"/></svg>',
  trash: '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><path fill="currentColor" d="M7 21q-.825 0-1.412-.587T5 19V6q-.425 0-.712-.288T4 5t.288-.712T5 4h4q0-.425.288-.712T10 3h4q.425 0 .713.288T15 4h4q.425 0 .713.288T20 5t-.288.713T19 6v13q0 .825-.587 1.413T17 21zm3.713-4.288Q11 16.426 11 16V9q0-.425-.288-.712T10 8t-.712.288T9 9v7q0 .425.288.713T10 17t.713-.288m4 0Q15 16.426 15 16V9q0-.425-.288-.712T14 8t-.712.288T13 9v7q0 .425.288.713T14 17t.713-.288"/></svg>',
  inbox: '<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"></polyline><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"></path></svg>',
  chevronUp: '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 640 640"><path fill="currentColor" d="M160 288c-12.9 0-24.6-7.8-29.6-19.8s-2.2-25.7 7-34.8l160-160c12.5-12.5 32.8-12.5 45.3 0l160 160c9.2 9.2 11.9 22.9 6.9 34.9S492.9 288 480 288z"/></svg>',
  chevronDown: '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 640 640"><path fill="currentColor" d="M160 352c-12.9 0-24.6 7.8-29.6 19.8s-2.2 25.7 7 34.8l160 160c12.5 12.5 32.8 12.5 45.3 0l160-160c9.2-9.2 11.9-22.9 6.9-34.9S492.9 352 480 352z"/></svg>',
};
let modpackExpanded = {};
let currentPath = "";
function showTab(name){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('nav button').forEach(b=>b.classList.remove('active'));
  document.getElementById('tab-'+name).classList.add('active');
  event.currentTarget.classList.add('active');
  if(name==='mods') loadMods();
  if(name==='files') loadFiles();
  if(name==='config') loadConfig();
  if(name==='debug') loadDebug();
  if(name==='network') loadNetwork();
}
async function api(path, opts={}){
  const r = await fetch(path, opts);
  if(r.status === 401){ location.href='/login'; throw new Error('unauth'); }
  return r;
}
function logout(){ fetch('/api/logout',{method:'POST'}).then(()=>location.href='/login'); }
let actionCount = 0;
function showAction(label){
  actionCount++;
  document.getElementById('actionLabel').textContent = label;
  document.getElementById('actionToast').classList.add('show');
}
function hideAction(){
  actionCount = Math.max(0, actionCount - 1);
  if(actionCount === 0) document.getElementById('actionToast').classList.remove('show');
}
async function withAction(label, fn){
  showAction(label);
  try{ return await fn(); }
  finally{ hideAction(); }
}
function escapeHtml(s){
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function colorizeLine(line){
  let cls = '';
  if(/^>\\s/.test(line)) cls = 'term-cmd';
  else if(/^\\[panel\\]/i.test(line)) cls = 'term-panel';
  else if(/\\bERROR\\b/i.test(line)) cls = 'lvl-error';
  else if(/\\bWARNING\\b/i.test(line)) cls = 'lvl-warning';
  else if(/\\bACTION\\b/i.test(line)) cls = 'lvl-action';
  else if(/\\bINFO\\b/i.test(line)) cls = 'lvl-info';
  else if(/\\bVERBOSE\\b|\\bTRACE\\b/i.test(line)) cls = 'lvl-verbose';
  let text = escapeHtml(line);
  text = text.replace(/(\\[[\\w.: -]+\\])/g, '<span class="term-tag">$1</span>');
  return cls ? `<span class="${cls}">${text}</span>` : text;
}
async function refreshStatus(){
  const r = await api('/api/status');
  const s = await r.json();
  document.getElementById('dot').classList.toggle('on', s.running);
  document.getElementById('statusText').textContent = s.running ? `online (pid ${s.pid})` : 'offline';
  document.getElementById('uptime').textContent = s.running ? `Uptime: ${Math.floor(s.uptime/60)} min` : '';
  document.getElementById('btnStart').disabled = s.running;
  document.getElementById('btnStop').disabled = !s.running;
  document.getElementById('btnRestart').disabled = !s.running;
}
async function loadPanelVersion(){
  try{
    const r = await api('/api/panel/version');
    const d = await r.json();
    document.getElementById('brandVersion').textContent = 'v' + d.version;
  }catch(e){}
}
async function checkUpdateBadge(){
  try{
    const r = await api('/api/panel/check_update');
    if(!r.ok) return;
    const d = await r.json();
    document.getElementById('updateDot').style.display = d.update_available ? 'block' : 'none';
  }catch(e){}
}
async function startServer(){
  await withAction('Starting server…', async ()=>{ await api('/api/start',{method:'POST'}); refreshStatus(); });
}
async function stopServer(){
  await withAction('Stopping server…', async ()=>{ await api('/api/stop',{method:'POST'}); refreshStatus(); });
}
async function restartServer(){
  await withAction('Restarting server…', async ()=>{ await api('/api/restart',{method:'POST'}); refreshStatus(); });
}
async function sendCmd(){
  const inp = document.getElementById('cmdInput');
  if(!inp.value.trim()) return;
  const cmd = inp.value;
  inp.value='';
  await withAction('Sending command…', async ()=>{
    await api('/api/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cmd})});
  });
}
const CONSOLE_DISPLAY_LIMIT = 100;
let consoleLines = [];
function clearConsole(){
  consoleLines = [];
  document.getElementById('console').innerHTML = '';
}
let lastConsoleLen = 0;
async function pollConsole(){
  try{
    const r = await api('/api/console?since='+lastConsoleLen);
    const data = await r.json();
    if(data.lines.length){
      const el = document.getElementById('console');
      const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 20;
      data.lines.forEach(l=>{ consoleLines.push(colorizeLine(l)); });
      if(consoleLines.length > CONSOLE_DISPLAY_LIMIT){
        consoleLines = consoleLines.slice(-CONSOLE_DISPLAY_LIMIT);
      }
      el.innerHTML = consoleLines.join("\\n") + "\\n";
      lastConsoleLen = data.total;
      if(atBottom) el.scrollTop = el.scrollHeight;
    }
  }catch(e){}
}
setInterval(pollConsole, 1500);
setInterval(refreshStatus, 4000);
setInterval(()=>{ if(document.getElementById('tab-network').classList.contains('active')) loadNetwork(); }, 4000);
setInterval(checkUpdateBadge, 10*60*1000);
function renderModRow(m, indented){
  const div = document.createElement('div');
  div.className = 'mod-item' + (indented ? ' mod-item-indented' : '');
  div.innerHTML = `
    <div class="item-left">${ICONS.package}<span class="mod-name">${m.name}</span><span class="size">${(m.size/1024).toFixed(1)} KB</span></div>
    <div class="actions">
      <label class="switch"><input type="checkbox" ${m.enabled?'checked':''}><span class="slider"></span></label>
      <button class="icon-btn-sm" title="Delete">${ICONS.trash}</button>
    </div>`;
  div.querySelector('input[type=checkbox]').addEventListener('change', e => toggleMod(m.folder, m.name, e.target.checked));
  div.querySelector('.icon-btn-sm').addEventListener('click', () => deleteMod(m.folder, m.name));
  return div;
}
async function loadMods(){
  const r = await api('/api/mods');
  const mods = await r.json();
  const el = document.getElementById('modsList');
  el.innerHTML = '';
  if(!mods.length){
    el.innerHTML = `<div class="empty">${ICONS.inbox}No mods installed.</div>`;
    return;
  }
  const standalone = mods.filter(m => !m.modpack);
  const grouped = {};
  mods.filter(m => m.modpack).forEach(m => { (grouped[m.modpack] = grouped[m.modpack] || []).push(m); });
  standalone.forEach(m => el.appendChild(renderModRow(m)));
  Object.keys(grouped).sort().forEach(pack => {
    const items = grouped[pack];
    const expanded = modpackExpanded[pack] === true;
    const allEnabled = items.every(m => m.enabled);
    const allDisabled = items.every(m => !m.enabled);
    const group = document.createElement('div');
    group.className = 'modpack-group';
    const header = document.createElement('div');
    header.className = 'modpack-header';
    header.innerHTML = `
      <div class="item-left">${ICONS.folder}<span class="mod-name">${pack}</span><span class="size">${items.length} mod${items.length>1?'s':''}</span></div>
      <div class="actions">
        <label class="switch"><input type="checkbox" class="modpack-switch" ${allEnabled?'checked':''}><span class="slider"></span></label>
        <button class="icon-btn-sm modpack-delete" title="Delete modpack">${ICONS.trash}</button>
        <button class="icon-btn-sm modpack-toggle" title="${expanded?'Collapse':'Expand'}">${expanded?ICONS.chevronUp:ICONS.chevronDown}</button>
      </div>`;
    const packSwitch = header.querySelector('.modpack-switch');
    packSwitch.indeterminate = !allEnabled && !allDisabled;
    const body = document.createElement('div');
    body.className = 'modpack-body';
    body.style.display = expanded ? 'block' : 'none';
    items.forEach(m => body.appendChild(renderModRow(m, true)));
    const toggleBtn = header.querySelector('.modpack-toggle');
    toggleBtn.addEventListener('click', () => {
      const nowExpanded = body.style.display === 'none';
      body.style.display = nowExpanded ? 'block' : 'none';
      modpackExpanded[pack] = nowExpanded;
      toggleBtn.innerHTML = nowExpanded ? ICONS.chevronUp : ICONS.chevronDown;
      toggleBtn.title = nowExpanded ? 'Collapse' : 'Expand';
    });
    packSwitch.addEventListener('change', e => toggleModpack(pack, e.target.checked));
    header.querySelector('.modpack-delete').addEventListener('click', () => deleteModpack(pack));
    group.appendChild(header);
    group.appendChild(body);
    el.appendChild(group);
  });
}
async function toggleModpack(pack, enabled){
  await withAction((enabled?'Enabling':'Disabling')+' modpack « '+pack+' »…', async ()=>{
    await api('/api/mods/modpack/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({modpack:pack,enabled})});
    loadMods();
  });
}
async function deleteModpack(pack){
  if(!confirm('Delete the entire modpack "'+pack+'" et tous les mods qu\\'il contient ?')) return;
  await withAction('Deleting modpack « '+pack+' »…', async ()=>{
    await api('/api/mods/modpack/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({modpack:pack})});
    loadMods();
  });
}
async function toggleMod(folder, name, enabled){
  await withAction('Updating mod "'+name+'"…', async ()=>{
    await api('/api/mods/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({folder,name,enabled})});
  });
}
async function deleteMod(folder, name){
  if(!confirm('Delete mod "'+name+'"?')) return;
  await withAction('Deleting mod « '+name+' »…', async ()=>{
    await api('/api/mods/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({folder,name})});
    loadMods();
  });
}
async function installGit(){
  const url = document.getElementById('gitUrl').value.trim();
  if(!url) return;
  await withAction('Cloning git repository…', async ()=>{
    const r = await api('/api/mods/git',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})});
    const d = await r.json();
    if(!r.ok) showAlertBox('Error: '+d.error);
    document.getElementById('gitUrl').value='';
    loadMods();
  });
}
async function uploadZip(file){
  if(!file) return;
  await withAction('Installing mod (zip)…', async ()=>{
    const fd = new FormData(); fd.append('file', file);
    const r = await api('/api/mods/upload',{method:'POST',body:fd});
    const d = await r.json();
    if(!r.ok) showAlertBox('Error: '+d.error);
    loadMods();
  });
}
const dz = document.getElementById('dropzone');
dz.addEventListener('dragover', e=>{e.preventDefault(); dz.classList.add('drag');});
dz.addEventListener('dragleave', ()=>dz.classList.remove('drag'));
dz.addEventListener('drop', e=>{e.preventDefault(); dz.classList.remove('drag'); if(e.dataTransfer.files[0]) uploadZip(e.dataTransfer.files[0]);});
async function loadFiles(){
  const r = await api('/api/files?path='+encodeURIComponent(currentPath));
  const items = await r.json();
  const bc = document.getElementById('breadcrumb');
  const parts = currentPath.split('/').filter(Boolean);
  let html = `<span onclick="goPath('')">${ICONS.folder}.minetest</span>`;
  let acc = '';
  parts.forEach(p=>{ acc += (acc?'/':'')+p; html += ' / <span onclick="goPath(\\''+acc+'\\')">'+p+'</span>'; });
  bc.innerHTML = html;
  const el = document.getElementById('filesList');
  el.innerHTML = items.length ? '' : `<div class="empty">${ICONS.inbox}Empty folder.</div>`;
  items.forEach(it=>{
    const div = document.createElement('div');
    div.className = 'file-item';
    const rel = (currentPath ? currentPath+'/' : '') + it.name;
    const icon = it.is_dir ? ICONS.folder : ICONS.file;
    const clickAction = it.is_dir ? `goPath('${rel}')` : `downloadFile('${rel}')`;
    div.innerHTML = `
      <div class="item-left clickable" onclick="${clickAction}">${icon}<span class="file-name">${it.name}</span>${it.is_dir?'':'<span class="size">'+(it.size/1024).toFixed(1)+' KB</span>'}</div>
      <button class="icon-btn-sm" onclick="deleteFile('${rel}')" title="Delete">${ICONS.trash}</button>`;
    el.appendChild(div);
  });
}
function goPath(p){ currentPath = p; loadFiles(); }
function downloadFile(rel){ window.open('/api/files/download?path='+encodeURIComponent(rel)); }
async function deleteFile(rel){
  if(!confirm('Delete "'+rel+'" ?')) return;
  await withAction('Deleting…', async ()=>{
    await api('/api/files/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:rel})});
    loadFiles();
  });
}
async function uploadFile(file){
  if(!file) return;
  await withAction('Uploading file…', async ()=>{
    const fd = new FormData(); fd.append('file', file); fd.append('path', currentPath);
    await api('/api/files/upload',{method:'POST',body:fd});
    loadFiles();
    document.getElementById('fileUpload').value='';
  });
}
async function mkdir(){
  const name = prompt('New folder name:');
  if(!name) return;
  await withAction('Creating folder…', async ()=>{
    await api('/api/files/mkdir',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:(currentPath?currentPath+'/':'')+name})});
    loadFiles();
  });
}
async function loadConfig(){
  const r = await api('/api/config');
  const d = await r.json();
  renderConfigFields(d.fields);
  document.getElementById('rawConfig').value = d.raw;
}
function renderConfigFields(fields){
  const el = document.getElementById('configFields');
  el.innerHTML = '';
  fields.forEach(f=>{
    const wrap = document.createElement('div');
    wrap.style.cssText = 'margin-bottom:16px;padding-bottom:14px;border-bottom:1px solid var(--border)';
    let inputHtml;
    if(f.type === 'bool'){
      const checked = (f.value === 'true' || f.value === true);
      inputHtml = `<label class="switch"><input type="checkbox" data-key="${f.key}" data-type="bool" ${checked?'checked':''}><span class="slider"></span></label>`;
    } else if(f.type === 'number'){
      inputHtml = `<input type="number" data-key="${f.key}" data-type="number" value="${f.value}" style="width:160px">`;
    } else {
      inputHtml = `<input type="text" data-key="${f.key}" data-type="text" value="${f.value}" style="width:100%;max-width:420px">`;
    }
    wrap.innerHTML = `<div style="font-size:13.5px;font-weight:600;margin-bottom:2px">${f.label} <span class="muted" style="font-weight:400">(${f.key})</span></div>
      <div class="muted" style="margin-bottom:8px">${f.desc}</div>
      ${inputHtml}`;
    el.appendChild(wrap);
  });
}
async function saveConfigFields(){
  const inputs = document.querySelectorAll('#configFields [data-key]');
  const fields = {};
  inputs.forEach(inp=>{
    fields[inp.dataset.key] = inp.dataset.type === 'bool' ? (inp.checked ? 'true' : 'false') : inp.value;
  });
  await withAction('Saving configuration…', async ()=>{
    const r = await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({fields})});
    const d = await r.json();
    if(r.ok){ loadConfig(); } else { showAlertBox('Error: '+d.error); }
  });
}
async function saveRawConfig(){
  const raw = document.getElementById('rawConfig').value;
  await withAction('Saving raw file…', async ()=>{
    const r = await api('/api/config/raw',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({raw})});
    if(r.ok){ loadConfig(); } else { const d = await r.json(); showAlertBox('Error: '+d.error); }
  });
}
let debugLines = [];
async function loadDebug(){
  const r = await api('/api/debug?lines=800');
  const d = await r.json();
  debugLines = d.lines;
  renderDebug();
  if(!d.exists){
    document.getElementById('debugConsole').textContent = "debug.txt does not exist yet (the server may never have been started).";
  }
}
function renderDebug(){
  const filter = document.getElementById('debugFilter').value.trim().toLowerCase();
  const el = document.getElementById('debugConsole');
  const filtered = filter ? debugLines.filter(l => l.toLowerCase().includes(filter)) : debugLines;
  el.innerHTML = filtered.length ? filtered.map(colorizeLine).join('\\n') : '(nothing to display)';
  el.scrollTop = el.scrollHeight;
}
async function clearDebug(){
  if(!confirm('Permanently clear debug.txt?')) return;
  await withAction('Clearing debug log…', async ()=>{
    await api('/api/debug/clear',{method:'POST'});
    loadDebug();
  });
}
async function loadNetwork(){
  try{
    const r = await api('/api/network');
    const d = await r.json();
    document.getElementById('netPort').textContent = d.port;
    const errEl = document.getElementById('netError');
    if(d.error){
      errEl.style.display = 'block';
      errEl.textContent = '⚠ ' + d.error;
    } else {
      errEl.style.display = 'none';
    }
    const conns = d.connections || [];
    const peers = conns.filter(c => c.peer);
    document.getElementById('netPeerCount').textContent = peers.length;
    document.getElementById('netSocketCount').textContent = conns.length;
    const tbody = document.getElementById('netTableBody');
    const table = document.getElementById('netTable');
    const empty = document.getElementById('netEmpty');
    if(!conns.length){
      table.style.display = 'none';
      empty.style.display = 'flex';
      empty.innerHTML = `${ICONS.inbox}No active connections on this port right now.`;
      return;
    }
    table.style.display = '';
    empty.style.display = 'none';
    tbody.innerHTML = '';
    conns.forEach(c=>{
      const tr = document.createElement('tr');
      const badgeCls = c.proto && c.proto.startsWith('udp') ? 'udp' : 'tcp';
      tr.innerHTML = `
        <td><span class="badge ${badgeCls}">${c.proto || '?'}</span></td>
        <td>${c.state || '—'}</td>
        <td>${c.local}</td>
        <td>${c.peer || '<span class="muted">—</span>'}</td>
        <td>${c.recv_q}</td>
        <td>${c.send_q}</td>`;
      tbody.appendChild(tr);
    });
  }catch(e){}
}
let updateInProgress = false;
let releaseNotesData = null;
function openUpdateModal(){
  document.getElementById('updateModal').style.display = 'flex';
  document.getElementById('updatePassword').value = '';
  const errEl = document.getElementById('updateError');
  errEl.style.display = 'none';
  errEl.textContent = '';
  releaseNotesData = null;
  document.getElementById('releaseNotesBtn').style.display = 'none';
  document.getElementById('releaseNotesPanel').style.display = 'none';
  document.getElementById('updateModalBox').classList.remove('with-notes');
  showUpdateFormView();
  checkUpdateVersion();
}
async function checkUpdateVersion(){
  const el = document.getElementById('versionCheck');
  el.innerHTML = `<div class="vc-loading"><svg class="vc-spin" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 3c4.97 0 9 4.03 9 9"></path></svg>Checking version on GitHub…</div>`;
  try{
    const r = await api('/api/panel/check_update');
    const d = await r.json();
    if(!r.ok){
      el.innerHTML = `<div class="vc-status error">⚠ ${d.error || 'Unable to check the version.'}</div>`;
      return;
    }
    document.getElementById('updateDot').style.display = d.update_available ? 'block' : 'none';
    releaseNotesData = d;
    document.getElementById('releaseNotesBtn').style.display = 'inline-flex';
    const statusHtml = d.update_available
      ? `<div class="vc-status available"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><path fill="currentColor" d="M5 19q-.425 0-.712-.288T4 18t.288-.712T5 17h1v-7q0-2.075 1.25-3.687T10.5 4.2v-.7q0-.625.438-1.062T12 2t1.063.438T13.5 3.5v.7q2 .5 3.25 2.113T18 10v7h1q.425 0 .713.288T20 18t-.288.713T19 19zm7 3q-.825 0-1.412-.587T10 20h4q0 .825-.587 1.413T12 22"/></svg>New version available</div>`
      : `<div class="vc-status uptodate"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><path fill="currentColor" d="m9.55 15.15l8.475-8.475q.3-.3.7-.3t.7.3t.3.713t-.3.712l-9.175 9.2q-.3.3-.7.3t-.7-.3L4.55 13q-.3-.3-.288-.712t.313-.713t.713-.3t.712.3z"/></svg>The panel is up to date</div>`;
    el.innerHTML = `
      <div class="vc-row"><span class="vc-label">Current version</span><span class="vc-value">v${d.current_version}</span></div>
      <div class="vc-row"><span class="vc-label">GitHub version</span><span class="vc-value">v${d.remote_version}</span></div>
      ${statusHtml}`;
  }catch(e){
    el.innerHTML = `<div class="vc-status error"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><path fill="currentColor" d="M2.725 21q-.275 0-.5-.137t-.35-.363t-.137-.488t.137-.512l9.25-16q.15-.25.388-.375T12 3t.488.125t.387.375l9.25 16q.15.25.138.513t-.138.487t-.35.363t-.5.137zm9.988-3.287Q13 17.425 13 17t-.288-.712T12 16t-.712.288T11 17t.288.713T12 18t.713-.288m0-3Q13 14.425 13 14v-3q0-.425-.288-.712T12 10t-.712.288T11 11v3q0 .425.288.713T12 15t.713-.288"/></svg>Unable to check the version (network problem).</div>`;
  }
}
function toggleReleaseNotes(){
  const panel = document.getElementById('releaseNotesPanel');
  const box = document.getElementById('updateModalBox');
  const btnLabel = document.getElementById('releaseNotesBtnLabel');
  const showing = panel.style.display !== 'none';
  if(showing){
    panel.style.display = 'none';
    box.classList.remove('with-notes');
    btnLabel.textContent = 'Release notes';
  }else{
    renderReleaseNotes();
    panel.style.display = 'block';
    box.classList.add('with-notes');
    btnLabel.textContent = 'Hide release notes';
  }
}
function renderReleaseNotes(){
  const panel = document.getElementById('releaseNotesPanel');
  const d = releaseNotesData;
  if(!d){
    panel.innerHTML = `<h4>Release notes</h4><div class="rn-empty">No information available.</div>`;
    return;
  }
  let html = `<h4><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline></svg>Release notes</h4>`;
  html += `<div class="rn-block"><div class="rn-version">v${escapeHtml(d.current_version)} (installed)</div><div class="rn-text">${escapeHtml(d.current_release_note || '—')}</div></div>`;
  if(d.update_available){
    html += `<div class="rn-block"><div class="rn-version">v${escapeHtml(d.remote_version)} (available)</div><div class="rn-text">${escapeHtml(d.remote_release_note || '—')}</div></div>`;
  }
  panel.innerHTML = html;
}
function closeUpdateModal(){
  if(updateInProgress) return;
  document.getElementById('updateModal').style.display = 'none';
}
document.getElementById('updateModal').addEventListener('click', e=>{
  if(e.target.id === 'updateModal') closeUpdateModal();
});
function showUpdateFormView(){
  updateInProgress = false;
  document.getElementById('updateModal').classList.remove('locked');
  document.getElementById('updateFormView').style.display = 'block';
  document.getElementById('updateProgressView').style.display = 'none';
  document.getElementById('updatePassword').disabled = false;
  document.getElementById('updateConfirmBtn').disabled = false;
  document.getElementById('updateCancelBtn').disabled = false;
}
function showUpdateProgressView(message){
  updateInProgress = true;
  document.getElementById('updateModal').classList.add('locked');
  document.getElementById('updateFormView').style.display = 'none';
  document.getElementById('updateProgressView').style.display = 'block';
  document.getElementById('updateStep').textContent = message;
}
async function confirmUpdate(){
  const pw = document.getElementById('updatePassword').value;
  const errEl = document.getElementById('updateError');
  errEl.style.display = 'none';
  errEl.textContent = '';
  if(!pw || pw.length < 4){
    errEl.textContent = 'The password must contain at least 4 characters.';
    errEl.style.display = 'block';
    return;
  }
  showUpdateProgressView('Stopping the server, downloading and installing the update…');
  try{
    const r = await api('/api/panel/update', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({password: pw})});
    let d = {};
    try{ d = await r.json(); }catch(e){}
    if(!r.ok){
      showUpdateFormView();
      errEl.textContent = 'Error: ' + (d.error || 'unknown');
      errEl.style.display = 'block';
      return;
    }
    showUpdateProgressView('Update installed. Restarting the panel — you will be redirected to the login page…');
    setTimeout(()=>{ location.href = '/login'; }, 4000);
  }catch(e){
    showUpdateProgressView('Panel restart in progress — you will be redirected to the login page…');
    setTimeout(()=>{ location.href = '/login'; }, 4000);
  }
}
let licenseLang = "en";
async function loadLicense() {
  const content = document.getElementById("licenseContent");
  content.innerHTML = `
    <span style="display:inline-flex; align-items:center; font-size:1em;">
      <svg xmlns="http://www.w3.org/2000/svg" 
          width="1em" 
          height="1em" 
          viewBox="0 0 24 24" 
          style="margin-right:6px; flex-shrink:0;">
        <path fill="none" 
              stroke="currentColor" 
              stroke-linecap="round" 
              stroke-linejoin="round" 
              stroke-width="2" 
              d="M12 3c4.97 0 9 4.03 9 9">
          <animateTransform 
            attributeName="transform" 
            dur="1.5s" 
            repeatCount="indefinite" 
            type="rotate" 
            values="0 12 12;360 12 12"/>
        </path>
      </svg>
      <span>Chargement...</span>
    </span>
  `;
  try {
    const r = await fetch("/license");
    content.textContent = await r.text();
  } catch {
    content.textContent = "Unable to load the license.";
  }
}
async function license() {
  document.getElementById("licenseModal").style.display = "flex";
  await loadLicense();
}
function closeLicense(){
  document.getElementById("licenseModal").style.display = "none";
}
document.getElementById("licenseModal").addEventListener("click", e=>{
  if(e.target.id==="licenseModal") closeLicense();
});
let powerActionInProgress = false;
function power(){
  document.getElementById('powerModal').style.display = 'flex';
}
function closePowerModal(){
  if(powerActionInProgress) return;
  document.getElementById('powerModal').style.display = 'none';
}
document.getElementById('powerModal').addEventListener('click', e=>{
  if(e.target.id === 'powerModal') closePowerModal();
});
async function confirmPanelRestart(){
  if(!confirm('Restart the panel? The Luanti server will be stopped and the panel restarted.')) return;
  powerActionInProgress = true;
  await withAction('Restarting the panel…', async ()=>{
    try{ await api('/api/panel/restart_panel', {method:'POST'}); }catch(e){}
  });
  setTimeout(()=>{ location.href = '/login'; }, 2500);
}
async function confirmPanelShutdown(){
  if(!confirm('Shut down the panel completely? You will have to restart the script manually to access it again.')) return;
  powerActionInProgress = true;
  await withAction('Shutting down the panel…', async ()=>{
    try{ await api('/api/panel/shutdown', {method:'POST'}); }catch(e){}
  });
  document.getElementById('powerModal').style.display = 'none';
  document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;font-family:sans-serif;color:#8a8f98;background:#0d0f14">The panel has been shut down. Restart the script to access it again.</div>';
}
function showAlertBox(message, title){
  document.getElementById('alertModalTitle').innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z"/></svg>' + (title || 'Error');
  document.getElementById('alertModalMessage').textContent = message;
  document.getElementById('alertModal').style.display = 'flex';
}
function translateStaticHtml(){
  const translations = {
    'License':'License', 'System':'System', 'Error':'Error', 'Cancel':'Cancel',
    'Update panel':'Update panel', 'Checking version on GitHub…':'Checking version on GitHub…',
    'New panel password':'New panel password', 'New password':'New password',
    'Download and update':'Download and update', 'Server command (e.g. /status)':'Server command (e.g. /status)',
    'Filter (e.g. ERROR, mod name...)':'Filter (e.g. ERROR, mod name...)', 'Refresh':'Refresh',
    'Current settings':'Current settings', 'Advanced configuration (raw minetest.conf)':'Advanced configuration (raw minetest.conf)',
    'Debug log':'Debug log', 'Network traffic':'Network traffic', 'connected peers':'connected peers',
    'listening/active sockets':'listening/active sockets', 'Protocol':'Protocol', 'State':'State',
    'Local address':'Local address', 'Remote address':'Remote address'
  };
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const nodes = [];
  let node;
  while ((node = walker.nextNode())) nodes.push(node);
  nodes.forEach(n => { if (translations[n.nodeValue.trim()]) n.nodeValue = n.nodeValue.replace(n.nodeValue.trim(), translations[n.nodeValue.trim()]); });
  document.querySelectorAll('[placeholder],[title]').forEach(el => ['placeholder','title'].forEach(attr => {
    const value = el.getAttribute(attr);
    if (translations[value]) el.setAttribute(attr, translations[value]);
  }));
}
translateStaticHtml();
function closeAlertBox(){
  document.getElementById('alertModal').style.display = 'none';
}
document.getElementById('alertModal').addEventListener('click', e=>{
  if(e.target.id === 'alertModal') closeAlertBox();
});
document.addEventListener('keydown', e=>{
  if(e.key === 'Escape' && document.getElementById('alertModal').style.display === 'flex') closeAlertBox();
});
refreshStatus();
loadPanelVersion();
checkUpdateBadge();
</script>"""
I18N_JS = r"""
(function(){
  const KEY = "panelLang";
  let dict = {};
  function currentLang(){
    return localStorage.getItem(KEY) || null;
  }
  function apply(root){
    root = root || document.body;
    const activeLang = currentLang();
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    let n;
    while ((n = walker.nextNode())) nodes.push(n);
    for (const node of nodes){
      if (!node.datasetOriginalText) {
        const raw = node.nodeValue;
        const trimmed = raw.trim();
        if (!trimmed) continue;
        node.datasetOriginalText = trimmed;
        node.datasetFullRaw = raw;
      }
      const original = node.datasetOriginalText;
      if (!activeLang) {
        node.nodeValue = node.datasetFullRaw;
      } else if (dict[original]) {
        node.nodeValue = node.datasetFullRaw.replace(original, dict[original]);
      }
    }
    const els = (
      root.nodeType === 1 &&
      root.matches &&
      root.matches("[placeholder],[title],[aria-label]")
    )
      ? [root, ...root.querySelectorAll("[placeholder],[title],[aria-label]")]
      : root.querySelectorAll
        ? root.querySelectorAll("[placeholder],[title],[aria-label]")
        : [];
    els.forEach(el=>{
      ["placeholder","title","aria-label"].forEach(attr=>{
        const origAttrKey = attr + "-original";
        if (!el.hasAttribute(origAttrKey)) {
          const val = el.getAttribute(attr);
          if (val) el.setAttribute(origAttrKey, val);
        }
        const originalVal = el.getAttribute(origAttrKey);
        if (!originalVal) return;
        if (!activeLang) {
          el.setAttribute(attr, originalVal);
        } else if (dict[originalVal]) {
          el.setAttribute(attr, dict[originalVal]);
        }
      });
    });
  }
  async function load(code){
    if (!code) {
      dict = {};
      apply(document.body);
      return;
    }
    try {
      const res = await fetch("/lang/" + code + ".json");
      dict = res.ok ? await res.json() : {};
    } catch(e){
      dict = {};
    }
    apply(document.body);
  }
  function buildSwitcher(langs){
    if (document.getElementById("i18n-switcher")) return;
    const wrap = document.createElement("div");
    wrap.id = "i18n-switcher";
    wrap.style.cssText =
      "position:fixed;bottom:16px;left:16px;z-index:9999;" +
      "font-family:-apple-system,system-ui,'Segoe UI',sans-serif;";
    const btn = document.createElement("button");
    btn.id = "i18n-btn";
    btn.type = "button";
    btn.title = "Change language";
    btn.style.cssText =
      "background:#1e222c;border:1px solid #262b36;color:#8a8f98;" +
      "width:34px;height:34px;border-radius:8px;cursor:pointer;" +
      "display:flex;align-items:center;justify-content:center;" +
      "box-shadow:0 8px 20px rgba(0,0,0,.35);transition:color .15s,border-color .15s;";
    btn.innerHTML =
      '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 16 16"><title xmlns="">translate</title><g fill="currentColor"><path d="M4.545 6.714L4.11 8H3l1.862-5h1.284L8 8H6.833l-.435-1.286zm1.634-.736L5.5 3.956h-.049l-.679 2.022z"/><path d="M0 2a2 2 0 0 1 2-2h7a2 2 0 0 1 2 2v3h3a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2v-3H2a2 2 0 0 1-2-2zm2-1a1 1 0 0 0-1 1v7a1 1 0 0 0 1 1h7a1 1 0 0 0 1-1V2a1 1 0 0 0-1-1zm7.138 9.995q.289.451.63.846c-.748.575-1.673 1.001-2.768 1.292c.178.217.451.635.555.867c1.125-.359 2.08-.844 2.886-1.494c.777.665 1.739 1.165 2.93 1.472c.133-.254.414-.673.629-.89c-1.125-.253-2.057-.694-2.82-1.284c.681-.747 1.222-1.651 1.621-2.757H14V8h-3v1.047h.765c-.318.844-.74 1.546-1.272 2.13a6 6 0 0 1-.415-.492a2 2 0 0 1-.94.31"/></g></svg>';
    btn.addEventListener("mouseenter", ()=>{ btn.style.color = "#eee"; btn.style.borderColor = "#5b8cff"; });
    btn.addEventListener("mouseleave", ()=>{ btn.style.color = "#8a8f98"; btn.style.borderColor = "#262b36"; });
    const panel = document.createElement("div");
    panel.id = "i18n-panel";
    panel.style.cssText =
      "position:absolute;bottom:42px;left:0;min-width:180px;max-height:320px;" +
      "background:#171a22;border:1px solid #262b36;" +
      "border-radius:12px;padding:6px;box-shadow:0 12px 32px rgba(0,0,0,.45);" +
      "display:none;flex-direction:column;";
    const listWrap = document.createElement("div");
    listWrap.id = "i18n-lang-list";
    listWrap.style.cssText =
      "overflow-y:auto;max-height:260px;display:flex;flex-direction:column;gap:2px;";
    function makeOption(code, label, flag){
      const opt = document.createElement("button");
      opt.type = "button";
      opt.dataset.code = code;
      opt.style.cssText =
        "display:flex;align-items:center;gap:10px;width:100%;text-align:left;" +
        "background:none;border:0;color:#e6e6e6;padding:9px 10px;border-radius:8px;" +
        "cursor:pointer;font-size:13.5px;font-weight:500;transition:background .12s;";
      const flagSpan = document.createElement("span");
      flagSpan.style.cssText =
        "display:flex;align-items:center;justify-content:center;width:22px;" +
        "flex-shrink:0;line-height:1;border-radius:2px;overflow:hidden;";
      if (flag && flag.trim().startsWith("<svg")) {
        flagSpan.innerHTML = flag;
      } else if (flag) {
        flagSpan.style.fontSize = "16px";
        flagSpan.textContent = flag;
      } else {
        flagSpan.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><path fill="currentColor" d="m15.075 18.95l-.85 2.425q-.1.275-.35.45t-.55.175q-.5 0-.812-.413t-.113-.912l3.8-10.05q.125-.275.375-.45t.55-.175h.75q.3 0 .55.175t.375.45L22.6 20.7q.2.475-.1.888t-.8.412q-.325 0-.562-.175t-.363-.475l-.85-2.4zM9.05 13.975L4.7 18.3q-.275.275-.687.288T3.3 18.3q-.275-.275-.275-.7t.275-.7l4.35-4.35q-.875-.875-1.588-2T4.75 8h2.1q.5.975 1 1.7t1.2 1.45q.825-.825 1.713-2.313T12.1 6H2q-.425 0-.712-.288T1 5t.288-.712T2 4h6V3q0-.425.288-.712T9 2t.713.288T10 3v1h6q.425 0 .713.288T17 5t-.288.713T16 6h-1.9q-.525 1.8-1.575 3.7t-2.075 2.9l2.4 2.45l-.75 2.05zM15.7 17.2h3.6l-1.8-5.1z"/></svg>';
      }
      const labelSpan = document.createElement("span");
      labelSpan.textContent = label;
      opt.appendChild(flagSpan);
      opt.appendChild(labelSpan);
      opt.addEventListener("mouseenter", ()=>{ if(opt.dataset.code !== (currentLang()||"")) opt.style.background = "#1e222c"; });
      opt.addEventListener("mouseleave", ()=>{ if(opt.dataset.code !== (currentLang()||"")) opt.style.background = "none"; });
      return opt;
    }
    if (!langs.length) {
      const empty = document.createElement("div");
      empty.textContent = "No languages installed";
      empty.style.cssText = "color:#8a8f98;font-size:12.5px;padding:9px 10px;";
      listWrap.appendChild(empty);
    }
    langs.forEach(l=>{
      const opt = makeOption(l.code, l.name, l.flag);
      opt.addEventListener("click", ()=>{
        localStorage.setItem(KEY, l.code);
        load(l.code);
        closePanel();
        updateActiveOption();
      });
      listWrap.appendChild(opt);
    });
    panel.appendChild(listWrap);
    const isAdminPage = !!document.getElementById("updateModal");
    if (isAdminPage) {
      const sep = document.createElement("div");
      sep.style.cssText = "height:1px;background:#262b36;margin:6px 2px;flex-shrink:0;";
      panel.appendChild(sep);
      const dlBtn = buildDownloadButton();
      dlBtn.addEventListener("click", (e)=>{ e.stopPropagation(); closePanel(); openDownloadPanel(); });
      panel.appendChild(dlBtn);
    }
    function updateActiveOption(){
      const active = currentLang() || "";
      listWrap.querySelectorAll("button[data-code]").forEach(o=>{
        const isActive = o.dataset.code === active;
        o.style.background = isActive ? "#1e2b45" : "none";
        o.style.color = isActive ? "#7aa2ff" : "#e6e6e6";
      });
    }
    let panelOpen = false;
    function openPanel(){
      panel.style.display = "flex";
      panelOpen = true;
      updateActiveOption();
    }
    function closePanel(){
      panel.style.display = "none";
      panelOpen = false;
    }
    btn.addEventListener("click", (e)=>{
      e.stopPropagation();
      panelOpen ? closePanel() : openPanel();
    });
    document.addEventListener("click", (e)=>{
      if (panelOpen && !wrap.contains(e.target)) closePanel();
    });
    wrap.appendChild(panel);
    wrap.appendChild(btn);
    document.body.appendChild(wrap);
    updateActiveOption();
  }
  function buildDownloadButton(){
    const btn = document.createElement("button");
    btn.type = "button";
    btn.id = "i18n-download-btn";
    btn.style.cssText =
      "display:flex;align-items:center;gap:10px;width:100%;text-align:left;" +
      "background:none;border:0;color:#7aa2ff;padding:9px 10px;border-radius:8px;" +
      "cursor:pointer;font-size:13.5px;font-weight:600;transition:background .12s;flex-shrink:0;";
    const iconSpan = document.createElement("span");
    iconSpan.style.cssText =
      "display:flex;align-items:center;justify-content:center;width:22px;flex-shrink:0;";
    iconSpan.innerHTML =
      '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><title xmlns="">download-rounded</title><path fill="currentColor" d="M11.625 15.513q-.175-.063-.325-.213l-3.6-3.6q-.3-.3-.288-.7t.288-.7q.3-.3.713-.312t.712.287L11 12.15V5q0-.425.288-.712T12 4t.713.288T13 5v7.15l1.875-1.875q.3-.3.713-.288t.712.313q.275.3.288.7t-.288.7l-3.6 3.6q-.15.15-.325.213t-.375.062t-.375-.062M6 20q-.825 0-1.412-.587T4 18v-2q0-.425.288-.712T5 15t.713.288T6 16v2h12v-2q0-.425.288-.712T19 15t.713.288T20 16v2q0 .825-.587 1.413T18 20z"/></svg>';
    const labelSpan = document.createElement("span");
    labelSpan.textContent = "Download languages";
    btn.appendChild(iconSpan);
    btn.appendChild(labelSpan);
    btn.addEventListener("mouseenter", ()=>{ btn.style.background = "#1e222c"; });
    btn.addEventListener("mouseleave", ()=>{ btn.style.background = "none"; });
    return btn;
  }
  let downloadOverlay = null;
  function openDownloadPanel(){
    const switcherPanel = document.getElementById("i18n-panel");
    if (switcherPanel) switcherPanel.style.display = "none";
    if (!downloadOverlay) {
      downloadOverlay = document.createElement("div");
      downloadOverlay.id = "i18n-download-overlay";
      downloadOverlay.style.cssText =
        "position:fixed;inset:0;background:rgba(5,6,10,.68);backdrop-filter:blur(2px);" +
        "display:flex;align-items:center;justify-content:center;z-index:10000;padding:16px;" +
        "font-family:-apple-system,system-ui,'Segoe UI',sans-serif;";
      const box = document.createElement("div");
      box.style.cssText =
        "background:#171a22;border:1px solid #262b36;border-radius:14px;padding:20px;" +
        "width:100%;max-width:360px;max-height:80vh;display:flex;flex-direction:column;" +
        "box-shadow:0 20px 60px rgba(0,0,0,.55);";
      const header = document.createElement("div");
      header.style.cssText = "display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;";
      const title = document.createElement("h3");
      title.textContent = "Download languages";
      title.style.cssText = "margin:0;font-size:15px;color:#f5f5f5;font-weight:700;";
      const closeBtn = document.createElement("button");
      closeBtn.type = "button";
      closeBtn.innerHTML = "&times;";
      closeBtn.style.cssText = "background:none;border:0;color:#8a8f98;font-size:22px;line-height:1;cursor:pointer;padding:0 4px;";
      closeBtn.addEventListener("click", closeDownloadPanel);
      header.appendChild(title);
      header.appendChild(closeBtn);
      const list = document.createElement("div");
      list.id = "i18n-download-list";
      list.style.cssText = "overflow-y:auto;display:flex;flex-direction:column;gap:2px;min-height:60px;";
      box.appendChild(header);
      box.appendChild(list);
      downloadOverlay.appendChild(box);
      downloadOverlay.addEventListener("click", (e)=>{ if (e.target === downloadOverlay) closeDownloadPanel(); });
      document.body.appendChild(downloadOverlay);
    }
    downloadOverlay.style.display = "flex";
    loadRemoteLangs();
  }
  function closeDownloadPanel(){
    if (downloadOverlay) downloadOverlay.style.display = "none";
  }
  async function loadRemoteLangs(){
    const list = document.getElementById("i18n-download-list");
    list.innerHTML = '<div style="color:#8a8f98;font-size:13px;padding:10px 2px;">Loading…</div>';
    try {
      const res = await fetch("/api/lang/remote");
      const data = await res.json();
      if (!res.ok) {
        list.innerHTML = `<div style="color:#ff6b6b;font-size:13px;padding:10px 2px;">${(data && data.error) || "Unable to load the list."}</div>`;
        return;
      }
      if (!data.length) {
        list.innerHTML = '<div style="color:#8a8f98;font-size:13px;padding:10px 2px;">No languages found.</div>';
        return;
      }
      list.innerHTML = "";
      data.forEach(l => list.appendChild(buildRemoteLangRow(l)));
    } catch(e){
      list.innerHTML = '<div style="color:#ff6b6b;font-size:13px;padding:10px 2px;">Unable to load the list.</div>';
    }
  }
  function buildRemoteLangRow(l){
    const row = document.createElement("div");
    row.style.cssText = "display:flex;align-items:center;gap:10px;padding:8px 6px;border-radius:8px;";
    const flagSpan = document.createElement("span");
    flagSpan.style.cssText =
      "display:flex;align-items:center;justify-content:center;width:22px;" +
      "flex-shrink:0;line-height:1;border-radius:2px;overflow:hidden;";
    if (l.flag && l.flag.trim().startsWith("<svg")) {
      flagSpan.innerHTML = l.flag;
    } else if (l.flag) {
      flagSpan.style.fontSize = "16px";
      flagSpan.textContent = l.flag;
    }
    const textWrap = document.createElement("div");
    textWrap.style.cssText = "flex:1;min-width:0;display:flex;flex-direction:column;";
    const nameEl = document.createElement("span");
    nameEl.textContent = l.name;
    nameEl.style.cssText = "color:#e6e6e6;font-size:13.5px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;";
    
    textWrap.appendChild(nameEl);

    if (l.author) {
      const authorEl = document.createElement("span");
      authorEl.textContent = "by " + l.author;
      authorEl.style.cssText = "color:#6c7280;font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;";
      textWrap.appendChild(authorEl);
    }

    const codeEl = document.createElement("span");
    codeEl.textContent = l.code;
    codeEl.style.cssText = "color:#8a8f98;font-size:11.5px;font-family:ui-monospace,monospace;";
    textWrap.appendChild(codeEl);

    row.appendChild(flagSpan);
    row.appendChild(textWrap);
    if (l.has_update) {
      const updateBtn = document.createElement("button");
      updateBtn.type = "button";
      updateBtn.title = "Update";
      updateBtn.style.cssText =
        "background:#1e222c;border:1px solid #262b36;color:#f0c975;width:28px;height:28px;" +
        "border-radius:7px;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;";
      updateBtn.innerHTML =
        '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><title xmlns="">upgrade-rounded</title><path fill="currentColor" d="M8 20q-.425 0-.712-.288T7 19t.288-.712T8 18h8q.425 0 .713.288T17 19t-.288.713T16 20zm3.288-4.288Q11 15.425 11 15V7.825L9.1 9.7q-.275.275-.687.288T7.7 9.7q-.275-.275-.275-.7t.275-.7l3.6-3.6q.15-.15.325-.212T12 4.425t.375.063t.325.212l3.6 3.6q.275.275.288.688T16.3 9.7q-.275.275-.7.275t-.7-.275L13 7.825V15q0 .425-.287.713T12 16t-.712-.288"/></svg>';
      updateBtn.addEventListener("click", async ()=>{
        updateBtn.disabled = true;
        updateBtn.style.opacity = "0.5";
        try {
          const res = await fetch("/api/lang/download", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({code: l.code})
          });
          const d = await res.json().catch(()=>({}));
          if (!res.ok) throw new Error(d.error || "Update failed.");
          row.remove();
          refreshInstalledLangs();
        } catch(e){
          updateBtn.disabled = false;
          updateBtn.style.opacity = "1";
          updateBtn.title = e.message || "Update failed.";
        }
      });
      row.appendChild(updateBtn);
    } else if (l.installed) {
      const badge = document.createElement("span");
      badge.textContent = "Installed";
      badge.style.cssText = "color:#3ecf8e;font-size:11.5px;font-weight:600;flex-shrink:0;";
      row.appendChild(badge);
    } else {
      const dlBtn = document.createElement("button");
      dlBtn.type = "button";
      dlBtn.title = "Download";
      dlBtn.style.cssText =
        "background:#1e222c;border:1px solid #262b36;color:#7aa2ff;width:28px;height:28px;" +
        "border-radius:7px;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;";
      dlBtn.innerHTML =
        '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><path fill="currentColor" d="M11.625 15.513q-.175-.063-.325-.213l-3.6-3.6q-.3-.3-.288-.7t.288-.7q.3-.3.713-.312t.712.287L11 12.15V5q0-.425.288-.712T12 4t.713.288T13 5v7.15l1.875-1.875q.3-.3.713-.288t.712.313q.275.3.288.7t-.288.7l-3.6 3.6q-.15.15-.325.213t-.375.062t-.375-.062M6 20q-.825 0-1.412-.587T4 18v-2q0-.425.288-.712T5 15t.713.288T6 16v2h12v-2q0-.425.288-.712T19 15t.713.288T20 16v2q0 .825-.587 1.413T18 20z"/></svg>';
      dlBtn.addEventListener("click", async ()=>{
        dlBtn.disabled = true;
        dlBtn.style.opacity = "0.5";
        try {
          const res = await fetch("/api/lang/download", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({code: l.code})
          });
          const d = await res.json().catch(()=>({}));
          if (!res.ok) throw new Error(d.error || "Download failed.");
          row.remove();
          refreshInstalledLangs();
        } catch(e){
          dlBtn.disabled = false;
          dlBtn.style.opacity = "1";
          dlBtn.title = e.message || "Download failed.";
        }
      });
      row.appendChild(dlBtn);
    }
    return row;
  }
  async function refreshInstalledLangs(){
    try {
      const res = await fetch("/api/lang/list");
      const langs = res.ok ? await res.json() : [];
      const existing = document.getElementById("i18n-switcher");
      if (existing) existing.remove();
      buildSwitcher(langs);
    } catch(e){}
  }
  document.addEventListener("DOMContentLoaded", async ()=>{
    const lang = currentLang();
    if (lang) {
      load(lang);
    }
    let langs = [];
    try {
      const res = await fetch("/api/lang/list");
      langs = res.ok ? await res.json() : [];
    } catch(e){}
    if (langs.length || document.getElementById("updateModal")) {
      buildSwitcher(langs);
    }
  });
  new MutationObserver(muts=>{
    for (const m of muts) {
      for (const node of m.addedNodes) {
        if (node.nodeType === 1) {
          apply(node);
        }
      }
    }
  }).observe(document.documentElement, {
    childList:true,
    subtree:true
  });
  window.i18n = {
    setLang: (c)=>{
      if (!c) {
        localStorage.removeItem(KEY);
        dict = {};
        apply(document.body);
        return;
      }
      localStorage.setItem(KEY, c);
      load(c);
    },
    apply,
    currentLang
  };
})();
"""
class Handler(BaseHTTPRequestHandler):
    server_version = "LuantiPanel/1.0"
    def log_message(self, fmt, *args):
        pass
    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def _send_html(self, html, status=200):
        html += '<script src="/i18n.js" defer></script>'
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""
    def _get_json(self):
        try:
            return json.loads(self._read_body().decode("utf-8"))
        except Exception:
            return {}
    def _get_cookie(self, name):
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith(name + "="):
                return part[len(name) + 1:]
        return None
    def _is_authed(self):
        token = self._get_cookie("session")
        if not token:
            return False
        exp = SESSIONS.get(token)
        if not exp or exp < time.time():
            SESSIONS.pop(token, None)
            return False
        return True
    def _require_auth(self):
        if not self._is_authed():
            self._send_json({"error": "unauthorized"}, 401)
            return False
        return True
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        if path == "/login":
            self._send_html(LOGIN_PAGE)
        elif path == "/":
            if self._is_authed():
                self._send_html(DASHBOARD_PAGE)
            else:
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
        elif path == "/api/status":
            if not self._require_auth(): return
            self._send_json(server_status())
        elif path == "/api/console":
            if not self._require_auth(): return
            since = int(qs.get("since", ["0"])[0])
            with console_lock:
                lines = list(console_buffer)
            new_lines = lines[since:] if since <= len(lines) else lines
            self._send_json({"lines": new_lines, "total": len(lines)})
        elif path == "/api/mods":
            if not self._require_auth(): return
            self._send_json(list_mods())
        elif path == "/api/files":
            if not self._require_auth(): return
            try:
                self._send_json(list_dir(qs.get("path", [""])[0]))
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
        elif path == "/api/files/download":
            if not self._require_auth(): return
            try:
                full = safe_rel_path(qs.get("path", [""])[0])
                if not os.path.isfile(full):
                    raise ValueError("File not found.")
                with open(full, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(full)}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
        elif path == "/api/config":
            if not self._require_auth(): return
            self._send_json(get_config_view())
        elif path == "/api/debug":
            if not self._require_auth(): return
            n = int(qs.get("lines", ["500"])[0])
            self._send_json({"lines": tail_text_file(DEBUG_FILE, max_lines=n), "exists": os.path.exists(DEBUG_FILE)})
        elif path == "/api/network":
            if not self._require_auth(): return
            self._send_json(get_network_connections())
        elif path == "/api/panel/version":
            if not self._require_auth(): return
            self._send_json({"version": PANEL_VERSION})
        elif path == "/api/panel/check_update":
            if not self._require_auth(): return
            try:
                self._send_json(check_for_update())
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
        elif path == "/i18n.js":
            body = I18N_JS.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/lang/list":
            self._send_json(list_available_langs())
        elif path == "/api/lang/remote":
            if not self._require_auth(): return
            try:
                self._send_json(list_remote_langs())
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
        elif path.startswith("/lang/") and path.endswith(".json"):
            try:
                code = path[len("/lang/"):-len(".json")]
                full = safe_lang_path(code)
                if not os.path.isfile(full):
                    raise ValueError("Language not found.")
                with open(full, encoding="utf-8") as f:
                    data = f.read()
                body = data.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self._send_json({"error": str(e)}, 404)
        elif path == "/license":
            license_text = """MIT License

        Copyright (c) 2026 Survivalier

        Permission is hereby granted, free of charge, to any person obtaining a copy
        of this software and associated documentation files (the "Software"), to deal
        in the Software without restriction, including without limitation the rights
        to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
        copies of the Software, and to permit persons to whom the Software is
        furnished to do so, subject to the following conditions:

        The above copyright notice and .this permission notice shall be included in all

        copies or substantial portions of the Software.

        THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
        IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
        FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
        AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
        LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
        OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
        SOFTWARE."""
            body = license_text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._send_json({"error": "not found"}, 404)
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/login":
            data = self._get_json()
            pw = data.get("password", "")
            if hashlib.sha256(pw.encode()).hexdigest() == PASSWORD_HASH:
                token = secrets.token_hex(32)
                SESSIONS[token] = time.time() + SESSION_DURATION
                self.send_response(200)
                self.send_header("Set-Cookie", f"session={token}; Path=/; HttpOnly; SameSite=Strict")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                time.sleep(1.0)
                self._send_json({"error": "invalid"}, 401)
            return
        if path == "/api/logout":
            token = self._get_cookie("session")
            SESSIONS.pop(token, None)
            self._send_json({"ok": True})
            return
        if not self._require_auth():
            return
        if path == "/api/start":
            ok, msg = start_server()
            self._send_json({"ok": ok, "message": msg})
        elif path == "/api/stop":
            ok, msg = stop_server()
            self._send_json({"ok": ok, "message": msg})
        elif path == "/api/restart":
            ok, msg = restart_server()
            self._send_json({"ok": ok, "message": msg})
        elif path == "/api/command":
            data = self._get_json()
            ok, msg = send_command(data.get("cmd", ""))
            self._send_json({"ok": ok, "message": msg})
        elif path == "/api/mods/toggle":
            data = self._get_json()
            try:
                folder = data["folder"]
                name = data["name"]
                enabled = bool(data["enabled"])
                rel_path = f"mods/{folder}" if "/" in folder else None
                set_mod_enabled(name, enabled, rel_path)
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
        elif path == "/api/mods/delete":
            data = self._get_json()
            try:
                delete_mod(data["folder"], data["name"])
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
        elif path == "/api/mods/modpack/toggle":
            data = self._get_json()
            try:
                toggle_modpack(data["modpack"], bool(data["enabled"]))
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
        elif path == "/api/mods/modpack/delete":
            data = self._get_json()
            try:
                delete_modpack(data["modpack"])
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
        elif path == "/api/mods/git":
            data = self._get_json()
            try:
                name = install_mod_from_git(data["url"])
                self._send_json({"ok": True, "name": name})
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
        elif path == "/api/mods/upload":
            try:
                ct = self.headers.get("Content-Type", "")
                body = self._read_body()
                fields, files = parse_multipart(body, ct)
                if "file" not in files:
                    raise ValueError("No file received.")
                filename, content = files["file"]
                name = install_mod_from_zip(filename, content)
                self._send_json({"ok": True, "name": name})
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
        elif path == "/api/files/upload":
            try:
                ct = self.headers.get("Content-Type", "")
                body = self._read_body()
                fields, files = parse_multipart(body, ct)
                if "file" not in files:
                    raise ValueError("No file received.")
                filename, content = files["file"]
                target_dir = safe_rel_path(fields.get("path", ""))
                os.makedirs(target_dir, exist_ok=True)
                dest = os.path.join(target_dir, os.path.basename(filename))
                with open(dest, "wb") as f:
                    f.write(content)
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
        elif path == "/api/files/delete":
            data = self._get_json()
            try:
                full = safe_rel_path(data["path"])
                if os.path.isdir(full):
                    shutil.rmtree(full)
                elif os.path.isfile(full):
                    os.remove(full)
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
        elif path == "/api/files/mkdir":
            data = self._get_json()
            try:
                full = safe_rel_path(data["path"])
                os.makedirs(full, exist_ok=True)
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
        elif path == "/api/config":
            data = self._get_json()
            try:
                fields = data.get("fields", {})
                update_conf_keys(fields)
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
        elif path == "/api/config/raw":
            data = self._get_json()
            try:
                write_conf_raw(data.get("raw", ""))
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
        elif path == "/api/debug/clear":
            try:
                clear_debug_file()
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
        elif path == "/api/lang/download":
            data = self._get_json()
            code = data.get("code", "")
            try:
                download_remote_lang(code)
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
        elif path == "/api/panel/update":
            data = self._get_json()
            new_password = data.get("password", "")
            if not isinstance(new_password, str) or len(new_password) < 4:
                self._send_json({"error": "Invalid password (4 characters minimum)."}, 400)
                return
            try:
                stop_server()
            except Exception:
                pass
            try:
                source = download_panel_update()
                new_source = apply_new_password(source, new_password)
                write_panel_source(new_source)
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
                return
            self._send_json({"ok": True})
            schedule_panel_restart()
        elif path == "/api/panel/restart_panel":
            try:
                stop_server()
            except Exception:
                pass
            self._send_json({"ok": True})
            schedule_panel_restart()
        elif path == "/api/panel/shutdown":
            try:
                stop_server()
            except Exception:
                pass
            self._send_json({"ok": True})
            schedule_panel_shutdown()
        else:
            self._send_json({"error": "not found"}, 404)
def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()
CERT_DIR = os.path.join(os.path.expanduser("~"), "Luanti Panel Data")
CERT_FILE = os.path.join(CERT_DIR, "cert.pem")
KEY_FILE = os.path.join(CERT_DIR, "key.pem")
def generate_certificate():
    os.makedirs(CERT_DIR, exist_ok=True)
    regenerate = True
    if os.path.isfile(CERT_FILE) and os.path.isfile(KEY_FILE):
        try:
            result = subprocess.run(
                [
                    "openssl",
                    "x509",
                    "-checkend",
                    "0",
                    "-noout",
                    "-in",
                    CERT_FILE,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if result.returncode == 0:
                regenerate = False
        except Exception:
            regenerate = True
    if not regenerate:
        return
    print("Generating a new HTTPS certificate...")
    for f in (CERT_FILE, KEY_FILE):
        try:
            if os.path.isfile(f):
                os.remove(f)
        except OSError:
            pass
    ip = get_local_ip()
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey", "rsa:2048",
            "-nodes",
            "-days", "3650",
            "-keyout", KEY_FILE,
            "-out", CERT_FILE,
            "-subj",
            "/C=FR/ST=Ile-de-France/L=Paris/O=Survivalier/OU=Luanti Panel/CN=luantipanel.local",
            "-addext",
            f"subjectAltName=DNS:luantipanel.local,IP:{ip}",
        ],
        check=True
    )
    if not os.path.isfile(CERT_FILE):
        raise FileNotFoundError(f"Certificate not found: {CERT_FILE}")
    if not os.path.isfile(KEY_FILE):
        raise FileNotFoundError(f"Private key not found: {KEY_FILE}")
    print("New HTTPS certificate generated.")
    print(f"  Certificate: {CERT_FILE}")
    print(f"  Private key: {KEY_FILE}")
def setup_password():
    while True:
        password = getpass.getpass("Set a password for Luanti Panel:  ")
        if len(password) < 4:
            print("The password must contain at least 4 characters.")
            continue
        confirm = getpass.getpass("Confirm password:  ")
        if password != confirm:
            print("Passwords do not match.\n")
            continue
        break
    with open(__file__, "r", encoding="utf-8") as f:
        source = f.read()
    source = re.sub(
        r'^PASSWORD\s*=\s*".*?"',
        f'PASSWORD = "{password}"',
        source,
        flags=re.MULTILINE
    )
    with open(__file__, "w", encoding="utf-8") as f:
        f.write(source)
    print("\nPassword saved.")
    print("Restarting Luanti Panel...\n")
    os.execv(sys.executable, [sys.executable] + sys.argv)
def main():
    generate_certificate()
    if PASSWORD == "change-me":
      setup_password()
    os.makedirs(MODS_DIR, exist_ok=True)
    os.makedirs(WORLD_DIR, exist_ok=True)
    os.makedirs(FILES_ROOT, exist_ok=True)
    os.makedirs(LANG_DIR, exist_ok=True)
    ip = get_local_ip()
    zeroconf = Zeroconf()
    info = ServiceInfo(
        "_https._tcp.local.",
        "Luanti Panel._https._tcp.local.",
        addresses=[socket.inet_aton(ip)],
        port=PORT,
        properties={
            b"path": b"/",
            b"version": PANEL_VERSION.encode()
        },
        server="luantipanel.local.",
    )
    try:
        zeroconf.register_service(info)
    except Exception as e:
        print(f"Unable to publish the mDNS service: {e}")
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(
        certfile=CERT_FILE,
        keyfile=KEY_FILE
    )
    httpd.socket = context.wrap_socket(
        httpd.socket,
        server_side=True
    )
    print(f"Luanti Panel listening on:")
    print(f"  https://luantipanel.local:{PORT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nPanel stopped.")
    finally:
        try:
            zeroconf.unregister_service(info)
            zeroconf.close()
        except Exception:
            pass
        if server_process is not None and server_process.poll() is None:
            stop_server()
if __name__ == "__main__":
    main()
