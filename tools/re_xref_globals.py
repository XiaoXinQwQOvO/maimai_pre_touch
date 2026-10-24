"""mice.dll deep static helpers: import table resolution + global-VA xref sweep."""
import os
import struct
import sys

from capstone import Cs, CS_ARCH_X86, CS_MODE_32, CS_OP_IMM, CS_OP_MEM, CS_GRP_JUMP, CS_GRP_CALL

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from re_pe import PE  # noqa: E402

IMPORT_DIR = 1


def parse_imports(pe):
    """Returns {iat_slot_va: 'module!func'} for the 32-bit import directory."""
    dd_rva, dd_size = struct.unpack_from("<II", pe.data, pe.dd_off + 8 * IMPORT_DIR)
    out = {}
    if not dd_rva:
        return out
    off = pe.rva2off(dd_rva)
    i = 0
    while True:
        ent = pe.data[off + i * 20: off + i * 20 + 20]
        if len(ent) < 20:
            break
        oft, ts, fc, name_rva, ft = struct.unpack("<IIIII", ent)
        if name_rva == 0:
            break
        modoff = pe.rva2off(name_rva)
        modname = pe.data[modoff:pe.data.find(b"\0", modoff)].decode("latin1")
        thunk = oft or ft
        k = 0
        slot = ft
        while True:
            tot_off = pe.rva2off(thunk + k * 4) if thunk else None
            if tot_off is None:
                break
            val = struct.unpack_from("<I", pe.data, tot_off)[0]
            if val == 0:
                break
            if val & 0x80000000:
                fname = f"ord#{val & 0xFFFF}"
            else:
                noff = pe.rva2off(val + 2)
                fname = pe.data[noff:pe.data.find(b"\0", noff)].decode("latin1")
            out[pe.rva2va(slot + k * 4)] = f"{modname}!{fname}"
            k += 1
        i += 1
    return out


def sweep_refs(pe, targets, imports=None):
    """Linear-sweep .text; report instructions touching any of `targets`."""
    md = Cs(CS_ARCH_X86, CS_MODE_32)
    md.detail = True
    sec = next(s for s in pe.sections if s["name"] == ".text")
    code = pe.data[sec["rawptr"]:sec["rawptr"] + sec["rawsize"]]
    base = pe.rva2va(sec["vaddr"])
    tset = set(t & 0xFFFFFFFF for t in targets)
    hits = {t: [] for t in tset}
    for ins in md.disasm(code, base):
        for op in ins.operands:
            val = None
            if op.type == CS_OP_IMM:
                val = op.imm & 0xFFFFFFFF
            elif op.type == CS_OP_MEM and op.mem.base == 0 and op.mem.index == 0:
                val = op.mem.disp & 0xFFFFFFFF
            if val in tset:
                name = ""
                if imports and val in imports:
                    name = imports[val]
                hits[val].append((ins.address, f"{ins.mnemonic} {ins.op_str}", name))
    return hits


GLOBALS = {
    0x1018B040: "touch_out_buf[14] (reply packet base)",
    0x1018B041: "touch_out_buf+1",
    0x1018B046: "touch_out_buf+6",
    0x1018AAC0: "g_pointerId",
    0x1018AA70: "g_last_k_byte",
    0x10161248: "g_touch_active",
    0x101614E4: "g_list_ptr_a",
    0x101614E8: "g_stride_a",
    0x101614EC: "g_ptr_a",
    0x10186804: "log_obj_eeprom",
    0x10186808: "log_obj_touchport",
}

IAT_PROBE = [0x10102360, 0x10102364, 0x10102368, 0x1010236C, 0x10102370,
             0x10102118, 0x10102120, 0x10102124]


def main():
    pe = PE()
    imports = parse_imports(pe)
    print(f"imports: {len(imports)} slots")
    for va in IAT_PROBE:
        print(f"  IAT {va:#010x} -> {imports.get(va, '<not an import>')}")
    print("\n=== interesting IAT entries (user/pointer/input related) ===")
    for va, name in sorted(imports.items()):
        low = name.lower()
        if any(k in low for k in ("pointer", "touch", "cursor", "mouse", "input.dll", "getkeyboard", "hid")):
            print(f"  {va:#010x} {name}")

    print("\n=== global refs ===")
    hits = sweep_refs(pe, GLOBALS.keys(), imports)
    for va in sorted(GLOBALS):
        print(f"\n--- {va:#010x} {GLOBALS[va]}")
        for addr, text, imp in hits[va]:
            print(f"    {addr:#010x}  {text}   {imp}")


if __name__ == "__main__":
    main()
