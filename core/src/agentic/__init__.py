"""Core library: definitions, policy, engine, agents."""

import logging
from importlib.metadata import version

#: From the package metadata: the release's one version source is the root pyproject
#: (scripts/agentic_release/version.py), never a second literal here.
__version__ = version("agentic-core")

# A library logs; the application decides where it goes. Without a handler, Python's
# last-resort handler would print the engine's warnings to stderr unredacted (DF-175).
logging.getLogger("agentic").addHandler(logging.NullHandler())
