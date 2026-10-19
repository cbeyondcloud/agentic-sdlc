"""A stand-in for `srt` in unit tests: records its arguments, checks the settings file,
then runs the command it was given (without any sandbox).

    python fake_srt.py --settings <file> -- bash -c "<command>"
"""

import json
import subprocess
import sys
from pathlib import Path

REQUIRED = {
    "network": {"allowedDomains", "deniedDomains"},
    "filesystem": {"denyRead", "allowRead", "allowWrite", "denyWrite"},
}

args = sys.argv[1:]
Path("argv.json").write_text(json.dumps(args), encoding="utf-8")
settings = json.loads(Path(args[1]).read_text(encoding="utf-8"))
for section, keys in REQUIRED.items():
    if set(settings.get(section, {})) != keys:
        print(f"fake srt: settings.{section} must hold exactly {sorted(keys)}", file=sys.stderr)
        sys.exit(90)
sys.exit(subprocess.run(args[-1], shell=True).returncode)
