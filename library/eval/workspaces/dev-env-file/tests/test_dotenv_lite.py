from dotenv_lite import parse_env


def test_simple_values_and_comments():
    assert parse_env("# local\nDEBUG=1\n\nNAME = app\n") == {"DEBUG": "1", "NAME": "app"}


def test_values_may_contain_equals():
    text = "DATABASE_URL=postgres://u:p@h/db?sslmode=require\n"
    assert parse_env(text) == {"DATABASE_URL": "postgres://u:p@h/db?sslmode=require"}


def test_quotes_are_removed():
    assert parse_env('GREETING="hello world"\nPATH_HINT=\'C:\\\\tools\'\n') == {
        "GREETING": "hello world",
        "PATH_HINT": "C:\\\\tools",
    }
