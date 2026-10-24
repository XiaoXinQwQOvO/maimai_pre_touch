"""External control channel for micetools' maitouch touch status.

Reverse-engineered facts this module relies on (see docs/re_maitouch.md):

  mice.dll is loaded by the game at an arbitrary base (measured: 0x69690000;
  its PE preferred base 0x10000000 is NOT used), so every address below is an
  RVA and is resolved as `module_base + rva` at runtime.
  +0x0018B040  uint8[14]  touch status packet that mice writes to virtual COM3
                          layout: '(' , 12 sensor bytes, ')'
                          every sensor byte is 0x40 | bitmask
  +0x00043FD   build_status(): called ~500x/s by the maitouch device thread
                          immediately before the 14-byte packet is written.
                          It overwrites the whole packet with the idle value
                          0x40 (i.e. "no touch") and then ORs in the zones it
                          derives from the real mouse / WM_POINTER contacts.

Strategy: freeze build_status with a one-byte `ret` patch (0x55 -> 0xC3) so
nothing ever rewrites the packet again, then write our own packet with
WriteProcessMemory.  No DLL injection, no frida, no code cave: two WriteProcess
Memory calls and the game keeps receiving exactly the touch state we choose.

Restore: put the original 0x55 back (and optionally the idle packet).
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import struct
import time

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
TH32CS_SNAPPROCESS = 0x00000002

PACKET_RVA = 0x0018B040
BUILD_STATUS_RVA = 0x00043FD
BUILD_STATUS_ORIG = 0x55   # push ebp
BUILD_STATUS_PATCH = 0xC3  # ret

PACKET_LEN = 14
BASE_CHAR = 0x40           # '@' -> "no touch" filler


class MODULEENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("th32ModuleID", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("GlblcntUsage", wt.DWORD), ("ProccntUsage", wt.DWORD),
                ("modBaseAddr", ctypes.c_void_p), ("modBaseSize", wt.DWORD),
                ("hModule", wt.HMODULE), ("szModule", ctypes.c_char * 256),
                ("szExePath", ctypes.c_char * 260)]


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wt.DWORD),
                ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                ("szExeFile", ctypes.c_char * 260)]


def find_pid(names=("maimai_dump_.exe", "maimai.exe")):
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        raise OSError("snapshot failed")
    try:
        pe = PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(pe)
        ok = kernel32.Process32First(snap, ctypes.byref(pe))
        while ok:
            exe = pe.szExeFile.decode("latin1")
            if exe.lower() in [n.lower() for n in names]:
                return pe.th32ProcessID, exe
            ok = kernel32.Process32Next(snap, ctypes.byref(pe))
    finally:
        kernel32.CloseHandle(snap)
    return None, None


def module_base(pid, name="mice.dll"):
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if snap == -1:
        raise OSError(f"module snapshot failed: {ctypes.get_last_error()}")
    try:
        me = MODULEENTRY32()
        me.dwSize = ctypes.sizeof(me)
        ok = kernel32.Module32First(snap, ctypes.byref(me))
        while ok:
            mod = me.szModule.decode("latin1")
            if mod.lower() == name.lower():
                return me.modBaseAddr or 0, me.modBaseSize
            ok = kernel32.Module32Next(snap, ctypes.byref(me))
    finally:
        kernel32.CloseHandle(snap)
    raise OSError(f"{name} not loaded in pid {pid}")


class MaitouchWriter:
    """Write the maitouch touch status of a running maimai from outside."""

    def __init__(self, pid=None, exe=None):
        if pid is None:
            pid, exe = find_pid()
            if pid is None:
                raise RuntimeError("maimai game process not found")
        self.pid = pid
        self.exe = exe
        self.base, self.size = module_base(pid)
        self.h = kernel32.OpenProcess(
            PROCESS_VM_OPERATION | PROCESS_VM_WRITE | PROCESS_VM_READ | PROCESS_QUERY_INFORMATION,
            False, pid)
        if not self.h:
            raise OSError(f"OpenProcess failed: {ctypes.get_last_error()}")
        self._frozen = False

    # -- low level ---------------------------------------------------------
    def read(self, addr, n):
        buf = ctypes.create_string_buffer(n)
        got = ctypes.c_size_t(0)
        if not kernel32.ReadProcessMemory(self.h, ctypes.c_void_p(addr), buf, n, ctypes.byref(got)):
            raise OSError(f"ReadProcessMemory {addr:#x}: {ctypes.get_last_error()}")
        return buf.raw[:got.value]

    def write(self, addr, data: bytes):
        buf = ctypes.create_string_buffer(data, len(data))
        done = ctypes.c_size_t(0)
        if not kernel32.WriteProcessMemory(self.h, ctypes.c_void_p(addr), buf, len(data),
                                           ctypes.byref(done)):
            raise OSError(f"WriteProcessMemory {addr:#x}: {ctypes.get_last_error()}")
        return done.value

    # -- derived addresses -------------------------------------------------
    @property
    def packet_addr(self):
        return self.base + PACKET_RVA

    @property
    def build_status_addr(self):
        return self.base + BUILD_STATUS_RVA

    # -- operations --------------------------------------------------------
    def probe(self):
        pro = self.read(self.build_status_addr, 6)
        pkt = self.read(self.packet_addr, PACKET_LEN)
        return {"pid": self.pid, "exe": self.exe, "base": hex(self.base),
                "build_status": pro.hex(" "), "packet": pkt.hex(" "),
                "frozen": self._frozen}

    def freeze(self):
        """Neutralise build_status so the packet stops being regenerated."""
        cur = self.read(self.build_status_addr, 1)
        if cur[0] == BUILD_STATUS_PATCH:
            self._frozen = True
            return "already frozen"
        if cur[0] != BUILD_STATUS_ORIG:
            raise RuntimeError(f"unexpected build_status prologue {cur.hex()} "
                               f"(expected {BUILD_STATUS_ORIG:02x}) - wrong mice.dll version?")
        self.write(self.build_status_addr, bytes([BUILD_STATUS_PATCH]))
        back = self.read(self.build_status_addr, 1)
        if back[0] != BUILD_STATUS_PATCH:
            raise RuntimeError("patch did not stick")
        self._frozen = True
        return "frozen"

    def unfreeze(self):
        self.write(self.build_status_addr, bytes([BUILD_STATUS_ORIG]))
        self._frozen = False
        return "restored"

    def write_packet(self, packet: bytes):
        if len(packet) != PACKET_LEN:
            raise ValueError("packet must be 14 bytes")
        self.write(self.packet_addr, packet)
        back = self.read(self.packet_addr, PACKET_LEN)
        if back != packet:
            raise RuntimeError(f"read back mismatch {back.hex()} != {packet.hex()}")
        return back

    def set_bytes(self, sensor_bytes: dict):
        """sensor_bytes: {1..12: bitmask}.  Builds 0x40|mask for each byte."""
        pkt = bytearray([0x28] + [BASE_CHAR] * 12 + [0x29])
        for idx, mask in sensor_bytes.items():
            if not 1 <= idx <= 12:
                raise ValueError(f"sensor byte index {idx} out of range 1..12")
            pkt[idx] = BASE_CHAR | (mask & 0x3F)
        return self.write_packet(bytes(pkt))

    def clear(self):
        return self.set_bytes({})

    def close(self):
        """Only closes the process handle; the `ret` patch stays in place until
        unfreeze()/restore is called explicitly (also survives this process)."""
        if self.h:
            kernel32.CloseHandle(self.h)
            self.h = None

    def restore(self):
        """Put mice.dll's own status generator back and reset the packet."""
        try:
            self.unfreeze()
        except OSError:
            pass
        self.clear()
        return "restored"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ---------------------------------------------------------------------------
