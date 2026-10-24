"""Touch injection backends.

The server talks to a backend through a tiny interface so the transport (and the
way we hand touch data to the game) stays swappable:

    backend.press(contact_id, nx, ny)   # nx, ny normalised 0..1 in the game client area
    backend.move(contact_id, nx, ny)
    backend.release(contact_id)
    backend.close()

Backends:

* ``maitouch`` - **default**.  Writes the 14-byte touch status packet that the
  micetools `maitouch` virtual COM port hands to the game, i.e. the game gets the
  touch data through its touch port (see `pc/maitouch_port.py`).
* ``synth``    - injects real Windows pointer contacts (PT_TOUCH).  Kept as a
  fallback; on this machine InjectSyntheticPointerInput is accepted by the API
  but no WM_POINTER ever reaches the window, so mice.dll never sees it
  (docs/re_maitouch.md section 5).
* ``log``      - dry run, logs only.
"""
from __future__ import annotations

import logging
import threading
import time

import sensors

log = logging.getLogger(__name__)


class TouchBackend:
    name = "base"

    def __init__(self, panel_provider=None):
        # ``panel_provider()`` returns the detected panel circle (centre + radii) or
        # None for "the whole frame is the panel"; it must be cheap to call - it runs
        # for every touch event
        self._panel_provider = panel_provider
        self.stats = {
            "presses": 0,
            "moves": 0,
            "releases": 0,
            "dropped": 0,
            "errors": 0,
            "active": 0,
            "last_event": 0.0,
        }

    def _panel(self):
        if self._panel_provider is None:
            return None
        try:
            panel = self._panel_provider()
        except Exception:  # noqa: BLE001 - never break touch because of detection
            return None
        if isinstance(panel, tuple):
            panel = panel[0]
        return panel

    def _sensor(self, nx: float, ny: float):
        return sensors.sensor_at(nx, ny, self._panel())

    def _touch(self) -> None:
        self.stats["last_event"] = time.time()

    def _fail(self, exc: Exception) -> None:
        self.stats["errors"] += 1
        if self.stats["errors"] <= 5 or self.stats["errors"] % 500 == 0:
            log.warning("%s: injection failed: %s", self.name, exc)

    def describe(self) -> str:
        return self.name

    def details(self) -> dict:
        return {}

    def press(self, contact_id: int, nx: float, ny: float) -> None:
        raise NotImplementedError

    def move(self, contact_id: int, nx: float, ny: float) -> None:
        raise NotImplementedError

    def release(self, contact_id: int) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class MaitouchPortBackend(TouchBackend):
    """Touches go through the maitouch serial-port status packet."""

    name = "maitouch"

    def __init__(self, process_names=None, watch_interval: float = 1.0, panel_provider=None):
        super().__init__(panel_provider)
        import atexit

        from maitouch_port import MaitouchPort

        self._port = MaitouchPort(process_names=process_names or ("maimai_dump_.exe", "maimai.exe"), log=log)
        self._contacts: dict[int, str] = {}
        self._watch_interval = watch_interval
        self._idle_restore = 5.0  # seconds without contacts before we hand control back
        self._stop = threading.Event()
        self._watchdog = threading.Thread(target=self._watch_loop, name="maitouch-watch", daemon=True)
        self._watchdog.start()
        self.stats["packets_written"] = 0
        self.stats["sensor_changes"] = 0
        # if the server dies without an orderly shutdown the game would keep the
        # `ret` patch; atexit gets us back to normal on Ctrl+C / normal exit.
        atexit.register(self._restore_quietly)

    def _restore_quietly(self) -> None:
        try:
            if self._port.attached:
                self._port.close()
                log.info("maitouch: restored build_status on exit")
        except Exception:  # noqa: BLE001
            pass

    # -- internals ----------------------------------------------------------
    def _watch_loop(self) -> None:
        was_attached = False
        while not self._stop.wait(self._watch_interval):
            try:
                if not self._port.attached:
                    if self._port.attach():
                        log.info("maitouch: game found (pid %s)", self._port.pid)
                        # a previous run may have left a frozen packet behind
                        info = self._port.probe()
                        if info.get("packet") and info["packet"] != sensors.IDLE_PACKET.hex(" "):
                            log.warning("maitouch: clearing leftover touch state (%s)", info.get("sensors"))
                            self._port.clear()
                    elif was_attached:
                        log.warning("maitouch: lost the game (%s)", self._port.last_error)
                    was_attached = self._port.attached
                    continue
                # only keep build_status frozen while the phone is actually in use,
                # so a mouse stays usable (and a crashed server is less harmful)
                if (
                    self._port.frozen
                    and not self._contacts
                    and time.time() - self.stats["last_event"] > self._idle_restore
                ):
                    self._port.restore()
                    log.info("maitouch: idle, build_status restored (in-game mouse works again)")
            except Exception as exc:  # noqa: BLE001 - never kill the watchdog
                log.warning("maitouch watchdog: %s", exc)
            was_attached = self._port.attached

    def _current_sensors(self) -> list[str]:
        return sorted(set(self._contacts.values()), key=sensors.SENSOR_ORDER.index)

    def _apply(self) -> bool:
        wanted = self._current_sensors()
        try:
            written = self._port.set_sensors(wanted)
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)
            return False
        if written:
            self.stats["packets_written"] += 1
            self.stats["sensors"] = [sensors.LABELS[name] for name in wanted]
            log.debug("maitouch: sensors %s -> %s", wanted, sensors.packet_from_sensors(wanted).hex())
        else:
            self.stats["errors"] += 1
            if self.stats["errors"] <= 5:
                log.warning("maitouch: write failed (%s)", self._port.last_error)
        return written

    # -- interface ----------------------------------------------------------
    def press(self, contact_id: int, nx: float, ny: float) -> None:
        sensor = self._sensor(nx, ny)
        if sensor is None:
            self._contacts.pop(contact_id, None)
            self.stats["dropped"] += 1
            self._apply()
            return
        if self._contacts.get(contact_id) != sensor:
            self.stats["sensor_changes"] += 1
        self._contacts[contact_id] = sensor
        self.stats["presses"] += 1
        self.stats["active"] = len(self._contacts)
        self._apply()
        self._touch()

    def move(self, contact_id: int, nx: float, ny: float) -> None:
        sensor = self._sensor(nx, ny)
        if sensor is None or sensor == self._contacts.get(contact_id):
            self.stats["moves"] += 1
            self._touch()
            return
        self._contacts[contact_id] = sensor
        self.stats["moves"] += 1
        self.stats["sensor_changes"] += 1
        self._apply()
        self._touch()

    def release(self, contact_id: int) -> None:
        if self._contacts.pop(contact_id, None) is not None:
            self.stats["releases"] += 1
            self.stats["active"] = len(self._contacts)
            self._apply()
            self._touch()

    def details(self) -> dict:
        info = {}
        try:
            info = self._port.probe()
        except Exception as exc:  # noqa: BLE001
            info = {"error": str(exc)}
        info["contacts"] = {cid: sensors.LABELS[name] for cid, name in self._contacts.items()}
        return info

    def close(self) -> None:
        self._stop.set()
        try:
            self._port.close()
        except Exception:  # noqa: BLE001
            pass


