"""анализатор спектра, отправка ascii команд"""
import os
import threading
import time

import serial

PORT = os.environ.get("SA6_PORT", "/dev/ttyACM0")
BAUD = 115200
SCN_ARGS = "200 20 10700000"   # параметры scn20
ATT = 10000                    # 10000
CHUNK_POINTS = 256             # точки

_lock = threading.Lock()       # к порту обращается один поток за раз
_ser = None
last_error = None              # текст последней ошибки (None - последний запрос прошёл)
last_ok = None                 # время последнего успешного скана

last_cmd, last_raw_len, last_raw_head, last_raw_tail, last_raw_hex = None, 0, None, None, None


class SA6Error(Exception):
    pass


def _close():
    global _ser
    try:
        _ser.close()
    except Exception:
        pass
    _ser = None


def _exchange(cmd, timeout):
    """единственное место, где трогаем порт: отправить команду и прочитать ответ до слова 'complete'."""
    global _ser, last_cmd, last_raw_len, last_raw_head, last_raw_tail, last_raw_hex
    buf = b""
    try:
        if _ser is None:
            _ser = serial.Serial(PORT, BAUD, timeout=0.5)
        _ser.reset_input_buffer()
        _ser.write(cmd)
        end = time.time() + timeout
        while time.time() < end and not buf.endswith(b"complete\r\n"):
            buf += _ser.read(_ser.in_waiting or 1)
        return buf
    except (serial.SerialException, OSError) as e:
        _close()
        raise SA6Error(f"ошибка порта SA6: {e}")
    finally:
        last_cmd, last_raw_len = cmd.decode("ascii", "replace").strip(), len(buf)
        last_raw_head, last_raw_tail, last_raw_hex = repr(buf[:80]), repr(buf[-40:]), buf[:40].hex(" ")


def _parse(buf, n, start_hz, index):
    if not buf.endswith(b"complete\r\n"):
        raise SA6Error(f"нет 'complete' (получено {len(buf)} байт: {buf[:80]!r})")
    head = buf.find(f"scn20 {start_hz} {index}".encode())
    header_end = buf.find(b"\r\n", head)
    if head < 0 or header_end < 0:
        raise SA6Error(f"нет заголовка ответа: {buf[:80]!r}")
    body = buf[header_end + 2:]
    if len(body) < 2 * n + 2 or body[2 * n:2 * n + 2] != b"\xff\xff":
        raise SA6Error(f"неверная длина (ждали {2 * n}+2 байт), body={body[:60]!r}")
    return [(800 - ((body[2 * i] << 8 | body[2 * i + 1]) & 0x7FF)) / 10 for i in range(n)]


def scan(start_hz, stop_hz, step_hz, index=0, timeout=10.0, retries=0):
    global last_error, last_ok
    n = (stop_hz - start_hz) // step_hz + 1
    cmd = f"scn20 {start_hz} {stop_hz} {step_hz} {SCN_ARGS} {ATT} {index}\r\n".encode("ascii")
    with _lock:
        for _ in range(retries + 1):
            try:
                amps = _parse(_exchange(cmd, timeout), n, start_hz, index)
            except SA6Error as e:
                _close()
                last_error = str(e)
            else:
                last_error, last_ok = None, time.time()
                return amps
    raise SA6Error(last_error)


def scan_wide(start_hz, stop_hz, step_hz, progress=None):
    total = (stop_hz - start_hz) // step_hz + 1
    out = []
    while len(out) < total:
        n = min(CHUNK_POINTS, total - len(out))
        a = start_hz + len(out) * step_hz
        out += scan(a, a + (n - 1) * step_hz, step_hz, index=len(out) // CHUNK_POINTS + 1, retries=2)
        if progress:
            progress(len(out), total)
    return out
