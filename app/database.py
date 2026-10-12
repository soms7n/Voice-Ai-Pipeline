"""Small SQLite foundation for the first real-call test."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path

from .config import get_settings


SCHEMA_VERSION = 2


def connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path, timeout=30, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


@contextmanager
def transaction(database_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open a connection, commit on success, roll back on error, ALWAYS close.

    (`with sqlite3.connect(...)` only commits/rolls back; it never closes.)
    """
    connection = connect(database_path or get_settings().database_path)
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


def _migrate(connection: sqlite3.Connection) -> None:
    """Bring databases created by earlier versions up to the current schema.

    CREATE TABLE IF NOT EXISTS never alters an existing table, so old files
    would otherwise keep the old columns forever.
    """
    if "status" not in _columns(connection, "campaigns"):
        connection.execute("ALTER TABLE campaigns ADD COLUMN status TEXT NOT NULL DEFAULT 'draft'")

    attempt_columns = _columns(connection, "call_attempts")
    if "telephony_status" not in attempt_columns:
        if "lifecycle_status" in attempt_columns:
            connection.execute("ALTER TABLE call_attempts RENAME COLUMN lifecycle_status TO telephony_status")
        else:
            connection.execute(
                "ALTER TABLE call_attempts ADD COLUMN telephony_status TEXT NOT NULL DEFAULT 'created'"
            )


def initialise_database() -> None:
    settings = get_settings()
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    with transaction(settings.database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS campaigns (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS contacts (
                id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
                full_name TEXT NOT NULL,
                phone_e164 TEXT NOT NULL,
                language TEXT NOT NULL DEFAULT 'English',
                consent_status TEXT NOT NULL DEFAULT 'unknown',
                consent_source TEXT,
                opted_out_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(campaign_id, phone_e164)
            );
            CREATE TABLE IF NOT EXISTS call_attempts (
                id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
                contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE RESTRICT,
                exotel_call_sid TEXT UNIQUE,
                exotel_stream_sid TEXT UNIQUE,
                telephony_status TEXT NOT NULL DEFAULT 'created',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS telephony_events (
                id TEXT PRIMARY KEY,
                call_attempt_id TEXT REFERENCES call_attempts(id) ON DELETE SET NULL,
                event_key TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS conversation_outcomes (
                id TEXT PRIMARY KEY,
                call_attempt_id TEXT NOT NULL UNIQUE REFERENCES call_attempts(id) ON DELETE CASCADE,
                disposition TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                reported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_call_attempts_contact ON call_attempts(contact_id);
            CREATE INDEX IF NOT EXISTS idx_telephony_events_attempt ON telephony_events(call_attempt_id);
            """
        )
        _migrate(connection)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def get_connection() -> Generator[sqlite3.Connection, None, None]:
    connection = connect(get_settings().database_path)
    try:
        yield connection
    finally:
        connection.close()