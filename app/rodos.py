"""управление релешкой хттп запросами"""
import os
import re

import requests

IP = os.environ.get("RODOS_IP", "192.168.1.20")
AUTH = (os.environ.get("RODOS_USER", "admin"), os.environ.get("RODOS_PASSWORD", "admin"))
CHANNELS = (1, 2, 3)


class RodosError(Exception):
    pass


def _get(path, auth=None, timeout=3):
    try:
        r = requests.get(f"http://{IP}{path}", auth=auth, timeout=timeout)
        r.raise_for_status()
        return r.text
    except requests.RequestException as e:
        raise RodosError(f"нет связи с RODOS ({e.__class__.__name__})")


def set_relay(ch, on):
    text = _get(f"/protect/rb{ch - 1}{'n' if on else 'f'}.cgi", AUTH)
    if "Success" not in text:
        raise RodosError(f"неожиданный ответ RODOS: {text[:40]!r}")


def read_states():
    """состояние читаем с устройства каждый раз и нигде не кэшируем:
    при любой проблеме вернется неизвестное"""
    try:
        found = dict(re.findall(r"<rl(\d)string>([01])</rl\1string>", _get("/pstat.xml", timeout=1)))
        return {ch: found[str(ch - 1)] == "1" for ch in CHANNELS}
    except (RodosError, KeyError):
        return {ch: None for ch in CHANNELS}


def all_off():
    """OFF на всех каналах"""
    errors = []
    for ch in CHANNELS:
        try:
            set_relay(ch, False)
        except RodosError as e:
            errors.append(f"канал {ch}: {e}")
    return errors
