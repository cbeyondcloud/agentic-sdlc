"""File names for exported reports."""

import re

RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_filename(title: str, ext: str = ".csv") -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip().rstrip(". ")
    if name.split(".")[0].upper() in RESERVED:
        name = "_" + name
    return (name or "report") + ext
