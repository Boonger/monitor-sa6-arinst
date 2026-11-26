"""мониторим узкие места, вблизи с пиковыми точками"""
import threading
import time

import numpy as np

from . import sa6, storage

PERIOD_S = 0.2
SCAN_TIMEOUT_S = 1.0
LABELS = {"detected": "обнаружен", "not_detected": "не обнаружен", "error": "ошибка измерения"}

preset = None     # активный пресет
paused = False
latest = {}       # id сигнала -> результат последнего цикла (подменяется целиком, читать из других потоков безопасно)
_thread = None
_idle = threading.Event()         # установлен, пока мониторинг не держит SA6
_idle.set()


def set_preset(p):
    global preset, latest
    preset, latest = p, {}


def pause(timeout=10):
    global paused
    paused = True
    _idle.wait(timeout)


def resume():
    global paused
    paused = False


def needed_excess_db(sig, prm):
    """превышение над фоном при калибровке минус допуск, но не ниже общего порога.
    слабая помеха на 10-20 дБ над шумом сигналом не считается"""
    return max(prm["threshold_db"], sig["excess_db"] - prm["tolerance_db"])


def measure(sigs, prm, abort):
    """попадание, если пик выше медианы окна не меньше чем на порог канала"""
    step = prm["step_hz"]
    mid, tol = prm["window_hz"] // step, prm["freq_tol_hz"] // step
    near = slice(mid - tol, mid + tol + 1)                  # точки окна возле сохранённой частоты
    stats = [{"need": needed_excess_db(s, prm), "hits": 0, "scans": 0, "best": -np.inf, "level": None,
              "held": None, "error": None} for s in sigs]
    end = time.time() + prm["integrate_s"]
    while True:
        for s, st in zip(sigs, stats):
            if abort():
                return None
            try:
                amps = np.array(sa6.scan(s["freq_hz"] - prm["window_hz"], s["freq_hz"] + prm["window_hz"],
                                         step, timeout=SCAN_TIMEOUT_S))
            except sa6.SA6Error as e:
                st["error"] = str(e)
                time.sleep(0.1)                             # не крутим процессор впустую
                continue
            level = float(amps[near].max())
            excess = level - float(np.median(amps))         # узкий сигнал медиану окна почти не меняет
            st["scans"] += 1
            st["hits"] += excess >= st["need"]
            st["held"] = amps if st["held"] is None else np.maximum(st["held"], amps)   # для графика
            if excess > st["best"]:
                st["best"], st["level"] = excess, level
        if time.time() >= end:
            return stats


def _cycle(p):
    prm, sigs = p["params"], p["signals"]
    stats = measure(sigs, prm, abort=lambda: paused or preset is not p)
    if stats is None:
        return None
    result = {}
    for s, st in zip(sigs, stats):
        if st["scans"] == 0:                                # ни одного успешного скана за цикл
            result[s["id"]] = {"status": "error", "error": st["error"]}
            continue
        # подтверждение несколькими измерениями 
        detected = st["hits"] >= min(prm["confirm"], st["scans"])
        result[s["id"]] = {
            "status": "detected" if detected else "not_detected",
            "level_dbm": round(st["level"], 1), "excess_db": round(st["best"], 1), "need_db": round(st["need"], 1),
            "hits": int(st["hits"]), "scans": st["scans"],
            "start_hz": s["freq_hz"] - prm["window_hz"], "step_hz": prm["step_hz"],
            "amps": [round(float(x), 1) for x in st["held"]]}
    return result


def _loop():
    global latest
    while True:
        p = preset
        if p is None or paused:
            _idle.set()
            time.sleep(0.3)
            continue
        _idle.clear()
        try:
            new = _cycle(p)
        except Exception as e:                    # что бы ни случилось - поток мониторинга не должен умереть
            new = {s["id"]: {"status": "error", "error": str(e)} for s in p["signals"]}
        finally:
            _idle.set()
        if new is not None and preset is p and not paused:   # пресет сменили / началась калибровка - результат устарел
            for sid, r in new.items():            # в журнал пишем только смену статуса, а не каждое измерение
                if r["status"] != latest.get(sid, {}).get("status"):
                    storage.log("error" if r["status"] == "error" else "info",
                                f"сигнал {sid}: {LABELS[r['status']]}" + (f" ({r['error']})" if "error" in r else ""))
            latest = new
        time.sleep(PERIOD_S)


def start():
    global _thread
    if _thread is None:
        _thread = threading.Thread(target=_loop, daemon=True)
        _thread.start()
