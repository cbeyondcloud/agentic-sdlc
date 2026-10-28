"""Text helpers for URLs."""

import re


def slugify(title):
    """'Hello, World!' -> 'hello-world'."""
    words = re.findall(r"[a-z0-9]+", title.lower())
    return "-".join(words)
