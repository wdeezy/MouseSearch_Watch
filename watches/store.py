"""
sqlite-backed persistence for watches.

The database lives at ``<DATA_PATH>/watches.db`` and is opened lazily. All
access goes through a re-entrant lock so the synchronous sqlite calls are safe
from the async app (they are tiny and never block for long).
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

EVENT_KINDS = {"checked", "matched", "grabbed", "skipped_seen", "skipped_cap", "error"}

JSON_LIST_COLUMNS = ("search_fields", "language_ids", "main_cat", "category_ids", "flag_ids")

WATCH_DEFAULTS: dict[str, Any] = {
    "name": "",
    "enabled": True,
    "query": "",
    "title_regex": "",
    "search_fields": ["title"],
    "language_ids": ["1"],
    "main_cat": [],
    "category_ids": [],
    "flag_ids": [],
    "flags_mode": "0",
    "search_type": "all",
    "min_seeders": None,
    "interval_minutes": 60,
    "torrent_category": "",
    "custom_relative_path": "",
    "custom_destination_path": "",
    "max_grabs_per_run": 3,
}

WATCH_COLUMNS = (
    "name", "enabled", "query", "title_regex", "search_fields", "language_ids", "main_cat",
    "category_ids", "flag_ids", "flags_mode", "search_type", "min_seeders", "interval_minutes",
    "torrent_category", "custom_relative_path", "custom_destination_path", "max_grabs_per_run",
)

MIN_INTERVAL_MINUTES = 10
MAX_EVENTS_PER_WATCH = 500

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_db_path: Path | None = None


class WatchValidationError(ValueError):
    """Raised when a watch definition fails validation (surfaces as HTTP 400)."""


# --------------------------------------------------------------------------- setup
def configure(db_path: str | Path | None) -> None:
    """Point the store at ``db_path`` (closing any open connection)."""
    global _db_path
    with _lock:
        close()
        _db_path = Path(db_path) if db_path else None


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None


def db_path() -> Path | None:
    return _db_path


def _get_conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is not None:
            return _conn
        if _db_path is None:
            raise RuntimeError("watch store is not configured; call watches.store.configure(path) first")
        _db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(_db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _create_schema(conn)
        _conn = conn
        return conn


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS watches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            query TEXT NOT NULL DEFAULT '',
            title_regex TEXT NOT NULL DEFAULT '',
            search_fields TEXT NOT NULL DEFAULT '["title"]',
            language_ids TEXT NOT NULL DEFAULT '[]',
            main_cat TEXT NOT NULL DEFAULT '[]',
            category_ids TEXT NOT NULL DEFAULT '[]',
            flag_ids TEXT NOT NULL DEFAULT '[]',
            flags_mode TEXT NOT NULL DEFAULT '0',
            search_type TEXT NOT NULL DEFAULT 'all',
            min_seeders INTEGER,
            interval_minutes INTEGER NOT NULL DEFAULT 60,
            torrent_category TEXT NOT NULL DEFAULT '',
            custom_relative_path TEXT NOT NULL DEFAULT '',
            custom_destination_path TEXT NOT NULL DEFAULT '',
            max_grabs_per_run INTEGER NOT NULL DEFAULT 3,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_run_at TEXT,
            last_error TEXT NOT NULL DEFAULT '',
            last_summary TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS watch_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
            ts TEXT NOT NULL,
            kind TEXT NOT NULL,
            mam_id TEXT,
            title TEXT,
            detail TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_watch_events_watch_ts ON watch_events(watch_id, ts DESC, id DESC);
        CREATE TABLE IF NOT EXISTS watch_seen (
            watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
            mam_id TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            grabbed INTEGER NOT NULL DEFAULT 0,
            title TEXT,
            PRIMARY KEY (watch_id, mam_id)
        );
        """
    )
    conn.commit()


# --------------------------------------------------------------------------- helpers
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _as_str_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        if value.startswith("["):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = value.split(",")
        else:
            value = value.split(",")
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _as_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "on", "yes")


def _as_int(value, *, field: str, default: int | None, minimum: int | None = None) -> int | None:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise WatchValidationError(f"{field} must be a whole number")
    if minimum is not None and parsed < minimum:
        raise WatchValidationError(f"{field} must be at least {minimum}")
    return parsed


