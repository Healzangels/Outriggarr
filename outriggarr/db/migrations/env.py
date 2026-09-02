from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, event, pool

from outriggarr.db.models import Base
from outriggarr.settings import Settings

config = context.config
# The app sets sqlalchemy.url programmatically; the alembic CLI takes it from the environment.
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", Settings.from_env().database_url)
# The alembic CLI gets logging from alembic.ini. In-process migrations (app startup,
# tests) must not touch logging: fileConfig would reset the root logger's level and
# handlers and silence the worker's and uvicorn's output.
if config.config_file_name is not None and config.attributes.get("configure_logging", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    if connectable.dialect.name == "sqlite":
        # pysqlite silently COMMITs before any DDL unless it is told not to manage
        # transactions; with that off and an explicit BEGIN, DDL really rolls back.
        @event.listens_for(connectable, "connect")
        def _no_implicit_commits(dbapi_connection, _record):  # noqa: ANN001
            dbapi_connection.isolation_level = None

        @event.listens_for(connectable, "begin")
        def _explicit_begin(conn):  # noqa: ANN001
            conn.exec_driver_sql("BEGIN")

    with connectable.connect() as connection:
        # render_as_batch: SQLite cannot ALTER most things; batch mode rebuilds the table.
        # SQLite DDL is transactional; alembic just defaults it off. With it on, an
        # interrupted migration rolls back instead of leaving _alembic_tmp_* tables
        # that crash-loop the next start.
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            transactional_ddl=True,
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
