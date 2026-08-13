import argparse
import os
import pathlib
import re
import shutil
import subprocess
from dataclasses import dataclass
from importlib import resources

MIGRATION_FILENAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


def package_migrations():
    return resources.files("kalshi_mm").joinpath("migrations")

SCHEMA_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version integer PRIMARY KEY,
    name text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: pathlib.Path

    @property
    def filename(self):
        return self.path.name

    def sql(self):
        return self.path.read_text()


def database_url():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "set DATABASE_URL, e.g. "
            "postgresql://localhost/kalshi_mm")
    return url


def connect(url=None):
    import psycopg
    return psycopg.connect(url or database_url())


def discover_migrations(directory=None):
    if directory is None:
        directory = package_migrations()
    elif isinstance(directory, (str, pathlib.Path)):
        directory = pathlib.Path(directory)
    if not directory.is_dir():
        raise FileNotFoundError(f"no migrations directory at {directory}")
    migrations, seen_versions = [], {}
    for path in sorted(directory.iterdir(), key=lambda entry: entry.name):
        if not path.name.endswith(".sql"):
            continue
        match = MIGRATION_FILENAME.match(path.name)
        if not match:
            raise ValueError(
                f"migration {path.name} does not match NNNN_lower_name.sql")
        version = int(match.group(1))
        if version in seen_versions:
            raise ValueError(
                f"duplicate migration version {version:04d}: "
                f"{seen_versions[version]} and {path.name}")
        seen_versions[version] = path.name
        migrations.append(Migration(version, match.group(2), path))
    return sorted(migrations, key=lambda migration: migration.version)


def ensure_migrations_table(connection):
    with connection.cursor() as cursor:
        cursor.execute(SCHEMA_MIGRATIONS_TABLE)
    connection.commit()


def applied_versions(connection):
    with connection.cursor() as cursor:
        cursor.execute("SELECT version FROM schema_migrations ORDER BY version")
        return {row[0] for row in cursor.fetchall()}


def pending_migrations(connection, directory=None):
    applied = applied_versions(connection)
    return [migration for migration in discover_migrations(directory)
            if migration.version not in applied]


def migrate(connection, directory=None):
    ensure_migrations_table(connection)
    applied = []
    for migration in pending_migrations(connection, directory):
        with connection.cursor() as cursor:
            cursor.execute(migration.sql())
            cursor.execute(
                "INSERT INTO schema_migrations (version, name) "
                "VALUES (%s, %s)", (migration.version, migration.name))
        connection.commit()
        applied.append(migration)
    return applied


def migration_status(connection, directory=None):
    ensure_migrations_table(connection)
    applied = applied_versions(connection)
    return [(migration, migration.version in applied)
            for migration in discover_migrations(directory)]


def run_migrate(args):
    with connect(args.url) as connection:
        applied = migrate(connection)
    if not applied:
        print("already up to date")
        return
    for migration in applied:
        print(f"applied {migration.filename}")


def run_status(args):
    with connect(args.url) as connection:
        rows = migration_status(connection)
    for migration, is_applied in rows:
        print(f"  {'applied' if is_applied else 'PENDING':>7}  "
              f"{migration.filename}")
    outstanding = sum(1 for _migration, is_applied in rows if not is_applied)
    print(f"{len(rows)} migrations, {outstanding} pending")


def run_import_players(args):
    from kalshi_mm.player_id_map import import_json_into_postgres
    with connect(args.url) as connection:
        imported, total = import_json_into_postgres(connection, args.path)
    print(f"imported {imported} of {total} players from {args.path}")


def run_shell(args):
    if shutil.which("psql") is None:
        raise SystemExit("psql is not installed; install the postgresql "
                         "client package")
    environment = dict(os.environ)
    packaged_config = resources.files("kalshi_mm").joinpath(".psqlrc")
    if packaged_config.is_file() and "PSQLRC" not in environment:
        with resources.as_file(packaged_config) as config_path:
            environment["PSQLRC"] = str(config_path)
            raise SystemExit(subprocess.call(
                ["psql", args.url or database_url()], env=environment))
    raise SystemExit(subprocess.call(["psql", args.url or database_url()],
                                     env=environment))


def main():
    parser = argparse.ArgumentParser(
        description="Schema migrations for the kalshi-mm database. Set "
                    "DATABASE_URL or pass --url.")
    parser.add_argument("--url", default=None)
    commands = parser.add_subparsers(dest="command", required=True)
    migrate_parser = commands.add_parser("migrate",
                                         help="apply pending migrations")
    migrate_parser.set_defaults(func=run_migrate)
    status_parser = commands.add_parser("status",
                                        help="show applied and pending")
    status_parser.set_defaults(func=run_status)
    import_parser = commands.add_parser(
        "import-players", help="load player_id_map.json into the players table")
    import_parser.add_argument("--path", default="player_id_map.json")
    import_parser.set_defaults(func=run_import_players)
    shell_parser = commands.add_parser(
        "shell", help="open a psql session against DATABASE_URL")
    shell_parser.set_defaults(func=run_shell)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
