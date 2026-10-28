"""Agent runtime: runners, metering, tools, children and stuck detection (SP4)."""

import os

# Pydantic AI advertises its hosted observability product on first use; the
# framework sends no telemetry anywhere, so the banner is off (spike S1).
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
