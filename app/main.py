"""сервак fastApi, интересного здесь мало"""
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from . import calibration, monitor, rodos, sa6, storage

_rodos_ok = None        # была ли связь с RODOS при прошлом опросе
_saved_state = None     # что записано в state.json


def _startup_off():
    """отключаем релешки при старте"""
    first = True
    while True:
        errors = rodos.all_off()
        if not errors:
            storage.log("info", "при запуске отправлен OFF на каналы 1-3")
            return
        if first:
            storage.log("error", "OFF при запуске не прошёл, повторяю: " + "; ".join(errors))
            first = False
        time.sleep(3)


def _try_off_after_loss():
    errors = rodos.all_off()
    storage.log("warn", "попытка OFF после потери связи: " + ("не удалась" if errors else "выполнена"))


@asynccontextmanager
async def lifespan(app):
    storage.init(os.environ.get("RF_DATA_DIR", "data"))
    storage.log("info", "приложение запущено")
    preset = storage.load_preset()
    monitor.set_preset(preset)
    if preset:
        storage.log("info", "загружен пресет от " + preset["created"])
    monitor.start()
    threading.Thread(target=_startup_off, daemon=True).start()
    yield
    rodos.all_off()


app = FastAPI(lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/status")
def status():
    global _rodos_ok, _saved_state
    states = rodos.read_states()
    connected = None not in states.values()
    if connected != _rodos_ok:      # смена связи: пишем в журнал; если пропала - пробуем OFF, вдруг связь ещё есть
        storage.log("info" if connected else "error", "связь с RODOS " + ("есть" if connected else "потеряна"))
        if _rodos_ok and not connected:
            threading.Thread(target=_try_off_after_loss, daemon=True).start()
        _rodos_ok = connected

    p = monitor.preset
    signals = [{"id": s["id"], "freq_hz": s["freq_hz"], **monitor.latest.get(s["id"], {"status": "unknown"})}
               for s in p["signals"]] if p else []

    current = {"relays": states, "monitor": {s["id"]: s["status"] for s in signals}}
    if current != _saved_state:     # состояния и результаты храним отдельно от пресета, пишем только при изменении
        try:
            storage.save_state({"saved": time.time(), **current})
            _saved_state = current
        except OSError as e:        # сбой записи на диск не должен ронять /api/status
            storage.log("error", f"не удалось сохранить state.json: {e}")

    return {"rodos": {"connected": connected, "relays": states},
            "sa6": {"connected": sa6.last_error is None and sa6.last_ok is not None, "error": sa6.last_error},
            "signals": signals, "has_preset": p is not None, "paused": monitor.paused,
            "calibration": calibration.status}


@app.post("/api/relay/{ch}/{action}")
def relay(ch: str, action: str):
    channels = rodos.CHANNELS if ch == "all" else [int(ch)] if ch in ("1", "2", "3") else None
    if channels is None or action not in ("on", "off"):
        raise HTTPException(404)
    errors = []
    for c in channels:
        try:
            rodos.set_relay(c, action == "on")
            storage.log("info", f"канал {c}: {'ВКЛ' if action == 'on' else 'ВЫКЛ'}")
        except rodos.RodosError as e:
            storage.log("error", f"канал {c}: команда не выполнена: {e}")
            errors.append(f"канал {c}: {e}")
    if errors:
        raise HTTPException(502, "; ".join(errors))
    return {"ok": True}


@app.post("/api/calibration/start")
def calibration_start():
    if not calibration.start():
        raise HTTPException(409, "калибровка уже выполняется")
    return {"ok": True}


@app.get("/api/preset")
def preset_get():
    return monitor.preset or {}


@app.delete("/api/preset")
def preset_delete():
    if calibration.status["state"] == "running":
        raise HTTPException(409, "идёт калибровка")
    storage.delete_preset()
    monitor.set_preset(None)
    storage.log("info", "пресет сброшен")
    return {"ok": True}


@app.get("/api/log")
def log_get(limit: int = 100):
    return storage.read_log(limit)
