"""Configuration handling: loads pc/config.json, applies defaults, allows overrides."""
from __future__ import annotations

import copy
import json
import os
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "http": {"host": "0.0.0.0", "port": 8770},
    "capture": {
        "backend": "dxcam",
        "output_idx": 0,
        "fps": 60,
        "follow_window": True,
        "process_names": ["maimai.exe", "maimai_dump_.exe"],
        "window_class_substr": "teaGfx",
        "region": None,
    },
    "video": {"encoder": "auto", "bitrate_kbps": 9000, "fps": 60, "scale": 1.0},
    "touch": {
        "backend": "maitouch",
        "process_names": ["maimai_dump_.exe", "maimai.exe"],
        "max_contacts": 10,
    },
    "debug": {"log_level": "INFO", "save_frames": False},
}

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load(path: str | None = None) -> dict[str, Any]:
    path = path or CONFIG_PATH
    if not os.path.exists(path):
        save(DEFAULT_CONFIG, path)
        return copy.deepcopy(DEFAULT_CONFIG)
    with open(path, "r", encoding="utf-8-sig") as fh:
        user = json.load(fh)
    return _deep_merge(DEFAULT_CONFIG, user)


def save(cfg: dict[str, Any], path: str | None = None) -> None:
    path = path or CONFIG_PATH
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
