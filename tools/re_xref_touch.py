"""Batch-xref helper: prints code references to the maitouch string cluster."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from re_pe import PE  # noqa: E402

MAITOUCH_STRINGS = [
    0x1318A8, 0x1318B0, 0x1318B8, 0x1318D8, 0x1318F8, 0x131900, 0x131920,
    0x131944, 0x131960, 0x131978, 0x13198C, 0x1319A0, 0x1319A5, 0x1319AA,
    0x1319AF, 0x1319B9, 0x1319C0, 0x1319C8, 0x13171C, 0x131738, 0x131754,
    0x131760, 0x1317C4, 0x131790,
]


def main():
    pe = PE()
    allrefs = []
    for rva in MAITOUCH_STRINGS:
        va = pe.rva2va(rva)
        raw = pe.read_rva(rva, 80) or b""
        text = raw.split(b"\0")[0].decode("latin1")
        refs = pe.find_imm32(va)
        print(f"{va:#010x} {text!r}  -> {len(refs)} refs")
        for r in refs:
            print(f"    ref va={r['va']:#010x} sec={r['sec']}")
            allrefs.append(r["va"])
    print("\n=== unique .text ref VAs sorted ===")
    for va in sorted(set(allrefs)):
        print(f"{va:#010x}")


if __name__ == "__main__":
    main()
