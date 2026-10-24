"""Print the zone -> packet-bit / pixel table (used by docs/re_maitouch.md)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import maitouch_state as MS  # noqa: E402

NAMES = ["C"] + [r + str(a) for r in "OI" for a in (0, 45, 90, 135, 180, 225, 270, 315)]


def main():
    print("| zone | ring / octant | packet byte | bit | client px (960x960) | screen px @ (800,240) |")
    print("|---|---|---|---|---|---|")
    for n in NAMES:
        idx, bit = MS.ZONES[n]
        cx, cy, r = MS.zone_center(n)
        if n == "C":
            ring = "centre"
        else:
            ring = ("outer " if n[0] == "O" else "inner ") + n[1:] + "deg"
        print(f"| {n} | {ring} | {idx} | 0x{bit:02X} | {cx},{cy} | {800 + cx},{240 + cy} |")
    print()
    ok = True
    for n in NAMES:
        cx, cy, r = MS.zone_center(n)
        back = MS.zone_at(cx, cy)
        flag = "OK" if back == n else "MISMATCH"
        ok = ok and back == n
        print(f"{n:>5} -> {str(back):>5}  {flag}")
    print("round-trip all zones:", ok)
    print("radii (px, client): centre r <", round(MS.R_CENTRE * 480, 1),
          " inner r <", round(MS.R_INNER * 480, 1), " outer r <", 480)


if __name__ == "__main__":
    main()
