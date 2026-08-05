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
PASSWORD = "change-moi-STP"
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
    console_push("[panel] Le processus du serveur s'est arrêté.")
def start_server():
    global server_process, server_start_time
    with server_lock:
        if server_process is not None and server_process.poll() is None:
            return False, "Le serveur tourne déjà."
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
            return False, f"Binaire introuvable : {LUANTI_BIN}"
        server_start_time = time.time()
        t = threading.Thread(target=reader_thread, args=(server_process,), daemon=True)
        t.start()
        console_push(f"[panel] Serveur démarré (pid={server_process.pid}).")
        return True, "Serveur démarré."
def stop_server():
    global server_process
    with server_lock:
        if server_process is None or server_process.poll() is not None:
            return False, "Le serveur n'est pas en cours d'exécution."
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
        console_push("[panel] Serveur arrêté.")
        return True, "Serveur arrêté."
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
            return False, "Le serveur n'est pas en cours d'exécution."
        try:
            server_process.stdin.write((cmd_text + "\n").encode("utf-8"))
            server_process.stdin.flush()
            console_push(f"> {cmd_text}")
            return True, "Commande envoyée."
        except Exception as e:
            return False, str(e)
def download_panel_update():
    """Télécharge la dernière version du script du panel depuis GitHub."""
    req = Request(UPDATE_URL, headers={"User-Agent": "LuantiPanel-Updater"})
    try:
        with urlopen(req, timeout=30) as resp:
            data = resp.read()
    except HTTPError as e:
        raise RuntimeError(f"Échec du téléchargement (HTTP {e.code}).")
    except URLError as e:
        raise RuntimeError(f"Échec du téléchargement : {e.reason}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise RuntimeError("Le fichier téléchargé n'est pas un script texte valide.")
    if "class Handler" not in text or "PASSWORD" not in text:
        raise RuntimeError("Le fichier téléchargé ne ressemble pas à un script de panel valide.")
    return text
def apply_new_password(source_text, new_password):
    """Remplace la ligne PASSWORD = "..." du script téléchargé par le nouveau mot de passe choisi."""
    escaped = new_password.replace("\\", "\\\\").replace('"', '\\"')
    pattern = re.compile(r'^PASSWORD\s*=\s*".*"\s*$', re.MULTILINE)
    if not pattern.search(source_text):
        raise ValueError("Impossible de localiser la ligne PASSWORD dans le nouveau fichier.")
    return pattern.sub(f'PASSWORD = "{escaped}"', source_text, count=1)
def write_panel_source(new_source):
    """Sauvegarde l'ancien script puis écrit la nouvelle version sur disque."""
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
    """Relance le processus du panel (re-exec) après un court délai, pour laisser
    le temps à la réponse HTTP de mise à jour d'être envoyée au navigateur."""
    def _do_restart():
        time.sleep(delay)
        console_push("[panel] Redémarrage du panel après mise à jour…")
        os.execv(sys.executable, [sys.executable] + sys.argv)
    threading.Thread(target=_do_restart, daemon=True).start()
def safe_mod_path(relfolder):
    """Empêche toute évasion du dossier MODS_DIR (path traversal).
    Accepte soit un mod autonome ("mymod"), soit un sous-mod d'un modpack
    ("modpack/submod") — jamais plus d'un niveau d'imbrication."""
    relfolder = (relfolder or "").strip().strip("/")
    parts = [p for p in relfolder.split("/") if p]
    if not parts or len(parts) > 2 or any(p in (".", "..") for p in parts):
        raise ValueError("Nom de mod invalide.")
    full = os.path.normpath(os.path.join(MODS_DIR, *parts))
    base = os.path.normpath(MODS_DIR)
    if full != base and not full.startswith(base + os.sep):
        raise ValueError("Chemin invalide.")
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
    """Retourne {nom_technique_du_mod: bool activé}.
    Dans world.mt, un mod est considéré activé dès que sa valeur n'est pas
    "false" — pour un mod autonome la valeur est "true", mais pour un mod
    faisant partie d'un modpack, Luanti stocke plutôt son chemin relatif,
    ex: load_mod_nations_chat = mods/nationsmod/nations_chat
    """
    enabled = {}
    for line in read_world_mt():
        m = re.match(r"^\s*load_mod_(.+?)\s*=\s*(.*)$", line)
        if m:
            val = m.group(2).strip()
            enabled[m.group(1)] = val != "" and val.lower() != "false"
    return enabled
def set_mod_enabled(modname, enabled, rel_path=None):
    """Active/désactive un mod dans world.mt.
    rel_path (ex: "mods/nationsmod/nations_chat") doit être fourni pour un
    mod appartenant à un modpack ; sinon la valeur "true" est utilisée."""
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
    """Lit le nom déclaré dans mod.conf (name = ...) si présent, sinon
    retombe sur le nom du dossier."""
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
    """Active ou désactive tous les mods d'un modpack en une seule fois."""
    full = safe_mod_path(modpack_name)
    if not os.path.isdir(full) or not is_modpack_dir(full):
        raise ValueError("Modpack introuvable.")
    for sub in sorted(os.listdir(full)):
        subfull = os.path.join(full, sub)
        if not os.path.isdir(subfull) or not is_mod_dir(subfull):
            continue
        techname = mod_technical_name(subfull, sub)
        rel_path = f"mods/{modpack_name}/{sub}"
        set_mod_enabled(techname, enabled, rel_path if enabled else None)
def delete_modpack(modpack_name):
    """Supprime un modpack entier (dossier + toutes ses entrées world.mt)."""
    full = safe_mod_path(modpack_name)
    if not os.path.isdir(full) or not is_modpack_dir(full):
        raise ValueError("Modpack introuvable.")
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
        raise RuntimeError(proc.stderr.strip() or "Échec du clonage git.")
    return os.path.basename(dest)
def install_mod_from_zip(filename, data):
    os.makedirs(MODS_DIR, exist_ok=True)
    tmp_dir = os.path.join(MODS_DIR, "_tmp_" + secrets.token_hex(4))
    os.makedirs(tmp_dir, exist_ok=True)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for member in z.namelist():
                # empêche l'évasion via zip malicieux
                norm = os.path.normpath(member)
                if norm.startswith("..") or os.path.isabs(norm):
                    raise ValueError("Archive suspecte (chemin invalide).")
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
     "desc": "Port réseau sur lequel le serveur écoute les connexions des joueurs."},
    {"key": "server_name", "label": "Nom du serveur", "type": "text", "default": "Mon serveur Luanti",
     "desc": "Nom affiché dans la liste des serveurs publics et en jeu."},
    {"key": "server_description", "label": "Description", "type": "text", "default": "",
     "desc": "Courte description affichée dans la liste des serveurs."},
    {"key": "motd", "label": "Message du jour (MOTD)", "type": "text", "default": "",
     "desc": "Message affiché aux joueurs à leur connexion."},
    {"key": "max_users", "label": "Joueurs max", "type": "number", "default": "15",
     "desc": "Nombre maximum de joueurs connectés simultanément."},
    {"key": "default_privs", "label": "Privilèges par défaut", "type": "text", "default": "interact, shout",
     "desc": "Privilèges accordés automatiquement à un nouveau joueur (ex: interact, shout, fly)."},
    {"key": "creative_mode", "label": "Mode créatif", "type": "bool", "default": "false",
     "desc": "Active l'inventaire créatif illimité pour tous les joueurs."},
    {"key": "enable_damage", "label": "Dégâts activés", "type": "bool", "default": "true",
     "desc": "Active les dégâts (chute, faim, mobs, etc.)."},
    {"key": "enable_pvp", "label": "PvP activé", "type": "bool", "default": "false",
     "desc": "Autorise les joueurs à se blesser entre eux."},
    {"key": "disallow_empty_password", "label": "Interdire mot de passe vide", "type": "bool", "default": "true",
     "desc": "Empêche la connexion avec un mot de passe de compte vide."},
    {"key": "strict_protocol_version_checking", "label": "Vérification stricte du protocole", "type": "bool", "default": "false",
     "desc": "Rejette les clients dont la version de protocole ne correspond pas exactement."},
    {"key": "static_spawnpoint", "label": "Point d'apparition fixe", "type": "text", "default": "",
     "desc": "Coordonnées fixes d'apparition, format x,y,z (vide = aléatoire)."},
    {"key": "max_block_send_distance", "label": "Distance d'envoi des blocs", "type": "number", "default": "10",
     "desc": "Distance (en mapblocks) de terrain envoyée aux joueurs. Impacte les perfs réseau."},
    {"key": "active_block_range", "label": "Portée des blocs actifs", "type": "number", "default": "4",
     "desc": "Distance dans laquelle les blocs sont simulés activement (mobs, cultures, etc.)."},
    {"key": "time_speed", "label": "Vitesse du temps", "type": "number", "default": "72",
     "desc": "Vitesse d'écoulement du cycle jour/nuit (72 = 1 jour réel = 20 min)."},
    {"key": "kick_msg_crash", "label": "Message de kick en cas de crash", "type": "text", "default": "",
     "desc": "Message affiché aux joueurs si le serveur plante et les déconnecte."},
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
    """Met à jour ou ajoute des clés dans minetest.conf sans toucher au reste du fichier."""
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
            "error": "Aucun outil réseau trouvé. Installe-le avec : pkg install iproute2"}
