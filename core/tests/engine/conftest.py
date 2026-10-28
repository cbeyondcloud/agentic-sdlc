"""Engine test fixtures; the helpers live in _engine_support (a unique module name, so
they never collide with another folder's conftest)."""

from _engine_support import make_harness  # noqa: F401  (registers the fixture)
