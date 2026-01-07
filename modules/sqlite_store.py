# modules/sqlite_store.py
import sqlite3
import time
import threading
import os
from typing import Optional, Iterable

DEFAULT_DB_PATH = os.environ.get("SCANNED_DB_PATH", "/data/scanned.db")

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS scanned_guids (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  feed_name TEXT NOT NULL,
  guid TEXT NOT NULL,
  title TEXT,
  imdb_id TEXT,
  size INTEGER,
  first_seen INTEGER NOT NULL,
  last_seen INTEGER NOT NULL,
  UNIQUE(feed_name, guid)
);
CREATE INDEX IF NOT EXISTS idx_feed_lastseen ON scanned_guids(feed_name, last_seen);
"""

class SQLiteStore:
    """
    Lightweight SQLite wrapper for storing scanned GUIDs per feed.
    Usage:
        store = SQLiteStore('/data/scanned.db')
        store.mark_seen('NZBplanet', 'guid123', title='The.Movie', imdb_id='123', size=12345)
        seen = store.has_seen('NZBplanet', 'guid123')
        store.prune_older_than(days=90)
        store.close()
    """
    def __init__(self, db_path: str = DEFAULT_DB_PATH, timeout: float = 30.0):
        self.db_path = db_path
        self.timeout = timeout
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, timeout=self.timeout, check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys = ON")
        # Ensure WAL and create schema
        with self._lock:
            cur = self._conn.cursor()
            cur.executescript(SCHEMA_SQL)
            self._conn.commit()

    def close(self):
        with self._lock:
            try:
                self._conn.commit()
            finally:
                self._conn.close()

    def _now(self) -> int:
        return int(time.time())

    def has_seen(self, feed_name: str, guid: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM scanned_guids WHERE feed_name=? AND guid=? LIMIT 1",
                (feed_name, guid),
            )
            return cur.fetchone() is not None

    def mark_seen(self, feed_name: str, guid: str, title: Optional[str] = None,
                  imdb_id: Optional[str] = None, size: Optional[int] = None) -> None:
        """
        Insert or update a scanned GUID record. Updates last_seen on conflict.
        """
        now = self._now()
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    """
                    INSERT INTO scanned_guids(feed_name,guid,title,imdb_id,size,first_seen,last_seen)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(feed_name,guid) DO UPDATE SET
                      title=excluded.title,
                      imdb_id=excluded.imdb_id,
                      size=excluded.size,
                      last_seen=excluded.last_seen
                    """,
                    (feed_name, guid, title, imdb_id, size, now, now),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def prune_older_than(self, days: int = 90) -> int:
        """
        Delete rows where last_seen is older than `days`. Returns number of rows deleted.
        """
        cutoff = int(time.time()) - int(days) * 86400
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM scanned_guids WHERE last_seen < ?",
                (cutoff,),
            )
            self._conn.commit()
            return cur.rowcount

    def list_recent(self, feed_name: Optional[str] = None, limit: int = 100) -> Iterable[dict]:
        """
        Yield recent rows for inspection.
        """
        with self._lock:
            if feed_name:
                cur = self._conn.execute(
                    "SELECT feed_name,guid,title,imdb_id,size,first_seen,last_seen FROM scanned_guids WHERE feed_name=? ORDER BY last_seen DESC LIMIT ?",
                    (feed_name, limit),
                )
            else:
                cur = self._conn.execute(
                    "SELECT feed_name,guid,title,imdb_id,size,first_seen,last_seen FROM scanned_guids ORDER BY last_seen DESC LIMIT ?",
                    (limit,),
                )
            cols = [c[0] for c in cur.description]
            for row in cur.fetchall():
                yield dict(zip(cols, row))

    def migrate_from_feed_files(self, feed_dir: str, file_pattern: str = "scanned_*.txt") -> int:
        """
        Migrate existing per-feed text files into the DB.
        Each file should contain one GUID per line, optionally with a timestamp and title.
        Expected line formats:
            GUID
            2026-01-07T01:04:27Z GUID Title...
            GUID|timestamp|title
        Returns number of rows inserted.
        """
        import glob
        inserted = 0
        files = glob.glob(os.path.join(feed_dir, file_pattern))
        for path in files:
            feed_name = os.path.splitext(os.path.basename(path))[0]
            # normalize feed_name: scanned_nzbplanet.txt -> nzbplanet
            if feed_name.startswith("scanned_"):
                feed_name = feed_name[len("scanned_"):]
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    # try pipe-separated first
                    if "|" in line:
                        parts = line.split("|", 2)
                        if len(parts) == 3:
                            ts, guid, title = parts
                        elif len(parts) == 2:
                            ts, guid = parts
                            title = None
                        else:
                            guid = parts[0]
                            title = None
                    else:
                        # try space-separated timestamp
                        parts = line.split(None, 2)
                        if len(parts) == 3 and parts[0].count("-") == 2 and "T" in parts[0]:
                            ts, guid, title = parts
                        elif len(parts) >= 1:
                            guid = parts[0]
                            title = parts[1] if len(parts) > 1 else None
                            ts = None
                        else:
                            continue
                    # parse timestamp if present
                    try:
                        first_seen = int(time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))) if ts else self._now()
                    except Exception:
                        first_seen = self._now()
                    # Upsert with provided timestamp as both first_seen and last_seen
                    with self._lock:
                        self._conn.execute(
                            """
                            INSERT INTO scanned_guids(feed_name,guid,title,first_seen,last_seen)
                            VALUES(?,?,?,?,?)
                            ON CONFLICT(feed_name,guid) DO UPDATE SET
                              title=COALESCE(excluded.title, scanned_guids.title),
                              last_seen=MAX(scanned_guids.last_seen, excluded.last_seen)
                            """,
                            (feed_name, guid, title, first_seen, first_seen),
                        )
                        inserted += 1
                self._conn.commit()
        return inserted
