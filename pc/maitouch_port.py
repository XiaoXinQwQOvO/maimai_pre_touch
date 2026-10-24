"""Write the `maitouch` touch status straight into the running game process.

micetools emulates the maimai touch panel as a virtual serial port (`com3=maitouch`).
Its device thread pushes a 14-byte status packet to the game every ~2 ms:

    '(' + 12 sensor bytes + ')'       sensor byte = 0x40 | bitmask

`mice.dll!build_status` (RVA 0x43FD) is the only code that rewrites that packet,
and it derives the touched sensors from the mouse cursor and the WM_POINTER
contact list.  Two writes therefore give us full control of what the game reads
from COM3:

  1. patch the first byte of `build_status` from ``push ebp`` (0x55) to
     ``ret`` (0xC3) so nothing regenerates the packet any more;
  2. ``WriteProcessMemory`` our own packet into the status buffer
     (RVA 0x18B040).

No mouse emulation, no code cave, no injection - the game simply receives the
touch data we chose on its touch port.

Background and evidence: ``docs/re_maitouch.md``.  Geometry/packet helpers:
``pc/sensors.py``.

CLI (handy while testing by hand):

    python pc/maitouch_port.py probe
    python pc/maitouch_port.py set C O90 I270
    python pc/maitouch_port.py clear
    python pc/maitouch_port.py restore
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import sensors  # noqa: E402

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

DEFAULT_PROCESS_NAMES = ("maimai_dump_.exe", "maimai.exe")
DEFAULT_MODULE = "mice.dll"

PACKET_RVA = 0x0018B040
BUILD_STATUS_RVA = 0x00043FD
TOUCH_ACTIVE_RVA = 0x00161248

BUILD_STATUS_PROLOGUE = bytes.fromhex("55 8b ec 83 e4 f8")
BUILD_STATUS_ORIG = BUILD_STATUS_PROLOGUE[0]  # 0x55 push ebp
BUILD_STATUS_PATCH = 0xC3  # ret


class MODULEENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD), ("th32ModuleID", wt.DWORD), ("th32ProcessID", wt.DWORD),
        ("GlblcntUsage", wt.DWORD), ("ProccntUsage", wt.DWORD),
        ("modBaseAddr", ctypes.c_void_p), ("modBaseSize", wt.DWORD), ("hModule", wt.HMODULE),
        ("szModule", ctypes.c_char * 256), ("szExePath", ctypes.c_char * 260),
    ]


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD), ("szExeFile", ctypes.c_char * 260),
    ]


def find_pids(names=DEFAULT_PROCESS_NAMES) -> list[tuple[int, str]]:
    """All running process ids whose exe name is in `names`."""
    wanted = {name.lower() for name in names}
    found: list[tuple[int, str]] = []
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return found
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Process32First(snapshot, ctypes.byref(entry))
        while ok:
            exe = entry.szExeFile.decode("latin1")
            if exe.lower() in wanted:
                found.append((entry.th32ProcessID, exe))
            ok = kernel32.Process32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return found


def module_base(pid: int, name: str = DEFAULT_MODULE) -> tuple[int, int] | None:
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if snapshot == INVALID_HANDLE_VALUE:
        return None
    try:
        entry = MODULEENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Module32First(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szModule.decode("latin1").lower() == name.lower():
                return int(entry.modBaseAddr or 0), int(entry.modBaseSize)
            ok = kernel32.Module32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return None


class MaitouchPort:
    """Lazily attached writer for the maitouch status packet.

    All methods are safe to call when the game is not running: they return False
    instead of raising, so the server can keep running and pick the game up later.
    """

    def __init__(self, process_names=DEFAULT_PROCESS_NAMES, module: str = DEFAULT_MODULE, log=None):
        self._names = tuple(process_names)
        self._module = module
        self._lock = threading.RLock()
        self._handle = None
        self._pid = None
        self._exe = None
        self._base = 0
        self._frozen = False
        self._last_packet = None
        self.last_error: str | None = None
        self.attach_attempts = 0
        self.attached_at = 0.0
        self._log = log

    # -- logging ------------------------------------------------------------
    def _say(self, level: str, message: str, *args) -> None:
        if self._log is not None:
            getattr(self._log, level)("maitouch: " + message, *args)

    # -- lifecycle ----------------------------------------------------------
    @property
    def attached(self) -> bool:
        return self._handle is not None

    @property
    def frozen(self) -> bool:
        return self._frozen

    @property
    def pid(self) -> int | None:
        return self._pid

    @property
    def base(self) -> int:
        return self._base

    def attach(self) -> bool:
        """Attach to the game process and its mice.dll. Idempotent."""
        with self._lock:
            if self._handle is not None:
                return True
            self.attach_attempts += 1
            for pid, exe in find_pids(self._names):
                module = module_base(pid, self._module)
                if not module:
                    continue
                base = module[0]
                handle = kernel32.OpenProcess(
                    PROCESS_VM_OPERATION | PROCESS_VM_WRITE | PROCESS_VM_READ | PROCESS_QUERY_INFORMATION,
                    False,
                    pid,
                )
                if not handle:
                    self.last_error = f"OpenProcess({pid}) failed: {ctypes.get_last_error()}"
                    continue
                self._handle = handle
                self._pid = pid
                self._exe = exe
                self._base = base
                self._frozen = False
                self._last_packet = None
                self.attached_at = time.time()
                self.last_error = None
                self._say("info", "attached to %s (pid %s), mice.dll base %#x", exe, pid, base)
                return True
            self.last_error = "game/mice.dll not found"
            return False

    def detach(self) -> None:
        with self._lock:
            if self._handle:
                kernel32.CloseHandle(self._handle)
            self._handle = None
            self._pid = None
            self._base = 0
            self._frozen = False
            self._last_packet = None

    def close(self) -> None:
        """Restore the game to its normal (mouse driven) state and detach."""
        try:
            self.restore()
        finally:
            self.detach()

    # -- memory access ------------------------------------------------------
    def read(self, rva: int, size: int) -> bytes | None:
        with self._lock:
            if self._handle is None:
                return None
            buffer = ctypes.create_string_buffer(size)
            read = ctypes.c_size_t(0)
            ok = kernel32.ReadProcessMemory(
                self._handle, ctypes.c_void_p(self._base + rva), buffer, size, ctypes.byref(read)
            )
            if not ok:
                self.last_error = f"ReadProcessMemory({rva:#x}) failed: {ctypes.get_last_error()}"
                self.detach()  # process probably gone
                return None
            return buffer.raw[: read.value]

    def write(self, rva: int, data: bytes) -> bool:
        with self._lock:
            if self._handle is None:
                return False
            buffer = ctypes.create_string_buffer(data, len(data))
            written = ctypes.c_size_t(0)
            ok = kernel32.WriteProcessMemory(
                self._handle, ctypes.c_void_p(self._base + rva), buffer, len(data), ctypes.byref(written)
            )
            if not ok:
                self.last_error = f"WriteProcessMemory({rva:#x}) failed: {ctypes.get_last_error()}"
                self.detach()
                return False
            return written.value == len(data)

    # -- state --------------------------------------------------------------
    def probe(self) -> dict:
        prologue = self.read(BUILD_STATUS_RVA, len(BUILD_STATUS_PROLOGUE))
        packet = self.read(PACKET_RVA, sensors.PACKET_LEN)
        active = self.read(TOUCH_ACTIVE_RVA, 4)
        if prologue:
            # the in-memory byte is the truth, not our cached flag
            self._frozen = prologue[0] == BUILD_STATUS_PATCH
        return {
            "attached": self.attached,
            "pid": self._pid,
            "exe": self._exe,
            "base": hex(self._base) if self._base else None,
            "frozen": self._frozen,
            "build_status": prologue.hex(" ") if prologue else None,
            "packet": packet.hex(" ") if packet else None,
            "sensors": sensors.sensors_in(packet) if packet else None,
            "touch_active": bool(active and int.from_bytes(active, "little")) if active else None,
            "last_error": self.last_error,
        }

    def freeze(self) -> bool:
        """Stop mice.dll from regenerating the packet (one byte `ret` patch)."""
        with self._lock:
            if not self.attach():
                return False
            current = self.read(BUILD_STATUS_RVA, len(BUILD_STATUS_PROLOGUE))
            if current is None:
                return False
            if current[0] == BUILD_STATUS_PATCH:
                self._frozen = True
                return True
            if current != BUILD_STATUS_PROLOGUE:
                self.last_error = (
                    f"unexpected build_status prologue {current.hex(' ')} "
                    f"(expected {BUILD_STATUS_PROLOGUE.hex(' ')}) - mice.dll version mismatch?"
                )
                self._say("warning", "%s", self.last_error)
                return False
            if not self.write(BUILD_STATUS_RVA, bytes([BUILD_STATUS_PATCH])):
                return False
            check = self.read(BUILD_STATUS_RVA, 1)
            if not check or check[0] != BUILD_STATUS_PATCH:
                self.last_error = "ret patch did not stick"
                return False
            self._frozen = True
            self._say("info", "build_status frozen at %#x", self._base + BUILD_STATUS_RVA)
            return True

    def unfreeze(self) -> bool:
        with self._lock:
            if not self.attached:
                return False
            current = self.read(BUILD_STATUS_RVA, 1)
            if current is None:
                return False
            if current[0] == BUILD_STATUS_ORIG:
                self._frozen = False
                return True
            if not self.write(BUILD_STATUS_RVA, bytes([BUILD_STATUS_ORIG])):
                return False
            self._frozen = False
            self._say("info", "build_status restored")
            return True

    def write_packet(self, packet: bytes) -> bool:
        if len(packet) != sensors.PACKET_LEN:
            raise ValueError(f"packet must be {sensors.PACKET_LEN} bytes")
        if not self.freeze():
            return False
        if not self.write(PACKET_RVA, packet):
            return False
        self._last_packet = packet
        return True

    def set_sensors(self, names) -> bool:
        """Write the packet for a set of sensor names (only when it changed)."""
        packet = sensors.packet_from_sensors(names)
        if packet == self._last_packet:
            return True
        return self.write_packet(packet)

    def clear(self) -> bool:
        return self.write_packet(sensors.IDLE_PACKET)

    def restore(self) -> bool:
        """Put the packet back to idle and let mice.dll drive it again."""
        with self._lock:
            if not self.attached:
                return True
            ok = self.write(PACKET_RVA, sensors.IDLE_PACKET)
            # unfreeze() looks at the actual byte, so this also cleans up a patch
            # left behind by an earlier (crashed) run
            ok = self.unfreeze() and ok
            self._last_packet = None
            return ok


def _cli(argv=None) -> int:
    parser = argparse.ArgumentParser(description="write maitouch touch status into the running game")
    parser.add_argument("command", choices=["probe", "freeze", "unfreeze", "clear", "restore", "set", "watch"])
    parser.add_argument("sensors", nargs="*", help="sensor names, e.g. C O0 I90 (use --list to show them)")
    parser.add_argument("--list", action="store_true", help="print the sensor table and exit")
    parser.add_argument("--seconds", type=float, default=5.0, help="watch duration")
    args = parser.parse_args(argv)

    if args.list:
        return sensors._selfcheck()

    port = MaitouchPort()
    if not port.attach():
        print(f"[!] cannot attach: {port.last_error}")
        return 1
    print("[i] probe:", port.probe())

    if args.command == "probe":
        pass
    elif args.command == "freeze":
        print("[i] freeze:", port.freeze())
    elif args.command == "unfreeze":
        print("[i] unfreeze:", port.unfreeze())
    elif args.command == "clear":
        print("[i] clear:", port.clear())
    elif args.command == "restore":
        print("[i] restore:", port.restore())
    elif args.command == "set":
        names = [n.upper() for n in args.sensors]
        print(f"[i] set {names}:", port.set_sensors(names), "->", port.probe()["packet"])
    elif args.command == "watch":
        deadline = time.time() + args.seconds
        last = None
        while time.time() < deadline:
            info = port.probe()
            sensors_now = info["sensors"]
            if sensors_now != last:
                last = sensors_now
                print(f"[{time.strftime('%H:%M:%S')}] touch_active={info['touch_active']} frozen={info['frozen']} sensors={sensors_now}")
            time.sleep(0.2)
    port.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
