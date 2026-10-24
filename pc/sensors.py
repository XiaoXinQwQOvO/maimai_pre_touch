"""The 17 touch sensors that micetools' `maitouch` device reports to the game.

This is the single source of truth for the sensor geometry on the PC side; the
phone client mirrors it for its overlay (see `web/app.js`).

Verified against mice.dll by reverse engineering (see `docs/re_maitouch.md`):

  * the device pushes a 14-byte packet ``'(' + 12 sensor bytes + ')'`` to COM3
    about every 2 ms; every sensor byte is ``0x40 | bitmask`` and ``0x40`` alone
    means "not touched";
  * only sensor bytes 1..4 carry data -> 4 bytes x 4 bits = 16 ring sensors,
    plus one centre sensor at byte 4 bit 0x10 -> 17 sensors;
  * a touch is normalised as ``u = 2*nx - 1``, ``v = 2*ny - 1`` (nx, ny are the
    coordinates inside the game client area), ``r = hypot(u, v)`` and
    ``angle = atan2(v, u)``, i.e. 0 deg points to screen-right and grows
    clockwise (screen y points down);
  * ``r >= 1.0``            -> outside the panel, no sensor;
  * ``r < 0.2241``          -> centre sensor;
  * ``r < 0.5518``          -> inner ring, else outer ring;
  * the ring is split into 8 sectors of 45 deg: bit ``0x01``/``0x02`` is the
    half of the quadrant nearer the vertical axis, ``0x04``/``0x08`` the half
    nearer the horizontal axis (the inner ring is the outer bit shifted left).

Geometry sanity check: ``python pc/sensors.py`` prints the table and round-trips
every sensor through coordinates.
"""
from __future__ import annotations

import math

PACKET_OPEN = 0x28  # '('
PACKET_CLOSE = 0x29  # ')'
NO_TOUCH = 0x40  # '@'
SENSOR_BYTES = 12
PACKET_LEN = 14

R_CENTRE = 0.2241  # r < this      -> centre sensor
R_INNER = 0.5518  # r < this      -> inner ring
R_MAX = 1.0  # r >= this     -> nothing

SECTORS = 8
SECTOR_DEG = 360.0 / SECTORS

CENTRE = "C"
RING_OUTER = "O"
RING_INNER = "I"


def _ring_name(ring: str, sector: int) -> str:
    return f"{ring}{int(sector) * 45}"


#: sensor name -> (packet byte index 1..12, bit)
SENSORS: dict[str, tuple[int, int]] = {}
# outer ring: quadrants 1..4 carry the 0x01/0x04 bits
_QUADRANT_BYTE = {0: 2, 1: 3, 2: 4, 3: 1}
for _quad, _byte in _QUADRANT_BYTE.items():
    for _half, _bit in ((0, 0x01), (1, 0x04)):
        _sector = _quad * 2 + _half
        SENSORS[_ring_name(RING_OUTER, _sector)] = (_byte, _bit)
        SENSORS[_ring_name(RING_INNER, _sector)] = (_byte, _bit << 1)
SENSORS[CENTRE] = (4, 0x10)

#: display order, outermost first
SENSOR_ORDER = [CENTRE] + [
    _ring_name(ring, sector) for ring in (RING_INNER, RING_OUTER) for sector in range(SECTORS)
]

#: human readable label per sensor (A1..A8 outer, B1..B8 inner, C centre)
LABELS = {CENTRE: "C"}
for _sector in range(SECTORS):
    LABELS[_ring_name(RING_OUTER, _sector)] = f"A{_sector + 1}"
    LABELS[_ring_name(RING_INNER, _sector)] = f"B{_sector + 1}"


def _normalise(nx: float, ny: float) -> tuple[float, float, float]:
    u = 2.0 * nx - 1.0
    v = 2.0 * ny - 1.0
    return u, v, math.hypot(u, v)


#: the whole frame is the panel when nothing has been detected (legacy behaviour)
PANEL_FULL = {"cx": 0.5, "cy": 0.5, "rx": 0.5, "ry": 0.5}


def normalise_panel(panel) -> tuple[float, float, float, float]:
    """Accept None, a dict (``cx, cy, rx/ry`` or ``cx, cy, r``) or a 4-tuple."""
    if panel is None:
        panel = PANEL_FULL
    if isinstance(panel, dict):
        cx = float(panel.get("cx", 0.5))
        cy = float(panel.get("cy", 0.5))
        radius = float(panel.get("r", 0.5) or 0.5)
        rx = float(panel.get("rx", radius) or radius)
        ry = float(panel.get("ry", radius) or radius)
    else:
        cx, cy, rx, ry = (float(value) for value in panel)
    if rx <= 0.0 or ry <= 0.0:
        raise ValueError(f"panel radius must be positive, got rx={rx} ry={ry}")
    return cx, cy, rx, ry


def to_uv(nx: float, ny: float, panel=None) -> tuple[float, float]:
    """Frame-normalised point -> panel coordinates (0,0 = panel centre, 1 = rim)."""
    cx, cy, rx, ry = normalise_panel(panel)
    return (nx - cx) / rx, (ny - cy) / ry


def from_uv(u: float, v: float, panel=None) -> tuple[float, float]:
    """Panel coordinates -> frame-normalised point."""
    cx, cy, rx, ry = normalise_panel(panel)
    return cx + u * rx, cy + v * ry


