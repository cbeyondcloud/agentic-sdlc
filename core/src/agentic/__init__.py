"""Core library: definitions, policy, engine, agents."""

import logging

__version__ = "0.1.0"

# A library logs; the application decides where it goes. Without a handler, Python's
# last-resort handler would print the engine's warnings to stderr unredacted (DF-175).
logging.getLogger("agentic").addHandler(logging.NullHandler())
