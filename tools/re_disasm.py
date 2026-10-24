"""Disassemble a VA range of mice.dll with capstone, annotating data refs."""
import os
import struct
import sys

from capstone import Cs, CS_ARCH_X86, CS_MODE_32, CS_OP_IMM, CS_OP_MEM

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from re_pe import PE  # noqa: E402


def annotate(pe, va):
    rva = pe.va2rva(va)
    if rva is None or rva < 0:
        return ""
    sec = pe.sec_of_rva(rva)
    if sec is None:
        return ""
    raw = pe.read_rva(rva, 96)
    if not raw:
        return ""
    if sec["name"] == ".rdata":
        if len(raw) >= 2 and raw[0] != 0 and raw[1] == 0:
            s = raw.decode("utf-16le", "replace").split("\0")[0]
            if s and all(32 <= ord(c) < 0x300 for c in s):
                return f"  ; WSTR {s!r}"
        s = raw.split(b"\0")[0].decode("latin1", "replace")
        if s and all(32 <= ord(c) < 127 for c in s):
            return f"  ; STR {s!r}"
    return ""


def main():
    pe = PE()
    start = int(sys.argv[1], 0)
    end = int(sys.argv[2], 0) if len(sys.argv) > 2 else start + 0x200
    md = Cs(CS_ARCH_X86, CS_MODE_32)
    md.detail = True
    off = pe.rva2off(pe.va2rva(start))
    code = pe.data[off:off + (end - start)]
    for ins in md.disasm(code, start):
        line = f"{ins.address:#010x}  {ins.bytes.hex():<20s} {ins.mnemonic} {ins.op_str}"
        note = ""
        for op in ins.operands:
            val = None
            if op.type == CS_OP_IMM:
                val = op.imm & 0xFFFFFFFF
            elif op.type == CS_OP_MEM and op.mem.base == 0 and op.mem.index == 0:
                val = op.mem.disp & 0xFFFFFFFF
            if val is not None and val >= 0x10100000 or (val is not None and 0x10000000 <= val < 0x101A6000):
                note += annotate(pe, val)
        print(line + note)


if __name__ == "__main__":
    main()
