"""A sliding-window limiter for login attempts."""


class Limiter:
    def __init__(self, limit: int = 5, window: float = 60.0):
        self.limit, self.window = limit, window
        self.attempts: dict = {}

    def allow(self, key: str, now: float) -> bool:
        recent = [t for t in self.attempts.get(key, []) if t > now - self.window]
        if len(recent) >= self.limit:
            self.attempts[key] = recent
            return False
        self.attempts[key] = recent + [now]
        return True
