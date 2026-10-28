"""File names for exported reports."""

import re


def safe_filename(title: str, ext: str = ".csv") -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", title).strip()
    return (name or "report") + ext
