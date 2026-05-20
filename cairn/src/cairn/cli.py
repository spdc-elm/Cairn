from pathlib import Path
from datetime import datetime

import click
import uvicorn

from cairn.server import db
from cairn.server.migrations import runner


@click.group()
def main():
    """Cairn - Fact-graph based collaborative exploration protocol."""


@main.command("serve")
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind host")
@click.option("--port", default=8000, show_default=True, help="Bind port")
@click.option(
    "--db",
    "--db-path",
    "db_path",
    type=click.Path(),
    default=str(db.DEFAULT_DB),
    show_default=True,
    help="SQLite database path",
)
@click.option("--log-level", default="info", show_default=True, help="Uvicorn log level")
@click.option("--access-log/--no-access-log", default=True, show_default=True, help="Enable Uvicorn access log")
def serve(host: str, port: int, db_path: str, log_level: str, access_log: bool):
    """Start the Cairn API server."""
    _run_server(host, port, db_path, log_level, access_log)


@main.command("server")
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind host")
@click.option("--port", default=8000, show_default=True, help="Bind port")
@click.option(
    "--db",
    "--db-path",
    "db_path",
    type=click.Path(),
    default=str(db.DEFAULT_DB),
    show_default=True,
    help="SQLite database path",
)
@click.option("--log-level", default="info", show_default=True, help="Uvicorn log level")
@click.option("--access-log/--no-access-log", default=True, show_default=True, help="Enable Uvicorn access log")
def server(host: str, port: int, db_path: str, log_level: str, access_log: bool):
    """Start the Cairn API server."""
    _run_server(host, port, db_path, log_level, access_log)


def _run_server(host: str, port: int, db_path: str, log_level: str, access_log: bool) -> None:
    db.configure(Path(db_path))
    from cairn.server.app import app

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level.lower(),
        access_log=access_log,
    )


@main.group("db")
def db_group():
    """Inspect and migrate the Cairn SQLite database."""


@db_group.command("status")
@click.option(
    "--db",
    "--db-path",
    "db_path",
    type=click.Path(path_type=Path),
    default=db.DEFAULT_DB,
    show_default=True,
    help="SQLite database path",
)
def db_status(db_path: Path):
    """Show applied and pending database migrations."""
    click.echo(f"DB: {db_path}")
    if not db_path.exists():
        available = runner.available_migrations()
        click.echo("exists: no")
        click.echo(f"latest: {available[-1].version if available else 'none'}")
        click.echo("applied: none")
        click.echo("pending: " + _format_versions(migration.version for migration in available))
        return
    with db.connect(db_path) as conn:
        migration_status = runner.status(conn)
    click.echo("exists: yes")
    click.echo(f"latest: {migration_status.latest or 'none'}")
    click.echo("applied: " + _format_versions(migration_status.applied))
    click.echo("pending: " + _format_versions(migration_status.pending))


@db_group.command("migrate")
@click.option(
    "--db",
    "--db-path",
    "db_path",
    type=click.Path(path_type=Path),
    default=db.DEFAULT_DB,
    show_default=True,
    help="SQLite database path",
)
def db_migrate(db_path: Path):
    """Apply pending database migrations."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with db.connect(db_path) as conn:
        before = runner.status(conn)
        after = runner.migrate(conn)
    applied_now = [version for version in after.applied if version in before.pending]
    click.echo(f"DB: {db_path}")
    click.echo("applied now: " + _format_versions(applied_now))
    click.echo("pending: " + _format_versions(after.pending))


@db_group.command("reset")
@click.option("--to", "target", required=True, help="Schema target, currently v3.2")
@click.option(
    "--db",
    "--db-path",
    "db_path",
    type=click.Path(path_type=Path),
    default=db.DEFAULT_DB,
    show_default=True,
    help="SQLite database path",
)
@click.option("--yes", is_flag=True, help="Confirm destructive reset")
def db_reset(target: str, db_path: Path, yes: bool):
    """Back up and recreate the database with the fresh target schema."""
    if target != "v3.2":
        raise click.ClickException("only --to v3.2 is supported")
    if not yes:
        raise click.ClickException("reset requires --yes")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = None
    if db_path.exists():
        backup_path = db_path.with_name(f"{db_path.name}.bak-{datetime.now().strftime('%Y%m%d%H%M%S')}")
        with db.connect(db_path) as source:
            source.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            with db.connect(backup_path) as backup:
                source.backup(backup)
        with db.connect(backup_path):
            pass
    tmp_path = db_path.with_suffix(db_path.suffix + ".reset-tmp")
    try:
        if tmp_path.exists():
            tmp_path.unlink()
        with db.connect(tmp_path) as conn:
            runner.migrate(conn)
        tmp_path.replace(db_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    click.echo(f"reset: {db_path}")
    if backup_path is not None:
        click.echo(f"backup: {backup_path}")


@db_group.command("restore")
@click.option(
    "--db",
    "--db-path",
    "db_path",
    type=click.Path(path_type=Path),
    default=db.DEFAULT_DB,
    show_default=True,
    help="SQLite database path",
)
@click.option("--backup", "backup_path", type=click.Path(path_type=Path), required=True, help="Backup database path")
@click.option("--yes", is_flag=True, help="Confirm restore overwrite")
def db_restore(db_path: Path, backup_path: Path, yes: bool):
    """Restore a database from a SQLite backup file."""
    if not yes:
        raise click.ClickException("restore requires --yes")
    if not backup_path.exists():
        raise click.ClickException(f"backup not found: {backup_path}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        with db.connect(db_path) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    tmp_path = db_path.with_suffix(db_path.suffix + ".restore-tmp")
    try:
        with db.connect(backup_path) as source, db.connect(tmp_path) as target:
            source.backup(target)
        tmp_path.replace(db_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    click.echo(f"restored: {db_path}")


def _format_versions(versions) -> str:
    values = tuple(versions)
    return ", ".join(values) if values else "none"


@main.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Dispatcher config path",
)
@click.option("--once", is_flag=True, help="Run one scheduling iteration and exit")
@click.option(
    "--startup-healthcheck-only",
    is_flag=True,
    help="Run startup worker healthchecks and exit",
)
@click.option(
    "--environment-healthcheck-only",
    is_flag=True,
    help="Run environment healthchecks and exit",
)
@click.option("--log-level", default="INFO", show_default=True, help="Log level")
def dispatch(config_path: Path, once: bool, startup_healthcheck_only: bool, environment_healthcheck_only: bool, log_level: str):
    """Run the Cairn dispatcher."""
    from cairn.dispatcher.logging import configure_logging
    from cairn.dispatcher.scheduler.loop import DispatcherLoop

    configure_logging(log_level, bare=startup_healthcheck_only or environment_healthcheck_only)
    loop = DispatcherLoop(config_path)
    try:
        if environment_healthcheck_only:
            loop.run_environment_healthchecks_only()
            return
        if startup_healthcheck_only:
            loop.run_startup_healthchecks_only()
            return
        loop.run(once=once)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
