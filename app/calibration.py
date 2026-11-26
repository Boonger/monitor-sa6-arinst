"""калибровка по трем сильнейшим пикам - сохраняем в пресет
больше 5 сканов я не ставил, даже на тре сканах ловятся все три точки. сканить с 
разбиением по 256 я начал после того, как спектроанализатор выключился, я испугался.
еще непонятная тема, не успел проверить, на самом снимке спектроанализатора при включенных >1 реле, 
сигналы переставали быть постоянными, по этому я сразу начал со сбора нескольких сканов для большей уверенности
"""
import threading
import time
from datetime import datetime, timezone

import numpy as np

from . import monitor, rodos, sa6, storage

SCAN = {"start_hz": 100_000_000, "stop_hz": 2_000_000_000, "step_hz": 1_000_000, "sweeps": 5}

PARAMS = {"window_hz": 3_000_000,   # окно мониторинга вокруг частоты: ±3 МГц
          "step_hz": 100_000,       # шаг сканирования окна
          "threshold_db": 10,       # минимальное превышение над фоном для «обнаружен»
          "tolerance_db": 15,       # допустимое падение превышения относительно калибровки
          "freq_tol_hz": 1_000_000,  # пик должен быть не дальше этого от сохранённой частоты
          "confirm": 2,             # сколько сканов за цикл должны показать сигнал
          "integrate_s": 2.0}       # длительность цикла измерения
SETTLE_S = 0.3             # пауза после включения реле
BG_POINTS = 31           # окно скользящей медианы для фона (31 точка = 31 МГц)
MIN_SEP_POINTS = 3       # пики ближе 3 точек (3 МГц) считаем одним сигналом
N_PEAKS = 3
MAX_BAD_SWEEPS_IN_ROW = 3

status = {"state": "idle", "error": None, "progress": ""}      # state: idle / running / done / error


class CalibrationError(Exception):
    pass


def find_peaks(amps, threshold_db, n=N_PEAKS):
    """n самых сильных пиков над локальным фоном (скользящая медиана) - как в test_sa6.py.
    Возвращает ([(индекс, превышение_дБ)] по возрастанию частоты, фон)."""
    a = np.array(amps, dtype=float)
    padded = np.pad(a, BG_POINTS // 2, mode="edge")
    background = np.median(np.lib.stride_tricks.sliding_window_view(padded, BG_POINTS), axis=1)
    excess = a - background
    candidates = [i for i in range(1, len(a) - 1)             # локальные максимумы выше порога, сильнейшие первыми
                  if excess[i] >= threshold_db and excess[i] >= excess[i - 1] and excess[i] > excess[i + 1]]
    candidates.sort(key=lambda i: -excess[i])
    peaks = []
    for i in candidates:
        if all(abs(i - j) >= MIN_SEP_POINTS for j, _ in peaks):    # соседние отсчёты одного пика не считаем
            peaks.append((i, float(excess[i])))
            if len(peaks) >= n:
                break
    return sorted(peaks), background


def _wide_spectrum():
    """сканы"""
    spectrum, bad_in_row = None, 0
    for k in range(SCAN["sweeps"]):
        try:
            amps = np.array(sa6.scan_wide(
                SCAN["start_hz"], SCAN["stop_hz"], SCAN["step_hz"],
                progress=lambda done, total, k=k: status.update(
                    progress=f"скан {k + 1}/{SCAN['sweeps']}: {done * 100 // total}%")))
        except sa6.SA6Error as e:
            bad_in_row += 1
            storage.log("warn", f"битый скан {k + 1}/{SCAN['sweeps']}: {e}")
            if bad_in_row >= MAX_BAD_SWEEPS_IN_ROW:
                raise CalibrationError(f"SA6 не отвечает ({bad_in_row} сканов подряд): {e}")
            continue
        bad_in_row = 0
        spectrum = amps if spectrum is None else np.maximum(spectrum, amps)
    if spectrum is None:
        raise CalibrationError("ни одного корректного скана")
    return spectrum


def _calibrate():
    for ch in rodos.CHANNELS:
        rodos.set_relay(ch, True)
        storage.log("info", f"канал {ch}: ВКЛ (калибровка)")
    time.sleep(SETTLE_S)

    spectrum = _wide_spectrum()
    peaks, background = find_peaks(spectrum, PARAMS["threshold_db"])
    f0, step = SCAN["start_hz"], SCAN["step_hz"]
    if len(peaks) < N_PEAKS:
        found = ", ".join(f"{(f0 + i * step) / 1e6:.0f} МГц" for i, _ in peaks) or "нет"
        raise CalibrationError(f"найдено пиков: {len(peaks)} из {N_PEAKS} "
                               f"(порог {PARAMS['threshold_db']} дБ, найдено: {found})")

    signals = [{"id": n, "freq_hz": f0 + i * step,            # частота точки = старт + индекс * шаг
                "level_dbm": round(float(spectrum[i]), 1), "background_dbm": round(float(background[i]), 1),
                "excess_db": round(exc, 1)}                   # по excess_db мониторинг считает порог канала
               for n, (i, exc) in enumerate(peaks, 1)]        # нумерация по возрастанию частоты
    return {"version": storage.PRESET_VERSION, "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "scan": SCAN, "params": PARAMS, "sa6": {"scn20_args": sa6.SCN_ARGS, "att": sa6.ATT},
            "signals": signals, "spectrum": [round(float(x), 1) for x in spectrum]}


def _run():
    monitor.pause()
    try:
        preset = _calibrate()
        storage.save_preset(preset)
        monitor.set_preset(preset)
        storage.log("info", "калибровка завершена: " + ", ".join(f"{s['freq_hz'] / 1e6:.2f} МГц" for s in preset["signals"]))
        result = {"state": "done", "error": None}
    except Exception as e:                  # любая ошибка (SA6, RODOS, диск): калибровка не удалась, приложение живёт
        storage.log("error", f"калибровка не удалась: {e or repr(e)}")
        result = {"state": "error", "error": str(e) or repr(e)}
    finally:
        errors = rodos.all_off()            # после калибровки генератор всегда выключаем
        if errors:
            storage.log("error", "после калибровки не удалось выключить реле: " + "; ".join(errors))
        monitor.resume()
    status.update(result, progress="")


def start():
    if status["state"] == "running":
        return False
    status.update(state="running", error=None, progress="запуск")
    threading.Thread(target=_run, daemon=True).start()
    return True
