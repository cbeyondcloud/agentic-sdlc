"""Money helpers. Amounts are integers in cents."""


def split_evenly(total_cents, parts):
    """Split an amount into `parts` shares that add up to the total."""
    share, remainder = divmod(total_cents, parts)
    return [share + 1] * remainder + [share] * (parts - remainder)