def normalize_watch(data: dict, *, existing: dict | None = None) -> dict:
    """
    Validate and normalize an incoming watch definition (from the API or a test).
    Unspecified fields fall back to ``existing`` (on update) or ``WATCH_DEFAULTS``.
    Raises ``WatchValidationError`` with a user-facing message.
    """
    base = dict(WATCH_DEFAULTS)
    if existing:
        base.update({key: existing.get(key, base[key]) for key in WATCH_COLUMNS})
    data = data or {}

    def pick(key):
        return data[key] if key in data else base[key]

    name = str(pick("name") or "").strip()
    if not name:
        raise WatchValidationError("name is required")

    title_regex = str(pick("title_regex") or "").strip()
    if title_regex:
        try:
            re.compile(title_regex, re.IGNORECASE)
        except re.error as exc:
            raise WatchValidationError(f"title_regex does not compile: {exc}")

    query = str(pick("query") or "").strip()
    if not query and not title_regex:
        raise WatchValidationError("either a search query or a title regex is required")

    search_fields = [f for f in _as_str_list(pick("search_fields")) if f in (
        "title", "author", "series", "narrator", "description", "tags", "filenames"
    )]
    if not search_fields:
        search_fields = ["title"]

    normalized = {
        "name": name,
        "enabled": _as_bool(pick("enabled"), True),
        "query": query,
        "title_regex": title_regex,
        "search_fields": search_fields,
        "language_ids": _as_str_list(pick("language_ids")),
        "main_cat": [m for m in _as_str_list(pick("main_cat")) if m != "all"],
        "category_ids": _as_str_list(pick("category_ids")),
        "flag_ids": _as_str_list(pick("flag_ids")),
        "flags_mode": "1" if str(pick("flags_mode") or "0").strip() == "1" else "0",
        "search_type": str(pick("search_type") or "all").strip() or "all",
        "min_seeders": _as_int(pick("min_seeders"), field="min_seeders", default=None, minimum=0),
        "interval_minutes": _as_int(
            pick("interval_minutes"), field="interval_minutes",
            default=WATCH_DEFAULTS["interval_minutes"], minimum=MIN_INTERVAL_MINUTES,
        ),
        "torrent_category": str(pick("torrent_category") or "").strip(),
        "custom_relative_path": str(pick("custom_relative_path") or "").strip(),
        "custom_destination_path": str(pick("custom_destination_path") or "").strip(),
        "max_grabs_per_run": _as_int(
            pick("max_grabs_per_run"), field="max_grabs_per_run",
            default=WATCH_DEFAULTS["max_grabs_per_run"], minimum=1,
        ),
    }
    return normalized


def _row_to_watch(row: sqlite3.Row) -> dict:
    watch = dict(row)
    for column in JSON_LIST_COLUMNS:
        try:
            watch[column] = json.loads(watch.get(column) or "[]")
        except (TypeError, json.JSONDecodeError):
            watch[column] = []
    watch["enabled"] = bool(watch.get("enabled"))
    return watch


def _watch_to_row_values(watch: dict) -> dict:
    values = {}
    for column in WATCH_COLUMNS:
        value = watch.get(column)
        if column in JSON_LIST_COLUMNS:
            value = json.dumps(list(value or []))
        elif column == "enabled":
            value = 1 if value else 0
        values[column] = value
    return values


# --------------------------------------------------------------------------- watches CRUD
def list_watches() -> list[dict]:
    with _lock:
        rows = _get_conn().execute("SELECT * FROM watches ORDER BY name COLLATE NOCASE, id").fetchall()
    return [_row_to_watch(row) for row in rows]


def get_watch(watch_id: int) -> dict | None:
    with _lock:
        row = _get_conn().execute("SELECT * FROM watches WHERE id = ?", (int(watch_id),)).fetchone()
    return _row_to_watch(row) if row else None


def upsert_watch(data: dict) -> dict:
    """
    Insert (no ``id``) or update (with ``id``) a watch. ``data`` is validated via
    ``normalize_watch``; the stored watch dict is returned.
    """
    watch_id = data.get("id") if data else None
    existing = get_watch(watch_id) if watch_id not in (None, "", 0) else None
    if watch_id not in (None, "", 0) and existing is None:
        raise WatchValidationError(f"watch {watch_id} does not exist")

    normalized = normalize_watch(data, existing=existing)
    values = _watch_to_row_values(normalized)
    now = utc_now_iso()

    with _lock:
        conn = _get_conn()
        if existing is None:
            columns = list(values.keys()) + ["created_at", "updated_at"]
            placeholders = ", ".join("?" for _ in columns)
            cursor = conn.execute(
                f"INSERT INTO watches ({', '.join(columns)}) VALUES ({placeholders})",
                list(values.values()) + [now, now],
            )
            new_id = cursor.lastrowid
        else:
            assignments = ", ".join(f"{column} = ?" for column in values.keys())
            conn.execute(
                f"UPDATE watches SET {assignments}, updated_at = ? WHERE id = ?",
                list(values.values()) + [now, int(existing["id"])],
            )
            new_id = int(existing["id"])
        conn.commit()
    return get_watch(new_id)


def delete_watch(watch_id: int) -> bool:
    with _lock:
        conn = _get_conn()
        cursor = conn.execute("DELETE FROM watches WHERE id = ?", (int(watch_id),))
        # ON DELETE CASCADE handles events/seen when foreign keys are on; be explicit anyway.
        conn.execute("DELETE FROM watch_events WHERE watch_id = ?", (int(watch_id),))
        conn.execute("DELETE FROM watch_seen WHERE watch_id = ?", (int(watch_id),))
        conn.commit()
        return cursor.rowcount > 0


