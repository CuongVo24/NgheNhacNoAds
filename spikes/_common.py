"""Shared helpers for the M0 spike scripts. Not part of the nghenhacnoads package."""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

OUT_DIR = Path(__file__).resolve().parent / "out"
USER_AGENT = "NgheNhacNoAds-spike/0.1 (+https://github.com/CuongVo24/NgheNhacNoAds)"


def setup_console() -> None:
    """Print UTF-8 so Vietnamese text shows up correctly in Windows consoles."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def out_path(*parts: str) -> Path:
    path = OUT_DIR.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def save_json(name: str, data: Any) -> Path:
    path = out_path(name)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


class Stopwatch:
    """Measures elapsed wall time in milliseconds."""

    def __init__(self) -> None:
        self._start = time.perf_counter()

    def ms(self) -> int:
        return round((time.perf_counter() - self._start) * 1000)


def median(values: list[int]) -> int | None:
    return round(statistics.median(values)) if values else None


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    """Render a Markdown table (for pasting results into docs/spikes)."""

    def cell(value: Any) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    return "\n".join(lines)
