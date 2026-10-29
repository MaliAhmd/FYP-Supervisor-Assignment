"""Short-lived connections, atomic migrations, and explicit transactions."""

from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
import sqlite3

from app.config import ROOT


class MigrationError(RuntimeError):
    pass


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def transaction(self, *, write: bool = True):
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self, directory: Path | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self.connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
        finally:
            connection.close()
        # A single write transaction also serializes competing startup migrations.
        with self.transaction() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY, checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            )""")
            for migration in sorted((directory or ROOT / "migrations").glob("*.sql")):
                source = migration.read_text(encoding="utf-8")
                checksum = sha256(source.encode()).hexdigest()
                existing = connection.execute(
                    "SELECT checksum FROM schema_migrations WHERE version = ?",
                    (migration.name,),
                ).fetchone()
                if existing:
                    if existing["checksum"] != checksum:
                        raise MigrationError(f"Applied migration changed: {migration.name}")
                    continue
                # executescript implicitly commits; use complete_statement instead.
                statement = ""
                for line in source.splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        connection.execute(statement)
                        statement = ""
                if statement.strip():
                    raise MigrationError(f"Incomplete SQL in {migration.name}")
                connection.execute(
                    "INSERT INTO schema_migrations(version, checksum) VALUES (?, ?)",
                    (migration.name, checksum),
                )
