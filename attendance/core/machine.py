"""The computer the program runs on: whether the admin page address in chat links still points at
it, and (on macOS) its power supply and CPU throttling.

Both failed silently before: a laptop on a 5 W phone charger ran at a quarter of its CPU speed with
the battery at 6 %, and chat links pointed at an IP address the computer no longer had.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .config import Config
    from ..store import Store

log = logging.getLogger(__name__)

ADDRESS_CACHE_S = 300
MIN_ADAPTER_W = 60  # a 15" MacBook Pro needs 87 W; below 60 W it drains and slows down under load
LOW_BATTERY = 20
THROTTLED_BELOW = 80  # CPU_Speed_Limit, percent
THROTTLED_FOR_S = 300
ALERT_EVERY_S = 6 * 3600  # one alert per problem in this long
URL_ALERT_KEY = "public_url_alert_day"
ALERTS_KEY = "machine_alerts"

# --- this computer's addresses -----------------------------------------------------------------

_addresses: tuple[float, dict] | None = None
_addresses_lock = threading.Lock()


def _run(args: list[str], timeout: float = 3.0) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _lan_ip() -> str | None:
    """The address used to reach the local network (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _gather_addresses() -> dict:
    ips = {"127.0.0.1", "::1"}
    lan = _lan_ip()
    if lan:
        ips.add(lan)
    if sys.platform != "win32":
        ips |= set(re.findall(r"inet6? ([0-9a-fA-F.:]+)", _run(["ifconfig"]) if sys.platform == "darwin"
                                                      else _run(["ip", "-o", "addr"])))
    names = {"localhost", socket.gethostname().lower()}
    local_name = _run(["scutil", "--get", "LocalHostName"]).strip() if sys.platform == "darwin" else ""
    if local_name:
        names.add(local_name.lower() + ".local")
    names |= {n.split(".")[0] for n in names}
    return {"ips": sorted(ips), "lan": lan, "names": sorted(names), "local_name": local_name}


def addresses(refresh: bool = False) -> dict:
    """This computer's IP addresses, LAN address and host names, cached for a few minutes."""
    global _addresses
    with _addresses_lock:
        if refresh or _addresses is None or time.time() - _addresses[0] > ADDRESS_CACHE_S:
            _addresses = (time.time(), _gather_addresses())
        return _addresses[1]


def addresses_if_ready() -> dict | None:
    """The cached addresses, or None while they are being gathered (in the background), so a page
    is never held up by the commands that list them."""
    with _addresses_lock:
        fresh = _addresses is not None and time.time() - _addresses[0] <= ADDRESS_CACHE_S
        cached = _addresses[1] if _addresses is not None else None
    if not fresh:
        threading.Thread(target=addresses, name="addresses", daemon=True).start()
    return cached


def public_url_problem(cfg: "Config", found: dict | None = None) -> dict | None:
    """When web.public_url names an IP address this computer does not have: the problem and better
    addresses. Host names are not looked up (that could block); only IP literals are checked."""
    url = cfg.web.public_url.strip()
    if not url:
        return None
    host = (urllib.parse.urlsplit(url if "://" in url else "http://" + url).hostname or "").lower()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return None
    found = found or addresses()
    if host in found["ips"]:
        return None
    port = urllib.parse.urlsplit(url if "://" in url else "http://" + url).port or cfg.web.port
    better = []
    if found.get("lan"):
        better.append(f"http://{found['lan']}:{port}")
    if found.get("local_name"):
        better.append(f"http://{found['local_name']}.local:{port}")
    text = (f"The address of this page in chat links ({url}) is not this computer's: links in Google Chat "
            f"do not open. This computer is at {found.get('lan') or 'an address not found'} now")
    text += f"; use {' or '.join(better)}, or give the computer a fixed address on the router." if better else "."
    return {"host": host, "better": better, "text": text}


# --- power and thermal (macOS) -----------------------------------------------------------------

@dataclass
class Power:
    on_ac: bool | None = None
    battery: int | None = None  # percent
    charging: bool | None = None
    adapter_w: int | None = None
    speed_limit: int | None = None  # percent of full CPU speed (macOS lowers it when hot or underpowered)
    checked_at: float = 0.0


def parse_batt(text: str) -> dict:
    """`pmset -g batt` -> on_ac, battery, charging."""
    out: dict = {}
    m = re.search(r"Now drawing from '([^']+)'", text)
    if m:
        out["on_ac"] = "AC" in m.group(1)
    m = re.search(r"(\d+)%;\s*([^;]+);", text)
    if m:
        out["battery"] = int(m.group(1))
        state = m.group(2).strip().lower()
        out["charging"] = state in ("charging", "charged", "finishing charge")
    return out


def parse_therm(text: str) -> int | None:
    """`pmset -g therm` -> CPU_Speed_Limit (percent), or None when not reported."""
    m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", text)
    return int(m.group(1)) if m else None


def parse_adapter(text: str) -> int | None:
    """`pmset -g adapter` -> watts, or None without an adapter."""
    m = re.search(r"Wattage\s*=\s*(\d+)\s*W", text)
    return int(m.group(1)) if m else None


def read_power() -> Power | None:
    """The power state now; None where it cannot be read (not macOS, or a desktop without a battery)."""
    if sys.platform != "darwin":
        return None
    batt = parse_batt(_run(["pmset", "-g", "batt"]))
    if not batt:
        return None
    return Power(on_ac=batt.get("on_ac"), battery=batt.get("battery"), charging=batt.get("charging"),
                 adapter_w=parse_adapter(_run(["pmset", "-g", "adapter"])),
                 speed_limit=parse_therm(_run(["pmset", "-g", "therm"])), checked_at=time.time())


def problems(p: Power) -> dict[str, str]:
    """Problem id -> chat text, for the power state `p` (throttling is timed by the watcher)."""
    found = {}
    if p.on_ac is False:
        found["battery_power"] = (f"🔋 *The attendance computer is running on battery* ({p.battery}%). "
                                  "Plug its charger in, or it switches off when the battery is empty.")
    elif p.adapter_w is not None and p.adapter_w < MIN_ADAPTER_W:
        found["weak_adapter"] = (f"🔌 *The attendance computer's charger gives only {p.adapter_w} W* "
                                 f"(needs at least {MIN_ADAPTER_W} W, ideally the laptop's own 87–96 W charger). "
                                 "The battery drains and the computer slows down under load.")
    if p.battery is not None and p.battery < LOW_BATTERY and not p.charging:
        found["low_battery"] = (f"🪫 *The attendance computer's battery is at {p.battery}% and not charging.* "
                                "It switches off when the battery is empty.")
    if p.speed_limit is not None and p.speed_limit < THROTTLED_BELOW:
        found["throttled"] = (f"🌡 *The attendance computer runs at {p.speed_limit}% of its CPU speed* (too hot or "
                              "not enough power). Check the charger and give the laptop air.")
    return found


RECOVERED = {"battery_power": "✅ The attendance computer is on mains power again.",
             "weak_adapter": "✅ The attendance computer's charger is strong enough again.",
             "low_battery": "✅ The attendance computer's battery is charging again.",
             "throttled": "✅ The attendance computer runs at full CPU speed again."}


class MachineWatch:
    """Checks power, throttling and the page address about once a minute (call `check` from a
    background thread) and posts to chat once per problem in ALERT_EVERY_S, plus once when it is over."""

    def __init__(self, cfg: "Config", store: "Store", alert: Callable[[str], None], now: Callable,
                 read: Callable[[], Power | None] = read_power, found: Callable[[], dict] = addresses):
        self.cfg, self.store, self.alert, self.now = cfg, store, alert, now
        self.read, self.found = read, found
        self.power: Power | None = None
        self._since: dict[str, float] = {}  # problem -> first seen in this run
        self._running = False
        self._last_start = 0.0

    def check_soon(self, every_s: float = 60.0) -> None:
        """Start a check in the background unless one ran within `every_s` or is running."""
        # Paced by the last start, not by self.power: that stays None where power cannot be read
        # (Windows, Linux, a Mac without a battery), which started a check on every loop pass.
        if self._running or time.time() - self._last_start < every_s:
            return
        self._running, self._last_start = True, time.time()

        def go():
            try:
                self.check()
            except Exception:
                log.exception("Machine check failed")
            finally:
                self._running = False

        threading.Thread(target=go, name="machine-check", daemon=True).start()

    def _state(self) -> dict:
        try:
            return json.loads(self.store.get(ALERTS_KEY) or "{}")
        except ValueError:
            return {}

    def check(self, t: float | None = None) -> None:
        t = time.time() if t is None else t
        power = self.read()
        if power is not None:
            power.checked_at = power.checked_at or t
            self.power = power
            found = problems(power)
            for key in list(self._since):
                if key not in found:
                    del self._since[key]
            for key in found:
                self._since.setdefault(key, t)
            state, changed = self._state(), False
            for key, text in found.items():
                if key == "throttled" and t - self._since[key] < THROTTLED_FOR_S:
                    continue
                entry = state.get(key) or {}
                if "sent" in entry and t - entry["sent"] < ALERT_EVERY_S:
                    continue  # told a short while ago; a problem that lasts is repeated now and then
                self.alert(text)
                state[key] = {"sent": t, "open": True}
                changed = True
            for key, entry in list(state.items()):
                if entry.get("open") and key not in found:
                    self.alert(RECOVERED.get(key, "✅ The attendance computer is fine again."))
                    entry["open"] = False
                    changed = True
            if changed:
                self.store.put(ALERTS_KEY, json.dumps(state))
        problem = public_url_problem(self.cfg, self.found())
        today = self.now().date().isoformat()
        if problem and self.cfg.google_chat.webhook_url and self.store.get(URL_ALERT_KEY) != today:
            self.alert("🔗 " + problem["text"])
            self.store.put(URL_ALERT_KEY, today)

    def status(self) -> dict | None:
        """For the Status page: the last power reading, or None where there is none."""
        if self.power is None:
            return None
        return {**asdict(self.power), "problems": list(problems(self.power))}