def update_run_state(
    watch_id: int,
    *,
    last_run_at: str | None = None,
    last_error: str | None = None,
    last_summary: str | None = None,
) -> None:
    assignments = []
    values: list[Any] = []
    if last_run_at is not None:
        assignments.append("last_run_at = ?")
        values.append(last_run_at)
    if last_error is not None:
        assignments.append("last_error = ?")
        values.append(last_error)
    if last_summary is not None:
        assignments.append("last_summary = ?")
        values.append(last_summary)
    if not assignments:
        return
    values.append(int(watch_id))
    with _lock:
        conn = _get_conn()
        conn.execute(f"UPDATE watches SET {', '.join(assignments)} WHERE id = ?", values)
        conn.commit()


# --------------------------------------------------------------------------- dedupe table
def mark_seen(watch_id: int, mam_id, grabbed: bool, *, title: str | None = None) -> None:
    mam_id = str(mam_id)
    with _lock:
        conn = _get_conn()
        conn.execute(
            """
            INSERT INTO watch_seen (watch_id, mam_id, first_seen_at, grabbed, title)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(watch_id, mam_id) DO UPDATE SET
                grabbed = MAX(watch_seen.grabbed, excluded.grabbed),
                title = COALESCE(excluded.title, watch_seen.title)
            """,
            (int(watch_id), mam_id, utc_now_iso(), 1 if grabbed else 0, title),
        )
        conn.commit()


def mark_many_seen(watch_id: int, items: Iterable[dict | str | int], grabbed: bool = False) -> int:
    count = 0
    for item in items:
        if isinstance(item, dict):
            mam_id = item.get("id") or item.get("mam_id")
            title = item.get("title")
        else:
            mam_id, title = item, None
        if mam_id in (None, ""):
            continue
        mark_seen(watch_id, mam_id, grabbed, title=title)
        count += 1
    return count


def is_seen(watch_id: int, mam_id) -> bool:
    with _lock:
        row = _get_conn().execute(
            "SELECT 1 FROM watch_seen WHERE watch_id = ? AND mam_id = ?",
            (int(watch_id), str(mam_id)),
        ).fetchone()
    return row is not None


def seen_ids(watch_id: int) -> set[str]:
    with _lock:
        rows = _get_conn().execute(
            "SELECT mam_id FROM watch_seen WHERE watch_id = ?", (int(watch_id),)
        ).fetchall()
    return {str(row[0]) for row in rows}


def list_seen(watch_id: int) -> list[dict]:
    with _lock:
        rows = _get_conn().execute(
            "SELECT mam_id, first_seen_at, grabbed, title FROM watch_seen WHERE watch_id = ? "
            "ORDER BY first_seen_at DESC, mam_id DESC",
            (int(watch_id),),
        ).fetchall()
    return [
        {"mam_id": row[0], "first_seen_at": row[1], "grabbed": bool(row[2]), "title": row[3]}
        for row in rows
    ]


def clear_seen(watch_id: int, mam_id=None) -> int:
    with _lock:
        conn = _get_conn()
        if mam_id is None:
            cursor = conn.execute("DELETE FROM watch_seen WHERE watch_id = ?", (int(watch_id),))
        else:
            cursor = conn.execute(
                "DELETE FROM watch_seen WHERE watch_id = ? AND mam_id = ?", (int(watch_id), str(mam_id))
            )
        conn.commit()
        return cursor.rowcount


# --------------------------------------------------------------------------- events
def log_event(watch_id: int, kind: str, *, mam_id=None, title: str | None = None, detail: str | None = None) -> None:
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown watch event kind: {kind}")
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO watch_events (watch_id, ts, kind, mam_id, title, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (
                int(watch_id),
                utc_now_iso(),
                kind,
                None if mam_id in (None, "") else str(mam_id),
                title,
                detail,
            ),
        )
        conn.commit()


def events_for(watch_id: int, limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit or 50), MAX_EVENTS_PER_WATCH))
    with _lock:
        rows = _get_conn().execute(
            "SELECT id, watch_id, ts, kind, mam_id, title, detail FROM watch_events "
            "WHERE watch_id = ? ORDER BY id DESC LIMIT ?",
            (int(watch_id), limit),
        ).fetchall()
    return [dict(row) for row in rows]


def prune_events(watch_id: int, keep: int = MAX_EVENTS_PER_WATCH) -> int:
    with _lock:
        conn = _get_conn()
        cursor = conn.execute(
            """
            DELETE FROM watch_events WHERE watch_id = ? AND id NOT IN (
                SELECT id FROM watch_events WHERE watch_id = ? ORDER BY id DESC LIMIT ?
            )
            """,
            (int(watch_id), int(watch_id), int(keep)),
        )
        conn.commit()
        return cursor.rowcount
