"""Text helpers for URLs."""

import re
import unicodedata


def slugify(title):
    """'Hello, World!' -> 'hello-world'."""
    plain = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    words = re.findall(r"[a-z0-9]+", plain.lower())
    return "-".join(words)
