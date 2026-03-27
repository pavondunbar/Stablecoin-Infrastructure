"""
migrations/env.py — Alembic migration environment.

Connects to the DATABASE_URL from the environment and uses the shared
SQLAlchemy Base so Alembic can auto-generate migrations from model changes.

Usage:
    # Generate a new migration after editing shared/models.py:
    alembic revision --autogenerate -m "add xyz column"

    # Apply all pending migrations:
    alembic upgrade head

    # Roll back one step:
    alembic downgrade -1
"""

import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context

# ── Add the project root to sys.path so shared/ is importable ─────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))

from shared.models import Base   # noqa: E402 — must come after path setup

# Alembic Config object (gives access to values in alembic.ini)
config = context.config

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Provide the SQLAlchemy metadata object for autogenerate support
target_metadata = Base.metadata

# Override sqlalchemy.url from the DATABASE_URL environment variable
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL:
    config.set_main_option("sqlalchemy.url", DATABASE_URL)


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode: emit SQL to stdout rather than
    connecting to the database.  Useful for generating SQL scripts.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode: connect to the target database and
    apply changes directly.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            # Include custom PostgreSQL enum types in autogenerate
            include_schemas=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
