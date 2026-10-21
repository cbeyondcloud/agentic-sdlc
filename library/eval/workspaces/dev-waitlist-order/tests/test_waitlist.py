from waitlist import next_student


def test_empty_waitlist():
    assert next_student([]) is None


def test_first_come_first_served():
    assert next_student([(30, "cy"), (10, "ana"), (20, "ben")]) == "ana"
