"""Retry schedule for webhook delivery."""


def retry_delays(attempts: int, base: float = 1.0, max_delay: float = 60.0) -> list:
    """Seconds to wait before each retry."""
    return [min(max_delay, base * 2**n) for n in range(attempts)]
