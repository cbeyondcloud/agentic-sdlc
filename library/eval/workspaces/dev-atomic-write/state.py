"""Save the agent's state file safely."""

import json
import os


def save_state(path, data: dict) -> None:
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.rename(tmp, path)
