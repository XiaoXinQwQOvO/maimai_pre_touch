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


def _fit_circle(xs: np.ndarray, ys: np.ndarray) -> tuple[float, float, float, float]:
    """Algebraic (Kasa) circle fit: returns ``cx, cy, r, rms``."""
    if len(xs) < 3:
        return float("nan"), float("nan"), 0.0, 0.0
    design = np.column_stack([xs, ys, np.ones_like(xs)])
    target = xs * xs + ys * ys
    try:
        solution, *_ = np.linalg.lstsq(design, target, rcond=None)
    except np.linalg.LinAlgError:
        return float("nan"), float("nan"), 0.0, 0.0
    cx = float(solution[0]) / 2.0
    cy = float(solution[1]) / 2.0
    radius = math.sqrt(max(1e-6, float(solution[2]) + cx * cx + cy * cy))
    distances = np.hypot(xs - cx, ys - cy)
    return cx, cy, radius, float(np.sqrt(np.mean((distances - radius) ** 2)))


def _fit_circle_ransac(
    xs: np.ndarray,
    ys: np.ndarray,
    tolerance: float = 4.0,
    iterations: int = 800,
    seed: int = 12345,
) -> tuple[float, float, float, np.ndarray] | None:
    """Circle through a point cloud that is only partly on the circle.

    The silhouette below the panel disc also contains the rest of the game's bright
    graphics, so a plain least squares fit is dragged away; sample three points, count
    inliers and refine the winner.
    """
    count = len(xs)
    if count < 20:
        return None
    rng = np.random.default_rng(seed)
    best: tuple[int, float, float, float] | None = None
    best_mask = np.zeros(count, dtype=bool)
    for _ in range(iterations):
        pick = rng.choice(count, size=3, replace=False)
        p0x, p0y = xs[pick[0]], ys[pick[0]]
        x1, y1 = xs[pick[1]] - p0x, ys[pick[1]] - p0y
        x2, y2 = xs[pick[2]] - p0x, ys[pick[2]] - p0y
        determinant = 2.0 * (x1 * y2 - x2 * y1)
        if abs(determinant) < 1e-6:
            continue
        ux = (y2 * (x1 * x1 + y1 * y1) - y1 * (x2 * x2 + y2 * y2)) / determinant
        uy = (x1 * (x2 * x2 + y2 * y2) - x2 * (x1 * x1 + y1 * y1)) / determinant
        cx, cy = p0x + ux, p0y + uy
        radius = math.hypot(p0x - cx, p0y - cy)
        if not (20.0 <= radius <= 400.0):
            continue
        distance = np.abs(np.hypot(xs - cx, ys - cy) - radius)
        inliers = distance < tolerance
        total = int(inliers.sum())
        if best is None or total > best[0]:
            best = (total, cx, cy, radius)
            best_mask = inliers
    if best is None or best[0] < 25:
        return None
    cx, cy, radius, _rms = _fit_circle(xs[best_mask], ys[best_mask])
    for _ in range(3):
        distance = np.abs(np.hypot(xs - cx, ys - cy) - radius)
        keep = distance < tolerance
        if keep.sum() < 25:
            break
        best_mask = keep
        cx, cy, radius, _rms = _fit_circle(xs[keep], ys[keep])
    return cx, cy, radius, best_mask