def safe_rel_path(rel):
    rel = (rel or "").strip().lstrip("/")
    full = os.path.normpath(os.path.join(FILES_ROOT, rel))
    base = os.path.normpath(FILES_ROOT)
    if full != base and not full.startswith(base + os.sep):
        raise ValueError("Chemin invalide.")
    return full
def list_dir(rel):
    full = safe_rel_path(rel)
    if not os.path.isdir(full):
        raise ValueError("Dossier introuvable.")
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
        raise ValueError("boundary manquant")
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
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Luanti Panel — Connexion</title>
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
<p class="sub">Connexion au panneau d'administration</p>
<div id="err"></div>
<div class="field">
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect><path d="M7 11V7a5 5 0 0 1 10 0v4"></path></svg>
  <input type="password" id="pw" placeholder="Mot de passe" autofocus>
</div>
<button class="submit" onclick="login()">
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M12 5l7 7-7 7"></path></svg>
  Connexion
</button>
</div>
<script>
document.getElementById('pw').addEventListener('keydown', e => { if(e.key==='Enter') login(); });
async function login(){
  const pw = document.getElementById('pw').value;
  const r = await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({password: pw})});
  if(r.ok){ location.href = '/'; } else { document.getElementById('err').textContent = '⚠ Mot de passe incorrect.'; }
}
</script>
</body></html>"""
DASHBOARD_PAGE = """<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Luanti Panel</title>
<style>
:root{--bg:#0d0f14;--panel:#171a22;--panel2:#1e222c;--accent:#5b8cff;--accent2:#4a7cff;--good:#3ecf8e;--bad:#ff6b6b;--muted:#8a8f98;--border:#262b36}
*{box-sizing:border-box}
body{font-family:-apple-system,system-ui,"Segoe UI",sans-serif;background:var(--bg);color:#e6e6e6;margin:0}
header{display:flex;align-items:center;justify-content:space-between;padding:14px 20px;background:var(--panel);position:sticky;top:0;z-index:10;border-bottom:1px solid var(--border)}
.brand{display:flex;align-items:center;gap:10px}
.brand-icon{width:32px;height:32px;border-radius:9px;background:linear-gradient(135deg,var(--accent),#8a5bff);display:flex;align-items:center;justify-content:center;flex-shrink:0}
header h1{font-size:15px;margin:0;font-weight:700}
.status-pill{display:flex;align-items:center;gap:6px;font-size:12.5px;color:var(--muted);background:var(--panel2);padding:5px 10px;border-radius:20px}
.status-dot{width:8px;height:8px;border-radius:50%;background:var(--bad);flex-shrink:0}
.status-dot.on{background:var(--good);box-shadow:0 0 6px var(--good)}
.icon-btn{background:var(--panel2);border:1px solid var(--border);color:var(--muted);width:34px;height:34px;border-radius:8px;cursor:pointer;display:flex;align-items:center;justify-content:center}
.icon-btn:hover{color:#eee;border-color:var(--accent)}
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
#console::-webkit-scrollbar {
    width: 5px;
}
#console::-webkit-scrollbar-track {
    background: transparent;
}
#console::-webkit-scrollbar-thumb {
    background: #777;
    border-radius: 999px;
}
#console::-webkit-scrollbar-thumb:hover {
    background: #999;
}
#console::-webkit-scrollbar-button {
    display: none;
    width: 0;
    height: 0;
}
#console::-webkit-scrollbar-corner {
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
</style></head>
<body>
<header>
  <div class="brand">
    <div class="brand-icon"><svg width="20" height="20" viewBox="0 0 24 24" fill="#fff"><path d="M6.11 0L1.76 2.516v4.478L3.638 8.08L.073 10.137v6.97L12.013 24l11.773-6.96l.14-.083v-6.672l-3.323-1.92V6.148l-1.061-.613l-1.156.774v.775l-1.11-.64v-.948c-.002-.11-.053-.182-.138-.24l-4.166-2.404a.28.28 0 0 0-.28 0l-2.62 1.515v-2.08Zm0 .64l3.41 1.966v4.297L6.11 8.867L2.312 6.676V2.834Zm6.721 2.77l3.613 2.086l-4.382 2.531a.277.277 0 0 0 0 .48l3.27 1.891l-7.2 4.07l-7.227-4.171L4.19 8.398l.684.397v2.217l1.236.715l1.239-.715V8.795l2.722-1.572V5.008Zm3.89 2.569v.466l-3.56 2.059l-.406-.234zm2.84.208l.487.282v4.33l-.496.287l-.614-.354V6.605ZM17 6.926l1.387.8v3.327l1.166.674l1.05-.61V9.006l2.77 1.6v.49L19.548 13.3l-3.381-1.951v-.944a.28.28 0 0 0-.139-.246l-2.314-1.338ZM5.429 9.113l.681.397l.686-.397v1.576l-.686.397l-.681-.397Zm-4.8 1.662l7.362 4.252c.086.05.19.051.278.002l7.343-4.154v.473l-7.76 4.386v1.43l.864.498v1.11l3.297 1.902l6.925-4.08v-1.19l1.11-.64v-1.112q1.661-.96 3.324-1.916v1.024l-2.217 1.277v.557l-1.11.638v1.11l-1.107.64v2.28l-6.93 4.095l-3.599-2.08V20.17l-1.06-.611v-1.11c-.385-.225-.773-.445-1.159-.67v-2.215l-3.324-1.92v1.11l-1.107-.64v3.325l-1.131-.652Zm15.26 1.053c1.21.697 2.402 1.392 3.604 2.082v.533l-1.107.641v1.191l-6.375 3.758l-2.742-1.582v-1.11l-.86-.495v-.787zm7.483 1.57v3.24l-3.879 2.24v-1.577l1.11-.64v-1.108l1.107-.64v-.556zM3.421 14.604l2.217 1.28v1.577l-1.446-.834l-1.879 1.086v-2.64l1.108.64zm1.32 1.392l-.138.24l.119.069l.138-.24zm.36.207l-.14.24l.12.07l.139-.24zm-.909 1.065l1.446.834l1.11.638v1.11l1.106.642v.469l-5.027-2.904Z"></path></svg></div>
    <h1>Luanti Panel</h1>
  </div>
  <div class="right-group">
    <div class="status-pill"><span class="status-dot" id="dot"></span><span id="statusText">...</span></div>
    <button class="icon-btn" onclick="openUpdateModal()" title="Mettre à jour le panel">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
    </button>
    <button class="icon-btn" onclick="logout()" title="Déconnexion">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"></path><polyline points="16 17 21 12 16 7"></polyline><line x1="21" y1="12" x2="9" y2="12"></line></svg>
    </button>
  </div>
