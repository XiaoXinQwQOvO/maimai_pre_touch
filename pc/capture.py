"""Screen capture of the game window area: DXGI Desktop Duplication (dxcam) with a GDI fallback."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import threading
import time

import numpy as np

log = logging.getLogger(__name__)


class DxcamCapture:
    """Continuous DXGI capture of a screen region using dxcam (GPU, sub-millisecond)."""

    name = "dxcam"

    def __init__(self, region: tuple[int, int, int, int], fps: int = 60, output_idx: int = 0):
        import dxcam  # imported lazily so the fallback works without it

        self._dxcam = dxcam
        self._region = region
        self._fps = fps
        self._camera = None
        self._output_idx = output_idx
        self._start()

    def _start(self) -> None:
        left, top, width, height = self._region
        if width % 2:
            width -= 1
        if height % 2:
            height -= 1
        self._camera = self._dxcam.create(output_idx=self._output_idx, output_color="BGRA")
        self._camera.start(
            region=(left, top, left + width, top + height),
            target_fps=self._fps,
            video_mode=True,
        )
        log.info("dxcam started: region=%s fps=%s", (left, top, width, height), self._fps)

    def get_frame(self) -> np.ndarray | None:
        try:
            return self._camera.get_latest_frame()
        except Exception as exc:  # pragma: no cover - device lost etc.
            log.warning("dxcam grab failed: %s", exc)
            return None

    def update_region(self, region: tuple[int, int, int, int]) -> None:
        if region == self._region:
            return
        self._region = region
        self.close()
        self._start()

    def close(self) -> None:
        try:
            if self._camera is not None:
                self._camera.stop()
                self._camera.release()
        except Exception:
            pass
        self._camera = None


class GdiCapture:
    """BitBlt based capture - slower (~5-10 ms) but never fails on odd desktop setups."""

    name = "gdi"

    def __init__(self, region: tuple[int, int, int, int], fps: int = 60, **_kw):
        self._region = region
        self._fps = fps
        self._min_dt = 1.0 / max(1, fps)
        self._last = 0.0
        self._frame: np.ndarray | None = None
        user32 = ctypes.WinDLL("user32")
        gdi32 = ctypes.WinDLL("gdi32")
        self._user32 = user32
        self._gdi32 = gdi32
        user32.GetDC.restype = wt.HDC
        user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
        gdi32.CreateCompatibleDC.restype = wt.HDC
        gdi32.CreateCompatibleBitmap.restype = wt.HBITMAP
        gdi32.SelectObject.restype = wt.HGDIOBJ
        gdi32.GetDIBits.restype = ctypes.c_int

    def get_frame(self) -> np.ndarray | None:
        now = time.perf_counter()
        if now - self._last < self._min_dt:
            return self._frame
        self._last = now
        left, top, width, height = self._region
        if width <= 0 or height <= 0:
            return self._frame
        src_dc = self._user32.GetDC(None)
        mem_dc = self._gdi32.CreateCompatibleDC(src_dc)
        bmp = self._gdi32.CreateCompatibleBitmap(src_dc, width, height)
        old = self._gdi32.SelectObject(mem_dc, bmp)
        try:
            self._gdi32.BitBlt(mem_dc, 0, 0, width, height, src_dc, left, top, 0x00CC0020)
            buf = (ctypes.c_ubyte * (width * height * 4))()
            info = _BITMAPINFO()
            info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            info.bmiHeader.biWidth = width
            info.bmiHeader.biHeight = -height  # top-down
            info.bmiHeader.biPlanes = 1
            info.bmiHeader.biBitCount = 32
            info.bmiHeader.biCompression = 0  # BI_RGB
            self._gdi32.GetDIBits(mem_dc, bmp, 0, height, buf, ctypes.byref(info), 0)
            arr = np.frombuffer(buf, dtype=np.uint8).reshape(height, width, 4)
            self._frame = np.ascontiguousarray(arr[..., [2, 1, 0, 3]])  # BGRA
        finally:
            self._gdi32.SelectObject(mem_dc, old)
            self._gdi32.DeleteObject(bmp)
            self._gdi32.DeleteDC(mem_dc)
            self._user32.ReleaseDC(None, src_dc)
        return self._frame

    def update_region(self, region: tuple[int, int, int, int]) -> None:
        self._region = region

    def close(self) -> None:
        pass


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


class CaptureManager:
    """Keeps one capture source bound to the (possibly moving) game window region."""

    def __init__(self, cfg: dict, region_provider):
        self.cfg = cfg
        self._region_provider = region_provider
        self._lock = threading.Lock()
        self._backend = None
        self._region: tuple[int, int, int, int] | None = None
        self._last_check = 0.0
        self.frames = 0
        self.last_frame_time = 0.0

    def _make(self, region):
        backend = self.cfg.get("backend", "dxcam")
        fps = int(self.cfg.get("fps", 60))
        output_idx = int(self.cfg.get("output_idx", 0))
        if backend == "dxcam":
            try:
                return DxcamCapture(region, fps=fps, output_idx=output_idx)
            except Exception as exc:
                log.warning("dxcam unavailable (%s); falling back to GDI", exc)
        return GdiCapture(region, fps=fps)

    def get_frame(self) -> np.ndarray | None:
        now = time.perf_counter()
        with self._lock:
            if self._backend is None or now - self._last_check > 1.0:
                self._last_check = now
                region = self._region_provider()
                if region and region != self._region:
                    if self._backend is not None:
                        self._backend.close()
                        self._backend = None
                    self._region = region
                    self._backend = self._make(region)
            if self._backend is None:
                return None
            frame = self._backend.get_frame()
            if frame is not None:
                if frame.shape[1] % 2:
                    frame = frame[:, :-1]
                if frame.shape[0] % 2:
                    frame = frame[:-1]
                self.frames += 1
                self.last_frame_time = time.time()
            return frame

    def close(self) -> None:
        with self._lock:
            if self._backend is not None:
                self._backend.close()
                self._backend = None