def sensor_at(nx: float, ny: float, panel=None) -> str | None:
    """Which sensor does a normalised point (0..1, 0..1) land on? None if outside."""
    u, v = to_uv(nx, ny, panel)
    r = math.hypot(u, v)
    if r >= R_MAX:
        return None
    if r < R_CENTRE:
        return CENTRE
    sector = int((math.degrees(math.atan2(v, u)) % 360.0) // SECTOR_DEG) % SECTORS
    return _ring_name(RING_OUTER if r >= R_INNER else RING_INNER, sector)


def sensor_radius(name: str) -> float:
    """Representative radius (0..1) of a sensor, used for snapping and drawing."""
    if name == CENTRE:
        return 0.0
    if name.startswith(RING_INNER):
        return (R_CENTRE + R_INNER) / 2.0
    return (R_INNER + R_MAX) / 2.0


def sensor_point(name: str, panel=None) -> tuple[float, float]:
    """Representative normalised point of a sensor (mid radius, sector centre)."""
    name = name.upper()
    if name not in SENSORS:
        raise KeyError(f"unknown sensor {name!r}; known: {SENSOR_ORDER}")
    if name == CENTRE:
        cx, cy, _, _ = normalise_panel(panel)
        return cx, cy
    sector = int(name[1:]) // 45
    angle = math.radians(sector * SECTOR_DEG + SECTOR_DEG / 2.0)
    radius = sensor_radius(name)
    return from_uv(radius * math.cos(angle), radius * math.sin(angle), panel)


def packet_from_sensors(names) -> bytes:
    """Build the 14-byte status packet for a set of sensor names."""
    buckets: dict[int, int] = {}
    for name in names:
        key = name.upper()
        if key not in SENSORS:
            raise KeyError(f"unknown sensor {name!r}; known: {SENSOR_ORDER}")
        index, bit = SENSORS[key]
        buckets[index] = buckets.get(index, 0) | bit
    body = [NO_TOUCH | buckets.get(index, 0) for index in range(1, SENSOR_BYTES + 1)]
    return bytes([PACKET_OPEN] + body + [PACKET_CLOSE])


IDLE_PACKET = packet_from_sensors(())


def sensors_in(packet: bytes) -> list[str]:
    """Sensor names touched in a 14-byte status packet."""
    active = [
        name
        for name, (index, bit) in SENSORS.items()
        if index < len(packet) and packet[index] & bit
    ]
    return sorted(active, key=SENSOR_ORDER.index)


def describe(packet: bytes) -> str:
    active = sensors_in(packet)
    if not active:
        return "no touch"
    return " ".join(f"{name}({LABELS[name]})" for name in active)


def snap_to_sensor(nx: float, ny: float, panel=None) -> tuple[float, float]:
    """Snap a raw touch onto the centre of the sensor it landed on (outside -> unchanged)."""
    name = sensor_at(nx, ny, panel)
    if name is None:
        return nx, ny
    return sensor_point(name, panel)


def panel_geometry(panel=None) -> dict:
    """Panel description for the phone overlay / API (all values are frame fractions)."""
    cx, cy, rx, ry = normalise_panel(panel)
    return {
        "cx": cx,
        "cy": cy,
        "rx": rx,
        "ry": ry,
        "r_centre": R_CENTRE,
        "r_inner": R_INNER,
        "r_max": R_MAX,
        "sectors": SECTORS,
        "labels": LABELS,
        "count": len(SENSORS),
        "order": SENSOR_ORDER,
        "stretch": rx / ry if ry else 1.0,
    }


def _selfcheck() -> int:
    print(f"sensors: {len(SENSORS)} (centre 1 + inner {SECTORS} + outer {SECTORS})")
    width = max(len(name) for name in SENSORS) + 2
    print(f"{'sensor':<{width}}{'label':<7}{'byte':<6}{'bit':<7}{'radius':<8}centre point (nx, ny)")
    failures = 0
    for name in SENSOR_ORDER:
        index, bit = SENSORS[name]
        px, py = sensor_point(name)
        back = sensor_at(px, py)
        radius = sensor_radius(name)
        marker = "" if back == name else f"  <- MISMATCH (round-trip gave {back})"
        if marker:
            failures += 1
        print(
            f"{name:<{width}}{LABELS[name]:<7}{index:<6}{bit:#04x}  {radius:<8.3f}"
            f"({px:.4f}, {py:.4f}){marker}"
        )
    print()
    print("idle packet   :", IDLE_PACKET.hex(" "))
    print("all touched   :", packet_from_sensors(SENSOR_ORDER).hex(" "))
    print("round-trip    :", describe(packet_from_sensors(["C", "O90", "I0"])))
    for name in SENSOR_ORDER:
        packet = packet_from_sensors([name])
        if sensors_in(packet) != [name]:
            failures += 1
            print(f"[!] packet round-trip failed for {name}: {sensors_in(packet)}")

    # a detected panel (off-centre, stretched) must still round-trip
    detected = {"cx": 0.2516, "cy": 0.7165, "rx": 0.275, "ry": 0.2812}
    print("\ndetected panel:", detected)
    for name in SENSOR_ORDER:
        px, py = sensor_point(name, detected)
        back = sensor_at(px, py, detected)
        if back != name:
            failures += 1
            print(f"[!] panel round-trip failed for {name}: got {back} at ({px:.4f}, {py:.4f})")
    # the rim is the outside boundary: just inside hits the outer ring, just outside nothing
    rim_in = sensor_at(detected["cx"] + detected["rx"] * 0.98, detected["cy"], detected)
    rim_out = sensor_at(detected["cx"] + detected["rx"] * 1.02, detected["cy"], detected)
    print(f"panel rim     : 0.98r -> {rim_in};  1.02r -> {rim_out}")
    if rim_in != "O0" or rim_out is not None:
        failures += 1
        print("[!] panel rim mapping is off")

    print(f"\nSELFCHECK: {'PASS' if failures == 0 else f'FAIL ({failures})'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(_selfcheck())