</header>
<nav>
  <button class="active" onclick="showTab('server')">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 17 10 11 4 5"></polyline><line x1="12" y1="19" x2="20" y2="19"></line></svg>
    Serveur
  </button>
  <button onclick="showTab('mods')">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="3" x2="6" y2="15"></line><circle cx="18" cy="6" r="3"></circle><circle cx="6" cy="18" r="3"></circle><path d="M18 9a9 9 0 0 1-9 9"></path></svg>
    Mods
  </button>
  <button onclick="showTab('files')">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>
    Fichiers
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
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"></polyline></svg>
    Réseau
  </button>
</nav>
<main>
<div class="tab active" id="tab-server">
  <div class="card">
    <h3><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>Contrôle du serveur</h3>
    <div class="actions">
      <button class="btn" id="btnStart" onclick="startServer()">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
        Démarrer
      </button>
      <button class="btn danger" id="btnStop" onclick="stopServer()">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><rect x="5" y="5" width="14" height="14" rx="1"></rect></svg>
        Arrêter
      </button>
      <button class="btn ghost" id="btnRestart" onclick="restartServer()">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"></polyline><polyline points="1 20 1 14 7 14"></polyline><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg>
        Redémarrer
      </button>
    </div>
    <p class="muted" id="uptime" style="margin-top:12px;margin-bottom:0"></p>
  </div>
  <div class="card">
    <h3><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 17 10 11 4 5"></polyline><line x1="12" y1="19" x2="20" y2="19"></line></svg>Console</h3>
    <div id="console"></div>
    <div class="row">
      <input id="cmdInput" placeholder="Commande serveur (ex: /status)" onkeydown="if(event.key==='Enter')sendCmd()">
      <button class="btn" onclick="sendCmd()">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"></line><polygon points="22 2 15 22 11 13 2 9 22 2"></polygon></svg>
        Envoyer
      </button>
      <button class="btn ghost" onclick="clearConsole()" title="Vider l'affichage de la console">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path></svg>
        Vider
      </button>
    </div>
  </div>
