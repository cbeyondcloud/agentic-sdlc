"""A sliding-window limiter for login attempts."""


class Limiter:
    def __init__(self, limit: int = 5, window: float = 60.0):
        self.limit, self.window = limit, window
        self.attempts: dict = {}

    def allow(self, key: str, now: float) -> bool:
        times = self.attempts.setdefault(key, [])
        recent = [t for t in times if t >= now - self.window]
        if len(recent) >= self.limit:
            return False
        times.append(now)
        return True
