"""Touch injection backends.

The server talks to a backend through a tiny interface so the transport (and the
way we hand touch data to the game) stays swappable:

    backend.press(contact_id, nx, ny)   # nx, ny normalised 0..1 in the game client area
    backend.move(contact_id, nx, ny)
    backend.release(contact_id)
    backend.close()

`SyntheticTouchBackend` injects real Windows pointer contacts (PT_TOUCH) - i.e. it
looks like a physical touch digitizer to the game, never like a mouse.
`MaitouchStateBackend` (see docs/re_maitouch.md) writes the virtual serial port's
touch state directly once it has been located in the game process.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)


class TouchBackend:
    name = "base"

    def __init__(self):
        self.stats = {
            "presses": 0,
            "moves": 0,
            "releases": 0,
            "dropped": 0,
            "errors": 0,
            "active": 0,
            "last_event": 0.0,
        }

    # --- helpers -----------------------------------------------------------

    def _touch(self) -> None:
        self.stats["last_event"] = time.time()

    def _fail(self, exc: Exception) -> None:
        self.stats["errors"] += 1
        if self.stats["errors"] <= 5 or self.stats["errors"] % 500 == 0:
            log.warning("%s: injection failed: %s", self.name, exc)

    def describe(self) -> str:
        return self.name

    # --- interface ---------------------------------------------------------

    def press(self, contact_id: int, nx: float, ny: float) -> None:
        raise NotImplementedError

    def move(self, contact_id: int, nx: float, ny: float) -> None:
        raise NotImplementedError

    def release(self, contact_id: int) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class SyntheticTouchBackend(TouchBackend):
    """Windows synthetic pointer injection (real multi-touch, up to 10 contacts)."""

    name = "synth"

    def __init__(self, client_rect_provider, max_contacts: int = 10):
        super().__init__()
        from touch_inject import SyntheticTouch

        self._rect_provider = client_rect_provider
        self._touch_api = SyntheticTouch(max_contacts=max_contacts)
        self._max = max_contacts
        self._ids: dict[int, int] = {}
        self._next = 1
        self._last_client = None

    def _to_screen(self, nx: float, ny: float) -> tuple[float, float] | None:
        rect = self._rect_provider()
        if not rect:
            return None
        self._last_client = rect
        x, y, w, h = rect
        px = x + min(max(nx, 0.0), 1.0) * (w - 1)
        py = y + min(max(ny, 0.0), 1.0) * (h - 1)
        return px, py

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
        except Exception:
            pass


class LogBackend(TouchBackend):
    """Does nothing but log - useful for dry runs without the game running."""

    name = "log"

    def __init__(self, *_args, **_kwargs):
        super().__init__()
        self._ids: set[int] = set()

    def press(self, contact_id: int, nx: float, ny: float) -> None:
        self._ids.add(contact_id)
        self.stats["presses"] += 1
        self.stats["active"] = len(self._ids)
        log.info("press id=%s at (%.3f, %.3f)", contact_id, nx, ny)
        self._touch()

    def move(self, contact_id: int, nx: float, ny: float) -> None:
        self.stats["moves"] += 1
        self._touch()

    def release(self, contact_id: int) -> None:
        self._ids.discard(contact_id)
        self.stats["releases"] += 1
        self.stats["active"] = len(self._ids)
        self._touch()

    def close(self) -> None:
        self._ids.clear()
        self.stats["active"] = 0


class MaitouchStateBackend(TouchBackend):
    """Writes the touch state that the `maitouch` virtual COM port reports.

    Enabled only when the reverse-engineering notes in docs/re_maitouch.md provide
    a reliable state location; until then it refuses to start rather than
    silently dropping touches.
    """

    name = "maitouch"

    def __init__(self, *args, **kwargs):
        super().__init__()
        raise NotImplementedError(
            "maitouch state backend is not wired up yet - see docs/re_maitouch.md "
            "and use touch.backend = \"synth\" meanwhile"
        )


def create_backend(cfg: dict, client_rect_provider, zone_map=None) -> TouchBackend:
    kind = (cfg or {}).get("backend", "synth").lower()
    max_contacts = int((cfg or {}).get("max_contacts", 10))
    if kind == "synth":
        return SyntheticTouchBackend(client_rect_provider, max_contacts=max_contacts)
    if kind == "log":
        return LogBackend()
    if kind in ("maitouch", "port"):
        return MaitouchStateBackend()
    raise ValueError(f"unknown touch backend: {kind}")
