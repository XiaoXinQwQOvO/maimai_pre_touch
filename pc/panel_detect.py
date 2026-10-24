"""Find the touch-panel circle the game draws on screen.

The waiting screen draws the panel boundary as a circle; the phone overlay and the
touch mapping both use it, so it has to be detected rather than assumed to be the
whole client area.

Detection is a brute-force circular Hough on an edge mask (no external model, no
training): for every candidate centre on a coarse grid and every radius we measure
how much of the circumference lies on edges, and prefer radii where that coverage
peaks sharply against its neighbours - which is what makes a ring different from a
rectangle or a blob.

All results are normalised to the captured frame: ``cx, cy, r`` are fractions of the
frame width, so ``r = 0.5`` means the circle touches the frame edge.

    python pc/panel_detect.py --save frame.png      # capture and report candidates
    python pc/panel_detect.py --image frame.png     # analyse a saved PNG
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import imgio  # noqa: E402  (sibling module, needed for CJK-safe PNG io)

LOG = logging.getLogger("panel_detect")


class PanelCircle:
    """A detected panel circle, normalised to the frame width."""

    __slots__ = ("cx", "cy", "r", "score", "coverage", "sharpness", "rx", "ry")

    def __init__(
        self,
        cx: float,
        cy: float,
        r: float,
        score: float = 0.0,
        coverage: float = 0.0,
        sharpness: float = 0.0,
        rx: float | None = None,
        ry: float | None = None,
    ):
        self.cx = float(cx)
        self.cy = float(cy)
        self.r = float(r)
        self.score = float(score)
        self.coverage = float(coverage)
        self.sharpness = float(sharpness)
        # horizontal / vertical radii: equal for a true circle, different when the
        # picture is stretched (which is what the phone has to compensate for)
        self.rx = float(r if rx is None else rx)
        self.ry = float(r if ry is None else ry)

    @property
    def stretch(self) -> float:
        """rx/ry: 1.0 = no stretch, >1 = wider than tall."""
        return self.rx / self.ry if self.ry else 1.0

    def as_dict(self) -> dict:
        return {
            "cx": round(self.cx, 5),
            "cy": round(self.cy, 5),
            "r": round(self.r, 5),
            "rx": round(self.rx, 5),
            "ry": round(self.ry, 5),
            "stretch": round(self.stretch, 4),
            "score": round(self.score, 4),
            "coverage": round(self.coverage, 3),
            "sharpness": round(self.sharpness, 3),
        }

    def __repr__(self) -> str:
        return (
            f"PanelCircle(cx={self.cx:.4f}, cy={self.cy:.4f}, r={self.r:.4f}, "
            f"rx={self.rx:.4f}, ry={self.ry:.4f}, cov={self.coverage:.2f}, sharp={self.sharpness:.2f})"
        )

    def near(self, other: "PanelCircle", tolerance: float = 0.035) -> bool:
        return (
            abs(self.cx - other.cx) <= tolerance
            and abs(self.cy - other.cy) <= tolerance
            and abs(self.r - other.r) <= tolerance
        )

    def blended(self, other: "PanelCircle", weight: float = 0.35) -> "PanelCircle":
        """Smooth a new detection into the locked one (keeps the overlay steady)."""
        keep = 1.0 - weight
        return PanelCircle(
            self.cx * keep + other.cx * weight,
            self.cy * keep + other.cy * weight,
            self.r * keep + other.r * weight,
            other.score,
            other.coverage,
            other.sharpness,
            self.rx * keep + other.rx * weight,
            self.ry * keep + other.ry * weight,
        )


def edge_mask(gray: np.ndarray, low: int = 40, high: int = 120) -> np.ndarray:
    import cv2

    blurred = cv2.GaussianBlur(gray, (5, 5), 1.2)
    edges = cv2.Canny(blurred, low, high)
    # thicken a little so a ±1 px miss still counts
    return cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1) > 0


def _sample(mask: np.ndarray, ys: np.ndarray, xs: np.ndarray) -> np.ndarray:
    h, w = mask.shape
    yi = np.clip(np.rint(ys).astype(int), 0, h - 1)
    xi = np.clip(np.rint(xs).astype(int), 0, w - 1)
    return mask[yi, xi]


def _ellipse_from_mask(filled: np.ndarray) -> tuple[float, float, float, float, float, float] | None:
    """Fit an ellipse to the outline of a filled shape.

    Returns ``(cx, cy, rx, ry, angle_deg, inlier_ratio)``; the radii are already
    mapped to screen axes when the ellipse is (nearly) axis aligned.  Points that do
    not sit on the ellipse - frame clips, the neighbouring picture element the shape
    merges with - are trimmed away and the fit is repeated.
    """
    import cv2

    contours, _ = cv2.findContours(filled, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    points = contour.reshape(-1, 2).astype(np.float64)
    if len(points) < 20:
        return None

    total = len(points)
    ellipse = cv2.fitEllipse(points.reshape(-1, 1, 2).astype(np.float32))
    inliers = points
    for _ in range(4):
        (ex, ey), (d1, d2), angle = ellipse
        if d1 <= 1.0 or d2 <= 1.0:
            return None
        radians = math.radians(angle)
        cos_a, sin_a = math.cos(radians), math.sin(radians)
        dx = inliers[:, 0] - ex
        dy = inliers[:, 1] - ey
        u = dx * cos_a + dy * sin_a
        v = -dx * sin_a + dy * cos_a
        distance = (u / (d1 / 2.0)) ** 2 + (v / (d2 / 2.0)) ** 2
        keep = np.abs(distance - 1.0) < 0.35
        if keep.sum() < 20:
            break
        inliers = inliers[keep]
        ellipse = cv2.fitEllipse(inliers.reshape(-1, 1, 2).astype(np.float32))

    (ex, ey), (d1, d2), angle = ellipse
    ratio = angle % 180.0
    if min(ratio, 180.0 - ratio) <= 15.0:  # the d1 axis is horizontal
        rx, ry = d1 / 2.0, d2 / 2.0
    elif abs(ratio - 90.0) <= 15.0:  # the d1 axis is vertical
        rx, ry = d2 / 2.0, d1 / 2.0
    else:
        # tilted ellipse: the touch mapping only knows screen axes, so keep the area
        rx = ry = (d1 + d2) / 4.0
    return ex, ey, rx, ry, angle, len(inliers) / total


def detect_disc(
    frame_bgr: np.ndarray,
    roi: tuple[float, float, float, float] = (0.0, 0.30, 0.55, 1.0),
    threshold: float | None = None,
    min_area_frac: float = 0.02,
) -> PanelCircle | None:
    """Find the big bright disc/ring the game draws (the waiting-screen panel circle).

    The shape is segmented by brightness, its holes are filled and an ellipse is fitted
    to the outline, which gives the centre and the two radii even when the picture is
    stretched.  Plausibility gates then reject convincing looking false positives.
    """
    import cv2

    height, width = frame_bgr.shape[:2]
    x0, y0 = int(roi[0] * width), int(roi[1] * height)
    x1, y1 = int(roi[2] * width), int(roi[3] * height)
    crop = frame_bgr[y0:y1, x0:x1]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)

    if threshold is None:
        otsu, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        threshold = max(float(otsu), 90.0)
    mask = (gray > threshold).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if count <= 1:
        return None
    best_index = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    if stats[best_index, cv2.CC_STAT_AREA] < min_area_frac * crop.size:
        return None
    component = (labels == best_index).astype(np.uint8)

    # fill the holes (dark graphics drawn inside the circle) so the fit follows the rim
    contours, _ = cv2.findContours(component, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    filled = np.zeros_like(component)
    cv2.drawContours(filled, contours, -1, 1, thickness=-1)

    fit = _ellipse_from_mask(filled)
    if fit is None:
        return None
    cx, cy, rx, ry, _angle, inlier_ratio = fit
    r = (rx + ry) / 2.0
    if r <= 8.0:
        return None

    theta = np.linspace(0.0, 2.0 * math.pi, 360, endpoint=False)
    ring_x = np.rint(cx + rx * np.cos(theta)).astype(int)
    ring_y = np.rint(cy + ry * np.sin(theta)).astype(int)
    inside = (ring_x >= 0) & (ring_x < crop.shape[1]) & (ring_y >= 0) & (ring_y < crop.shape[0])
    if int(inside.sum()) < 60:
        return None
    coverage = float(filled[ring_y[inside], ring_x[inside]].mean())

    # plausibility gates: the panel circle is a big, round, well covered shape.
    # Without them a busy desktop produces convincing looking false positives.
    area = float(filled.sum())
    filled_area = math.pi * rx * ry
    area_ratio = area / filled_area if filled_area > 0 else 0.0
    stretch = rx / ry if ry else 0.0
    reasons = []
    if coverage < 0.40:
        reasons.append(f"coverage {coverage:.2f}")
    if not (0.12 <= r / width <= 0.65):
        reasons.append(f"radius {r / width:.3f} of the frame")
    if not (0.6 <= stretch <= 1.7):
        reasons.append(f"stretch {stretch:.2f}")
    if not (0.6 <= area_ratio <= 1.6):
        reasons.append(f"area ratio {area_ratio:.2f}")
    if reasons:
        LOG.debug("panel: rejected a circle candidate (%s)", ", ".join(reasons))
        return None

    return PanelCircle(
        cx=(cx + x0) / width,
        cy=(cy + y0) / height,
        r=r / width,
        score=coverage * 0.6 + min(1.0, inlier_ratio) * 0.4,
        coverage=coverage,
        sharpness=inlier_ratio,
        rx=rx / width,
        ry=ry / height,
    )


def screen_is_dark(frame_bgr: np.ndarray, threshold: float = 24.0) -> bool:
    """True while the game is still on its boot/black screen."""
    import cv2

    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    return float(gray.mean()) < threshold


class PanelTracker:
    """Waits out the black screen, detects the panel circle, then keeps it locked.

    States: ``dark`` (boot screen), ``searching`` (picture is up, circle not confirmed
    yet), ``locked`` (circle fixed; the phone overlay and the touch mapping use it),
    ``lost`` (was locked, circle disappeared - e.g. the game changed screens).
    """

    def __init__(
        self,
        dark_threshold: float = 24.0,
        lock_frames: int = 3,
        tolerance: float = 0.04,
        verify_interval: float = 2.0,
        max_misses: int = 4,
        roi: tuple[float, float, float, float] | None = None,
        log=None,
    ):
        self.dark_threshold = dark_threshold
        self.lock_frames = lock_frames
        self.tolerance = tolerance
        self.verify_interval = verify_interval
        self.max_misses = max_misses
        # where the panel circle lives in the picture (default: lower left corner)
        self.roi = tuple(roi) if roi else (0.0, 0.30, 0.55, 1.0)
        self.state = "dark"
        self.circle: PanelCircle | None = None
        self._pending: PanelCircle | None = None
        self._confirmations = 0
        self._misses = 0
        self._last_verify = 0.0
        self._log = log
        self.updates = 0
        self.locks = 0

    def _say(self, level: str, message: str, *args) -> None:
        if self._log is not None:
            getattr(self._log, level)("panel: " + message, *args)

    def reset(self) -> None:
        self.state = "dark"
        self.circle = None
        self._pending = None
        self._confirmations = 0
        self._misses = 0
        self._last_verify = 0.0

    def update(self, frame_bgr: np.ndarray, now: float | None = None) -> dict:
        import time

        now = time.time() if now is None else now
        self.updates += 1
        if screen_is_dark(frame_bgr, self.dark_threshold):
            if self.circle is not None:
                self._say("info", "screen went dark, waiting for the picture to come back")
            self.state = "dark"
            self.circle = None
            self._pending = None
            self._confirmations = 0
            self._misses = 0
            return self.status()

        detected = detect_disc(frame_bgr, roi=self.roi)

        if self.circle is None:
            if detected is None:
                self.state = "searching"
                self._confirmations = 0
                return self.status()
            if self._pending is not None and self._pending.near(detected, self.tolerance):
                self._pending = self._pending.blended(detected, 0.5)
                self._confirmations += 1
            else:
                self._pending = detected
                self._confirmations = 1
            if self._confirmations >= self.lock_frames:
                self.circle = self._pending
                self.state = "locked"
                self.locks += 1
                self._last_verify = now
                self._say(
                    "info",
                    "locked panel circle cx=%.4f cy=%.4f r=%.4f (stretch %.3f, score %.2f)",
                    self.circle.cx,
                    self.circle.cy,
                    self.circle.r,
                    self.circle.stretch,
                    self.circle.score,
                )
                return self.status()
            self.state = "searching"
            return self.status()

        # locked: re-check now and then so a re-layout / re-scale is picked up
        if now - self._last_verify >= self.verify_interval:
            self._last_verify = now
            if detected is not None and detected.near(self.circle, self.tolerance):
                self.circle = self.circle.blended(detected, 0.25)
                self._misses = 0
                self.state = "locked"
            else:
                self._misses += 1
                if self._misses >= self.max_misses:
                    self._say("warning", "panel circle lost, searching again")
                    self.circle = None
                    self._pending = None
                    self._confirmations = 0
                    self._misses = 0
                    self.state = "lost"
        return self.status()

    def status(self) -> dict:
        return {
            "state": self.state,
            "locked": self.circle is not None,
            "updates": self.updates,
            "locks": self.locks,
            "circle": self.circle.as_dict() if self.circle else None,
        }

    def panel(self) -> dict | None:
        """Geometry for the touch mapping / phone overlay."""
        if self.circle is None:
            return None
        return {
            "cx": self.circle.cx,
            "cy": self.circle.cy,
            "rx": self.circle.rx,
            "ry": self.circle.ry,
            "r": self.circle.r,
            "stretch": self.circle.stretch,
        }


def detect_panel(
    frame_bgr: np.ndarray,
    roi: tuple[float, float, float, float] | None = None,
    r_range: tuple[float, float] = (0.06, 0.62),
    centres: int = 17,
    radii: int = 26,
    angles: int = 240,
    scale: float = 0.5,
    top: int = 8,
) -> list[PanelCircle]:
    """Best panel-circle candidates, best first (normalised to the frame width)."""
    candidates: list[PanelCircle] = []
    disc = detect_disc(frame_bgr)
    if disc is not None:
        candidates.append(disc)
    return candidates + detect_ring(
        frame_bgr, roi=roi, r_range=r_range, centres=centres, radii=radii, angles=angles, scale=scale, top=top
    )


def detect_ring(
    frame_bgr: np.ndarray,
    roi: tuple[float, float, float, float] | None = None,
    r_range: tuple[float, float] = (0.06, 0.62),
    centres: int = 17,
    radii: int = 26,
    angles: int = 240,
    scale: float = 0.5,
    top: int = 8,
) -> list[PanelCircle]:
    """Edge-Hough candidates for a thin ring (fallback when the shape is not a disc)."""
    import cv2

    height, width = frame_bgr.shape[:2]
    x0, y0, x1, y1 = (
        (int(roi[0] * width), int(roi[1] * height), int(roi[2] * width), int(roi[3] * height))
        if roi
        else (0, 0, width, height)
    )
    crop = frame_bgr[y0:y1, x0:x1]
    if scale != 1.0:
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    mask = edge_mask(gray)
    ch, cw = mask.shape

    theta = np.linspace(0.0, 2.0 * math.pi, angles, endpoint=False)
    cos_t, sin_t = np.cos(theta), np.sin(theta)

    # candidate centres: inside the ROI (a panel circle must be fully drawable)
    gx = np.linspace(cw * 0.12, cw * 0.88, centres)
    gy = np.linspace(ch * 0.12, ch * 0.88, centres)
    cx_grid, cy_grid = np.meshgrid(gx, gy)
    cx_flat, cy_flat = cx_grid.ravel(), cy_grid.ravel()

    # radii in crop pixels, converted from frame-normalised fractions
    r_lo = r_range[0] * width * scale
    r_hi = r_range[1] * width * scale
    r_list = np.linspace(r_lo, r_hi, radii)

    coverage = np.zeros((radii, cx_flat.size), dtype=np.float32)
    for i, radius in enumerate(r_list):
        xs = cx_flat[:, None] + radius * cos_t[None, :]
        ys = cy_flat[:, None] + radius * sin_t[None, :]
        coverage[i] = _sample(mask, ys, xs).mean(axis=1)

    best: list[PanelCircle] = []
    xs_axis, ys_axis = np.nonzero(np.ones_like(coverage))
    for idx in range(radii):
        inner = coverage[idx - 1] if idx > 0 else coverage[idx]
        outer = coverage[idx + 1] if idx + 1 < radii else coverage[idx]
        sharp = coverage[idx] - np.maximum(inner, outer)
        score = coverage[idx] + 1.5 * sharp
        k = int(np.argmax(score))
        best.append(
            PanelCircle(
                cx=(cx_flat[k] / scale + x0) / width,
                cy=(cy_flat[k] / scale + y0) / height,
                r=r_list[idx] / scale / width,
                score=float(score[k]),
                coverage=float(coverage[idx][k]),
                sharpness=float(sharp[k]),
            )
        )
    best.sort(key=lambda c: -c.score)

    # drop near-duplicates
    unique: list[PanelCircle] = []
    for candidate in best:
        if all(
            abs(candidate.cx - other.cx) > 0.02 or abs(candidate.cy - other.cy) > 0.02 or abs(candidate.r - other.r) > 0.02
            for other in unique
        ):
            unique.append(candidate)
        if len(unique) >= top:
            break
    return unique


def frame_from_window(save_path: str | None = None, raise_window: bool = True) -> np.ndarray | None:
    """Grab the game client area (one frame) with dxcam."""
    import time

    import dxcam

    import imgio
    import window

    window.set_dpi_aware()
    game = window.find_game_window()
    if not game:
        print("[!] game window not found")
        return None
    if raise_window:
        # the desktop is captured as composited, so anything on top would be seen
        window.bring_to_front(game)
        time.sleep(0.5)
    left, top, width, height = window.client_rect(game)
    camera = dxcam.create(output_idx=0, output_color="BGR")
    frame = None
    for _ in range(50):
        frame = camera.grab(region=(left, top, left + width, top + height))
        if frame is not None:
            break
        time.sleep(0.05)
    camera.release()
    if frame is None:
        return None
    frame = np.asarray(frame)
    if save_path:
        import cv2

        os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
        imgio.imwrite(save_path, frame)
        print(f"[i] frame saved to {save_path}")
    return frame


def _track(seconds: float, interval: float, preview: str) -> int:
    """Live loop: watch the black screen end, lock the circle, keep verifying."""
    import time

    import cv2
    import dxcam

    import window

    window.set_dpi_aware()
    game = window.find_game_window()
    if not game:
        print("[!] no game window")
        return 1
    window.bring_to_front(game)
    time.sleep(0.5)
    left, top, width, height = window.client_rect(game)
    print(f"[i] tracking client rect {left},{top},{width},{height} for {seconds:.0f}s")

    camera = dxcam.create(output_idx=0, output_color="BGR")
    tracker = PanelTracker(log=_PrintLogger())
    deadline = time.time() + seconds
    last_state = None
    saved = False
    while time.time() < deadline:
        frame = camera.grab(region=(left, top, left + width, top + height))
        if frame is None:
            time.sleep(0.05)
            continue
        frame = np.asarray(frame)
        status = tracker.update(frame)
        if status["state"] != last_state:
            last_state = status["state"]
            circle = status["circle"]
            print(
                f"[{time.strftime('%H:%M:%S')}] state={status['state']:<9} "
                + (f"cx={circle['cx']:.4f} cy={circle['cy']:.4f} rx={circle['rx']:.4f} "
                   f"ry={circle['ry']:.4f} stretch={circle['stretch']:.3f} score={circle['score']:.2f}"
                   if circle else "no circle")
            )
        if preview and status["circle"] and not saved:
            annotated = frame.copy()
            px, py = int(status["circle"]["cx"] * width), int(status["circle"]["cy"] * height)
            prx, pry = int(status["circle"]["rx"] * width), int(status["circle"]["ry"] * height)
            cv2.ellipse(annotated, (px, py), (prx, pry), 0, 0, 360, (0, 0, 255), 4)
            cv2.drawMarker(annotated, (px, py), (0, 255, 255), cv2.MARKER_CROSS, 60, 3)
            imgio.imwrite(preview, annotated)
            imgio.imwrite(preview.replace(".png", "_raw.png"), frame)
            print(f"[i] preview -> {preview}")
            saved = True
        time.sleep(interval)
    camera.release()
    print(f"[i] final: {tracker.status()}")
    return 0


class _PrintLogger:
    def info(self, message, *args):
        print("[i] " + (message % args if args else message))

    def warning(self, message, *args):
        print("[!] " + (message % args if args else message))

    def debug(self, message, *args):
        pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", help="analyse a saved PNG instead of capturing")
    parser.add_argument("--save", help="capture and save the frame here")
    parser.add_argument("--roi", default="", help="x0,y0,x1,y1 as 0..1 fractions")
    parser.add_argument("--full", action="store_true", help="search the whole frame, not just the bottom-left")
    parser.add_argument("--r-min", type=float, default=0.06)
    parser.add_argument("--r-max", type=float, default=0.62)
    parser.add_argument("--preview", default="", help="write an annotated PNG here so a human can check")
    parser.add_argument("--track", type=float, default=0.0, help="live tracking for N seconds instead of one shot")
    parser.add_argument("--interval", type=float, default=0.5, help="tracking interval")
    args = parser.parse_args(argv)

    import cv2

    if args.track > 0:
        return _track(args.track, args.interval, args.preview)

    if args.image:
        frame = imgio.imread(args.image)
        if frame is None:
            print(f"[!] cannot read {args.image}")
            return 1
    else:
        frame = frame_from_window(args.save)
        if frame is None:
            print("[!] no frame")
            return 1

    roi = None
    if args.roi:
        roi = tuple(float(v) for v in args.roi.split(","))
        if len(roi) != 4:
            raise SystemExit("--roi needs x0,y0,x1,y1")
    elif not args.full:
        roi = (0.0, 0.50, 0.55, 1.0)  # bottom-left area

    height, width = frame.shape[:2]
    print(f"[i] frame {width}x{height} mean={frame.mean():.1f} roi={roi or '(full)'}")
    candidates = detect_panel(frame, roi=roi, r_range=(args.r_min, args.r_max))
    if not candidates:
        print("[i] no candidate circles")
        return 0
    colours = [(0, 0, 255), (0, 200, 0), (255, 0, 0), (0, 200, 255), (255, 0, 255), (255, 255, 0), (128, 0, 255), (0, 128, 255)]
    for i, candidate in enumerate(candidates):
        px_cx, px_cy = int(candidate.cx * width), int(candidate.cy * height)
        px_rx, px_ry = int(candidate.rx * width), int(candidate.ry * height)
        print(
            f"  {i}. cx={candidate.cx:.4f} cy={candidate.cy:.4f} r={candidate.r:.4f} "
            f"rx={candidate.rx:.4f} ry={candidate.ry:.4f} stretch={candidate.stretch:.3f}  "
            f"px=({px_cx},{px_cy}) rx={px_rx} ry={px_ry}  cov={candidate.coverage:.2f} "
            f"sharp={candidate.sharpness:.2f} score={candidate.score:.2f}"
        )
        if args.preview:
            colour = colours[i % len(colours)]
            cv2.ellipse(frame, (px_cx, px_cy), (px_rx, px_ry), 0, 0, 360, colour, 3)
            cv2.drawMarker(frame, (px_cx, px_cy), colour, cv2.MARKER_CROSS, 40, 2)
            cv2.putText(
                frame, f"#{i} r={candidate.r:.3f}", (px_cx - 40, max(20, px_cy - px_ry - 8)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, colour, 2, cv2.LINE_AA,
            )
    if args.preview:
        os.makedirs(os.path.dirname(os.path.abspath(args.preview)), exist_ok=True)
        imgio.imwrite(args.preview, frame)
        print(f"[i] preview -> {args.preview}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