def _silhouette_arc(
    mask: np.ndarray,
    offset_x: int,
    offset_y: int,
    dark_level: float = 60.0,
    gray=None,
    frame_width: int = 0,
) -> tuple[float, float, float, float | None] | None:
    """Fit the panel disc's lower rim and measure its horizontal radius.

    The disc sits at the bottom of the picture, so its lower rim is the one boundary that
    reliably separates "inside the disc" from "background": the graphics drawn inside
    (sector lines, labels) never touch it, and the game's other bright furniture is
    trimmed away by the robust fit.

    Returns ``(cx, pole_y, curvature, rx)`` in full frame pixels.  ``pole_y`` is the
    bottom-most point of the rim, ``curvature`` the radius of the circle matching the rim
    there (the real radius for a round picture, ``rx^2/ry`` for a stretched one) and
    ``rx`` the measured horizontal radius, or ``None`` when no row of the disc borders on
    darkness (the usual case in game).
    """
    rows_count, cols_count = mask.shape
    xs: list[float] = []
    ys: list[float] = []
    for column in range(cols_count):
        rows = np.nonzero(mask[:, column])[0]
        if rows.size < 2:
            continue
        bottom = int(rows[-1])
        if bottom < rows_count * 0.42:  # the disc's lower rim is in the bottom half
            continue
        xs.append(float(column + offset_x))
        ys.append(float(bottom + offset_y))
    if len(xs) < 30:
        return None
    fit = _fit_circle_ransac(np.array(xs), np.array(ys))
    if fit is None:
        return None
    cx, cy, curvature, _inliers = fit
    if curvature <= 20.0:
        return None
    pole_y = cy + curvature

    # horizontal radius: the widest row whose rim borders on darkness (a crop edge only
    # counts when it is the frame's own edge), kept only if it is centred on the disc
    rx: float | None = None
    if gray is not None:
        centre_column = cx - offset_x
        best_half = 0.0
        for row in range(mask.shape[0]):
            columns = np.nonzero(mask[row, :])[0]
            if columns.size < 4:
                continue
            left = int(columns[0])
            right = int(columns[-1])
            left_inside = left - 3 >= 0
            right_inside = right + 3 < cols_count
            left_dark = left_inside and gray[row, left - 3] <= dark_level
            right_dark = right_inside and gray[row, right + 3] <= dark_level
            left_edge = not left_inside and left <= 1 and offset_x == 0
            right_edge = (
                not right_inside
                and right >= cols_count - 2
                and frame_width > 0
                and offset_x + cols_count >= frame_width
            )
            if not (left_dark or right_dark or left_edge or right_edge):
                continue
            if left_dark and right_dark:
                half = (right - left) / 2.0
                middle = (left + right) / 2.0
            elif right_dark or right_edge:
                half = right - centre_column
                middle = centre_column
            else:
                half = centre_column - left
                middle = centre_column
            if half <= 0.0 or abs(middle - centre_column) > 0.2 * curvature:
                continue  # a one sided span that is not centred on the disc
            best_half = max(best_half, half)
        if best_half > 0.0 and 0.5 * curvature <= best_half <= 2.0 * curvature:
            rx = best_half
    return cx, pole_y, curvature, rx


def detect_disc(
    frame_bgr: np.ndarray,
    roi: tuple[float, float, float, float] = (0.0, 0.24, 0.60, 1.0),
    threshold: float | None = None,
    min_coverage: float = 0.35,
) -> PanelCircle | None:
    """Find the big bright disc the game draws on its waiting screen.

    The disc's lower rim is segmented by brightness and a circle is fitted to it with a
    robust fit (the rest of the game's graphics in the same band would drag a plain least
    squares fit away).  The vertical radius comes from that fit; the horizontal one is
    measured when the disc's sides border on darkness, and a disagreement between the two
    is reported as picture stretching.
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
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8)).astype(bool)

    arc = _silhouette_arc(mask, x0, y0, gray=gray, frame_width=width)
    if arc is None:
        return None
    cx, pole_y, curvature, rx_measured = arc

    if rx_measured is not None and abs(rx_measured / curvature - 1.0) > 0.08:
        # the horizontal extent disagrees with the rim's curvature: the picture is
        # stretched, and for an ellipse ``curvature == rx^2 / ry`` at the bottom pole
        rx = rx_measured
        ry = rx * rx / curvature
    else:
        rx = rx_measured if rx_measured is not None else curvature
        ry = curvature
    stretch = rx / ry if ry else 0.0
    if not (0.6 <= stretch <= 1.7):
        rx, ry = curvature, curvature
        stretch = 1.0
    cy = pole_y - ry
    r = (rx + ry) / 2.0

    # plausibility gates: big, in the lower left, round, and reasonably well covered
    reasons = []
    if not (0.12 <= r / width <= 0.50):
        reasons.append(f"radius {r / width:.3f} of the frame")
    if not (0.55 <= cy / height <= 1.00):
        reasons.append(f"centre y {cy / height:.3f}")
    if not (0.05 <= cx / width <= 0.45):
        reasons.append(f"centre x {cx / width:.3f}")
    if reasons:
        LOG.debug("panel: rejected a circle candidate (%s)", ", ".join(reasons))
        return None

    # how much of the fitted rim really sits on the bright shape
    theta = np.linspace(0.0, 2.0 * math.pi, 360, endpoint=False)
    ring_x = np.rint(cx + rx * np.cos(theta) - x0).astype(int)
    ring_y = np.rint(cy + ry * np.sin(theta) - y0).astype(int)
    inside = (ring_x >= 0) & (ring_x < crop.shape[1]) & (ring_y >= 0) & (ring_y < crop.shape[0])
    if int(inside.sum()) < 60:
        return None
    coverage = float(mask[ring_y[inside], ring_x[inside]].mean())
    if coverage < min_coverage:
        LOG.debug("panel: rejected a circle candidate (coverage %.2f)", coverage)
        return None

    return PanelCircle(
        cx=cx / width,
        cy=cy / height,
        r=r / width,
        score=coverage,
        coverage=coverage,
        sharpness=min(1.0, coverage / min_coverage) if min_coverage else 1.0,
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
