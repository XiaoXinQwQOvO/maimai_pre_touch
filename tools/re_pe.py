"""Minimal x86 PE analyser for mice.dll (no external deps).

Provides:
  * section table / RVA<->file offset helpers
  * find_strings(needle, wide=True/False) -> list of (va, rva, fileoff, text)
  * find_refs(va) -> list of (ref_va, section, off) for imm32 / rel32 / abs32 occurrences
  * disasm-free heuristics: extracts the containing function via a linear scan
    backwards for 0xCC padding.

Usage:  python tools/re_pe.py strings
        python tools/re_pe.py xref <needle>
"""
from __future__ import annotations

import struct
import sys
import os
import json

DLL = r"G:\Original\maimai\mice.dll"


class PE:
    def __init__(self, path=DLL):
        self.path = path
        with open(path, "rb") as f:
            self.data = f.read()
        d = self.data
        assert d[:2] == b"MZ", "not MZ"
        e_lfanew = struct.unpack_from("<I", d, 0x3C)[0]
        assert d[e_lfanew:e_lfanew + 4] == b"PE\0\0", "not PE"
        coff = e_lfanew + 4
        (self.machine, self.nsec, self.timestamp, self.ptr_symtab, self.nsym,
         self.opt_size, self.characteristics) = struct.unpack_from("<HHIIIHH", d, coff)
        opt = coff + 20
        self.opt_off = opt
        magic = struct.unpack_from("<H", d, opt)[0]
        self.magic = magic
        self.is64 = magic == 0x20B
        if self.is64:
            self.image_base = struct.unpack_from("<Q", d, opt + 24)[0]
            self.size_of_image = struct.unpack_from("<I", d, opt + 56)[0]
            self.size_of_headers = struct.unpack_from("<I", d, opt + 60)[0]
            self.entry_rva = struct.unpack_from("<I", d, opt + 16)[0]
            self.dd_off = opt + 112
        else:
            self.image_base = struct.unpack_from("<I", d, opt + 28)[0]
            self.size_of_image = struct.unpack_from("<I", d, opt + 56)[0]
            self.size_of_headers = struct.unpack_from("<I", d, opt + 60)[0]
            self.entry_rva = struct.unpack_from("<I", d, opt + 16)[0]
            self.dd_off = opt + 96
        self.dll_characteristics = struct.unpack_from("<H", d, opt + 70)[0]
        self.sections = []
        sec = opt + self.opt_size
        for i in range(self.nsec):
            name = d[sec:sec + 8].rstrip(b"\0").decode("latin1")
            vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", d, sec + 8)
            chars = struct.unpack_from("<I", d, sec + 36)[0]
            self.sections.append({
                "name": name, "vsize": vsize, "vaddr": vaddr,
                "rawsize": rawsize, "rawptr": rawptr, "chars": chars,
            })
            sec += 40

    # ---- mapping ----
    def sec_of_rva(self, rva):
        for s in self.sections:
            if s["vaddr"] <= rva < s["vaddr"] + max(s["vsize"], s["rawsize"]):
                return s
        return None

    def rva2off(self, rva):
        s = self.sec_of_rva(rva)
        if s is None:
            if rva < self.size_of_headers:
                return rva
            return None
        delta = rva - s["vaddr"]
        if delta >= s["rawsize"]:
            return None
        return s["rawptr"] + delta

    def off2rva(self, off):
        for s in self.sections:
            if s["rawptr"] <= off < s["rawptr"] + s["rawsize"]:
                return s["vaddr"] + (off - s["rawptr"])
        if off < self.size_of_headers:
            return off
        return None

    def rva2va(self, rva):
        return self.image_base + rva

    def va2rva(self, va):
        return va - self.image_base

    def read_rva(self, rva, n):
        off = self.rva2off(rva)
        if off is None:
            return None
        return self.data[off:off + n]

    # ---- strings ----
    def find_ascii(self, needle: bytes, limit=64):
        out = []
        start = 0
        while True:
            i = self.data.find(needle, start)
            if i < 0 or len(out) >= limit:
                break
            # must be a plausible C string start: previous byte is 0 or non-printable
            if i == 0 or self.data[i - 1] == 0 or not (32 <= self.data[i - 1] < 127):
                rva = self.off2rva(i)
                end = self.data.find(b"\0", i)
                out.append({
                    "kind": "ascii", "fileoff": i,
                    "rva": rva, "va": None if rva is None else self.rva2va(rva),
                    "text": self.data[i:end].decode("latin1", "replace")[:120],
                })
            start = i + 1
        return out

    def find_wide(self, needle: str, limit=64):
        nb = needle.encode("utf-16le")
        out = []
        start = 0
        while True:
            i = self.data.find(nb, start)
            if i < 0 or len(out) >= limit:
                break
            rva = self.off2rva(i)
            end = i
            while end + 1 < len(self.data) and self.data[end:end + 2] != b"\0\0":
                end += 2
            out.append({
                "kind": "wide", "fileoff": i,
                "rva": rva, "va": None if rva is None else self.rva2va(rva),
                "text": self.data[i:end].decode("utf-16le", "replace")[:120],
            })
            start = i + 2
        return out

    # ---- xrefs ----
    def find_imm32(self, va, code_only=True):
        """Find every 4-byte little-endian occurrence of `va` (absolute address)."""
        pat = struct.pack("<I", va & 0xFFFFFFFF)
        out = []
        start = 0
        while True:
            i = self.data.find(pat, start)
            if i < 0:
                break
            rva = self.off2rva(i)
            s = self.sec_of_rva(rva) if rva is not None else None
            if rva is not None and (not code_only or (s and s["name"] == ".text")):
                out.append({"fileoff": i, "rva": rva, "va": self.rva2va(rva), "sec": s["name"] if s else None})
            start = i + 1
        return out

    def find_rel32_to(self, target_rva, code_only=True):
        """Find `call/jmp rel32` (E8/E9) whose computed target == target_rva."""
        out = []
        s = self.sec_of_rva(target_rva)
        for sec in self.sections:
            if code_only and sec["name"] != ".text":
                continue
            base_rva = sec["vaddr"]
            blk = self.data[sec["rawptr"]:sec["rawptr"] + sec["rawsize"]]
            i = 0
            n = len(blk)
            while i < n - 5:
                op = blk[i]
                if op in (0xE8, 0xE9):
                    rel = struct.unpack_from("<i", blk, i + 1)[0]
                    dest = base_rva + i + 5 + rel
                    if dest == target_rva:
                        out.append({"fileoff": sec["rawptr"] + i, "rva": base_rva + i,
                                    "va": self.rva2va(base_rva + i), "op": hex(op)})
                i += 1
        return out

    def section_of_va(self, va):
        rva = self.va2rva(va)
        s = self.sec_of_rva(rva)
        return s

    def dump_sections(self):
        rows = []
        for s in self.sections:
            rows.append(f"{s['name']:10s} va={self.rva2va(s['vaddr']):#010x} "
                        f"rva={s['vaddr']:#08x} vsize={s['vsize']:#08x} "
                        f"rawsize={s['rawsize']:#08x} rawptr={s['rawptr']:#08x} chars={s['chars']:#010x}")
        return "\n".join(rows)


