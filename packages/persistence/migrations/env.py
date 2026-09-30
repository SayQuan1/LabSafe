"""Online-only migration environment with a schema-specific advisory lock."""

import hashlib

from alembic import context
from sqlalchemy import text

from packages.persistence.database import database_engine

if context.is_offline_mode():
    raise RuntimeError("Offline migration is disabled; use an explicit disposable database")

engine = database_engine()
with engine.connect() as connection:
    lock = hashlib.sha256(f"labsafe:migrate:{engine.url.database}".encode()).hexdigest()
    if connection.scalar(text("SELECT GET_LOCK(:name, 10)"), {"name": lock}) != 1:
        raise RuntimeError("Another migration owns the database lock")
    connection.commit()
    try:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()
        connection.commit()
    finally:
        connection.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": lock})
engine.dispose()