</div>
<div class="tab" id="tab-mods">
  <div class="card">
    <h3><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="3" x2="6" y2="15"></line><circle cx="18" cy="6" r="3"></circle><circle cx="6" cy="18" r="3"></circle><path d="M18 9a9 9 0 0 1-9 9"></path></svg>Installer un mod</h3>
    <div class="row">
      <input id="gitUrl" placeholder="URL du dépôt git (https://...)">
      <button class="btn" onclick="installGit()">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="3" x2="6" y2="15"></line><circle cx="18" cy="6" r="3"></circle><circle cx="6" cy="18" r="3"></circle><path d="M18 9a9 9 0 0 1-9 9"></path></svg>
        Cloner
      </button>
    </div>
    <div class="dropzone" id="dropzone" onclick="document.getElementById('zipInput').click()">
      <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M16 16l-4-4-4 4"></path><path d="M12 12v9"></path><path d="M20.39 18.39A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.3"></path></svg>
      Cliquer ou déposer un fichier .zip de mod ici
    </div>
    <input type="file" id="zipInput" accept=".zip" style="display:none" onchange="uploadZip(this.files[0])">
  </div>
  <div class="card">
    <h3><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2L2 7l10 5 10-5-10-5z"></path><path d="M2 17l10 5 10-5"></path><path d="M2 12l10 5 10-5"></path></svg>Mods installés</h3>
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
        Dossier
      </button>
    </div>
    <div id="filesList" style="margin-top:10px"></div>
  </div>
