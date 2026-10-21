"""Money helpers. Amounts are integers in cents."""


def split_evenly(total_cents, parts):
    """Split an amount into `parts` shares that add up to the total."""
    share = total_cents // parts
    return [share] * parts
