"""Alembic environment: runs on the connection `agentic_cp.db.migrate_async` hands over.

The connection's `search_path` is already the target schema, and the version table lives
in that schema too, so one database can hold several deployments' schemas."""

from alembic import context

from agentic_cp.tables import metadata

config = context.config
connection = config.attributes.get("connection")
if connection is None:
    raise RuntimeError(
        "run migrations with `python -m agentic_cp migrate` (agentic_cp.db.migrate), "
        "not the alembic command line"
    )

context.configure(
    connection=connection,
    target_metadata=metadata,
    version_table_schema=config.attributes["schema"],
    transaction_per_migration=True,
)
with context.begin_transaction():
    context.run_migrations()
