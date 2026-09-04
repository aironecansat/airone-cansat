"""SQLite storage manager with WAL mode, migrations, and per-thread connections."""
from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ..core.errors import MigrationError, StorageError

logger = logging.getLogger(__name__)

_MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "migrations")


@dataclass
class DatabaseHealth:
    connected: bool
    size_mb: float
    wal_size_mb: float
    table_counts: Dict[str, int]


class DatabaseManager:
    """Thread-safe SQLite manager. Each thread gets its own connection."""

    def __init__(self, db_path: str, migrations_dir: Optional[str] = None) -> None:
        self.db_path = db_path
        self.migrations_dir = migrations_dir or _MIGRATIONS_DIR
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._local = threading.local()
        # Initialise once on the creating thread.
        conn = self._connect()
        self._configure(conn)
        self.run_migrations(conn)
        self._restrict_permissions()

    def _restrict_permissions(self) -> None:
        """Make the database (and WAL/SHM side files) owner-only (0600).

        The database holds bcrypt password hashes and the token revocation
        list; other local users must not be able to read it. In-memory
        databases have no file to protect.
        """

        if self.db_path == ":memory:" or self.db_path.startswith("file:"):
            return
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path + suffix
            if os.path.exists(path):
                try:
                    os.chmod(path, 0o600)
                except OSError as exc:  # pragma: no cover - platform dependent
                    logger.warning("Could not restrict permissions on %s: %s", path, exc)

    # -- connection management ------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path, timeout=30.0, check_same_thread=False,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _configure(conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.commit()

    @property
    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._configure(conn)
            self._local.conn = conn
        return conn

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Cursor]:
        conn = self.connection
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()

    # -- retrying execution ---------------------------------------------
    def execute_with_retry(
        self, sql: str, params: Tuple = (), retries: int = 3
    ) -> sqlite3.Cursor:
        last_exc: Optional[Exception] = None
        for attempt in range(retries):
            try:
                conn = self.connection
                cur = conn.execute(sql, params)
                conn.commit()
                return cur
            except sqlite3.OperationalError as exc:
                last_exc = exc
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    wait = 0.1 * (2 ** attempt)
                    logger.warning("SQLite busy, retrying in %.2fs (%s)", wait, exc)
                    time.sleep(wait)
                    continue
                raise
        raise StorageError(f"execute_with_retry exhausted retries: {last_exc}")

    def executemany(self, sql: str, seq_of_params: List[Tuple]) -> None:
        conn = self.connection
        conn.executemany(sql, seq_of_params)
        conn.commit()

    def query(self, sql: str, params: Tuple = ()) -> List[sqlite3.Row]:
        return self.connection.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Tuple = ()) -> Optional[sqlite3.Row]:
        return self.connection.execute(sql, params).fetchone()

    # -- migrations ------------------------------------------------------
    def run_migrations(self, conn: Optional[sqlite3.Connection] = None) -> List[int]:
        conn = conn or self.connection
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT, checksum TEXT)"
        )
        conn.commit()
        applied = {
            row[0] for row in conn.execute("SELECT version FROM schema_migrations")
        }
        if not os.path.isdir(self.migrations_dir):
            logger.warning("Migrations dir %s not found", self.migrations_dir)
            return []
        newly_applied: List[int] = []
        for fname in sorted(os.listdir(self.migrations_dir)):
            if not fname.endswith(".sql"):
                continue
            try:
                version = int(fname.split("_", 1)[0])
            except ValueError:
                logger.warning("Skipping unnumbered migration %s", fname)
                continue
            if version in applied:
                continue
            path = os.path.join(self.migrations_dir, fname)
            with open(path, "r", encoding="utf-8") as fh:
                sql = fh.read()
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            try:
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at, checksum) "
                    "VALUES (?, ?, ?)",
                    (version, datetime.now(timezone.utc).isoformat(), checksum),
                )
                conn.commit()
            except sqlite3.Error as exc:
                conn.rollback()
                raise MigrationError(f"Migration {fname} failed: {exc}") from exc
            newly_applied.append(version)
            logger.info("Applied migration %s (version %d)", fname, version)
        return newly_applied

    def schema_version(self) -> int:
        row = self.query_one("SELECT MAX(version) AS v FROM schema_migrations")
        return int(row["v"]) if row and row["v"] is not None else 0

    # -- backup & health -------------------------------------------------
    def backup_to(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        dest = sqlite3.connect(path)
        try:
            self.connection.backup(dest)
        finally:
            dest.close()
        logger.info("Database backed up to %s", path)

    def health_check(self) -> DatabaseHealth:
        try:
            size_mb = os.path.getsize(self.db_path) / (1024 * 1024) if os.path.exists(self.db_path) else 0.0
            wal_path = self.db_path + "-wal"
            wal_mb = os.path.getsize(wal_path) / (1024 * 1024) if os.path.exists(wal_path) else 0.0
            tables = [
                r[0] for r in self.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            ]
            counts: Dict[str, int] = {}
            for t in tables:
                if t.startswith("sqlite_"):
                    continue
                counts[t] = self.connection.execute(
                    f"SELECT COUNT(*) FROM {t}"
                ).fetchone()[0]
            return DatabaseHealth(True, round(size_mb, 4), round(wal_mb, 4), counts)
        except sqlite3.Error as exc:
            logger.error("Database health check failed: %s", exc)
            return DatabaseHealth(False, 0.0, 0.0, {})

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