</div>
<div class="tab" id="tab-config">
  <div class="card">
    <h3><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg>Paramètres courants</h3>
    <p class="muted" style="margin:-6px 0 16px">Ces réglages modifient <code>minetest.conf</code>. Un redémarrage du serveur est nécessaire pour qu'ils prennent effet.</p>
    <div id="configFields"></div>
    <div class="row" style="margin-top:4px">
      <button class="btn" onclick="saveConfigFields()">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"></path><polyline points="17 21 17 13 7 13 7 21"></polyline><polyline points="7 3 7 8 15 8"></polyline></svg>
        Enregistrer
      </button>
    </div>
  </div>
  <div class="card">
    <h3><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 17 10 11 4 5"></polyline><line x1="12" y1="19" x2="20" y2="19"></line></svg>Configuration avancée (minetest.conf brut)</h3>
    <p class="muted" style="margin:-6px 0 14px">Tous les paramètres possibles de Luanti ne peuvent pas être listés individuellement (il en existe des centaines selon la version et les mods). Ce champ affiche le fichier <code>minetest.conf</code> tel quel : tu peux y ajouter, modifier ou supprimer n'importe quelle clé au format <code>nom_du_parametre = valeur</code>, une par ligne.</p>
    <textarea id="rawConfig" spellcheck="false" style="width:100%;height:260px;background:#0d0f14;border:1px solid var(--border);border-radius:8px;color:#eee;font-family:ui-monospace,monospace;font-size:12.5px;padding:10px;resize:vertical"></textarea>
    <div class="row">
      <button class="btn ghost" onclick="loadConfig()">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"></polyline><polyline points="1 20 1 14 7 14"></polyline><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg>
        Recharger
      </button>
      <button class="btn" onclick="saveRawConfig()">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"></path><polyline points="17 21 17 13 7 13 7 21"></polyline><polyline points="7 3 7 8 15 8"></polyline></svg>
        Enregistrer le fichier brut
      </button>
    </div>
  </div>
</div>
<div class="tab" id="tab-debug">
  <div class="card">
    <h3><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="8" y="6" width="8" height="14" rx="4"></rect><path d="M19 7l-3 2"></path><path d="M5 7l3 2"></path><path d="M19 19l-3-2"></path><path d="M5 19l3-2"></path><line x1="12" y1="2" x2="12" y2="6"></line><line x1="3" y1="13" x2="8" y2="13"></line><line x1="16" y1="13" x2="21" y2="13"></line></svg>Journal de débogage</h3>
    <p class="muted" style="margin:-6px 0 14px"><code>debug.txt</code> contient les logs internes du serveur. Chaque ligne commence en général par un niveau : <b style="color:var(--bad)">ERROR</b> (erreur bloquante, souvent un crash de mod), <b style="color:#f0c975">WARNING</b> (problème non bloquant), <b style="color:#7ee787">ACTION</b> (connexion/déconnexion, chat, placement de bloc), <b>INFO</b> (information générale), <b class="muted">VERBOSE / TRACE</b> (détails techniques). Utile pour diagnostiquer un mod qui plante ou un script Lua en erreur.</p>
    <div class="row" style="margin-bottom:10px">
      <input id="debugFilter" placeholder="Filtrer (ex: ERROR, nom du mod...)" oninput="renderDebug()" style="flex:1">
      <button class="btn ghost" onclick="loadDebug()" title="Rafraîchir">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"></polyline><polyline points="1 20 1 14 7 14"></polyline><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg>
      </button>
      <button class="btn danger" onclick="clearDebug()">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path></svg>
        Vider
      </button>
    </div>
    <div id="debugConsole" style="background:#000;color:#d8dee9;font-family:ui-monospace,monospace;font-size:12px;padding:12px;height:420px;overflow-y:auto;border-radius:10px;white-space:pre-wrap;border:1px solid var(--border)"></div>
  </div>
