#!/usr/bin/env python3
"""
Action Shark - Linux Wine/Proton Trainer Manager
Overhaul: Desktop Pro UI (Faithful to Mockup 2)
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from collections import deque

from PyQt6.QtWidgets import (
    QApplication,
    QWidget,
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QPushButton,
    QLineEdit,
    QLabel,
    QFileDialog,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QHeaderView,
    QMessageBox,
    QInputDialog,
    QMenu,
    QProgressBar,
    QSplitter,
    QComboBox,
    QFrame,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QIcon

import wemod_manager as wm
import styles
from ui_components import (
    StatusBadgeDelegate,
    FilterChipBar,
    GameBannerWidget,
    WeModDialog,
    SettingsDialog,
    get_shark_icon,
    get_cheatengine_icon,
    get_wemod_icon,
    get_folder_icon,
    get_gear_icon,
    get_refresh_icon,
    get_filter_icon,
    get_back_icon,
    get_minimize_icon,
    get_maximize_icon,
    get_close_icon,
    get_search_icon,
    RoundedButton,
    SurfaceFrame,
)


CONFIG_PATH = os.path.expanduser('~/.config/trainer_manager/config.json')
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BUILT_PREFIX_DIR = os.path.expanduser('~/.config/trainer_manager/built_prefixes')


# ── helpers ────────────────────────────────────────────────────────

def _read_file_safe(path, mode='r'):
    try:
        return Path(path).read_text() if mode == 'r' else Path(path).read_bytes()
    except (OSError, PermissionError):
        return '' if mode == 'r' else b''


def load_config():
    try:
        return json.loads(Path(CONFIG_PATH).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(cfg):
    Path(CONFIG_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(CONFIG_PATH).write_text(json.dumps(cfg, indent=2))


def _steam_libraries():
    steam_roots = [
        os.path.expanduser('~/.local/share/Steam'),
        os.path.expanduser('~/.steam/steam'),
        os.path.expanduser('~/.steam/root'),
    ]
    libs = []
    for r in steam_roots:
        if os.path.isdir(r) and r not in libs:
            libs.append(r)
    vdf_path = None
    for lib in libs:
        p = os.path.join(lib, 'steamapps', 'libraryfolders.vdf')
        if os.path.isfile(p):
            vdf_path = p
            break
    if not vdf_path:
        return libs
    vdf = vdf_path
    raw = _read_file_safe(vdf)
    if raw:
        for m in re.finditer(r'"path"\s+"([^"]+)"', raw):
            p = m.group(1)
            if p and os.path.isdir(p):
                libs.append(p)
    return libs


def _find_steam_prefix(appid):
    for lib in _steam_libraries():
        pfx = os.path.join(lib, 'steamapps', 'compatdata', str(appid), 'pfx')
        if os.path.isdir(pfx):
            return pfx
    return None


# ── Steam games (via protontricks) ─────────────────────────────────

def get_steam_games():
    try:
        r = subprocess.run(
            ['protontricks', '-l'],
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []

    games = []
    for line in r.stdout.strip().splitlines():
        line = line.strip()
        if '(' in line and line.endswith(')'):
            name = line[:line.rfind('(')].strip()
            appid = line[line.rfind('(') + 1:-1].strip()
            if appid.isdigit():
                games.append({
                    'name': name,
                    'appid': appid,
                    'wineprefix': _find_steam_prefix(appid),
                    'source': 'Steam',
                    'pid': None,
                })
    return games


# ── running Wine/Proton processes ──────────────────────────────────

def get_running_wine_entries():
    entries = []
    seen_prefixes = set()

    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        pid = entry

        environ_raw = _read_file_safe(f'/proc/{pid}/environ', 'rb')
        if not environ_raw:
            continue
        environ = environ_raw.decode('utf-8', errors='replace').split('\0')
        env = {}
        for var in environ:
            if '=' in var:
                k, v = var.split('=', 1)
                env[k] = v

        wineprefix = env.get('WINEPREFIX')
        if not wineprefix:
            continue

        cmdline_raw = _read_file_safe(f'/proc/{pid}/cmdline', 'rb')
        cmdline = cmdline_raw.decode('utf-8', errors='replace').replace('\0', ' ').strip() if cmdline_raw else ''
        comm = _read_file_safe(f'/proc/{pid}/comm').strip()

        if 'wineserver' in comm.lower():
            continue

        is_wine_proc = (
            'wine' in comm.lower() or 'wine' in cmdline.lower() or
            comm.lower() in ('wine', 'wine64', 'wine-preloader') or
            '.exe' in cmdline.lower()
        )
        if not is_wine_proc:
            continue

        is_proton = 'proton' in cmdline.lower()
        exe_path = None
        appid = None

        for part in cmdline.split():
            if part.lower().endswith('.exe') and os.path.isfile(part):
                exe_path = part
                break
            m2 = re.search(r"'([^']+\.exe)'", part)
            if m2 and os.path.isfile(m2.group(1)):
                exe_path = m2.group(1)
                break

        proc_name = comm
        if exe_path:
            proc_name = os.path.basename(exe_path)
        elif '.exe' in cmdline.lower():
            m = re.search(r'([^/\s]+\.exe)', cmdline, re.I)
            if m:
                proc_name = m.group(1)

        m = re.search(r'compatdata/(\d+)/', cmdline)
        if m:
            appid = m.group(1)

        launcher_keywords = [
            'lutris', 'bottles', 'heroic', 'epicgames', 'epic',
            'gog', 'battlenet', 'battle.net', 'ubisoft', 'uplay',
            'origin', 'ea', 'galaxy', 'launcher', 'upc', 'socialclub',
        ]
        is_launcher = any(k in proc_name.lower() for k in launcher_keywords)

        entry = {
            'name': proc_name,
            'pid': int(pid),
            'comm': comm,
            'cmdline': cmdline,
            'wineprefix': wineprefix,
            'appid': appid,
            'exe_path': exe_path,
            'is_proton': is_proton,
            'is_launcher': is_launcher,
        }
        entries.append(entry)
        if wineprefix not in seen_prefixes:
            seen_prefixes.add(wineprefix)

    return entries


# ── launcher-config prefix detection ───────────────────────────────

def _detect_lutris():
    out = []
    cfg_dir = os.path.expanduser('~/.config/lutris/games/')
    if not os.path.isdir(cfg_dir):
        return out
    for fname in os.listdir(cfg_dir):
        if not fname.endswith('.json'):
            continue
        try:
            data = json.loads(Path(os.path.join(cfg_dir, fname)).read_text())
        except (OSError, json.JSONDecodeError):
            continue
        pfx = data.get('wine_prefix') or ''
        if pfx and os.path.isdir(pfx):
            out.append({
                'name': f"Lutris: {data.get('name', fname)}",
                'wineprefix': pfx,
                'source': 'Lutris',
                'exe': data.get('exe', ''),
            })
    return out


def _detect_bottles():
    out = []
    for base in [
        os.path.expanduser('~/.var/app/com.usebottles.bottles/data/bottles/'),
        os.path.expanduser('~/.local/share/bottles/'),
    ]:
        if not os.path.isdir(base):
            continue
        for bottle in os.listdir(base):
            pfx = os.path.join(base, bottle)
            if os.path.isdir(os.path.join(pfx, 'drive_dos')):
                out.append({
                    'name': f"Bottles: {bottle}",
                    'wineprefix': pfx,
                    'source': 'Bottles',
                    'exe': '',
                })
    return out


def _detect_heroic():
    out = []
    for cfg_dir in [
        os.path.expanduser('~/.config/heroic/'),
        os.path.expanduser('~/.var/app/com.heroicgameslauncher.hgl/config/heroic/'),
    ]:
        if not os.path.isdir(cfg_dir):
            continue

        cfg_file = os.path.join(cfg_dir, 'config.json')
        if os.path.isfile(cfg_file):
            try:
                data = json.loads(Path(cfg_file).read_text())
            except (OSError, json.JSONDecodeError):
                data = {}
            for game_id, game_data in data.items():
                if not isinstance(game_data, dict):
                    continue
                pfx = game_data.get('winePrefix') or game_data.get('wine_prefix', '')
                if pfx and os.path.isdir(pfx):
                    out.append({
                        'name': f"Heroic: {game_data.get('title', game_id)}",
                        'wineprefix': pfx,
                        'source': 'Heroic',
                        'exe': game_data.get('executable', ''),
                    })

        gc_dir = os.path.join(cfg_dir, 'GamesConfig')
        if os.path.isdir(gc_dir):
            for fname in os.listdir(gc_dir):
                if not fname.endswith('.json'):
                    continue
                try:
                    gc = json.loads(Path(os.path.join(gc_dir, fname)).read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                pfx = gc.get('winePrefix') or gc.get('wine_prefix', '')
                if pfx and os.path.isdir(pfx):
                    out.append({
                        'name': f"Heroic: {gc.get('title', fname[:-5])}",
                        'wineprefix': pfx,
                        'source': 'Heroic',
                        'exe': gc.get('executable', ''),
                    })
    return out


def _detect_portproton():
    out = []
    seen_real = set()
    bases = [
        os.path.expanduser('~/.var/app/ru.linux_gaming.PortProton/data/prefixes/'),
        os.path.expanduser('~/PortProton/prefixes/'),
        os.path.expanduser('~/PortProton/data/prefixes/'),
    ]
    for base in bases:
        if not os.path.isdir(base):
            continue
        for entry in os.listdir(base):
            pfx = os.path.join(base, entry)
            if not os.path.isdir(os.path.join(pfx, 'drive_c')):
                continue
            real = os.path.realpath(pfx)
            if real in seen_real:
                continue
            seen_real.add(real)
            out.append({
                'name': entry,
                'wineprefix': real,
                'source': 'PortProton',
                'exe': '',
            })
    return out


def _detect_playonlinux():
    out = []
    pol = os.path.expanduser('~/.PlayOnLinux/wineprefix/')
    if not os.path.isdir(pol):
        return out
    for name in os.listdir(pol):
        pfx = os.path.join(pol, name)
        if os.path.isdir(os.path.join(pfx, 'drive_c')):
            out.append({
                'name': f"PlayOnLinux: {name}",
                'wineprefix': pfx,
                'source': 'PlayOnLinux',
                'exe': '',
            })
    return out


def _detect_hydra():
    out = []
    base = os.path.expanduser('~/.config/hydralauncher/wine-prefixes')
    if not os.path.isdir(base):
        return out
    for entry in sorted(os.listdir(base)):
        pfx = os.path.join(base, entry)
        if not os.path.isdir(os.path.join(pfx, 'drive_c')):
            continue
        out.append({
            'name': f"Hydra: {entry}",
            'wineprefix': pfx,
            'source': 'Hydra',
            'exe': '',
        })
    return out


def _detect_custom():
    out = []
    default = os.path.expanduser('~/.wine')
    if os.path.isdir(os.path.join(default, 'drive_c')):
        out.append({
            'name': 'Default Wine (~/.wine)',
            'wineprefix': default,
            'source': 'Custom',
            'exe': '',
        })
    for base in [os.path.expanduser('~/Games'), os.path.expanduser('~/wineprefixes')]:
        if not os.path.isdir(base):
            continue
        for item in os.listdir(base):
            pfx = os.path.join(base, item)
            if os.path.isdir(os.path.join(pfx, 'drive_c')):
                out.append({
                    'name': item,
                    'wineprefix': pfx,
                    'source': 'Custom',
                    'exe': '',
                })
    return out


_LAUNCHER_DETECTORS = [
    _detect_lutris,
    _detect_bottles,
    _detect_heroic,
    _detect_portproton,
    _detect_playonlinux,
    _detect_hydra,
    _detect_custom,
]


def get_launcher_prefixes():
    out = []
    for fn in _LAUNCHER_DETECTORS:
        out.extend(fn())
    return out


def get_custom_prefixes():
    cfg = load_config()
    return cfg.get('custom_prefixes', [])


def scan_trainer_exes(folder_path):
    if not folder_path or not os.path.isdir(folder_path):
        return []
    exes = []
    for root, dirs, files in os.walk(folder_path):
        for f in files:
            if f.lower().endswith('.exe'):
                exes.append(os.path.join(root, f))
    return exes


def get_all_entries():
    seen = set()
    items = []
    hidden = set(load_config().get('hidden_prefixes', []))

    def _add(entry):
        key = f"{entry.get('source','')}:{entry.get('appid','')}:{entry.get('wineprefix','')}"
        if key not in seen:
            seen.add(key)
            if entry.get('source') != 'Não-Steam' and entry.get('wineprefix', '') in hidden:
                return
            items.append(entry)

    for g in get_steam_games():
        _add(g)

    for p in get_running_wine_entries():
        pfx = p['wineprefix']
        appid = p['appid']
        if appid:
            steam_pfx = _find_steam_prefix(appid)
            if steam_pfx:
                pfx = steam_pfx
        if not pfx and appid:
            pfx = _find_steam_prefix(appid)

        label = p['name']
        if p['is_proton']:
            label += ' [Proton]'
        if p['is_launcher']:
            label = f"Launcher: {label}"

        _add({
            'name': label,
            'appid': appid,
            'wineprefix': pfx or '',
            'source': f"PID {p['pid']} — {p['comm']}",
            'exe': p.get('exe_path', ''),
            'pid': p['pid'],
            'is_proton': p['is_proton'],
        })

    for l in get_launcher_prefixes():
        _add({
            'name': l['name'],
            'appid': None,
            'wineprefix': l['wineprefix'],
            'source': l['source'],
            'exe': l.get('exe', ''),
            'is_proton': False,
        })

    for c in get_custom_prefixes():
        pfx = c.get('wineprefix', '')
        if pfx:
            _add({
                'name': c.get('name', pfx),
                'appid': None,
                'wineprefix': pfx,
                'source': 'Não-Steam',
                'exe': '',
                'is_proton': False,
            })

    return items


# ── wine binary ────────────────────────────────────────────────────

def find_wine_binary(wineprefix=None):
    if wineprefix:
        from wemod_manager import _get_wine_binary
        wb = _get_wine_binary(wineprefix)
        if wb:
            return wb
    steam_roots = [
        os.path.expanduser('~/.local/share/Steam'),
        os.path.expanduser('~/.steam/steam'),
        os.path.expanduser('~/.steam/root'),
    ]
    candidates = []
    for r in steam_roots:
        common = os.path.join(r, 'steamapps', 'common')
        if os.path.isdir(common):
            candidates.append(common)
        compat = os.path.join(r, 'compatibilitytools.d')
        if os.path.isdir(compat):
            candidates.append(compat)

    proton_dirs = []
    for base in candidates:
        if not os.path.isdir(base):
            continue
        for d in os.listdir(base):
            if 'proton' in d.lower():
                proton_dirs.append(os.path.join(base, d))

    for d in proton_dirs:
        for name in ('wine', 'wine64'):
            wine = os.path.join(d, 'files', 'bin', name)
            if os.path.isfile(wine):
                return wine

    for candidate in ('/usr/bin/wine', '/usr/local/bin/wine'):
        if os.path.isfile(candidate):
            return candidate
    for candidate in ('/usr/bin/wine64', '/usr/local/bin/wine64'):
        if os.path.isfile(candidate):
            return candidate
    return shutil.which('wine') or shutil.which('wine64') or 'wine'


def get_available_wine_runners(wineprefix=None):
    """Return one executable per real installation, with the preferred one first."""
    runners = []
    seen = set()

    def identity(path):
        # wine and wine64 belong to one runner even when they are distinct
        # executables. Resolve directory aliases before comparing installations.
        binary_dir = Path(os.path.realpath(os.path.dirname(path)))
        if binary_dir.name == 'bin' and binary_dir.parent.name in ('files', 'dist'):
            return str(binary_dir.parent.parent)
        return os.path.realpath(path)

    def add(label, path):
        if os.path.isfile(path) and identity(path) not in seen:
            seen.add(identity(path))
            runners.append((label, os.path.realpath(path)))

    def display_name(directory):
        manifest = _read_file_safe(os.path.join(directory, 'compatibilitytool.vdf'))
        match = re.search(r'"display_name"\s+"([^"\n]+)"', manifest)
        return match.group(1) if match else os.path.basename(directory)

    for cand in ('/usr/bin/wine', '/usr/bin/wine64', '/usr/local/bin/wine'):
        if os.path.isfile(cand):
            add('System Wine', cand)
            break

    scanned = set()
    for r in _steam_libraries():
        candidates = [
            os.path.join(r, 'compatibilitytools.d'),
            os.path.join(r, 'steamapps', 'common'),
        ]
        for base in candidates:
            base = os.path.realpath(base)
            if base in scanned or not os.path.isdir(base):
                continue
            scanned.add(base)
            for d in sorted(os.listdir(base), key=str.casefold):
                directory = os.path.join(base, d)
                # CachyOS and other valid tools need not have "proton" in
                # their folder name. Keep a fallback for older installations.
                if not (os.path.isfile(os.path.join(directory, 'compatibilitytool.vdf'))
                        or os.path.isfile(os.path.join(directory, 'proton'))
                        or 'proton' in d.lower()):
                    continue
                for relative in ('files/bin/wine', 'files/bin/wine64',
                                 'dist/bin/wine', 'dist/bin/wine64'):
                    binary = os.path.join(directory, relative)
                    if os.path.isfile(binary):
                        add(display_name(directory), binary)
                        break

    if wineprefix:
        preferred = find_wine_binary(wineprefix)
        if preferred and os.path.isfile(preferred):
            key = identity(preferred)
            for index, (label, path) in enumerate(runners):
                if identity(path) == key:
                    runners.pop(index)
                    runners.insert(0, (label, os.path.realpath(preferred)))
                    break
            else:
                directory = key if os.path.isdir(key) else os.path.dirname(key)
                label = display_name(directory) if os.path.isdir(key) else f'Wine ({os.path.basename(preferred)})'
                runners.insert(0, (label, os.path.realpath(preferred)))

    return runners


def _setup_proton_env_for_exe(env, wine_bin):
    wine_real = os.path.realpath(wine_bin)
    wine_dir = os.path.dirname(wine_real)
    for base in (os.path.dirname(wine_dir), os.path.dirname(os.path.dirname(wine_dir))):
        lib = os.path.join(base, 'lib')
        lib64 = os.path.join(base, 'lib64')
        dll_paths = []
        lib_paths = []
        for d in (lib64, lib):
            wine_dll = os.path.join(d, 'wine')
            if os.path.isdir(wine_dll):
                dll_paths.append(wine_dll)
            if os.path.isdir(d):
                lib_paths.append(d)
        if dll_paths:
            env['WINEDLLPATH'] = ':'.join(dll_paths)
        if lib_paths:
            existing = env.get('LD_LIBRARY_PATH', '')
            env['LD_LIBRARY_PATH'] = ':'.join(lib_paths) + (':' + existing if existing else '')
            break


def run_exe_in_prefix(wine_bin, exe_path, wineprefix, pin=None, extra_env=None):
    logfile = f'/tmp/trainer_{pin or os.getpid()}.log'
    env = os.environ.copy()
    env['WINEPREFIX'] = wineprefix
    if extra_env:
        env.update(extra_env)

    if 'proton' in wine_bin.lower():
        _setup_proton_env_for_exe(env, wine_bin)

    subprocess.Popen(
        [wine_bin, exe_path],
        env=env,
        stdout=open(logfile, 'w'),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return logfile


def _find_wine_bin_for_pid(pid):
    seen = set()
    while pid and pid not in seen:
        seen.add(pid)
        raw = _read_file_safe(f'/proc/{pid}/cmdline', 'rb')
        if raw:
            for part in raw.decode('utf-8', errors='replace').split('\0'):
                part = part.strip("'\"")
                if re.search(r'/wine(64)?$', part) and os.path.isfile(part):
                    return part
        env_raw = _read_file_safe(f'/proc/{pid}/environ', 'rb')
        if env_raw:
            for var in env_raw.decode('utf-8', errors='replace').split('\0'):
                if var.startswith('WINEDLLPATH='):
                    base_part = var.split('=', 1)[1].split(':', 1)[0]
                    base_dir = base_part.rsplit('/files/', 1)[0]
                    for name in ('wine', 'wine64'):
                        candidate = os.path.join(base_dir, 'files', 'bin', name)
                        if os.path.isfile(candidate):
                            return candidate
        status = _read_file_safe(f'/proc/{pid}/status')
        if not status:
            break
        m = re.search(r'PPid:\s+(\d+)', status)
        if not m:
            break
        pid = int(m.group(1))
    return None


# ── UI: ProtonRunner (Exact Mockup 2 Desktop Pro) ────────────────────

class ProtonRunner(QWidget):

    def __init__(self):
        super().__init__()
        self.setWindowTitle('Action Shark')
        self.setWindowIcon(get_shark_icon(32))
        # The mockup uses a custom, frameless window chrome.
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumSize(980, 620)
        self.resize(1270, 676)
        self._drag_origin = None

        self.setStyleSheet(styles.GLOBAL_STYLESHEET)
        self.config = load_config()

        # Thread queues for built prefix / backups
        self._built_msg_queue = deque()
        self._built_progress_queue = deque()
        self._built_task_done = False
        self._built_task_ok = False
        self._built_poll = QTimer(self)
        self._built_poll.timeout.connect(self._built_poll_tick)
        self._built_poll.setInterval(100)

        self._build_ui()
        self.ensurePolished()
        self.search_bar.setFixedSize(516, 35)
        for button in (self.ce_btn, self.wemod_btn, self.open_folder_btn):
            button.setFixedHeight(53)
        self._load_tree()

        # Clear initial focus so search bar doesn't start focused with outline
        self.tree.setFocus()

        # 5-second auto refresh
        self._auto_refresh_timer = QTimer(self)
        self._auto_refresh_timer.timeout.connect(self._auto_refresh)
        self._auto_refresh_timer.start(5000)

    def _build_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # The mockup is one rounded surface with both headers integrated.
        surface = SurfaceFrame()
        surface.setObjectName("surface")
        surface_layout = QHBoxLayout(surface)
        surface_layout.setContentsMargins(1, 1, 1, 1)
        surface_layout.setSpacing(0)

        left_container = QFrame()
        left_container.setObjectName("tableContainer")
        left_layout = QVBoxLayout(left_container)
        left_layout.setContentsMargins(18, 0, 18, 16)
        left_layout.setSpacing(0)

        left_header = QFrame()
        left_header.setObjectName("leftHeader")
        left_header.setFixedHeight(62)
        top_bar_layout = QHBoxLayout(left_header)
        top_bar_layout.setContentsMargins(0, 0, 0, 0)
        top_bar_layout.setSpacing(0)

        # Left: Vector Shark Icon + Title
        logo_layout = QHBoxLayout()
        logo_layout.setSpacing(8)
        shark_label = QLabel()
        shark_label.setPixmap(get_shark_icon(30).pixmap(30, 30))
        logo_layout.addWidget(shark_label)

        title_label = QLabel("Action Shark")
        title_label.setStyleSheet("font-size: 23px; font-weight: 500; color: #e5e5e8;")
        logo_layout.addWidget(title_label)
        top_bar_layout.addLayout(logo_layout)

        top_bar_layout.addStretch()

        # Center: Search Bar (Pill) + Filter Icon
        search_container = QHBoxLayout()
        search_container.setSpacing(12)

        self.search_bar = QLineEdit()
        self.search_bar.setObjectName("searchBar")
        self.search_bar.setPlaceholderText("Search...")
        self.search_bar.setFixedWidth(516)
        self.search_bar.setFixedHeight(35)
        self.search_bar.addAction(get_search_icon(16), QLineEdit.ActionPosition.LeadingPosition)
        self.search_bar.setClearButtonEnabled(True)
        self.search_bar.textChanged.connect(self._apply_filters)
        search_container.addWidget(self.search_bar)

        filter_menu_btn = QPushButton()
        filter_menu_btn.setIcon(get_filter_icon(18))
        filter_menu_btn.setObjectName("headerIconBtn")
        filter_menu_btn.setFixedSize(28, 32)
        filter_menu_btn.setToolTip("Filtrar / Opções Rápidas")
        filter_menu_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        filter_menu_btn.clicked.connect(self._quick_menu)
        search_container.addWidget(filter_menu_btn)

        top_bar_layout.addLayout(search_container)

        left_layout.addWidget(left_header)

        # ── Split-View Content ──────────────────────────────────────
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)

        # ── Left Column (~65% width) ────────────────────────────────
        # Filter Chips Bar
        self.chip_bar = FilterChipBar()
        self.chip_bar.filter_changed.connect(lambda code: self._apply_filters())
        left_layout.addWidget(self.chip_bar)
        left_layout.addSpacing(8)

        # Main Table
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['Game Name', 'Source', 'Prefix Path', 'Status'])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(False)
        self.tree.setSelectionBehavior(QTreeWidget.SelectionBehavior.SelectRows)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_context_menu)
        self.tree.itemSelectionChanged.connect(self._on_item_selected)

        self.status_delegate = StatusBadgeDelegate(self.tree)
        self.tree.setItemDelegate(self.status_delegate)

        h = self.tree.header()
        h.setFixedHeight(35)
        h.setStretchLastSection(False)
        # Each right-hand divider resizes only its own column. Stretch mode
        # on Game Name previously moved the other dividers during a drag.
        h.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        h.setMinimumSectionSize(80)
        h.setSectionsMovable(False)
        self.tree.setHorizontalScrollMode(QTreeWidget.ScrollMode.ScrollPerPixel)
        self.tree.setColumnWidth(0, 290)
        self.tree.setColumnWidth(1, 132)
        self.tree.setColumnWidth(2, 310)
        self.tree.setColumnWidth(3, 116)
        for column, name in enumerate(('Game Name', 'Source', 'Prefix Path', 'Status')):
            self.tree.headerItem().setToolTip(
                column, f'{name}: arraste a borda direita para ajustar a largura.')

        left_layout.addWidget(self.tree)
        self.splitter.addWidget(left_container)

        # ── Right Column: Inspector + integrated window controls ─────
        inspector_panel = QFrame()
        inspector_panel.setObjectName("inspectorPanel")
        inspector_layout = QVBoxLayout(inspector_panel)
        inspector_layout.setContentsMargins(20, 0, 20, 16)
        inspector_layout.setSpacing(0)

        # The window chrome lives inside the right header in the mockup.
        insp_top = QHBoxLayout()
        insp_top.setContentsMargins(0, 0, 0, 0)
        insp_top.setSpacing(8)
        insp_top_widget = QWidget()
        insp_top_widget.setFixedHeight(62)
        insp_top_widget.setLayout(insp_top)

        back_btn = QPushButton()
        back_btn.setIcon(get_back_icon(18))
        back_btn.setObjectName("windowIconBtn")
        back_btn.setFixedSize(28, 32)
        back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        back_btn.clicked.connect(self._refresh)
        insp_top.addWidget(back_btn)

        insp_top.addStretch()

        insp_gear = QPushButton()
        insp_gear.setIcon(get_gear_icon(14))
        insp_gear.setToolTip('Configurações')
        insp_gear.setObjectName("windowIconBtn")
        insp_gear.setFixedSize(28, 32)
        insp_gear.clicked.connect(self._open_settings)
        insp_top.addWidget(insp_gear)

        minimize_btn = QPushButton()
        minimize_btn.setIcon(get_minimize_icon(16))
        minimize_btn.setToolTip('Minimizar')
        minimize_btn.setObjectName("windowIconBtn")
        minimize_btn.setFixedSize(28, 32)
        minimize_btn.clicked.connect(self.showMinimized)
        insp_top.addWidget(minimize_btn)

        maximize_btn = QPushButton()
        maximize_btn.setIcon(get_maximize_icon(16))
        maximize_btn.setToolTip('Maximizar / Restaurar')
        maximize_btn.setObjectName("windowIconBtn")
        maximize_btn.setFixedSize(28, 32)
        maximize_btn.clicked.connect(self._toggle_maximized)
        insp_top.addWidget(maximize_btn)

        close_btn = QPushButton()
        close_btn.setIcon(get_close_icon(16))
        close_btn.setToolTip('Fechar')
        close_btn.setObjectName("windowIconBtn")
        close_btn.setFixedSize(28, 32)
        close_btn.clicked.connect(self.close)
        insp_top.addWidget(close_btn)
        inspector_layout.addWidget(insp_top_widget)

        # Game Banner Widget
        self.banner_widget = GameBannerWidget()
        inspector_layout.addWidget(self.banner_widget)
        inspector_layout.addSpacing(12)

        # Wine Version Section
        wine_lbl = QLabel("Wine Version  ⓘ")
        wine_lbl.setStyleSheet("font-size: 15px; font-weight: 400; color: #dddddf;")
        wine_lbl.setFixedHeight(24)
        wine_lbl.setToolTip("Versão do Wine/Proton em uso para este prefixo.")
        inspector_layout.addWidget(wine_lbl)
        inspector_layout.addSpacing(6)

        self.wine_version_combo = QComboBox()
        self.wine_version_combo.setFixedHeight(35)
        inspector_layout.addWidget(self.wine_version_combo)
        inspector_layout.addSpacing(8)

        # Trainer Path Section
        trainer_field = QFrame()
        trainer_field.setObjectName('trainerField')
        trainer_field.setFixedHeight(49)
        tr_row = QHBoxLayout(trainer_field)
        tr_row.setContentsMargins(4, 5, 6, 5)
        tr_row.setSpacing(4)
        self.trainer_exe_input = QLineEdit()
        self.trainer_exe_input.setObjectName('trainerInput')
        self.trainer_exe_input.setPlaceholderText("/path/to/trainer.exe")
        tr_row.addWidget(self.trainer_exe_input)

        browse_btn = RoundedButton("Browse")
        browse_btn.setObjectName("browseBtn")
        browse_btn.setFixedSize(81, 37)
        browse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        browse_btn.clicked.connect(self._browse_trainer_exe)
        tr_row.addWidget(browse_btn)
        inspector_layout.addWidget(trainer_field)
        inspector_layout.addSpacing(15)

        # Primary Action: Launch Trainer Button (Always vibrant blue, clickable)
        self.launch_btn = RoundedButton("Launch Trainer")
        self.launch_btn.setObjectName("launchTrainerBtn")
        self.launch_btn.setFixedHeight(40)
        self.launch_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.launch_btn.clicked.connect(self._launch_trainer)
        inspector_layout.addWidget(self.launch_btn)

        inspector_layout.addSpacing(14)
        divider = QFrame()
        divider.setObjectName('divider')
        divider.setFixedHeight(1)
        inspector_layout.addWidget(divider)
        inspector_layout.addSpacing(13)

        # Quick Actions Header
        qa_lbl = QLabel("Quick Actions")
        qa_lbl.setStyleSheet("font-size: 15px; font-weight: 400; color: #eeeeef;")
        qa_lbl.setFixedHeight(24)
        inspector_layout.addWidget(qa_lbl)
        inspector_layout.addSpacing(14)

        # Quick Actions 2x2 Grid (Exact Mockup Layout)
        qa_grid = QGridLayout()
        qa_grid.setSpacing(14)
        qa_grid.setColumnStretch(0, 1)
        qa_grid.setColumnStretch(1, 1)

        self.ce_btn = RoundedButton(" Cheat Engine")
        self.ce_btn.setIcon(get_cheatengine_icon(16))
        self.ce_btn.setObjectName("quickActionBtn")
        self.ce_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.ce_btn.clicked.connect(self._run_ce_inspector)
        qa_grid.addWidget(self.ce_btn, 0, 0)

        self.wemod_btn = RoundedButton(" WeMod Manager")
        self.wemod_btn.setIcon(get_wemod_icon(16))
        self.wemod_btn.setObjectName("quickActionBtn")
        self.wemod_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.wemod_btn.clicked.connect(self._open_wemod_modal)
        qa_grid.addWidget(self.wemod_btn, 0, 1)

        self.open_folder_btn = RoundedButton(" Open Folder")
        self.open_folder_btn.setIcon(get_folder_icon(16))
        self.open_folder_btn.setObjectName("quickActionBtn")
        self.open_folder_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_folder_btn.clicked.connect(self._open_prefix_folder)
        qa_grid.addWidget(self.open_folder_btn, 1, 0)
        for button in (self.ce_btn, self.wemod_btn, self.open_folder_btn):
            button.setFixedHeight(53)

        inspector_layout.addLayout(qa_grid)
        inspector_layout.addStretch()

        # Toast / Status output
        self.status_toast = QLabel("")
        self.status_toast.setStyleSheet("color: #34d399; font-size: 11px; font-weight: 500;")
        self.status_toast.setWordWrap(True)
        inspector_layout.addWidget(self.status_toast)

        self.splitter.addWidget(inspector_panel)
        self.splitter.setStretchFactor(0, 7)
        self.splitter.setStretchFactor(1, 3)
        self.splitter.setSizes([884, 386])

        surface_layout.addWidget(self.splitter)
        main_layout.addWidget(surface)

        self.log = QTextEdit()
        self.log.setVisible(False)

    def _toggle_maximized(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            # Dragging the integrated header moves the frameless window.
            pos = event.position().toPoint()
            edges = Qt.Edge(0)
            if pos.x() < 6:
                edges |= Qt.Edge.LeftEdge
            elif pos.x() >= self.width() - 6:
                edges |= Qt.Edge.RightEdge
            if pos.y() < 6:
                edges |= Qt.Edge.TopEdge
            elif pos.y() >= self.height() - 6:
                edges |= Qt.Edge.BottomEdge
            if edges and not self.isMaximized() and self.windowHandle():
                if self.windowHandle().startSystemResize(edges):
                    return
            if pos.y() < 62:
                if self.windowHandle() and self.windowHandle().startSystemMove():
                    return
                self._drag_origin = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_origin)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_origin = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.position().y() < 62:
            self._toggle_maximized()

    def _quick_menu(self):
        menu = QMenu(self)
        menu.addAction('Adicionar Prefixo', self._add_custom_prefix)
        menu.addAction('Atualizar Lista', self._refresh)
        menu.addAction('Configurações', self._open_settings)
        menu.exec(self.search_bar.mapToGlobal(self.search_bar.rect().bottomRight()))

    # ── Toast status ─────────────────────────────────────────────────

    def _show_toast(self, msg: str, is_error: bool = False):
        color = "#f87171" if is_error else "#34d399"
        self.status_toast.setStyleSheet(f"color: {color}; font-size: 11px; font-weight: 500;")
        self.status_toast.setText(msg)
        self._log(msg)
        QTimer.singleShot(5000, lambda: self.status_toast.setText("") if self.status_toast.text() == msg else None)

    # ── Data Loading & Table Population ──────────────────────────────

    def _refresh(self):
        self._show_toast("Atualizando...")
        self._load_tree()
        self._show_toast("✓ Lista atualizada.")

    def _auto_refresh(self):
        running_entries = get_running_wine_entries()
        running_prefixes = {p['wineprefix'] for p in running_entries if p.get('wineprefix')}
        running_pids = {p['pid'] for p in running_entries if p.get('pid')}

        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            d = item.data(0, Qt.ItemDataRole.UserRole)
            if not d:
                continue
            pfx = d.get('wineprefix', '')
            pid = d.get('pid')
            is_running = (pid in running_pids) or (bool(pfx) and pfx in running_prefixes)
            new_status = "Active" if is_running else "Idle"
            if item.text(3) != new_status:
                item.setText(3, new_status)

        self._apply_filters()

    def _load_tree(self):
        sel_data = self._selected_data()
        sel_pfx = sel_data.get('wineprefix') if sel_data else None

        self.tree.clear()
        running_entries = get_running_wine_entries()
        running_prefixes = {p['wineprefix'] for p in running_entries if p.get('wineprefix')}
        running_pids = {p['pid'] for p in running_entries if p.get('pid')}

        matched_item = None
        first_item = None

        for e in get_all_entries():
            pfx = e.get('wineprefix', '')
            pid = e.get('pid')
            is_running = (pid in running_pids) or (bool(pfx) and pfx in running_prefixes)
            status = "Active" if is_running else "Idle"

            item = QTreeWidgetItem([
                e['name'],
                e.get('source', ''),
                pfx,
                status,
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, e)
            self.tree.addTopLevelItem(item)

            if not first_item and pfx:
                first_item = item
            if sel_pfx and pfx == sel_pfx:
                matched_item = item

        self._apply_filters()

        target = matched_item or first_item
        if target:
            self.tree.setCurrentItem(target)

    # ── Filtering ────────────────────────────────────────────────────

    def _apply_filters(self):
        search_query = self.search_bar.text().strip().lower()
        active_chip = self.chip_bar.get_active_filter()

        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            data = item.data(0, Qt.ItemDataRole.UserRole) or {}

            name = (data.get('name') or '').lower()
            source = (data.get('source') or '').lower()
            pfx = (data.get('wineprefix') or '').lower()
            appid = str(data.get('appid') or '')
            is_active = (item.text(3).lower() == 'active')

            chip_match = True
            if active_chip == 'STEAM':
                chip_match = ('steam' in source)
            elif active_chip == 'LUTRIS':
                chip_match = ('lutris' in source)
            elif active_chip == 'BOTTLES':
                chip_match = ('bottles' in source)
            elif active_chip == 'ACTIVE':
                chip_match = is_active

            search_match = True
            if search_query:
                search_match = (
                    search_query in name or
                    search_query in source or
                    search_query in pfx or
                    search_query in appid
                )

            item.setHidden(not (chip_match and search_match))

    # ── Selection & Inspector Sync ───────────────────────────────────

    def _selected_data(self):
        sel = self.tree.selectedItems()
        return sel[0].data(0, Qt.ItemDataRole.UserRole) if sel else None

    def _on_item_selected(self):
        data = self._selected_data()
        if not data:
            self.banner_widget.set_game("Select a Game", "", "")
            self.wine_version_combo.clear()
            return

        name = data.get('name', 'Unknown')
        source = data.get('source', '')
        appid = data.get('appid', '')
        pfx = data.get('wineprefix', '')

        self.banner_widget.set_game(name, source, appid)

        self.wine_version_combo.clear()
        runners = get_available_wine_runners(pfx)
        for label, path in runners:
            self.wine_version_combo.addItem(label, path)

    # ── Actions ──────────────────────────────────────────────────────

    def _browse_trainer_exe(self):
        start_dir = self.config.get('trainers_folder', '') or os.path.expanduser('~')
        path, _ = QFileDialog.getOpenFileName(
            self, 'Selecionar Executável do Trainer',
            start_dir,
            'Executáveis (*.exe);;Todos os arquivos (*)',
        )
        if path:
            self.trainer_exe_input.setText(path)

    def _launch_trainer(self):
        data = self._selected_data()
        if not data:
            self._show_toast("Selecione um jogo/prefixo na tabela primeiro", is_error=True)
            return
        pfx = data.get('wineprefix', '')
        if not pfx or not os.path.isdir(pfx):
            self._show_toast("WINEPREFIX não encontrado ou inválido", is_error=True)
            return

        exe = self.trainer_exe_input.text().strip()
        if not exe or not os.path.isfile(exe):
            # Prompt user directly to choose trainer exe
            self._browse_trainer_exe()
            exe = self.trainer_exe_input.text().strip()
            if not exe or not os.path.isfile(exe):
                return

        idx = self.wine_version_combo.currentIndex()
        if idx >= 0 and self.wine_version_combo.itemData(idx):
            wine_bin = self.wine_version_combo.itemData(idx)
        else:
            pid = data.get('pid')
            wine_bin = (_find_wine_bin_for_pid(pid) or find_wine_binary(pfx)) if pid else find_wine_binary(pfx)

        try:
            logfile = run_exe_in_prefix(wine_bin, exe, pfx)
            self._show_toast(f"✓ Trainer iniciado! (log: {logfile})")
        except Exception as e:
            self._show_toast(f"✗ Falha ao iniciar trainer: {e}", is_error=True)

    def _run_ce_inspector(self):
        data = self._selected_data()
        if not data:
            self._show_toast("Selecione um prefixo primeiro", is_error=True)
            return
        pfx = data.get('wineprefix', '')
        if not pfx or not os.path.isdir(pfx):
            self._show_toast("Selecione um prefixo válido primeiro", is_error=True)
            return

        ce_exe = self.config.get('ce_path', '').strip()
        if not ce_exe or not os.path.isfile(ce_exe):
            reply = QMessageBox.question(
                self, 'Cheat Engine Não Configurado',
                'O executável do Cheat Engine ainda não foi configurado.\nDeseja selecionar o arquivo agora?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                f, _ = QFileDialog.getOpenFileName(
                    self, 'Selecionar Cheat Engine',
                    os.path.expanduser('~'),
                    'Executáveis (*.exe);;Todos os arquivos (*)',
                )
                if f:
                    ce_exe = f
                    self.config['ce_path'] = f
                    save_config(self.config)
                else:
                    return
            else:
                return

        idx = self.wine_version_combo.currentIndex()
        if idx >= 0 and self.wine_version_combo.itemData(idx):
            wine_bin = self.wine_version_combo.itemData(idx)
        else:
            pid = data.get('pid')
            wine_bin = (_find_wine_bin_for_pid(pid) or find_wine_binary(pfx)) if pid else find_wine_binary(pfx)

        try:
            logfile = run_exe_in_prefix(wine_bin, ce_exe, pfx)
            self._show_toast(f"✓ Cheat Engine iniciado! (log: {logfile})")
        except Exception as e:
            self._show_toast(f"✗ Falha ao iniciar Cheat Engine: {e}", is_error=True)

    def _open_wemod_modal(self):
        data = self._selected_data()
        if not data:
            self._show_toast("Selecione um jogo/prefixo primeiro", is_error=True)
            return
        pfx = data.get('wineprefix', '')
        name = data.get('name', 'Jogo')
        dlg = WeModDialog(pfx, name, self)
        dlg.exec()

    def _open_prefix_folder(self):
        data = self._selected_data()
        if not data:
            return
        pfx = data.get('wineprefix', '')
        if pfx and os.path.isdir(pfx):
            subprocess.Popen(['xdg-open', pfx])
            self._show_toast(f"✓ Pasta aberta: {os.path.basename(pfx)}")

    def _open_settings(self):
        dlg = SettingsDialog(self.config, save_config, self)
        if dlg.exec():
            self._refresh()

    # ── Context Menu (Right Click) ───────────────────────────────────

    def _tree_context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if not item:
            menu = QMenu(self)
            act_show = menu.addAction('Mostrar Prefixos Ocultos')
            action = menu.exec(self.tree.viewport().mapToGlobal(pos))
            if action == act_show:
                self._show_hidden_prefixes()
            return

        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return
        pfx = data.get('wineprefix', '')
        if not pfx:
            return
        has_pid = bool(data.get('pid'))

        menu = QMenu(self)
        act_copy = menu.addAction('Copiar WINEPREFIX')
        act_open = menu.addAction('Abrir pasta do WINEPREFIX')
        if has_pid:
            act_kill = menu.addAction('Matar Processo')
        menu.addSeparator()

        save_menu = menu.addMenu('Salvar como')
        act_backup = save_menu.addAction('Backup')
        act_standard = save_menu.addAction('Prefixo Padrão')
        act_restore = menu.addAction('Restaurar Backup')
        menu.addSeparator()

        act_hide = menu.addAction('Ocultar Prefixo')
        act_remove = menu.addAction('Remover Prefixo')

        action = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if action == act_copy:
            QApplication.clipboard().setText(pfx)
            self._show_toast("✓ WINEPREFIX copiado para a área de transferência")
        elif action == act_open:
            if os.path.isdir(pfx):
                subprocess.Popen(['xdg-open', pfx])
        elif has_pid and action == act_kill:
            self._kill_prefix_processes(pfx)
            self._refresh()
        elif action == act_backup:
            self._backup_prefix(pfx, data.get('source') == 'Não-Steam')
        elif action == act_standard:
            self._save_as_standard_prefix(pfx, data.get('source') == 'Não-Steam')
        elif action == act_restore:
            self._restore_backup(pfx, data.get('source') == 'Não-Steam')
        elif action == act_hide:
            self._hide_prefix(pfx)
        elif action == act_remove:
            self._remove_prefix(pfx, data.get('source') == 'Não-Steam')

    # ── Prefix Management & Backups ──────────────────────────────────

    def _kill_prefix_processes(self, wineprefix):
        wineprefix_bytes = f'WINEPREFIX={wineprefix}\0'.encode('utf-8')
        killed = []
        for entry in os.listdir('/proc'):
            if not entry.isdigit():
                continue
            env_raw = _read_file_safe(f'/proc/{entry}/environ', 'rb')
            if not env_raw or wineprefix_bytes not in env_raw:
                continue
            try:
                os.kill(int(entry), 9)
                killed.append(int(entry))
            except (OSError, PermissionError):
                pass
        if killed:
            self._show_toast(f"✓ {len(killed)} processo(s) finalizado(s) no prefixo")
        else:
            self._show_toast("Nenhum processo rodando neste prefixo")

    def _add_custom_prefix(self):
        name, ok = QInputDialog.getText(
            self, 'Adicionar Prefixo Não-Steam',
            'Nome do jogo / prefixo:'
        )
        if not ok or not name.strip():
            return
        pfx = QFileDialog.getExistingDirectory(
            self, 'Selecionar WINEPREFIX (pasta que contém drive_c/)'
        )
        if not pfx:
            return
        if not os.path.isdir(os.path.join(pfx, 'drive_c')):
            QMessageBox.warning(
                self, 'Aviso',
                'A pasta selecionada não contém "drive_c".\nNão é um WINEPREFIX válido.'
            )
            return
        cfg = load_config()
        custom = cfg.setdefault('custom_prefixes', [])
        for c in custom:
            if c['wineprefix'] == pfx:
                QMessageBox.information(self, 'Info', 'Este prefixo já foi adicionado.')
                return
        custom.append({'name': name.strip(), 'wineprefix': pfx})
        save_config(cfg)
        self._refresh()
        self._show_toast(f"✓ Prefixo adicionado: {name.strip()}")

    def _show_hidden_prefixes(self):
        self._open_settings()

    def _hide_prefix(self, pfx):
        cfg = load_config()
        hidden = cfg.setdefault('hidden_prefixes', [])
        if pfx not in hidden:
            hidden.append(pfx)
        save_config(cfg)
        self._refresh()
        self._show_toast("Prefixo ocultado")

    def _remove_prefix(self, pfx, is_custom=False):
        if not os.path.isdir(pfx):
            QMessageBox.warning(self, 'Aviso', 'Prefixo não existe no disco.')
            return
        delete_target = os.path.dirname(pfx) if not is_custom else pfx
        reply = QMessageBox.question(
            self, 'Confirmar',
            f'Tem certeza que deseja apagar permanentemente a pasta do prefixo?\n{delete_target}',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._kill_prefix_processes(pfx)
        result = subprocess.run(['rm', '-rf', delete_target], capture_output=True, text=True)
        if result.returncode != 0:
            QMessageBox.critical(self, 'Erro', f'Não foi possível apagar o prefixo:\n{result.stderr}')
            return
        if is_custom:
            cfg = load_config()
            cfg['custom_prefixes'] = [c for c in cfg.get('custom_prefixes', []) if c['wineprefix'] != pfx]
            save_config(cfg)
        self._refresh()
        self._show_toast("✓ Prefixo apagado do disco")

    def _wemod_built_log_window(self, title: str):
        win = QDialog(self)
        win.setWindowTitle(title)
        win.setModal(True)
        win.setMinimumSize(700, 480)
        win.setStyleSheet(styles.GLOBAL_STYLESHEET)
        layout = QVBoxLayout(win)

        self._wemod_built_label = QLabel('Preparando…')
        self._wemod_built_label.setWordWrap(True)
        layout.addWidget(self._wemod_built_label)

        self._wemod_built_bar = QProgressBar()
        self._wemod_built_bar.setRange(0, 100)
        self._wemod_built_bar.setValue(0)
        layout.addWidget(self._wemod_built_bar)

        self._wemod_built_log = QTextEdit()
        self._wemod_built_log.setReadOnly(True)
        self._wemod_built_log.setStyleSheet(
            'background: #181920; color: #d4d4d8; font-family: monospace; font-size: 11px;'
        )
        layout.addWidget(self._wemod_built_log, stretch=1)

        self._wemod_built_close_btn = QPushButton('Fechar')
        self._wemod_built_close_btn.setVisible(False)
        self._wemod_built_close_btn.clicked.connect(win.accept)
        layout.addWidget(self._wemod_built_close_btn, alignment=Qt.AlignmentFlag.AlignRight)
        win.show()
        QApplication.processEvents()
        return win

    def _built_poll_tick(self):
        while self._built_msg_queue:
            msg = self._built_msg_queue.popleft()
            if hasattr(self, '_wemod_built_log'):
                self._wemod_built_log.append(msg)
        while self._built_progress_queue:
            step, pct = self._built_progress_queue.popleft()
            if hasattr(self, '_wemod_built_label'):
                self._wemod_built_label.setText(step)
            if hasattr(self, '_wemod_built_bar'):
                self._wemod_built_bar.setValue(pct)
        if self._built_task_done:
            self._built_poll.stop()
            self.setEnabled(True)
            if hasattr(self, '_wemod_built_close_btn'):
                self._wemod_built_close_btn.setVisible(True)

    def _backup_prefix(self, pfx, is_custom=False):
        source = os.path.dirname(pfx) if not is_custom else pfx
        if not os.path.isdir(source):
            QMessageBox.warning(self, 'Aviso', 'Pasta do prefixo não encontrada.')
            return
        default_name = os.path.basename(source)
        name, ok = QInputDialog.getText(
            self, 'Salvar Backup',
            'Nome do backup:', text=default_name,
        )
        if not ok or not name.strip():
            return
        name = name.strip()
        dst = os.path.join(source, f'{name}.zip')
        if os.path.exists(dst):
            reply = QMessageBox.question(
                self, 'Sobrescrever',
                f'O arquivo já existe:\n{dst}\nSobrescrever?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        self._wemod_built_progress = self._wemod_built_log_window(f'Backup — {name}')
        self.setEnabled(False)
        self._built_msg_queue.clear()
        self._built_progress_queue.clear()
        self._built_task_done = False
        self._built_poll.start()

        def task():
            try:
                self._built_msg_queue.append(f'Salvando backup em: {dst}')
                self._built_progress_queue.append(('Compactando arquivos...', 0))
                skipped = 0
                file_count = sum(len(f) for _, _, f in os.walk(source, followlinks=False))
                with zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED) as zf:
                    current = 0
                    for root, dirs, files in os.walk(source, followlinks=False):
                        for f in files:
                            fpath = os.path.join(root, f)
                            if os.path.islink(fpath):
                                skipped += 1
                                continue
                            arcname = os.path.relpath(fpath, source)
                            try:
                                zf.write(fpath, arcname)
                                current += 1
                            except (PermissionError, OSError):
                                skipped += 1
                                continue
                            pct = int(80 * current / file_count) if file_count else 80
                            self._built_progress_queue.append((f'Compactando... ({current}/{file_count})', pct))
                if skipped:
                    self._built_msg_queue.append(f'{skipped} arquivo(s) especial(is) ignorado(s)')
                self._built_progress_queue.append(('Concluído!', 100))
                self._built_msg_queue.append(f'Backup salvo com sucesso: {dst}')
                self._built_task_ok = True
            except Exception as e:
                self._built_msg_queue.append(f'ERRO: {e}')
                self._built_task_ok = False
            self._built_task_done = True

        import threading
        threading.Thread(target=task, daemon=True).start()

    def _save_as_standard_prefix(self, pfx, is_custom=False):
        source = os.path.dirname(pfx) if not is_custom else pfx
        if not os.path.isdir(source):
            QMessageBox.warning(self, 'Aviso', 'Pasta do prefixo não encontrada.')
            return
        default_name = os.path.basename(source)
        name, ok = QInputDialog.getText(
            self, 'Salvar como Prefixo Padrão',
            'Nome do prefixo padrão:', text=default_name,
        )
        if not ok or not name.strip():
            return
        name = name.strip()
        dst = os.path.join(BUILT_PREFIX_DIR, f'{name}.zip')
        if os.path.exists(dst):
            reply = QMessageBox.question(
                self, 'Sobrescrever',
                f'O arquivo já existe:\n{dst}\nSobrescrever?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        self._wemod_built_progress = self._wemod_built_log_window(f'Prefixo Padrão — {name}')
        self.setEnabled(False)
        self._built_msg_queue.clear()
        self._built_progress_queue.clear()
        self._built_task_done = False
        self._built_poll.start()

        def task():
            try:
                self._built_msg_queue.append(f'Salvando prefixo padrão em: {dst}')
                self._built_progress_queue.append(('Compactando arquivos...', 0))
                os.makedirs(BUILT_PREFIX_DIR, exist_ok=True)
                skipped = 0
                file_count = sum(len(f) for _, _, f in os.walk(source, followlinks=False))
                with zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED) as zf:
                    current = 0
                    for root, dirs, files in os.walk(source, followlinks=False):
                        for f in files:
                            fpath = os.path.join(root, f)
                            if os.path.islink(fpath):
                                skipped += 1
                                continue
                            arcname = os.path.relpath(fpath, source)
                            try:
                                zf.write(fpath, arcname)
                                current += 1
                            except (PermissionError, OSError):
                                skipped += 1
                                continue
                            pct = int(80 * current / file_count) if file_count else 80
                            self._built_progress_queue.append((f'Compactando... ({current}/{file_count})', pct))
                if skipped:
                    self._built_msg_queue.append(f'{skipped} arquivo(s) especial(is) ignorado(s)')
                self._built_progress_queue.append(('Concluído!', 100))
                self._built_msg_queue.append(f'Prefixo padrão salvo: {dst}')
                self._built_task_ok = True
            except Exception as e:
                self._built_msg_queue.append(f'ERRO: {e}')
                self._built_task_ok = False
            self._built_task_done = True

        import threading
        threading.Thread(target=task, daemon=True).start()

    def _restore_backup(self, pfx, is_custom=False):
        source = os.path.dirname(pfx) if not is_custom else pfx
        if not os.path.isdir(source):
            QMessageBox.warning(self, 'Aviso', 'Pasta do prefixo não encontrada.')
            return
        backups = sorted(
            f for f in os.listdir(source)
            if f.endswith('.zip') and os.path.isfile(os.path.join(source, f))
        )
        if not backups:
            QMessageBox.information(self, 'Backup', 'Nenhum backup encontrado neste prefixo.')
            return
        if len(backups) == 1:
            selected = backups[0]
        else:
            selected, ok = QInputDialog.getItem(
                self, 'Restaurar Backup',
                'Selecione o backup:', backups, 0, False,
            )
            if not ok:
                return
        backup_path = os.path.join(source, selected)
        reply = QMessageBox.question(
            self, 'Confirmar',
            f'Restaurar backup?\n{selected}\n\nOs arquivos atuais do prefixo serão substituídos.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._wemod_built_progress = self._wemod_built_log_window(f'Restaurar — {selected}')
        self.setEnabled(False)
        self._built_msg_queue.clear()
        self._built_progress_queue.clear()
        self._built_task_done = False
        self._built_poll.start()

        def task():
            try:
                self._kill_prefix_processes(pfx)
                self._built_msg_queue.append(f'Restaurando backup: {selected}')
                self._built_progress_queue.append(('Removendo prefixo atual...', 10))
                if os.path.isdir(pfx):
                    shutil.rmtree(pfx)
                self._built_progress_queue.append(('Extraindo backup...', 30))
                with zipfile.ZipFile(backup_path, 'r') as zf:
                    zf.extractall(source)
                self._built_progress_queue.append(('Concluído!', 100))
                self._built_msg_queue.append('Backup restaurado com sucesso!')
                self._built_task_ok = True
            except Exception as e:
                self._built_msg_queue.append(f'ERRO: {e}')
                self._built_task_ok = False
            self._built_task_done = True

        import threading
        threading.Thread(target=task, daemon=True).start()

    def _log(self, msg):
        self.log.append(msg)


# ── entry point ────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setApplicationName('Action Shark')
    app.setDesktopFileName('trainer_manager')
    app.setWindowIcon(get_shark_icon())
    win = ProtonRunner()
    win.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
