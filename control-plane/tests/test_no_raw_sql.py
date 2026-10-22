"""No string-built SQL outside the database layer (Review Focus 6): with the tenant in a GUC,
injected SQL could call `set_config`, so raw SQL stays in `db.py`, `db_roles.py`,
`tenancy/` and `migrations/`, where every statement is reviewed as such."""

import ast
from pathlib import Path

import agentic_cp

PACKAGE = Path(agentic_cp.__file__).resolve().parent
ALLOWED = ("db.py", "db_roles.py", "tenancy/", "migrations/")


def raw_sql_calls(source: str) -> list[int]:
    """Line numbers of `text(...)`, `sqlalchemy.text(...)`, `.exec_driver_sql(...)` and
    `.execute("<literal>")` calls in `source`."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "text":
            found.append(node.lineno)
        elif isinstance(func, ast.Attribute) and func.attr in ("text", "exec_driver_sql"):
            found.append(node.lineno)
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "execute"
            and node.args
            and isinstance(node.args[0], (ast.Constant, ast.JoinedStr, ast.BinOp))
        ):
            found.append(node.lineno)
    return found


def test_the_scanner_finds_each_form():
    source = (
        "from sqlalchemy import text\n"
        "text('select 1')\n"
        "sa.text('x')\n"
        "conn.exec_driver_sql('x')\n"
        "conn.execute(f'select {x}')\n"
        "conn.execute(select(t))\n"
    )
    assert raw_sql_calls(source) == [2, 3, 4, 5]


def test_no_raw_sql_outside_db_layer():
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        if rel in ALLOWED or rel.startswith(tuple(a for a in ALLOWED if a.endswith("/"))):
            continue
        for line in raw_sql_calls(path.read_text(encoding="utf-8")):
            offenders.append(f"{rel}:{line}")
    assert offenders == [], "raw SQL outside the database layer: " + ", ".join(offenders)
