"""Ring/sector zone geometry for the maimai touch panel.

Coordinates are normalised to the game client area: (0, 0) is the top-left corner,
(1, 1) the bottom-right.  The playfield is a circle centred on `center`; distances
are expressed as fractions of half the shorter client side (1.0 == the edge of the
circle that inscribes the client area).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

RING_LETTERS_OUTER_FIRST = ("A", "B", "C", "D", "E")


@dataclass(frozen=True)
class Zone:
    """A touched zone: ring 0 = innermost .. 4 = outermost, sector 0 = 12 o'clock, clockwise."""

    ring: int
    sector: int
    sectors: int = 8

    @property
    def letter(self) -> str:
        return RING_LETTERS_OUTER_FIRST[max(0, min(4, 4 - self.ring))]

    @property
    def label(self) -> str:
        return f"{self.letter}{self.sector + 1}"

    def as_dict(self) -> dict:
        return {"ring": self.ring, "sector": self.sector, "label": self.label}


class ZoneMap:
    """Maps normalised coordinates to zones and back."""

    def __init__(self, center=(0.5, 0.5), ring_edges=(0.34, 0.5, 0.66, 0.82, 1.0), sectors=8, sector_offset_deg=0.0):
        self.center = (float(center[0]), float(center[1]))
        self.ring_edges = [float(v) for v in ring_edges]
        if len(self.ring_edges) != 5:
            raise ValueError("ring_edges must contain 5 values")
        self.sectors = int(sectors)
        self.sector_offset_deg = float(sector_offset_deg)

    @classmethod
    def from_config(cls, cfg: dict) -> "ZoneMap":
        return cls(
            center=cfg.get("center", (0.5, 0.5)),
            ring_edges=cfg.get("ring_edges", (0.34, 0.5, 0.66, 0.82, 1.0)),
            sectors=cfg.get("sectors", 8),
            sector_offset_deg=cfg.get("sector_offset_deg", 0.0),
        )

    # ---- geometry helpers -------------------------------------------------

    def radius_of(self, nx: float, ny: float) -> float:
        dx = (nx - self.center[0]) * 2.0
        dy = (ny - self.center[1]) * 2.0
        return math.hypot(dx, dy)

    def angle_deg(self, nx: float, ny: float) -> float:
        """0 deg at 12 o'clock, increasing clockwise."""
        dx = nx - self.center[0]
        dy = ny - self.center[1]
        return math.degrees(math.atan2(dx, -dy)) % 360.0

    def zone_of(self, nx: float, ny: float) -> Zone | None:
        radius = self.radius_of(nx, ny)
        if radius > self.ring_edges[-1]:
            return None
        ring = 0
        for index, edge in enumerate(self.ring_edges):
            if radius <= edge:
                ring = index
                break
        else:  # pragma: no cover
            ring = len(self.ring_edges) - 1
        sector_width = 360.0 / self.sectors
        angle = (self.angle_deg(nx, ny) - self.sector_offset_deg) % 360.0
        sector = int(angle // sector_width) % self.sectors
        return Zone(ring=ring, sector=sector, sectors=self.sectors)

    def point_of(self, ring: int, sector: int) -> tuple[float, float]:
        """Representative point (normalised) for a zone: mid-radius, sector centre."""
        ring = max(0, min(len(self.ring_edges) - 1, int(ring)))
        inner = self.ring_edges[ring - 1] if ring > 0 else 0.0
        outer = self.ring_edges[ring]
        radius = (inner + outer) / 2.0
        sector_width = 360.0 / self.sectors
        angle = math.radians((int(sector) + 0.5) * sector_width + self.sector_offset_deg)
        dx = radius * math.sin(angle)
        dy = -radius * math.cos(angle)
        return self.center[0] + dx / 2.0, self.center[1] + dy / 2.0

    def snap(self, nx: float, ny: float) -> tuple[float, float]:
        """Snap a raw touch to the centre of the zone it landed in."""
        zone = self.zone_of(nx, ny)
        if zone is None:
            # outside the panel: pull the point onto the outermost ring
            dx = nx - self.center[0]
            dy = ny - self.center[1]
            length = math.hypot(dx, dy) or 1.0
            scale = (self.ring_edges[-1] * 0.97 / (length * 2.0))
            return self.center[0] + dx * scale, self.center[1] + dy * scale
        return self.point_of(zone.ring, zone.sector)

    def describe(self) -> str:
        edges = ", ".join(f"{e:.2f}" for e in self.ring_edges)
        return f"ZoneMap(center={self.center}, ring_edges=[{edges}], sectors={self.sectors}, offset={self.sector_offset_deg})"