TARGETS = [
    "maitouch", "STAT}", "HALT}", ">C:%d", ">D:%d S:%d L:%d", "ABCD", "EFGH",
    "IJKL", "MNOPQRSTU", "VWXY", "15222", "Micetools",
    "ReadFile(%ls) unimplemented", "WriteFile(%ls) unimplemented",
    "DeviceIoControl", "Refusing to attach to reserved port COM%hhu",
    "Attach to COM%hhu failed: %d", "Idle data on port %ls: %02x",
    "Port idler", "%ls woke up", "GetPointerInfo", "GetPointerType",
    "maicam", "aime_tn32msec", "mailed",
]


def main():
    pe = PE()
    print(f"{os.path.basename(pe.path)}: image_base={pe.image_base:#x} image_size={pe.size_of_image:#x} "
          f"entry={pe.rva2va(pe.entry_rva):#x} machine={pe.machine:#x}")
    print(pe.dump_sections())
    if len(sys.argv) == 1 or sys.argv[1] == "strings":
        print("\n=== strings ===")
        for t in TARGETS:
            hits = pe.find_ascii(t.encode()) + pe.find_wide(t)
            for h in hits:
                print(f"{h['kind']:5s} va={h['va']:#x} rva={h['rva']:#x} off={h['fileoff']:#x}  {h['text']!r}")
    if len(sys.argv) >= 3 and sys.argv[1] == "xref":
        needle = sys.argv[2]
        hits = pe.find_ascii(needle.encode()) + pe.find_wide(needle)
        for h in hits:
            print(f"\n--- {h['kind']} va={h['va']:#x} {h['text']!r}")
            for r in pe.find_imm32(h["va"]):
                print(f"    imm32 ref va={r['va']:#x} rva={r['rva']:#x} fileoff={r['fileoff']:#x} sec={r['sec']}")


if __name__ == "__main__":
    main()
