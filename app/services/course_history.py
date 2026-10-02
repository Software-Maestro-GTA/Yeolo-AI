"""Bounded persistent course history; stores hashed users and provider IDs only."""

import asyncio
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

from app.core.config import settings
from app.schemas.course import CourseRequestSchema


def history_key(request: CourseRequestSchema) -> str:
    """Hash user and normalized destination, without storing personal profiles."""
    trip = request.tripCondition
    value = f'{request.userId}|{trip.destinationCountry.strip().casefold()}|{trip.destinationCity.strip().casefold()}'
    return hashlib.sha256(value.encode()).hexdigest()


class CourseHistory:
    """SQLite history with TTL, size bounds, and an atomic duplicate claim.

    Args:
        path: Shared persistent database path on this server.
        ttl_seconds: Lifetime of remembered place sets.
        max_entries: Maximum remembered courses per user/destination.
    """

    def __init__(self, path: str | Path | None = None, ttl_seconds: int = 2592000, max_entries: int = 20) -> None:
        self.path = Path(path or settings.COURSE_HISTORY_PATH)
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.execute('CREATE TABLE IF NOT EXISTS courses (user_key TEXT NOT NULL, signature TEXT NOT NULL, ids TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(user_key, signature))')
        except sqlite3.Error:
            connection.close()
            raise
        return connection

    def _ensure_available(self) -> None:
        connection = self._connect()
        try:
            connection.execute('BEGIN IMMEDIATE')
            probe = uuid4().hex
            connection.execute('INSERT INTO courses VALUES (?, ?, ?, ?)', (probe, probe, '[]', time.time()))
        finally:
            try:
                # Exercise SQLite journal writes without retaining a probe or pruning history.
                connection.rollback()
            finally:
                connection.close()

    async def ensure_available(self) -> None:
        """Verify configured storage with a rolled-back write off the event loop.

        Returns:
            None once the directory, database, and transaction journal are writable.
        Raises:
            OSError: The configured directory cannot be accessed or created.
            sqlite3.Error: Database initialization or a real write transaction fails.

        Existing course history is preserved, including expired entries; the probe
        does not invoke normal history cleanup or persist a dummy course.
        """
        await asyncio.to_thread(self._ensure_available)

    def _operate(self, key: str, ids: set[str] | None) -> list[set[str]] | bool:
        connection = self._connect()
        try:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('DELETE FROM courses WHERE created < ?', (time.time() - self.ttl_seconds,))
            if ids is None:
                rows = connection.execute('SELECT ids FROM courses WHERE user_key = ? ORDER BY created DESC LIMIT ?', (key, self.max_entries)).fetchall()
                result = [set(json.loads(row[0])) for row in rows]
            else:
                if not ids:
                    raise ValueError('Cannot record an empty course')
                serialized = json.dumps(sorted(ids), separators=(',', ':'))
                signature = hashlib.sha256(serialized.encode()).hexdigest()
                cursor = connection.execute('INSERT OR IGNORE INTO courses VALUES (?, ?, ?, ?)', (key, signature, serialized, time.time()))
                result = cursor.rowcount == 1
                connection.execute('DELETE FROM courses WHERE user_key = ? AND signature NOT IN (SELECT signature FROM courses WHERE user_key = ? ORDER BY created DESC LIMIT ?)', (key, key, self.max_entries))
                connection.execute('DELETE FROM courses WHERE rowid NOT IN (SELECT rowid FROM courses ORDER BY created DESC LIMIT 10000)')
            connection.commit()
            return result
        finally:
            connection.close()

    async def recent(self, user_key: str) -> list[set[str]]:
        """Return recent successful place sets; SQLite I/O runs off the event loop."""
        return await asyncio.to_thread(self._operate, user_key, None)

    async def record_if_novel(self, user_key: str, place_ids: set[str]) -> bool:
        """Atomically accept a new place set, or return False for a duplicate."""
        return await asyncio.to_thread(self._operate, user_key, place_ids)