class SyntheticTouchBackend(TouchBackend):
    """Windows synthetic pointer injection (kept as a fallback, see module docstring)."""

    name = "synth"

    def __init__(self, client_rect_provider, max_contacts: int = 10, panel_provider=None):
        super().__init__(panel_provider)
        from touch_inject import SyntheticTouch

        self._rect_provider = client_rect_provider
        self._touch_api = SyntheticTouch(max_contacts=max_contacts)
        self._max = max_contacts
        self._ids: dict[int, int] = {}
        self._next = 1

    def _to_screen(self, nx: float, ny: float) -> tuple[float, float] | None:
        rect = self._rect_provider()
        if not rect:
            return None
        x, y, w, h = rect
        return x + min(max(nx, 0.0), 1.0) * (w - 1), y + min(max(ny, 0.0), 1.0) * (h - 1)

    def _alloc_id(self) -> int:
        for _ in range(self._max * 2):
            candidate = self._next
            self._next = self._next % self._max + 1
            if candidate not in self._ids.values():
                return candidate
        return 1

    def press(self, contact_id: int, nx: float, ny: float) -> None:
        point = self._to_screen(nx, ny)
        if point is None:
            self.stats["dropped"] += 1
            return
        if contact_id in self._ids:
            self.release(contact_id)
        pointer_id = self._alloc_id()
        try:
            self._touch_api.down(pointer_id, *point)
        except OSError as exc:
            self._fail(exc)
            return
        self._ids[contact_id] = pointer_id
        self.stats["presses"] += 1
        self.stats["active"] = len(self._ids)
        self._touch()

    def move(self, contact_id: int, nx: float, ny: float) -> None:
        pointer_id = self._ids.get(contact_id)
        point = self._to_screen(nx, ny)
        if pointer_id is None or point is None:
            self.stats["dropped"] += 1
            return
        try:
            self._touch_api.move(pointer_id, *point)
        except OSError as exc:
            self._fail(exc)
            return
        self.stats["moves"] += 1
        self._touch()

    def release(self, contact_id: int) -> None:
        pointer_id = self._ids.pop(contact_id, None)
        if pointer_id is None:
            return
        try:
            self._touch_api.up(pointer_id)
        except OSError as exc:
            self._fail(exc)
        self.stats["releases"] += 1
        self.stats["active"] = len(self._ids)
        self._touch()

    def close(self) -> None:
        for contact_id in list(self._ids):
            self.release(contact_id)
        try:
            self._touch_api.close()
        except Exception:  # noqa: BLE001
            pass


