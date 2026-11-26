"""логи, state и прочее"""
import json
import os
import sqlite3
import threading
import time

PRESET_VERSION = 3
_dir = "data"
_db = None
_lock = threading.Lock()
_write_lock = threading.Lock()


def init(data_dir):
    global _dir, _db
    _dir = data_dir
    os.makedirs(_dir, exist_ok=True)
    _db = sqlite3.connect(os.path.join(_dir, "log.db"), check_same_thread=False)
    _db.execute("CREATE TABLE IF NOT EXISTS log (id INTEGER PRIMARY KEY, ts REAL, level TEXT, text TEXT)")


def log(level, text):
    with _lock:
        _db.execute("INSERT INTO log (ts, level, text) VALUES (?, ?, ?)", (time.time(), level, text))
        _db.commit()


def read_log(limit=100):
    with _lock:
        rows = _db.execute("SELECT ts, level, text FROM log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{"ts": r[0], "level": r[1], "text": r[2]} for r in rows]


def _write(name, obj):
    """при отключении питания файл не останется наполовину записанным"""
    path = os.path.join(_dir, name)
    with _write_lock:             # status опрашивается параллельно: без замка потоки делят один .tmp и роняют друг друга
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(path + ".tmp", path)


def load_preset():
    try:
        with open(os.path.join(_dir, "preset.json"), encoding="utf-8") as f:
            preset = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log("error", f"пресет повреждён и не загружен: {e}")
        return None
    if preset.get("version") != PRESET_VERSION:
        log("error", f"неподдерживаемая версия пресета: {preset.get('version')}")
        return None
    return preset


def save_preset(preset):
    _write("preset.json", preset)


def delete_preset():
    try:
        os.remove(os.path.join(_dir, "preset.json"))
    except FileNotFoundError:
        pass


def save_state(state):
    _write("state.json", state)
