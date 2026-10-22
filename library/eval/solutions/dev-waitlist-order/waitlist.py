"""Course waitlists. Entries are (joined_at, student)."""


def next_student(waitlist: list):
    if not waitlist:
        return None
    return min(waitlist)[1]
