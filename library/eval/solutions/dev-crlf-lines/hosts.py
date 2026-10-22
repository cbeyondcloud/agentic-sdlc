"""Parse a hosts file into {name: address}."""


def parse_hosts(text: str) -> dict:
    entries = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0]
        parts = line.split()
        if len(parts) >= 2:
            for name in parts[1:]:
                entries[name] = parts[0]
    return entries