class LogBackend(TouchBackend):
    """Does nothing but log - useful for dry runs without the game running."""

    name = "log"

    def __init__(self, *_args, panel_provider=None, **_kwargs):
        super().__init__(panel_provider)
        self._ids: dict[int, str] = {}

    def press(self, contact_id: int, nx: float, ny: float) -> None:
        sensor = self._sensor(nx, ny)
        self._ids[contact_id] = sensor or "outside"
        self.stats["presses"] += 1
        self.stats["active"] = len(self._ids)
        log.info("press id=%s at (%.3f, %.3f) -> %s", contact_id, nx, ny, sensor or "outside the panel")
        self._touch()

    def move(self, contact_id: int, nx: float, ny: float) -> None:
        self.stats["moves"] += 1
        self._touch()

    def release(self, contact_id: int) -> None:
        self._ids.pop(contact_id, None)
        self.stats["releases"] += 1
        self.stats["active"] = len(self._ids)
        self._touch()

    def details(self) -> dict:
        return {"contacts": dict(self._ids)}

    def close(self) -> None:
        self._ids.clear()
        self.stats["active"] = 0


def create_backend(cfg: dict, client_rect_provider, zone_map=None, panel_provider=None) -> TouchBackend:
    kind = (cfg or {}).get("backend", "maitouch").lower()
    max_contacts = int((cfg or {}).get("max_contacts", 10))
    if kind in ("maitouch", "port"):
        return MaitouchPortBackend(
            process_names=(cfg or {}).get("process_names"), panel_provider=panel_provider
        )
    if kind == "synth":
        return SyntheticTouchBackend(
            client_rect_provider, max_contacts=max_contacts, panel_provider=panel_provider
        )
    if kind == "log":
        return LogBackend(panel_provider=panel_provider)
    raise ValueError(f"unknown touch backend: {kind}")
