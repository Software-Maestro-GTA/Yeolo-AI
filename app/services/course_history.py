"""Bounded history of provider IDs and independent model planning choices."""

import asyncio
import hashlib
import json
import logging
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

from app.core.config import settings
from app.schemas.course import CourseRequestSchema
from app.services.course_diversity import clean_metadata, overlap_scores

logger = logging.getLogger(__name__)


def _history_entry(serialized_ids: str, serialized_metadata: str | None) -> dict | None:
    """Read one local row; invalid IDs are skipped and bad metadata loses no IDs."""
    try:
        ids = json.loads(serialized_ids)
    except (ValueError, TypeError):
        logger.warning('Skipping unreadable course history IDs')
        return None
    if not isinstance(ids, list) or not ids or any(not isinstance(value, str) or not value.strip() for value in ids):
        logger.warning('Skipping malformed course history IDs')
        return None
    metadata = {}
    if serialized_metadata:
        try:
            metadata = json.loads(serialized_metadata)
        except (ValueError, TypeError):
            logger.warning('Ignoring unreadable course history metadata')
        if not isinstance(metadata, dict):
            logger.warning('Ignoring malformed course history metadata')
            metadata = {}
    fields = {
        name: [value for value in metadata.get(name, []) if isinstance(value, str)]
        if isinstance(metadata.get(name), list) else []
        for name in ('attraction_ids', 'areas', 'experiences')
    }
    return {'place_ids': ids, **clean_metadata(fields, set(ids))}


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
            connection.execute('CREATE TABLE IF NOT EXISTS course_plans (user_key TEXT NOT NULL, signature TEXT NOT NULL, metadata TEXT NOT NULL, PRIMARY KEY(user_key, signature))')
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

    def _operate(self, key: str, ids: set[str] | None, metadata: dict | None = None, max_overlap: float | None = None, profiles: bool = False) -> list | bool:
        connection = self._connect()
        try:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('DELETE FROM courses WHERE created < ?', (time.time() - self.ttl_seconds,))
            def prune_metadata() -> None:
                connection.execute('DELETE FROM course_plans WHERE NOT EXISTS (SELECT 1 FROM courses WHERE courses.user_key = course_plans.user_key AND courses.signature = course_plans.signature)')

            rows = connection.execute('SELECT c.ids, p.metadata FROM courses c LEFT JOIN course_plans p ON c.user_key = p.user_key AND c.signature = p.signature WHERE c.user_key = ? ORDER BY c.created DESC LIMIT ?', (key, self.max_entries)).fetchall()
            entries = [entry for row in rows if (entry := _history_entry(*row)) is not None]
            if ids is None:
                result = entries if profiles else [set(item['place_ids']) for item in entries]
            else:
                if not ids:
                    raise ValueError('Cannot record an empty course')
                serialized = json.dumps(sorted(ids), separators=(',', ':'))
                signature = hashlib.sha256(serialized.encode()).hexdigest()
                safe_metadata = clean_metadata(metadata, ids)
                scores = overlap_scores(ids, safe_metadata['attraction_ids'], entries)
                if max_overlap is not None and max(scores.values()) > max_overlap:
                    result = False
                else:
                    cursor = connection.execute('INSERT OR IGNORE INTO courses VALUES (?, ?, ?, ?)', (key, signature, serialized, time.time()))
                    result = cursor.rowcount == 1
                    if result and metadata is not None:
                        connection.execute('INSERT INTO course_plans VALUES (?, ?, ?)', (key, signature, json.dumps(safe_metadata, separators=(',', ':'))))
                connection.execute('DELETE FROM courses WHERE user_key = ? AND signature NOT IN (SELECT signature FROM courses WHERE user_key = ? ORDER BY created DESC LIMIT ?)', (key, key, self.max_entries))
                connection.execute('DELETE FROM courses WHERE rowid NOT IN (SELECT rowid FROM courses ORDER BY created DESC LIMIT 10000)')
            prune_metadata()
            connection.commit()
            return result
        finally:
            connection.close()

    async def recent(self, user_key: str) -> list[set[str]]:
        """Return recent successful place sets; SQLite I/O runs off the event loop."""
        return await asyncio.to_thread(self._operate, user_key, None)

    async def recent_profiles(self, user_key: str) -> list[dict]:
        """Read bounded planning profiles, including backward compatible ID-only rows."""
        return await asyncio.to_thread(self._operate, user_key, None, profiles=True)

    async def record_if_novel(self, user_key: str, place_ids: set[str], *, metadata: dict | None = None, max_overlap: float | None = None) -> bool:
        """Atomically check exact or optional partial overlap before remembering IDs.

        The partial guard uses the latest rows under BEGIN IMMEDIATE. Metadata
        contains only selected IDs and independently generated planning choices.
        OSError and SQLite errors propagate to the caller's fail-open policy.
        """
        return await asyncio.to_thread(self._operate, user_key, place_ids, metadata, max_overlap)