# zone model (derived from docs/re_maitouch.md, evidence: tools/frida_mouse_grid.py)
# Each zone is (packet byte index 1..12, bit).
# ---------------------------------------------------------------------------
ZONES = {
    # centre
    "C":     (4, 0x10),
    # outer ring: 8 octants, azimuth measured from screen-right, clockwise
    # (screen y grows downwards).  bit 0x01 = first octant of the quadrant,
    # 0x04 = second octant; the inner ring is the same bit shifted left by 1.
    "O270":  (1, 0x01),   # 270..315 deg
    "O315":  (1, 0x04),   # 315..360 deg
    "O0":    (2, 0x01),   #   0..45  deg
    "O45":   (2, 0x04),   #  45..90  deg
    "O90":   (3, 0x01),   #  90..135 deg
    "O135":  (3, 0x04),   # 135..180 deg
    "O180":  (4, 0x01),   # 180..225 deg
    "O225":  (4, 0x04),   # 225..270 deg
    # inner ring (same octants, bit << 1)
    "I270":  (1, 0x02),
    "I315":  (1, 0x08),
    "I0":    (2, 0x02),
    "I45":   (2, 0x08),
    "I90":   (3, 0x02),
    "I135":  (3, 0x08),
    "I180":  (4, 0x02),
    "I225":  (4, 0x08),
}

# geometry of the touch circle, measured (see docs/re_maitouch.md)
# u = 2*cx/clientW - 1 , v = 2*cy/clientH - 1
R_CENTRE = 0.2241      # r < R_CENTRE  -> centre sensor
R_INNER = 0.5518       # r < R_INNER   -> inner ring, else outer ring
R_MAX = 1.0            # r >= R_MAX    -> no sensor at all


def packet_from_zones(names):
    buckets = {}
    for n in names:
        key = n.strip().upper()
        if key not in ZONES:
            raise KeyError(f"unknown zone {n!r}; known: {sorted(ZONES)}")
        idx, bit = ZONES[key]
        buckets[idx] = buckets.get(idx, 0) | bit
    return bytes([0x28] + [BASE_CHAR | buckets.get(i, 0) for i in range(1, 13)] + [0x29])


def describe(packet: bytes):
    """Human readable list of active zones found in a 14-byte packet."""
    out = []
    for name, (idx, bit) in ZONES.items():
        if idx < len(packet) and packet[idx] & bit:
            out.append(name)
    return sorted(out)


def zone_at(cx, cy, width=960, height=960):
    """Which sensor does a game-client pixel (cx, cy) land on?  None if outside."""
    import math
    u = 2.0 * cx / width - 1.0
    v = 2.0 * cy / height - 1.0
    r = math.hypot(u, v)
    if r >= R_MAX:
        return None
    if r < R_CENTRE:
        return "C"
    deg = math.degrees(math.atan2(v, u)) % 360.0
    octant = int(deg // 45.0)                      # 0..7, each 45 deg wide
    name = ("O" if r >= R_INNER else "I") + str(octant * 45)
    return name


def zone_center(name, width=960, height=960):
    """Representative client pixel + radius for a sensor (centre of its area)."""
    import math
    name = name.strip().upper()
    if name == "C":
        return (width // 2, height // 2, 0.0)
    ring = 0.80 if name.startswith("O") else 0.40
    deg = float(name[1:]) + 22.5
    rad = math.radians(deg)
    cx = (1.0 + ring * math.cos(rad)) * width / 2.0
    cy = (1.0 + ring * math.sin(rad)) * height / 2.0
    return (int(round(cx)), int(round(cy)), ring)


if __name__ == "__main__":
    with MaitouchWriter() as w:
        print(w.probe())
        print("current zones:", describe(w.read(w.packet_addr, PACKET_LEN)))