</div>
<div class="tab" id="tab-network">
  <div class="card">
    <h3 style="justify-content:space-between;display:flex">
      <span style="display:flex;align-items:center;gap:8px"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"></polyline></svg>Trafic réseau — port <span id="netPort">…</span></span>
      <button class="icon-btn" onclick="loadNetwork()" title="Rafraîchir">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"></polyline><polyline points="1 20 1 14 7 14"></polyline><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg>
      </button>
    </h3>
    <p class="muted" style="margin:-6px 0 16px">Liste des connexions actives (joueurs et pairs) sur le port du serveur Luanti, lue depuis la table des sockets du système (<code>ss</code>/<code>netstat</code>) — mise à jour automatiquement toutes les 4 secondes tant que cet onglet est ouvert. Ceci montre qui est connecté et l'état des files d'attente réseau, pas le contenu des paquets : pour une inspection paquet par paquet il faudrait un outil de capture (ex. <code>tcpdump</code>), qui nécessite généralement les droits root sur Android et n'est pas fourni ici.</p>
    <div class="net-summary">
      <div class="net-stat"><div class="n" id="netPeerCount">0</div><div class="l">pairs connectés</div></div>
      <div class="net-stat"><div class="n" id="netSocketCount">0</div><div class="l">sockets en écoute/actives</div></div>
    </div>
    <div id="netError" class="muted" style="display:none;margin-bottom:10px;color:var(--bad)"></div>
    <div style="overflow-x:auto">
      <table class="net-table" id="netTable">
        <thead><tr><th>Protocole</th><th>État</th><th>Adresse locale</th><th>Adresse distante</th><th>Recv-Q</th><th>Send-Q</th></tr></thead>
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
  <div class="modal-box">
    <div id="updateFormView">
      <h3><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>Mettre à jour le panel</h3>
      <p>Ceci va arrêter le serveur Luanti, télécharger la dernière version du panel depuis GitHub, puis redémarrer. La mise à jour remplace le fichier du panel, définis donc un nouveau mot de passe de connexion.</p>
      <label for="updatePassword">Nouveau mot de passe du panel</label>
      <input type="password" id="updatePassword" placeholder="Nouveau mot de passe">
      <div class="modal-error" id="updateError"></div>
      <div class="modal-actions">
        <button class="btn ghost" id="updateCancelBtn" onclick="closeUpdateModal()">Annuler</button>
        <button class="btn" id="updateConfirmBtn" onclick="confirmUpdate()">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
          Télécharger et mettre à jour
        </button>
      </div>
    </div>
    <div id="updateProgressView" class="update-progress" style="display:none">
      <span class="upd-spin-lg">
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 3c4.97 0 9 4.03 9 9"><animateTransform attributeName="transform" dur="1.5s" repeatCount="indefinite" type="rotate" values="0 12 12;360 12 12"/></path></svg>
        <svg class="upd-icon-lg" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="currentColor" d="M12 2a1 1 0 0 1 1 1v10.586l2.293-2.293a1 1 0 0 1 1.414 1.414l-4 4a1 1 0 0 1-1.414 0l-4-4a1 1 0 1 1 1.414-1.414L11 13.586V3a1 1 0 0 1 1-1M5 17a1 1 0 0 1 1 1v2h12v-2a1 1 0 1 1 2 0v2a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-2a1 1 0 0 1 1-1"/></svg>
      </span>
      <h4>Mise à jour en cours…</h4>
      <p id="updateStep">Arrêt du serveur, téléchargement et installation de la mise à jour…</p>
    </div>
  </div>
