"""Misc static queries for mice.dll."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from re_pe import PE  # noqa: E402


def strings(pe, rva0, rva1):
    off0, off1 = pe.rva2off(rva0), pe.rva2off(rva1)
    data = pe.data[off0:off1]
    i = 0
    while i < len(data):
        if 32 <= data[i] < 127:
            j = i
            while j < len(data) and 32 <= data[j] < 127:
                j += 1
            rva = rva0 + i
            print(f"{rva:#08x} {pe.rva2va(rva):#010x} {data[i:j].decode()!r}")
            i = j
        else:
            i += 1


def raw(pe, va, n):
    rva = pe.va2rva(va)
    b = pe.read_rva(rva, n)
    print(f"{rva:#x} ({pe.rva2va(rva):#010x}): {b.hex(' ')}")
    print(f"   ascii: {b.decode('latin1')!r}")
    import struct
    for k in range(0, n, 4):
        if k + 4 <= len(b):
            f = struct.unpack_from("<f", b, k)[0]
            print(f"   +{k:#x} hex={b[k:k+4].hex(' ')} float={f!r}")


if __name__ == "__main__":
    pe = PE()
    cmd = sys.argv[1]
    if cmd == "strings":
        strings(pe, int(sys.argv[2], 0), int(sys.argv[3], 0))
    elif cmd == "raw":
        raw(pe, int(sys.argv[2], 0), int(sys.argv[3], 0))
    elif cmd == "xref":
        needle = sys.argv[2].encode()
        for h in pe.find_ascii(needle) + pe.find_wide(sys.argv[2]):
            print(f"{h['kind']} va={h['va']:#x} {h['text']!r}")
            for r in pe.find_imm32(h["va"]):
                print(f"   ref va={r['va']:#010x}")
