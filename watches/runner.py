"""
Watch runner: search -> regex filter -> dedupe -> cap -> grab.

The runner never imports ``app`` (that would be circular); instead ``app.py``
injects the callables it needs via ``configure(...)``. Tests inject fakes.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from . import store

logger = logging.getLogger("watches")
# app.py injects its own logger; this keeps the bare module quiet under unittest.
logger.addHandler(logging.NullHandler())

DEFAULT_PAUSE_SECONDS = 5
# Newest first so the per-run cap keeps the most recent releases.
WATCH_SORT_TYPE = "dateDesc"
# How far before last_run_at to look, belt and braces on top of the dedupe table.
LOOKBACK_DAYS = 1


async def _noop_async(*_args, **_kwargs):
    return None


def _default_config(_key: str, default=None):
    return default


deps = SimpleNamespace(
    build_params=None,            # (opts: dict, *, perpage=None) -> dict
    search=None,                  # async (params: dict) -> list[dict]
    add=None,                     # async (item, *, category, custom_relative_path, custom_destination_path) -> dict
    login=None,                   # async () -> bool
    notify=_noop_async,           # async (event, success, **details)
    toast=_noop_async,            # async (message, category)
    logger=logger,
    config=_default_config,       # (key, default) -> value
)


def configure(**overrides) -> None:
    """Inject the app callables the runner relies on (see ``deps``)."""
    for key, value in overrides.items():
        if not hasattr(deps, key):
            raise KeyError(f"unknown runner dependency: {key}")
        setattr(deps, key, value)


def _require(name: str) -> Callable[..., Any]:
    fn = getattr(deps, name, None)
    if fn is None:
        raise RuntimeError(f"watch runner dependency '{name}' is not configured")
    return fn


# --------------------------------------------------------------------------- helpers
def watch_to_search_opts(watch: dict) -> dict:
    """Translate a stored watch into the option dict ``build_mam_search_params`` accepts."""
    fields = set(watch.get("search_fields") or ["title"])
    opts: dict[str, Any] = {
        "query": watch.get("query") or "",
        "search_in_title": "title" in fields,
        "search_in_author": "author" in fields,
        "search_in_series": "series" in fields,
        "search_in_narrator": "narrator" in fields,
        "search_in_description": "description" in fields,
        "search_in_tags": "tags" in fields,
        "search_in_filenames": "filenames" in fields,
        "language_ids": list(watch.get("language_ids") or []),
        "main_cat": list(watch.get("main_cat") or []),
        "category_ids": list(watch.get("category_ids") or []),
        "flag_ids": list(watch.get("flag_ids") or []),
        "flags_mode": watch.get("flags_mode") or "0",
        "searchType": watch.get("search_type") or "all",
        "sort_type": WATCH_SORT_TYPE,
    }
    if watch.get("min_seeders") not in (None, ""):
        opts["min_seeders"] = str(watch["min_seeders"])
    start_date = compute_start_date(watch.get("last_run_at"))
    if start_date:
        opts["start_date"] = start_date
    return opts


def compute_start_date(last_run_at: str | None, *, lookback_days: int = LOOKBACK_DAYS) -> str | None:
    last_run = store.parse_iso(last_run_at)
    if last_run is None:
        return None
    return (last_run - timedelta(days=lookback_days)).date().isoformat()


def is_due(watch: dict, now: datetime | None = None) -> bool:
    if not watch.get("enabled"):
        return False
    now = now or datetime.now(timezone.utc)
    last_run = store.parse_iso(watch.get("last_run_at"))
    if last_run is None:
        return True
    interval = max(1, int(watch.get("interval_minutes") or 60))
    return now - last_run >= timedelta(minutes=interval)


def _added_sort_key(item: dict) -> str:
    return str(item.get("added") or "")


def summarize(summary: dict) -> str:
    parts = [
        f"checked {summary.get('checked', 0)}",
        f"matched {summary.get('matched', 0)}",
    ]
    if summary.get("dry_run"):
        parts.append(f"would grab {summary.get('would_grab', 0)}")
    else:
        parts.append(f"grabbed {summary.get('grabbed', 0)}")
    if summary.get("skipped_seen"):
        parts.append(f"seen {summary['skipped_seen']}")
    if summary.get("skipped_cap"):
        parts.append(f"capped {summary['skipped_cap']}")
    if summary.get("errors"):
        parts.append(f"errors {summary['errors']}")
    return ", ".join(parts)


# --------------------------------------------------------------------------- core
async def run_watch(watch: dict, *, dry_run: bool) -> dict:
    """
    Run one watch. With ``dry_run=True`` nothing is grabbed, nothing is marked
    seen and no events or run state are written, so a watch can be proven
    before it is armed. ``watch`` may be unsaved (no ``id``) for previews.

    Returns a summary::

        {checked, matched, grabbed, would_grab, skipped_seen, skipped_cap, errors,
         error, items: [{id, title, added, size, seeders, matched, seen, action, detail}]}
    """
    watch_id = watch.get("id")
    persist = not dry_run and watch_id not in (None, "", 0)
    name = watch.get("name") or f"watch {watch_id}"
    log = deps.logger

    summary: dict[str, Any] = {
        "watch_id": watch_id,
        "name": name,
        "dry_run": dry_run,
        "started_at": store.utc_now_iso(),
        "checked": 0,
        "matched": 0,
        "grabbed": 0,
        "would_grab": 0,
        "skipped_seen": 0,
        "skipped_cap": 0,
        "errors": 0,
        "error": None,
        "items": [],
    }

    def record(kind: str, **kwargs):
        if persist:
            try:
                store.log_event(watch_id, kind, **kwargs)
            except Exception as exc:  # pragma: no cover - logging must never break a run
                log.warning(f"[WATCH] Failed to log {kind} event for '{name}': {exc}")

    def finish(error_text: str | None = None):
        summary["finished_at"] = store.utc_now_iso()
        summary["summary_text"] = summarize(summary)
        if persist:
            store.update_run_state(
                watch_id,
                last_run_at=summary["finished_at"],
                last_error=error_text or "",
                last_summary=summary["summary_text"],
            )
            try:
                store.prune_events(watch_id)
            except Exception:  # pragma: no cover
                pass
        return summary

    # 1. Search
    try:
        pattern = re.compile(watch.get("title_regex") or "", re.IGNORECASE)
        login = deps.login
        if login is not None and not await login():
            raise RuntimeError("MAM login failed (check the mam_id cookie in Settings)")
        params = _require("build_params")(watch_to_search_opts(watch))
        results = await _require("search")(params)
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        log.error(f"[WATCH] '{name}' search failed: {message}")
        summary["error"] = message
        summary["errors"] += 1
        record("error", detail=f"search failed: {message}")
        return finish(message)

    results = list(results or [])
    summary["checked"] = len(results)
    record("checked", detail=f"{len(results)} result(s) for query '{watch.get('query') or ''}'")

    seen = store.seen_ids(watch_id) if watch_id not in (None, "", 0) else set()

    # 2. Filter + dedupe
    candidates: list[tuple[dict, dict]] = []
    for item in results:
        title = str(item.get("title") or "")
        mam_id = str(item.get("id") or "")
        entry = {
            "id": mam_id,
            "title": title,
            "added": item.get("added"),
            "size": item.get("size"),
            "seeders": item.get("seeders"),
            "matched": False,
            "seen": False,
            "action": "no_match",
            "detail": "",
        }
        summary["items"].append(entry)

        if not pattern.search(title):
            continue
        entry["matched"] = True
        summary["matched"] += 1

        if mam_id in seen:
            entry["seen"] = True
            entry["action"] = "skipped_seen"
            entry["detail"] = "already recorded for this watch"
            summary["skipped_seen"] += 1
            continue

        if str(item.get("my_snatched") or "0") == "1":
            # MAM says the account already has this torrent: treat it as seen so
            # a fresh watch never re-grabs the back-catalog.
            entry["seen"] = True
            entry["action"] = "skipped_seen"
            entry["detail"] = "already snatched on MAM"
            summary["skipped_seen"] += 1
            if persist:
                store.mark_seen(watch_id, mam_id, False, title=title)
                record("skipped_seen", mam_id=mam_id, title=title, detail=entry["detail"])
            continue

        record("matched", mam_id=mam_id, title=title)
        candidates.append((item, entry))

    # 3. Cap (newest first)
    candidates.sort(key=lambda pair: _added_sort_key(pair[0]), reverse=True)
    cap = max(1, int(watch.get("max_grabs_per_run") or 1))
    for item, entry in candidates[cap:]:
        entry["action"] = "skipped_cap"
        entry["detail"] = f"over the per-run cap of {cap}; will retry next run"
        summary["skipped_cap"] += 1
        record("skipped_cap", mam_id=entry["id"], title=entry["title"], detail=entry["detail"])

    # 4. Grab
    last_error = ""
    for item, entry in candidates[:cap]:
        if dry_run:
            entry["action"] = "would_grab"
            summary["would_grab"] += 1
            continue

        try:
            result = await _require("add")(
                item,
                category=watch.get("torrent_category") or "",
                custom_relative_path=watch.get("custom_relative_path") or None,
                custom_destination_path=watch.get("custom_destination_path") or None,
            )
        except Exception as exc:
            result = {"error": str(exc) or exc.__class__.__name__}

        result = result or {}
        failed = bool(result.get("error")) or result.get("status") == "insufficient_buffer"
        if failed:
            message = result.get("error") or result.get("message") or "unknown error"
            entry["action"] = "error"
            entry["detail"] = message
            summary["errors"] += 1
            last_error = message
            log.warning(f"[WATCH] '{name}' failed to grab {entry['id']} '{entry['title']}': {message}")
            record("error", mam_id=entry["id"], title=entry["title"], detail=message)
            continue

        entry["action"] = "grabbed"
        entry["detail"] = result.get("message") or "added to client"
        summary["grabbed"] += 1
        if persist:
            store.mark_seen(watch_id, entry["id"], True, title=entry["title"])
        record("grabbed", mam_id=entry["id"], title=entry["title"], detail=entry["detail"])
        log.info(f"[WATCH] '{name}' grabbed {entry['id']} '{entry['title']}'")
        try:
            await deps.notify(
                "watch_grabbed",
                True,
                task="watch",
                watch=name,
                title=entry["title"],
                mid=entry["id"],
                hash=result.get("hash"),
                message=entry["detail"],
            )
        except Exception as exc:  # pragma: no cover
            log.warning(f"[WATCH] notification failed: {exc}")
        try:
            await deps.toast(f"Watch '{name}' grabbed: {entry['title']}", "success")
        except Exception:  # pragma: no cover
            pass

    return finish(last_error or None)


async def run_all_watches(*, pause_seconds: float | None = None) -> list[dict]:
    """
    Scheduler tick: run every enabled watch whose interval has elapsed, spacing
    the MAM requests out and isolating failures so one watch cannot stop the rest.
    """
    if pause_seconds is None:
        try:
            pause_seconds = float(deps.config("WATCHES_PAUSE_SECONDS", DEFAULT_PAUSE_SECONDS))
        except (TypeError, ValueError):
            pause_seconds = DEFAULT_PAUSE_SECONDS

    try:
        watches = store.list_watches()
    except Exception as exc:
        deps.logger.error(f"[WATCH] Unable to load watches: {exc}")
        return []

    now = datetime.now(timezone.utc)
    due = [watch for watch in watches if is_due(watch, now)]
    if not due:
        deps.logger.debug(f"[WATCH] Tick: {len(watches)} watch(es), none due")
        return []

    deps.logger.info(f"[WATCH] Tick: {len(due)} of {len(watches)} watch(es) due")
    summaries = []
    for index, watch in enumerate(due):
        if index > 0 and pause_seconds > 0:
            await asyncio.sleep(pause_seconds)
        try:
            summary = await run_watch(watch, dry_run=False)
            summaries.append(summary)
            deps.logger.info(f"[WATCH] '{watch.get('name')}': {summary.get('summary_text')}")
        except Exception as exc:
            deps.logger.error(f"[WATCH] '{watch.get('name')}' crashed: {exc}", exc_info=True)
            try:
                store.log_event(watch["id"], "error", detail=f"run crashed: {exc}")
                store.update_run_state(watch["id"], last_run_at=store.utc_now_iso(), last_error=str(exc))
            except Exception:
                pass
    return summaries
