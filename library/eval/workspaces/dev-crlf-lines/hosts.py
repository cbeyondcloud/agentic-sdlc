"""Parse a hosts file into {name: address}."""


def parse_hosts(text: str) -> dict:
    entries = {}
    for line in text.split("\n"):
        line = line.split("#", 1)[0]
        parts = line.split(" ")
        if len(parts) >= 2 and parts[0]:
            for name in parts[1:]:
                if name:
                    entries[name] = parts[0]
    return entries
