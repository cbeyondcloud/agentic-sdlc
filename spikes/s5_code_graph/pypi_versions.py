"""List PyPI releases (version, upload date, yanked) on or before a cutoff date."""

import json
import sys
import urllib.request

CUTOFF = "2026-10-06"


def main() -> None:
    for pkg in sys.argv[1:]:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/json") as r:
            data = json.load(r)
        rows = []
        for ver, files in data["releases"].items():
            if not files:
                continue
            ts = min(f["upload_time_iso_8601"] for f in files)
            yanked = any(f.get("yanked") for f in files)
            rows.append((ts, ver, yanked))
        rows.sort()
        print(f"== {pkg} (licence: {data['info'].get('license')!r}; latest {data['info']['version']})")
        for ts, ver, yanked in rows[-40:]:
            ok = "OK " if ts[:10] <= CUTOFF else "NEW"
            print(f"  {ok} {ver:12} {ts[:10]} {'YANKED' if yanked else ''}")


main()
