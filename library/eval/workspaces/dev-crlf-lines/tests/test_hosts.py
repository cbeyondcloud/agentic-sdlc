from hosts import parse_hosts


def test_lf_file():
    text = "127.0.0.1 localhost\n10.0.0.5 db db.internal # primary\n"
    assert parse_hosts(text) == {"localhost": "127.0.0.1", "db": "10.0.0.5", "db.internal": "10.0.0.5"}


def test_crlf_file():
    text = "127.0.0.1 localhost\r\n10.0.0.5 db\r\n"
    assert parse_hosts(text) == {"localhost": "127.0.0.1", "db": "10.0.0.5"}


def test_tabs_separate_fields():
    assert parse_hosts("10.0.0.7\tcache\n") == {"cache": "10.0.0.7"}