</div>
<script>
const ICONS = {
  folder: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>',
  file: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline></svg>',
  package: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2L2 7l10 5 10-5-10-5z"></path><path d="M2 17l10 5 10-5"></path><path d="M2 12l10 5 10-5"></path></svg>',
  trash: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path><path d="M10 11v6"></path><path d="M14 11v6"></path></svg>',
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
  document.getElementById('statusText').textContent = s.running ? `en ligne (pid ${s.pid})` : 'hors ligne';
  document.getElementById('uptime').textContent = s.running ? `Uptime : ${Math.floor(s.uptime/60)} min` : '';
  document.getElementById('btnStart').disabled = s.running;
  document.getElementById('btnStop').disabled = !s.running;
  document.getElementById('btnRestart').disabled = !s.running;
}
async function startServer(){
  await withAction('Démarrage du serveur…', async ()=>{ await api('/api/start',{method:'POST'}); refreshStatus(); });
}
async function stopServer(){
  await withAction('Arrêt du serveur…', async ()=>{ await api('/api/stop',{method:'POST'}); refreshStatus(); });
}
async function restartServer(){
  await withAction('Redémarrage du serveur…', async ()=>{ await api('/api/restart',{method:'POST'}); refreshStatus(); });
}
async function sendCmd(){
  const inp = document.getElementById('cmdInput');
  if(!inp.value.trim()) return;
  const cmd = inp.value;
  inp.value='';
  await withAction('Envoi de la commande…', async ()=>{
    await api('/api/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cmd})});
  });
}
function clearConsole(){
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
      data.lines.forEach(l=>{ el.innerHTML += colorizeLine(l) + "\\n"; });
      lastConsoleLen = data.total;
      if(atBottom) el.scrollTop = el.scrollHeight;
    }
  }catch(e){}
}
setInterval(pollConsole, 1500);
setInterval(refreshStatus, 4000);
setInterval(()=>{ if(document.getElementById('tab-network').classList.contains('active')) loadNetwork(); }, 4000);
function renderModRow(m, indented){
  const div = document.createElement('div');
  div.className = 'mod-item' + (indented ? ' mod-item-indented' : '');
  div.innerHTML = `
    <div class="item-left">${ICONS.package}<span class="mod-name">${m.name}</span><span class="size">${(m.size/1024).toFixed(1)} Ko</span></div>
    <div class="actions">
      <label class="switch"><input type="checkbox" ${m.enabled?'checked':''}><span class="slider"></span></label>
      <button class="icon-btn-sm" title="Supprimer">${ICONS.trash}</button>
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
    el.innerHTML = `<div class="empty">${ICONS.inbox}Aucun mod installé.</div>`;
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
        <button class="icon-btn-sm modpack-delete" title="Supprimer le modpack">${ICONS.trash}</button>
        <button class="icon-btn-sm modpack-toggle" title="${expanded?'Réduire':'Déployer'}">${expanded?ICONS.chevronUp:ICONS.chevronDown}</button>
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
      toggleBtn.title = nowExpanded ? 'Réduire' : 'Déployer';
    });
    packSwitch.addEventListener('change', e => toggleModpack(pack, e.target.checked));
    header.querySelector('.modpack-delete').addEventListener('click', () => deleteModpack(pack));
    group.appendChild(header);
    group.appendChild(body);
    el.appendChild(group);
  });
}
async function toggleModpack(pack, enabled){
  await withAction((enabled?'Activation':'Désactivation')+' du modpack « '+pack+' »…', async ()=>{
    await api('/api/mods/modpack/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({modpack:pack,enabled})});
    loadMods();
  });
}
async function deleteModpack(pack){
  if(!confirm('Supprimer tout le modpack "'+pack+'" et tous les mods qu\\'il contient ?')) return;
  await withAction('Suppression du modpack « '+pack+' »…', async ()=>{
    await api('/api/mods/modpack/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({modpack:pack})});
    loadMods();
  });
}
async function toggleMod(folder, name, enabled){
  await withAction('Mise à jour du mod « '+name+' »…', async ()=>{
    await api('/api/mods/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({folder,name,enabled})});
  });
}
async function deleteMod(folder, name){
  if(!confirm('Supprimer le mod "'+name+'" ?')) return;
  await withAction('Suppression du mod « '+name+' »…', async ()=>{
    await api('/api/mods/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({folder,name})});
    loadMods();
  });
}
async function installGit(){
  const url = document.getElementById('gitUrl').value.trim();
  if(!url) return;
  await withAction('Clonage du dépôt git…', async ()=>{
    const r = await api('/api/mods/git',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})});
    const d = await r.json();
    if(!r.ok) alert('Erreur : '+d.error);
    document.getElementById('gitUrl').value='';
    loadMods();
  });
}
async function uploadZip(file){
  if(!file) return;
  await withAction('Installation du mod (zip)…', async ()=>{
    const fd = new FormData(); fd.append('file', file);
    const r = await api('/api/mods/upload',{method:'POST',body:fd});
    const d = await r.json();
    if(!r.ok) alert('Erreur : '+d.error);
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
  el.innerHTML = items.length ? '' : `<div class="empty">${ICONS.inbox}Dossier vide.</div>`;
  items.forEach(it=>{
    const div = document.createElement('div');
    div.className = 'file-item';
    const rel = (currentPath ? currentPath+'/' : '') + it.name;
    const icon = it.is_dir ? ICONS.folder : ICONS.file;
    const clickAction = it.is_dir ? `goPath('${rel}')` : `downloadFile('${rel}')`;
    div.innerHTML = `
      <div class="item-left clickable" onclick="${clickAction}">${icon}<span class="file-name">${it.name}</span>${it.is_dir?'':'<span class="size">'+(it.size/1024).toFixed(1)+' Ko</span>'}</div>
      <button class="icon-btn-sm" onclick="deleteFile('${rel}')" title="Supprimer">${ICONS.trash}</button>`;
    el.appendChild(div);
  });
}
function goPath(p){ currentPath = p; loadFiles(); }
function downloadFile(rel){ window.open('/api/files/download?path='+encodeURIComponent(rel)); }
async function deleteFile(rel){
  if(!confirm('Supprimer "'+rel+'" ?')) return;
  await withAction('Suppression…', async ()=>{
    await api('/api/files/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:rel})});
    loadFiles();
  });
}
async function uploadFile(file){
  if(!file) return;
  await withAction('Envoi du fichier…', async ()=>{
    const fd = new FormData(); fd.append('file', file); fd.append('path', currentPath);
    await api('/api/files/upload',{method:'POST',body:fd});
    loadFiles();
    document.getElementById('fileUpload').value='';
  });
}
async function mkdir(){
  const name = prompt('Nom du nouveau dossier :');
  if(!name) return;
  await withAction('Création du dossier…', async ()=>{
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
  await withAction('Enregistrement de la configuration…', async ()=>{
    const r = await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({fields})});
    const d = await r.json();
    if(r.ok){ loadConfig(); } else { alert('Erreur : '+d.error); }
  });
}
async function saveRawConfig(){
  const raw = document.getElementById('rawConfig').value;
  await withAction('Enregistrement du fichier brut…', async ()=>{
    const r = await api('/api/config/raw',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({raw})});
    if(r.ok){ loadConfig(); } else { const d = await r.json(); alert('Erreur : '+d.error); }
  });
}
let debugLines = [];
async function loadDebug(){
  const r = await api('/api/debug?lines=800');
  const d = await r.json();
  debugLines = d.lines;
  renderDebug();
  if(!d.exists){
    document.getElementById('debugConsole').textContent = "debug.txt n'existe pas encore (le serveur n'a peut-être jamais été démarré).";
  }
}
function renderDebug(){
  const filter = document.getElementById('debugFilter').value.trim().toLowerCase();
  const el = document.getElementById('debugConsole');
  const filtered = filter ? debugLines.filter(l => l.toLowerCase().includes(filter)) : debugLines;
  el.innerHTML = filtered.length ? filtered.map(colorizeLine).join('\\n') : '(rien à afficher)';
  el.scrollTop = el.scrollHeight;
}
async function clearDebug(){
  if(!confirm('Vider définitivement le fichier debug.txt ?')) return;
  await withAction('Suppression du journal de débogage…', async ()=>{
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
      empty.innerHTML = `${ICONS.inbox}Aucune connexion active sur ce port pour le moment.`;
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
function openUpdateModal(){
  document.getElementById('updateModal').style.display = 'flex';
  document.getElementById('updatePassword').value = '';
  const errEl = document.getElementById('updateError');
  errEl.style.display = 'none';
  errEl.textContent = '';
  showUpdateFormView();
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
    errEl.textContent = 'Le mot de passe doit contenir au moins 4 caractères.';
    errEl.style.display = 'block';
    return;
  }
  showUpdateProgressView('Arrêt du serveur, téléchargement et installation de la mise à jour…');
  try{
    const r = await api('/api/panel/update', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({password: pw})});
    let d = {};
    try{ d = await r.json(); }catch(e){}
    if(!r.ok){
      showUpdateFormView();
      errEl.textContent = 'Erreur : ' + (d.error || 'inconnue');
      errEl.style.display = 'block';
      return;
    }
    showUpdateProgressView('Mise à jour installée. Redémarrage du panel — tu vas être redirigé vers la connexion…');
    setTimeout(()=>{ location.href = '/login'; }, 4000);
  }catch(e){
    showUpdateProgressView('Redémarrage du panel en cours — tu vas être redirigé vers la connexion…');
    setTimeout(()=>{ location.href = '/login'; }, 4000);
  }
}
refreshStatus();
</script>
</body></html>"""
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
                    raise ValueError("Fichier introuvable.")
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
        elif path == "/license":
            license_text = """MIT License

        Copyright (c) 2026 Survivalier

        Permission is hereby granted, free of charge, to any person obtaining a copy
        of this software and associated documentation files (the "Software"), to deal
        in the Software without restriction, including without limitation the rights
        to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
        copies of the Software, and to permit persons to whom the Software is
        furnished to do so, subject to the following conditions:

        The above copyright notice and this permission notice shall be included in all
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
                    raise ValueError("Aucun fichier reçu.")
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
                    raise ValueError("Aucun fichier reçu.")
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
        elif path == "/api/panel/update":
            data = self._get_json()
            new_password = data.get("password", "")
            if not isinstance(new_password, str) or len(new_password) < 4:
                self._send_json({"error": "Mot de passe invalide (4 caractères minimum)."}, 400)
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
        else:
            self._send_json({"error": "not found"}, 404)
def main():
    if PASSWORD == "change-moi-STP":
        print("!! ATTENTION : change la variable PASSWORD en haut du fichier avant usage.")
    os.makedirs(MODS_DIR, exist_ok=True)
    os.makedirs(WORLD_DIR, exist_ok=True)
    os.makedirs(FILES_ROOT, exist_ok=True)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Luanti Panel en écoute sur http://{HOST}:{PORT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt du panneau.")
        if server_process is not None and server_process.poll() is None:
            stop_server()
if __name__ == "__main__":
    main()
